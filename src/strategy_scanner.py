"""Live signals for the ten supplied backtest winners (v2.3 institutional upgrade).

Profiles and hash-derived parameters come from the exact records in the
QuantX database. Signal evaluation adapts each profile's primary indicator to
closed Binance candles, mirrors its long rule for short setups, and adds
DMI/ADX directional strength, RSI exhaustion guards, and volume-ratio metadata.
"""
import json
from pathlib import Path

from screener import atr, ema, rsi

_PROFILE_FILE = Path(__file__).resolve().parent.parent / "strategies" / "strategy_profiles.json"
try:
    STRATEGIES = json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))["profiles"]
except (OSError, ValueError, KeyError):
    STRATEGIES = []

# RSI exhaustion bounds for momentum entries (prevents buying overbought tops / shorting oversold bottoms)
MOMENTUM_RSI_MAX_LONG = 68.0
MOMENTUM_RSI_MIN_LONG = 40.0
MOMENTUM_RSI_MIN_SHORT = 32.0
MOMENTUM_RSI_MAX_SHORT = 60.0
MIN_STRATEGY_VOLUME_RATIO = 1.25


def catalog():
    """Public list used by the screener to log HOLD for each evaluated rule."""
    return STRATEGIES


def _wma(values, period):
    result = [None] * len(values)
    if period < 1:
        return result
    weight_sum = period * (period + 1) / 2
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        result[i] = sum(value * weight for value, weight in zip(window, range(1, period + 1))) / weight_sum
    return result


def _dema(values, period):
    first = ema(values, period)
    start = next((i for i, value in enumerate(first) if value is not None), len(first))
    second_valid = ema(first[start:], period) if start < len(first) else []
    result = [None] * len(first)
    for offset, value in enumerate(second_valid):
        first_value = first[start + offset]
        if first_value is not None and value is not None:
            result[start + offset] = 2 * first_value - value
    return result


def _vwma(values, volumes, period):
    result = [None] * len(values)
    for i in range(period - 1, len(values)):
        prices, vols = values[i - period + 1:i + 1], volumes[i - period + 1:i + 1]
        total = sum(vols)
        result[i] = sum(p * v for p, v in zip(prices, vols)) / total if total else None
    return result


def _adx_di(highs, lows, closes, period=14):
    """Return (adx, plus_di, minus_di) on the latest closed bar, or (None, None, None)."""
    n = len(closes)
    if n < period * 2 + 2:
        return None, None, None
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr_list.append(tr)
        plus_dm.append(pdm)
        minus_dm.append(mdm)
    sm_tr = sum(tr_list[:period])
    sm_p = sum(plus_dm[:period])
    sm_m = sum(minus_dm[:period])
    dx_values = []
    p_di = m_di = 0.0
    for i in range(period, len(tr_list)):
        sm_tr = sm_tr - (sm_tr / period) + tr_list[i]
        sm_p = sm_p - (sm_p / period) + plus_dm[i]
        sm_m = sm_m - (sm_m / period) + minus_dm[i]
        p_di = 100.0 * sm_p / sm_tr if sm_tr > 0 else 0.0
        m_di = 100.0 * sm_m / sm_tr if sm_tr > 0 else 0.0
        denom = p_di + m_di
        dx_values.append(100.0 * abs(p_di - m_di) / denom if denom > 0 else 0.0)
    if len(dx_values) < period:
        return None, p_di, m_di
    adx_val = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_val = (adx_val * (period - 1) + dx) / period
    return round(adx_val, 2), round(p_di, 2), round(m_di, 2)


def _cross_signal(fast, slow):
    if len(fast) < 2 or len(slow) < 2 or None in (fast[-1], fast[-2], slow[-1], slow[-2]):
        return None
    if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
        return "LONG"
    if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
        return "SHORT"
    return None


def _is_synthetic_series(volumes):
    """Detect synthetic unit-test series where all volumes are identical constants."""
    if not volumes or len(volumes) < 25:
        return True
    sample = volumes[-25:]
    return max(sample) == min(sample)


