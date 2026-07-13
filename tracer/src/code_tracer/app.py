"""Ephemeral browser UI: 127.0.0.1, random port, one-time token, heartbeat kill.

No daemon, no fixed port, no systemd. The listener dies when the browser tab
stops sending heartbeats (configurable) or on Ctrl+C. Serves a single inlined
static/index.html. All command work runs in-process against the index.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from .config import Config, project_data_dir
from .db import IndexDB
from .embedder import get_embedder
from .indexer import Indexer
from . import graph as graphmod
from . import search as searchmod
from . import stack as stackmod

import sys

log = logging.getLogger("code_tracer.app")


def _static_dir() -> Path:
    """Locate static/ both for a normal install and a PyInstaller freeze."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        cand = base / "static"
        if (cand / "index.html").exists():
            return cand
    # dev/install layout: src/code_tracer/app.py -> repo/static
    return Path(__file__).resolve().parent.parent.parent / "static"


STATIC_DIR = _static_dir()


class _State:
    def __init__(self, root: Path, cfg: Config, token: str):
        self.root = root
        self.cfg = cfg
        self.token = token
        self.last_heartbeat = time.time()
        self.lock = threading.Lock()
        self.index_status = {"running": False, "done": 0, "total": 0, "stats": None}

    def db(self) -> IndexDB:
        return IndexDB(project_data_dir(self.root) / "index.db")


def _static_index_html() -> bytes:
    p = STATIC_DIR / "index.html"
    return p.read_bytes()


class Handler(BaseHTTPRequestHandler):
    state: _State = None  # set on the server instance's handler class

    # silence default logging (avoid leaking snippets/paths to stderr noisily)
    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ---------------------------------------------------------
    def _check_token(self, params) -> bool:
        tok = params.get("token", [None])[0]
        if tok is None:
            tok = self.headers.get("X-Tracer-Token")
        return tok == self.state.token

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forbid(self):
        self.send_response(403)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"403 forbidden: missing or invalid token")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -- GET -------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._check_token(params):
            return self._forbid()
        if parsed.path in ("/", "/index.html"):
            try:
                body = _static_index_html()
            except OSError:
                return self._send_json({"error": "static missing"}, 500)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/config":
            c = self.state.cfg
            return self._send_json({
                "root": str(self.state.root),
                "heartbeat_timeout_s": c.heartbeat_timeout_s,
                "sanitizer_path": c.sanitizer_path,
                "editor_command": c.editor_command,
            })
        if parsed.path == "/api/index/status":
            return self._send_json(self.state.index_status)
        return self._send_json({"error": "not found"}, 404)

    # -- POST ------------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._check_token(params):
            return self._forbid()
        body = self._read_body()
        route = parsed.path
        try:
            if route == "/api/heartbeat":
                self.state.last_heartbeat = time.time()
                return self._send_json({"ok": True})
            if route == "/api/where":
                return self._api_where(body)
            if route == "/api/trace":
                return self._api_trace(body)
            if route == "/api/stack":
                return self._api_stack(body)
            if route == "/api/outline":
                return self._api_outline(body)
            if route == "/api/index":
                return self._api_index(body)
            if route == "/api/open":
                return self._api_open(body)
            if route == "/api/copy":
                return self._api_copy(body)
        except Exception as exc:  # noqa: BLE001
            log.exception("api error")
            return self._send_json({"error": str(exc)}, 500)
        return self._send_json({"error": "not found"}, 404)

    # -- API impls -------------------------------------------------------
    def _api_where(self, body):
        q = body.get("q", "")
        fast = bool(body.get("fast", False))
        db = self.state.db()
        hits = searchmod.where(db, q, self.state.cfg, fast=fast)
        db.close()
        return self._send_json({"hits": [
            {"path": h.path, "line": h.line, "qualname": h.qualname,
             "kind": h.kind, "why": h.why or h.docstring[:80] or h.kind}
            for h in hits
        ]})

    def _api_trace(self, body):
        target = body.get("target", "")
        direction = body.get("direction", "down")
        depth = int(body.get("depth", 3))
        db = self.state.db()
        node = graphmod.trace(db, target, direction=direction, depth=depth)
        db.close()
        if node is None:
            return self._send_json({"error": "not found"}, 404)
        return self._send_json({"tree": _node_json(node)})

    def _api_stack(self, body):
        text = body.get("text", "")
        db = self.state.db()
        fmt, frames = stackmod.parse(text)
        frames = stackmod.bind(db, frames)
        db.close()
        return self._send_json({"format": fmt, "frames": [
            {"file": fr.resolved_path or fr.file, "line": fr.line,
             "func": fr.func, "in_index": fr.in_index}
            for fr in frames
        ]})

    def _api_outline(self, body):
        path = body.get("path", "")
        db = self.state.db()
        rel = os.path.relpath(Path(path).resolve(), self.state.root) if path else ""
        rows = db.conn.execute(
            "SELECT s.* FROM symbols s JOIN files f ON f.id=s.file_id "
            "WHERE f.path=? OR f.path LIKE ? ORDER BY s.start_line",
            (rel, f"%{os.path.basename(path)}"),
        ).fetchall()
        db.close()
        return self._send_json({"symbols": [
            {"line": r["start_line"], "kind": r["kind"],
             "qualname": r["qualname"], "signature": r["signature"] or ""}
            for r in rows
        ]})

    def _api_index(self, body):
        directory = body.get("dir") or str(self.state.root)
        root = Path(directory).resolve()
        st = self.state.index_status
        if st["running"]:
            return self._send_json({"error": "already running"}, 409)

        def run():
            st.update(running=True, done=0, total=0, stats=None)
            try:
                db = IndexDB(project_data_dir(root) / "index.db")
                embedder = get_embedder(self.state.cfg)
                idx = Indexer(root, db, embedder=embedder)

                def prog(done, total, rel, changed):
                    st["done"], st["total"] = done, total

                stats = idx.index(progress=prog)
                db.close()
                st["stats"] = stats.__dict__
            except Exception as exc:  # noqa: BLE001
                st["stats"] = {"error": str(exc)}
            finally:
                st["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return self._send_json({"started": True})

    def _api_open(self, body):
        path = body.get("path", "")
        line = int(body.get("line", 1))
        tmpl = self.state.cfg.editor_command
        cmd = tmpl.format(path=os.path.join(str(self.state.root), path), line=line)
        try:
            subprocess.Popen(shlex.split(cmd))
            return self._send_json({"ok": True})
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"ok": False, "error": str(exc)})

    def _api_copy(self, body):
        text = body.get("text", "")
        sanitize = bool(body.get("sanitize", False))
        if not sanitize:
            return self._send_json({"text": text, "sanitized": False})
        # sanitize path: run external CLI; block on failure, never leak raw
        san = os.path.expanduser(self.state.cfg.sanitizer_path)
        if not Path(san).exists():
            return self._send_json({
                "blocked": True,
                "reason": f"sanitizer not found at {san}; copy blocked",
            }, 200)
        try:
            proc = subprocess.run(
                [san], input=text.encode("utf-8"),
                capture_output=True, timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"blocked": True, "reason": str(exc)})
        if proc.returncode != 0:
            return self._send_json({
                "blocked": True,
                "reason": f"sanitizer exit {proc.returncode}",
            })
        out = proc.stdout.decode("utf-8", "replace")
        vault_id = ""
        m = proc.stderr.decode("utf-8", "replace")
        # sanitizer may print vault_id on stderr; best-effort surface it
        return self._send_json({
            "text": out, "sanitized": True, "vault_id": _extract_vault(m),
        })


