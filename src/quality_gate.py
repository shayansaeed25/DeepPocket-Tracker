"""Signal quality gate — blocks low-probability setups before they reach Discord or the Sheet.

Every condition below comes directly from the 7-day backtest on 2026-10-10 .. 2026-10-17
(264 closed signals, -$119.51 net):

  1. Family-specific stop caps:
       - SMART_VECTOR solo: stop <= 2.5% (wider stops bled heavily)
       - CONSENSUS:         stop <= 4.0%
       - WHALE_FLOW:        stop <= 3.5%
  2. Stop distance < 0.2% of entry:  whipsaw / stablecoin noise             -> floor stop at 0.2%
  3. BUY when BTC momentum or overall regime is bearish (<= -12 / -20):     -> block counter-regime longs
  4. SELL when BTC momentum or overall regime is bullish (>= +12 / +20):    -> block counter-regime shorts
  5. Same-minute signal bursts (up to 20 at once):                          -> cap new signals per scan + open per family
  6. Whale Flow pool liquidity < $500k or < 15 swaps or < 40% net flow:     -> require real pool depth
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_STOP_PCT = 4.0
MIN_STOP_PCT = 0.2
MAX_STOP_PCT_BY_FAMILY = {
    "SMART_VECTOR": 2.5,
    "CONSENSUS": 4.0,
    "WHALE_FLOW": 3.5,
}
SMART_VECTOR_SOLO_TIMEFRAMES = {"4h"}

BTC_GUARD_ENABLED = True
BTC_GUARD_SELL_ENABLED = True
BTC_GUARD_THRESHOLD = 20.0
REGIME_BUY_BLOCK_SCORE = -12.0
REGIME_SELL_BLOCK_SCORE = 12.0

WHALE_FLOW_MIN_LIQUIDITY_USD = 500_000
WHALE_FLOW_MIN_SWAPS = 15
WHALE_FLOW_MIN_NET_FLOW_PCT = 40.0

WHALE_FLOW_MIN_STOP_PCT = 1.2
WHALE_FLOW_MAX_STOP_PCT = 3.2
WHALE_FLOW_DEFAULT_STOP_PCT = 2.2
QUALITY_MIN_SCORE = 55.0
ACCOUNT_EQUITY_DEFAULT_USD = 150.0

# Caps per scan run and across currently-open positions, keyed by signal family.
MAX_NEW_PER_SCAN = {
    "CONSENSUS": 3,
    "SMART_VECTOR": 2,
    "WHALE_FLOW": 2,
    "WATCH": 4,
}
MAX_OPEN = {
    "CONSENSUS": 6,
    "SMART_VECTOR": 4,
    "WHALE_FLOW": 4,
    "WATCH": 12,
}


def _ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + float(out[i - 1]) * (1.0 - k)
    return out


def htf_trend(klines: dict[str, Any] | None, is_long: bool = True) -> int:
    """Return +1 if higher-timeframe trend aligns with `is_long`, -1 if it opposes, 0 if mixed/unavailable."""
    if not klines:
        return 0
    closes = klines.get("close") or []
    if len(closes) < 55:
        return 0
    ema20 = _ema(closes, 20)[-1]
    ema50 = _ema(closes, 50)[-1]
    price = closes[-1]
    if ema20 is None or ema50 is None:
        return 0
    if price > ema20 > ema50:
        raw = 1
    elif price < ema20 < ema50:
        raw = -1
    else:
        return 0
    return raw if is_long else -raw


def htf_blocks(h4_trend: int, d1_trend: int) -> bool:
    """Block only when BOTH 4h and 1d trends oppose the trade direction."""
    return h4_trend < 0 and d1_trend < 0


def volume_ratio(klines: dict[str, Any] | None, lookback: int = 20) -> float | None:
    """Ratio of the last CLOSED candle's volume (`volumes[-2]`) to the preceding `lookback` closed candles."""
    if not klines:
        return None
    volumes = klines.get("volume") or []
    if len(volumes) < lookback + 2:
        return None
    closed_vol = float(volumes[-2])
    prior = [float(v) for v in volumes[-(lookback + 2):-2]]
    avg = sum(prior) / len(prior) if prior else 0.0
    if avg <= 0:
        return None
    return closed_vol / avg


