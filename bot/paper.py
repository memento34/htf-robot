"""Durable local paper ledger. Exchange access is public market data only."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _floor(value, step):
    value, step = Decimal(str(value)), Decimal(str(step))
    if step <= 0:
        raise ValueError("Invalid contract lot size")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _quote(book):
    if int(time.time() * 1000) - int(book.get("ts", 0)) > 10000:
        raise ValueError("Order book is stale")
    bid = float(book["bids"][0][0])
    ask = float(book["asks"][0][0])
    if bid <= 0 or ask <= bid or (ask - bid) / bid > 0.002:
        raise ValueError("Invalid or wide order book")
    return bid, ask


class PaperBroker:
    def __init__(self, store, start_balance=10000, fee_bps=5, slippage_bps=3):
        self.store = store
        self.fee_rate = float(fee_bps) / 10000
        self.slip_rate = float(slippage_bps) / 10000
        if start_balance <= 0 or self.fee_rate < 0 or self.slip_rate < 0:
            raise ValueError("Invalid paper account configuration")
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paper_account (
                    id INTEGER PRIMARY KEY CHECK(id=1), initial_usdt REAL NOT NULL,
                    cash_usdt REAL NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY, direction TEXT NOT NULL, contracts TEXT NOT NULL,
                    qty_base REAL NOT NULL, entry_price REAL NOT NULL, stop_price REAL NOT NULL,
                    take_price REAL NOT NULL, notional_usd REAL NOT NULL, entry_fee REAL NOT NULL,
                    opened_at TEXT NOT NULL, signal_key TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, symbol TEXT NOT NULL,
                    action TEXT NOT NULL, direction TEXT NOT NULL, contracts TEXT NOT NULL,
                    price REAL NOT NULL, notional_usd REAL NOT NULL, fee_usdt REAL NOT NULL,
                    net_pnl_usdt REAL, reason TEXT NOT NULL, signal_key TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO paper_account VALUES(1,?,?,?)",
                       (float(start_balance), float(start_balance), _now()))

    def account(self):
        with self.store.connect() as db:
            row = db.execute("SELECT initial_usdt,cash_usdt,created_at FROM paper_account WHERE id=1").fetchone()
        return {"initial_usdt": row[0], "cash_usdt": row[1], "created_at": row[2]}

    def positions(self):
        with self.store.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM paper_positions ORDER BY opened_at").fetchall()
        return [dict(row) for row in rows]

    def equity(self, quotes=None):
        quotes = quotes or {}
        account = self.account()
        unrealized = 0.0
        for pos in self.positions():
            price = quotes.get(pos["symbol"], pos["entry_price"])
            unrealized += (price - pos["entry_price"]) * pos["qty_base"] * (1 if pos["direction"] == "long" else -1)
        return account["cash_usdt"] + unrealized

    def open(self, symbol, instrument, decision, candle_ts, book, cfg):
        direction = decision["decision"]
        if direction not in ("long", "short"):
            raise ValueError("Only long or short paper entries")
        bid, ask = _quote(book)
        reference = float(decision["reference_price"])
        raw_price = ask if direction == "long" else bid
        if abs(raw_price / reference - 1) > 0.0025:
            raise ValueError("Price moved over 0.25% since signal")
        fill = raw_price * (1 + self.slip_rate if direction == "long" else 1 - self.slip_rate)
        stop, take = float(decision["stop_loss_price"]), float(decision["take_profit_price"])
        if not (stop < fill < take if direction == "long" else take < fill < stop):
            raise ValueError("Stop or target invalid against paper fill")
        base = symbol.split("-")[0]
        ct_val = Decimal(str(instrument["ctVal"]))
        if instrument.get("ctValCcy") != base or ct_val <= 0:
            raise ValueError("Unsupported contract value unit")
        contracts = _floor(Decimal(str(decision["size_usd"])) / (Decimal(str(fill)) * ct_val), instrument["lotSz"])
        if contracts < Decimal(str(instrument["minSz"])):
            raise ValueError("Risk-approved size below instrument minimum")
        qty = float(contracts * ct_val)
        contracts_text = format(contracts, "f")
        notional = qty * fill
        fee = notional * self.fee_rate
        key = f"{symbol}:{candle_ts}:{direction}"
        opened = _now()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM signals WHERE signal_key=?", (key,)).fetchone():
                return None
            if db.execute("SELECT 1 FROM paper_positions WHERE symbol=?", (symbol,)).fetchone():
                raise ValueError("Position already open")
            count, used = db.execute("SELECT COUNT(*),COALESCE(SUM(notional_usd),0) FROM paper_positions").fetchone()
            cash = db.execute("SELECT cash_usdt FROM paper_account WHERE id=1").fetchone()[0]
            if count >= cfg.max_open_positions or used + notional > cash * cfg.max_total_notional_pct / 100 + 1e-6:
                raise ValueError("Paper position or exposure limit")
            if fee >= cash:
                raise ValueError("Insufficient paper cash")
            db.execute("INSERT INTO signals VALUES(?,?,?,?,?)", (key, symbol, key, "paper_open", opened))
            db.execute("UPDATE paper_account SET cash_usdt=cash_usdt-? WHERE id=1", (fee,))
            db.execute("INSERT INTO paper_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (symbol, direction, contracts_text, qty, fill, stop, take, notional, fee, opened, key))
            db.execute("INSERT INTO paper_trades(ts,symbol,action,direction,contracts,price,notional_usd,fee_usdt,net_pnl_usdt,reason,signal_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (opened, symbol, "open", direction, contracts_text, fill, notional, fee, None, "signal", key))
        result = {"signal_key": key, "symbol": symbol, "direction": direction, "status": "paper_open",
                  "avg_fill_price": fill, "reference_price": reference, "notional_usd": notional,
                  "contracts": contracts_text, "fee_usdt": fee}
        self.store.audit("paper_open", result)
        return result

    def mark_and_close(self, symbol, book):
        bid, ask = _quote(book)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM paper_positions WHERE symbol=?", (symbol,)).fetchone()
            if row is None:
                return None, None
            pos = dict(row)
            direction = pos["direction"]
            exit_quote = bid if direction == "long" else ask
            reason = "stop" if (exit_quote <= pos["stop_price"] if direction == "long" else exit_quote >= pos["stop_price"]) else (
                "take" if (exit_quote >= pos["take_price"] if direction == "long" else exit_quote <= pos["take_price"]) else None)
            if reason is None:
                return exit_quote, None
            fill = exit_quote * (1 - self.slip_rate if direction == "long" else 1 + self.slip_rate)
            gross = (fill - pos["entry_price"]) * pos["qty_base"] * (1 if direction == "long" else -1)
            fee = fill * pos["qty_base"] * self.fee_rate
            net = gross - fee - pos["entry_fee"]
            now = _now()
            hold_ms = max(0, int((datetime.fromisoformat(now)-datetime.fromisoformat(pos["opened_at"])).total_seconds()*1000))
            db.execute("UPDATE paper_account SET cash_usdt=cash_usdt+?-? WHERE id=1", (gross, fee))
            db.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
            db.execute("UPDATE signals SET status=? WHERE signal_key=?", ("paper_closed", pos["signal_key"]))
            db.execute("INSERT INTO paper_trades(ts,symbol,action,direction,contracts,price,notional_usd,fee_usdt,net_pnl_usdt,reason,signal_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (now, symbol, "close", direction, pos["contracts"], fill, fill*pos["qty_base"], fee, net, reason, pos["signal_key"]))
            db.execute("INSERT INTO closed_positions VALUES(?,?,?,?,?)",
                       (pos["signal_key"], int(datetime.fromisoformat(now).timestamp()*1000), net, hold_ms,
                        json.dumps({"symbol": symbol, "direction": direction, "entry": pos["entry_price"], "exit": fill, "reason": reason})))
        result = {"signal_key": pos["signal_key"], "symbol": symbol, "direction": direction,
                  "status": "paper_closed", "avg_fill_price": fill, "net_pnl_usdt": net, "fee_usdt": fee,
                  "reason": reason}
        self.store.audit("paper_close", result)
        return exit_quote, result
