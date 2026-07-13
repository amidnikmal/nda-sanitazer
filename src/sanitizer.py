from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable
from urllib.parse import urlparse
import uuid

import requests
import yaml


PLACEHOLDER_RE = re.compile(r"\[\s*([A-Z]+)\s*_\s*(\d+)\s*\]", re.IGNORECASE)
CANONICAL_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+_\d+\]")
VAULT_ID_RE = re.compile(r"^[0-9a-f]{32}$")

DICTIONARY_CATEGORIES = {
    "project_names": "PROJECT",
    "service_names": "SERVICE",
    "internal_domains": "DOMAIN",
    "db_schemas": "SCHEMA",
    "people": "PERSON",
    "business_terms": "BUSINESS",
}

PII_CATEGORIES = {
    "EMAIL_ADDRESS": "EMAIL",
    "IP_ADDRESS": "IP",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CARD",
    "CRYPTO": "CRYPTO",
    "IBAN_CODE": "IBAN",
    "LOCATION": "LOCATION",
    "PERSON": "PERSON",
    "DATE_TIME": "DATETIME",
    "NRP": "NRP",
    "MEDICAL_LICENSE": "MEDLICENSE",
    "URL": "URL",
}

BUILTIN_SECRET_PATTERNS = {
    "AWS access key": re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "assigned credential": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)\b"
        r"\s*[:=]\s*[\"']?[A-Za-z0-9+/_.=-]{8,}"
    ),
}

DETECT_SECRETS_PLUGINS = [
    "ArtifactoryDetector",
    "AWSKeyDetector",
    "AzureStorageKeyDetector",
    "BasicAuthDetector",
    "CloudantDetector",
    "DiscordBotTokenDetector",
    "GitHubTokenDetector",
    "GitLabTokenDetector",
    "IbmCloudIamDetector",
    "IbmCosHmacDetector",
    "JwtTokenDetector",
    "MailchimpDetector",
    "NpmDetector",
    "OpenAIDetector",
    "PrivateKeyDetector",
    "PypiTokenDetector",
    "SendGridDetector",
    "SlackDetector",
    "SoftlayerDetector",
    "SquareOAuthDetector",
    "StripeDetector",
    "TelegramBotTokenDetector",
    "TwilioKeyDetector",
]


class SanitizerError(RuntimeError):
    """Base error which is safe to show without exposing protected values."""


class SecretDetectedError(SanitizerError):
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = findings
        super().__init__(
            f"blocked by secret scanner: {len(findings)} potential secret(s) detected"
        )


class VaultBuilder:
    def __init__(self, reserved_text: str = "") -> None:
        self.replacements: dict[str, str] = {}
        self._by_value: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self._reserved_text = reserved_text

    def placeholder_for(self, category: str, value: str) -> str:
        key = (category, value)
        existing = self._by_value.get(key)
        if existing:
            return existing

        counter = self._counters.get(category, 0)
        while True:
            counter += 1
            placeholder = f"[{category}_{counter}]"
            if placeholder not in self._reserved_text and placeholder not in self.replacements:
                break

        self._counters[category] = counter
        self._by_value[key] = placeholder
        self.replacements[placeholder] = value
        return placeholder


