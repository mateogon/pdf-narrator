"""Local HTTP server for the desktop window: the app files and the library's media.

Rules: listen on 127.0.0.1 only, random port; media URLs carry a per-session
token; media paths must resolve inside the library folder; Range requests are
supported so audio can seek.
"""

import hmac
import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

STATIC_DIR = Path(__file__).parent / "static"
_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4b": "audio/mp4", ".png": "image/png",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".js": "text/javascript", ".css": "text/css",
          ".html": "text/html; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml"}


def _inside(root, path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class MediaServer:
    def __init__(self, library_root, static_dir=STATIC_DIR):
        self._library_root = library_root  # callable: the folder may change in Settings
        self.static_dir = Path(static_dir).resolve()
        self.token = secrets.token_urlsafe(24)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                server._handle(self, send_body=True)

            def do_HEAD(self):
                server._handle(self, send_body=False)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="narrator-http", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def app_url(self):
        return f"http://127.0.0.1:{self.port}/app/index.html"

    def media_url(self, path):
        """URL for a file inside the library, or None when it is outside."""
        root = Path(self._library_root()).resolve()
        path = Path(path).resolve()
        if not _inside(root, path):
            return None
        relative = quote(path.relative_to(root).as_posix())
        version = int(path.stat().st_mtime) if path.exists() else 0
        return f"/media/{self.token}/{relative}?v={version}"

    # --- request handling --------------------------------------------------------------

    def _handle(self, request, send_body):
        path = unquote(urlsplit(request.path).path)
        if path in {"/", "/app", "/app/"}:
            path = "/app/index.html"
        if path.startswith("/app/"):
            target = (self.static_dir / path[len("/app/"):]).resolve()
            if not _inside(self.static_dir, target) or not target.is_file():
                return self._error(request, 404)
            return self._send_file(request, target, send_body, cache=False)
        match = re.fullmatch(r"/media/([^/]+)/(.+)", path)
        if match:
            if not hmac.compare_digest(match.group(1), self.token):
                return self._error(request, 403)
            root = Path(self._library_root()).resolve()
            target = (root / match.group(2)).resolve()
            if not _inside(root, target) or not target.is_file():
                return self._error(request, 404)
            return self._send_file(request, target, send_body, cache=True)
        return self._error(request, 404)

    def _error(self, request, status):
        request.send_response(status)
        request.send_header("Content-Length", "0")
        request.end_headers()

    def _send_file(self, request, target, send_body, cache):
        size = target.stat().st_size
        content_type = _TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        header = request.headers.get("Range")
        status = 200
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                request.send_response(416)
                request.send_header("Content-Range", f"bytes */{size}")
                request.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
            end = min(end, size - 1)
            if start > end:
                request.send_response(416)
                request.send_header("Content-Range", f"bytes */{size}")
                request.end_headers()
                return
            status = 206
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Accept-Ranges", "bytes")
        request.send_header("Content-Length", str(end - start + 1))
        request.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            request.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        request.end_headers()
        if not send_body:
            return
        with open(target, "rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = handle.read(min(256 * 1024, remaining))
                if not block:
                    break
                try:
                    request.wfile.write(block)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(block)
