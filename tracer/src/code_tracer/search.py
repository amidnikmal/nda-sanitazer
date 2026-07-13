"""Hybrid search for `where`: FTS5 (BM25) + vector top-K -> RRF -> optional LLM rerank."""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .db import IndexDB, unpack_vector
from .embedder import Embedder, get_embedder
from . import llm

log = logging.getLogger("code_tracer.search")


@dataclass
class Hit:
    symbol_id: int
    path: str
    line: int
    name: str
    qualname: str
    kind: str
    signature: str
    docstring: str
    score: float
    why: str = ""


def _fts_query(text: str) -> str:
    # Turn a free-text question into an OR of quoted terms so FTS5 never errors
    # on punctuation/operators in the user string.
    terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)
    terms = [t for t in terms if len(t) > 1]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


def _fts_search(db: IndexDB, question: str, limit: int = 50) -> list[tuple[int, float]]:
    q = _fts_query(question)
    try:
        rows = db.conn.execute(
            "SELECT rowid, bm25(symbols_fts) AS score FROM symbols_fts "
            "WHERE symbols_fts MATCH ? ORDER BY score LIMIT ?",
            (q, limit),
        ).fetchall()
    except Exception as exc:
        log.debug("fts failed: %s", exc)
        return []
    # bm25 returns lower=better; convert rank to a list ordered best-first
    return [(r["rowid"], r["score"]) for r in rows]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # vectors are L2-normalized on write


def _vector_search(db: IndexDB, embedder: Embedder, question: str,
                   limit: int = 50) -> list[tuple[int, float]]:
    qvec = embedder.embed([question])[0]
    rows = db.conn.execute(
        "SELECT id, embedding FROM symbols WHERE embedding IS NOT NULL"
    ).fetchall()
    scored = []
    for r in rows:
        vec = unpack_vector(r["embedding"])
        if len(vec) != len(qvec):
            continue
        scored.append((r["id"], _cosine(qvec, vec)))
    scored.sort(key=lambda t: t[1], reverse=True)
    return scored[:limit]


def _rrf(rankings: list[list[tuple[int, float]]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, (sid, _s) in enumerate(ranking):
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _load_hits(db: IndexDB, ids: list[int]) -> dict[int, Hit]:
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    rows = db.conn.execute(
        f"SELECT s.id, s.name, s.qualname, s.kind, s.start_line, s.signature, "
        f"s.docstring, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
        f"WHERE s.id IN ({ph})",
        ids,
    ).fetchall()
    out = {}
    for r in rows:
        out[r["id"]] = Hit(
            symbol_id=r["id"], path=r["path"], line=r["start_line"],
            name=r["name"], qualname=r["qualname"], kind=r["kind"],
            signature=r["signature"] or "", docstring=r["docstring"] or "",
            score=0.0,
        )
    return out


def where(db: IndexDB, question: str, cfg: Config, *, fast: bool = False,
          embedder: Optional[Embedder] = None, top_n: int = 5) -> list[Hit]:
    embedder = embedder or get_embedder(cfg)
    fts = _fts_search(db, question, 50)
    vec = _vector_search(db, embedder, question, 50)
    fused = _rrf([fts, vec])
    if not fused:
        return []
    ranked_ids = sorted(fused, key=lambda i: fused[i], reverse=True)
    hits_map = _load_hits(db, ranked_ids[:20])
    ranked = []
    for sid in ranked_ids:
        h = hits_map.get(sid)
        if h is None:
            continue
        h.score = fused[sid]
        ranked.append(h)
        if len(ranked) >= 20:
            break

    if not fast and llm.server_available(cfg):
        reranked = _llm_rerank(question, ranked[:20], cfg)
        if reranked:
            ranked = reranked
    return ranked[:top_n]


def _llm_rerank(question: str, hits: list[Hit], cfg: Config) -> Optional[list[Hit]]:
    listing = "\n".join(
        f"[{i}] {h.path}:{h.line} {h.qualname} ({h.kind}) :: {h.signature} "
        f"{h.docstring[:80]}"
        for i, h in enumerate(hits)
    )
    prompt = (
        "You rank code locations by relevance to a developer question. "
        "Return ONLY a JSON array of the item indices, most relevant first, "
        "including at most 5 indices.\n\n"
        f"Question: {question}\n\nItems:\n{listing}\n\nJSON:"
    )
    resp = llm.chat(prompt, cfg, temperature=0.0, max_tokens=64)
    if not resp:
        return None
    try:
        m = re.search(r"\[[\d,\s]*\]", resp)
        order = json.loads(m.group(0)) if m else None
    except Exception:
        return None
    if not order:
        return None
    out = []
    for idx in order:
        if isinstance(idx, int) and 0 <= idx < len(hits):
            hits[idx].why = "llm-ranked"
            out.append(hits[idx])
    # append any not chosen, preserving base order
    chosen = set(id(h) for h in out)
    for h in hits:
        if id(h) not in chosen:
            out.append(h)
    return out
