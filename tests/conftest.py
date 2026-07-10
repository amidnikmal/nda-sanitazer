from pathlib import Path
import shutil

import pytest

from src.sanitizer import Sanitizer


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "nda-sanitizer"
    (root / "config").mkdir(parents=True)
    (root / "vaults").mkdir()
    (root / "logs").mkdir()
    source = Path(__file__).resolve().parents[1] / "config" / "nda_terms.yaml"
    shutil.copy2(source, root / "config" / "nda_terms.yaml")
    return root


@pytest.fixture
def deterministic_sanitizer(project_root: Path) -> Sanitizer:
    return Sanitizer(root=project_root, require_llm=False, enable_pii=False)

