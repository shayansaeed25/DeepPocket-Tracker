"""Top Gainers (BUY) / Top Losers (SELL) movers strategies -- separate signals.

Two independent strategies share one candidate list (Binance 24h movers
among the already quality-filtered coin universe):

  MOVERS_PULLBACK   user's rule set: pullback/rally to 15m EMA20 or VWAP,
                    1.5x volume confirmation candle, 1h EMA50/EMA200 trend.
  MOVERS_ORDERBLOCK institutional-style: 1h displacement + break of
                    structure leaves an order block; first retest of the
                    zone on 15m with a rejection candle (liquidity sweep
                    and fair value gap raise confidence).

Both use SL beyond structure (never wider than MAX_RISK_PCT), TP1 = 2R,
TP2 = 3.5R. TP1 closes half (paper accounting) and the stop moves to entry.
Signals are WATCH ONLY by default (MOVERS_WATCH_ONLY) so they are paper
forward tests until enough closed trades exist.

This module is self-contained (no import of screener.py) to avoid cycles.
"""
import time

import requests

# ----------------------------- CONFIG ---------------------------------
MOVERS_ENABLED = True
MOVERS_WATCH_ONLY = True          # flip to False only after a good forward test

MIN_MOVE_PCT = 3.0                # 24h change must be at least +3% (gainers) / -3% (losers)
MAX_MOVE_PCT = 20.0               # beyond this the move is late-stage / bounce risk -> skip
MIN_QUOTE_VOLUME_USD = 50_000_000  # 24h volume filter
TOP_N_EACH = 15                   # candidates per side per scan

TRIGGER_WINDOW = 6                # closed 15m candles checked per scan (scan runs every 90 min)
VOLUME_MULT = 1.5                 # confirmation candle volume vs prior-20 average
TOUCH_TOLERANCE = 0.002           # 0.2% proximity counts as "touched EMA20/VWAP"
PULLBACK_LOOKBACK = 3             # candles (incl. trigger) that form the pullback/rally leg
STRUCT_SL_BUFFER = 0.015          # SL 1.5% beyond pullback low / rally high
ATR_SL_MULT = 1.5                 # ... or 1.5 x ATR(15m), whichever is farther (safer)
MAX_RISK_PCT = 4.0                # skip the signal if the stop is wider than this (review: 4-6% stops lost)
MIN_RISK_PCT = 0.4                # skip if the stop is so tight it is just noise
TP1_R = 2.0
TP2_R = 3.5
PARTIAL_TP1_PCT = 0.5             # paper accounting: close 50% at TP1
STALE_R = 0.5                     # skip if price already ran > 0.5R away from the entry

OB_MAX_AGE_1H = 36                # displacement candle must be within the last 36 closed 1h candles
OB_BODY_ATR = 1.2                 # displacement body >= 1.2 x ATR(1h)
OB_VOLUME_MULT = 1.3
OB_STRUCTURE_LOOKBACK = 5         # displacement must break the prior 5-candle high/low
OB_WICK_MIN = 0.4                 # rejection wick >= 40% of candle range
OB_SL_ATR_BUFFER = 0.5            # SL = beyond zone/sweep by 0.5 x ATR(15m)

MAX_OPEN_PER_STRATEGY = 3         # risk management: concurrent paper positions
MAX_OPEN_SAME_SIDE = 2

SCANNERS = ("MOVERS_PULLBACK", "MOVERS_ORDERBLOCK")
LABELS = {"MOVERS_PULLBACK": "Movers Pullback", "MOVERS_ORDERBLOCK": "Movers Order Block"}


# ----------------------------- INDICATORS -----------------------------

