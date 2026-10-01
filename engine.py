"""Deterministic virtual perpetual account. No exchange order client exists."""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import market

SYMBOLS = tuple(s.strip().upper() for s in os.getenv("PAPER_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP").split(",") if s.strip())
if not SYMBOLS or any(not s.endswith("-USDT-SWAP") or not s[:-10].replace("-", "").isalnum() for s in SYMBOLS):
    raise ValueError("PAPER_SYMBOLS must contain USDT swap instrument IDs")
DB_PATH = Path(os.getenv("PAPER_DB_PATH", "/data/paper.sqlite" if os.name != "nt" else "paper.sqlite"))
START_CASH = float(os.getenv("PAPER_START_CASH", "10000"))
if not math.isfinite(START_CASH) or START_CASH <= 0:
    raise ValueError("PAPER_START_CASH must be a positive finite number")
RISK_FRACTION = 0.01
MAX_MARGIN_FRACTION = 0.20
LEVERAGE = 2.0
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
STALE_TICKER_MS = 120_000
STALE_CANDLE_MS = 30 * 60_000
POLL_SECONDS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def signal(candles: list[dict]) -> dict:
    closes = [c["close"] for c in candles]
    fast, slow = mean(closes[-12:]), mean(closes[-36:])
    tr = [max(c["high"] - c["low"], abs(c["high"] - candles[i - 1]["close"]),
              abs(c["low"] - candles[i - 1]["close"])) for i, c in enumerate(candles) if i > 0]
    atr = mean(tr[-14:])
    if fast > slow * 1.001 and closes[-1] > fast:
        direction = "LONG"
    elif fast < slow * 0.999 and closes[-1] < fast:
        direction = "SHORT"
    else:
        direction = "FLAT"
    return {"direction": direction, "fast": fast, "slow": slow, "atr": atr,
            "close": closes[-1], "candle_ts": candles[-1]["ts"]}


