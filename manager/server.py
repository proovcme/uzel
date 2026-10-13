"""Local HTTP control plane and optional external chat endpoint."""

from __future__ import annotations

import json
import signal
import sys
import sqlite3
import select
import socket
import threading
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import config
from core import Manager, ManagerError
from rooms.http import RoomHTTPMixin


MANAGER: Manager
MAX_BODY = 4 * 1024 * 1024
STATIC = config.ROOT / "static"


class Handler(RoomHTTPMixin, BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.headers.get('Host') not in (f'127.0.0.1:{config.MANAGER_PORT}', f'localhost:{config.MANAGER_PORT}'):
            self._json(403, {'error': 'Unexpected Host header'})
            return False
        return True

    def log_message(self, fmt, *args):
        sys.stderr.write("uzel: " + fmt % args + "\n")

    def _json(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("Request body outside limit")
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ValueError("Expected JSON object")
        return body

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in (f"http://127.0.0.1:{config.MANAGER_PORT}",
                          f"http://localhost:{config.MANAGER_PORT}")

    def _static(self, filename: str, mime: str) -> None:
        data = (STATIC / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _proxy(self, method: str, path: str, body: bytes | None = None) -> None:
        MANAGER.chat_enter()
        try:
            headers = {"Content-Type": "application/json"}
            session = self.headers.get(config.SESSION_HEADER) if config.SESSION_HEADER else None
            if session:
                headers[config.SESSION_HEADER] = session
            request = urllib.request.Request(config.PROXY_ENDPOINT + (path.replace("/api/chat/sessions",config.SESSION_PATH,1) if path.startswith("/api/chat/sessions") else path),
                                             data=body, headers=headers,
                                             method=method)
            try:
                response = urllib.request.urlopen(request, timeout=300)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                self.send_response(response.status)
                for name in ("Content-Type", "Content-Length", *([config.SESSION_HEADER] if config.SESSION_HEADER else [])):
                    value = response.headers.get(name)
                    if value:
                        self.send_header(name, value)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while chunk := response.read1(8192):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        finally:
            MANAGER.chat_leave()

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path.startswith('/api/rooms') and self.rooms_get(MANAGER.rooms):
                return
            if path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.css":
                self._static("app.css", "text/css; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == '/rooms.js':
                self._static('rooms.js', 'text/javascript; charset=utf-8')
            elif path == '/rooms.css':
                self._static('rooms.css', 'text/css; charset=utf-8')
            elif path == "/api/status":
                self._json(200, MANAGER.status())
            elif path == "/api/prompt/status":
                self._json(200, {"job": MANAGER._enhancer.snapshot()})
            elif path == "/api/rag/connection":
                self._json(200, MANAGER.rag.public())
            elif path == "/api/catalog":
                query = parse_qs(urlsplit(self.path).query)
                self._json(200, MANAGER.model_catalog(refresh=query.get("refresh") == ["1"]))
            elif path == "/api/workspace":
                self._json(200, MANAGER.store.index())
            elif path == "/api/workspace/entry":
                query = parse_qs(urlsplit(self.path).query)
                self._json(200, MANAGER.store.get(query.get('id',[''])[0]))
            elif path == "/api/image/gallery":
                self._json(200, {'images': MANAGER.store.gallery()})
            elif path == "/api/image/output":
                query = parse_qs(urlsplit(self.path).query)
                key = query.get('job',[None])[0] or (MANAGER._job or {}).get('id')
                self._static_output(MANAGER.store.output(key))
            elif path == "/api/logs":
                query = parse_qs(urlsplit(self.path).query)
                self._json(200, {"text": MANAGER.logs(query.get("service", ["manager"])[0])})
            elif path == "/api/image/job":
                self._json(200, {"job": MANAGER.status()["job"]})
            elif path == "/api/image/progress":
                self._json(200, MANAGER.image_progress())
            elif path == "/v1/models":
                self._proxy("GET", path)
            elif config.SESSION_PATH and path.startswith("/api/chat/sessions/"):
                self._proxy("GET", path)
            else:
                self._json(404, {"error": "Unknown route"})
        except PermissionError as exc:
            self._json(403, {'error': str(exc)})
        except (ValueError,TypeError) as exc:
            self._json(404, {"error": str(exc)})
        except ManagerError as exc:
            self._json(409, {"error": str(exc)})
        except sqlite3.Error as exc:
            self._json(503, {"error": "Local history storage unavailable: " + str(exc)})
        except (OSError, urllib.error.URLError) as exc:
            self._json(502, {"error": str(exc)})

    def do_POST(self):
        if not self._same_origin():
            self._json(403, {"error": "Cross-origin action refused"})
            return
        path = urlsplit(self.path).path
        try:
            if path.startswith('/api/rooms') and self.rooms_post(MANAGER.rooms):
                return
            if path == '/api/rag/connection':
                self._json(200, MANAGER.rag.save(self._body()))
            elif path == '/api/rag/datasets':
                self._body()
                self._json(200, {'datasets': MANAGER.rag.client().datasets()})
            elif path == "/api/workspace":
                self._json(200, MANAGER.store.put(self._body()))
            elif path == "/api/mode":
                self._json(200, MANAGER.set_mode(self._body().get("mode", "")))
            elif path == "/api/models/start":
                spec = self._body()
                self._json(202, MANAGER.start_model(spec.get("model"), spec.get("backend", "auto")))
            elif path == "/api/models/delete-preview":
                self._json(200, MANAGER.preview_delete(self._body().get("model")))
            elif path == "/api/models/trash":
                spec = self._body()
                self._json(200, MANAGER.trash_model(spec.get("token"), spec.get("confirmation")))
            elif path == "/api/prompt/enhance":
                self._json(202, MANAGER._enhancer.start(self._body()))
            elif path == "/api/image/trash":
                self._json(200, MANAGER.store.trash_images(self._body().get("ids")))
            elif path == "/api/image/series":
                spec = self._body()
                spec["generate"] = True
                self._json(202, MANAGER._enhancer.start(spec, expand=False))
            elif path == "/api/prompt/cancel":
                self._json(200, MANAGER._enhancer.cancel())
            elif path == "/api/chat":
                spec = self._body()
                if spec.get("stream"):
                    MANAGER.validate_chat(spec)
                    self._stream_chat(spec)
                else:
                    self._json(200, MANAGER.chat(spec))
            elif path == '/api/chat/cancel':
                request_id = self._body().get('request_id')
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
                    raise ValueError('Invalid chat request identifier')
                self._json(200, MANAGER.cancel_chat(request_id))
            elif path == "/api/image/jobs":
                self._json(202, MANAGER.generate(self._body()))
            elif path == "/api/image/cancel":
                self._json(200, MANAGER.cancel())
            elif path == "/v1/chat/completions" or (config.SESSION_PATH and path == "/api/chat/sessions"):
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY:
                    raise ValueError("Request body outside limit")
                self._proxy("POST", path, self.rfile.read(length))
            else:
                self._json(404, {"error": "Unknown route"})
        except PermissionError as exc:
            self._json(403, {'error': str(exc)})
        except ManagerError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(422, {"error": str(exc)})
        except sqlite3.Error as exc:
            self._json(503, {"error": "Local history storage unavailable: " + str(exc)})
        except (OSError, urllib.error.URLError) as exc:
            self._json(502, {"error": str(exc)})

    def _stream_chat(self, spec):
        spec = dict(spec, request_id=spec.get('request_id') or uuid.uuid4().hex)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        stream = MANAGER.stream_chat(spec)
        finished = threading.Event()
        def watch_disconnect():
            while not finished.wait(.25):
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if readable and not self.connection.recv(1, socket.MSG_PEEK):
                        MANAGER.cancel_chat(spec['request_id'])
                        return
                except OSError:
                    if not finished.is_set():
                        MANAGER.cancel_chat(spec['request_id'])
                    return
        threading.Thread(target=watch_disconnect, daemon=True).start()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            for line in stream:
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                data = json.dumps({"error": str(exc)}, ensure_ascii=False).encode()
                self.wfile.write(b"data: " + data + b"\n\ndata: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            finished.set()
            stream.close()

    def _static_output(self, path):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_PUT(self):
        if not self._same_origin():
            self._json(403, {"error": "Cross-origin action refused"})
            return
        path = urlsplit(self.path).path
        if not config.SESSION_PATH or not path.startswith("/api/chat/sessions/") or not path.endswith("/state"):
            self._json(404, {"error": "Unknown route"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("Request body outside limit")
            self._proxy("PUT", path, self.rfile.read(length))
        except ManagerError as exc:
            self._json(409, {"error": str(exc)})
        except (ValueError, OSError, urllib.error.URLError) as exc:
            self._json(502, {"error": str(exc)})


def main() -> None:
    global MANAGER
    MANAGER = Manager()
    server = ThreadingHTTPServer((config.MANAGER_HOST, config.MANAGER_PORT), Handler)
    server.daemon_threads = True
    def terminate(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        MANAGER.rooms.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
