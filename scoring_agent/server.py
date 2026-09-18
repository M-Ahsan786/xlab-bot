"""The little local server the app's window talks to.

The UI used to run inside an embedded WebView2 and reach Python through pywebview's bridge.
That bridge deadlocked the window on roughly one launch in ten - blocked with 0% CPU, and no
amount of patching fixed it - so the window is now a Chrome app window and this replaces the
bridge with plain HTTP on localhost.

Shape:
    GET  /            the UI (index.html with app.js inlined)
    POST /api/<name>  call an Api method with a JSON list of arguments, get JSON back
    GET  /events      a Server-Sent Events stream carrying log/progress/done/... to the page

Locked down:
    - bound to 127.0.0.1 only, on a port the OS picks, so nothing off this machine can reach it;
    - every request must carry the random token minted at startup, so no other page or process
      on this machine can drive the agent either;
    - only methods explicitly listed in ALLOWED are callable.
"""
from __future__ import annotations

import json
import queue
import secrets
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# What the page is allowed to call. Anything not here is a 404, whatever it asks for.
SEP = bytes([13, 10])        # SSE record separator

ALLOWED = {
    "pick_folder", "preview", "start_run", "start_make_live", "cancel", "is_running",
    "reset_session", "open_path", "session_status", "sign_out",
    "app_version", "update_settings", "check_update", "install_update",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "ScoringAgent"
    MAX_BODY = 8 * 1024 * 1024        # the page only ever sends small JSON

    # keep the console quiet - the app has its own activity log
    def log_message(self, *a):
        pass

    # ---------- helpers ----------
    def _authed(self) -> bool:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        token = (q.get("t") or [None])[0] or self.headers.get("X-Agent-Token")
        return secrets.compare_digest(str(token or ""), self.server.token)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if not self._authed():
            return self._send(403, b"forbidden", "text/plain")
        if path == "/":
            return self._send(200, self.server.html.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/events":
            return self._events()
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # Read the body FIRST, always. Answering while the request body is still unread leaves
        # the connection out of step and the client sees it aborted - even for a plain 404.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length < 0 or length > self.MAX_BODY:
            return self._json({"error": "request too large"}, 413)
        raw = self.rfile.read(length) if length else b""

        if not self._authed():
            return self._json({"error": "forbidden"}, 403)
        path = self.path.split("?")[0]
        if not path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        name = path[5:]
        if name not in ALLOWED or not hasattr(self.server.api, name):
            return self._json({"error": "unknown method"}, 404)

        try:
            args = json.loads(raw or b"[]")
            if not isinstance(args, list):
                args = [args]
        except Exception:
            return self._json({"error": "bad request"}, 400)

        try:
            return self._json({"result": getattr(self.server.api, name)(*args)})
        except Exception as e:
            return self._json({"error": str(e) or e.__class__.__name__}, 500)

    def _client_gone(self) -> bool:
        """Has the window closed?

        Writing to a socket the other end has closed quietly succeeds for a while, so a failed
        write is not a reliable signal. Peeking for end-of-stream is: the app uses this to know
        when to shut itself down.
        """
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            if not r:
                return False
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except Exception:
            return True

    def _events(self):
        """Hold the connection open and stream whatever the backend emits."""
        q = self.server.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                if self._client_gone():
                    break
                try:
                    item = q.get(timeout=4)
                except queue.Empty:
                    self.wfile.write(b": ping" + SEP + SEP)     # keeps the connection alive
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(item).encode("utf-8") + SEP + SEP)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            self.server.unsubscribe(q)


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, api, html: str):
        super().__init__(("127.0.0.1", 0), _Handler)   # port 0 = let Windows choose a free one
        self.api = api
        self.html = html
        self.token = secrets.token_urlsafe(24)
        self._subs: list = []
        self._lock = threading.Lock()
        self.ever_connected = False      # has the window ever reached us?
        self.last_disconnect = 0.0       # when the last one went away

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    # ---------- events ----------
    @property
    def clients(self) -> int:
        """How many app windows are listening right now."""
        with self._lock:
            return len(self._subs)

    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
            self.ever_connected = True
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)
            if not self._subs:
                self.last_disconnect = time.time()

    def emit(self, name: str, payload):
        """Push an event to the page. Never blocks the caller, never raises."""
        item = {"name": name, "payload": payload}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def start(self):
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.2},
                         daemon=True).start()
        return self