def quality_score(
    is_long: bool,
    agreement_count: int,
    regime_alignment: str | None,
    h4_trend: int,
    d1_trend: int,
    volume_ratio: float | None,
) -> tuple[float, dict[str, float]]:
    """Compute a 0..100 quality score and its component breakdown (trend 30, volume 25, consensus 25, regime 20)."""
    if h4_trend > 0 and d1_trend > 0:
        trend_pts = 30.0
    elif h4_trend < 0 and d1_trend < 0:
        trend_pts = 0.0
    elif h4_trend > 0 or d1_trend > 0:
        trend_pts = 22.0 if (h4_trend >= 0 and d1_trend >= 0) else 15.0
    elif h4_trend < 0 or d1_trend < 0:
        trend_pts = 5.0
    else:
        trend_pts = 15.0

    if volume_ratio is None:
        vol_pts = 12.5
    else:
        vol_pts = round(max(0.0, min(25.0, (float(volume_ratio) - 0.5) * 25.0)), 2)

    count = max(1, int(agreement_count or 1))
    if count >= 3:
        cons_pts = 25.0
    elif count == 2:
        cons_pts = 18.0
    else:
        cons_pts = 10.0

    reg = str(regime_alignment or "UNKNOWN").upper()
    if reg == "ALIGNED":
        reg_pts = 20.0
    elif reg in {"NEUTRAL", "UNKNOWN"}:
        reg_pts = 10.0
    else:
        reg_pts = 0.0

    parts = {
        "trend": trend_pts,
        "volume": vol_pts,
        "consensus": cons_pts,
        "regime": reg_pts,
    }
    total = round(min(100.0, sum(parts.values())), 2)
    return total, parts


def atr_stop_pct(klines: dict[str, Any] | None, period: int = 14, mult: float = 1.5) -> float:
    """Volatility-scaled stop distance (%) clamped to [WHALE_FLOW_MIN_STOP_PCT, WHALE_FLOW_MAX_STOP_PCT]."""
    if not klines:
        return WHALE_FLOW_DEFAULT_STOP_PCT
    closes = klines.get("close") or []
    highs = klines.get("high") or []
    lows = klines.get("low") or []
    if len(closes) < period + 2 or len(highs) < len(closes) or len(lows) < len(closes):
        return WHALE_FLOW_DEFAULT_STOP_PCT
    idx = len(closes) - 2
    trs: list[float] = []
    for i in range(1, idx + 1):
        tr = max(
            float(highs[i]) - float(lows[i]),
            abs(float(highs[i]) - float(closes[i - 1])),
            abs(float(lows[i]) - float(closes[i - 1])),
        )
        trs.append(tr)
    if len(trs) < period:
        return WHALE_FLOW_DEFAULT_STOP_PCT
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    price = float(closes[idx])
    if price <= 0:
        return WHALE_FLOW_DEFAULT_STOP_PCT
    raw_pct = (atr * mult / price) * 100.0
    return round(max(WHALE_FLOW_MIN_STOP_PCT, min(WHALE_FLOW_MAX_STOP_PCT, raw_pct)), 4)


def size_for_risk(
    entry: float,
    stop: float,
    equity_usd: float = ACCOUNT_EQUITY_DEFAULT_USD,
    risk_pct: float = 1.0,
) -> float | None:
    """Position notional in USD so a stop-out loses `risk_pct`% of `equity_usd`."""
    try:
        entry_f, stop_f = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    if entry_f <= 0 or stop_f <= 0 or entry_f == stop_f:
        return None
    stop_dist_pct = abs(entry_f - stop_f) / entry_f
    if stop_dist_pct <= 0:
        return None
    risk_dollars = float(equity_usd) * (float(risk_pct) / 100.0)
    return round(risk_dollars / stop_dist_pct, 2)