class Sanitizer:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        require_llm: bool = True,
        enable_pii: bool = True,
        llm_base_url: str | None = None,
    ) -> None:
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.config_path = self.root / "config" / "nda_terms.yaml"
        self.vault_dir = self.root / "vaults"
        self.log_dir = self.root / "logs"
        self.require_llm = require_llm
        self.enable_pii = enable_pii
        self.llm_base_url = (llm_base_url or os.getenv(
            "NDA_SANITIZER_LLM_URL", "http://127.0.0.1:8080"
        )).rstrip("/")
        self._validate_local_llm_url()
        self._analyzer: Any | None = None
        self._model_id: str | None = None
        self._session = requests.Session()
        self._session.trust_env = False

        self.vault_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.vault_dir, 0o700)
        os.chmod(self.log_dir, 0o700)
        self.logger = self._build_logger()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"nda_sanitizer.{self.root}")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(
            self.log_dir / "sanitizer.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        return logger

    def _validate_local_llm_url(self) -> None:
        parsed = urlparse(self.llm_base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise SanitizerError("LLM endpoint must be an HTTP loopback address")

    def sanitize(self, text: str) -> tuple[str, str]:
        clean_text, builder = self._process(text)
        vault_id = uuid.uuid4().hex
        self._save_vault(vault_id, builder.replacements)
        counts = self._category_counts(builder.replacements)
        self.logger.info(
            "sanitize vault_id=%s replacement_count=%d categories=%s",
            vault_id,
            len(builder.replacements),
            json.dumps(counts, sort_keys=True),
        )
        return clean_text, vault_id

    def restore(self, text: str, vault_id: str) -> str:
        replacements = self._load_vault(vault_id)

        def replace(match: re.Match[str]) -> str:
            canonical = f"[{match.group(1).upper()}_{int(match.group(2))}]"
            return replacements.get(canonical, match.group(0))

        restored = PLACEHOLDER_RE.sub(replace, text)
        self.logger.info(
            "restore vault_id=%s matched_placeholder_count=%d",
            vault_id,
            len(PLACEHOLDER_RE.findall(text)),
        )
        return restored

    def check(self, text: str) -> dict[str, Any]:
        findings = self._scan_secrets(text)
        if findings:
            report = {
                "blocked": True,
                "secret_findings": findings,
                "replacement_count": 0,
                "categories": {},
            }
            self.logger.info("check blocked secret_count=%d", len(findings))
            return report

        _, builder = self._process(text, secrets_already_checked=True)
        counts = self._category_counts(builder.replacements)
        report = {
            "blocked": False,
            "secret_findings": [],
            "replacement_count": len(builder.replacements),
            "categories": counts,
            "placeholders": list(builder.replacements),
        }
        self.logger.info(
            "check replacement_count=%d categories=%s",
            len(builder.replacements),
            json.dumps(counts, sort_keys=True),
        )
        return report

    def _process(
        self, text: str, *, secrets_already_checked: bool = False
    ) -> tuple[str, VaultBuilder]:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not secrets_already_checked:
            findings = self._scan_secrets(text)
            if findings:
                self.logger.warning("sanitize blocked secret_count=%d", len(findings))
                raise SecretDetectedError(findings)

        builder = VaultBuilder(text)
        clean = self._replace_dictionary(text, builder)
        if self.enable_pii:
            clean = self._replace_pii(clean, builder)
        if self.require_llm:
            clean = self._replace_llm_entities(clean, builder)
        return clean, builder

    def _scan_secrets(self, text: str) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()

        for name, pattern in BUILTIN_SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                key = (name, line)
                if key not in seen:
                    findings.append({"type": name, "line": line})
                    seen.add(key)

        try:
            from detect_secrets.core.scan import scan_line
            from detect_secrets.settings import transient_settings

            with transient_settings(
                {"plugins": [{"name": name} for name in DETECT_SECRETS_PLUGINS]}
            ):
                for line_number, line in enumerate(text.splitlines() or [text], start=1):
                    for secret in scan_line(line):
                        secret_type = str(
                            getattr(secret, "type", secret.__class__.__name__)
                        )
                        key = (secret_type, line_number)
                        if key not in seen:
                            findings.append({"type": secret_type, "line": line_number})
                            seen.add(key)
        except ImportError as exc:
            raise SanitizerError("detect-secrets is not installed") from exc

        return findings

    def _load_terms(self) -> list[tuple[str, str]]:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise SanitizerError("unable to load NDA term dictionary") from exc

        if not isinstance(data, dict):
            raise SanitizerError("NDA term dictionary must be a mapping")

        terms: list[tuple[str, str]] = []
        for yaml_category, placeholder_category in DICTIONARY_CATEGORIES.items():
            values = data.get(yaml_category, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise SanitizerError(f"dictionary category {yaml_category} must be a string list")
            for value in values:
                if value:
                    terms.append((value, placeholder_category))
        terms.sort(key=lambda item: len(item[0]), reverse=True)
        return terms

    def _replace_dictionary(self, text: str, builder: VaultBuilder) -> str:
        spans: list[tuple[int, int, str]] = []
        for term, category in self._load_terms():
            spans.extend((m.start(), m.end(), category) for m in re.finditer(re.escape(term), text))
        return self._replace_spans(text, spans, builder)

    def _get_analyzer(self) -> Any:
        if self._analyzer is not None:
            return self._analyzer
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            configuration = {
                "nlp_engine_name": "spacy",
                "models": [
                    {"lang_code": "en", "model_name": "en_core_web_lg"},
                    {"lang_code": "ru", "model_name": "ru_core_news_lg"},
                ],
            }
            provider = NlpEngineProvider(nlp_configuration=configuration)
            engine = provider.create_engine()
            self._analyzer = AnalyzerEngine(
                nlp_engine=engine,
                supported_languages=["en", "ru"],
            )
        except (ImportError, OSError, ValueError) as exc:
            raise SanitizerError("unable to initialize Presidio en/ru models") from exc
        return self._analyzer

    def _replace_pii(self, text: str, builder: VaultBuilder) -> str:
        analyzer = self._get_analyzer()
        spans: list[tuple[int, int, str]] = []
        for language in ("en", "ru"):
            try:
                results = analyzer.analyze(text=text, language=language)
            except Exception as exc:  # Presidio can wrap model-specific failures.
                raise SanitizerError(f"Presidio analysis failed for language {language}") from exc
            for result in results:
                category = PII_CATEGORIES.get(result.entity_type)
                if category:
                    spans.append((result.start, result.end, category))
        return self._replace_spans(text, spans, builder)

    def _replace_llm_entities(self, text: str, builder: VaultBuilder) -> str:
        candidates = self._llm_candidates(text)
        spans: list[tuple[int, int, str]] = []
        for candidate in sorted(candidates, key=len, reverse=True):
            spans.extend(
                (match.start(), match.end(), "SENSITIVE")
                for match in re.finditer(re.escape(candidate), text)
            )
        return self._replace_spans(text, spans, builder)

    def _llm_candidates(self, text: str) -> list[str]:
        model_id = self._get_model_id()
        schema = {
            "type": "array",
            "items": {"type": "string"},
        }
        payload = {
            "model": model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a local privacy classifier. Identify exact substrings that are "
                        "names of companies, projects, internal systems, products, people, or "
                        "other organization-specific entities. Never return placeholders such as "
                        "[PROJECT_1]. Return a JSON array of exact, case-sensitive substrings from "
                        "the input. Return [] if there are none. /no_think"
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0,
            "max_tokens": 256,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "sensitive_entities",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        try:
            response = self._session.post(
                f"{self.llm_base_url}/v1/chat/completions",
                json=payload,
                timeout=(3, 120),
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = self._parse_json_array(content)
        except (requests.RequestException, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SanitizerError("local LLM entity pass failed") from exc

        candidates: list[str] = []
        for value in parsed:
            if not isinstance(value, str):
                continue
            if value != value.strip() or not 2 <= len(value) <= 160:
                continue
            if "\n" in value or CANONICAL_PLACEHOLDER_RE.search(value):
                continue
            if value not in text or not any(character.isalnum() for character in value):
                continue
            if value not in candidates:
                candidates.append(value)
        return candidates

    def _get_model_id(self) -> str:
        if self._model_id:
            return self._model_id
        try:
            response = self._session.get(f"{self.llm_base_url}/v1/models", timeout=(3, 10))
            response.raise_for_status()
            self._model_id = str(response.json()["data"][0]["id"])
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise SanitizerError("local llama-server is unavailable") from exc
        return self._model_id

    @staticmethod
    def _parse_json_array(content: str) -> list[Any]:
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
        start = content.find("[")
        end = content.rfind("]")
        if start < 0 or end < start:
            raise ValueError("LLM response does not contain a JSON array")
        parsed = json.loads(content[start : end + 1])
        if not isinstance(parsed, list):
            raise ValueError("LLM response is not a JSON array")
        return parsed

    @staticmethod
    def _replace_spans(
        text: str,
        spans: Iterable[tuple[int, int, str]],
        builder: VaultBuilder,
    ) -> str:
        protected = [(match.start(), match.end()) for match in CANONICAL_PLACEHOLDER_RE.finditer(text)]

        candidates: list[tuple[int, int, str]] = []
        for start, end, category in spans:
            if start < 0 or end > len(text) or start >= end:
                continue
            if any(start < protected_end and end > protected_start for protected_start, protected_end in protected):
                continue
            candidates.append((start, end, category))

        # Sort by start, longest first at each start, then merge every overlapping
        # run into a single region. Two dictionary/PII spans can partially overlap
        # (for example "Alex Example" and "Example Revenue Formula" sharing the word
        # "Example"). Dropping the later span would leave its exclusive tail
        # ("Revenue Formula") in the clean text, so instead the whole overlapping
        # region is masked as one placeholder. The category is taken from the
        # longest contributing span. Adjacent, non-overlapping spans stay separate.
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        merged: list[list[Any]] = []
        for start, end, category in candidates:
            if merged and start < merged[-1][1]:
                group = merged[-1]
                if end > group[1]:
                    group[1] = end
                if end - start > group[3]:
                    group[2] = category
                    group[3] = end - start
            else:
                merged.append([start, end, category, end - start])

        replacements: list[tuple[int, int, str]] = []
        for start, end, category, _ in merged:
            placeholder = builder.placeholder_for(category, text[start:end])
            replacements.append((start, end, placeholder))

        output = text
        for start, end, placeholder in reversed(replacements):
            output = output[:start] + placeholder + output[end:]
        return output

    def _save_vault(self, vault_id: str, replacements: dict[str, str]) -> None:
        payload = {
            "version": 1,
            "vault_id": vault_id,
            "replacements": replacements,
        }
        fd, temporary_path = tempfile.mkstemp(prefix=".vault-", dir=self.vault_dir)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.vault_dir / f"{vault_id}.json")
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise

    def _load_vault(self, vault_id: str) -> dict[str, str]:
        if not VAULT_ID_RE.fullmatch(vault_id):
            raise SanitizerError("invalid vault id")
        try:
            payload = json.loads(
                (self.vault_dir / f"{vault_id}.json").read_text(encoding="utf-8")
            )
            replacements = payload["replacements"]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise SanitizerError("unable to load vault") from exc
        if not isinstance(replacements, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in replacements.items()
        ):
            raise SanitizerError("vault has an invalid replacement mapping")
        return replacements

    @staticmethod
    def _category_counts(replacements: dict[str, str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for placeholder in replacements:
            match = CANONICAL_PLACEHOLDER_RE.fullmatch(placeholder)
            if match:
                category = placeholder[1 : placeholder.rfind("_")]
                counts[category] = counts.get(category, 0) + 1
        return counts


def _read_text(argument: str | None) -> str:
    return argument if argument is not None else sys.stdin.read()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nda-sanitizer")
    parser.add_argument("--root", type=Path, help="project root (defaults to package root)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sanitize_parser = subparsers.add_parser("sanitize", help="sanitize text and create a vault")
    sanitize_parser.add_argument("text", nargs="?", help="text; reads stdin when omitted")

    restore_parser = subparsers.add_parser("restore", help="restore placeholders from a vault")
    restore_parser.add_argument("vault_id")
    restore_parser.add_argument("text", nargs="?", help="text; reads stdin when omitted")

    check_parser = subparsers.add_parser("check", help="dry-run without creating a vault")
    check_parser.add_argument("text", nargs="?", help="text; reads stdin when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sanitizer = Sanitizer(root=args.root)
    try:
        if args.command == "sanitize":
            clean_text, vault_id = sanitizer.sanitize(_read_text(args.text))
            print(json.dumps(
                {"clean_text": clean_text, "vault_id": vault_id},
                ensure_ascii=False,
            ))
        elif args.command == "restore":
            print(sanitizer.restore(_read_text(args.text), args.vault_id), end="")
        elif args.command == "check":
            print(json.dumps(sanitizer.check(_read_text(args.text)), ensure_ascii=False, indent=2))
    except SanitizerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

