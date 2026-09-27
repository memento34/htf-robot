"""OKX public prices + local paper execution + Railway web dashboard."""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .api import OkxClient
from .backtest import metrics, report_30d, simulate, validate
from .logic import (data_agent, envelope, final_decision, mean_reversion_agent,
                    news_agent, order_flow_agent, risk_agent, trend_agent, volatility_agent)
from .paper import PaperBroker
from .store import Store
from .web import DashboardRuntime, serve as serve_dashboard

log = logging.getLogger("okxpaper")


def number(name, default, typ=float):
    return typ(os.getenv(name, str(default)))


def default_db():
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    return str(Path(volume or "/data") / "paper.sqlite3")


@dataclass(frozen=True)
class Config:
    db: str = field(default_factory=lambda: os.getenv("PAPER_DB", default_db()))
    enabled: bool = field(default_factory=lambda: os.getenv("BOT_ENABLED", "false").lower() == "true")
    interval: int = field(default_factory=lambda: number("SCAN_INTERVAL_SECONDS", 15, int))
    max_symbols: int = field(default_factory=lambda: number("MAX_SYMBOLS_PER_CYCLE", 40, int))
    max_open_positions: int = field(default_factory=lambda: number("MAX_OPEN_POSITIONS", 3, int))
    max_leverage: int = field(default_factory=lambda: number("MAX_LEVERAGE", 3, int))
    risk_per_trade_pct: float = field(default_factory=lambda: number("RISK_PER_TRADE_PCT", 0.5))
    daily_loss_limit_pct: float = field(default_factory=lambda: number("DAILY_LOSS_LIMIT_PCT", 2))
    max_total_notional_pct: float = field(default_factory=lambda: number("MAX_TOTAL_NOTIONAL_PCT", 30))
    min_volume: float = field(default_factory=lambda: number("MIN_24H_VOLUME_USDT", 1_000_000))
    fee_bps: float = field(default_factory=lambda: number("PAPER_FEE_BPS", 5))
    slippage_bps: float = field(default_factory=lambda: number("PAPER_SLIPPAGE_BPS", 3))
    start_balance: float = field(default_factory=lambda: number("PAPER_START_BALANCE_USDT", 10000))
    test_days: int = field(default_factory=lambda: number("PAPER_TEST_DAYS", 30, int))
    allow_pending_wfa: bool = field(default_factory=lambda: os.getenv("PAPER_ALLOW_PENDING_WFA", "true").lower() == "true")
    full_backtest_all: bool = field(default_factory=lambda: os.getenv("FULL_BACKTEST_ALL", "true").lower() == "true")
    stop_pct: float = 0.5

    def check(self):
        if not 1 <= self.max_leverage <= 3: raise ValueError("MAX_LEVERAGE must be 1..3")
        if not 0 < self.risk_per_trade_pct <= 0.5: raise ValueError("RISK_PER_TRADE_PCT must be 0..0.5")
        if not 0 < self.daily_loss_limit_pct <= 2: raise ValueError("DAILY_LOSS_LIMIT_PCT must be 0..2")
        if not 0 < self.max_total_notional_pct <= 30: raise ValueError("MAX_TOTAL_NOTIONAL_PCT must be 0..30")
        if not 1 <= self.max_open_positions <= 3: raise ValueError("MAX_OPEN_POSITIONS must be 1..3")
        if not 1 <= self.max_symbols <= 200: raise ValueError("MAX_SYMBOLS_PER_CYCLE must be 1..200")
        if not 1 <= self.test_days <= 90: raise ValueError("PAPER_TEST_DAYS must be 1..90")
        if self.interval < 10 or self.start_balance <= 0 or self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("Invalid paper configuration")


