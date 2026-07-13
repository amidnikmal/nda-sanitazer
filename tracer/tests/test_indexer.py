from code_tracer.embedder import FakeEmbedder
from code_tracer.indexer import Indexer


def _symbol_names(db):
    return {r["name"] for r in db.conn.execute("SELECT name FROM symbols")}


def _edges(db):
    """Set of (caller_name, callee_name) resolved edges."""
    rows = db.conn.execute(
        "SELECT c.name AS caller, e.callee_name AS callee, e.confidence "
        "FROM edges e JOIN symbols c ON c.id = e.caller_id"
    ).fetchall()
    return {(r["caller"], r["callee"]) for r in rows}


def test_finds_all_symbols(indexed):
    db, stats, _ = indexed
    names = _symbol_names(db)
    for expected in {
        "main", "load_input", "read_file",           # app.py
        "process", "validate", "check_length", "transform",  # service.py
        "formatOutput", "sanitize", "wrap",          # utils.js
    }:
        assert expected in names, f"missing symbol {expected}"


def test_expected_edges(indexed):
    db, _, _ = indexed
    edges = _edges(db)
    expected = {
        ("main", "load_input"),
        ("main", "process"),
        ("load_input", "read_file"),
        ("process", "validate"),
        ("process", "transform"),
        ("validate", "check_length"),
        ("formatOutput", "wrap"),
        ("formatOutput", "sanitize"),
    }
    missing = expected - edges
    assert not missing, f"missing edges: {missing}"


def test_edge_confidence_same_file_is_high(indexed):
    db, _, _ = indexed
    row = db.conn.execute(
        "SELECT e.confidence FROM edges e JOIN symbols c ON c.id=e.caller_id "
        "WHERE c.name='process' AND e.callee_name='validate'"
    ).fetchone()
    assert row["confidence"] == "high"


def test_incremental_no_duplicates(tmp_path, fixture_repo):
    from code_tracer.db import IndexDB
    db = IndexDB(tmp_path / "index.db")
    idx = Indexer(fixture_repo, db, embedder=FakeEmbedder())
    s1 = idx.index()
    n_syms_1 = db.counts()["symbols"]
    n_edges_1 = db.counts()["edges"]

    s2 = idx.index()
    n_syms_2 = db.counts()["symbols"]
    n_edges_2 = db.counts()["edges"]

    assert s2.files_indexed == 0
    assert s2.files_unchanged == s1.files_scanned
    assert n_syms_1 == n_syms_2, "symbols duplicated on reindex"
    assert n_edges_1 == n_edges_2, "edges duplicated on reindex"
    db.close()


def test_reindex_after_change(tmp_path, fixture_repo):
    import shutil
    from code_tracer.db import IndexDB

    work = tmp_path / "repo"
    shutil.copytree(fixture_repo, work)
    db = IndexDB(tmp_path / "index.db")
    idx = Indexer(work, db, embedder=FakeEmbedder())
    idx.index()
    before = db.counts()["symbols"]

    # add a new function to app.py
    app = work / "app.py"
    app.write_text(app.read_text() + "\n\ndef brand_new_fn():\n    return read_file('x')\n")
    idx.index()
    names = {r["name"] for r in db.conn.execute("SELECT name FROM symbols")}
    assert "brand_new_fn" in names
    assert db.counts()["symbols"] == before + 1
    db.close()
