"""
DeepPocket Tracker — Wall Street & Smart-Money Liquidity Engine (v5.0 Dynamic Auto-Selector Edition)
----------------------------------------------------------------------------------------------------
Provides three institutional price-action + orderflow engines evaluated on every scan and
verified on previous historical candles by `src/backtester.py`:

  1. `Smart Money Liquidity Sweep` (ID 9001):
     Stop-hunts above prior 20-candle Swing Highs (Bearish Sweep -> SHORT) or below
     Swing Lows (Bullish Sweep -> LONG) with rejection wicks and confirming CVD.
  2. `Smart Money CVD Breakdown` (ID 9002):
     Impulsive range breakdowns/breakouts through 12-candle support/resistance floors
     with volume expansion (`>= 1.35x`) and aggressive seller/buyer Taker CVD.
  3. `Smart Money EMA20 Pullback` (ID 9003):
     Captures sustained stair-step trends (such as BTC dropping $83k -> $73k) where
     price in an established `EMA20 < EMA50` downtrend bounces into the `EMA20`
     dynamic resistance zone and prints a bearish rejection candle (`close < open`,
     `close < ema20`, seller CVD).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

MIN_STOP_PCT = 0.85
MAX_STOP_PCT = 2.25

# Fast Sniper TP1 / TP2 bounds (% of entry price) so +1.0%..+2.4% moves lock in TP1 + Break-Even
SNIPER_TP1_MIN_PCT = 0.90
SNIPER_TP1_MAX_PCT = 1.25
SNIPER_TP2_MIN_PCT = 1.85
SNIPER_TP2_MAX_PCT = 2.45
TP1_RR = 0.60   # TP1 at 0.60R (clamped to 0.90%..1.25%) -> high hit rate + instant Break-Even lock
TP2_RR = 1.40   # TP2 at 1.40R (clamped to 1.85%..2.45%) + 55% step-lock ratchet

SWEEP_LOOKBACK = 20
BREAKDOWN_LOOKBACK = 12
MIN_SWEEP_WICK_RATIO = 0.32
MIN_BREAKDOWN_VOL_RATIO = 1.35
MIN_SWEEP_VOL_RATIO = 1.15
BEARISH_CVD_MAX_BUY_SHARE = 0.485
BULLISH_CVD_MIN_BUY_SHARE = 0.515

# L2 Orderbook Wall & Imbalance thresholds
SHORT_MAX_BID_ASK_RATIO = 1.35
LONG_MIN_BID_ASK_RATIO = 0.72
WALL_CLUSTER_MULTIPLIER = 4.5

# Macro Whipsaw Blackout window (minutes after breaking Fed/CPI/NFP headline)
MACRO_BLACKOUT_MINUTES = 35.0
MACRO_SHOCK_KEYWORDS = (
    "fomc", "federal reserve", "fed rate", "powell", "cpi", "inflation",
    "nonfarm", "nfp", "payroll", "rate cut", "rate hike", "emergency",
)


def compute_sniper_targets(entry: float, raw_sl: float, is_long: bool) -> tuple[float, float, float]:
    """Compute realistic high-hit-rate Sniper TP1 (`0.90%..1.25%`) and TP2 (`1.85%..2.45%`)
    so trades that move +1.0%..+2.4% in profit immediately bank TP1 and move SL to Break-Even."""
    risk_pct = abs(entry - raw_sl) / entry * 100.0
    tp1_pct = min(max(risk_pct * TP1_RR, SNIPER_TP1_MIN_PCT), SNIPER_TP1_MAX_PCT)
    tp2_pct = min(max(risk_pct * TP2_RR, SNIPER_TP2_MIN_PCT), SNIPER_TP2_MAX_PCT)
    if tp2_pct <= tp1_pct * 1.3:
        tp2_pct = round(tp1_pct * 1.65, 4)
    sign = 1.0 if is_long else -1.0
    tp1 = round(entry * (1.0 + sign * tp1_pct / 100.0), 8)
    tp2 = round(entry * (1.0 + sign * tp2_pct / 100.0), 8)
    return tp1, tp2, round(risk_pct, 2)


def _ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + float(out[i - 1]) * (1.0 - k)
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    trs: list[float] = [0.0]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out[period] = sum(trs[1 : period + 1]) / period
    for i in range(period + 1, len(closes)):
        out[i] = (float(out[i - 1]) * (period - 1) + trs[i]) / period
    return out


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(len(closes) - period, len(closes)):
        diff = float(closes[i]) - float(closes[i - 1])
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    if losses <= 1e-12:
        return 100.0
    rs = (gains / period) / (losses / period)
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def candle_taker_buy_share(klines: dict[str, Any], idx: int, window: int = 2) -> float:
    """Return aggressive buyer share (0.0..1.0) over `window` closed candles ending at `idx`."""
    qv = klines.get("quote_volume") or []
    tb = klines.get("taker_buy_quote_volume") or []
    start = max(0, idx - window + 1)
    if len(qv) > idx and len(tb) > idx:
        total_q = sum(float(qv[i]) for i in range(start, idx + 1))
        total_b = sum(float(tb[i]) for i in range(start, idx + 1))
        if total_q > 0 and total_b != total_q:
            return round(max(0.0, min(1.0, total_b / total_q)), 4)
    opens = klines.get("open") or []
    highs = klines.get("high") or []
    lows = klines.get("low") or []
    closes = klines.get("close") or []
    if len(closes) <= idx:
        return 0.5
    shares: list[float] = []
    for i in range(start, idx + 1):
        rng = float(highs[i]) - float(lows[i])
        if rng <= 0:
            shares.append(0.5)
        else:
            pos = (float(closes[i]) - float(lows[i])) / rng
            body_bias = 0.08 if float(closes[i]) > float(opens[i]) else (-0.08 if float(closes[i]) < float(opens[i]) else 0.0)
            shares.append(max(0.05, min(0.95, pos + body_bias)))
    return round(sum(shares) / len(shares), 4)


def cvd_zscore(klines: dict[str, Any], idx: int, lookback: int = 20) -> float:
    """Normalized Cumulative Volume Delta impulse on candle `idx` vs prior `lookback` bars."""
    volumes = klines.get("volume") or []
    if len(volumes) <= idx or idx < lookback:
        return 0.0
    deltas: list[float] = []
    for i in range(idx - lookback, idx + 1):
        buy_share = candle_taker_buy_share(klines, i, window=1)
        net_delta = (2.0 * buy_share - 1.0) * float(volumes[i])
        deltas.append(net_delta)
    prior = deltas[:-1]
    mean = sum(prior) / len(prior) if prior else 0.0
    variance = sum((x - mean) ** 2 for x in prior) / len(prior) if prior else 0.0
    std = variance ** 0.5
    if std <= 1e-9:
        return 0.0
    return round((deltas[-1] - mean) / std, 2)


# =========================================================================
# PROP-DESK FACTOR 1: 4-QUADRANT OI + PRICE + CVD MATRIX & EXHAUSTION GUARD
# =========================================================================
def classify_oi_price_cvd_quadrant(
    price_change_pct: float,
    oi_change_pct: float | None,
    taker_buy_share: float | None,
    funding_rate_pct: float | None = None,
    rsi_val: float | None = None,
) -> dict[str, Any]:
    """Classify derivatives + spot orderflow into the 4 Institutional Quadrants."""
    t_buy = 0.50 if taker_buy_share is None else float(taker_buy_share)
    if oi_change_pct is None:
        return {
            "quadrant": "SPOT_CVD_ONLY",
            "blocks_short": False,
            "blocks_long": False,
            "score_bonus": 0,
            "reason": None,
        }

    oi_chg = float(oi_change_pct)
    p_chg = float(price_change_pct)

    if p_chg <= -0.25 and oi_chg >= 1.2 and t_buy <= 0.49:
        return {
            "quadrant": "NEW_SHORTS_BUILDING",
            "blocks_short": False,
            "blocks_long": True,
            "score_bonus": 6,
            "reason": f"Fresh Short Build-Up (Price {p_chg:+.1f}%, OI {oi_chg:+.1f}%)",
        }

    if p_chg >= 0.25 and oi_chg >= 1.2 and t_buy >= 0.51:
        return {
            "quadrant": "NEW_LONGS_BUILDING",
            "blocks_short": True,
            "blocks_long": False,
            "score_bonus": 6,
            "reason": f"Fresh Long Build-Up (Price {p_chg:+.1f}%, OI {oi_chg:+.1f}%)",
        }

    if p_chg < 0 and oi_chg <= -3.5:
        is_capitulated = (
            p_chg <= -4.5
            or (rsi_val is not None and rsi_val <= 25.0)
            or (funding_rate_pct is not None and funding_rate_pct <= -0.035)
        )
        return {
            "quadrant": "BOTTOM_CAPITULATION_TRAP" if is_capitulated else "LONG_LIQUIDATION_FLUSH",
            "blocks_short": bool(is_capitulated),
            "blocks_long": False,
            "score_bonus": -8 if is_capitulated else 0,
            "reason": (
                f"bottom capitulation exhaustion (Price {p_chg:+.1f}%, OI {oi_chg:+.1f}% liquidation flush)"
                if is_capitulated else None
            ),
        }

    if p_chg > 0 and oi_chg <= -3.5:
        is_exhausted = (
            p_chg >= 4.5
            or (rsi_val is not None and rsi_val >= 75.0)
            or (funding_rate_pct is not None and funding_rate_pct >= 0.05)
        )
        return {
            "quadrant": "TOP_EXHAUSTION_TRAP" if is_exhausted else "SHORT_COVERING_BOUNCE",
            "blocks_short": False,
            "blocks_long": bool(is_exhausted),
            "score_bonus": -8 if is_exhausted else 0,
            "reason": (
                f"top short-squeeze exhaustion (Price {p_chg:+.1f}%, OI {oi_chg:+.1f}% covering bounce)"
                if is_exhausted else None
            ),
        }

    return {
        "quadrant": "NEUTRAL_OI",
        "blocks_short": False,
        "blocks_long": False,
        "score_bonus": 0,
        "reason": None,
    }


# =========================================================================
# PROP-DESK FACTOR 2: LIVE L2 ORDERBOOK DEPTH & WHALE WALL FILTER
# =========================================================================
def analyze_l2_orderbook_wall(
    orderbook: dict[str, Any] | None,
    entry_price: float,
    direction: str,
    tp1_price: float | None = None,
    band_pct: float = 1.8,
) -> dict[str, Any]:
    """Check Binance L2 orderbook (`bids` and `asks`) within `band_pct` of `entry_price`."""
    if not orderbook or entry_price <= 0:
        return {
            "ok": True,
            "bid_ask_ratio": None,
            "bid_usd": 0.0,
            "ask_usd": 0.0,
            "wall_blocked": False,
            "score_bonus": 0,
            "reason": None,
        }

    bids_raw = orderbook.get("bids") or []
    asks_raw = orderbook.get("asks") or []
    if not bids_raw or not asks_raw:
        return {
            "ok": True,
            "bid_ask_ratio": None,
            "bid_usd": 0.0,
            "ask_usd": 0.0,
            "wall_blocked": False,
            "score_bonus": 0,
            "reason": None,
        }

    lower_bound = entry_price * (1.0 - band_pct / 100.0)
    upper_bound = entry_price * (1.0 + band_pct / 100.0)

    bid_levels: list[tuple[float, float]] = []
    ask_levels: list[tuple[float, float]] = []

    for row in bids_raw:
        try:
            p, q = float(row[0]), float(row[1])
            if lower_bound <= p <= entry_price * 1.001 and q > 0:
                bid_levels.append((p, p * q))
        except (IndexError, TypeError, ValueError):
            continue

    for row in asks_raw:
        try:
            p, q = float(row[0]), float(row[1])
            if entry_price * 0.999 <= p <= upper_bound and q > 0:
                ask_levels.append((p, p * q))
        except (IndexError, TypeError, ValueError):
            continue

    bid_usd = sum(u for _, u in bid_levels)
    ask_usd = sum(u for _, u in ask_levels)
    if bid_usd <= 0 or ask_usd <= 0:
        return {
            "ok": True,
            "bid_ask_ratio": None,
            "bid_usd": round(bid_usd, 2),
            "ask_usd": round(ask_usd, 2),
            "wall_blocked": False,
            "score_bonus": 0,
            "reason": None,
        }

    ratio = round(bid_usd / ask_usd, 2)
    is_long = str(direction).upper() in {"LONG", "BUY"}

    wall_blocked = False
    wall_price = None
    if not is_long and bid_levels:
        avg_bid = bid_usd / len(bid_levels)
        tp_floor = float(tp1_price) if tp1_price else lower_bound
        for p, usd_val in bid_levels:
            if tp_floor <= p <= entry_price and usd_val >= avg_bid * WALL_CLUSTER_MULTIPLIER and usd_val >= 0.45 * ask_usd:
                wall_blocked = True
                wall_price = p
                break
    elif is_long and ask_levels:
        avg_ask = ask_usd / len(ask_levels)
        tp_ceil = float(tp1_price) if tp1_price else upper_bound
        for p, usd_val in ask_levels:
            if entry_price <= p <= tp_ceil and usd_val >= avg_ask * WALL_CLUSTER_MULTIPLIER and usd_val >= 0.45 * bid_usd:
                wall_blocked = True
                wall_price = p
                break

    if not is_long:
        if ratio >= SHORT_MAX_BID_ASK_RATIO:
            return {
                "ok": False,
                "bid_ask_ratio": ratio,
                "bid_usd": round(bid_usd, 2),
                "ask_usd": round(ask_usd, 2),
                "wall_blocked": True,
                "score_bonus": -10,
                "reason": f"L2 orderbook bid wall absorbing sell pressure (Bid/Ask={ratio:.2f}x >= {SHORT_MAX_BID_ASK_RATIO}x)",
            }
        if wall_blocked:
            return {
                "ok": False,
                "bid_ask_ratio": ratio,
                "bid_usd": round(bid_usd, 2),
                "ask_usd": round(ask_usd, 2),
                "wall_blocked": True,
                "score_bonus": -8,
                "reason": f"L2 whale buy wall at ${wall_price:.6g} blocks path to TP1 (Bid/Ask={ratio:.2f}x)",
            }
        bonus = 5 if ratio <= 0.80 else 0
        return {
            "ok": True,
            "bid_ask_ratio": ratio,
            "bid_usd": round(bid_usd, 2),
            "ask_usd": round(ask_usd, 2),
            "wall_blocked": False,
            "score_bonus": bonus,
            "reason": None,
        }
    else:
        if ratio <= LONG_MIN_BID_ASK_RATIO:
            return {
                "ok": False,
                "bid_ask_ratio": ratio,
                "bid_usd": round(bid_usd, 2),
                "ask_usd": round(ask_usd, 2),
                "wall_blocked": True,
                "score_bonus": -10,
                "reason": f"L2 orderbook ask wall capping upside (Bid/Ask={ratio:.2f}x <= {LONG_MIN_BID_ASK_RATIO}x)",
            }
        if wall_blocked:
            return {
                "ok": False,
                "bid_ask_ratio": ratio,
                "bid_usd": round(bid_usd, 2),
                "ask_usd": round(ask_usd, 2),
                "wall_blocked": True,
                "score_bonus": -8,
                "reason": f"L2 whale sell wall at ${wall_price:.6g} blocks path to TP1 (Bid/Ask={ratio:.2f}x)",
            }
        bonus = 5 if ratio >= 1.25 else 0
        return {
            "ok": True,
            "bid_ask_ratio": ratio,
            "bid_usd": round(bid_usd, 2),
            "ask_usd": round(ask_usd, 2),
            "wall_blocked": False,
            "score_bonus": bonus,
            "reason": None,
        }


# =========================================================================
# PROP-DESK FACTOR 3: RELATIVE STRENGTH (RS) VS BTC BETA RANKING
# =========================================================================
def relative_strength_vs_btc(
    coin_klines: dict[str, Any] | None,
    btc_klines: dict[str, Any] | None,
    lookback: int = 4,
) -> dict[str, Any]:
    """Compare altcoin's `lookback`-candle return & CVD against `BTCUSDT`.
    Only blocks a SHORT when the altcoin is genuinely decoupling upward (`RS >= +2.0%` AND
    `coin_ret > 0%` with buyer CVD `>= 0.52`), so BTC-led market dumps never falsely block
    altcoin shorts."""
    if not coin_klines or not btc_klines:
        return {
            "rs_vs_btc_pct": None,
            "status": "UNKNOWN",
            "blocks_short": False,
            "blocks_long": False,
            "score_bonus_short": 0,
            "score_bonus_long": 0,
            "reason": None,
        }
    c_closes = coin_klines.get("close") or []
    b_closes = btc_klines.get("close") or []
    if len(c_closes) < lookback + 3 or len(b_closes) < lookback + 3:
        return {
            "rs_vs_btc_pct": None,
            "status": "UNKNOWN",
            "blocks_short": False,
            "blocks_long": False,
            "score_bonus_short": 0,
            "score_bonus_long": 0,
            "reason": None,
        }

    c_idx = len(c_closes) - 2
    b_idx = len(b_closes) - 2
    c_prev = float(c_closes[c_idx - lookback])
    b_prev = float(b_closes[b_idx - lookback])
    if c_prev <= 0 or b_prev <= 0:
        return {
            "rs_vs_btc_pct": None,
            "status": "UNKNOWN",
            "blocks_short": False,
            "blocks_long": False,
            "score_bonus_short": 0,
            "score_bonus_long": 0,
            "reason": None,
        }

    coin_ret = (float(c_closes[c_idx]) - c_prev) / c_prev * 100.0
    btc_ret = (float(b_closes[b_idx]) - b_prev) / b_prev * 100.0
    rs_pct = round(coin_ret - btc_ret, 2)
    coin_buy_share = candle_taker_buy_share(coin_klines, c_idx, window=2)

    if rs_pct <= -0.8:
        status = "WEAK_LAGGARD"
    elif rs_pct >= 0.8:
        status = "STRONG_LEADER"
    else:
        status = "INLINE"

    blocks_short = (rs_pct >= 2.0 and coin_ret > 0.0 and coin_buy_share >= 0.52)
    blocks_long = (rs_pct <= -2.0 and coin_ret < 0.0 and coin_buy_share <= 0.48)

    reason = None
    if blocks_short:
        reason = f"relative strength decoupling vs BTC (RS={rs_pct:+.2f}%, buyer CVD={coin_buy_share * 100:.0f}%)"
    elif blocks_long:
        reason = f"relative weakness laggard vs BTC (RS={rs_pct:+.2f}%, buyer CVD={coin_buy_share * 100:.0f}%)"

    return {
        "rs_vs_btc_pct": rs_pct,
        "coin_return_pct": round(coin_ret, 2),
        "btc_return_pct": round(btc_ret, 2),
        "status": status,
        "blocks_short": blocks_short,
        "blocks_long": blocks_long,
        "score_bonus_short": 5 if status == "WEAK_LAGGARD" else (-6 if blocks_short else 0),
        "score_bonus_long": 5 if status == "STRONG_LEADER" else (-6 if blocks_long else 0),
        "reason": reason,
    }


# =========================================================================
# PROP-DESK FACTOR 4: FAIR VALUE GAP (FVG) & PULLBACK EQUILIBRIUM DETECTION
# =========================================================================
def detect_fair_value_gap(
    klines: dict[str, Any],
    idx: int,
    direction: str,
    atr_val: float,
) -> dict[str, Any]:
    """Detect 3-candle institutional Fair Value Gap (FVG) and compute optimal pullback entry."""
    highs = klines.get("high") or []
    lows = klines.get("low") or []
    opens = klines.get("open") or []
    closes = klines.get("close") or []
    if idx < 2 or len(closes) <= idx:
        return {"fvg_present": False, "fvg_midpoint": None, "overextended": False}

    is_long = str(direction).upper() in {"LONG", "BUY"}
    open_p = float(opens[idx])
    close_p = float(closes[idx])
    body = abs(close_p - open_p)
    overextended = atr_val > 0 and (body >= 1.85 * atr_val)

    if not is_long:
        c2_low = float(lows[idx - 2])
        c0_high = float(highs[idx])
        fvg_present = c0_high < c2_low
        fvg_top = c2_low if fvg_present else (open_p + close_p) / 2.0
        fvg_bottom = c0_high if fvg_present else close_p
        midpoint = round((fvg_top + fvg_bottom) / 2.0, 8)
    else:
        c2_high = float(highs[idx - 2])
        c0_low = float(lows[idx])
        fvg_present = c0_low > c2_high
        fvg_bottom = c2_high if fvg_present else (open_p + close_p) / 2.0
        fvg_top = c0_low if fvg_present else close_p
        midpoint = round((fvg_top + fvg_bottom) / 2.0, 8)

    return {
        "fvg_present": fvg_present,
        "fvg_top": round(fvg_top, 8),
        "fvg_bottom": round(fvg_bottom, 8),
        "fvg_midpoint": midpoint,
        "overextended": overextended,
    }


# =========================================================================
# PROP-DESK FACTOR 5: US MACRO WHIPSAW BLACKOUT GUARD (FOMC / CPI / NFP)
# =========================================================================
def check_macro_whipsaw_blackout(
    recent_news_items: list[dict[str, Any]] | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Hold new entries for `MACRO_BLACKOUT_MINUTES` (35m) when a breaking US Macro / Fed / CPI
    headline just hit the tape, avoiding 2-sided stop-hunt whipsaw wicks."""
    now = now_utc or datetime.now(timezone.utc)
    now_ts = now.timestamp()

    for item in (recent_news_items or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").lower()
        cat = str(item.get("category") or "").lower()
        is_macro = ("macro" in cat or "fed" in cat or "cpi" in cat) or any(
            kw in title for kw in MACRO_SHOCK_KEYWORDS
        )
        if not is_macro:
            continue
        ts = item.get("published_ts") or item.get("seen_ts") or item.get("timestamp")
        if ts is not None:
            try:
                age_min = (now_ts - float(ts)) / 60.0
                if 0.0 <= age_min <= MACRO_BLACKOUT_MINUTES:
                    return {
                        "blackout": True,
                        "age_minutes": round(age_min, 1),
                        "headline": item.get("title"),
                        "reason": (
                            f"US Macro Whipsaw Blackout active ({age_min:.0f}m since breaking "
                            f"Fed/CPI headline: '{str(item.get('title', ''))[:65]}')"
                        ),
                    }
            except (TypeError, ValueError):
                continue

    return {"blackout": False, "age_minutes": None, "headline": None, "reason": None}


def evaluate_at_index(
    symbol: str,
    klines: dict[str, Any],
    timeframe: str,
    idx: int,
    btc_bearish_bias: bool | None = None,
    funding_rate_pct: float | None = None,
    oi_change_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Evaluate all 3 Smart-Money setups (`9001` Sweep, `9002` Breakdown, `9003` EMA20 Pullback) at closed candle `idx`."""
    closes = klines.get("close") or []
    highs = klines.get("high") or []
    lows = klines.get("low") or []
    opens = klines.get("open") or []
    volumes = klines.get("volume") or []
    if len(closes) <= idx or idx < 52:
        return []

    ema20 = _ema(closes[: idx + 1], 20)[-1]
    ema50 = _ema(closes[: idx + 1], 50)[-1]
    atr_list = _atr(highs[: idx + 1], lows[: idx + 1], closes[: idx + 1], 14)
    atr_val = atr_list[-1]
    rsi_val = _rsi(closes[: idx + 1], 14)
    if ema20 is None or ema50 is None or atr_val is None or atr_val <= 0:
        return []

    price = float(closes[idx])
    open_p = float(opens[idx])
    high_p = float(highs[idx])
    low_p = float(lows[idx])
    vol_now = float(volumes[idx])
    if price <= 0:
        return []

    prior_vols = [float(v) for v in volumes[idx - 20 : idx]]
    avg_vol = sum(prior_vols) / len(prior_vols) if prior_vols else 0.0
    vol_ratio = round(vol_now / avg_vol, 2) if avg_vol > 0 else 1.0

    buy_share_1 = candle_taker_buy_share(klines, idx, window=1)
    buy_share_2 = candle_taker_buy_share(klines, idx, window=2)
    cvd_z = cvd_zscore(klines, idx, lookback=20)
    rng = max(high_p - low_p, 1e-12)
    body = abs(price - open_p)

    ref_close = float(closes[max(0, idx - 4)])
    price_chg_4b = ((price - ref_close) / ref_close * 100.0) if ref_close > 0 else 0.0
    oi_quad = classify_oi_price_cvd_quadrant(
        price_chg_4b, oi_change_pct, min(buy_share_1, buy_share_2),
        funding_rate_pct=funding_rate_pct, rsi_val=rsi_val,
    )

    results: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. KEY LEVEL LIQUIDITY SWEEP (ID 9001: Stop-Hunt at Swing High / Swing Low)
    # -------------------------------------------------------------------------
    sweep_start = max(0, idx - SWEEP_LOOKBACK)
    prior_swing_high = max(float(highs[i]) for i in range(sweep_start, idx))
    prior_swing_low = min(float(lows[i]) for i in range(sweep_start, idx))

    upper_wick_ratio = (high_p - max(open_p, price)) / rng
    lower_wick_ratio = (min(open_p, price) - low_p) / rng

    # Bearish Liquidity Sweep (Bull Trap / Stop-Hunt above Swing High -> SHORT)
    swept_high = high_p > prior_swing_high and price < prior_swing_high
    bearish_rejection = (
        swept_high
        and (upper_wick_ratio >= MIN_SWEEP_WICK_RATIO or price < open_p)
        and vol_ratio >= MIN_SWEEP_VOL_RATIO
        and min(buy_share_1, buy_share_2) <= BEARISH_CVD_MAX_BUY_SHARE
        and (btc_bearish_bias is not False)
        and not oi_quad["blocks_short"]
    )
    if bearish_rejection:
        fvg_info = detect_fair_value_gap(klines, idx, "SHORT", atr_val)
        raw_sl = max(high_p + 0.25 * atr_val, price * (1.0 + MIN_STOP_PCT / 100.0))
        risk_pct = (raw_sl - price) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=False)
            squeeze_tag = ""
            if (funding_rate_pct is not None and funding_rate_pct > 0.01) or (oi_change_pct is not None and oi_change_pct >= 3.0):
                squeeze_tag = " · Crowded Long Squeeze"
            if fvg_info["fvg_present"]:
                squeeze_tag += f" · Bearish FVG (${fvg_info['fvg_midpoint']:.5g})"
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9001,
                "strategy_name": "Smart Money Liquidity Sweep",
                "strategy_ids": [9001],
                "strategy_names": ["Smart Money Liquidity Sweep"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "SHORT",
                "action": "SELL",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Bearish Stop-Hunt above ${prior_swing_high:.6g} · "
                    f"rejection wick {upper_wick_ratio * 100:.0f}% · "
                    f"seller CVD {(1 - min(buy_share_1, buy_share_2)) * 100:.0f}% · vol {vol_ratio}x{squeeze_tag}"
                ),
                "details": {
                    "item_id": 9001,
                    "strategy": "Smart Money Liquidity Sweep",
                    "trigger_type": "LIQUIDITY_SWEEP_HIGH",
                    "swept_level": round(prior_swing_high, 8),
                    "wick_pct": round(upper_wick_ratio * 100.0, 1),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(min(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "fvg_present": fvg_info["fvg_present"],
                    "fvg_midpoint": fvg_info["fvg_midpoint"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    # Bullish Liquidity Sweep (Bear Trap / Stop-Hunt below Swing Low -> LONG)
    swept_low = low_p < prior_swing_low and price > prior_swing_low
    bullish_rejection = (
        swept_low
        and (lower_wick_ratio >= MIN_SWEEP_WICK_RATIO or price > open_p)
        and vol_ratio >= MIN_SWEEP_VOL_RATIO
        and max(buy_share_1, buy_share_2) >= BULLISH_CVD_MIN_BUY_SHARE
        and (btc_bearish_bias is False)
        and price >= ema50 * 0.992
        and not oi_quad["blocks_long"]
    )
    if bullish_rejection:
        fvg_info = detect_fair_value_gap(klines, idx, "LONG", atr_val)
        raw_sl = min(low_p - 0.25 * atr_val, price * (1.0 - MIN_STOP_PCT / 100.0))
        risk_pct = (price - raw_sl) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=True)
            fvg_tag = f" · Bullish FVG (${fvg_info['fvg_midpoint']:.5g})" if fvg_info["fvg_present"] else ""
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9001,
                "strategy_name": "Smart Money Liquidity Sweep",
                "strategy_ids": [9001],
                "strategy_names": ["Smart Money Liquidity Sweep"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "LONG",
                "action": "BUY",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Bullish Stop-Hunt below ${prior_swing_low:.6g} · "
                    f"rejection wick {lower_wick_ratio * 100:.0f}% · "
                    f"buyer CVD {max(buy_share_1, buy_share_2) * 100:.0f}% · vol {vol_ratio}x{fvg_tag}"
                ),
                "details": {
                    "item_id": 9001,
                    "strategy": "Smart Money Liquidity Sweep",
                    "trigger_type": "LIQUIDITY_SWEEP_LOW",
                    "swept_level": round(prior_swing_low, 8),
                    "wick_pct": round(lower_wick_ratio * 100.0, 1),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(max(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "fvg_present": fvg_info["fvg_present"],
                    "fvg_midpoint": fvg_info["fvg_midpoint"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    # -------------------------------------------------------------------------
    # 2. SMART-MONEY CVD BREAKDOWN / BREAKOUT (ID 9002)
    # -------------------------------------------------------------------------
    bd_start = max(0, idx - BREAKDOWN_LOOKBACK)
    support_floor = min(float(lows[i]) for i in range(bd_start, idx))
    resistance_ceiling = max(float(highs[i]) for i in range(bd_start, idx))

    not_capitulated = (
        not (rsi_val is not None and rsi_val < 20.0 and price_chg_4b <= -4.5)
        and not oi_quad["blocks_short"]
    )
    is_bearish_structure = price < ema20 <= ema50 or (btc_bearish_bias is True and price < ema20)
    broke_support = price < support_floor and open_p > price
    strong_bear_displacement = (0.55 * atr_val <= body <= 1.85 * atr_val) and vol_ratio >= MIN_BREAKDOWN_VOL_RATIO
    aggressive_sellers = min(buy_share_1, buy_share_2) <= BEARISH_CVD_MAX_BUY_SHARE or cvd_z <= -1.2

    if is_bearish_structure and broke_support and strong_bear_displacement and aggressive_sellers and not_capitulated:
        fvg_info = detect_fair_value_gap(klines, idx, "SHORT", atr_val)
        raw_sl = max(high_p + 0.20 * atr_val, price * (1.0 + MIN_STOP_PCT / 100.0))
        risk_pct = (raw_sl - price) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=False)
            fvg_tag = f" · FVG Zone ${fvg_info['fvg_midpoint']:.5g}" if fvg_info["fvg_present"] else ""
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9002,
                "strategy_name": "Smart Money CVD Breakdown",
                "strategy_ids": [9002],
                "strategy_names": ["Smart Money CVD Breakdown"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "SHORT",
                "action": "SELL",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Support Breakdown below ${support_floor:.6g} · "
                    f"seller CVD {(1 - min(buy_share_1, buy_share_2)) * 100:.0f}% (z={cvd_z:+.1f}) · "
                    f"vol {vol_ratio}x · EMA20 < EMA50{fvg_tag}"
                ),
                "details": {
                    "item_id": 9002,
                    "strategy": "Smart Money CVD Breakdown",
                    "trigger_type": "CVD_SUPPORT_BREAKDOWN",
                    "broken_level": round(support_floor, 8),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(min(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "fvg_present": fvg_info["fvg_present"],
                    "fvg_midpoint": fvg_info["fvg_midpoint"],
                    "overextended_candle": fvg_info["overextended"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    not_overbought = (
        not (rsi_val is not None and rsi_val > 80.0 and price_chg_4b >= 4.5)
        and not oi_quad["blocks_long"]
    )
    is_bullish_structure = price > ema20 >= ema50 and (btc_bearish_bias is False)
    broke_resistance = price > resistance_ceiling and price > open_p
    strong_bull_displacement = (0.55 * atr_val <= body <= 1.85 * atr_val) and vol_ratio >= MIN_BREAKDOWN_VOL_RATIO
    aggressive_buyers = max(buy_share_1, buy_share_2) >= BULLISH_CVD_MIN_BUY_SHARE and cvd_z >= 1.1

    if is_bullish_structure and broke_resistance and strong_bull_displacement and aggressive_buyers and not_overbought:
        fvg_info = detect_fair_value_gap(klines, idx, "LONG", atr_val)
        raw_sl = min(low_p - 0.20 * atr_val, price * (1.0 - MIN_STOP_PCT / 100.0))
        risk_pct = (price - raw_sl) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=True)
            fvg_tag = f" · FVG Zone ${fvg_info['fvg_midpoint']:.5g}" if fvg_info["fvg_present"] else ""
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9002,
                "strategy_name": "Smart Money CVD Breakdown",
                "strategy_ids": [9002],
                "strategy_names": ["Smart Money CVD Breakdown"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "LONG",
                "action": "BUY",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Resistance Breakout above ${resistance_ceiling:.6g} · "
                    f"buyer CVD {max(buy_share_1, buy_share_2) * 100:.0f}% (z={cvd_z:+.1f}) · "
                    f"vol {vol_ratio}x · EMA20 > EMA50{fvg_tag}"
                ),
                "details": {
                    "item_id": 9002,
                    "strategy": "Smart Money CVD Breakdown",
                    "trigger_type": "CVD_RESISTANCE_BREAKOUT",
                    "broken_level": round(resistance_ceiling, 8),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(max(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "fvg_present": fvg_info["fvg_present"],
                    "fvg_midpoint": fvg_info["fvg_midpoint"],
                    "overextended_candle": fvg_info["overextended"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    # -------------------------------------------------------------------------
    # 3. SMART-MONEY EMA20 TREND PULLBACK (ID 9003: Captures $83k -> $73k Stair-Step Trends)
    # -------------------------------------------------------------------------
    prev_high = float(highs[idx - 1])
    prev_low = float(lows[idx - 1])
    touched_ema20_short = max(high_p, prev_high) >= ema20 * 0.996 and max(high_p, prev_high) <= ema50 * 1.006
    bearish_pullback = (
        ema20 < ema50
        and touched_ema20_short
        and price < ema20
        and price < open_p
        and body >= 0.30 * atr_val
        and vol_ratio >= 1.10
        and min(buy_share_1, buy_share_2) <= 0.485
        and (rsi_val is None or rsi_val <= 65.0)
        and (btc_bearish_bias is not False)
        and not oi_quad["blocks_short"]
    )
    if bearish_pullback:
        pullback_peak = max(high_p, prev_high)
        raw_sl = max(pullback_peak + 0.25 * atr_val, price * (1.0 + MIN_STOP_PCT / 100.0))
        risk_pct = (raw_sl - price) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=False)
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9003,
                "strategy_name": "Smart Money EMA20 Pullback",
                "strategy_ids": [9003],
                "strategy_names": ["Smart Money EMA20 Pullback"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "SHORT",
                "action": "SELL",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Downtrend EMA20 Pullback Rejection at ${ema20:.6g} · "
                    f"seller CVD {(1 - min(buy_share_1, buy_share_2)) * 100:.0f}% · "
                    f"vol {vol_ratio}x · EMA20 < EMA50"
                ),
                "details": {
                    "item_id": 9003,
                    "strategy": "Smart Money EMA20 Pullback",
                    "trigger_type": "EMA20_BEARISH_PULLBACK",
                    "ema20": round(ema20, 8),
                    "ema50": round(ema50, 8),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(min(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    touched_ema20_long = min(low_p, prev_low) <= ema20 * 1.004 and min(low_p, prev_low) >= ema50 * 0.994
    bullish_pullback = (
        ema20 > ema50
        and touched_ema20_long
        and price > ema20
        and price > open_p
        and body >= 0.30 * atr_val
        and vol_ratio >= 1.10
        and max(buy_share_1, buy_share_2) >= 0.515
        and (rsi_val is None or rsi_val >= 35.0)
        and (btc_bearish_bias is False)
        and not oi_quad["blocks_long"]
    )
    if bullish_pullback:
        pullback_trough = min(low_p, prev_low)
        raw_sl = min(pullback_trough - 0.25 * atr_val, price * (1.0 - MIN_STOP_PCT / 100.0))
        risk_pct = (price - raw_sl) / price * 100.0
        if MIN_STOP_PCT <= risk_pct <= MAX_STOP_PCT:
            tp1, tp2, r_pct = compute_sniper_targets(price, raw_sl, is_long=True)
            results.append({
                "symbol": symbol,
                "scanner": "STRATEGY",
                "strategy_id": 9003,
                "strategy_name": "Smart Money EMA20 Pullback",
                "strategy_ids": [9003],
                "strategy_names": ["Smart Money EMA20 Pullback"],
                "strategy_agreement_count": 2,
                "timeframe": timeframe,
                "direction": "LONG",
                "action": "BUY",
                "price": price,
                "stop_loss": round(raw_sl, 8),
                "take_profit_1": tp1,
                "take_profit_2": tp2,
                "risk_pct": r_pct,
                "watch_only": False,
                "confidence": (
                    f"Uptrend EMA20 Pullback Bounce at ${ema20:.6g} · "
                    f"buyer CVD {max(buy_share_1, buy_share_2) * 100:.0f}% · "
                    f"vol {vol_ratio}x · EMA20 > EMA50"
                ),
                "details": {
                    "item_id": 9003,
                    "strategy": "Smart Money EMA20 Pullback",
                    "trigger_type": "EMA20_BULLISH_PULLBACK",
                    "ema20": round(ema20, 8),
                    "ema50": round(ema50, 8),
                    "volume_ratio": vol_ratio,
                    "trigger_volume_ratio": vol_ratio,
                    "taker_buy_share_pct": round(max(buy_share_1, buy_share_2) * 100.0, 1),
                    "cvd_zscore": cvd_z,
                    "oi_quadrant": oi_quad["quadrant"],
                    "rsi_14": rsi_val,
                    "price_change_4b_pct": round(price_chg_4b, 2),
                    "rr_tp1": TP1_RR,
                    "rr_tp2": TP2_RR,
                },
            })

    return results


def evaluate_latest(
    symbol: str,
    klines: dict[str, Any],
    timeframe: str,
    btc_bearish_bias: bool | None = None,
    funding_rate_pct: float | None = None,
    oi_change_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Evaluate Smart-Money setups on the latest closed candle (`len(closes) - 2`)."""
    closes = (klines or {}).get("close") or []
    if len(closes) < 55:
        return []
    return evaluate_at_index(
        symbol,
        klines,
        timeframe,
        len(closes) - 2,
        btc_bearish_bias=btc_bearish_bias,
        funding_rate_pct=funding_rate_pct,
        oi_change_pct=oi_change_pct,
    )