class Runner:
    def __init__(self, cfg, api, store):
        self.cfg, self.api, self.store = cfg, api, store
        self.paper = PaperBroker(store, cfg.start_balance, cfg.fee_bps, cfg.slippage_bps)
        self.instruments = {}
        self.cursor = 0
        self.last_discovery = 0
        self.validation_thread = None
        self.api_errors = 0
        self.last_direction = {}
        self.test_start = store.get("paper_test_start")
        self.fast_halt = "FAST_NOT_READY"
        self.fast_last = 0.0
        self.fast_errors = 0
        self.eligible_count = 0

    def refresh_universe(self):
        if time.monotonic() - self.last_discovery > 3600 or not self.instruments:
            self.instruments = self.api.instruments()
            self.last_discovery = time.monotonic()
            self.store.audit("universe", {"count": len(self.instruments), "symbols": sorted(self.instruments)})

    def historical_loop(self, runtime=None):
        """Resumable 30-day research over every discovered USDT perpetual."""
        while True:
            symbols = sorted(self.instruments)
            if not symbols:
                time.sleep(10)
                continue
            for symbol in symbols:
                cached = self.store.full_backtest(symbol)
                if cached:
                    age = time.time() - datetime.fromisoformat(cached["ts"]).timestamp()
                    if age < (86400 if cached["status"] == "DONE" else 3600):
                        continue
                if runtime:
                    runtime.update(full_backtest_symbol=symbol)
                try:
                    report = report_30d(self.api, symbol, self.cfg.fee_bps, self.cfg.slippage_bps)
                    self.store.save_full_backtest(symbol, report)
                    log.info("30-day backtest complete: %s", symbol)
                except Exception as exc:
                    self.store.save_full_backtest(symbol, {"symbol": symbol, "error": str(exc)})
                    self.store.audit("full_backtest_error", {"symbol": symbol, "error": str(exc)})
                    log.exception("30-day backtest failed: %s", symbol)
                    time.sleep(60)
                time.sleep(0.25)
            if runtime:
                runtime.update(full_backtest_symbol=None)
            time.sleep(900)

    def start_validation(self, symbol):
        record = self.store.validation(symbol)
        if record and time.time() - datetime.fromisoformat(record["ts"]).timestamp() < 86400:
            return
        if self.validation_thread and self.validation_thread.is_alive():
            return

        def job():
            try:
                report = validate(self.api, symbol, self.cfg.fee_bps, self.cfg.slippage_bps)
                self.store.save_validation(symbol, report["status"], report["weights"], report)
                self.store.audit("walk_forward", report)
            except Exception as exc:
                self.store.audit("walk_forward_error", {"symbol": symbol, "error": str(exc)})
                log.exception("WFA failed for %s", symbol)
        self.validation_thread = threading.Thread(target=job, daemon=True)
        self.validation_thread.start()

    def quick_backtest(self, symbol, candles):
        cached = self.store.quick_backtest(symbol)
        if cached and time.time() - datetime.fromisoformat(cached["ts"]).timestamp() < 60:
            return cached["report"]
        results, weights = {}, {}
        for name in ("trend_momentum", "mean_reversion"):
            result = metrics(simulate(candles[:-1], name, self.cfg.fee_bps, self.cfg.slippage_bps))
            results[name] = result
            weights[name] = 1.0 if result["trade_count"] >= 1 and result["pnl_usd"] > 0 else 0.0
        weights["order_flow"] = 0.0
        report = {"symbol": symbol, "status": "PASS" if any(weights.values()) else "NO_EDGE",
                  "bars": len(candles)-1, "weights": weights, "strategies": results,
                  "note": "Short historical screen; not 90-day walk-forward validation."}
        self.store.save_quick_backtest(symbol, report)
        return report

    def strategy_weights(self, validation, quick):
        quick_weights = quick["weights"]
        if validation and validation["status"] == "APPROVED":
            weights = {name: min(value, quick_weights.get(name, 0)) for name, value in validation["weights"].items()}
            return weights, "APPROVED" if any(weights.values()) else "QUICK_NO_EDGE"
        if validation is None and self.cfg.allow_pending_wfa:
            return quick_weights, "PENDING_PAPER" if any(quick_weights.values()) else "QUICK_NO_EDGE"
        return {}, validation["status"] if validation else "PENDING_BLOCKED"

    def fast_check(self):
        """Check real public bids/asks for simulated stops every five seconds."""
        drift = self.api.server_ms() - int(time.time() * 1000)
        quotes = {}
        for pos in self.paper.positions():
            book = self.api.book(pos["symbol"])
            price, result = self.paper.mark_and_close(pos["symbol"], book)
            if result is None and price is not None:
                quotes[pos["symbol"]] = price
        equity = self.paper.equity(quotes)
        self.store.equity_point(equity)
        day = datetime.now(timezone.utc).date().isoformat()
        baseline = self.store.baseline(day, equity)
        reasons = []
        if abs(drift) > 2000: reasons.append("CLOCK_DRIFT")
        if equity <= 0 or 100 * (equity / baseline - 1) <= -self.cfg.daily_loss_limit_pct:
            reasons.append("DAILY_LOSS")
        old = self.fast_halt
        self.fast_halt = ",".join(reasons) if reasons else None
        self.fast_last = time.monotonic()
        self.fast_errors = 0
        if old != self.fast_halt:
            self.store.audit("fast_path_state", {"halt": self.fast_halt, "equity": equity, "drift_ms": drift})
        return equity, baseline, drift

    def fast_loop(self):
        while True:
            started = time.monotonic()
            try:
                self.fast_check()
            except Exception as exc:
                self.fast_errors += 1
                self.fast_halt = "PRICE_FEED_UNAVAILABLE"
                self.store.audit("fast_path_error", {"error": str(exc), "count": self.fast_errors})
            time.sleep(max(0.5, 5 - (time.monotonic() - started)))

    def cycle(self):
        self.refresh_universe()
        if self.fast_last == 0:
            self.fast_check()
        drift = self.api.server_ms() - int(time.time() * 1000)
        if abs(drift) > 2000:
            self.store.audit("clock_halt", {"drift_ms": drift})
            return
        tickers = self.api.tickers()
        positions = self.paper.positions()
        mark = {s: float(tickers[s].get("last") or 0) for s in tickers if float(tickers[s].get("last") or 0) > 0}
        equity = self.paper.equity(mark)
        self.store.equity_point(equity)
        day = datetime.now(timezone.utc).date().isoformat()
        baseline = self.store.baseline(day, equity)
        compatible = [{"instId": p["symbol"], "posSide": p["direction"], "pos": p["qty_base"],
                       "notionalUsd": p["notional_usd"]} for p in positions]
        def volume_usdt(symbol):
            ticker = tickers[symbol]
            return float(ticker.get("volCcy24h") or 0) * float(ticker.get("last") or 0)
        symbols = sorted((s for s in self.instruments if s in tickers and volume_usdt(s) >= self.cfg.min_volume),
                         key=volume_usdt, reverse=True)
        if not symbols:
            raise ValueError("No liquid production USDT perpetual markets found")
        self.eligible_count = len(symbols)
        if self.cfg.enabled and self.test_start is None:
            self.test_start = self.store.get_or_set("paper_test_start", datetime.now(timezone.utc).isoformat())
        batch = [symbols[(self.cursor+i) % len(symbols)] for i in range(min(self.cfg.max_symbols, len(symbols)))]
        self.cursor = (self.cursor + len(batch)) % len(symbols)
        self.store.audit("cycle_start", {"universe": len(self.instruments), "eligible": len(symbols),
                                          "batch": batch, "equity": equity, "drift_ms": drift})
        expired = self.test_start is not None and time.time() >= datetime.fromisoformat(self.test_start).timestamp() + self.cfg.test_days * 86400
        if expired and self.store.mark_once("paper_test_report_emitted"):
            self.store.audit("paper_test_complete", self.store.performance(self.cfg.test_days))
        for symbol in batch:
            try:
                candles = self.api.candles(symbol, limit=300)
                if len(candles) < 180:
                    continue
                quick = self.quick_backtest(symbol, candles)
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
                vol, news = volatility_agent(candles), news_agent()
                self.start_validation(symbol)
                validation = self.store.validation(symbol)
                weights, validation_status = self.strategy_weights(validation, quick)
                signed = sum((1 if s["signal"] == "long" else -1 if s["signal"] == "short" else 0) *
                             s["strength_0_1"] * weights.get(s["agent_name"], 0) for s in (trend, mean, flow))
                proposed = "long" if signed > 0 else "short" if signed < 0 else None
                previous = self.last_direction.get(symbol)
                cooldown = bool(previous and proposed and previous[0] != proposed and time.monotonic()-previous[1] < 600)
                risk = risk_agent(equity, baseline, compatible, [], self.cfg, data["data_quality_flag"], drift,
                                  candidate_symbol=symbol, candidate_direction=proposed, cooldown=cooldown)
                fast_stale = time.monotonic() - self.fast_last > 15
                if self.api_errors >= 5 or expired or self.store.consecutive_losses_today(day) or self.fast_halt or fast_stale:
                    risk.update({"risk_approved": False, "veto": True, "trading_halted": True,
                                 "veto_reason": self.fast_halt or "TEST_EXPIRED_OR_OPERATIONAL_HALT",
                                 "halt_reason": self.fast_halt or "TEST_EXPIRED_OR_OPERATIONAL_HALT"})
                decision = final_decision([trend, mean, flow], weights, news, risk, vol,
                                          candles[-1]["c"], equity, self.cfg,
                                          any(p["symbol"] == symbol for p in positions))
                decision["reference_price"] = candles[-1]["c"]
                decision["validation_status"] = validation_status
                if decision["decision"] != "hold" and validation_status == "PENDING_PAPER":
                    decision["rationale_summary"] += "; geçici paper sinyali, WFA bekleniyor"
                elif decision["decision"] == "hold" and not any(weights.values()) and not risk["veto"]:
                    decision["rationale_summary"] = ("Kısa geçmiş testinde pozitif sonuç yok" if validation_status == "QUICK_NO_EDGE"
                                                     else "WFA sonucu bekleniyor" if validation is None else "WFA stratejiyi onaylamadı")
                messages = [data,
                            *[envelope("layer_3_"+s["agent_name"], "layer_8_final_decision", s) for s in (trend, mean, flow)],
                            envelope("layer_3_volatility", "layer_8_final_decision", vol),
                            envelope("layer_2_news", "layer_8_final_decision", news, "DEGRADED"),
                            envelope("layer_5_walk_forward", "layer_8_final_decision",
                                     validation or {"status": validation_status, "weights": weights},
                                     "OK" if validation_status == "APPROVED" else "DEGRADED"),
                            envelope("layer_4_quick_backtest", "layer_8_final_decision", quick,
                                     "OK" if quick["status"] == "PASS" else "DEGRADED"),
                            envelope("layer_6_risk", "layer_8_final_decision", risk)]
                self.store.audit("decision", {"symbol": symbol, "inputs": messages, "final": decision})
                if decision["decision"] != "hold":
                    self.last_direction[symbol] = (decision["decision"], time.monotonic())
                if decision["decision"] != "hold" and self.cfg.enabled:
                    result = self.paper.open(symbol, self.instruments[symbol], decision, candles[-1]["ts"], book, self.cfg)
                    if result:
                        positions = self.paper.positions()
                        compatible = [{"instId": p["symbol"], "posSide": p["direction"], "pos": p["qty_base"],
                                       "notionalUsd": p["notional_usd"]} for p in positions]
            except Exception as exc:
                self.api_errors += 1
                self.store.audit("symbol_error", {"symbol": symbol, "error": str(exc)})
                log.exception("Symbol %s failed", symbol)
        self.api_errors = max(0, self.api_errors - 1)


