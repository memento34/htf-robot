from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from .api import OkxClient, OkxError
from .backtest import report_30d, validate
from .logic import (data_agent, envelope, final_decision, mean_reversion_agent,
                    news_agent, order_flow_agent, risk_agent, trend_agent,
                    volatility_agent)
from .store import Store
from .web import DashboardRuntime, serve as serve_dashboard

log = logging.getLogger("okxpaper")


def number(name, default, typ=float):
    return typ(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Config:
    db: str = os.getenv("BOT_DB", "/data/bot.sqlite3")
    enabled: bool = os.getenv("BOT_ENABLED", "false").lower() == "true"
    interval: int = number("SCAN_INTERVAL_SECONDS", 60, int)
    max_symbols: int = number("MAX_SYMBOLS_PER_CYCLE", 40, int)
    max_open_positions: int = number("MAX_OPEN_POSITIONS", 3, int)
    max_leverage: int = number("MAX_LEVERAGE", 3, int)
    risk_per_trade_pct: float = number("RISK_PER_TRADE_PCT", 0.5)
    daily_loss_limit_pct: float = number("DAILY_LOSS_LIMIT_PCT", 2)
    max_total_notional_pct: float = number("MAX_TOTAL_NOTIONAL_PCT", 30)
    min_volume: float = number("MIN_24H_VOLUME_USDT", 1_000_000)
    fee_bps: float = number("FEE_BPS", 5)
    slippage_bps: float = number("SLIPPAGE_BPS", 3)
    stop_pct: float = 0.5
    position_mode: str = os.getenv("OKX_POSITION_MODE", "net")
    test_days: int = number("PAPER_TEST_DAYS", 30, int)

    def check(self):
        if self.max_leverage < 1 or self.max_leverage > 3:
            raise ValueError("MAX_LEVERAGE must be 1..3")
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 0.5:
            raise ValueError("RISK_PER_TRADE_PCT must be 0..0.5")
        if self.daily_loss_limit_pct <= 0 or self.daily_loss_limit_pct > 2:
            raise ValueError("DAILY_LOSS_LIMIT_PCT must be 0..2")
        if self.max_total_notional_pct <= 0 or self.max_total_notional_pct > 30:
            raise ValueError("MAX_TOTAL_NOTIONAL_PCT must be 0..30")
        if self.max_open_positions < 1 or self.max_open_positions > 3:
            raise ValueError("MAX_OPEN_POSITIONS must be 1..3")
        if self.position_mode not in ("net", "long_short"):
            raise ValueError("OKX_POSITION_MODE must be net or long_short")
        if self.max_symbols < 1 or self.max_symbols > 200:
            raise ValueError("MAX_SYMBOLS_PER_CYCLE must be 1..200")
        if self.test_days < 1 or self.test_days > 90:
            raise ValueError("PAPER_TEST_DAYS must be 1..90")


def client():
    return OkxClient(os.getenv("OKX_DEMO_API_KEY", ""), os.getenv("OKX_DEMO_SECRET_KEY", ""),
                     os.getenv("OKX_DEMO_PASSPHRASE", ""))


def floor_step(value, step):
    value, step = Decimal(str(value)), Decimal(str(step))
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def order_body(symbol, inst, decision, candle_ts, cfg):
    price = Decimal(str(decision["reference_price"]))
    ct_val = Decimal(str(inst["ctVal"]))
    if inst.get("ctValCcy") != symbol.split("-")[0] or ct_val <= 0:
        raise ValueError("Unsupported contract value unit")
    contracts = floor_step(Decimal(str(decision["size_usd"])) / (price * ct_val), inst["lotSz"])
    if contracts < Decimal(str(inst["minSz"])):
        raise ValueError("Risk-approved size is below instrument minimum")
    tick = inst["tickSz"]
    stop = floor_step(decision["stop_loss_price"], tick)
    take = floor_step(decision["take_profit_price"], tick)
    if stop <= 0 or take <= 0:
        raise ValueError("Stop/target rounded to zero")
    if decision["decision"] == "long" and not (stop < price < take):
        raise ValueError("Invalid long stop/target")
    if decision["decision"] == "short" and not (take < price < stop):
        raise ValueError("Invalid short stop/target")
    side = "buy" if decision["decision"] == "long" else "sell"
    signal_key = f"{symbol}:{candle_ts}:{decision['decision']}"
    clid = "P" + hashlib.sha256(signal_key.encode()).hexdigest()[:30]
    body = {"instId": symbol, "tdMode": "cross", "clOrdId": clid,
            "side": side, "ordType": "market", "sz": str(contracts),
            "attachAlgoOrds": [{"tpTriggerPx": str(take), "tpOrdPx": "-1",
                                "slTriggerPx": str(stop), "slOrdPx": "-1"}]}
    if cfg.position_mode == "long_short":
        body["posSide"] = decision["decision"]
    else:
        body["posSide"] = "net"
    return signal_key, body


class Runner:
    def __init__(self, cfg, api, store):
        self.cfg, self.api, self.store = cfg, api, store
        self.instruments = {}
        self.cursor = 0
        self.last_discovery = 0
        self.validation_thread = None
        self.api_errors = 0
        self.execution_halted = False
        self.last_direction = {}
        self.last_history_poll = 0.0
        self.history_unavailable = True
        self.test_start = self.store.get_or_set("paper_test_start", datetime.now(timezone.utc).isoformat()) if cfg.enabled else None
        self.fast_halt = "FAST_NOT_READY" if cfg.enabled else None
        self.fast_last = 0.0
        self.fast_errors = 0
        self.eligible_count = 0

    def refresh_universe(self):
        if time.monotonic() - self.last_discovery > 3600 or not self.instruments:
            self.instruments = self.api.instruments()
            self.last_discovery = time.monotonic()
            self.store.audit("universe", {"count": len(self.instruments), "symbols": sorted(self.instruments)})

    def start_validation(self, symbol):
        record = self.store.validation(symbol)
        if record:
            age = time.time() - datetime.fromisoformat(record["ts"]).timestamp()
            if age < 86400:
                return
        if self.validation_thread and self.validation_thread.is_alive():
            return

        def job():
            try:
                report = validate(self.api, symbol, self.cfg.fee_bps, self.cfg.slippage_bps)
                self.store.save_validation(symbol, report["status"], report["weights"], report)
                self.store.audit("walk_forward", report)
                log.info("WFA %s %s", symbol, report["status"])
            except Exception as exc:
                self.store.audit("walk_forward_error", {"symbol": symbol, "error": str(exc)})
                log.exception("WFA failed for %s", symbol)

        self.validation_thread = threading.Thread(target=job, daemon=True)
        self.validation_thread.start()

    def execute(self, symbol, inst, decision, candle_ts):
        signal_key, body = order_body(symbol, inst, decision, candle_ts, self.cfg)
        if not self.store.reserve(signal_key, symbol, body["clOrdId"]):
            return
        self.store.audit("order_intent", {"signal_key": signal_key, "body": body})
        started = time.monotonic()
        submitted = False
        try:
            # Recheck private state immediately before submission.
            if any(p.get("instId") == symbol and Decimal(p.get("pos") or "0") != 0 for p in self.api.positions()):
                raise OkxError("Position appeared before order")
            if any(o.get("instId") == symbol for o in self.api.pending()):
                raise OkxError("Pending order appeared before order")
            last = Decimal(str(self.api.ticker(symbol).get("last") or "0"))
            reference = Decimal(str(decision["reference_price"]))
            if last <= 0 or abs(last/reference - 1) > Decimal("0.0025"):
                raise OkxError("Price moved over 0.25% since signal")
            attached = body["attachAlgoOrds"][0]
            stop, take = Decimal(attached["slTriggerPx"]), Decimal(attached["tpTriggerPx"])
            if decision["decision"] == "long" and not (stop < last < take):
                raise OkxError("Stop/target invalid against live price")
            if decision["decision"] == "short" and not (take < last < stop):
                raise OkxError("Stop/target invalid against live price")
            self.api.set_leverage(symbol, decision["leverage"])
            submitted = True
            ack = self.api.place(body)
            if not ack or ack[0].get("sCode") != "0":
                raise OkxError(f"Order rejected: {ack}")
            outcome = None
            for _ in range(3):
                try:
                    outcome = self.api.order(symbol, body["clOrdId"])
                    break
                except OkxError:
                    time.sleep(0.5)
            if outcome is None:
                raise OkxError("Accepted order not yet queryable")
            status = outcome.get("state", "unknown")
            self.store.signal_status(signal_key, status)
            self.store.audit("order_result", {"signal_key": signal_key, "status": status,
                         "avg_fill_price": outcome.get("avgPx"), "fill_size": outcome.get("accFillSz"),
                         "latency_ms": round((time.monotonic()-started)*1000),
                         "reference_price": decision["reference_price"], "direction": decision["decision"],
                         "response": outcome})
        except Exception as exc:
            # A transport failure on POST is ambiguous. Query the same clOrdId;
            # never submit a second market order without human reconciliation.
            if submitted:
                try:
                    outcome = self.api.order(symbol, body["clOrdId"])
                    status = outcome.get("state", "unknown")
                except Exception:
                    outcome, status = None, "UNRESOLVED_NO_RETRY"
            else:
                outcome, status = None, "rejected_precheck"
            self.store.signal_status(signal_key, status)
            if status == "UNRESOLVED_NO_RETRY":
                self.execution_halted = True
            self.store.audit("order_error", {"signal_key": signal_key, "error": str(exc),
                                              "reconciled": outcome})
            log.error("Order %s: %s", signal_key, exc)

    def reconcile(self):
        unresolved = self.store.unresolved()
        for signal_key, symbol, clid in unresolved:
            try:
                found = self.api.order(symbol, clid)
                status = found.get("state", "UNRESOLVED_NO_RETRY")
                self.store.signal_status(signal_key, status)
                self.store.audit("order_reconciled", {"signal_key": signal_key, "response": found})
            except Exception:
                pass
        self.execution_halted = bool(self.store.unresolved())

    def poll_closed_positions(self):
        if time.monotonic() - self.last_history_poll < 300:
            return
        min_time = int(datetime.fromisoformat(self.test_start).timestamp()*1000) if self.test_start else int(time.time()*1000)
        cursor = None
        complete = False
        for _ in range(20):
            rows = self.api.positions_history(cursor)
            self.store.ingest_closed(rows, min_time)
            if not rows:
                complete = True
                break
            earliest = min(int(row["uTime"]) for row in rows)
            if len(rows) < 100 or earliest < min_time:
                complete = True
                break
            if earliest == cursor:
                break
            cursor = earliest
        self.history_unavailable = not complete
        self.last_history_poll = time.monotonic()
        if self.history_unavailable:
            self.store.audit("history_gap", {"reason": "Closed-position pagination incomplete"})

    def fast_check(self):
        """Five-second pure-Python protection path. Exchange-attached stops remain primary."""
        reasons = []
        drift = self.api.server_ms() - int(time.time()*1000)
        if abs(drift) > 2000:
            reasons.append("CLOCK_DRIFT")
        balance = self.api.balance()
        equity = float(balance.get("totalEq") or 0)
        day = datetime.now(timezone.utc).date().isoformat()
        baseline = self.store.baseline(day, equity)
        self.store.equity_point(equity)
        if equity <= 0 or (baseline > 0 and 100*(equity/baseline-1) <= -self.cfg.daily_loss_limit_pct):
            reasons.append("DAILY_LOSS")
        for pos in self.api.positions():
            if Decimal(pos.get("pos") or "0") == 0:
                continue
            liquidation = float(pos.get("liqPx") or 0)
            if liquidation <= 0:
                reasons.append("LIQ_PRICE_UNKNOWN")
                continue
            last = float(self.api.ticker(pos["instId"]).get("last") or 0)
            if last <= 0 or abs(last-liquidation)/last < 0.03:
                reasons.append("LIQUIDATION_PROXIMITY")
        old = self.fast_halt
        self.fast_halt = ",".join(sorted(set(reasons))) if reasons else None
        self.fast_last = time.monotonic()
        self.fast_errors = 0
        if self.fast_halt != old:
            self.store.audit("fast_path_state", {"halt": self.fast_halt, "equity": equity, "drift_ms": drift})

    def fast_loop(self):
        while True:
            started = time.monotonic()
            try:
                self.fast_check()
            except Exception as exc:
                self.fast_errors += 1
                if self.fast_errors >= 3:
                    self.fast_halt = "FAST_PATH_API_ERRORS"
                self.store.audit("fast_path_error", {"error": str(exc), "count": self.fast_errors})
            time.sleep(max(0.5, 5 - (time.monotonic()-started)))

    def cycle(self):
        self.refresh_universe()
        now_ms = int(time.time() * 1000)
        drift = self.api.server_ms() - now_ms
        if abs(drift) > 2000:
            self.store.audit("clock_halt", {"drift_ms": drift})
            return
        self.reconcile()
        self.poll_closed_positions()
        balance = self.api.balance()
        equity = float(balance.get("totalEq") or 0)
        if equity <= 0:
            raise OkxError("No demo account equity")
        self.store.equity_point(equity)
        day = datetime.now(timezone.utc).date().isoformat()
        baseline = self.store.baseline(day, equity)
        positions = [p for p in self.api.positions() if Decimal(p.get("pos") or "0") != 0]
        pending = self.api.pending()
        tickers = self.api.tickers()
        symbols = sorted(s for s in self.instruments if s in tickers and
                         float(tickers[s].get("volCcy24h") or 0) * float(tickers[s].get("last") or 0) >= self.cfg.min_volume)
        if not symbols:
            raise OkxError("No liquid USDT perpetual instruments found")
        self.eligible_count = len(symbols)
        batch = [symbols[(self.cursor+i) % len(symbols)] for i in range(min(self.cfg.max_symbols, len(symbols)))]
        self.cursor = (self.cursor + len(batch)) % len(symbols)
        self.store.audit("cycle_start", {"universe": len(self.instruments), "eligible": len(symbols),
                                         "batch": batch, "equity": equity, "drift_ms": drift})
        expired = self.test_start is not None and time.time() >= datetime.fromisoformat(self.test_start).timestamp() + self.cfg.test_days * 86400
        if expired and self.store.mark_once("paper_test_report_emitted"):
            report = self.store.performance(self.cfg.test_days)
            self.store.audit("paper_test_complete", report)
            log.info("PAPER_TEST_COMPLETE %s", json.dumps(report))
        for symbol in batch:
            try:
                candles = self.api.candles(symbol)
                if len(candles) < 60:
                    continue
                trend, mean = trend_agent(candles), mean_reversion_agent(candles)
                if trend["signal"] == mean["signal"] == "flat":
                    continue
                book = self.api.book(symbol)
                data = data_agent(symbol, candles, book, int(time.time()*1000))
                try:
                    bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
                    if bid <= 0 or ask <= bid or (ask-bid)/bid > 0.002:
                        data["data_quality_flag"] = "DEGRADED"
                        data["payload"]["data_quality_flag"] = "DEGRADED"
                except (IndexError, KeyError, ValueError, TypeError):
                    data["data_quality_flag"] = "GAP"
                    data["payload"]["data_quality_flag"] = "GAP"
                flow = order_flow_agent(data["payload"].get("orderbook_imbalance") if data["data_quality_flag"] == "OK" else None)
                vol = volatility_agent(candles)
                news = news_agent()
                self.start_validation(symbol)
                validation = self.store.validation(symbol)
                weights = validation["weights"] if validation and validation["status"] == "APPROVED" else {}
                signed = sum((1 if s["signal"] == "long" else -1 if s["signal"] == "short" else 0) *
                             s["strength_0_1"] * weights.get(s["agent_name"], 0) for s in (trend, mean, flow))
                proposed = "long" if signed > 0 else "short" if signed < 0 else None
                previous = self.last_direction.get(symbol)
                cooldown = bool(previous and proposed and previous[0] != proposed and time.monotonic()-previous[1] < 600)
                risk = risk_agent(equity, baseline, positions, pending, self.cfg, data["data_quality_flag"], drift,
                                  candidate_symbol=symbol, candidate_direction=proposed, cooldown=cooldown)
                fast_stale = self.cfg.enabled and time.monotonic()-self.fast_last > 15
                if self.api_errors >= 5 or self.execution_halted or expired or self.history_unavailable or self.store.consecutive_losses_today(day) or self.fast_halt or fast_stale:
                    risk.update({"risk_approved": False, "veto": True, "trading_halted": True,
                                 "veto_reason": self.fast_halt or "TEST_EXPIRED_OR_OPERATIONAL_HALT",
                                 "halt_reason": self.fast_halt or "TEST_EXPIRED_OR_OPERATIONAL_HALT"})
                decision = final_decision([trend, mean, flow], weights, news, risk, vol,
                                          candles[-1]["c"], equity, self.cfg,
                                          any(p.get("instId") == symbol for p in positions))
                decision["reference_price"] = candles[-1]["c"]
                messages = [data,
                            *[envelope("layer_3_"+s["agent_name"], "layer_8_final_decision", s) for s in (trend, mean, flow)],
                            envelope("layer_3_volatility", "layer_8_final_decision", vol),
                            envelope("layer_2_news", "layer_8_final_decision", news, "DEGRADED"),
                            envelope("layer_5_walk_forward", "layer_8_final_decision", validation or {}, "OK" if weights else "DEGRADED"),
                            envelope("layer_6_risk", "layer_8_final_decision", risk)]
                self.store.audit("decision", {"symbol": symbol, "inputs": messages, "final": decision})
                if decision["decision"] != "hold":
                    self.last_direction[symbol] = (decision["decision"], time.monotonic())
                if decision["decision"] != "hold" and self.cfg.enabled:
                    self.execute(symbol, self.instruments[symbol], decision, candles[-1]["ts"])
            except Exception as exc:
                self.api_errors += 1
                self.store.audit("symbol_error", {"symbol": symbol, "error": str(exc)})
                log.exception("Symbol %s failed", symbol)
        self.api_errors = max(0, self.api_errors - 1)


def dashboard_worker(runtime):
    """Keep the web UI responsive even while OKX or bot setup is unavailable."""
    try:
        cfg = Config()
        cfg.check()
        runtime.update(bot_enabled=cfg.enabled, test_days=cfg.test_days)
        api = client()
        if not all((api.key, api.secret, api.passphrase)):
            runtime.update(state="setup", message="Railway Variables içine OKX demo API anahtarlarını ekleyin.")
            return
        volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        persistent = os.path.ismount(Path(cfg.db).parent) or volume_path == str(Path(cfg.db).parent)
        if cfg.enabled and os.name != "nt" and not persistent:
            runtime.update(state="setup", message="Demo emirleri için BOT_DB klasörüne Railway volume bağlayın.")
            return
        store = Store(cfg.db)
        runtime.store = store
        runner = Runner(cfg, api, store)
        runtime.update(state="starting", message="OKX demo bağlantısı kontrol ediliyor.", test_start=runner.test_start)
        if cfg.enabled:
            threading.Thread(target=runner.fast_loop, daemon=True).start()
        while True:
            started = time.monotonic()
            try:
                runner.cycle()
                runtime.update(state="running" if cfg.enabled else "observe",
                               message="OKX demo taraması çalışıyor." if cfg.enabled else "İzleme açık; BOT_ENABLED=false olduğu için emirler kapalı.",
                               last_cycle=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               eligible_count=runner.eligible_count,
                               fast_halt=runner.fast_halt,
                               wfa_in_progress="Çalışıyor" if runner.validation_thread and runner.validation_thread.is_alive() else "Beklemede")
            except Exception as exc:
                runner.api_errors += 1
                store.audit("cycle_error", {"error": str(exc)})
                runtime.update(state="error", message=f"Bot döngüsü: {str(exc)[:180]}", fast_halt=runner.fast_halt)
                log.exception("Dashboard worker cycle failed")
            time.sleep(max(1, cfg.interval - (time.monotonic() - started)))
    except Exception as exc:
        runtime.update(state="setup", message=f"Kurulum hatası: {str(exc)[:180]}")
        log.exception("Dashboard worker setup failed")


def main():
    parser = argparse.ArgumentParser(description="OKX demo-only USDT perpetual bot")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("serve", help="Railway web dashboard and background demo bot")
    bt = sub.add_parser("backtest")
    bt.add_argument("symbol")
    all_bt = sub.add_parser("backtest-all")
    all_bt.add_argument("--output", default="reports/backtests-30d.jsonl")
    all_bt.add_argument("--limit", type=int, default=0, help="Optional number of symbols; 0 means all")
    wf = sub.add_parser("validate")
    wf.add_argument("symbol")
    sub.add_parser("report")
    sub.add_parser("audit-verify")
    sub.add_parser("list-instruments")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "serve":
        runtime = DashboardRuntime()
        threading.Thread(target=dashboard_worker, args=(runtime,), daemon=True).start()
        serve_dashboard(runtime, int(os.getenv("PORT", "8080")))
        return
    cfg = Config()
    cfg.check()
    api = client()
    if args.command == "list-instruments":
        print(json.dumps(sorted(api.instruments()), indent=2))
        return
    if args.command == "backtest-all":
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        finished = set()
        if target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                try:
                    finished.add(json.loads(line)["symbol"])
                except (ValueError, KeyError):
                    pass
        symbols = sorted(api.instruments())
        if args.limit:
            symbols = symbols[:args.limit]
        with target.open("a", encoding="utf-8") as out:
            for i, symbol in enumerate(symbols, 1):
                if symbol in finished:
                    continue
                try:
                    report = report_30d(api, symbol, cfg.fee_bps, cfg.slippage_bps)
                except Exception as exc:
                    report = {"symbol": symbol, "error": str(exc), "mode": "HISTORICAL_BACKTEST_NOT_FORWARD_PAPER"}
                out.write(json.dumps(report, separators=(",", ":")) + "\n")
                out.flush()
                log.info("Backtest %s/%s: %s", i, len(symbols), symbol)
        print(str(target))
        return
    store = Store(cfg.db)
    if args.command == "report":
        print(json.dumps(store.performance(cfg.test_days), indent=2))
    elif args.command == "audit-verify":
        print("OK" if store.verify_audit() else "FAILED")
    elif args.command == "backtest":
        print(json.dumps(report_30d(api, args.symbol, cfg.fee_bps, cfg.slippage_bps), indent=2))
    elif args.command == "validate":
        report = validate(api, args.symbol, cfg.fee_bps, cfg.slippage_bps)
        store.save_validation(args.symbol, report["status"], report["weights"], report)
        store.audit("walk_forward", report)
        print(json.dumps(report, indent=2))
    elif args.command == "run":
        if cfg.enabled and not all((api.key, api.secret, api.passphrase)):
            raise SystemExit("BOT_ENABLED requires OKX demo API credentials")
        volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        persistent = os.path.ismount(Path(cfg.db).parent) or volume_path == str(Path(cfg.db).parent)
        if cfg.enabled and os.name != "nt" and not persistent:
            raise SystemExit("Mount a persistent Railway volume at the BOT_DB directory before enabling orders")
        runner = Runner(cfg, api, store)
        if cfg.enabled:
            threading.Thread(target=runner.fast_loop, daemon=True).start()
        while True:
            start = time.monotonic()
            try:
                runner.cycle()
            except Exception as exc:
                runner.api_errors += 1
                store.audit("cycle_error", {"error": str(exc)})
                log.exception("Cycle failed")
            time.sleep(max(1, cfg.interval - (time.monotonic() - start)))


if __name__ == "__main__":
    main()
