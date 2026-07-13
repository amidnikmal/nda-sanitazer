import json
import os
from pathlib import Path

import pytest
import requests

from src.sanitizer import Sanitizer, SecretDetectedError, VaultBuilder


def test_round_trip_is_byte_exact(deterministic_sanitizer: Sanitizer) -> None:
    original = "SecretProjectX uses InternalServiceAlpha.\nСтрока в UTF-8.\n"

    clean, vault_id = deterministic_sanitizer.sanitize(original)

    assert deterministic_sanitizer.restore(clean, vault_id).encode() == original.encode()


def test_repeated_term_gets_one_placeholder(deterministic_sanitizer: Sanitizer) -> None:
    clean, vault_id = deterministic_sanitizer.sanitize(
        "SecretProjectX calls SecretProjectX twice."
    )

    assert clean.count("[PROJECT_1]") == 2
    vault = json.loads(
        (deterministic_sanitizer.vault_dir / f"{vault_id}.json").read_text()
    )
    assert vault["replacements"] == {"[PROJECT_1]": "SecretProjectX"}


def test_dictionary_term_does_not_leak(deterministic_sanitizer: Sanitizer) -> None:
    clean, _ = deterministic_sanitizer.sanitize(
        "Deploy SecretProjectX through MercuryBuildBus."
    )

    assert "SecretProjectX" not in clean
    assert "MercuryBuildBus" not in clean


def test_overlapping_dictionary_terms_do_not_leak(deterministic_sanitizer: Sanitizer) -> None:
    # "Alex Example" (people) and "Example Revenue Formula" (business_terms) share
    # the word "Example". The overlapping region must be fully masked instead of
    # leaving either term's exclusive tail in the clean text.
    original = "Alex Example Revenue Formula"

    clean, vault_id = deterministic_sanitizer.sanitize(original)

    assert "Alex Example" not in clean
    assert "Example Revenue Formula" not in clean
    assert "Revenue Formula" not in clean
    assert deterministic_sanitizer.restore(clean, vault_id) == original


@pytest.mark.parametrize(
    "separator",
    [" ", " ", "  ", "\t", "\n"],
    ids=["nbsp", "narrow-nbsp", "double-space", "tab", "newline"],
)
def test_dictionary_matches_whitespace_variants(
    deterministic_sanitizer: Sanitizer, separator: str
) -> None:
    # Pasted text often separates the tokens of a term with a non-breaking or
    # doubled space; the exact-string match used to miss those and leak the term.
    original = f"Contact Alex{separator}Example soon"

    clean, vault_id = deterministic_sanitizer.sanitize(original)

    assert "Alex" not in clean
    assert "Example" not in clean
    assert deterministic_sanitizer.restore(clean, vault_id) == original


def test_case_insensitive_matching_is_opt_in(project_root: Path) -> None:
    case_sensitive = Sanitizer(root=project_root, require_llm=False, enable_pii=False)
    clean, _ = case_sensitive.sanitize("secretprojectx")
    assert clean == "secretprojectx"  # documented default: exact case

    folded = Sanitizer(
        root=project_root, require_llm=False, enable_pii=False, case_insensitive=True
    )
    clean, vault_id = folded.sanitize("secretprojectx")
    assert "secretprojectx" not in clean
    assert folded.restore(clean, vault_id) == "secretprojectx"


def test_dictionary_term_whitespace_is_stripped(tmp_path: Path) -> None:
    root = tmp_path / "nda-sanitizer"
    (root / "config").mkdir(parents=True)
    (root / "vaults").mkdir()
    (root / "logs").mkdir()
    (root / "config" / "nda_terms.yaml").write_text(
        'project_names:\n  - "SecretProjectX "\n', encoding="utf-8"
    )
    sanitizer = Sanitizer(root=root, require_llm=False, enable_pii=False)

    clean, vault_id = sanitizer.sanitize("Use SecretProjectX.")

    assert "SecretProjectX" not in clean
    assert sanitizer.restore(clean, vault_id) == "Use SecretProjectX."


def test_placeholder_lookalike_in_source_round_trips(
    deterministic_sanitizer: Sanitizer,
) -> None:
    # restore() canonicalizes tolerant spellings, so a lookalike already present
    # in the source must not collide with an id we assign to a sanitized value.
    original = "step [ project _ 1 ] of SecretProjectX"

    clean, vault_id = deterministic_sanitizer.sanitize(original)

    assert deterministic_sanitizer.restore(clean, vault_id) == original


def test_span_is_clipped_around_existing_placeholder(
    deterministic_sanitizer: Sanitizer,
) -> None:
    text = "John SecretProjectX"
    builder = VaultBuilder(text)
    after_dictionary = deterministic_sanitizer._replace_dictionary(text, builder)

    # A later layer flags the whole "John [PROJECT_1]" region as one entity.
    result = deterministic_sanitizer._replace_spans(
        after_dictionary, [(0, len(after_dictionary), "PERSON")], builder
    )

    assert "John" not in result
    assert "[PROJECT_1]" in result