def dashboard_worker(runtime):
    """The HTTP listener starts first and remains available during feed errors."""
    try:
        cfg = Config()
        cfg.check()
        runtime.update(bot_enabled=cfg.enabled, test_days=cfg.test_days,
                       allow_pending_wfa=cfg.allow_pending_wfa)
        volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "")
        if cfg.enabled and os.getenv("RAILWAY_ENVIRONMENT") and (not volume or not Path(cfg.db).is_relative_to(Path(volume))):
            runtime.update(state="setup", message="30 günlük kalıcı paper test için Railway Volume bağlayın ve PAPER_DB yolunu volume altında tutun.")
            return
        store = Store(cfg.db)
        runner = Runner(cfg, OkxClient(), store)
        runtime.store = store
        runtime.update(state="starting", message="Gerçek OKX piyasa verisi bekleniyor.", test_start=runner.test_start)
        threading.Thread(target=runner.fast_loop, daemon=True).start()
        if cfg.full_backtest_all:
            threading.Thread(target=runner.historical_loop, args=(runtime,), daemon=True).start()
        while True:
            started = time.monotonic()
            try:
                runner.cycle()
                runtime.update(state="running" if cfg.enabled else "observe",
                               message="Canlı piyasa verisiyle yerel paper simülasyonu çalışıyor." if cfg.enabled else "İzleme açık. Paper işlem için BOT_ENABLED=true ayarlayın.",
                               last_cycle=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               test_start=runner.test_start,
                               eligible_count=runner.eligible_count, fast_halt=runner.fast_halt,
                               wfa_in_progress="Çalışıyor" if runner.validation_thread and runner.validation_thread.is_alive() else "Beklemede")
            except Exception as exc:
                runner.api_errors += 1
                store.audit("cycle_error", {"error": str(exc)})
                runtime.update(state="error", message=f"Piyasa taraması: {str(exc)[:180]}", fast_halt=runner.fast_halt)
                log.exception("Dashboard worker cycle failed")
            time.sleep(max(1, cfg.interval - (time.monotonic() - started)))
    except Exception as exc:
        runtime.update(state="setup", message=f"Kurulum hatası: {str(exc)[:180]}")
        log.exception("Dashboard worker setup failed")