def annotate_sizing(
    signal: dict[str, Any],
    equity_usd: float = ACCOUNT_EQUITY_DEFAULT_USD,
    risk_pct: float = 1.0,
) -> dict[str, Any]:
    """Attach `size_1pct_risk_usd` to `signal['details']` when entry and stop_loss are valid."""
    size = size_for_risk(signal.get("price"), signal.get("stop_loss"), equity_usd=equity_usd, risk_pct=risk_pct)
    if size is not None:
        details = dict(signal.get("details") or {}) if isinstance(signal.get("details"), dict) else {}
        details["size_1pct_risk_usd"] = size
        signal["details"] = details
    return signal


def loss_streak_pause(
    journal_path: str,
    now: datetime | None = None,
    extra_rows: list[dict[str, Any]] | None = None,
    streak_limit: int = 3,
    lookback_hours: float = 4.0,
) -> str | None:
    """Return a pause reason if the last `streak_limit` closed trades within `lookback_hours` were all SL_HIT."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    try:
        with open(journal_path, "r", newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    except OSError:
        pass
    if extra_rows:
        rows.extend(extra_rows)
    terminal_outcomes = {"SL_HIT", "TP2_HIT", "TP3_HIT", "BE_HIT"}
    terminal: list[tuple[datetime, str]] = []
    for r in rows:
        outcome = str(r.get("outcome") or "").strip().upper()
        if outcome not in terminal_outcomes:
            continue
        ts_raw = str(r.get("event_time_utc") or "").strip()
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        terminal.append((ts, outcome))
    terminal.sort(key=lambda item: item[0])
    if len(terminal) < streak_limit:
        return None
    recent = terminal[-streak_limit:]
    cutoff = now - timedelta(hours=lookback_hours)
    if all(outcome == "SL_HIT" and ts >= cutoff for ts, outcome in recent):
        return f"{streak_limit} stop-outs in a row within the last {lookback_hours:g}h — pausing new entries"
    return None


def stop_distance_pct(signal: dict[str, Any]) -> float | None:
    try:
        price = float(signal.get("price") or 0)
        stop = float(signal.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or stop <= 0:
        return None
    return abs(price - stop) / price * 100.0


def family_of(strategy_names: list[str] | None, scanner: str = "STRATEGY") -> str | None:
    if scanner in {"WHALE", "WHALE_FLOW"}:
        return "WHALE_FLOW"
    names = [str(n).strip() for n in (strategy_names or []) if str(n).strip()]
    if not names:
        return None
    if len(names) >= 2:
        return "CONSENSUS"
    if names == ["Smart Vector"]:
        return "SMART_VECTOR"
    return "CONSENSUS"


def family_of_trade(trade: dict[str, Any]) -> str | None:
    details = trade.get("details") if isinstance(trade.get("details"), dict) else {}
    tier = trade.get("tier") or details.get("tier")
    if tier == "C":
        return "WATCH"
    scanner = str(trade.get("scanner") or "").upper()
    if not scanner.startswith("STRATEGY"):
        return None
    name = str(trade.get("strategy_name") or "")
    if "Consensus:" in name or len(trade.get("strategy_names") or []) >= 2:
        return "CONSENSUS"
    if "Smart Vector" in name or "Smart Vector" in scanner:
        return "SMART_VECTOR"
    return "CONSENSUS" if name else None


def btc_guard(signal: dict[str, Any], regime: dict[str, Any] | None) -> str | None:
    """Return a rejection reason if BTC momentum or composite regime opposes the signal direction."""
    if not BTC_GUARD_ENABLED or not regime:
        return None
    raw_dir = str(signal.get("direction") or signal.get("action") or "").upper()
    is_long = raw_dir in {"LONG", "BUY"}
    is_short = raw_dir in {"SHORT", "SELL"}
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    if is_long and details.get("wz_reversal_override"):
        return None

    # v6.3 Relief-Rally Unblock: when BTC 1h & 4h are BOTH trending UP while composite regime
    # is only moderately bearish (> -40), allow H4-aligned LONGs so the bot doesn't deadlock at 0 trades.
    btc_note = str(((regime.get("components") or {}).get("btc_momentum") or {}).get("note") or "")
    r_score_raw = regime.get("score")
    intraday_relief_rally = (
        "1h ↑" in btc_note
        and "4h ↑" in btc_note
        and (r_score_raw is None or float(r_score_raw) > -40.0)
    )
    if is_long and intraday_relief_rally:
        return None

    btc_mom = ((regime.get("components") or {}).get("btc_momentum") or {}).get("score")
    if btc_mom is not None:
        try:
            mom = float(btc_mom)
            if is_long and mom <= -BTC_GUARD_THRESHOLD:
                return f"BTC momentum is bearish ({mom:+.0f})"
            if is_short and BTC_GUARD_SELL_ENABLED and mom >= BTC_GUARD_THRESHOLD:
                return f"BTC momentum is bullish ({mom:+.0f})"
        except (TypeError, ValueError):
            pass

    regime_label = regime.get("label")
    regime_score = regime.get("score")
    if is_long and regime_label == "BEARISH":
        return f"BTC regime is bearish ({regime_score})"
    if is_short and BTC_GUARD_SELL_ENABLED and regime_label == "BULLISH":
        return f"BTC regime is bullish ({regime_score})"
    if regime_score is not None:
        try:
            score_val = float(regime_score)
            if is_long and score_val <= REGIME_BUY_BLOCK_SCORE:
                return f"BTC regime score is bearish ({score_val:+.0f} <= {REGIME_BUY_BLOCK_SCORE:+.0f})"
            if is_short and BTC_GUARD_SELL_ENABLED and score_val >= REGIME_SELL_BLOCK_SCORE:
                return f"BTC regime score is bullish ({score_val:+.0f} >= {REGIME_SELL_BLOCK_SCORE:+.0f})"
        except (TypeError, ValueError):
            pass
    return None


def check(signal: dict[str, Any], family: str | None = None, regime: dict[str, Any] | None = None) -> str | None:
    """Return a human-readable rejection reason, or None when the signal passes."""
    dist = stop_distance_pct(signal)
    if dist is None:
        return "no valid stop"
    cap = MAX_STOP_PCT_BY_FAMILY.get(family or "", MAX_STOP_PCT)
    if dist > cap:
        return f"stop distance {dist:.2f}% is wider than {cap:.1f}% cap"
    if dist < MIN_STOP_PCT:
        return f"stop distance {dist:.2f}% is tighter than {MIN_STOP_PCT:.1f}% noise floor"

    guard_msg = btc_guard(signal, regime)
    if guard_msg:
        return guard_msg

    if family == "SMART_VECTOR":
        tf = str(signal.get("timeframe") or "")
        if tf and tf not in SMART_VECTOR_SOLO_TIMEFRAMES:
            return f"Smart Vector solo on {tf} blocked (allowed: {sorted(SMART_VECTOR_SOLO_TIMEFRAMES)})"

    if family == "WHALE_FLOW":
        details = signal.get("details") or {}
        if details:
            liq = float(details.get("pool_liquidity_usd") or 0)
            swaps = int(details.get("trade_count") or 0)
            net = abs(float(details.get("net_buy_sell_imbalance_pct") or 0))
            if liq < WHALE_FLOW_MIN_LIQUIDITY_USD:
                return f"pool liquidity ${liq:,.0f} < ${WHALE_FLOW_MIN_LIQUIDITY_USD:,.0f}"
            if swaps < WHALE_FLOW_MIN_SWAPS:
                return f"swap count {swaps} < {WHALE_FLOW_MIN_SWAPS}"
            if net < WHALE_FLOW_MIN_NET_FLOW_PCT:
                return f"net flow {net:.1f}% < {WHALE_FLOW_MIN_NET_FLOW_PCT:.0f}%"

    return None


def rank_key(signal: dict[str, Any]) -> tuple:
    """Sort best-first: higher quality_score when present, then more agreeing rules, 4h over 1h, tighter stop."""
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    q_score = float(details.get("quality_score") or 0.0)
    fams = int(details.get("independent_families") or signal.get("strategy_agreement_count") or 1)
    dist = stop_distance_pct(signal) or 99.0
    return (
        -round(q_score, 2),
        -fams,
        -int(signal.get("strategy_agreement_count") or 1),
        0 if signal.get("timeframe") == "4h" else 1,
        round(dist, 4),
    )
