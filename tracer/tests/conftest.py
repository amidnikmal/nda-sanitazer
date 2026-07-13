import os
from pathlib import Path

import pytest

from code_tracer.db import IndexDB
from code_tracer.embedder import FakeEmbedder
from code_tracer.indexer import Indexer

FIXTURE = Path(__file__).parent / "fixtures" / "mini_repo"


@pytest.fixture
def fixture_repo() -> Path:
    return FIXTURE


@pytest.fixture
def index_db(tmp_path) -> IndexDB:
    db = IndexDB(tmp_path / "index.db")
    yield db
    db.close()


@pytest.fixture
def indexed(tmp_path, fixture_repo):
    """Return (db, stats) for a freshly indexed fixture repo (fake embedder)."""
    db = IndexDB(tmp_path / "index.db")
    idx = Indexer(fixture_repo, db, embedder=FakeEmbedder())
    stats = idx.index()
    return db, stats, idx
