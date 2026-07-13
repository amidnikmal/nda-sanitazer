import json
import os
from pathlib import Path

import pytest
import requests

from src.sanitizer import Sanitizer, SecretDetectedError


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