@pytest.mark.parametrize(
    "content,expected",
    [
        ('["Velmorix Quasar", "Internal', ["Velmorix Quasar"]),
        ('["array[0] of X", "Bet', ["array[0] of X"]),
        ('["A", "B"]', ["A", "B"]),
        ("<think>x</think>```json\n[\"A\"]\n```", ["A"]),
    ],
    ids=["truncated", "bracket-in-string", "normal", "fenced-with-think"],
)
def test_parse_json_array_salvages_truncated_output(content, expected) -> None:
    assert Sanitizer._parse_json_array(content) == expected


def test_clean_candidates_strips_and_recovers_case() -> None:
    text = "Velmorix Quasar must stay secret"

    assert Sanitizer._clean_candidates(["Velmorix Quasar "], text) == ["Velmorix Quasar"]
    assert Sanitizer._clean_candidates(["velmorix quasar"], text) == ["Velmorix Quasar"]
    assert Sanitizer._clean_candidates(["[PROJECT_1]"], text) == []


def test_chunk_text_covers_entire_input(deterministic_sanitizer: Sanitizer) -> None:
    assert deterministic_sanitizer._chunk_text("short") == ["short"]

    big = "x" * (deterministic_sanitizer._LLM_CHUNK_CHARS * 3)
    chunks = deterministic_sanitizer._chunk_text(big)
    step = (
        deterministic_sanitizer._LLM_CHUNK_CHARS
        - deterministic_sanitizer._LLM_CHUNK_OVERLAP
    )
    covered: set[int] = set()
    for index, chunk in enumerate(chunks):
        covered.update(range(index * step, index * step + len(chunk)))
    assert covered.issuperset(range(len(big)))


def test_secret_stops_before_sanitization(deterministic_sanitizer: Sanitizer) -> None:
    with pytest.raises(SecretDetectedError):
        deterministic_sanitizer.sanitize(
            "Synthetic credential: AKIA1234567890ABCDEF"
        )


def test_restore_tolerates_placeholder_spacing(deterministic_sanitizer: Sanitizer) -> None:
    clean, vault_id = deterministic_sanitizer.sanitize("SecretProjectX")

    reformatted = clean.replace("[PROJECT_1]", "[ project _ 1 ]")

    assert deterministic_sanitizer.restore(reformatted, vault_id) == "SecretProjectX"

def test_later_layer_cannot_wrap_existing_placeholder(
    deterministic_sanitizer: Sanitizer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def replace_entire_text(text: str, builder: object) -> str:
        return deterministic_sanitizer._replace_spans(
            text, [(0, len(text), "PERSON")], builder
        )

    deterministic_sanitizer.enable_pii = True
    monkeypatch.setattr(deterministic_sanitizer, "_replace_pii", replace_entire_text)
    clean, vault_id = deterministic_sanitizer.sanitize("SecretProjectX")

    assert clean == "[PROJECT_1]"
    assert deterministic_sanitizer.restore(clean, vault_id) == "SecretProjectX"


def test_check_does_not_create_vault(deterministic_sanitizer: Sanitizer) -> None:
    report = deterministic_sanitizer.check("SecretProjectX and InternalServiceAlpha")

    assert report["replacement_count"] == 2
    assert report["categories"] == {"PROJECT": 1, "SERVICE": 1}
    assert list(deterministic_sanitizer.vault_dir.glob("*.json")) == []


def test_vault_permissions_are_private(deterministic_sanitizer: Sanitizer) -> None:
    _, vault_id = deterministic_sanitizer.sanitize("SecretProjectX")
    mode = os.stat(deterministic_sanitizer.vault_dir / f"{vault_id}.json").st_mode

    assert mode & 0o777 == 0o600


def test_presidio_detects_synthetic_email_name_and_ip(project_root: Path) -> None:
    sanitizer = Sanitizer(root=project_root, require_llm=False, enable_pii=True)
    original = "Contact John Smith at john.smith@example.com from 203.0.113.42."

    clean, vault_id = sanitizer.sanitize(original)

    assert "john.smith@example.com" not in clean
    assert "203.0.113.42" not in clean
    assert "John Smith" not in clean
    assert sanitizer.restore(clean, vault_id) == original


@pytest.mark.integration
@pytest.mark.flaky(reruns=1)
def test_local_llm_detects_unknown_synthetic_project(project_root: Path) -> None:
    try:
        response = requests.get("http://127.0.0.1:8080/v1/models", timeout=2)
        response.raise_for_status()
    except requests.RequestException:
        pytest.skip("local llama-server is not running")

    sanitizer = Sanitizer(root=project_root, require_llm=True, enable_pii=False)
    original = (
        "Our confidential internal project is named Velmorix Quasar. "
        "Velmorix Quasar must not be disclosed."
    )

    clean, vault_id = sanitizer.sanitize(original)

    assert "Velmorix Quasar" not in clean
    assert "[SENSITIVE_1]" in clean
    assert sanitizer.restore(clean, vault_id) == original

