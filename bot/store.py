"""Persistent append-only audit and operational state. Use a Railway volume."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(Path(path))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append only'); END;
                CREATE TABLE IF NOT EXISTS signals (
                    signal_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, cl_ord_id TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS baselines (day TEXT PRIMARY KEY, equity REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS validations (
                    symbol TEXT PRIMARY KEY, ts TEXT NOT NULL, status TEXT NOT NULL,
                    weights TEXT NOT NULL, report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quick_backtests (
                    symbol TEXT PRIMARY KEY, ts TEXT NOT NULL, status TEXT NOT NULL,
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS full_backtests (
                    symbol TEXT PRIMARY KEY, ts TEXT NOT NULL, status TEXT NOT NULL,
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS closed_positions (
                    id TEXT PRIMARY KEY, u_time_ms INTEGER NOT NULL, pnl_usd REAL NOT NULL,
                    hold_ms INTEGER, payload TEXT NOT NULL
                );
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
                CREATE INDEX IF NOT EXISTS audit_kind_id ON audit(kind,id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def audit(self, kind, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
            prev = prior[0] if prior else "0" * 64
            digest = hashlib.sha256((prev + ts + kind + encoded).encode()).hexdigest()
            db.execute("INSERT INTO audit(ts,kind,payload,prev_hash,hash) VALUES(?,?,?,?,?)",
                       (ts, kind, encoded, prev, digest))
            return digest

    def verify_audit(self):
        prev = "0" * 64
        with self.connect() as db:
            for ts, kind, payload, prior, digest in db.execute("SELECT ts,kind,payload,prev_hash,hash FROM audit ORDER BY id"):
                if prior != prev or hashlib.sha256((prev + ts + kind + payload).encode()).hexdigest() != digest:
                    return False
                prev = digest
        return True

    def reserve(self, signal_key, symbol, cl_ord_id):
        ts = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            try:
                db.execute("INSERT INTO signals VALUES(?,?,?,?,?)", (signal_key, symbol, cl_ord_id, "reserved", ts))
                return True
            except sqlite3.IntegrityError:
                return False

    def signal_status(self, signal_key, status):
        with self.connect() as db:
            db.execute("UPDATE signals SET status=? WHERE signal_key=?", (status, signal_key))

    def unresolved(self):
        with self.connect() as db:
            return db.execute("SELECT signal_key,symbol,cl_ord_id FROM signals WHERE status IN ('reserved','UNRESOLVED_NO_RETRY')").fetchall()

    def baseline(self, day, current_equity):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO baselines(day,equity) VALUES(?,?)", (day, current_equity))
            return db.execute("SELECT equity FROM baselines WHERE day=?", (day,)).fetchone()[0]

    def get_or_set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, value))
            return db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0]

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def mark_once(self, key, value="1"):
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)", (key, value)).rowcount == 1

    def equity_point(self, value):
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO equity(ts,value) VALUES(?,?)", (ts, value))

    def ingest_closed(self, rows, min_time_ms):
        count = 0
        with self.connect() as db:
            for row in rows:
                try:
                    update = int(row["uTime"])
                    if update < min_time_ms or row.get("type") not in ("2", "3", "6"):
                        continue
                    created = int(row.get("cTime") or update)
                    ident = str(row.get("posId", "")) + ":" + str(update)
                    pnl = float(row.get("realizedPnl") or 0)
                    db.execute("INSERT OR IGNORE INTO closed_positions VALUES(?,?,?,?,?)",
                               (ident, update, pnl, update-created, json.dumps(row, separators=(",", ":"))))
                    count += 1
                except (KeyError, ValueError, TypeError):
                    continue
        return count

    def consecutive_losses_today(self, day):
        day_ms = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)
        with self.connect() as db:
            rows = db.execute("SELECT pnl_usd FROM closed_positions WHERE u_time_ms>=? ORDER BY u_time_ms DESC LIMIT 3", (day_ms,)).fetchall()
        return len(rows) == 3 and all(r[0] < 0 for r in rows)

    def save_validation(self, symbol, status, weights, report):
        ts = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO validations VALUES(?,?,?,?,?)",
                       (symbol, ts, status, json.dumps(weights), json.dumps(report)))

    def validation(self, symbol):
        with self.connect() as db:
            row = db.execute("SELECT ts,status,weights,report FROM validations WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        return {"ts": row[0], "status": row[1], "weights": json.loads(row[2]), "report": json.loads(row[3])}

    def save_quick_backtest(self, symbol, report):
        ts = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO quick_backtests VALUES(?,?,?,?)",
                       (symbol, ts, report["status"], json.dumps(report, separators=(",", ":"))))

    def quick_backtest(self, symbol):
        with self.connect() as db:
            row = db.execute("SELECT ts,status,report FROM quick_backtests WHERE symbol=?", (symbol,)).fetchone()
        return {"ts": row[0], "status": row[1], "report": json.loads(row[2])} if row else None

    def save_full_backtest(self, symbol, report):
        ts = datetime.now(timezone.utc).isoformat()
        status = "ERROR" if "error" in report else "DONE"
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO full_backtests VALUES(?,?,?,?)",
                       (symbol, ts, status, json.dumps(report, separators=(",", ":"))))

    def full_backtest(self, symbol):
        with self.connect() as db:
            row = db.execute("SELECT ts,status FROM full_backtests WHERE symbol=?", (symbol,)).fetchone()
        return {"ts": row[0], "status": row[1]} if row else None

    def dashboard_snapshot(self):
        """Small read-only snapshot for the password-protected web console."""
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            current = db.execute("SELECT ts,value FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
            start = db.execute("SELECT value FROM equity ORDER BY ts ASC LIMIT 1").fetchone()
            baseline = db.execute("SELECT equity FROM baselines WHERE day=?", (day,)).fetchone()
            curve = db.execute("SELECT ts,value FROM equity ORDER BY ts DESC LIMIT 100").fetchall()
            decision_rows = db.execute("SELECT ts,payload FROM audit WHERE kind='decision' ORDER BY id DESC LIMIT 16").fetchall()
            order_rows = db.execute("SELECT ts,kind,payload FROM audit WHERE kind IN ('paper_open','paper_close','order_error') ORDER BY id DESC LIMIT 12").fetchall()
            universe_row = db.execute("SELECT payload FROM audit WHERE kind='universe' ORDER BY id DESC LIMIT 1").fetchone()
            validations = db.execute("SELECT symbol,status,ts FROM validations ORDER BY ts DESC LIMIT 100").fetchall()
            quick_counts = db.execute("SELECT status,COUNT(*) FROM quick_backtests GROUP BY status").fetchall()
            full_counts = db.execute("SELECT status,COUNT(*) FROM full_backtests GROUP BY status").fetchall()
            closed = db.execute("SELECT COUNT(*),COALESCE(SUM(pnl_usd),0) FROM closed_positions").fetchone()
            account = db.execute("SELECT initial_usdt,cash_usdt FROM paper_account WHERE id=1").fetchone()
            open_rows = db.execute("SELECT symbol,direction,contracts,entry_price,stop_price,take_price,notional_usd,opened_at FROM paper_positions ORDER BY opened_at").fetchall()
        current_value = current[1] if current else None
        day_value = baseline[0] if baseline else None
        start_value = account[0] if account else (start[0] if start else None)
        def pct(a, b):
            return round(100*(a/b-1), 3) if a is not None and b and b > 0 else None
        decisions = []
        for ts, payload in decision_rows:
            try:
                row = json.loads(payload)
                final = row.get("final", {})
                decisions.append({"ts": ts, "symbol": row.get("symbol"), "decision": final.get("decision"),
                                  "confidence": final.get("confidence_0_1"), "reason": final.get("rationale_summary"),
                                  "validation_status": final.get("validation_status")})
            except (ValueError, TypeError):
                continue
        orders = []
        for ts, kind, payload in order_rows:
            try:
                row = json.loads(payload)
                orders.append({"ts": ts, "kind": kind, "signal_key": row.get("signal_key"),
                               "status": row.get("status", "error"), "avg_fill_price": row.get("avg_fill_price")})
            except (ValueError, TypeError):
                continue
        universe = json.loads(universe_row[0]) if universe_row else {"count": 0, "symbols": []}
        return {"equity_usd": current_value, "equity_at": current[0] if current else None,
                "paper_cash_usdt": account[1] if account else None,
                "paper_initial_usdt": account[0] if account else None,
                "open_positions": [{"symbol": s, "direction": d, "contracts": c, "entry_price": e,
                                    "stop_price": stop, "take_price": take, "notional_usd": n, "opened_at": ts}
                                   for s,d,c,e,stop,take,n,ts in open_rows],
                "day_return_pct": pct(current_value, day_value), "test_return_pct": pct(current_value, start_value),
                "closed_positions": closed[0], "realized_pnl_usd": closed[1],
                "equity_curve": [{"ts": ts, "value": value} for ts, value in reversed(curve)],
                "decisions": decisions, "orders": orders,
                "universe_count": universe.get("count", 0), "symbols": universe.get("symbols", []),
                "validations": [{"symbol": s, "status": status, "ts": ts} for s, status, ts in validations],
                "quick_backtests": dict(quick_counts), "full_backtests": dict(full_counts)}

    def performance(self, days=30):
        from datetime import timedelta
        import math
        import statistics
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self.connect() as db:
            rows = db.execute("SELECT ts,value FROM equity WHERE ts>=? ORDER BY ts", (cutoff,)).fetchall()
            counts = db.execute("SELECT kind,COUNT(*) FROM audit WHERE ts>=? GROUP BY kind", (cutoff,)).fetchall()
            order_rows = db.execute("SELECT payload FROM audit WHERE ts>=? AND kind='order_result'", (cutoff,)).fetchall()
            closes = db.execute("SELECT pnl_usd,hold_ms FROM closed_positions WHERE u_time_ms>=? ORDER BY u_time_ms",
                                (int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp()*1000),)).fetchall()
        if len(rows) < 2:
            return {"period_days": days, "status": "INSUFFICIENT_FORWARD_DATA", "equity_points": len(rows)}
        vals = [r[1] for r in rows]
        peak, drawdown = vals[0], 0
        for v in vals:
            peak = max(peak, v)
            drawdown = min(drawdown, 100 * (v / peak - 1))
        daily = {}
        for ts, val in rows:
            daily[ts[:10]] = val
        daily_closes = list(daily.values())
        changes = [daily_closes[i]/daily_closes[i-1]-1 for i in range(1,len(daily_closes)) if daily_closes[i-1] > 0]
        sharpe = statistics.mean(changes)/statistics.stdev(changes)*math.sqrt(365) if len(changes)>1 and statistics.stdev(changes) else None
        downside = [min(0,x) for x in changes]
        denominator = math.sqrt(sum(x*x for x in downside)/len(downside)) if downside else 0
        sortino = statistics.mean(changes)/denominator*math.sqrt(365) if denominator else None
        annualized = (vals[-1]/vals[0])**(365/max(1,len(daily)-1))-1 if vals[0]>0 else 0
        calmar = annualized/abs(drawdown/100) if drawdown else None
        order_events = [json.loads(r[0]) for r in order_rows]
        wins = [c[0] for c in closes if c[0] > 0]
        losses = [-c[0] for c in closes if c[0] < 0]
        latencies = [float(o["latency_ms"]) for o in order_events if o.get("latency_ms") is not None]
        slips = []
        for event in order_events:
            try:
                ref, fill = float(event["reference_price"]), float(event["avg_fill_price"])
                sign = 1 if event["direction"] == "long" else -1
                if ref > 0 and fill > 0:
                    slips.append(sign*(fill-ref)/ref*10000)
            except (KeyError, TypeError, ValueError):
                pass
        return {"period_days": days, "from": rows[0][0], "to": rows[-1][0],
                "starting_equity": vals[0], "ending_equity": vals[-1],
                "pnl_usd": vals[-1] - vals[0], "return_pct": 100 * (vals[-1] / vals[0] - 1),
                "max_drawdown_pct": drawdown, "event_counts": dict(counts),
                "sharpe_daily": sharpe, "sortino_daily": sortino, "calmar_annualized": calmar,
                "avg_order_latency_ms": statistics.mean(latencies) if latencies else None,
                "avg_slippage_bps": statistics.mean(slips) if slips else None,
                "closed_positions": len(closes),
                "win_rate_pct": 100*len(wins)/len(closes) if closes else None,
                "profit_factor": sum(wins)/sum(losses) if losses else None,
                "avg_hold_minutes": statistics.mean(c[1]/60000 for c in closes if c[1] is not None) if closes else None,
                "avg_r_multiple": None,
                "note_unavailable": "R multiple needs per-position risk attribution; omitted rather than invented.",
                "note": "Forward local paper observations using OKX production public prices. Fees and slippage are modelled; funding and market impact are not."}
