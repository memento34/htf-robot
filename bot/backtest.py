"""Historical 1m test and rolling, out-of-sample strategy gate.

Order book and news are unavailable historically here. Only the candle-based
agents are validated; their paper-trading weights are approved separately.
"""
from __future__ import annotations

import math
import statistics
import time
from datetime import datetime, timezone

from .logic import mean_reversion_agent, trend_agent

DAY_MS = 86_400_000


def download(client, symbol, days):
    cutoff = int(time.time() * 1000) - days * DAY_MS
    cursor = None
    by_ts = {}
    while True:
        chunk = client.candles(symbol, 100, history=True, after=cursor)
        if not chunk:
            break
        for c in chunk:
            if c["ts"] >= cutoff:
                by_ts[c["ts"]] = c
        earliest = chunk[0]["ts"]
        if earliest <= cutoff or earliest == cursor:
            break
        cursor = earliest
    return [by_ts[k] for k in sorted(by_ts)]


def metrics(trades, initial=10000):
    equity = initial
    peak = initial
    max_dd = 0.0
    returns = []
    for trade in trades:
        pnl = trade["pnl_usd"]
        returns.append(pnl / max(equity, 1))
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity / peak - 1) * 100)
    gains = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] > 0)
    losses = -sum(t["pnl_usd"] for t in trades if t["pnl_usd"] < 0)
    sharpe = (statistics.mean(returns) / statistics.stdev(returns) * math.sqrt(len(returns))) if len(returns) > 1 and statistics.stdev(returns) else 0
    downside = [min(0, r) for r in returns]
    sortino = (statistics.mean(returns) / math.sqrt(sum(x*x for x in downside) / len(downside)) * math.sqrt(len(returns))) if downside and any(downside) else 0
    return {"trade_count": len(trades), "pnl_usd": round(equity - initial, 2),
            "return_pct": round(100 * (equity / initial - 1), 4),
            "sharpe": round(sharpe, 4), "sortino": round(sortino, 4),
            "max_drawdown_pct": round(max_dd, 4),
            "win_rate_pct": round(100 * sum(t["pnl_usd"] > 0 for t in trades) / len(trades), 4) if trades else 0,
            "profit_factor": round(gains / losses, 4) if losses else (None if not trades else 999),
            "avg_r_multiple": round(statistics.mean(t["r"] for t in trades), 4) if trades else 0}


def simulate(candles, agent_name, fee_bps=5, slippage_bps=3):
    if len(candles) < 100:
        return []
    agent = trend_agent if agent_name == "trend_momentum" else mean_reversion_agent
    trades = []
    i = 60
    while i < len(candles) - 1:
        if any(candles[j]["ts"] - candles[j-1]["ts"] != 60000 for j in range(i-59, i+1)):
            i += 1
            continue
        signal = agent(candles[i-59:i+1])
        if signal["signal"] == "flat" or signal["strength_0_1"] < 0.5:
            i += 1
            continue
        direction = 1 if signal["signal"] == "long" else -1
        entry = candles[i+1]["o"]
        stop_dist = entry * 0.005
        stop = entry - direction * stop_dist
        target = entry + direction * 1.5 * stop_dist
        exit_price = candles[min(i+16, len(candles)-1)]["c"]
        exit_i = min(i+16, len(candles)-1)
        for j in range(i+1, exit_i+1):
            c = candles[j]
            if direction == 1:
                if c["l"] <= stop:
                    exit_price, exit_i = stop, j
                    break
                if c["h"] >= target:
                    exit_price, exit_i = target, j
                    break
            else:
                if c["h"] >= stop:
                    exit_price, exit_i = stop, j
                    break
                if c["l"] <= target:
                    exit_price, exit_i = target, j
                    break
        notional = 100
        gross = direction * (exit_price - entry) / entry * notional
        costs = notional * (2 * fee_bps + 2 * slippage_bps) / 10000
        pnl = gross - costs
        trades.append({"entry_ts": candles[i+1]["ts"], "exit_ts": candles[exit_i]["ts"],
                       "direction": signal["signal"], "pnl_usd": pnl,
                       "r": pnl / (notional * stop_dist / entry)})
        i = exit_i + 1
    return trades


def report_30d(client, symbol, fee_bps=5, slippage_bps=3):
    candles = download(client, symbol, 30)
    start = datetime.fromtimestamp(candles[0]["ts"] / 1000, timezone.utc).isoformat() if candles else None
    end = datetime.fromtimestamp(candles[-1]["ts"] / 1000, timezone.utc).isoformat() if candles else None
    return {"symbol": symbol, "period": f"{start}/{end}", "bars": len(candles),
            "mode": "HISTORICAL_BACKTEST_NOT_FORWARD_PAPER",
            "assumptions": {"fee_bps_per_side": fee_bps, "slippage_bps_per_side": slippage_bps,
                            "funding_included": False, "book_and_news_included": False,
                            "fill_rule": "next bar open; conservative stop before target if both touched"},
            "strategies": {name: metrics(simulate(candles, name, fee_bps, slippage_bps))
                           for name in ("trend_momentum", "mean_reversion")}}


def validate(client, symbol, fee_bps=5, slippage_bps=3):
    candles = download(client, symbol, 90)
    if len(candles) < 75 * 1440 * 0.9:
        return {"symbol": symbol, "status": "REJECTED", "reason": "Insufficient 1m history", "bars": len(candles), "weights": {}}
    start = candles[0]["ts"]
    windows = []
    approvals = {}
    for name in ("trend_momentum", "mean_reversion"):
        checks = []
        for offset in (0, 15):
            is_start, is_end = start + offset*DAY_MS, start + (offset+60)*DAY_MS
            os_end = is_end + 15*DAY_MS
            ins = [c for c in candles if is_start <= c["ts"] < is_end]
            oos = [c for c in candles if is_end <= c["ts"] < os_end]
            m_in = metrics(simulate(ins, name, fee_bps, slippage_bps))
            m_out = metrics(simulate(oos, name, fee_bps, slippage_bps))
            decay = ((m_in["sharpe"] - m_out["sharpe"]) / max(abs(m_in["sharpe"]), 0.1)) * 100
            checks.append({"window": {"in_sample_days": 60, "out_sample_days": 15, "offset_days": offset},
                           "in_sample": m_in, "out_sample": m_out,
                           "performance_decay_pct": round(decay, 2)})
        approved = all(c["out_sample"]["trade_count"] >= 5 and
                       c["out_sample"]["return_pct"] > 0 and
                       c["out_sample"]["profit_factor"] is not None and
                       c["out_sample"]["profit_factor"] > 1 for c in checks)
        approvals[name] = "APPROVED" if approved else "DEGRADED"
        windows.append({"strategy": name, "parameter_stability": "stable_fixed_parameters",
                        "status": approvals[name], "windows": checks})
    # Order-flow has no historical order-book validation and remains disabled.
    weights = {"trend_momentum": 1.0 if approvals["trend_momentum"] == "APPROVED" else 0,
               "mean_reversion": 1.0 if approvals["mean_reversion"] == "APPROVED" else 0,
               "order_flow": 0.0}
    return {"symbol": symbol, "status": "APPROVED" if any(weights.values()) else "REJECTED",
            "weights": weights, "bars": len(candles), "windows": windows,
            "limitations": "No historical book/news/funding; fixed parameters, not optimized"}