def _extract_vault(text: str) -> str:
    import re

    m = re.search(r"\b[0-9a-f]{32}\b", text)
    return m.group(0) if m else ""


def _node_json(node) -> dict:
    return {
        "symbol_id": node.symbol_id,
        "qualname": node.qualname,
        "path": node.path,
        "line": node.line,
        "kind": node.kind,
        "confidence": node.confidence,
        "explain": node.explain,
        "children": [
            {"confidence": e.confidence, **_node_json(e.node)}
            for e in node.children
        ],
    }


def _watchdog(server, state: _State, timeout: float):
    while True:
        time.sleep(1.0)
        if time.time() - state.last_heartbeat > timeout:
            log.info("heartbeat timeout (%.0fs) -> shutting down", timeout)
            server.shutdown()
            return


def serve(root: Path, open_browser: bool = True,
          heartbeat_timeout: Optional[float] = None) -> None:
    cfg = Config.load()
    token = secrets.token_urlsafe(24)
    state = _State(root, cfg, token)
    hb = heartbeat_timeout if heartbeat_timeout is not None else cfg.heartbeat_timeout_s

    handler_cls = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/?token={token}"

    wd = threading.Thread(
        target=_watchdog, args=(server, state, hb), daemon=True
    )
    # give the tab a grace period before first heartbeat
    state.last_heartbeat = time.time() + max(hb, 5)
    wd.start()

    print(f"code-tracer UI: {url}", flush=True)
    print(f"(ephemeral: 127.0.0.1 only, one-time token, dies after "
          f"{hb:.0f}s without heartbeat or on Ctrl+C)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
