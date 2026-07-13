import json
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest

from code_tracer import app as appmod
from code_tracer.config import Config
from code_tracer.db import IndexDB
from code_tracer.embedder import FakeEmbedder
from code_tracer.indexer import Indexer


def _start_server(root, cfg, token, heartbeat=1.0):
    state = appmod._State(root, cfg, token)
    handler_cls = type("BoundHandler", (appmod.Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, state, port


@pytest.fixture
def running_app(tmp_path, fixture_repo):
    # index into a temp db that the app will reopen via project_data_dir;
    # but app opens project_data_dir(root)/index.db, so point XDG there.
    import os
    os.environ["XDG_DATA_HOME"] = str(tmp_path / "xdg")
    from code_tracer.config import project_data_dir
    db = IndexDB(project_data_dir(fixture_repo) / "index.db")
    Indexer(fixture_repo, db, embedder=FakeEmbedder()).index()
    db.close()

    cfg = Config()
    cfg.sanitizer_path = str(tmp_path / "does-not-exist-sanitizer")
    server, state, port = _start_server(fixture_repo, cfg, "secret-token")
    yield {"port": port, "state": state, "cfg": cfg}
    server.shutdown()
    server.server_close()


def _url(port, path, token=None):
    u = f"http://127.0.0.1:{port}{path}"
    if token is not None:
        u += ("&" if "?" in path else "?") + "token=" + token
    return u


def test_no_token_forbidden(running_app):
    port = running_app["port"]
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(_url(port, "/"), timeout=5)
    assert ei.value.code == 403


def test_wrong_token_forbidden(running_app):
    port = running_app["port"]
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(_url(port, "/", token="nope"), timeout=5)
    assert ei.value.code == 403


def test_index_html_served_with_token(running_app):
    port = running_app["port"]
    resp = urllib.request.urlopen(_url(port, "/", token="secret-token"), timeout=5)
    body = resp.read().decode()
    assert resp.status == 200
    assert "code-tracer" in body


def _post(port, path, payload, token="secret-token"):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        _url(port, path, token=token), data=data,
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def test_api_where(running_app):
    port = running_app["port"]
    out = _post(port, "/api/where", {"q": "authentication payload", "fast": True})
    assert out["hits"]
    assert any("service.py" in h["path"] for h in out["hits"])


def test_api_trace(running_app):
    port = running_app["port"]
    out = _post(port, "/api/trace", {"target": "main", "direction": "down", "depth": 3})
    assert out["tree"]["qualname"] == "main"
    assert out["tree"]["children"]


def test_api_stack(running_app):
    port = running_app["port"]
    txt = ('Traceback (most recent call last):\n'
           '  File "app.py", line 8, in main\n    x\n')
    out = _post(port, "/api/stack", {"text": txt})
    assert out["format"] == "python"
    assert out["frames"]


def test_sanitize_toggle_blocks_when_unavailable(running_app):
    port = running_app["port"]
    out = _post(port, "/api/copy", {"text": "secret text", "sanitize": True})
    assert out.get("blocked") is True
    assert "text" not in out  # raw text never returned when blocked


def test_copy_raw_when_toggle_off(running_app):
    port = running_app["port"]
    out = _post(port, "/api/copy", {"text": "hello", "sanitize": False})
    assert out["text"] == "hello"
    assert out["sanitized"] is False


def test_heartbeat_shutdown():
    # a server with a short heartbeat window should shut itself down
    cfg = Config()
    state = appmod._State(__import__("pathlib").Path("."), cfg, "t")
    handler_cls = type("BH", (appmod.Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    # last_heartbeat already far in the past -> watchdog kills quickly
    state.last_heartbeat = time.time() - 100
    wd = threading.Thread(target=appmod._watchdog, args=(server, state, 0.5), daemon=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    wd.start()
    t.join(timeout=5)
    assert not t.is_alive(), "server did not shut down on heartbeat timeout"
    server.server_close()
