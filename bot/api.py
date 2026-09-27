"""Read-only OKX production market data. No credentials or trading endpoints."""
from __future__ import annotations

import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = "https://openapi.okx.com"


class OkxError(RuntimeError):
    pass


class OkxClient:
    def __init__(self, min_gap=0.11):
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self):
        with self._lock:
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(self, method, path, params=None, retries=3):
        if method != "GET" or not path.startswith(("/api/v5/public/", "/api/v5/market/")):
            raise ValueError("Only public, read-only OKX market endpoints are permitted")
        query = ("?" + urlencode(params)) if params else ""
        req = Request(HOST + path + query, headers={"User-Agent": "okx-local-paper-lab/2.0"}, method="GET")
        for attempt in range(retries):
            self._throttle()
            try:
                with urlopen(req, timeout=12) as response:
                    payload = json.load(response)
                if payload.get("code") == "0":
                    return payload.get("data", [])
                raise OkxError(f"OKX code={payload.get('code')} msg={payload.get('msg')}")
            except (HTTPError, URLError, TimeoutError) as exc:
                if attempt == retries - 1:
                    raise OkxError(f"GET {path}: {exc}") from exc
                time.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")

    def instruments(self):
        rows = self.request("GET", "/api/v5/public/instruments", {"instType": "SWAP"})
        return {r["instId"]: r for r in rows if r.get("settleCcy") == "USDT"
                and r.get("ctType") == "linear" and r.get("state") == "live"
                and r.get("instId", "").endswith("-USDT-SWAP")}

    def tickers(self):
        return {r["instId"]: r for r in self.request("GET", "/api/v5/market/tickers", {"instType": "SWAP"})}

    def ticker(self, symbol):
        return self.request("GET", "/api/v5/market/ticker", {"instId": symbol})[0]

    def candles(self, symbol, limit=100, history=False, after=None):
        params = {"instId": symbol, "bar": "1m", "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        endpoint = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        raw = self.request("GET", endpoint, params)
        return sorted([{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
                        "l": float(r[3]), "c": float(r[4]), "v": float(r[7]),
                        "confirm": r[8]} for r in raw if len(r) >= 9 and r[8] == "1"], key=lambda x: x["ts"])

    def book(self, symbol):
        return self.request("GET", "/api/v5/market/books", {"instId": symbol, "sz": "5"})[0]

    def server_ms(self):
        return int(self.request("GET", "/api/v5/public/time")[0]["ts"])
