"""Small OKX v5 client. The host and demo header are intentionally fixed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HOST = "https://openapi.okx.com"


class OkxError(RuntimeError):
    pass


class OkxClient:
    def __init__(self, key="", secret="", passphrase="", min_gap=0.11):
        self.key, self.secret, self.passphrase = key, secret, passphrase
        self.min_gap = min_gap
        self._lock = threading.Lock()
        self._last = 0.0

    def _throttle(self):
        with self._lock:
            wait = self.min_gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def request(self, method, path, params=None, body=None, private=False, retries=3):
        if not path.startswith("/api/v5/"):
            raise ValueError("Only OKX v5 paths are permitted")
        if private and not all((self.key, self.secret, self.passphrase)):
            raise OkxError("Demo API credentials are missing")
        query = ("?" + urlencode(params)) if params else ""
        request_path = path + query
        raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        headers = {"Content-Type": "application/json", "x-simulated-trading": "1", "User-Agent": "okx-paper-research/1.0"}
        if private:
            stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            signature = base64.b64encode(hmac.new(self.secret.encode(), (stamp + method + request_path + raw_body).encode(), hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": signature,
                            "OK-ACCESS-TIMESTAMP": stamp, "OK-ACCESS-PASSPHRASE": self.passphrase})
        req = Request(HOST + request_path, data=raw_body.encode() if body is not None else None,
                      headers=headers, method=method)
        for attempt in range(retries):
            self._throttle()
            try:
                with urlopen(req, timeout=12) as response:
                    payload = json.load(response)
                if payload.get("code") == "0":
                    return payload.get("data", [])
                # Never retry a private POST: a timeout may mean the order succeeded.
                raise OkxError(f"OKX code={payload.get('code')} msg={payload.get('msg')}")
            except (HTTPError, URLError, TimeoutError) as exc:
                if method != "GET" or attempt == retries - 1:
                    raise OkxError(f"{method} {path}: {exc}") from exc
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

    def balance(self):
        return self.request("GET", "/api/v5/account/balance", private=True)[0]

    def positions(self):
        return self.request("GET", "/api/v5/account/positions", {"instType": "SWAP"}, private=True)

    def positions_history(self, after=None):
        params = {"instType": "SWAP", "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        return self.request("GET", "/api/v5/account/positions-history", params, private=True)

    def pending(self):
        return self.request("GET", "/api/v5/trade/orders-pending", {"instType": "SWAP"}, private=True)

    def order(self, symbol, cl_ord_id):
        return self.request("GET", "/api/v5/trade/order", {"instId": symbol, "clOrdId": cl_ord_id}, private=True)[0]

    def set_leverage(self, symbol, leverage):
        return self.request("POST", "/api/v5/account/set-leverage", body={"instId": symbol, "lever": str(leverage), "mgnMode": "cross"}, private=True)

    def place(self, body):
        return self.request("POST", "/api/v5/trade/order", body=body, private=True, retries=1)
