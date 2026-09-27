"""Independent rule agents, risk veto and one final decision point."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(producer, consumer, payload, quality="OK"):
    import uuid
    return {"msg_id": str(uuid.uuid4()), "producer_layer": producer, "consumer_layer": consumer,
            "schema_version": "1.0", "timestamp": utc_now(), "payload": payload,
            "data_quality_flag": quality}


def ema(values, span):
    a = 2 / (span + 1)
    x = values[0]
    for value in values[1:]:
        x = a * value + (1 - a) * x
    return x


def rsi(values, n=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))][-n:]
    gains = sum(max(c, 0) for c in changes) / n
    losses = sum(max(-c, 0) for c in changes) / n
    return 100 if losses == 0 else 100 - 100 / (1 + gains / losses)


def atr(candles, n=14):
    rows = candles[-(n + 1):]
    tr = [max(rows[i]["h"] - rows[i]["l"], abs(rows[i]["h"] - rows[i - 1]["c"]),
              abs(rows[i]["l"] - rows[i - 1]["c"])) for i in range(1, len(rows))]
    return sum(tr) / len(tr) if tr else 0


def make_signal(name, direction="flat", strength=0.0, invalidation=0.0, rationale="insufficient data"):
    return {"agent_name": name, "timestamp": utc_now(), "signal": direction,
            "strength_0_1": round(max(0.0, min(1.0, strength)), 4),
            "suggested_timeframe": "1m", "invalidation_price": round(invalidation, 10),
            "rationale": rationale}


def data_agent(symbol, candles, book, now_ms):
    if len(candles) < 60:
        return envelope("layer_1_data", "layer_8_final_decision", {"symbol": symbol}, "GAP")
    latest = candles[-1]
    gaps = any(b["ts"] - a["ts"] != 60000 for a, b in zip(candles[-60:-1], candles[-59:]))
    stale = now_ms - latest["ts"] > 130000 or now_ms - int(book.get("ts", 0)) > 10000
    bids = sum(float(row[1]) for row in book.get("bids", [])[:5])
    asks = sum(float(row[1]) for row in book.get("asks", [])[:5])
    imbalance = (bids - asks) / (bids + asks) if bids + asks else 0
    quality = "GAP" if gaps or not bids or not asks else "STALE" if stale else "OK"
    payload = {"symbol": symbol, "timestamp": datetime.fromtimestamp(latest["ts"] / 1000, timezone.utc).isoformat(),
               "ohlcv": {"tf": "1m", **{k: latest[k] for k in ("o", "h", "l", "c", "v")}},
               "orderbook_imbalance": round(imbalance, 5), "data_quality_flag": quality}
    return envelope("layer_1_data", "layer_8_final_decision", payload, quality)


def trend_agent(candles):
    name = "trend_momentum"
    if len(candles) < 60:
        return make_signal(name)
    prices = [c["c"] for c in candles]
    fast, slow = ema(prices[-45:], 9), ema(prices[-60:], 21)
    slope = (prices[-1] - prices[-6]) / prices[-6]
    gap = (fast - slow) / slow
    if abs(gap) < 0.0005 or abs(slope) < 0.001 or gap * slope <= 0:
        return make_signal(name, rationale="EMA gap and 5m slope do not align")
    direction = "long" if gap > 0 else "short"
    return make_signal(name, direction, min(abs(gap) * 400, 1),
                       prices[-1] - atr(candles) * 1.5 if direction == "long" else prices[-1] + atr(candles) * 1.5,
                       "EMA9/21 and 5m slope aligned")


def mean_reversion_agent(candles):
    name = "mean_reversion"
    if len(candles) < 60:
        return make_signal(name)
    prices = [c["c"] for c in candles]
    sample = prices[-20:]
    avg = sum(sample) / 20
    sd = math.sqrt(sum((x - avg) ** 2 for x in sample) / 20)
    score = (prices[-1] - avg) / sd if sd else 0
    strength = min(abs(score) / 3, 1)
    if score <= -2 and rsi(prices) < 30:
        return make_signal(name, "long", strength, min(c["l"] for c in candles[-5:]), "Below lower Bollinger band; RSI < 30")
    if score >= 2 and rsi(prices) > 70:
        return make_signal(name, "short", strength, max(c["h"] for c in candles[-5:]), "Above upper Bollinger band; RSI > 70")
    return make_signal(name, rationale="No Bollinger/RSI extreme")


def order_flow_agent(imbalance):
    name = "order_flow"
    if imbalance is None or abs(imbalance) < 0.25:
        return make_signal(name, rationale="Book imbalance below threshold or unavailable")
    return make_signal(name, "long" if imbalance > 0 else "short", min(abs(imbalance), 1),
                       rationale="Top-5 book imbalance threshold crossed")


def volatility_agent(candles):
    name = "volatility_regime"
    if len(candles) < 60:
        return {"agent_name": name, "regime": "unknown", "multiplier": 0.0}
    ratio = atr(candles) / candles[-1]["c"]
    regime = "explosive" if ratio > 0.012 else "normal" if ratio > 0.001 else "low_vol"
    return {"agent_name": name, "regime": regime, "multiplier": 0.0 if regime == "explosive" else 1.0}


def news_agent():
    # No attributed news source is configured. It cannot initiate a trade.
    return {"timestamp": utc_now(), "event_type": "social", "direction": "neutral",
            "severity_1_5": 1, "horizon": "minutes", "confidence_0_1": 0.0,
            "summary": "No verified news feed configured"}


def risk_agent(equity, day_start_equity, positions, pending, cfg, data_quality, clock_drift_ms,
               candidate_symbol=None, candidate_direction=None, cooldown=False):
    daily_pct = 100 * (equity / day_start_equity - 1) if day_start_equity > 0 else -100
    reasons = []
    if data_quality != "OK": reasons.append(f"DATA_{data_quality}")
    if abs(clock_drift_ms) > 2000: reasons.append("CLOCK_DRIFT")
    if daily_pct <= -cfg.daily_loss_limit_pct: reasons.append("DAILY_LOSS")
    if len(positions) >= cfg.max_open_positions: reasons.append("POSITION_LIMIT")
    if pending: reasons.append("OPEN_ORDERS")
    if equity <= 0: reasons.append("NO_EQUITY")
    if cooldown: reasons.append("WHIPSAW_COOLDOWN")
    if candidate_symbol and candidate_direction and candidate_symbol.startswith(("BTC-", "ETH-")):
        for pos in positions:
            other = pos.get("instId", "")
            if not other.startswith(("BTC-", "ETH-")) or other == candidate_symbol:
                continue
            side = pos.get("posSide")
            if side == "net":
                side = "long" if float(pos.get("pos") or 0) > 0 else "short"
            if side == candidate_direction:
                reasons.append("BTC_ETH_CORRELATION")
                break
    used_notional = sum(abs(float(p.get("notionalUsd") or 0)) for p in positions)
    if positions and used_notional <= 0: reasons.append("EXPOSURE_UNKNOWN")
    max_notional = min(max(0, equity * cfg.max_total_notional_pct / 100 - used_notional),
                       equity * cfg.max_leverage)
    if max_notional <= 0: reasons.append("EXPOSURE_LIMIT")
    return {"timestamp": utc_now(), "risk_approved": not reasons,
            "max_position_size_usd": max_notional, "max_leverage": cfg.max_leverage,
            "daily_pnl_pct": round(daily_pct, 4),
            "trading_halted": daily_pct <= -cfg.daily_loss_limit_pct or abs(clock_drift_ms) > 2000,
            "halt_reason": ",".join(reasons) if reasons else None,
            "veto": bool(reasons), "veto_reason": ",".join(reasons) if reasons else None}


def final_decision(signals, weights, news, risk, vol, price, equity, cfg, existing_symbol=False):
    result = {"timestamp": utc_now(), "decision": "hold", "size_usd": 0,
              "leverage": 0, "stop_loss_price": 0, "take_profit_price": 0,
              "confidence_0_1": 0, "contributing_agents": [],
              "veto_applied": risk["veto"], "rationale_summary": ""}
    if risk["veto"] or existing_symbol or vol["multiplier"] == 0:
        result["rationale_summary"] = risk["veto_reason"] or "Existing position or explosive volatility"
        return result
    votes = [(s["signal"], s["strength_0_1"] * weights.get(s["agent_name"], 0), s["agent_name"])
             for s in signals if s["signal"] != "flat" and weights.get(s["agent_name"], 0) > 0]
    signed = sum((1 if d == "long" else -1) * w for d, w, _ in votes)
    total = sum(w for _, w, _ in votes)
    agreement = abs(signed) / total if total else 0
    result["confidence_0_1"] = round(agreement, 4)
    if abs(signed) < 0.6 or agreement < 0.65:
        result["rationale_summary"] = "Weighted signals do not cross agreement threshold"
        return result
    direction = "long" if signed > 0 else "short"
    if news["confidence_0_1"] >= 0.7 and news["severity_1_5"] >= 4 and news["direction"] not in ("neutral", "bullish" if direction == "long" else "bearish"):
        result["rationale_summary"] = "Contrary high-severity news"
        return result
    stop_dist = max(price * 0.003, price * cfg.stop_pct / 100)
    stop = price - stop_dist if direction == "long" else price + stop_dist
    take = price + 1.5 * stop_dist if direction == "long" else price - 1.5 * stop_dist
    risk_notional = equity * cfg.risk_per_trade_pct / 100 * price / stop_dist
    size = min(risk_notional, risk["max_position_size_usd"])
    if size <= 0:
        result["rationale_summary"] = "No risk budget"
        return result
    result.update({"decision": direction, "size_usd": round(size, 4),
                   "leverage": cfg.max_leverage, "stop_loss_price": round(stop, 10),
                   "take_profit_price": round(take, 10),
                   "contributing_agents": [n for d, _, n in votes if d == direction],
                   "rationale_summary": "Two-agent weighted agreement; fixed stop and 1.5R target"})
    return result
