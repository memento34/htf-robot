"""Keyless OKX public market data. This module contains no trading endpoint."""
from __future__ import annotations

import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://www.okx.com"


def _get(path: str, params: dict[str, str]) -> list:
    url = BASE + path + "?" + urlencode(params)
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "AstraPaper/1.0"})
    with urlopen(request, timeout=10) as response:
        payload = json.load(response)
    if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
        raise RuntimeError("OKX public market data unavailable")
    return payload["data"]


def candles(symbol: str) -> list[dict]:
    rows = _get("/api/v5/market/candles", {"instId": symbol, "bar": "15m", "limit": "100"})
    result = []
    for row in rows:
        if len(row) < 9 or row[8] != "1":
            continue  # Never trade on an unfinished candle.
        result.append({"ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                       "low": float(row[3]), "close": float(row[4])})
    result.sort(key=lambda item: item["ts"])
    if len(result) < 50:
        raise RuntimeError("Not enough confirmed 15-minute candles")
    return result


def ticker(symbol: str) -> dict:
    rows = _get("/api/v5/market/ticker", {"instId": symbol})
    if not rows:
        raise RuntimeError("No ticker data")
    row = rows[0]
    price, stamp = float(row["last"]), int(row["ts"])
    if price <= 0 or stamp > int(time.time() * 1000) + 30_000:
        raise RuntimeError("Invalid market price or timestamp")
    return {"price": price, "ts": stamp}