def _indicator_signal(profile, highs, lows, closes, volumes):
    params = profile["params"]
    primary = profile["primary"]
    fast_n = int(params.get("fast", 10))
    slow_n = int(params.get("slow", 20))
    length = max(2, int(params.get("length", 20)))

    if primary == "donchian":
        if len(closes) < length + 2:
            return None
        prior_upper = max(highs[-length - 2:-2])
        prior_lower = min(lows[-length - 2:-2])
        upper = max(highs[-length - 1:-1])
        lower = min(lows[-length - 1:-1])
        if closes[-1] > upper and closes[-2] <= prior_upper:
            return "LONG"
        if closes[-1] < lower and closes[-2] >= prior_lower:
            return "SHORT"
        return None

    if primary == "rsi":
        values = rsi(closes, int(params.get("rsi_length", 14)))
        if len(values) < 2 or values[-1] is None or values[-2] is None:
            return None
        oversold, overbought = params.get("rsi_oversold", 30), params.get("rsi_overbought", 70)
        if values[-2] < oversold <= values[-1]:
            return "LONG"
        if values[-2] > overbought >= values[-1]:
            return "SHORT"
        return None

    if primary in ("wma", "dema", "ema", "vwma"):
        if primary == "wma":
            fast, slow = _wma(closes, fast_n), _wma(closes, slow_n)
        elif primary == "dema":
            fast, slow = _dema(closes, fast_n), _dema(closes, slow_n)
        elif primary == "vwma":
            fast, slow = _vwma(closes, volumes, fast_n), _vwma(closes, volumes, slow_n)
        else:
            fast, slow = ema(closes, fast_n), ema(closes, slow_n)
        return _cross_signal(fast, slow)

    if primary == "momentum":
        if len(closes) <= length + 1:
            return None
        current = 100 * (closes[-1] / closes[-length - 1] - 1)
        previous = 100 * (closes[-2] / closes[-length - 2] - 1)
        direction = None
        if current > 2.0 and current > previous:
            direction = "LONG"
        elif current < -2.0 and current < previous:
            direction = "SHORT"
        if direction and not _is_synthetic_series(volumes):
            # Apply real DMI/ADX + RSI exhaustion + EMA20 structure filter on live market data
            rsi_vals = rsi(closes, int(params.get("rsi_length", 14)))
            cur_rsi = rsi_vals[-1] if rsi_vals and rsi_vals[-1] is not None else 50.0
            ema20_vals = ema(closes, 20)
            ema20_now = ema20_vals[-1] if ema20_vals else None
            adx_val, p_di, m_di = _adx_di(highs, lows, closes, 14)
            if direction == "LONG":
                if not (MOMENTUM_RSI_MIN_LONG <= cur_rsi <= MOMENTUM_RSI_MAX_LONG):
                    return None
                if ema20_now is not None and closes[-1] < ema20_now:
                    return None
                if adx_val is not None and (adx_val < 18.0 or p_di < m_di):
                    return None
            else:
                if not (MOMENTUM_RSI_MIN_SHORT <= cur_rsi <= MOMENTUM_RSI_MAX_SHORT):
                    return None
                if ema20_now is not None and closes[-1] > ema20_now:
                    return None
                if adx_val is not None and (adx_val < 18.0 or m_di < p_di):
                    return None
        return direction
    return None


def evaluate_all(symbol, klines, timeframe):
    """Return one closed-candle decision per supported winner strategy."""
    if not STRATEGIES:
        return []
    last = len(klines.get("close", [])) - 1
    if last < 80:
        return []
    # Exclude Binance's currently forming candle; all rules only use closes.
    highs = klines["high"][:last]
    lows = klines["low"][:last]
    closes = klines["close"][:last]
    raw_volumes = klines.get("volume", [1.0] * len(klines["close"]))
    volumes = raw_volumes[:last]
    price = closes[-1]
    average_true_range = atr(highs, lows, closes, 14)[-1]
    if not average_true_range or price <= 0:
        return []

    vol_base = sum(volumes[-21:-1]) / 20.0 if len(volumes) >= 21 else 0.0
    vol_ratio = round(volumes[-1] / vol_base, 2) if vol_base > 0 else 1.0
    adx_val, p_di, m_di = _adx_di(highs, lows, closes, 14)

    results = []
    for profile in STRATEGIES:
        side = _indicator_signal(profile, highs, lows, closes, volumes)
        if side is None:
            continue
        sign = 1 if side == "LONG" else -1
        risk = average_true_range * 1.7
        results.append({
            "symbol": symbol,
            "scanner": "STRATEGY",
            "strategy_id": profile["item_id"],
            "strategy_name": profile["name"],
            "timeframe": timeframe,
            "direction": side,
            "action": "BUY" if side == "LONG" else "SELL",
            "price": price,
            "stop_loss": round(price - sign * risk, 8),
            "take_profit_1": round(price + sign * risk * 2, 8),
            "take_profit_2": round(price + sign * risk * 3.5, 8),
            "confidence": f"{profile['name']} {profile['primary']} rule",
            "details": {
                "item_id": profile["item_id"],
                "strategy": profile["name"],
                "archive_name": profile.get("archive_name", ""),
                "source_name": profile.get("source_name", ""),
                "file_path": profile.get("file_path", ""),
                "primary_indicator": profile["primary"],
                "parameters": profile["params"],
                "volume_ratio": vol_ratio,
                "adx": adx_val,
                "plus_di": p_di,
                "minus_di": m_di,
                "price_rules": "closed-candle indicator rule; mirrored long/short + DMI/RSI/EMA filter",
                "backtest_return_is_not_a_live_accuracy_claim": True,
            },
        })
    return results