def _ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def _atr(highs, lows, closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    trs = [None]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    out[period] = sum(trs[1:period + 1]) / period
    for i in range(period + 1, len(closes)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def _session_vwap(k):
    """VWAP anchored to the UTC day (resets at 00:00 UTC)."""
    out, day, cum_pv, cum_v = [], None, 0.0, 0.0
    for i, ts in enumerate(k["open_time"]):
        d = int(ts) // 86_400_000
        if d != day:
            day, cum_pv, cum_v = d, 0.0, 0.0
        typical = (k["high"][i] + k["low"][i] + k["close"][i]) / 3
        cum_pv += typical * k["volume"][i]
        cum_v += k["volume"][i]
        out.append(cum_pv / cum_v if cum_v > 0 else k["close"][i])
    return out


def _avg(values):
    return sum(values) / len(values) if values else 0.0


# ----------------------------- CANDIDATES -----------------------------

def fetch_movers(base_url, universe, timeout=20):
    """24h tickers for the coin universe: [{symbol, pct, quote_volume, last}]."""
    resp = requests.get(f"{base_url}/api/v3/ticker/24hr", timeout=timeout)
    if resp.status_code != 200:
        return []
    allowed = set(universe)
    out = []
    for row in resp.json():
        symbol = row.get("symbol")
        if symbol not in allowed:
            continue
        try:
            out.append({"symbol": symbol, "pct": float(row["priceChangePercent"]),
                        "quote_volume": float(row["quoteVolume"]), "last": float(row["lastPrice"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def pick_candidates(tickers):
    """Return (gainers, losers) passing the 24h move and volume filters."""
    liquid = [t for t in tickers if t["quote_volume"] >= MIN_QUOTE_VOLUME_USD]
    gainers = sorted((t for t in liquid if MIN_MOVE_PCT <= t["pct"] <= MAX_MOVE_PCT),
                     key=lambda t: t["pct"], reverse=True)[:TOP_N_EACH]
    losers = sorted((t for t in liquid if -MAX_MOVE_PCT <= t["pct"] <= -MIN_MOVE_PCT),
                    key=lambda t: t["pct"])[:TOP_N_EACH]
    return gainers, losers


# ----------------------------- SHARED HELPERS -------------------------

def _finish(symbol, scanner, is_long, entry, sl, trigger_close_ts, k15, trigger_idx, ticker,
            timeframe, confidence, details):
    """Apply risk filters, build TP1/TP2, run staleness checks, return signal or None."""
    risk = entry - sl if is_long else sl - entry
    if risk <= 0 or entry <= 0:
        return None
    risk_pct = risk / entry * 100
    if risk_pct > MAX_RISK_PCT or risk_pct < MIN_RISK_PCT:
        return None
    sign = 1 if is_long else -1
    tp1 = entry + sign * TP1_R * risk
    tp2 = entry + sign * TP2_R * risk
    last_idx = len(k15["close"]) - 2
    for j in range(trigger_idx + 1, last_idx + 1):          # nothing resolved since the trigger
        if is_long and (k15["low"][j] <= sl or k15["high"][j] >= tp1):
            return None
        if not is_long and (k15["high"][j] >= sl or k15["low"][j] <= tp1):
            return None
    current = k15["close"][last_idx]
    if (current - entry) * sign > STALE_R * risk:             # already ran away -> chasing
        return None
    details = dict(details)
    details.update({"change_24h_pct": round(ticker["pct"], 2),
                    "volume_24h_usd": round(ticker["quote_volume"]),
                    "risk_pct": round(risk_pct, 2), "rr_tp1": TP1_R, "rr_tp2": TP2_R,
                    "tp1_action": "close 50%, move SL to entry"})
    return {
        "symbol": symbol, "scanner": scanner, "strategy_name": LABELS[scanner],
        "direction": "LONG" if is_long else "SHORT", "action": "BUY" if is_long else "SELL",
        "timeframe": timeframe, "price": entry, "stop_loss": sl,
        "take_profit_1": tp1, "take_profit_2": tp2,
        "risk_pct": round(risk_pct, 2), "confidence": confidence,
        "watch_only": MOVERS_WATCH_ONLY, "partial_tp1_pct": PARTIAL_TP1_PCT,
        "created_ts": float(trigger_close_ts), "details": details,
        "cooldown_key": f"{symbol}|{scanner}|{'LONG' if is_long else 'SHORT'}",
    }


# ----------------------------- STRATEGY A: PULLBACK -------------------

def evaluate_pullback(symbol, is_long, ticker, k15, k1h):
    """Gainers BUY / Losers SELL: pullback to 15m EMA20/VWAP + volume confirmation + 1h trend."""
    n, n1 = len(k15["close"]), len(k1h["close"])
    if n < 60 or n1 < 205:
        return None
    e50 = _ema(k1h["close"], 50)[n1 - 2]
    e200 = _ema(k1h["close"], 200)[n1 - 2]
    if e50 is None or e200 is None or (e50 > e200) != is_long:
        return None
    ema20 = _ema(k15["close"], 20)
    atr15 = _atr(k15["high"], k15["low"], k15["close"])
    vwap = _session_vwap(k15)
    last_idx = n - 2
    for t in range(last_idx, last_idx - TRIGGER_WINDOW, -1):
        if t < 25 or ema20[t] is None or atr15[t] is None:
            continue
        o, c = k15["open"][t], k15["close"][t]
        if (c > o) != is_long or c == o:
            continue
        avg_vol = _avg(k15["volume"][t - 20:t])
        if avg_vol <= 0 or k15["volume"][t] < VOLUME_MULT * avg_vol:
            continue
        window = range(t - PULLBACK_LOOKBACK, t + 1)
        if is_long:
            touched_ema = any(ema20[j] and k15["low"][j] <= ema20[j] * (1 + TOUCH_TOLERANCE) for j in window)
            touched_vwap = any(k15["low"][j] <= vwap[j] * (1 + TOUCH_TOLERANCE) for j in window)
            if not (touched_ema or touched_vwap) or not (c > ema20[t] and c > vwap[t]):
                continue
            extreme = min(k15["low"][j] for j in window)
            sl = min(extreme * (1 - STRUCT_SL_BUFFER), c - ATR_SL_MULT * atr15[t])
        else:
            touched_ema = any(ema20[j] and k15["high"][j] >= ema20[j] * (1 - TOUCH_TOLERANCE) for j in window)
            touched_vwap = any(k15["high"][j] >= vwap[j] * (1 - TOUCH_TOLERANCE) for j in window)
            if not (touched_ema or touched_vwap) or not (c < ema20[t] and c < vwap[t]):
                continue
            extreme = max(k15["high"][j] for j in window)
            sl = max(extreme * (1 + STRUCT_SL_BUFFER), c + ATR_SL_MULT * atr15[t])
        level = "EMA20+VWAP" if touched_ema and touched_vwap else "EMA20" if touched_ema else "VWAP"
        side = "Top Gainer" if is_long else "Top Loser"
        vol_x = k15["volume"][t] / avg_vol
        sig = _finish(
            symbol, "MOVERS_PULLBACK", is_long, c, sl, k15["open_time"][t] + 900_000, k15, t, ticker,
            "15m entry · 1h trend",
            f"{side} {ticker['pct']:+.1f}% · {'pullback' if is_long else 'rally'} to {level} · "
            f"volume {vol_x:.1f}x · 1h EMA50 {'>' if is_long else '<'} EMA200",
            {"touched": level, "volume_ratio": round(vol_x, 2), "trigger_volume_ratio": round(vol_x, 2),
             "pullback_extreme" if is_long else "rally_extreme": extreme,
             "ema20": round(ema20[t], 8), "vwap": round(vwap[t], 8)})
        if sig:
            return sig
    return None


# ----------------------------- STRATEGY B: ORDER BLOCK ----------------

def evaluate_order_block(symbol, is_long, ticker, k15, k1h):
    """Institutional order block: 1h displacement + BOS, first 15m retest with rejection."""
    n1, n = len(k1h["close"]), len(k15["close"])
    if n1 < 60 or n < 80:
        return None
    atr1 = _atr(k1h["high"], k1h["low"], k1h["close"])
    atr15 = _atr(k15["high"], k15["low"], k15["close"])
    last1, last15 = n1 - 2, n - 2
    sign = 1 if is_long else -1
    for d in range(last1, max(OB_STRUCTURE_LOOKBACK + 20, last1 - OB_MAX_AGE_1H), -1):
        if atr1[d] is None:
            continue
        body = (k1h["close"][d] - k1h["open"][d]) * sign
        prior = range(d - OB_STRUCTURE_LOOKBACK, d)
        if is_long:
            broke = k1h["close"][d] > max(k1h["high"][i] for i in prior)
        else:
            broke = k1h["close"][d] < min(k1h["low"][i] for i in prior)
        if body < OB_BODY_ATR * atr1[d] or not broke:
            continue
        avg_vol = _avg(k1h["volume"][d - 20:d])
        if avg_vol <= 0 or k1h["volume"][d] < OB_VOLUME_MULT * avg_vol:
            continue
        disp_vol_x = k1h["volume"][d] / avg_vol
        ob = next((i for i in range(d - 1, d - 4, -1)
                   if (k1h["close"][i] < k1h["open"][i]) == is_long
                   and k1h["close"][i] != k1h["open"][i]), None)
        if ob is None:
            continue
        zone_lo, zone_hi = k1h["low"][ob], k1h["high"][ob]
        mid = (zone_lo + zone_hi) / 2
        later = range(d + 1, last1 + 1)
        if is_long and any(k1h["close"][i] < zone_lo for i in later):
            continue
        if not is_long and any(k1h["close"][i] > zone_hi for i in later):
            continue
        fvg = (d + 1 <= last1) and (
            (k1h["low"][d + 1] > k1h["high"][d - 1]) if is_long else (k1h["high"][d + 1] < k1h["low"][d - 1]))
        disp_close_ts = k1h["open_time"][d] + 3_600_000
        start = next((j for j, ts in enumerate(k15["open_time"]) if ts >= disp_close_ts), None)
        if start is None:
            continue
        first_tap = next((j for j in range(start, last15 + 1)
                          if (k15["low"][j] <= zone_hi if is_long else k15["high"][j] >= zone_lo)), None)
        if first_tap is None or first_tap < last15 - TRIGGER_WINDOW + 1:
            continue
        for t in range(first_tap, min(first_tap + 3, last15 + 1)):
            if atr15[t] is None:
                continue
            o, c, hi, lo = k15["open"][t], k15["close"][t], k15["high"][t], k15["low"][t]
            rng = hi - lo
            if rng <= 0 or (c > o) != is_long:
                continue
            if is_long:
                tapped = lo <= zone_hi and lo >= zone_lo - OB_SL_ATR_BUFFER * atr15[t]
                wick = (min(o, c) - lo) / rng
                good = c > mid
                swept = lo < zone_lo
                sl = min(zone_lo, lo) - OB_SL_ATR_BUFFER * atr15[t]
            else:
                tapped = hi >= zone_lo and hi <= zone_hi + OB_SL_ATR_BUFFER * atr15[t]
                wick = (hi - max(o, c)) / rng
                good = c < mid
                swept = hi > zone_hi
                sl = max(zone_hi, hi) + OB_SL_ATR_BUFFER * atr15[t]
            if not (tapped and good and wick >= OB_WICK_MIN):
                continue
            quality = "High" if (fvg or swept) else "Medium"
            side = "Top Gainer" if is_long else "Top Loser"
            tags = [x for x, on in (("FVG", fvg), ("liquidity sweep", swept)) if on]
            sig = _finish(
                symbol, "MOVERS_ORDERBLOCK", is_long, c, sl, k15["open_time"][t] + 900_000, k15, t, ticker,
                "15m entry · 1h order block",
                f"{side} {ticker['pct']:+.1f}% · {'bullish' if is_long else 'bearish'} order block retest · "
                f"{quality}" + (f" ({', '.join(tags)})" if tags else ""),
                {"ob_zone_low": zone_lo, "ob_zone_high": zone_hi, "fvg": bool(fvg),
                 "liquidity_sweep": bool(swept), "rejection_wick_pct": round(wick * 100, 1),
                 "volume_ratio": round(disp_vol_x, 2), "trigger_volume_ratio": round(disp_vol_x, 2),
                 "quality": quality})
            if sig:
                return sig
    return None


# ----------------------------- SCAN ENTRY POINT -----------------------

def scan_movers(universe, base_url, get_klines, sleep_seconds=0.1, log=print):
    """Return a list of candidate signals (cooldown/risk caps are applied by the caller)."""
    tickers = fetch_movers(base_url, universe)
    gainers, losers = pick_candidates(tickers)
    log(f"[movers] {len(gainers)} gainer(s) and {len(losers)} loser(s) pass the 24h move/volume filter.")
    signals = []
    for is_long, group in ((True, gainers), (False, losers)):
        for ticker in group:
            symbol = ticker["symbol"]
            try:
                k15 = get_klines(symbol, "15m", 300)
                time.sleep(sleep_seconds)
                k1h = get_klines(symbol, "1h", 300)
                time.sleep(sleep_seconds)
            except requests.RequestException as exc:
                log(f"[movers] {symbol} data error: {exc}")
                continue
            if not k15 or not k1h:
                continue
            for evaluate in (evaluate_pullback, evaluate_order_block):
                sig = evaluate(symbol, is_long, ticker, k15, k1h)
                if sig:
                    signals.append(sig)
    return signals
