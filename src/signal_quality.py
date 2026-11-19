"""Signal quality score (0-100) + Market Session & Orderflow intelligence (v2.3).

Four base evidence components, each first normalised to 0..1 and THEN weighted:

    trend       30  H4 + D1 trend agree with the trade direction (multi-timeframe alignment)
    volume      25  volume expansion on the signal candle (or net flow imbalance for on-chain flow)
    confluence  25  number of INDEPENDENT evidence families (not raw rule names - see strategy_policy)
    regime      20  BTC / market regime backdrop for this direction

Plus optional institutional modifiers (when market context is supplied by the screener):
    session_bonus      London (07-13 UTC), London-NY Overlap (13-17 UTC), US (17-21 UTC) vs Asia Dead-Zone (22-05 UTC)
    orderflow_bonus    Relative Strength vs BTC (4h), Spot Taker Buy/Sell CVD share, and Derivatives OI/Funding confirmation
"""

from datetime import datetime, timezone

WEIGHTS = {"trend": 30.0, "volume": 25.0, "confluence": 25.0, "regime": 20.0}
TIER_A_MIN_SCORE = 70.0
TIER_B_MIN_SCORE = 50.0
TIER_RISK_PCT = {"A": 1.0, "B": 0.5, "C": 0.0}   # share of equity risked per trade, by tier
MIN_SCORE_LIVE = TIER_B_MIN_SCORE            # kept for older callers: live needs at least tier B
MIN_VOLUME_RATIO = 1.5          # below this a breakout/cross has no volume confirmation
VOLUME_FULL_CREDIT_RATIO = 2.5  # ratio at which the volume component is maxed out
FLOW_FULL_CREDIT_IMBALANCE_PCT = 60.0
REGIME_FULL_SPAN = 40.0         # regime score of +/-40 is a full tailwind / headwind
EMA_FAST, EMA_SLOW = 20, 50

TREND_COMPONENT = {"ALIGNED": 1.0, "PARTIAL": 0.5, "MIXED": 0.25, "NEUTRAL": 0.25, "COUNTER-TREND": 0.0}


def session_info(now=None):
    """Return active institutional trading session metadata for the given UTC datetime."""
    now = now or datetime.now(timezone.utc)
    if isinstance(now, (int, float)):
        now = datetime.fromtimestamp(float(now), tz=timezone.utc)
    hour = now.hour
    if 13 <= hour < 17:
        return {
            "name": "LONDON_NY_OVERLAP",
            "code": "LONDON_NY_OVERLAP",
            "label": "🔥 London–NY Overlap (Peak Liquidity)",
            "quality": "PEAK",
            "bonus": 4.0,
            "score_bonus": 4.0,
            "is_dead_zone": False,
        }
    if 7 <= hour < 13:
        return {
            "name": "LONDON_SESSION",
            "code": "LONDON_SESSION",
            "label": "🇬🇧 London Session (High Liquidity)",
            "quality": "HIGH",
            "bonus": 2.5,
            "score_bonus": 2.5,
            "is_dead_zone": False,
        }
    if 17 <= hour < 21:
        return {
            "name": "US_SESSION",
            "code": "US_SESSION",
            "label": "🇺🇸 US Session (Active Volume)",
            "quality": "HIGH",
            "bonus": 2.5,
            "score_bonus": 2.5,
            "is_dead_zone": False,
        }
    if 5 <= hour < 7 or 21 <= hour < 22:
        return {
            "name": "SESSION_TRANSITION",
            "code": "TRANSITION",
            "label": "🌐 Session Transition",
            "quality": "MODERATE",
            "bonus": 0.0,
            "score_bonus": 0.0,
            "is_dead_zone": False,
        }
    return {
        "name": "ASIA_DEAD_ZONE",
        "code": "ASIA_DEAD_ZONE",
        "label": "🌙 Late-NY / Asia Low-Liquidity Window",
        "quality": "LOW",
        "bonus": -4.0,
        "score_bonus": -4.0,
        "is_dead_zone": True,
    }


