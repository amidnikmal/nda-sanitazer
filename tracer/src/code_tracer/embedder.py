"""Pluggable, in-process CPU embedder.

Interface: ``Embedder.embed(texts) -> list[list[float]]`` with a fixed ``dim``.

Two implementations:
  * ``LlamaEmbedder`` — real GGUF embedding model via llama-cpp-python (CPU).
    Used only when both llama_cpp is importable AND a GGUF file is present.
  * ``FakeEmbedder`` — deterministic hash-based vectors. No deps, no network.
    Used as fallback and in tests so the core + ``where`` (FTS5) work fully.

``get_embedder()`` picks the real one when possible, else the fake.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
from typing import Protocol, Sequence

from .config import Config, models_dir


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


class FakeEmbedder:
    """Deterministic hash-based embedder. Stable across runs; no I/O.

    Produces token-hash bag-of-words vectors so that texts sharing tokens land
    near each other — enough for tests and a graceful no-model fallback.
    """

    name = "fake-hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            tokens = _tokenize(text)
            for tok in tokens:
                h = hashlib.sha1(tok.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "little") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
            out.append(_l2_normalize(vec))
        return out


def _tokenize(text: str) -> list[str]:
    import re

    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", text.lower())


class LlamaEmbedder:
    """Real GGUF embedder via llama-cpp-python (CPU, in-process)."""

    name = "llama-gguf"

    def __init__(self, model_path: str, n_threads: int | None = None):
        from llama_cpp import Llama  # imported lazily

        self.model_path = model_path
        self._llm = Llama(
            model_path=model_path,
            embedding=True,
            n_ctx=1024,
            n_threads=n_threads or os.cpu_count() or 4,
            verbose=False,
        )
        # probe dimension
        probe = self._llm.create_embedding("dim probe")
        self.dim = len(probe["data"][0]["embedding"])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            resp = self._llm.create_embedding(text)
            vec = resp["data"][0]["embedding"]
            out.append(_l2_normalize([float(x) for x in vec]))
        return out


# Preferred GGUF filenames, in resolution order (Qwen3 -> bge-m3 -> gemma).
_MODEL_CANDIDATES = [
    "Qwen3-Embedding-0.6B-Q8_0.gguf",
    "bge-m3-Q8_0.gguf",
    "embeddinggemma-300M-Q8_0.gguf",
]


def resolve_model_path(cfg: Config | None = None) -> str | None:
    """Return path to a usable GGUF embedding model, or None."""
    if cfg and cfg.embed_model_path:
        p = Path(os.path.expanduser(cfg.embed_model_path))
        return str(p) if p.exists() else None
    mdir = models_dir()
    for name in _MODEL_CANDIDATES:
        p = mdir / name
        if p.exists():
            return str(p)
    # any .gguf in the models dir
    ggufs = sorted(mdir.glob("*.gguf"))
    return str(ggufs[0]) if ggufs else None


def _llama_available() -> bool:
    try:
        import llama_cpp  # noqa: F401

        return True
    except Exception:
        return False


def get_embedder(cfg: Config | None = None, force_fake: bool = False) -> Embedder:
    """Pick the real embedder when a model + llama_cpp are present; else fake."""
    if force_fake or os.environ.get("CODE_TRACER_FAKE_EMBED"):
        return FakeEmbedder()
    model_path = resolve_model_path(cfg)
    if model_path and _llama_available():
        try:
            return LlamaEmbedder(model_path)
        except Exception:
            # Any load failure -> graceful fallback, never crash indexing.
            return FakeEmbedder()
    return FakeEmbedder()
