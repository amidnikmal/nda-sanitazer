"""Runtime stack-trace parsers (python / PHP / Go panic / Node.js) + binding."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

from .db import IndexDB


@dataclass
class Frame:
    file: Optional[str]
    line: Optional[int]
    func: Optional[str]
    raw: str
    in_index: bool = False
    symbol_id: Optional[int] = None
    resolved_path: Optional[str] = None


# ---- format detection & parsing -------------------------------------------

_PY_FRAME = re.compile(
    r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)', re.M
)
_PHP_FRAME = re.compile(
    r"^#\d+\s+(?P<file>[^(]+)\((?P<line>\d+)\):\s*(?P<func>.+)$"
)
_PHP_THROWN = re.compile(r"in (?P<file>[^ ]+) on line (?P<line>\d+)")
_GO_FILE = re.compile(r"^\s+(?P<file>[^\s:]+\.go):(?P<line>\d+)")
_GO_FUNC = re.compile(r"^(?P<func>[\w./*()]+)\(")
_NODE_FRAME = re.compile(
    r"^\s*at (?:(?P<func>[^(]+?) )?\(?(?P<file>[^():]+):(?P<line>\d+):\d+\)?$"
)


def detect_format(text: str) -> str:
    if "Traceback (most recent call last)" in text or _PY_FRAME.search(text):
        return "python"
    if "goroutine " in text or re.search(r"\.go:\d+", text) and "panic" in text:
        return "go"
    if re.search(r"^#\d+ ", text, re.M) or "Stack trace:" in text:
        return "php"
    if re.search(r"^\s*at .+:\d+:\d+", text, re.M):
        return "node"
    if "panic:" in text or re.search(r"\.go:\d+", text):
        return "go"
    return "unknown"


def parse_python(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for m in _PY_FRAME.finditer(text):
        frames.append(Frame(file=m.group("file"), line=int(m.group("line")),
                            func=m.group("func"), raw=m.group(0).strip()))
    return frames


def parse_php(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for line in text.splitlines():
        m = _PHP_FRAME.match(line.strip())
        if m:
            func = m.group("func").strip()
            func = re.sub(r"\(.*$", "", func)  # drop args
            frames.append(Frame(file=m.group("file").strip(),
                                line=int(m.group("line")),
                                func=func, raw=line.strip()))
            continue
        t = _PHP_THROWN.search(line)
        if t:
            frames.append(Frame(file=t.group("file"), line=int(t.group("line")),
                                func=None, raw=line.strip()))
    return frames


def parse_go(text: str) -> list[Frame]:
    frames: list[Frame] = []
    lines = text.splitlines()
    pending_func: Optional[str] = None
    for line in lines:
        fm = _GO_FUNC.match(line.strip())
        if fm and (".go" not in line):
            pending_func = fm.group("func")
            continue
        m = _GO_FILE.match(line)
        if m:
            func = pending_func
            # strip receiver path, keep last component
            if func:
                func = func.split("/")[-1]
                func = re.sub(r"^\**\(?\*?[\w.]+\)?\.", "", func)
            frames.append(Frame(file=m.group("file"), line=int(m.group("line")),
                                func=func, raw=line.strip()))
            pending_func = None
    return frames


def parse_node(text: str) -> list[Frame]:
    frames: list[Frame] = []
    for line in text.splitlines():
        m = _NODE_FRAME.match(line)
        if m:
            func = (m.group("func") or "").strip() or None
            if func:
                func = func.split(".")[-1]
            frames.append(Frame(file=m.group("file"), line=int(m.group("line")),
                                func=func, raw=line.strip()))
    return frames


_PARSERS = {
    "python": parse_python,
    "php": parse_php,
    "go": parse_go,
    "node": parse_node,
}


def parse(text: str, fmt: Optional[str] = None) -> tuple[str, list[Frame]]:
    fmt = fmt or detect_format(text)
    parser = _PARSERS.get(fmt)
    if parser is None:
        return fmt, []
    return fmt, parser(text)


# ---- binding to the index --------------------------------------------------

def bind(db: IndexDB, frames: list[Frame], root: Optional[str] = None) -> list[Frame]:
    """Attach each frame to a symbol in the index; mark out-of-index frames."""
    for fr in frames:
        if not fr.file:
            continue
        base = os.path.basename(fr.file)
        rows = db.conn.execute(
            "SELECT f.id, f.path FROM files f WHERE f.path=? OR f.path LIKE ?",
            (fr.file, f"%{base}"),
        ).fetchall()
        if not rows:
            fr.in_index = False
            continue
        # prefer exact suffix match
        file_row = None
        for r in rows:
            if r["path"].endswith(fr.file) or os.path.basename(r["path"]) == base:
                file_row = r
                break
        file_row = file_row or rows[0]
        fr.resolved_path = file_row["path"]
        fr.in_index = True
        # locate symbol by line, else by func name
        srow = None
        if fr.line is not None:
            srow = db.conn.execute(
                "SELECT id FROM symbols WHERE file_id=? AND start_line<=? AND end_line>=? "
                "ORDER BY (end_line-start_line) ASC LIMIT 1",
                (file_row["id"], fr.line, fr.line),
            ).fetchone()
        if srow is None and fr.func:
            srow = db.conn.execute(
                "SELECT id FROM symbols WHERE file_id=? AND (name=? OR qualname=?) LIMIT 1",
                (file_row["id"], fr.func, fr.func),
            ).fetchone()
        if srow:
            fr.symbol_id = srow["id"]
    return frames
