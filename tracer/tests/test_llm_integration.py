"""LLM-dependent tests. Auto-skip when llama-server on :8080 is unreachable."""

import pytest

from code_tracer.config import Config
from code_tracer import llm


def _server_up() -> bool:
    llm.clear_probe_cache()
    return llm.server_available(Config(), timeout=0.5)


skip_no_llm = pytest.mark.skipif(
    not _server_up(), reason="llama-server on 127.0.0.1:8080 unavailable"
)


@pytest.mark.integration
@skip_no_llm
def test_where_rerank(indexed):
    from code_tracer import search as searchmod
    db, _, _ = indexed
    cfg = Config()
    hits = searchmod.where(db, "authentication payload validation", cfg, fast=False)
    assert hits


@pytest.mark.integration
@skip_no_llm
def test_trace_explain(indexed):
    from code_tracer import graph as graphmod
    db, _, _ = indexed
    cfg = Config()
    node = graphmod.trace(db, "main", direction="down", depth=2)
    graphmod.annotate(db, node, cfg, lambda row: "def main(): pass")
    assert node is not None


def test_llm_absent_degrades_silently():
    """Core guarantee: chat() returns None (never raises) when server absent."""
    cfg = Config()
    cfg.llm_url = "http://127.0.0.1:1/v1"  # nothing listening
    llm.clear_probe_cache()
    assert llm.server_available(cfg) is False
    assert llm.chat("hello", cfg) is None
