"""Directory indexer: walk -> parse -> resolve edges -> store.

Incremental by file content hash. Respects .gitignore plus a builtin skip
list; skips binary files. Embeds each symbol (real or fake embedder).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import pathspec

from .db import IndexDB, pack_vector
from .embedder import Embedder, get_embedder
from .languages import lang_for_path
from .parsing import Symbol, parse_source

log = logging.getLogger("code_tracer.indexer")

BUILTIN_IGNORE_DIRS = {
    "node_modules", ".venv", "venv", "build", "dist", ".git", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".tox", "target", "vendor", ".idea",
    ".gradle", "out", ".next", ".svn", ".hg", "site-packages", ".stversions",
}


@dataclass
class IndexStats:
    files_scanned: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_removed: int = 0
    symbols: int = 0
    edges: int = 0
    chunks: int = 0
    seconds: float = 0.0


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_binary(data: bytes) -> bool:
    if b"\x00" in data[:8192]:
        return True
    return False


def _load_gitignore(root: Path) -> pathspec.PathSpec:
    patterns: list[str] = list(BUILTIN_IGNORE_DIRS)
    patterns = [d + "/" for d in BUILTIN_IGNORE_DIRS]
    gi = root / ".gitignore"
    if gi.exists():
        try:
            patterns += gi.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            pass
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def iter_source_files(root: Path) -> Iterable[Path]:
    spec = _load_gitignore(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        # prune ignored / builtin dirs in-place
        keep = []
        for d in dirnames:
            if d in BUILTIN_IGNORE_DIRS:
                continue
            rp = os.path.normpath(os.path.join(rel_dir, d)) + "/"
            if spec.match_file(rp):
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = os.path.relpath(full, root)
            if spec.match_file(rel):
                continue
            if lang_for_path(fn) is None:
                continue
            yield full


class Indexer:
    def __init__(self, root: str | Path, db: IndexDB, embedder: Optional[Embedder] = None):
        self.root = Path(root).resolve()
        self.db = db
        self.embedder = embedder if embedder is not None else get_embedder()

    def index(self, progress=None) -> IndexStats:
        t0 = time.time()
        stats = IndexStats()
        seen: set[str] = set()

        files = list(iter_source_files(self.root))
        total = len(files)
        for i, full in enumerate(files):
            rel = os.path.relpath(full, self.root)
            seen.add(rel)
            stats.files_scanned += 1
            try:
                data = full.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            fhash = _sha_bytes(data)
            existing = self.db.get_file(rel)
            if existing and existing["hash"] == fhash:
                stats.files_unchanged += 1
                if progress:
                    progress(i + 1, total, rel, changed=False)
                continue
            # changed or new: drop old rows then reindex
            if existing:
                self.db.delete_file(rel)
            self._index_file(rel, full, data, fhash, stats)
            stats.files_indexed += 1
            if progress:
                progress(i + 1, total, rel, changed=True)

        # remove files that disappeared
        for gone in self.db.all_file_paths() - seen:
            self.db.delete_file(gone)
            stats.files_removed += 1

        # resolve edges after all symbols are in place
        self._resolve_edges()
        self.db.commit()

        counts = self.db.counts()
        stats.symbols = counts["symbols"]
        stats.chunks = counts["chunks"]
        stats.edges = counts["edges"]
        stats.seconds = time.time() - t0
        return stats

    def _index_file(self, rel: str, full: Path, data: bytes, fhash: str,
                    stats: IndexStats) -> None:
        lang = lang_for_path(rel)
        code = data.decode("utf-8", "replace")
        try:
            symbols = parse_source(lang, code)
        except Exception as exc:  # never let one file kill the run
            log.warning("parse failed for %s: %s", rel, exc)
            symbols = []
        st = full.stat()
        file_id = self.db.upsert_file(rel, lang, fhash, st.st_mtime, st.st_size)

        # embed all symbol texts in one batch
        texts = [self._embed_text(s) for s in symbols]
        vectors = self.embedder.embed(texts) if texts else []

        for sym, vec in zip(symbols, vectors):
            blob = pack_vector(vec) if vec else None
            sid = self.db.insert_symbol(
                file_id=file_id,
                name=sym.name,
                qualname=sym.qualname,
                kind=sym.kind,
                start_line=sym.start_line,
                end_line=sym.end_line,
                signature=sym.signature,
                docstring=sym.docstring,
                body_hash=sym.body_hash,
                embedding=blob,
            )
            sym._db_id = sid  # type: ignore[attr-defined]
            self.db.insert_fts(
                sid, sym.name, sym.qualname, sym.identifiers,
                sym.docstring, sym.comments,
            )
            self.db.insert_chunk(sid, file_id, self._embed_text(sym), blob)

        # stash raw calls for edge resolution pass
        self._pending = getattr(self, "_pending", [])
        for sym in symbols:
            self._pending.append((sym._db_id, sym.calls))  # type: ignore[attr-defined]
        self.db.set_file_symbol_count(file_id, len(symbols))

    @staticmethod
    def _embed_text(sym: Symbol) -> str:
        parts = [sym.qualname, sym.signature, sym.docstring, sym.comments]
        return "\n".join(p for p in parts if p).strip() or sym.name

    def _resolve_edges(self) -> int:
        """Resolve call names to callee symbol ids by heuristic scope rules.

        Priority: same file > same directory > globally unique name.
        Ambiguous -> confidence=low with candidate list. Unknown -> callee_id
        NULL (kept as an approximate/dangling edge).
        """
        conn = self.db.conn
        # Build lookup tables.
        rows = conn.execute(
            "SELECT s.id, s.name, s.file_id, f.path FROM symbols s "
            "JOIN files f ON f.id = s.file_id"
        ).fetchall()
        by_name: dict[str, list[tuple]] = {}
        file_dir: dict[int, str] = {}
        sym_file: dict[int, int] = {}
        for r in rows:
            by_name.setdefault(r["name"], []).append(
                (r["id"], r["file_id"], os.path.dirname(r["path"]))
            )
            file_dir[r["file_id"]] = os.path.dirname(r["path"])
            sym_file[r["id"]] = r["file_id"]

        pending = getattr(self, "_pending", [])
        n_edges = 0
        for caller_id, calls in pending:
            caller_file = sym_file.get(caller_id)
            caller_dir = file_dir.get(caller_file, "")
            for call in calls:
                cands = by_name.get(call.name, [])
                if not cands:
                    # dangling / external
                    self.db.insert_edge(
                        caller_id=caller_id, callee_id=None,
                        callee_name=call.name, call_line=call.line,
                        confidence="low", candidates=None,
                    )
                    n_edges += 1
                    continue
                callee_id, confidence, candidates = self._pick(
                    cands, caller_file, caller_dir
                )
                self.db.insert_edge(
                    caller_id=caller_id, callee_id=callee_id,
                    callee_name=call.name, call_line=call.line,
                    confidence=confidence, candidates=candidates,
                )
                n_edges += 1
        self._pending = []
        return n_edges

    @staticmethod
    def _pick(cands, caller_file, caller_dir):
        same_file = [c for c in cands if c[1] == caller_file]
        if len(same_file) >= 1:
            return same_file[0][0], ("high" if len(same_file) == 1 else "medium"), None
        same_dir = [c for c in cands if c[2] == caller_dir]
        if len(same_dir) == 1:
            return same_dir[0][0], "medium", None
        if len(cands) == 1:
            return cands[0][0], "high", None
        # ambiguous global: pick first, mark low, list candidates
        candidates = ",".join(str(c[0]) for c in cands[:8])
        return cands[0][0], "low", candidates