def orderflow_adjustment(direction, taker_buy_share=None, oi_change_pct=None, alt_return_pct=None, btc_return_pct=None, rs_vs_btc_pct=None):
    """Compute a bounded score adjustment (-10..+10) from Relative Strength vs BTC, Spot Taker CVD, and OI."""
    is_long = str(direction).upper() in {"LONG", "BUY"}
    adj = 0.0
    if rs_vs_btc_pct is None and alt_return_pct is not None and btc_return_pct is not None:
        try:
            rs_vs_btc_pct = float(alt_return_pct) - float(btc_return_pct)
        except (TypeError, ValueError):
            rs_vs_btc_pct = None
    if rs_vs_btc_pct is not None:
        fav_rs = rs_vs_btc_pct if is_long else -rs_vs_btc_pct
        if fav_rs >= 0.5:
            adj += 3.5
        elif fav_rs <= -1.2:
            adj -= 5.0
    if taker_buy_share is not None:
        if is_long and taker_buy_share >= 0.53:
            adj += 3.0
        elif not is_long and taker_buy_share <= 0.47:
            adj += 3.0
        elif (is_long and taker_buy_share < 0.46) or (not is_long and taker_buy_share > 0.54):
            adj -= 4.5
    if oi_change_pct is not None and oi_change_pct >= 3.0:
        adj += 2.5
    return max(-10.0, min(10.0, round(adj, 1)))


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    out = sum(values[:period]) / period
    for value in values[period:]:
        out = value * k + out * (1 - k)
    return out


def _vote(klines):
    """+1 bullish, -1 bearish, 0 unclear, None unknown - on CLOSED candles only."""
    closes = (klines or {}).get("close") or []
    closes = closes[:-1]                      # drop the forming candle
    if len(closes) < EMA_SLOW + 5:
        return None
    fast, slow, close = _ema(closes, EMA_FAST), _ema(closes, EMA_SLOW), closes[-1]
    if fast > slow and close > slow:
        return 1
    if fast < slow and close < slow:
        return -1
    return 0


def trend_alignment(direction, klines_4h, klines_1d):
    """Return (label, votes). Label is None when the higher-timeframe data is unavailable."""
    votes = {"4h": _vote(klines_4h), "1d": _vote(klines_1d)}
    if None in votes.values():
        return None, votes
    side = 1 if str(direction).upper() in {"LONG", "BUY"} else -1
    agree = sum(1 for v in votes.values() if v == side)
    oppose = sum(1 for v in votes.values() if v == -side)
    if agree == 2:
        label = "ALIGNED"
    elif oppose and not agree:
        label = "COUNTER-TREND"
    elif agree and oppose:
        label = "MIXED"
    elif agree:
        label = "PARTIAL"
    else:
        label = "NEUTRAL"
    return label, votes


def volume_ratio(klines, lookback=20):
    """Last CLOSED candle volume / mean of the prior `lookback` closed candles."""
    volumes = (klines or {}).get("volume") or []
    volumes = volumes[:-1]
    if len(volumes) < lookback + 1:
        return None
    base = sum(volumes[-lookback - 1:-1]) / lookback
    return volumes[-1] / base if base > 0 else None


def taker_buy_ratio(klines, lookback=3):
    """Recent closed-candle spot taker-buy share (0.0..1.0), or None if unavailable."""
    qv = (klines or {}).get("quote_volume") or []
    tb = (klines or {}).get("taker_buy_quote_volume") or (klines or {}).get("taker_buy_quote") or []
    qv, tb = qv[:-1], tb[:-1]
    if len(qv) < lookback or len(tb) < lookback:
        return None
    total_q = sum(qv[-lookback:])
    if total_q <= 0:
        return None
    return round(sum(tb[-lookback:]) / total_q, 4)


