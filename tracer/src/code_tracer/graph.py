"""Call-graph BFS for `trace` and symbol resolution for `stack`/`outline`."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from .config import Config
from .db import IndexDB
from . import llm

log = logging.getLogger("code_tracer.graph")


@dataclass
class Node:
    symbol_id: int
    name: str
    qualname: str
    path: str
    line: int
    kind: str
    confidence: str = "high"
    explain: str = ""
    children: list["Edge"] = field(default_factory=list)


@dataclass
class Edge:
    confidence: str
    node: Node


def resolve_symbol(db: IndexDB, target: str) -> list[dict]:
    """Resolve `file:line` or a function name to candidate symbol rows."""
    m = re.match(r"^(.*):(\d+)$", target)
    if m:
        path_part, line = m.group(1), int(m.group(2))
        rows = db.conn.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE (f.path=? OR f.path LIKE ?) AND s.start_line<=? AND s.end_line>=? "
            "ORDER BY (s.end_line - s.start_line) ASC",
            (path_part, f"%{path_part}", line, line),
        ).fetchall()
        return [dict(r) for r in rows]
    # by name or qualname
    rows = db.conn.execute(
        "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
        "WHERE s.name=? OR s.qualname=?",
        (target, target),
    ).fetchall()
    return [dict(r) for r in rows]


def _row_to_node(row: dict, confidence: str = "high") -> Node:
    return Node(
        symbol_id=row["id"], name=row["name"], qualname=row["qualname"],
        path=row["path"], line=row["start_line"], kind=row["kind"],
        confidence=confidence,
    )


def _fetch_symbol(db: IndexDB, sid: int) -> Optional[dict]:
    r = db.conn.execute(
        "SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.id=?",
        (sid,),
    ).fetchone()
    return dict(r) if r else None


def trace(db: IndexDB, target: str, *, direction: str = "down", depth: int = 3,
          fan_out: int = 8) -> Optional[Node]:
    """BFS the call graph. direction: 'down' (callees) or 'up' (callers)."""
    cands = resolve_symbol(db, target)
    if not cands:
        return None
    root_row = cands[0]
    root = _row_to_node(root_row)
    visited: set[int] = {root.symbol_id}
    _expand(db, root, direction, depth, fan_out, visited)
    return root


def _expand(db: IndexDB, node: Node, direction: str, depth: int, fan_out: int,
            visited: set[int]) -> None:
    if depth <= 0:
        return
    if direction == "down":
        rows = db.conn.execute(
            "SELECT callee_id, callee_name, confidence FROM edges "
            "WHERE caller_id=? AND callee_id IS NOT NULL LIMIT ?",
            (node.symbol_id, fan_out * 4),
        ).fetchall()
        neigh = [(r["callee_id"], r["confidence"]) for r in rows]
    else:
        rows = db.conn.execute(
            "SELECT caller_id, confidence FROM edges WHERE callee_id=? LIMIT ?",
            (node.symbol_id, fan_out * 4),
        ).fetchall()
        neigh = [(r["caller_id"], r["confidence"]) for r in rows]

    seen_here: set[int] = set()
    count = 0
    for sid, conf in neigh:
        if sid in seen_here:
            continue
        seen_here.add(sid)
        if count >= fan_out:
            break
        if sid in visited:
            # still show edge but do not recurse (cycle guard)
            row = _fetch_symbol(db, sid)
            if row:
                node.children.append(Edge(conf, _row_to_node(row, conf)))
            count += 1
            continue
        row = _fetch_symbol(db, sid)
        if not row:
            continue
        visited.add(sid)
        child = _row_to_node(row, conf)
        node.children.append(Edge(conf, child))
        _expand(db, child, direction, depth - 1, fan_out, visited)
        count += 1


def annotate(db: IndexDB, node: Node, cfg: Config, body_lookup) -> None:
    """One-line 'what it does' per node via LLM, cached by body_hash."""
    if not llm.server_available(cfg):
        return
    _annotate_rec(db, node, cfg, body_lookup, set())


def _annotate_rec(db, node, cfg, body_lookup, seen):
    if node.symbol_id in seen:
        return
    seen.add(node.symbol_id)
    row = _fetch_symbol(db, node.symbol_id)
    if row:
        bh = row.get("body_hash")
        cached = db.get_annotation(bh) if bh else None
        if cached:
            node.explain = cached
        else:
            body = body_lookup(row) if body_lookup else ""
            text = llm.chat(
                "In ONE short sentence, say what this code does. No preamble.\n\n"
                f"{body[:1500]}",
                cfg, temperature=0.0, max_tokens=48,
            )
            if text:
                node.explain = text.replace("\n", " ").strip()
                if bh:
                    db.set_annotation(bh, node.explain)
    for e in node.children:
        _annotate_rec(db, e.node, cfg, body_lookup, seen)


# ---- rendering -------------------------------------------------------------

def to_mermaid(root: Node) -> str:
    lines = ["flowchart TD"]
    seen_edges: set[tuple[int, int]] = set()

    def nid(n: Node) -> str:
        return f"n{n.symbol_id}"

    def label(n: Node) -> str:
        base = n.qualname or n.name
        return base.replace('"', "'")

    def walk(n: Node) -> None:
        lines.append(f'    {nid(n)}["{label(n)}"]')
        for e in n.children:
            key = (n.symbol_id, e.node.symbol_id)
            arrow = "-->" if e.confidence == "high" else "-.->"
            if key not in seen_edges:
                seen_edges.add(key)
                lines.append(f"    {nid(n)} {arrow} {nid(e.node)}")
            walk(e.node)

    walk(root)
    return "\n".join(dict.fromkeys(lines))
