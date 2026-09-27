"""Password-protected, read-only Railway dashboard using the Python stdlib."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("okxpaper.web")
ASSETS = Path(__file__).with_name("assets")
COOKIE = "okx_paper_session"


class DashboardRuntime:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"state": "starting", "message": "Bot başlatılıyor", "last_cycle": None,
                      "bot_enabled": False, "fast_halt": None, "test_start": None,
                      "test_days": 30, "eligible_count": 0}
        self.store = None

    def update(self, **fields):
        with self._lock:
            self._data.update(fields)

    def snapshot(self):
        with self._lock:
            data = self._data.copy()
            store = self.store
        data["server_time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if store is not None:
            try:
                data.update(store.dashboard_snapshot())
            except Exception as exc:
                data["storage_error"] = str(exc)[:240]
        return data


def _login_page(error=""):
    safe = "Giriş başarısız. Şifreyi kontrol edin." if error else ""
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Paper Lab · Giriş</title>
    <link rel="stylesheet" href="/assets/dashboard.css"></head>
    <body class="gate-body"><main class="gate-card"><div class="gate-mark">↗</div>
    <div class="eyebrow">DEMO TRADING · GÜVENLİ ERİŞİM</div><h1>Paper Lab</h1>
    <p>OKX vadeli işlem robotunun yalnızca okunabilir izleme paneli.</p>
    <form action="/login" method="post"><label for="password">Panel şifresi</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
    <button type="submit" class="primary-button">Panele gir <span>→</span></button></form>
    <div class="form-error" role="alert">{safe}</div>
    <div class="gate-footer"><span class="live-dot"></span> Gerçek para işlemi yok · Salt okunur arayüz</div>
    </main></body></html>"""


def _setup_page():
    return """<!doctype html><html lang="tr"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>OKX Paper Lab · Kurulum</title>
    <link rel="stylesheet" href="/assets/dashboard.css"></head>
    <body class="gate-body"><main class="gate-card"><div class="gate-mark">↗</div>
    <div class="eyebrow">RAILWAY · KURULUM GEREKİYOR</div><h1>Arayüz hazır.</h1>
    <p>İzleme verilerini açmak için Railway Variables bölümüne en az 12 karakterlik
    <code>DASHBOARD_PASSWORD</code> ekleyin ve servisi yeniden dağıtın.</p>
    <div class="setup-note">Bu sayfa şifre ayarlanana kadar hesap ve işlem verisi göstermez.
    Demo API anahtarları ayrıca Railway Variables içinde tutulmalıdır.</div>
    </main></body></html>"""


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, runtime):
        self.runtime = runtime
        self.password = os.getenv("DASHBOARD_PASSWORD", "")
        self.secret = secrets.token_bytes(32)
        self.attempts = {}
        self.attempt_lock = threading.Lock()
        super().__init__(address, DashboardHandler)

    def sign(self, expiry):
        return hmac.new(self.secret, str(expiry).encode(), hashlib.sha256).hexdigest()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status, data, content_type="text/html; charset=utf-8", extra=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, ensure_ascii=False, allow_nan=False), "application/json; charset=utf-8")

    def _authorized(self):
        if len(self.server.password) < 12:
            return False
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar[COOKIE].value
            expiry_text, supplied = token.split(".", 1)
            expiry = int(expiry_text)
            return expiry >= time.time() and hmac.compare_digest(supplied, self.server.sign(expiry))
        except (KeyError, ValueError, TypeError, cookies.CookieError):
            return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok"})
        elif path == "/assets/dashboard.css":
            self._send(200, (ASSETS / "dashboard.css").read_bytes(), "text/css; charset=utf-8")
        elif path == "/assets/dashboard.js":
            self._send(200, (ASSETS / "dashboard.js").read_bytes(), "application/javascript; charset=utf-8")
        elif path == "/":
            if len(self.server.password) < 12:
                self._send(200, _setup_page())
            elif self._authorized():
                self._send(200, (ASSETS / "dashboard.html").read_bytes())
            else:
                self._send(200, _login_page())
        elif path == "/api/status":
            if not self._authorized():
                self._json(401, {"error": "Giriş gerekli"})
            else:
                self._json(200, self.server.runtime.snapshot())
        elif path == "/api/report":
            if not self._authorized():
                self._json(401, {"error": "Giriş gerekli"})
            elif self.server.runtime.store is None:
                self._json(503, {"error": "Veritabanı henüz hazır değil"})
            else:
                days = self.server.runtime.snapshot().get("test_days", 30)
                self._json(200, self.server.runtime.store.performance(days))
        else:
            self._json(404, {"error": "Bulunamadı"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/logout":
            self._send(303, b"", extra={"Location": "/", "Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"})
            return
        if path != "/login" or len(self.server.password) < 12:
            self._json(404, {"error": "Bulunamadı"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 4096 or length <= 0:
            self._send(400, _login_page(error="bad"))
            return
        with self.server.attempt_lock:
            now = time.monotonic()
            key = self.client_address[0]
            recent = [t for t in self.server.attempts.get(key, []) if now-t < 60]
            self.server.attempts[key] = recent
            if len(recent) >= 8:
                self._send(429, _login_page(error="rate"))
                return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        candidate = parse_qs(body).get("password", [""])[0]
        if not hmac.compare_digest(candidate, self.server.password):
            with self.server.attempt_lock:
                self.server.attempts[key].append(time.monotonic())
            self._send(401, _login_page(error="wrong"))
            return
        expiry = int(time.time()) + 12*3600
        token = f"{expiry}.{self.server.sign(expiry)}"
        self._send(303, b"", extra={"Location": "/", "Set-Cookie": f"{COOKIE}={token}; Path=/; Max-Age=43200; HttpOnly; Secure; SameSite=Strict"})


def serve(runtime, port):
    with DashboardServer(("0.0.0.0", int(port)), runtime) as server:
        log.info("Dashboard listening on 0.0.0.0:%s", port)
        server.serve_forever(poll_interval=0.5)
