"""Configuration and per-project data-dir resolution."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml_read
except ModuleNotFoundError:  # pragma: no cover - py310 fallback
    import tomli as _toml_read  # type: ignore

import tomli_w


CONFIG_PATH = Path(os.path.expanduser("~/.config/code-tracer.toml"))


def data_root() -> Path:
    """Base data dir; honours XDG_DATA_HOME."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "code-tracer"


def models_dir() -> Path:
    d = data_root() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_data_dir(project_path: str | Path) -> Path:
    """Deterministic per-project data dir keyed by absolute path hash."""
    abs_path = str(Path(project_path).resolve())
    h = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:16]
    d = data_root() / h
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULTS: dict[str, Any] = {
    "editor_command": "code -g {path}:{line}",
    "sanitizer_path": os.path.expanduser("~/nda-sanitizer/.venv/bin/nda-sanitizer"),
    "llm_url": "http://127.0.0.1:8080/v1",
    "llm_model": "local",
    "embed_model_path": "",  # empty => auto-resolve in models_dir()
    "embed_disable_thinking": True,  # append /no_think & enable_thinking=false
    "heartbeat_timeout_s": 15,
    "no_think_mode": "kwargs",  # "kwargs" | "suffix" | "both"
}


@dataclass
class Config:
    editor_command: str = DEFAULTS["editor_command"]
    sanitizer_path: str = DEFAULTS["sanitizer_path"]
    llm_url: str = DEFAULTS["llm_url"]
    llm_model: str = DEFAULTS["llm_model"]
    embed_model_path: str = DEFAULTS["embed_model_path"]
    embed_disable_thinking: bool = DEFAULTS["embed_disable_thinking"]
    heartbeat_timeout_s: int = DEFAULTS["heartbeat_timeout_s"]
    no_think_mode: str = DEFAULTS["no_think_mode"]
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or CONFIG_PATH
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        with open(path, "rb") as fh:
            data = _toml_read.load(fh)
        known = {k: data[k] for k in DEFAULTS if k in data}
        extra = {k: v for k, v in data.items() if k not in DEFAULTS}
        cfg = cls(**known)
        cfg._extra = extra
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or CONFIG_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        out = {k: getattr(self, k) for k in DEFAULTS}
        out.update(self._extra)
        with open(path, "wb") as fh:
            tomli_w.dump(out, fh)
