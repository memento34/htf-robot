"""Single-process Railway web service and paper worker."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import POLL_SECONDS, PaperEngine

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ENGINE = PaperEngine()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send(200, b'{"status":"ok","mode":"paper"}', "application/json")
        if path == "/api/state":
            body = json.dumps(ENGINE.snapshot(), ensure_ascii=False, allow_nan=False).encode("utf-8")
            return self._send(200, body, "application/json; charset=utf-8")
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "application/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path not in assets:
            return self._send(404, b"Not found", "text/plain")
        filename, content_type = assets[path]
        return self._send(200, (STATIC / filename).read_bytes(), content_type)

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print("http:", fmt % args, flush=True)


def worker():
    while True:
        try:
            ENGINE.tick()
        except Exception as exc:
            ENGINE.last_error = f"Worker error: {type(exc).__name__}: {exc}"
            print(ENGINE.last_error, flush=True)
        threading.Event().wait(POLL_SECONDS)


if __name__ == "__main__":
    threading.Thread(target=worker, name="paper-worker", daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"Astra Paper listening on {port}; no private trade endpoint is configured", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