def return_pct(klines, candles=4):
    """Percentage return over the last `candles` closed bars."""
    closes = (klines or {}).get("close") or []
    closes = closes[:-1]
    if len(closes) < candles + 1 or closes[-candles - 1] <= 0:
        return None
    return round((closes[-1] / closes[-candles - 1] - 1) * 100.0, 2)


def _clamp01(x):
    return max(0.0, min(1.0, x))


def volume_component(ratio):
    if ratio is None:
        return 0.0
    return _clamp01((ratio - 1.0) / (VOLUME_FULL_CREDIT_RATIO - 1.0))


def flow_component(imbalance_pct):
    if imbalance_pct is None:
        return 0.0
    return _clamp01(abs(float(imbalance_pct)) / FLOW_FULL_CREDIT_IMBALANCE_PCT)


def confluence_component(independent_families):
    return {0: 0.0, 1: 0.34, 2: 0.67}.get(int(independent_families), 1.0)


def regime_component(regime, is_long):
    if not regime or regime.get("score") is None or regime.get("label") == "UNKNOWN":
        return 0.5                                   # no information = neutral, not a penalty
    directional = regime["score"] if is_long else -regime["score"]
    return _clamp01((directional + REGIME_FULL_SPAN) / (2 * REGIME_FULL_SPAN))


def regime_alignment(regime, is_long, threshold=15):
    if not regime or regime.get("score") is None or regime.get("label") == "UNKNOWN":
        return "UNKNOWN"
    directional = regime["score"] if is_long else -regime["score"]
    if directional >= threshold:
        return "ALIGNED"
    return "COUNTER-TREND" if directional <= -threshold else "NEUTRAL"


def score_signal(direction, trend_label, vol_component, independent_families, regime, extra_bonus=0.0):
    """Combine the normalised components. Returns a JSON-safe dict (also stored in the journal)."""
    is_long = str(direction).upper() in {"LONG", "BUY"}
    parts = {
        "trend": TREND_COMPONENT.get(trend_label, 0.0),
        "volume": vol_component,
        "confluence": confluence_component(independent_families),
        "regime": regime_component(regime, is_long),
    }
    total = sum(parts[name] * WEIGHTS[name] for name in WEIGHTS) + float(extra_bonus or 0.0)
    total = max(0.0, min(100.0, total))
    alignment = regime_alignment(regime, is_long)
    score = round(total, 1)
    return {
        "score": score,
        "tier": tier(score, trend_label, independent_families, alignment),
        "regime_alignment": alignment,
        "trend_alignment": trend_label or "UNKNOWN",
        "independent_families": int(independent_families),
        "components": {name: round(value, 2) for name, value in parts.items()},
    }


def tier(score, trend_label, independent_families, regime_alignment=None):
    """Grade a signal A / B / C (see module docstring)."""
    if trend_label in (None, "COUNTER-TREND") or regime_alignment == "COUNTER-TREND":
        return "C"
    if score >= TIER_A_MIN_SCORE and independent_families >= 2 and trend_label in ("ALIGNED", "PARTIAL"):
        return "A"
    if score >= TIER_B_MIN_SCORE:
        return "B"
    return "C"


def gate(result, volume_ratio_value=None, live=True):
    """None when the signal passes, else a short reason. Watch-only signals only get annotated."""
    if not live:
        return None
    if result["trend_alignment"] == "UNKNOWN":
        return "trend data unavailable (live signals fail closed)"
    if result["trend_alignment"] == "COUNTER-TREND":
        return "H4 and D1 trend both oppose the trade"
    if volume_ratio_value is not None and volume_ratio_value < MIN_VOLUME_RATIO:
        return f"volume ratio {volume_ratio_value:.2f} is below {MIN_VOLUME_RATIO:g}"
    if result.get("regime_alignment") == "COUNTER-TREND":
        return "the BTC / market regime is against this direction"
    if result["score"] < TIER_B_MIN_SCORE:
        return f"quality score {result['score']:.0f} is below {TIER_B_MIN_SCORE:g} (tier C)"
    return None
