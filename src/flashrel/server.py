"""Local web server of the dashboard (http://127.0.0.1:8585).

It listens on the loopback interface only, so nothing outside the PC can reach it (and
Windows Firewall does not ask). Every action (POST) must carry the session token that is
embedded in the page, so another web page open in the same browser cannot drive the rig.
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from flashrel.controller import Controller, JobRefused

WEB = Path(__file__).resolve().parent / "web"
# Fixed types: on Windows, mimetypes reads the registry, where ".js" is often text/plain,
# and with nosniff the browser would then refuse to run the dashboard.
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json; charset=utf-8", ".ico": "image/x-icon"}


class RigHTTPServer(ThreadingHTTPServer):
    # Windows' SO_REUSEADDR would let a second program bind a port that is in use, so the
    # fallback to the next free port would never happen.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True
JOB_KINDS = {"enroll", "selftest", "posttest", "recheck", "portcheck", "retire", "checks", "fix", "undo",
             "inventory", "export", "summary", "sim_unplug", "sim_replug", "sim_hub", "sim_enroll_all",
             "sim_move"}


def make_handler(ctrl: Controller):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FlashRel/2"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the log quiet
            return

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    html = (WEB / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", ctrl.token)
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if path.startswith("/static/"):
                    name = unquote(path[len("/static/"):])
                    file = (WEB / name).resolve()
                    if WEB not in file.parents or not file.is_file():
                        return self._send(404, b"not found", "text/plain")
                    ctype = CONTENT_TYPES.get(file.suffix.lower()) or \
                        mimetypes.guess_type(file.name)[0] or "application/octet-stream"
                    return self._send(200, file.read_bytes(), ctype)
                if path == "/api/state":
                    return self._json(ctrl.snapshot_json())
                if path.startswith("/api/drive/"):
                    return self._json(ctrl.drive_detail(unquote(path.rsplit("/", 1)[1])))
                if path == "/api/setup":
                    return self._json(ctrl.setup_info())
                if path == "/api/readiness":
                    return self._json(ctrl.readiness())
                return self._send(404, b"not found", "text/plain")
            except KeyError:
                return self._json({"error": "unknown drive"}, 404)
            except Exception as exc:  # a dashboard request must never take the rig down
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or self.headers.get("X-FlashRel-Token") != ctrl.token:
                return self._json({"error": "forbidden"}, 403)
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad request"}, 400)
            path = urlparse(self.path).path
            try:
                if path == "/api/run/start":
                    return self._json(ctrl.start_test(force=body.get("force") is True))  # only a real true
                if path == "/api/run/end":
                    return self._json(ctrl.end_now(str(body.get("reason", "operator"))))
                if path == "/api/run/pause":
                    return self._json(ctrl.pause(str(body.get("reason", "operator"))))
                if path == "/api/run/resume":
                    return self._json(ctrl.resume())
                if path == "/api/run/finish":
                    return self._json(ctrl.finish())
                if path == "/api/note":
                    return self._json(ctrl.note(str(body.get("text", "")), str(body.get("drive", ""))))
                if path == "/api/temperature":
                    return self._json(ctrl.add_temperature(float(body["celsius"]), str(body.get("location", "room")),
                                                           str(body.get("note", ""))))
                if path == "/api/job":
                    kind = str(body.get("kind", ""))
                    if kind not in JOB_KINDS or (kind.startswith("sim_") and not ctrl.sim_root):
                        return self._json({"error": f"unknown action {kind}"}, 400)
                    return self._json(ctrl.submit(kind, dict(body.get("args") or {})))
                if path == "/api/shutdown":
                    if not body.get("confirm"):
                        return self._json({"error": "confirmation required"}, 400)
                    return self._json(ctrl.request_shutdown())
                return self._json({"error": "not found"}, 404)
            except JobRefused as exc:
                return self._json({"error": str(exc)}, 409)
            except (KeyError, ValueError, TypeError) as exc:
                return self._json({"error": f"bad request: {exc}"}, 400)
            except Exception as exc:
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    return Handler


def serve(ctrl: Controller, port: int = 8585) -> tuple[ThreadingHTTPServer, str]:
    last: Exception | None = None
    for candidate in range(port, port + 15):
        try:
            httpd = RigHTTPServer(("127.0.0.1", candidate), make_handler(ctrl))
        except OSError as exc:
            last = exc
            continue
        threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
        url = f"http://127.0.0.1:{candidate}/"
        ctrl.url = url
        return httpd, url
    raise OSError(f"no free port from {port} to {port + 14}: {last}")
