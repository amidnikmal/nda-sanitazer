"""SQLite schema and access helpers for the code index.

Tables: files / symbols / edges / chunks (+ annotations cache).
FTS5 virtual table over symbol names, identifiers, docstrings, comments.
Per-symbol embedding vectors stored as raw float32 blobs.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,   -- relative to project root
    lang      TEXT,
    hash      TEXT NOT NULL,          -- sha256 of content
    mtime     REAL,
    size      INTEGER,
    n_symbols INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    qualname   TEXT,                  -- e.g. Class.method
    kind       TEXT,                  -- function|method|class
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    signature  TEXT,
    docstring  TEXT,
    body_hash  TEXT,                  -- sha256 of symbol source text
    embedding  BLOB                   -- float32[] or NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    caller_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    callee_id   INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    callee_name TEXT NOT NULL,        -- raw called name (for unresolved)
    call_line   INTEGER,
    confidence  TEXT DEFAULT 'high',  -- high|medium|low
    candidates  TEXT                  -- comma-separated candidate symbol ids
);
CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON edges(callee_id);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,
    symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE,
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS annotations (
    body_hash TEXT PRIMARY KEY,
    text      TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, qualname, identifiers, docstring, comments
);
"""


def pack_vector(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


class IndexDB:
    def __init__(self, db_path: str | Path):
        self.path = str(db_path)
        self._connect()
        if not self._schema_is_current():
            # Older on-disk schema: индекс — производные данные, дешевле
            # пересоздать, чем мигрировать.
            self.conn.close()
            for suffix in ("", "-wal", "-shm"):
                Path(self.path + suffix).unlink(missing_ok=True)
            self._connect()
        self._init_schema()

    def _connect(self) -> None:
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")

    def _schema_is_current(self) -> bool:
        n_tables = self.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        if n_tables == 0:  # свежий пустой файл — схему создаст _init_schema
            return True
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:  # таблицы есть, meta нет — до-versioning база
            return False
        return row is not None and row["value"] == str(SCHEMA_VERSION)

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self.conn.commit()

    # -- files -----------------------------------------------------------
    def get_file(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM files WHERE path=?", (path,)
        ).fetchone()

    def all_file_paths(self) -> set[str]:
        return {r["path"] for r in self.conn.execute("SELECT path FROM files")}

    def delete_file(self, path: str) -> None:
        row = self.get_file(path)
        if row is None:
            return
        fid = row["id"]
        # FTS rows are keyed by symbol rowid; drop them first.
        sym_ids = [
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM symbols WHERE file_id=?", (fid,)
            )
        ]
        for sid in sym_ids:
            self.conn.execute("DELETE FROM symbols_fts WHERE rowid=?", (sid,))
        self.conn.execute("DELETE FROM files WHERE id=?", (fid,))

    def upsert_file(
        self, path: str, lang: str, file_hash: str, mtime: float, size: int
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO files(path, lang, hash, mtime, size) VALUES (?,?,?,?,?)",
            (path, lang, file_hash, mtime, size),
        )
        return int(cur.lastrowid)

    def set_file_symbol_count(self, file_id: int, n: int) -> None:
        self.conn.execute(
            "UPDATE files SET n_symbols=? WHERE id=?", (n, file_id)
        )

    # -- symbols ---------------------------------------------------------
    def insert_symbol(self, **kw) -> int:
        cur = self.conn.execute(
            """INSERT INTO symbols
               (file_id,name,qualname,kind,start_line,end_line,signature,
                docstring,body_hash,embedding)
               VALUES (:file_id,:name,:qualname,:kind,:start_line,:end_line,
                       :signature,:docstring,:body_hash,:embedding)""",
            kw,
        )
        return int(cur.lastrowid)

    def insert_fts(
        self,
        rowid: int,
        name: str,
        qualname: str,
        identifiers: str,
        docstring: str,
        comments: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO symbols_fts(rowid,name,qualname,identifiers,docstring,comments)"
            " VALUES (?,?,?,?,?,?)",
            (rowid, name, qualname, identifiers, docstring, comments),
        )

    def insert_edge(self, **kw) -> None:
        self.conn.execute(
            """INSERT INTO edges
               (caller_id,callee_id,callee_name,call_line,confidence,candidates)
               VALUES (:caller_id,:callee_id,:callee_name,:call_line,
                       :confidence,:candidates)""",
            kw,
        )

    def insert_chunk(self, symbol_id: int | None, file_id: int, text: str,
                     embedding: bytes | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO chunks(symbol_id,file_id,text,embedding) VALUES (?,?,?,?)",
            (symbol_id, file_id, text, embedding),
        )
        return int(cur.lastrowid)

    # -- annotations cache ----------------------------------------------
    def get_annotation(self, body_hash: str) -> str | None:
        row = self.conn.execute(
            "SELECT text FROM annotations WHERE body_hash=?", (body_hash,)
        ).fetchone()
        return row["text"] if row else None

    def set_annotation(self, body_hash: str, text: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO annotations(body_hash,text) VALUES (?,?)",
            (body_hash, text),
        )
        self.conn.commit()

    # -- counts ----------------------------------------------------------
    def counts(self) -> dict[str, int]:
        c = self.conn.execute
        return {
            "files": c("SELECT COUNT(*) FROM files").fetchone()[0],
            "symbols": c("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "edges": c("SELECT COUNT(*) FROM edges").fetchone()[0],
            "chunks": c("SELECT COUNT(*) FROM chunks").fetchone()[0],
        }

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
