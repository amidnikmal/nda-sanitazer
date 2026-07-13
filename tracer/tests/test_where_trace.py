from code_tracer.config import Config
from code_tracer.embedder import FakeEmbedder
from code_tracer import search as searchmod
from code_tracer import graph as graphmod


def test_where_fast_finds_process(indexed):
    db, _, _ = indexed
    cfg = Config()
    hits = searchmod.where(db, "authentication payload validation", cfg,
                           fast=True, embedder=FakeEmbedder(), top_n=5)
    paths = [f"{h.path}:{h.line}" for h in hits]
    assert any("service.py" in p for p in paths), paths
    # process should be in top 5
    assert any(h.qualname == "process" for h in hits)


def test_where_by_symbol_name(indexed):
    db, _, _ = indexed
    cfg = Config()
    hits = searchmod.where(db, "check_length", cfg, fast=True,
                           embedder=FakeEmbedder(), top_n=5)
    assert any(h.name == "check_length" for h in hits)


def test_trace_down(indexed):
    db, _, _ = indexed
    node = graphmod.trace(db, "main", direction="down", depth=3)
    assert node is not None
    # collect all descendant qualnames
    seen = set()

    def walk(n):
        seen.add(n.qualname)
        for e in n.children:
            walk(e.node)

    walk(node)
    assert {"main", "load_input", "process", "validate", "transform"} <= seen


def test_trace_up(indexed):
    db, _, _ = indexed
    node = graphmod.trace(db, "check_length", direction="up", depth=3)
    assert node is not None
    seen = set()

    def walk(n):
        seen.add(n.qualname)
        for e in n.children:
            walk(e.node)

    walk(node)
    assert {"check_length", "validate", "process"} <= seen


def test_trace_mermaid(indexed):
    db, _, _ = indexed
    node = graphmod.trace(db, "formatOutput", direction="down", depth=2)
    mm = graphmod.to_mermaid(node)
    assert mm.startswith("flowchart TD")
    assert "formatOutput" in mm


def test_resolve_symbol_by_fileline(indexed):
    db, _, _ = indexed
    cands = graphmod.resolve_symbol(db, "pkg/service.py:6")
    assert cands
    assert cands[0]["name"] == "process"