def main():
    parser = argparse.ArgumentParser(description="OKX real-price local paper trading bot")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run")
    sub.add_parser("serve")
    bt = sub.add_parser("backtest"); bt.add_argument("symbol")
    all_bt = sub.add_parser("backtest-all")
    all_bt.add_argument("--output", default="reports/backtests-30d.jsonl")
    all_bt.add_argument("--limit", type=int, default=0)
    wf = sub.add_parser("validate"); wf.add_argument("symbol")
    sub.add_parser("report")
    sub.add_parser("audit-verify")
    sub.add_parser("list-instruments")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command in (None, "serve"):
        runtime = DashboardRuntime()
        threading.Thread(target=dashboard_worker, args=(runtime,), daemon=True).start()
        serve_dashboard(runtime, int(os.getenv("PORT", "8080")))
        return
    cfg = Config(); cfg.check()
    api = OkxClient()
    if args.command == "list-instruments":
        print(json.dumps(sorted(api.instruments()), indent=2)); return
    if args.command == "backtest-all":
        target = Path(args.output); target.parent.mkdir(parents=True, exist_ok=True)
        finished = set()
        if target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                try: finished.add(json.loads(line)["symbol"])
                except (ValueError, KeyError): pass
        symbols = sorted(api.instruments())
        if args.limit: symbols = symbols[:args.limit]
        with target.open("a", encoding="utf-8") as out:
            for i, symbol in enumerate(symbols, 1):
                if symbol in finished: continue
                try: report = report_30d(api, symbol, cfg.fee_bps, cfg.slippage_bps)
                except Exception as exc: report = {"symbol": symbol, "error": str(exc), "mode": "HISTORICAL_BACKTEST"}
                out.write(json.dumps(report, separators=(",", ":")) + "\n"); out.flush()
                log.info("Backtest %s/%s: %s", i, len(symbols), symbol)
        print(str(target)); return
    store = Store(cfg.db)
    PaperBroker(store, cfg.start_balance, cfg.fee_bps, cfg.slippage_bps)
    if args.command == "report": print(json.dumps(store.performance(cfg.test_days), indent=2))
    elif args.command == "audit-verify": print("OK" if store.verify_audit() else "FAILED")
    elif args.command == "backtest": print(json.dumps(report_30d(api, args.symbol, cfg.fee_bps, cfg.slippage_bps), indent=2))
    elif args.command == "validate":
        report = validate(api, args.symbol, cfg.fee_bps, cfg.slippage_bps)
        store.save_validation(args.symbol, report["status"], report["weights"], report)
        store.audit("walk_forward", report)
        print(json.dumps(report, indent=2))
    elif args.command == "run":
        runner = Runner(cfg, api, store)
        threading.Thread(target=runner.fast_loop, daemon=True).start()
        if cfg.full_backtest_all:
            threading.Thread(target=runner.historical_loop, daemon=True).start()
        while True:
            started = time.monotonic()
            try: runner.cycle()
            except Exception as exc:
                runner.api_errors += 1
                store.audit("cycle_error", {"error": str(exc)})
                log.exception("Cycle failed")
            time.sleep(max(1, cfg.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
