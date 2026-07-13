"""code-tracer command-line interface."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.tree import Tree
from rich.table import Table

from . import __version__
from .config import Config, project_data_dir
from .db import IndexDB
from .embedder import get_embedder
from .indexer import Indexer
from . import graph as graphmod
from . import search as searchmod
from . import stack as stackmod
from . import llm

app = typer.Typer(add_completion=False, help="Local code-insight tracer.")
console = Console()
err = Console(stderr=True)


def _db_for(project_dir: str | Path) -> IndexDB:
    ddir = project_data_dir(project_dir)
    return IndexDB(ddir / "index.db")


def _resolve_project(dir_opt: Optional[str]) -> Path:
    return Path(dir_opt).resolve() if dir_opt else Path.cwd()


def _body_lookup(root: Path):
    def fn(row: dict) -> str:
        try:
            path = root / row["path"]
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[row["start_line"] - 1: row["end_line"]])
        except OSError:
            return row.get("signature", "")
    return fn


@app.command()
def index(
    directory: str = typer.Argument(..., help="Project directory to index."),
    fake_embed: bool = typer.Option(False, "--fake-embed", help="Force fake embedder."),
):
    """Build or incrementally update the index for DIRECTORY."""
    root = Path(directory).resolve()
    if not root.is_dir():
        err.print(f"[red]not a directory:[/] {root}")
        raise typer.Exit(2)
    cfg = Config.load()
    db = _db_for(root)
    embedder = get_embedder(cfg, force_fake=fake_embed)
    console.print(f"Indexing [bold]{root}[/]  (embedder: {embedder.name})")
    idx = Indexer(root, db, embedder=embedder)

    def progress(done, total, rel, changed):
        if changed:
            console.print(f"  [{done}/{total}] {rel}", highlight=False)

    stats = idx.index(progress=progress)
    db.close()
    console.print(
        f"[green]done[/] files_scanned={stats.files_scanned} "
        f"indexed={stats.files_indexed} unchanged={stats.files_unchanged} "
        f"removed={stats.files_removed} symbols={stats.symbols} "
        f"edges={stats.edges} chunks={stats.chunks} "
        f"in {stats.seconds:.2f}s "
        f"({stats.files_scanned / max(stats.seconds, 1e-6):.1f} files/s)"
    )


@app.command()
def where(
    question: str = typer.Argument(..., help="Natural-language question."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d"),
    fast: bool = typer.Option(False, "--fast", help="Skip LLM rerank."),
):
    """Find where to look: file:line — symbol — why."""
    root = _resolve_project(directory)
    cfg = Config.load()
    db = _db_for(root)
    hits = searchmod.where(db, question, cfg, fast=fast)
    db.close()
    if not hits:
        console.print("[yellow]no matches[/] (is the directory indexed?)")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("file:line")
    table.add_column("symbol")
    table.add_column("why")
    for h in hits:
        why = h.why or (h.docstring[:60] if h.docstring else h.kind)
        table.add_row(f"{h.path}:{h.line}", h.qualname, why)
    console.print(table)


@app.command()
def trace(
    target: str = typer.Argument(..., help="file:line or function name."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d"),
    up: int = typer.Option(0, "--up", help="Callers depth."),
    down: int = typer.Option(0, "--down", help="Callees depth."),
    explain: bool = typer.Option(False, "--explain", help="LLM one-liner per node."),
    mermaid: bool = typer.Option(False, "--mermaid", help="Emit mermaid graph."),
):
    """BFS the call graph up/down from TARGET."""
    root = _resolve_project(directory)
    cfg = Config.load()
    db = _db_for(root)

    # default: down 3 unless up given
    if up == 0 and down == 0:
        down = 3
    direction = "up" if up > 0 else "down"
    depth = up if up > 0 else down

    node = graphmod.trace(db, target, direction=direction, depth=depth)
    if node is None:
        db.close()
        console.print(f"[yellow]symbol not found:[/] {target}")
        raise typer.Exit(1)
    if explain:
        graphmod.annotate(db, node, cfg, _body_lookup(root))

    if mermaid:
        console.print(graphmod.to_mermaid(node))
        db.close()
        return

    label = f"[bold]{node.qualname}[/]  {node.path}:{node.line}"
    if node.explain:
        label += f"  [dim]{node.explain}[/]"
    rtree = Tree(label)
    _render(node, rtree)
    console.print(rtree)
    db.close()


def _render(node, rtree):
    # group children by file
    by_file: dict[str, list] = {}
    for e in node.children:
        by_file.setdefault(e.node.path, []).append(e)
    for path, edges in by_file.items():
        branch = rtree.add(f"[cyan]{path}[/]")
        for e in edges:
            conf = e.confidence
            color = {"high": "green", "medium": "yellow", "low": "red"}.get(conf, "white")
            lbl = f"[{color}]{conf}[/] {e.node.qualname}:{e.node.line}"
            if e.node.explain:
                lbl += f"  [dim]{e.node.explain}[/]"
            child_branch = branch.add(lbl)
            if e.node.children:
                _render(e.node, child_branch)


@app.command()
def stack(
    directory: Optional[str] = typer.Option(None, "--dir", "-d"),
    fmt: Optional[str] = typer.Option(None, "--format", help="Force format."),
):
    """Read a runtime stack trace from stdin and bind frames to the index."""
    root = _resolve_project(directory)
    text = sys.stdin.read()
    detected, frames = stackmod.parse(text, fmt)
    db = _db_for(root)
    frames = stackmod.bind(db, frames)
    db.close()
    console.print(f"format: [bold]{detected}[/]  frames: {len(frames)}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("#")
    table.add_column("frame")
    table.add_column("in-index")
    for i, fr in enumerate(frames):
        loc = f"{fr.resolved_path or fr.file}:{fr.line}" if fr.file else "?"
        mark = "[green]yes[/]" if fr.in_index else "[red]no[/]"
        table.add_row(str(i), f"{loc} {fr.func or ''}", mark)
    console.print(table)


@app.command()
def outline(
    file: str = typer.Argument(..., help="File to outline."),
    directory: Optional[str] = typer.Option(None, "--dir", "-d"),
):
    """List symbols defined in FILE."""
    root = _resolve_project(directory)
    db = _db_for(root)
    rel = os.path.relpath(Path(file).resolve(), root)
    rows = db.conn.execute(
        "SELECT s.* FROM symbols s JOIN files f ON f.id=s.file_id "
        "WHERE f.path=? OR f.path LIKE ? ORDER BY s.start_line",
        (rel, f"%{os.path.basename(file)}"),
    ).fetchall()
    db.close()
    if not rows:
        console.print(f"[yellow]no symbols for[/] {file}")
        raise typer.Exit(1)
    table = Table(show_header=True, header_style="bold")
    table.add_column("line")
    table.add_column("kind")
    table.add_column("symbol")
    table.add_column("signature")
    for r in rows:
        table.add_row(str(r["start_line"]), r["kind"], r["qualname"],
                      (r["signature"] or "")[:60])
    console.print(table)


@app.command(name="app")
def app_ui(
    directory: Optional[str] = typer.Option(None, "--dir", "-d"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
):
    """Launch the ephemeral browser UI (127.0.0.1, random port, token)."""
    from .app import serve
    root = _resolve_project(directory)
    serve(root, open_browser=open_browser)


@app.command()
def version():
    """Print version."""
    console.print(f"code-tracer {__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