class PaperEngine:
    def __init__(self, db_path: Path = DB_PATH, provider=market):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider
        self.lock = threading.RLock()
        self.last_error = ""
        self.errors = {}
        self._init_db()

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions (
                  symbol TEXT PRIMARY KEY, side TEXT NOT NULL, qty REAL NOT NULL,
                  entry REAL NOT NULL, stop REAL NOT NULL, take REAL NOT NULL,
                  entry_fee REAL NOT NULL, opened_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS marks (symbol TEXT PRIMARY KEY, price REAL NOT NULL,
                  market_ts INTEGER NOT NULL, observed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT NOT NULL,
                  action TEXT NOT NULL, price REAL, qty REAL, pnl REAL,
                  reason TEXT NOT NULL, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_recent ON events(id DESC);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('cash', ?)", (str(START_CASH),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('peak_equity', ?)", (str(START_CASH),))
            db.execute("INSERT OR IGNORE INTO meta VALUES ('start_cash', ?)", (str(START_CASH),))

    @staticmethod
    def _meta(db, key, default=""):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    @staticmethod
    def _set(db, key, value):
        db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))

    @staticmethod
    def _event(db, kind, symbol, action, reason, price=None, qty=None, pnl=None, details=None):
        db.execute("INSERT INTO events(ts,kind,symbol,action,price,qty,pnl,reason,details) VALUES (?,?,?,?,?,?,?,?,?)",
                   (utc_now(), kind, symbol, action, price, qty, pnl, reason,
                    json.dumps(details or {}, separators=(",", ":"))))

    def _equity(self, db):
        cash = float(self._meta(db, "cash", START_CASH))
        unrealized = 0.0
        for p in db.execute("SELECT * FROM positions"):
            mark = db.execute("SELECT price FROM marks WHERE symbol=?", (p["symbol"],)).fetchone()
            if mark:
                sign = 1 if p["side"] == "LONG" else -1
                unrealized += (mark[0] - p["entry"]) * p["qty"] * sign
        return cash + unrealized

    def _close(self, db, p, price, reason):
        sign = 1 if p["side"] == "LONG" else -1
        fill = price * (1 - SLIPPAGE_RATE * sign)
        gross = (fill - p["entry"]) * p["qty"] * sign
        exit_fee = fill * p["qty"] * FEE_RATE
        net = gross - p["entry_fee"] - exit_fee
        self._set(db, "cash", float(self._meta(db, "cash")) + gross - exit_fee)
        db.execute("DELETE FROM positions WHERE symbol=?", (p["symbol"],))
        self._event(db, "FILL", p["symbol"], "CLOSE_" + p["side"], reason,
                    fill, p["qty"], net, {"gross": gross, "entry_fee": p["entry_fee"], "exit_fee": exit_fee})

    def _open(self, db, symbol, side, price, atr, equity):
        sign = 1 if side == "LONG" else -1
        fill = price * (1 + SLIPPAGE_RATE * sign)
        distance = max(1.5 * atr, fill * 0.004)
        qty = min(equity * RISK_FRACTION / distance,
                  equity * MAX_MARGIN_FRACTION * LEVERAGE / fill)
        if qty <= 0 or qty * fill < 10:
            self._event(db, "BLOCK", symbol, side, "Position too small after risk sizing")
            return
        fee = fill * qty * FEE_RATE
        stop = fill - sign * distance
        take = fill + sign * 2 * distance
        self._set(db, "cash", float(self._meta(db, "cash")) - fee)
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?)",
                   (symbol, side, qty, fill, stop, take, fee, utc_now()))
        self._event(db, "FILL", symbol, "OPEN_" + side, "Trend rule passed; virtual market fill",
                    fill, qty, -fee, {"stop": stop, "take": take, "risk_usdt": qty * distance,
                                      "fee": fee, "slippage_rate": SLIPPAGE_RATE})

    def tick_symbol(self, symbol: str, now_ms: int | None = None):
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        try:
            tick = self.provider.ticker(symbol)
            bars = self.provider.candles(symbol)
            if now_ms - tick["ts"] > STALE_TICKER_MS or now_ms - bars[-1]["ts"] > STALE_CANDLE_MS:
                raise RuntimeError("Market data stale; no virtual execution")
            if any(not (c["low"] > 0 and c["low"] <= c["close"] <= c["high"]) for c in bars[-50:]):
                raise RuntimeError("Invalid candle data")
            decision = signal(bars)
        except Exception as exc:
            with self.lock, self._connect() as db:
                self.errors[symbol] = f"{symbol}: {exc}"
                self.last_error = " | ".join(self.errors.values())
                last = float(self._meta(db, "error_at:" + symbol, "0"))
                if time.time() - last > 300:
                    self._event(db, "DATA", symbol, "PAUSE", str(exc))
                    self._set(db, "error_at:" + symbol, time.time())
            return

        with self.lock, self._connect() as db:
            self.errors.pop(symbol, None)
            self.last_error = " | ".join(self.errors.values())
            db.execute("INSERT INTO marks VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,market_ts=excluded.market_ts,observed_at=excluded.observed_at",
                       (symbol, tick["price"], tick["ts"], utc_now()))
            p = db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
            protective_exit = False
            if p and ((p["side"] == "LONG" and tick["price"] <= p["stop"]) or
                      (p["side"] == "SHORT" and tick["price"] >= p["stop"])):
                self._close(db, p, tick["price"], "Virtual stop triggered at observed ticker")
                p = None
                protective_exit = True
            elif p and ((p["side"] == "LONG" and tick["price"] >= p["take"]) or
                        (p["side"] == "SHORT" and tick["price"] <= p["take"])):
                self._close(db, p, tick["price"], "Virtual take profit triggered at observed ticker")
                p = None
                protective_exit = True

            candle_key = "last_candle:" + symbol
            if int(self._meta(db, candle_key, "0")) >= decision["candle_ts"]:
                return
            self._set(db, candle_key, decision["candle_ts"])
            equity = self._equity(db)
            day = datetime.now(timezone.utc).date().isoformat()
            if self._meta(db, "day") != day:
                self._set(db, "day", day)
                self._set(db, "day_start_equity", equity)
            peak = max(float(self._meta(db, "peak_equity", equity)), equity)
            self._set(db, "peak_equity", peak)
            details = {**decision, "ticker": tick["price"], "equity": equity,
                       "risk": {"per_trade": RISK_FRACTION, "max_margin": MAX_MARGIN_FRACTION,
                                "leverage": LEVERAGE, "daily_loss": MAX_DAILY_LOSS,
                                "max_drawdown": MAX_DRAWDOWN}}
            self._event(db, "DECISION", symbol, decision["direction"],
                        "Closed 15-minute candle evaluated", tick["price"], details=details)
            if p and decision["direction"] not in (p["side"], "FLAT"):
                self._close(db, p, tick["price"], "Opposite 15-minute trend")
                p = None
                equity = self._equity(db)
            if p or protective_exit or decision["direction"] == "FLAT":
                return
            day_start = float(self._meta(db, "day_start_equity", equity))
            blocks = []
            if equity <= day_start * (1 - MAX_DAILY_LOSS):
                blocks.append("Daily loss limit")
            if equity <= peak * (1 - MAX_DRAWDOWN):
                blocks.append("Peak drawdown limit")
            if db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] >= len(SYMBOLS):
                blocks.append("Position count limit")
            if equity <= 0:
                blocks.append("No positive equity")
            if blocks:
                self._event(db, "BLOCK", symbol, decision["direction"], ", ".join(blocks), details=details)
            else:
                self._open(db, symbol, decision["direction"], tick["price"], decision["atr"], equity)

    def tick(self):
        for symbol in SYMBOLS:
            self.tick_symbol(symbol)

    def snapshot(self):
        with self.lock, self._connect() as db:
            positions = [dict(p) for p in db.execute("SELECT * FROM positions ORDER BY symbol")]
            marks = {m["symbol"]: dict(m) for m in db.execute("SELECT * FROM marks")}
            for p in positions:
                mark = marks.get(p["symbol"])
                p["mark"] = mark["price"] if mark else None
                sign = 1 if p["side"] == "LONG" else -1
                p["unrealized"] = (p["mark"] - p["entry"]) * p["qty"] * sign if mark else 0
            events = [dict(e) for e in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 120")]
            for e in events:
                e["details"] = json.loads(e["details"])
            cash = float(self._meta(db, "cash", START_CASH))
            start_cash = float(self._meta(db, "start_cash", START_CASH))
            equity = cash + sum(p["unrealized"] for p in positions)
            peak = float(self._meta(db, "peak_equity", equity))
            return {"mode": "PAPER ONLY", "source": "OKX public swap data (no account)",
                    "symbols": SYMBOLS, "cash": cash, "equity": equity,
                    "start_cash": start_cash,
                    "unrealized": equity - cash, "return_pct": (equity / start_cash - 1) * 100,
                    "drawdown_pct": (1 - equity / peak) * 100 if peak else 0,
                    "positions": positions, "marks": marks, "events": events,
                    "last_error": self.last_error, "poll_seconds": POLL_SECONDS,
                    "decision_minutes": 15, "server_time": utc_now()}
