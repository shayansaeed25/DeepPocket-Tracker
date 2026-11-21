"""
DeepPocket Tracker (v3.0 — Smart-Money Liquidity & Vectorized Backtester Edition)
---------------------------------------------------------------------------------
Refactored quantitative crypto scanner & trading signal engine built around
four institutional pillars:

  1. Target Universe Focus (Top 30 High-Liquidity Crypto USDT Pairs):
     Eliminates synthetic stock mirrors (`NVDAB`, `TSLAB`, `SPCXB`, `EWYB`),
     gold pegs (`XAUT`), and meme/pump noise (`PUMP`, `MUBARAK`).
  2. Fast Vectorized Historical Candle Backtester (`src/backtester.py`):
     Evaluates all active strategies across historical 1h/4h OHLCV candles on
     every scan, ranking them by Profit Factor, Max Drawdown %, Win Rate %, and
     SL Rate %, and automatically pruning negative/low-expectancy strategies.
  3. Wall Street Smart-Money Liquidity Mechanics (`src/smart_money.py`):
     Detects Key Level Liquidity Sweeps (Stop-Hunts at 24-bar Swing Highs/Lows)
     and High-Volume CVD Breakdowns/Breakouts with strict structure-based
     Hard Invalidation stops (0.8%..3.0%) and 1:2 / 1:3.5 Risk-to-Reward targets.
  4. Proven Institutional Execution Gates (Validated on Oct 17–27 Dataset:
     85.7% Win Rate, Profit Factor 4.90):
     - Blocks Asia Dead-Zone (`22:00–05:00 UTC` / `03:00–10:00 AM PKT`) entries.
     - Enforces Spot Taker CVD Alignment (`SELL` requires `taker_buy_share <= 50.5%`,
       `BUY` requires `taker_buy_share >= 52.0%`).
     - Enforces Minimum Volume Expansion (`volume_ratio >= 1.15`).
     - Blocks `MIXED` / `COUNTER-TREND` H4+D1 setups and blocks `BUY` setups when
       BTC/Market Regime is Bearish (`score <= -8`).
     - Strict 1-to-1 sync between Discord Alerts and Google Sheets `Positions`.
"""

import os
import json
import re
import sys
import time
import statistics
import requests

from quality_filter import (
    get_quality_symbols,
    DEFAULT_TOP_N,
    MANUAL_MEME_BLACKLIST,
    TOP_30_INSTITUTIONAL_BASES,
)
import movers_strategy
import market_regime
import quality_gate
import risk_manager
import signal_quality
import daily_board
import smart_money
import backtester

WHALE_VOLUME_ENABLED = False
ONCHAIN_DEX_LIVE_ENABLED = os.environ.get("ENABLE_ONCHAIN_DEX_LIVE", "0") == "1"  # Disabled: 0% WR in live audit
MOVERS_LIVE_SCAN_ENABLED = os.environ.get("ENABLE_MOVERS_LIVE", "0") == "1"       # Disabled: prevents bottom-chasing
ONLY_ALERT_TIER_AB = True
LIVE_SCAN_TOP_N = 30           # Restrict live scan to Top 30 high-liquidity pairs
MIN_STRATEGY_VOLUME_RATIO = 1.10
SELL_MAX_TAKER_BUY_PCT = 49.5  # Require sellers in control (<49.5% taker buy) on SHORTs
BUY_MIN_TAKER_BUY_PCT = 50.5   # Require buyers in control (>50.5% taker buy) on LONGs
BEARISH_REGIME_NO_BUY_SCORE = -8.0
BULLISH_REGIME_NO_SELL_SCORE = 10.0

CURRENT_REGIME = None

# ----------------------------- CONFIG ---------------------------------

BINANCE_BASE = "https://data-api.binance.vision"
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

EXCLUDED_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "USDP", "USDE", "USD1", "DAI", "EUR", "EURI", "AEUR",
                        "XUSD", "BFUSD", "RLUSD", "PYUSD", "USDS", "UST", "BUSD", "GUSD"}
MIN_24H_QUOTE_VOLUME_USD = 2_000_000
QUOTE_ASSET = "USDT"
TOP_N_BY_MARKET_VOLUME = 200
QUALITY_TOP_N_MARKET_CAP = 500

KLINE_INTERVAL = "1h"
KLINE_LOOKBACK = 200

VOLUME_SPIKE_MULTIPLIER = 3.0
VOLUME_LOOKBACK_CANDLES = 20
WHALE_PRESSURE_LOOKBACK = 3
WHALE_PRESSURE_MIN_SURGE = 1.5
WHALE_PRESSURE_BUY_SHARE = 0.58
WHALE_PRESSURE_SELL_SHARE = 0.42
WHALE_PRESSURE_MIN_MOVE_PCT = 1.0

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_BULLISH_MIN = 45
RSI_BEARISH_MAX = 55

BB_PERIOD = 20
BB_STDDEV = 2.0

EMA_FAST = 9
EMA_SLOW = 21

ATR_PERIOD = 14
STOP_LOSS_ATR_MULTIPLIER = 1.5
TP1_RISK_REWARD = 2.0
TP2_RISK_REWARD = 3.5

ONLY_SEND_HIGH_CONFIDENCE = True
CONSENSUS_SAME_TIMEFRAME = True
SCAN_TIMEFRAMES = ["1h", "4h"]

RESISTANCE_LOOKBACK = 30
VOLUME_TREND_CANDLES = 4

REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_CALLS = 0.15

STATE_FILE = os.path.join(os.path.dirname(__file__), "..", "last_signals.txt")
SIGNAL_COOLDOWN_HOURS = 6

FUTURES_BASE = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"
BITGET_BASE = "https://api.bitget.com"
FUTURES_FALLBACK_ENABLED = True
ENABLE_OI_CONFIRMATION = True
OI_LOOKBACK = 20
OI_SPIKE_THRESHOLD_PCT = 5.0
ENABLE_FUNDING_RATE = True
ENABLE_MTF_CONFLUENCE = True
ALWAYS_SEND_SCAN_SUMMARY = True

TREND_MOMENTUM_ENABLED = False
TREND_LOOKBACK_CANDLES = 10
TREND_MIN_MOVE_PCT = 12.0
TREND_MIN_DIRECTIONAL_CANDLES_RATIO = 0.6
TREND_MIN_SUSTAINED_VOLUME_RATIO = 1.3
TREND_HIGH_MOVE_PCT = 20.0
TREND_HIGH_DIRECTIONAL_RATIO = 0.7
TREND_HIGH_VOLUME_RATIO = 1.6
TREND_STOP_ATR_MULTIPLIER = 2.0
TREND_ONLY_SEND_HIGH_CONFIDENCE = True
TREND_SIGNAL_COOLDOWN_HOURS = 12

TREND_EARLY_LOOKBACK_CANDLES = 5
TREND_EARLY_MIN_MOVE_PCT = 6.0
TREND_EARLY_MIN_DIRECTIONAL_CANDLES_RATIO = 0.6
TREND_EARLY_MIN_SUSTAINED_VOLUME_RATIO = 1.3
TREND_EARLY_MAX_RSI_LONG = 65
TREND_EARLY_MIN_RSI_SHORT = 35

TREND_LATE_STAGE_RSI_LONG = 75
TREND_LATE_STAGE_RSI_SHORT = 25

NEWS_CONTEXT_ENABLED = True
NEWS_CONTEXT_FILE = os.path.join(os.path.dirname(__file__), "..", "recent_news.json")
NEWS_CONTEXT_LOOKBACK_HOURS = 48


# ----------------------------- INDICATORS ---------------------------------

def sma(values, period):
    return [None] * (period - 1) + [
        sum(values[i - period + 1:i + 1]) / period for i in range(period - 1, len(values))
    ]


def stdev(values, period):
    out = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        out.append(statistics.pstdev(window))
    return out


def ema(values, period):
    out = [None] * len(values)
    k = 2 / (period + 1)
    seed_index = period - 1
    if len(values) <= seed_index:
        return out
    seed = sum(values[:period]) / period
    out[seed_index] = seed
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    out[period] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100 - (100 / (1 + (avg_gain / avg_loss))) if avg_loss != 0 else 100
    return out


def atr(highs, lows, closes, period=14):
    trs = [None]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out = [None] * len(closes)
    if len(trs) <= period:
        return out
    first = sum(trs[1:period + 1]) / period
    out[period] = first
    for i in range(period + 1, len(closes)):
        out[i] = (out[i - 1] * (period - 1) + trs[i]) / period
    return out


def bollinger_bands(closes, period=20, mult=2.0):
    basis = sma(closes, period)
    dev = stdev(closes, period)
    upper = [b + mult * d if b is not None else None for b, d in zip(basis, dev)]
    lower = [b - mult * d if b is not None else None for b, d in zip(basis, dev)]
    return upper, basis, lower


# ----------------------------- INSTITUTIONAL LAYER (v3.0) ---------------------------------

WHALE_FLOW_ATR_MULTIPLIER = 1.7
WHALE_FLOW_FALLBACK_STOP_PCT = 2.5


def atr_trade_levels(klines, direction, atr_multiplier=WHALE_FLOW_ATR_MULTIPLIER):
    if not klines or len(klines.get("close", [])) < 40:
        return None
    last = len(klines["close"]) - 1
    highs, lows, closes = klines["high"][:last], klines["low"][:last], klines["close"][:last]
    value = atr(highs, lows, closes, ATR_PERIOD)[-1]
    price = closes[-1]
    if not price or price <= 0:
        return None
    risk = value * atr_multiplier if value else price * WHALE_FLOW_FALLBACK_STOP_PCT / 100
    sign = 1 if str(direction).upper() in {"LONG", "BUY"} else -1
    return {"price": price, "stop_loss": round(price - sign * risk, 8),
            "take_profit_1": round(price + sign * risk * 2, 8),
            "take_profit_2": round(price + sign * risk * 3.5, 8), "atr": value}


def assess_quality(signal, symbol, direction, independent_families, flow_imbalance_pct=None):
    """Score and grade (tier A/B/C) a signal and annotate its details with HTF trend,
    market session intelligence, and spot orderflow / relative strength."""
    live = not signal.get("watch_only")
    timeframe = str(signal.get("timeframe", "1h"))
    tf_match = re.search(r"\b(\d+[mhd])\b", timeframe)
    interval_token = tf_match.group(1) if tf_match else (timeframe if re.fullmatch(r"\d+[mhd]", timeframe) else None)
    try:
        k4h = get_klines(symbol, "4h", 120)
        k1d = get_klines(symbol, "1d", 120)
        if interval_token == "4h":
            ratio_klines = k4h
        else:
            ratio_klines = get_klines(symbol, interval_token, 120) if interval_token else None
    except requests.RequestException as exc:
        print(f"[quality-score] {symbol}: higher-timeframe data unavailable: {exc}", file=sys.stderr)
        k4h = k1d = ratio_klines = None

    label, votes = signal_quality.trend_alignment(direction, k4h, k1d)
    is_long_sig = str(direction).upper() in {"LONG", "BUY"}
    expected_vote = 1 if is_long_sig else -1
    if label in {"MIXED", "NEUTRAL"} and isinstance(votes, dict) and votes.get("4h") == expected_vote:
        # When 4h trend agrees with the signal during a fresh market dump/rally before the 50-day 1d EMA crosses, treat as PARTIAL
        label = "PARTIAL"
    details = dict(signal.get("details") or {}) if isinstance(signal.get("details"), dict) else {}

    sess = signal_quality.session_info()
    t_buy = signal_quality.taker_buy_ratio(ratio_klines or k4h)
    if t_buy is None and details.get("taker_buy_share_pct") is not None:
        try:
            t_buy = float(details["taker_buy_share_pct"]) / 100.0
        except (TypeError, ValueError):
            t_buy = None
    live_regime_active = isinstance(CURRENT_REGIME, dict) and "components" in CURRENT_REGIME

    if flow_imbalance_pct is not None:
        ratio, vol_part = None, signal_quality.flow_component(flow_imbalance_pct)
        if label in {"ALIGNED", "PARTIAL"}:
            independent_families += 1
    else:
        pre_ratio = None
        for key in ("trigger_volume_ratio", "volume_ratio"):
            if details.get(key) is not None:
                try:
                    pre_ratio = max(pre_ratio or 0.0, float(details[key]))
                except (TypeError, ValueError):
                    pass
        raw_ratio = signal_quality.volume_ratio(ratio_klines)
        if raw_ratio is not None and pre_ratio is not None:
            ratio = max(raw_ratio, pre_ratio)
        else:
            ratio = raw_ratio if raw_ratio is not None else pre_ratio
        vol_part = signal_quality.volume_component(ratio)
        if live_regime_active and label in {"ALIGNED", "PARTIAL"} and ratio is not None and ratio >= 1.5:
            is_long_dir = str(direction).upper() in {"LONG", "BUY"}
            orderflow_agrees = t_buy is not None and ((is_long_dir and t_buy >= 0.52) or (not is_long_dir and t_buy <= 0.48))
            if orderflow_agrees or str(signal.get("scanner", "")).startswith("MOVERS"):
                independent_families = max(independent_families, 2)

    extra_bonus = 0.0
    if live_regime_active:
        orderflow_adj = signal_quality.orderflow_adjustment(
            direction,
            taker_buy_share=t_buy,
            oi_change_pct=signal.get("oi_change_pct"),
        )
        extra_bonus = sess["score_bonus"] + orderflow_adj

    if signal.get("backtest_approved"):
        extra_bonus += 8.0
    result = signal_quality.score_signal(
        direction, label, vol_part, independent_families, CURRENT_REGIME, extra_bonus=extra_bonus
    )
    reason = signal_quality.gate(result, ratio, live=True)
    if reason and reason.startswith("volume ratio") and ratio is not None and ratio >= MIN_STRATEGY_VOLUME_RATIO:
        if signal.get("backtest_approved") or (
            set(signal.get("strategy_names") or []) & strategy_policy_module.SMART_MONEY_STRATEGIES
        ):
            reason = None
    tier = result["tier"]
    if live and reason:
        signal["watch_only"] = True
        signal["downgrade_reason"] = reason
        details["downgraded"] = reason
        tier = "C"
    details.update({
        "quality_score": result["score"],
        "tier": tier,
        "trend_alignment": result["trend_alignment"],
        "independent_families": result["independent_families"],
        "quality_components": result["components"],
        "regime_alignment_q": result["regime_alignment"],
        "volume_ratio": None if ratio is None else round(ratio, 2),
        "trend_votes": votes,
        "passes_live_gate": reason is None,
        "session_name": sess["name"],
        "session_label": sess["label"],
        "session_quality": sess["quality"],
    })
    if t_buy is not None:
        details["taker_buy_share_pct"] = round(t_buy * 100.0, 1)
    signal["tier"] = tier
    signal["tier_risk_pct"] = signal_quality.TIER_RISK_PCT[tier] if not signal.get("watch_only") else 0.0
    signal["details"] = details
    return None


def passes_institutional_v3_gates(
    signal,
    backtest_rankings=None,
    btc_klines=None,
    coin_klines=None,
    orderbook=None,
    recent_news_items=None,
    now_utc=None,
):
    """Apply the v5.0 Dynamic Auto-Selector & Institutional Execution Gates:
      1. Macro / BTC Regime Directional Priority (`score <= -8` blocks BUY, `score >= +10` blocks SELL).
      2. US 10Y Treasury Yield Headwind Guard (`us10y_alignment == "HEADWIND"` blocked).
      3. US Macro Whipsaw Blackout Guard (35m hold after breaking FOMC / Fed / CPI / NFP shocks).
      4. Session Gate: Block `ASIA_DEAD_ZONE` (`22:00–05:00 UTC`).
      5. H4 + D1 Trend Structure Gate: Require `ALIGNED` or `PARTIAL` (or 4h-aligned `MIXED`).
      6. Spot Taker CVD Gate: `SELL` requires `taker_buy_share_pct <= 49.5%`, `BUY` requires `>= 50.5%`.
      7. Minimum Volume Expansion Gate: Require `volume_ratio >= 1.10x`.
      8. 4-Quadrant OI + Price + CVD Capitulation Guard (`BOTTOM_CAPITULATION_TRAP` / `TOP_EXHAUSTION_TRAP`).
      9. Relative Strength (RS) vs. BTC Beta Filter: Blocks shorting strong bullish decouplers (`RS >= +2.0%` with positive return).
     10. Live L2 Orderbook Depth & Whale Wall Filter: Blocks trades into opposing whale walls.
     11. Historical Vectorized Backtester Auto-Selector Gate: Only allows strategies proven profitable on previous candles (`approved == True`).
    Returns `(True, None)` if approved, or `(False, reason)` if rejected.
    """
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    direction = str(signal.get("direction") or ("LONG" if signal.get("action") == "BUY" else "SHORT")).upper()
    is_long = direction in {"LONG", "BUY"}
    is_wz_reversal = bool(is_long and details.get("wz_reversal_override"))

    # 1. Macro / BTC Regime Directional Priority
    if isinstance(CURRENT_REGIME, dict) and CURRENT_REGIME.get("score") is not None:
        r_score = float(CURRENT_REGIME["score"])
        if is_long and (not is_wz_reversal) and r_score <= BEARISH_REGIME_NO_BUY_SCORE:
            return False, f"counter-regime BUY blocked in bearish market ({r_score:+.1f})"
        if (not is_long) and r_score >= BULLISH_REGIME_NO_SELL_SCORE:
            return False, f"counter-regime SELL blocked in bullish market ({r_score:+.1f})"
        if market_regime.yield_blocks_signal(CURRENT_REGIME, is_long):
            return False, "US 10Y Treasury Yield headwind moving against signal direction"

    if details.get("us10y_alignment") == "HEADWIND":
        return False, "US 10Y Treasury Yield headwind (us10y_alignment=HEADWIND)"

    # 2. US Macro Whipsaw Blackout Guard (FOMC / CPI / NFP 35-minute shock window)
    if recent_news_items:
        macro_chk = smart_money.check_macro_whipsaw_blackout(recent_news_items, now_utc=now_utc)
        if macro_chk.get("blackout"):
            return False, macro_chk["reason"]

    # 3. Session Gate (Asia Dead-Zone blocked)
    sess_name = details.get("session_name") or signal_quality.session_info()["name"]
    if sess_name == "ASIA_DEAD_ZONE":
        return False, "Asia Dead-Zone low-liquidity window (22:00–05:00 UTC)"

    # 4. H4 + D1 Trend Structure Gate (verified WZ Oversold V-bottom LONGs exempt from lagging H4+D1 lock)
    trend_align = details.get("trend_alignment")
    trend_votes = details.get("trend_votes") if isinstance(details.get("trend_votes"), dict) else {}
    expected_vote = 1 if is_long else -1
    if (not is_wz_reversal) and trend_align not in {"ALIGNED", "PARTIAL"}:
        if not (trend_align in {"MIXED", "NEUTRAL"} and trend_votes.get("4h") == expected_vote):
            return False, f"H4+D1 trend structure is {trend_align} (requires ALIGNED or PARTIAL)"

    # 5. Spot Taker CVD Alignment Gate
    t_buy_pct = details.get("taker_buy_share_pct")
    t_buy_frac = None
    if t_buy_pct is not None:
        t_val = float(t_buy_pct)
        t_buy_frac = t_val / 100.0
        if not is_long and t_val > SELL_MAX_TAKER_BUY_PCT:
            return False, f"opposing spot buyer absorption ({t_val:.1f}% taker buy > {SELL_MAX_TAKER_BUY_PCT}%)"
        if is_long and t_val < BUY_MIN_TAKER_BUY_PCT:
            return False, f"opposing spot seller pressure ({t_val:.1f}% taker buy < {BUY_MIN_TAKER_BUY_PCT}%)"

    # 6. Minimum Volume Expansion Gate
    vol_ratio = details.get("volume_ratio")
    if vol_ratio is not None and float(vol_ratio) < MIN_STRATEGY_VOLUME_RATIO:
        return False, f"dead volume ({float(vol_ratio):.2f}x < {MIN_STRATEGY_VOLUME_RATIO}x minimum)"

    # 6b. Choppy / Sideways Market Filter (ADX >= 20.0 & Directional DI Alignment)
    # Validated on 58 live trades: ADX < 20 had -$4.73 net loss vs ADX >= 20 with 81.8% WR (+$37.37).
    adx_raw = details.get("adx")
    if adx_raw is not None:
        try:
            adx_num = float(adx_raw)
            if adx_num < 20.0:
                return False, f"choppy/ranging market (ADX {adx_num:.1f} < 20.0 minimum)"
        except (TypeError, ValueError):
            pass
    p_di_raw, m_di_raw = details.get("plus_di"), details.get("minus_di")
    if p_di_raw is not None and m_di_raw is not None:
        try:
            p_di_num, m_di_num = float(p_di_raw), float(m_di_raw)
            if (not is_long) and p_di_num >= m_di_num:
                return False, f"opposing bullish DI momentum on SHORT (+DI {p_di_num:.1f} >= -DI {m_di_num:.1f})"
            if is_long and (not is_wz_reversal) and m_di_num >= p_di_num:
                return False, f"opposing bearish DI momentum on LONG (-DI {m_di_num:.1f} >= +DI {p_di_num:.1f})"
        except (TypeError, ValueError):
            pass

    # 7. 4-Quadrant Open Interest (OI) + Price + CVD Capitulation Guard
    oi_chg = signal.get("oi_change_pct") if signal.get("oi_change_pct") is not None else details.get("oi_change_pct")
    funding_pct = signal.get("funding_rate_pct") if signal.get("funding_rate_pct") is not None else details.get("funding_rate_pct")
    p_chg_4b = details.get("price_change_4b_pct")
    if p_chg_4b is None and coin_klines and len(coin_klines.get("close") or []) >= 6:
        closes = coin_klines["close"]
        c_now = float(closes[-2])
        c_ref = float(closes[-6])
        p_chg_4b = ((c_now - c_ref) / c_ref * 100.0) if c_ref > 0 else 0.0
    if oi_chg is not None and p_chg_4b is not None:
        quad = smart_money.classify_oi_price_cvd_quadrant(
            float(p_chg_4b), float(oi_chg), t_buy_frac,
            funding_rate_pct=funding_pct, rsi_val=details.get("rsi_14"),
        )
        details["oi_quadrant"] = quad["quadrant"]
        if (not is_long) and quad["blocks_short"]:
            return False, quad["reason"] or f"OI+CVD quadrant {quad['quadrant']} blocks SHORT"
        if is_long and quad["blocks_long"]:
            return False, quad["reason"] or f"OI+CVD quadrant {quad['quadrant']} blocks LONG"

    # 7b. Free Institutional Liquidation Magnet Telemetry & Extreme-Trap Guard (NO analysis paralysis)
    if coin_klines:
        liq_mag = smart_money.estimate_liquidation_magnets(
            coin_klines, oi_change_pct=oi_chg, funding_rate_pct=funding_pct
        )
        if liq_mag.get("short_liq_above") is not None:
            details.setdefault("liq_short_above", liq_mag["short_liq_above"])
            details.setdefault("liq_short_dist_pct", liq_mag["short_liq_dist_pct"])
            details.setdefault("liq_long_below", liq_mag["long_liq_below"])
            details.setdefault("liq_long_dist_pct", liq_mag["long_liq_dist_pct"])
            details.setdefault("liq_magnet_bias", liq_mag["magnet_bias"])
            details.setdefault("liq_short_share_pct", liq_mag["short_liq_share_pct"])
            details.setdefault("liq_long_share_pct", liq_mag["long_liq_share_pct"])
        if (not is_long) and liq_mag.get("extreme_trap_short"):
            return False, liq_mag["reason"]
        if is_long and liq_mag.get("extreme_trap_long"):
            return False, liq_mag["reason"]

    # 8. Relative Strength (RS) vs. BTC Beta Filter
    if coin_klines and btc_klines and str(signal.get("symbol", "")).upper() != "BTCUSDT":
        rs_info = smart_money.relative_strength_vs_btc(coin_klines, btc_klines, lookback=4)
        if rs_info.get("rs_vs_btc_pct") is not None:
            details["rs_vs_btc_pct"] = rs_info["rs_vs_btc_pct"]
            details["rs_status"] = rs_info["status"]
            if (not is_long) and rs_info["blocks_short"]:
                return False, rs_info["reason"]
            if is_long and rs_info["blocks_long"]:
                return False, rs_info["reason"]

    # 9. Live L2 Orderbook Depth & Whale Wall Filter
    if orderbook and signal.get("price"):
        ob_info = smart_money.analyze_l2_orderbook_wall(
            orderbook,
            float(signal["price"]),
            direction,
            tp1_price=signal.get("take_profit_1"),
        )
        if ob_info.get("bid_ask_ratio") is not None:
            details["orderbook_bid_ask_ratio"] = ob_info["bid_ask_ratio"]
            details["orderbook_bid_usd"] = ob_info["bid_usd"]
            details["orderbook_ask_usd"] = ob_info["ask_usd"]
        if not ob_info["ok"]:
            return False, ob_info["reason"]
        # Anti-Bottom-Chase Exhaustion Guard: When ADX >= 45 or directional DI >= 44, block if L2 book has opposing buyer/seller absorption
        ba_ratio = ob_info.get("bid_ask_ratio")
        adx_val = float(details.get("adx") or 0.0)
        minus_di_val = float(details.get("minus_di") or 0.0)
        plus_di_val = float(details.get("plus_di") or 0.0)
        if ba_ratio is not None:
            if (not is_long) and (adx_val >= 45.0 or minus_di_val >= 44.0) and float(ba_ratio) >= 1.15:
                return False, f"exhausted dump (ADX={adx_val:.1f}, -DI={minus_di_val:.1f}) into L2 buyer absorption ({float(ba_ratio):.2f}x bids/asks)"
            if is_long and (adx_val >= 45.0 or plus_di_val >= 44.0) and float(ba_ratio) <= 0.85:
                return False, f"exhausted rally (ADX={adx_val:.1f}, +DI={plus_di_val:.1f}) into L2 seller absorption ({float(ba_ratio):.2f}x bids/asks)" 

    # 10. Historical Vectorized Backtester Auto-Selector Gate
    if backtest_rankings:
        sname = signal.get("strategy_name") or ""
        member_names = [n for n in (signal.get("strategy_names") or [sname]) if n]
        approved_stats = []
        pruned_stats = []
        for name in member_names:
            stat = backtest_rankings.get(name)
            if isinstance(stat, dict) and stat.get("trades", 0) > 0:
                if stat.get("approved", False):
                    approved_stats.append(stat)
                elif stat.get("trades", 0) >= backtester.MIN_TRADES_FOR_PRUNING:
                    pruned_stats.append(stat)
        if pruned_stats and not approved_stats:
            st0 = pruned_stats[0]
            return False, (
                f"kicked out by historical candle backtester ({st0.get('strategy')}: "
                f"PF={st0.get('profit_factor')}, WR={st0.get('win_rate_pct')}%, Net=${st0.get('net_pnl_usd'):+.2f})"
            )
        if approved_stats:
            best_st = max(approved_stats, key=lambda s: float(s.get("profit_factor", 0.0)))
            details["backtest_pf"] = best_st.get("profit_factor")
            details["backtest_wr_pct"] = best_st.get("win_rate_pct")
            details["backtest_trades"] = best_st.get("trades")
            details["backtest_net_usd"] = best_st.get("net_pnl_usd")

    return True, None


# ----------------------------- BINANCE DATA ---------------------------------

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


def is_leveraged_token(base):
    for suffix in LEVERAGED_SUFFIXES:
        if base.endswith(suffix) and len(base) - len(suffix) >= 3:
            stem = base[: -len(suffix)]
            if stem in LEVERAGED_STEMS:
                return True
    return False


LEVERAGED_STEMS = {"BTC", "ETH", "BNB", "XRP", "ADA", "LINK", "DOT", "LTC", "EOS", "TRX", "XLM", "UNI", "SUSHI",
                   "AAVE", "1INCH", "FIL", "SXP", "YFI", "BCH", "XTZ", "DOGE", "SOL", "AVAX", "MATIC"}


def get_usdt_universe():
    """Return list of active USDT spot symbols ranked by 24h quote volume,
    excluding stablecoins, leveraged tokens, non-ASCII tickers, synthetic stock
    tokens (`*BUSDT`), and meme tokens."""
    resp = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    exchange_info = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", timeout=REQUEST_TIMEOUT).json()
    tradable = {
        s["symbol"] for s in exchange_info["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == QUOTE_ASSET and s["isSpotTradingAllowed"]
    }

    quality_symbols = get_quality_symbols(QUALITY_TOP_N_MARKET_CAP)
    if quality_symbols is None:
        print("Quality filter unavailable this run -- proceeding without it (fundamentals check skipped).")

    rows = []
    fallback_rows = []
    counts = {"tradable_usdt": 0, "excluded": 0, "not_in_quality_list": 0, "illiquid": 0}
    for row in data:
        symbol = row["symbol"]
        if symbol not in tradable:
            continue
        if not symbol.endswith(QUOTE_ASSET):
            continue
        counts["tradable_usdt"] += 1
        base = symbol[: -len(QUOTE_ASSET)]
        if not base.isascii():
            counts["excluded"] += 1
            continue
        if base in EXCLUDED_BASE_ASSETS or base in MANUAL_MEME_BLACKLIST:
            counts["excluded"] += 1
            continue
        if len(base) >= 4 and base.endswith("B") and base not in {"ARB", "BNB", "TRB", "CKB", "OKB"}:
            # Exclude synthetic equity/index mirrors like NVDAB, TSLAB, SPCXB, EWYB, MSTRB, CRCLB, SKHYB, KORUB
            counts["excluded"] += 1
            continue
        if is_leveraged_token(base):
            counts["excluded"] += 1
            continue
        try:
            quote_vol = float(row["quoteVolume"])
        except (KeyError, ValueError):
            continue
        if quote_vol < MIN_24H_QUOTE_VOLUME_USD:
            counts["illiquid"] += 1
            continue
        if quality_symbols is not None and base not in quality_symbols:
            counts["not_in_quality_list"] += 1
            fallback_rows.append((symbol, quote_vol))
            continue
        rows.append((symbol, quote_vol))

    rows.sort(key=lambda r: r[1], reverse=True)
    if len(rows) < TOP_N_BY_MARKET_VOLUME and fallback_rows:
        fallback_rows.sort(key=lambda r: r[1], reverse=True)
        seen = {s for s, _ in rows}
        for sym, qv in fallback_rows:
            if sym not in seen:
                rows.append((sym, qv))
                seen.add(sym)
                if len(rows) >= TOP_N_BY_MARKET_VOLUME:
                    break

    universe = [s for s, _ in rows[:TOP_N_BY_MARKET_VOLUME]]
    print(f"[universe] {counts['tradable_usdt']} tradable USDT pairs -> {counts['excluded']} stablecoin/leveraged/meme, "
          f"{counts['not_in_quality_list']} outside the market-cap allow-list, {counts['illiquid']} below "
          f"${MIN_24H_QUOTE_VOLUME_USD:,.0f} 24h volume -> scanning {len(universe)} coins.")
    return universe


def select_top_institutional_universe(universe, limit=LIVE_SCAN_TOP_N):
    """Restrict the live scan universe to the Top `limit` (30) high-liquidity
    institutional crypto pairs, prioritizing `TOP_30_INSTITUTIONAL_BASES`."""
    preferred = [s for s in universe if s[:-4] in TOP_30_INSTITUTIONAL_BASES]
    others = [s for s in universe if s[:-4] not in TOP_30_INSTITUTIONAL_BASES]
    combined = (preferred + others)[:limit]
    return combined


def get_klines(symbol, interval, limit=KLINE_LOOKBACK, start_time=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_time is not None:
        params["startTime"] = int(start_time)
    resp = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        return None
    raw = resp.json()
    if not isinstance(raw, list) or len(raw) < (2 if start_time is not None else 30):
        return None
    return {
        "open_time": [int(c[0]) for c in raw],
        "open": [float(c[1]) for c in raw],
        "high": [float(c[2]) for c in raw],
        "low": [float(c[3]) for c in raw],
        "close": [float(c[4]) for c in raw],
        "volume": [float(c[5]) for c in raw],
        "quote_volume": [float(c[7]) for c in raw],
        "taker_buy_quote_volume": [float(c[10]) for c in raw],
    }


def get_orderbook_depth(symbol, limit=100):
    """Fetch live Binance Spot L2 orderbook (`bids` and `asks`) for Whale Wall & Bid/Ask depth checks."""
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            return None
        return data
    except Exception:
        return None


TRACKER_RECOVERY_MAX_PAGES = 7


def get_tracker_klines(symbol, interval, limit=100, start_time=None):
    if start_time is None:
        return get_klines(symbol, interval, limit)
    combined = {key: [] for key in ("open_time", "open", "high", "low", "close")}
    cursor = int(start_time)
    complete = False
    for page in range(TRACKER_RECOVERY_MAX_PAGES):
        batch = get_klines(symbol, interval, 1000, start_time=cursor)
        if not batch:
            break
        for key in combined:
            combined[key].extend(batch.get(key, []))
        times = batch.get("open_time", [])
        if not times:
            break
        cursor = times[-1] + 60_000
        if len(times) < 1000 or times[-1] >= int(time.time() * 1000) - 60_000:
            complete = True
            break
        if page + 1 < TRACKER_RECOVERY_MAX_PAGES:
            time.sleep(SLEEP_BETWEEN_CALLS)
    if not combined["open_time"]:
        return None
    combined["truncated"] = not complete
    return combined


# ------------------------ FUTURES DATA (OKX -> Bitget -> Bybit -> Binance US-Safe Chain) ------------------------

def _okx_open_interest_change(symbol, period, limit=OI_LOOKBACK):
    """Fetch Futures OI % change from OKX public Rubik endpoint (works reliably on US GitHub Actions runners)."""
    if not str(symbol).endswith("USDT"):
        return None
    ccy = str(symbol)[:-4].upper()
    period_map = {"5m": "5m", "15m": "5m", "30m": "5m", "1h": "1H", "4h": "1H", "1d": "1D"}
    okx_period = period_map.get(str(period).split("/")[0], "1H")
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/rubik/stat/contracts/open-interest-volume",
            params={"ccy": ccy, "period": okx_period},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            rows = resp.json().get("data") or []
            if len(rows) >= 2:
                # OKX returns newest-first: [ts, oi_usd, vol_usd]
                span = min(max(int(limit), 2), len(rows))
                newest = float(rows[0][1])
                oldest = float(rows[span - 1][1])
                if oldest > 0:
                    return round((newest - oldest) / oldest * 100.0, 2)
    except Exception:
        pass
    return None


def _bybit_open_interest_change(symbol, period, limit=OI_LOOKBACK):
    interval_map = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}
    bybit_interval = interval_map.get(str(period).split("/")[0], "1h")
    try:
        resp = requests.get(
            f"{BYBIT_BASE}/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": bybit_interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        rows = (resp.json().get("result") or {}).get("list") or []
        if len(rows) < 2:
            return None
        newest = float(rows[0]["openInterest"])
        oldest = float(rows[-1]["openInterest"])
        if oldest <= 0:
            return None
        return round((newest - oldest) / oldest * 100.0, 2)
    except Exception:
        return None


def _okx_funding_rate(symbol):
    if not str(symbol).endswith("USDT"):
        return None
    inst_id = f"{str(symbol)[:-4].upper()}-USDT-SWAP"
    try:
        resp = requests.get(
            f"{OKX_BASE}/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            if data and data[0].get("fundingRate") not in (None, ""):
                return round(float(data[0]["fundingRate"]) * 100.0, 4)
    except Exception:
        pass
    return None


def _bitget_funding_rate(symbol):
    try:
        resp = requests.get(
            f"{BITGET_BASE}/api/v2/mix/market/current-fund-rate",
            params={"symbol": symbol, "productType": "USDT-FUTURES"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json().get("data") or []
            if data and data[0].get("fundingRate") not in (None, ""):
                return round(float(data[0]["fundingRate"]) * 100.0, 4)
    except Exception:
        pass
    return None


def _bybit_or_okx_funding_rate(symbol):
    okx_rate = _okx_funding_rate(symbol)
    if okx_rate is not None:
        return okx_rate
    bg_rate = _bitget_funding_rate(symbol)
    if bg_rate is not None:
        return bg_rate
    try:
        resp = requests.get(
            f"{BYBIT_BASE}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            items = (resp.json().get("result") or {}).get("list") or []
            if items and items[0].get("fundingRate") not in (None, ""):
                return round(float(items[0]["fundingRate"]) * 100.0, 4)
    except Exception:
        pass
    return None


def get_open_interest_change(symbol, period, limit=OI_LOOKBACK):
    """Fetch Futures Open Interest % change with US-runner-safe OKX -> Bybit -> Binance fallback chain."""
    clean_period = str(period).split("/")[0] if period else "1h"
    if FUTURES_FALLBACK_ENABLED:
        okx_oi = _okx_open_interest_change(symbol, clean_period, limit=limit)
        if okx_oi is not None:
            return okx_oi
    try:
        resp = requests.get(
            f"{FUTURES_BASE}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": clean_period, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) >= 2:
                first_oi = float(data[0]["sumOpenInterest"])
                last_oi = float(data[-1]["sumOpenInterest"])
                if first_oi != 0:
                    return round((last_oi - first_oi) / first_oi * 100, 2)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        pass
    if FUTURES_FALLBACK_ENABLED:
        return _bybit_open_interest_change(symbol, clean_period, limit=limit)
    return None


def get_funding_rate(symbol):
    """Fetch perpetual Futures Funding Rate (%) with US-runner-safe OKX -> Bitget -> Bybit -> Binance chain."""
    if FUTURES_FALLBACK_ENABLED:
        rate_fb = _bybit_or_okx_funding_rate(symbol)
        if rate_fb is not None:
            return rate_fb
    try:
        resp = requests.get(
            f"{FUTURES_BASE}/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            rate = resp.json().get("lastFundingRate")
            if rate is not None:
                return round(float(rate) * 100, 4)
    except (requests.RequestException, KeyError, ValueError, TypeError):
        pass
    return None


def enrich_with_derivatives(confidence, symbol, timeframe):
    oi_change = get_open_interest_change(symbol, timeframe) if ENABLE_OI_CONFIRMATION else None
    funding_rate = get_funding_rate(symbol) if ENABLE_FUNDING_RATE else None
    oi_confirmed = oi_change is not None and oi_change >= OI_SPIKE_THRESHOLD_PCT
    if confidence.startswith("High") and oi_confirmed:
        confidence = "Very High (Volume+OI+Whale Confirmed)"
    return confidence, oi_change, funding_rate, oi_confirmed


def check_resistance_breakout(highs, closes, idx, lookback=RESISTANCE_LOOKBACK):
    start = max(0, idx - lookback)
    if start >= idx:
        return False, None
    resistance = max(highs[start:idx])
    return closes[idx] > resistance, resistance


def check_support_breakdown(lows, closes, idx, lookback=RESISTANCE_LOOKBACK):
    start = max(0, idx - lookback)
    if start >= idx:
        return False, None
    support = min(lows[start:idx])
    return closes[idx] < support, support


def check_volume_trend(volumes, idx, candles=VOLUME_TREND_CANDLES):
    start = idx - candles + 1
    if start < 0:
        return False
    window = volumes[start:idx + 1]
    return all(window[i] < window[i + 1] for i in range(len(window) - 1))


# ----------------------------- SIGNAL LOGIC ---------------------------------

def detect_volume_spike(volumes):
    if len(volumes) < VOLUME_LOOKBACK_CANDLES + 2:
        return False, 0.0
    latest = volumes[-2]
    window = volumes[-(VOLUME_LOOKBACK_CANDLES + 2):-2]
    avg = sum(window) / len(window)
    if avg == 0:
        return False, 0.0
    ratio = latest / avg
    return ratio >= VOLUME_SPIKE_MULTIPLIER, round(ratio, 2)


def evaluate_symbol(symbol, klines, timeframe):
    closes, highs, lows, volumes = (
        klines["close"], klines["high"], klines["low"], klines["volume"]
    )

    spike, ratio = detect_volume_spike(volumes)
    if not spike:
        return None

    idx = len(closes) - 2
    rsi_vals = rsi(closes, RSI_PERIOD)
    upper, basis, lower = bollinger_bands(closes, BB_PERIOD, BB_STDDEV)
    ema_fast_vals = ema(closes, EMA_FAST)
    ema_slow_vals = ema(closes, EMA_SLOW)
    atr_vals = atr(highs, lows, closes, ATR_PERIOD)

    if None in (rsi_vals[idx], upper[idx], basis[idx], ema_fast_vals[idx],
                ema_slow_vals[idx], atr_vals[idx], rsi_vals[idx - 1]):
        return None

    price = closes[idx]
    volume_trending_up = check_volume_trend(volumes, idx)

    is_bullish_candle = closes[idx] > klines["open"][idx]
    rsi_rising_through_zone = rsi_vals[idx - 1] < RSI_BULLISH_MIN <= rsi_vals[idx] and rsi_vals[idx] < 70
    ema_bullish = ema_fast_vals[idx] > ema_slow_vals[idx]
    near_or_above_basis = price >= basis[idx]

    long_confirmed = is_bullish_candle and ema_bullish and near_or_above_basis and (
        rsi_rising_through_zone or rsi_vals[idx] >= RSI_BULLISH_MIN
    )

    is_bearish_candle = closes[idx] < klines["open"][idx]
    rsi_falling_through_zone = rsi_vals[idx - 1] > RSI_BEARISH_MAX >= rsi_vals[idx] and rsi_vals[idx] > 30
    ema_bearish = ema_fast_vals[idx] < ema_slow_vals[idx]
    near_or_below_basis = price <= basis[idx]

    short_confirmed = is_bearish_candle and ema_bearish and near_or_below_basis and (
        rsi_falling_through_zone or rsi_vals[idx] <= RSI_BEARISH_MAX
    )

    if long_confirmed:
        breakout_confirmed, level = check_resistance_breakout(highs, closes, idx)

        stop_loss = round(price - STOP_LOSS_ATR_MULTIPLIER * atr_vals[idx], 6)
        swing_low = min(lows[idx - 5:idx + 1])
        stop_loss = round(min(stop_loss, swing_low * 0.998), 6)

        risk_pct = round((price - stop_loss) / price * 100, 2)
        risk_distance = price - stop_loss
        take_profit_1 = round(price + TP1_RISK_REWARD * risk_distance, 6)
        take_profit_2 = round(price + TP2_RISK_REWARD * risk_distance, 6)

        if ratio >= 6 and rsi_rising_through_zone and (breakout_confirmed or volume_trending_up):
            confidence = "High (Volume+Whale Confirmed)"
        elif ratio >= 4:
            confidence = "Medium (Strong Volume)"
        else:
            confidence = "Watch (Volume + Technical)"

        confidence, oi_change_pct, funding_rate_pct, oi_confirmed = enrich_with_derivatives(
            confidence, symbol, timeframe
        )

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": "LONG",
            "action": "BUY",
            "price": price,
            "volume_ratio": ratio,
            "rsi": round(rsi_vals[idx], 1),
            "stop_loss": stop_loss,
            "risk_pct": risk_pct,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "confidence": confidence,
            "level_broken": breakout_confirmed,
            "level_label": "Resistance Breakout",
            "level_value": round(level, 6) if level else None,
            "volume_trending_up": volume_trending_up,
            "suitable_for": "Spot + Futures" if ratio >= 4 else "Spot",
            "oi_change_pct": oi_change_pct,
            "funding_rate_pct": funding_rate_pct,
            "oi_confirmed": oi_confirmed,
            "confluence": False,
        }

    if short_confirmed:
        breakdown_confirmed, level = check_support_breakdown(lows, closes, idx)

        stop_loss = round(price + STOP_LOSS_ATR_MULTIPLIER * atr_vals[idx], 6)
        swing_high = max(highs[idx - 5:idx + 1])
        stop_loss = round(max(stop_loss, swing_high * 1.002), 6)

        risk_pct = round((stop_loss - price) / price * 100, 2)
        risk_distance = stop_loss - price
        take_profit_1 = round(price - TP1_RISK_REWARD * risk_distance, 6)
        take_profit_2 = round(price - TP2_RISK_REWARD * risk_distance, 6)

        if ratio >= 6 and rsi_falling_through_zone and (breakdown_confirmed or volume_trending_up):
            confidence = "High (Volume+Whale Confirmed)"
        elif ratio >= 4:
            confidence = "Medium (Strong Volume)"
        else:
            confidence = "Watch (Volume + Technical)"

        confidence, oi_change_pct, funding_rate_pct, oi_confirmed = enrich_with_derivatives(
            confidence, symbol, timeframe
        )

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": "SHORT",
            "action": "SELL",
            "price": price,
            "volume_ratio": ratio,
            "rsi": round(rsi_vals[idx], 1),
            "stop_loss": stop_loss,
            "risk_pct": risk_pct,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "confidence": confidence,
            "level_broken": breakdown_confirmed,
            "level_label": "Support Breakdown",
            "level_value": round(level, 6) if level else None,
            "volume_trending_up": volume_trending_up,
            "suitable_for": "Futures only (can't short on spot)",
            "oi_change_pct": oi_change_pct,
            "funding_rate_pct": funding_rate_pct,
            "oi_confirmed": oi_confirmed,
            "confluence": False,
        }

    return None


def evaluate_whale_volume_pressure(symbol, klines, timeframe):
    quote = klines.get("quote_volume")
    taker_buy = klines.get("taker_buy_quote_volume")
    closes = klines.get("close", [])
    highs, lows = klines.get("high", []), klines.get("low", [])
    if not quote or not taker_buy or len(closes) < VOLUME_LOOKBACK_CANDLES + WHALE_PRESSURE_LOOKBACK + 3:
        return None

    idx = len(closes) - 2
    recent_start = idx - WHALE_PRESSURE_LOOKBACK + 1
    baseline_start = recent_start - VOLUME_LOOKBACK_CANDLES
    if baseline_start < 0:
        return None
    baseline = quote[baseline_start:recent_start]
    recent_quote = quote[recent_start:idx + 1]
    recent_buy = taker_buy[recent_start:idx + 1]
    baseline_avg = sum(baseline) / len(baseline) if baseline else 0
    recent_avg = sum(recent_quote) / len(recent_quote) if recent_quote else 0
    if baseline_avg <= 0 or sum(recent_quote) <= 0:
        return None
    surge = recent_avg / baseline_avg
    buy_share = sum(recent_buy) / sum(recent_quote)
    move_pct = (closes[idx] / closes[recent_start - 1] - 1) * 100 if closes[recent_start - 1] else 0

    if surge < WHALE_PRESSURE_MIN_SURGE:
        return None
    if buy_share >= WHALE_PRESSURE_BUY_SHARE and move_pct >= WHALE_PRESSURE_MIN_MOVE_PCT:
        direction = "LONG"
    elif buy_share <= WHALE_PRESSURE_SELL_SHARE and move_pct <= -WHALE_PRESSURE_MIN_MOVE_PCT:
        direction = "SHORT"
    else:
        return None

    atr_values = atr(highs, lows, closes, ATR_PERIOD)
    atr_now = atr_values[idx]
    if not atr_now:
        return None
    price = closes[idx]
    sign = 1 if direction == "LONG" else -1
    risk = max(atr_now * STOP_LOSS_ATR_MULTIPLIER, price * 0.005)
    stop = price - sign * risk
    risk_pct = risk / price * 100
    return {
        "symbol": symbol,
        "scanner": "WHALE_VOLUME_PRESSURE",
        "timeframe": timeframe,
        "direction": direction,
        "action": "BUY" if direction == "LONG" else "SELL",
        "price": price,
        "stop_loss": round(stop, 8),
        "risk_pct": round(risk_pct, 2),
        "take_profit_1": round(price + sign * risk * TP1_RISK_REWARD, 8),
        "take_profit_2": round(price + sign * risk * TP2_RISK_REWARD, 8),
        "confidence": "Spot taker-buy/sell pressure candidate",
        "details": {
            "volume_surge_vs_prior_20": round(surge, 2),
            "taker_buy_share_pct": round(buy_share * 100, 2),
            "price_move_pct": round(move_pct, 2),
            "measurement": "Binance spot taker-buy quote volume; does not identify institutions",
        },
    }


# ----------------------------- TREND MOMENTUM SIGNAL LOGIC ---------------------------------

def detect_trend_momentum_stats(klines, idx, lookback=TREND_LOOKBACK_CANDLES):
    closes, opens, volumes = klines["close"], klines["open"], klines["volume"]
    start = idx - lookback + 1
    if start - lookback < 0:
        return None

    start_price = closes[start - 1]
    end_price = closes[idx]
    if start_price == 0:
        return None
    pct_move = (end_price - start_price) / start_price * 100

    window_opens = opens[start:idx + 1]
    window_closes = closes[start:idx + 1]
    total_candles = len(window_closes)
    bullish_candles = sum(1 for o, c in zip(window_opens, window_closes) if c > o)
    bearish_candles = sum(1 for o, c in zip(window_opens, window_closes) if c < o)

    prior_volumes = volumes[start - lookback:start]
    cur_volumes = volumes[start:idx + 1]
    if not prior_volumes or sum(prior_volumes) == 0:
        return None
    sustained_volume_ratio = (sum(cur_volumes) / len(cur_volumes)) / (sum(prior_volumes) / len(prior_volumes))

    return {
        "pct_move": pct_move,
        "bullish_candles": bullish_candles,
        "bearish_candles": bearish_candles,
        "total_candles": total_candles,
        "sustained_volume_ratio": round(sustained_volume_ratio, 2),
        "window_low": min(klines["low"][start:idx + 1]),
        "window_high": max(klines["high"][start:idx + 1]),
    }


def _build_trend_signal(symbol, timeframe, klines, idx, stats, lookback, direction,
                         directional_candles, current_rsi, confidence, stage):
    closes, highs, lows = klines["close"], klines["high"], klines["low"]
    price = closes[idx]
    atr_vals = atr(highs, lows, closes, ATR_PERIOD)
    atr_now = atr_vals[idx]

    if direction == "LONG":
        action = "BUY"
        stop_loss = price - TREND_STOP_ATR_MULTIPLIER * atr_now if atr_now else stats["window_low"] * 0.99
        stop_loss = round(min(stop_loss, stats["window_low"] * 0.995), 6)
        risk_distance = price - stop_loss
        take_profit_1 = round(price + TP1_RISK_REWARD * risk_distance, 6)
        take_profit_2 = round(price + TP2_RISK_REWARD * risk_distance, 6)
    else:
        action = "SELL"
        stop_loss = price + TREND_STOP_ATR_MULTIPLIER * atr_now if atr_now else stats["window_high"] * 1.01
        stop_loss = round(max(stop_loss, stats["window_high"] * 1.005), 6)
        risk_distance = stop_loss - price
        take_profit_1 = round(price - TP1_RISK_REWARD * risk_distance, 6)
        take_profit_2 = round(price - TP2_RISK_REWARD * risk_distance, 6)
    risk_pct = round(risk_distance / price * 100, 2)

    return {
        "signal_type": "TREND_MOMENTUM",
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": direction,
        "action": action,
        "price": price,
        "pct_move": round(stats["pct_move"], 2),
        "lookback_candles": lookback,
        "directional_candles": directional_candles,
        "total_candles": stats["total_candles"],
        "sustained_volume_ratio": stats["sustained_volume_ratio"],
        "rsi": current_rsi,
        "stop_loss": stop_loss,
        "risk_pct": risk_pct,
        "take_profit_1": take_profit_1,
        "take_profit_2": take_profit_2,
        "confidence": confidence,
        "stage": stage,
        "rank_score": abs(stats["pct_move"]) * stats["sustained_volume_ratio"],
    }


def evaluate_trend_momentum(symbol, klines, timeframe):
    closes, highs, lows = klines["close"], klines["high"], klines["low"]
    idx = len(closes) - 2

    rsi_vals = rsi(closes, RSI_PERIOD)
    current_rsi = round(rsi_vals[idx], 1) if rsi_vals[idx] is not None else None

    early_stats = detect_trend_momentum_stats(klines, idx, lookback=TREND_EARLY_LOOKBACK_CANDLES)
    if early_stats is not None:
        abs_move = abs(early_stats["pct_move"])
        direction = "LONG" if early_stats["pct_move"] > 0 else "SHORT"
        directional_candles = early_stats["bullish_candles"] if direction == "LONG" else early_stats["bearish_candles"]
        directional_ratio = directional_candles / early_stats["total_candles"]
        rsi_still_early = (
            current_rsi is not None and (
                (direction == "LONG" and current_rsi < TREND_EARLY_MAX_RSI_LONG) or
                (direction == "SHORT" and current_rsi > TREND_EARLY_MIN_RSI_SHORT)
            )
        )

        if (abs_move >= TREND_EARLY_MIN_MOVE_PCT
                and directional_ratio >= TREND_EARLY_MIN_DIRECTIONAL_CANDLES_RATIO
                and early_stats["sustained_volume_ratio"] >= TREND_EARLY_MIN_SUSTAINED_VOLUME_RATIO
                and rsi_still_early):
            return _build_trend_signal(
                symbol, timeframe, klines, idx, early_stats, TREND_EARLY_LOOKBACK_CANDLES,
                direction, directional_candles, current_rsi,
                confidence="Early (Move Building)", stage="🟢 Early — move just starting",
            )

    stats = detect_trend_momentum_stats(klines, idx, lookback=TREND_LOOKBACK_CANDLES)
    if stats is None:
        return None

    abs_move = abs(stats["pct_move"])
    if abs_move < TREND_MIN_MOVE_PCT:
        return None

    direction = "LONG" if stats["pct_move"] > 0 else "SHORT"
    directional_candles = stats["bullish_candles"] if direction == "LONG" else stats["bearish_candles"]
    directional_ratio = directional_candles / stats["total_candles"]

    if directional_ratio < TREND_MIN_DIRECTIONAL_CANDLES_RATIO:
        return None

    if stats["sustained_volume_ratio"] < TREND_MIN_SUSTAINED_VOLUME_RATIO:
        return None

    is_high = (abs_move >= TREND_HIGH_MOVE_PCT
               and directional_ratio >= TREND_HIGH_DIRECTIONAL_RATIO
               and stats["sustained_volume_ratio"] >= TREND_HIGH_VOLUME_RATIO)
    base_confidence = "High (Strong Sustained Trend)" if is_high else "Medium (Trend Building)"

    exhausted = current_rsi is not None and (
        (direction == "LONG" and current_rsi >= TREND_LATE_STAGE_RSI_LONG) or
        (direction == "SHORT" and current_rsi <= TREND_LATE_STAGE_RSI_SHORT)
    )
    if exhausted:
        confidence = f"{base_confidence} -- ⚠️ Late-Stage"
        stage = "🔴 Late-Stage — move likely already extended, chasing risk"
    else:
        confidence = base_confidence
        stage = "🟡 Confirmed — full move already happened"

    return _build_trend_signal(
        symbol, timeframe, klines, idx, stats, TREND_LOOKBACK_CANDLES,
        direction, directional_candles, current_rsi, confidence, stage,
    )


# ----------------------------- NEWS CONTEXT (read-only) ---------------------------------

def load_news_context_for_symbol(symbol):
    if not NEWS_CONTEXT_ENABLED or not os.path.exists(NEWS_CONTEXT_FILE):
        return None
    try:
        with open(NEWS_CONTEXT_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (ValueError, OSError):
        return None

    base = symbol.replace("USDT", "")
    cutoff = time.time() - NEWS_CONTEXT_LOOKBACK_HOURS * 3600
    matches = [e for e in entries if e.get("coin") == base and e.get("timestamp", 0) >= cutoff]
    if not matches:
        return None
    return max(matches, key=lambda e: e["timestamp"])


def news_alignment_note(direction, impact):
    if impact == "Bullish" and direction == "LONG":
        return "✅ aligns with signal"
    if impact == "Bearish" and direction == "SHORT":
        return "✅ aligns with signal"
    if impact == "Mixed":
        return "ℹ️ mixed/unclear"
    return "⚠️ conflicts with signal direction"


# ----------------------------- STATE / COOLDOWN ---------------------------------

def load_recent_signals():
    if not os.path.exists(STATE_FILE):
        return {}
    out = {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 2:
                out[parts[0]] = float(parts[1])
    return out


def save_recent_signals(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        for symbol, ts in state.items():
            f.write(f"{symbol},{ts}\n")


def prune_and_check(state, symbol):
    return prune_and_check_with_hours(state, symbol, SIGNAL_COOLDOWN_HOURS)


def _cooldown_hours_for_key(key):
    if "|TREND|" in key:
        return TREND_SIGNAL_COOLDOWN_HOURS
    if "|WHALE_FLOW|" in key:
        return 2.9
    if "|WHALE_VOLUME_PRESSURE|" in key:
        return 3.0
    return SIGNAL_COOLDOWN_HOURS


def prune_and_check_with_hours(state, key, cooldown_hours):
    now = time.time()
    for k in list(state.keys()):
        if now - state[k] > _cooldown_hours_for_key(k) * 3600:
            del state[k]
    if key in state:
        return False
    state[key] = now
    return True


def _trade_group_key(signal):
    symbol = str(signal.get("symbol", "")).upper()
    scanner = str(signal.get("scanner", "")).upper()
    direction = str(signal.get("direction") or ("LONG" if signal.get("action") == "BUY" else "SHORT")).upper()
    if scanner in {"WHALE_VOLUME", "WHALE_VOLUME_PRESSURE"}:
        family = "WHALE_VOLUME"
    elif scanner in {"WHALE", "WHALE_FLOW"}:
        family = "WHALE_FLOW"
    elif scanner == "TREND":
        family = "TREND"
    elif scanner == "STRATEGY":
        member_ids = signal.get("strategy_ids") or []
        if not member_ids:
            details = signal.get("details", {})
            member_ids = [details.get("item_id")] if details.get("item_id") is not None else []
        if not member_ids:
            member_ids = signal.get("strategy_names") or [signal.get("strategy_name", "unknown")]
        family = ("STRATEGY", tuple(sorted(str(item) for item in member_ids)))
    else:
        family = scanner or "UNKNOWN"
    return family, symbol, direction


# ----------------------------- DISCORD ---------------------------------

def _tp_label(signal, which):
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    usd = details.get("tp1_usd" if which == 1 else "tp2_usd")
    if usd is not None:
        suffix = " · SL → entry after TP1" if which == 1 else ""
        return f"Take Profit {which} (≈ ${usd:g} on $100){suffix}"
    return "Take Profit 1 (2R)" if which == 1 else "Take Profit 2 (3.5R)"


def _inject_regime(payload):
    regime = CURRENT_REGIME
    if not regime or regime.get("score") is None:
        return payload
    for embed in payload.get("embeds", []):
        title = str(embed.get("title", ""))
        if title.startswith(("📍", "📰", "🌍", "📋")):
            continue
        fields = embed.setdefault("fields", [])
        if any(str(f.get("name", "")).startswith("BTC / Market Regime") for f in fields):
            continue
        if title.startswith("📊"):
            fields.append({"name": "BTC / Market Regime", "value": market_regime.short_text(regime), "inline": True})
            if regime.get("us10y"):
                fields.append({"name": "US 10Y Yield", "value": market_regime.yield_text(regime), "inline": True})
            continue
        direction = next((f.get("value") for f in fields if f.get("name") == "Direction"), None)
        is_long = market_regime.direction_is_long(direction) if direction else None
        fields.append({"name": "BTC / Market Regime", "value": market_regime.short_text(regime, is_long),
                       "inline": False})
        if regime.get("us10y"):
            fields.append({"name": "US 10Y Yield", "value": market_regime.yield_text(regime, is_long),
                           "inline": False})
    return payload


def _post_discord(payload):
    payload = _inject_regime(payload)
    response = None
    for attempt in range(6):
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 429 or attempt == 5:
            return response
        try:
            delay = float(response.json().get("retry_after", 1.0))
        except (ValueError, TypeError, AttributeError):
            delay = 1.0
        time.sleep(min(max(delay, 0.25), 5.0))
    return response


def send_discord_signal(signal):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set, skipping send. Signal:", signal)
        return

    base = signal["symbol"].replace("USDT", "")
    is_short = signal["direction"] == "SHORT"
    emoji = "🔻" if is_short else "🐋"
    label = "Whale Volume — Distribution" if is_short else "Whale Volume — Volume Spike"
    level_value_str = f"${signal['level_value']:.6g}" if signal["level_value"] else "n/a"
    oi_str = f"{signal['oi_change_pct']:+.2f}%" if signal.get("oi_change_pct") is not None else "n/a"
    funding_str = f"{signal['funding_rate_pct']:+.4f}%" if signal.get("funding_rate_pct") is not None else "n/a"

    fields = [
        {"name": "Direction", "value": signal["action"], "inline": True},
        {"name": "Timeframe", "value": signal["timeframe"], "inline": True},
        {"name": "Price", "value": f"${signal['price']:.6g}", "inline": True},
        {"name": "Volume vs Avg", "value": f"{signal['volume_ratio']}x", "inline": True},
        {"name": "RSI", "value": str(signal["rsi"]), "inline": True},
        {"name": "Stop Loss", "value": f"${signal['stop_loss']:.6g} ({signal['risk_pct']}% risk)", "inline": True},
        {"name": _tp_label(signal, 1), "value": f"${signal['take_profit_1']:.6g}", "inline": True},
        {"name": _tp_label(signal, 2), "value": f"${signal['take_profit_2']:.6g}", "inline": True},
        {"name": signal["level_label"], "value": (f"Yes ({level_value_str})" if signal["level_broken"] else "No"), "inline": True},
        {"name": "Volume Trend", "value": ("Rising 4 candles straight" if signal["volume_trending_up"] else "Not sustained"), "inline": True},
        {"name": "Open Interest Δ", "value": f"{oi_str}{' (confirms whale positioning)' if signal.get('oi_confirmed') else ''}", "inline": True},
        {"name": "Funding Rate", "value": funding_str, "inline": True},
        {"name": "Suitable For", "value": signal["suitable_for"], "inline": True},
        {"name": "Confidence", "value": signal["confidence"], "inline": True},
    ]
    if signal.get("confluence"):
        fields.append({"name": "Multi-Timeframe", "value": "Confirmed on 2+ timeframes", "inline": True})

    news = signal.get("recent_news")
    if news:
        note = news_alignment_note(signal["direction"], news["impact"])
        news_value = f"[{news['category']}] {news['title'][:110]} — {note}"
        fields.append({"name": f"📰 Recent News ({news['impact']})", "value": news_value, "inline": False})

    embed = {
        "title": f"{emoji} {label}: {base}/USDT",
        "color": 15158332 if is_short else 3066993,
        "fields": fields,
        "footer": {"text": "DeepPocket Tracker — not financial advice, DYOR"},
    }
    payload = {"embeds": [embed]}
    try:
        resp = _post_discord(payload)
        if resp.status_code >= 300:
            print(f"Discord send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Discord send error: {e}", file=sys.stderr)


def send_discord_trend_signal(signal):
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set, skipping send. Signal:", signal)
        return

    base = signal["symbol"].replace("USDT", "")
    is_short = signal["direction"] == "SHORT"
    is_late_stage = "Late-Stage" in signal["confidence"]
    if is_late_stage:
        emoji = "⚠️"
    else:
        emoji = "📉" if is_short else "📈"
    label = "Trend Momentum (SHORT)" if is_short else "Trend Momentum (LONG)"

    fields = [
        {"name": "Stage", "value": signal.get("stage", "n/a"), "inline": False},
        {"name": "Direction", "value": signal["action"], "inline": True},
        {"name": "Timeframe", "value": signal["timeframe"], "inline": True},
        {"name": "Price", "value": f"${signal['price']:.6g}", "inline": True},
        {"name": f"Move ({signal['lookback_candles']} candles)", "value": f"{signal['pct_move']:+.2f}%", "inline": True},
        {"name": "Candles Aligned", "value": f"{signal['directional_candles']}/{signal['total_candles']}", "inline": True},
        {"name": "Sustained Volume", "value": f"{signal['sustained_volume_ratio']}x vs prior window", "inline": True},
        {"name": "RSI", "value": str(signal["rsi"]) if signal["rsi"] is not None else "n/a", "inline": True},
        {"name": "Stop Loss", "value": f"${signal['stop_loss']:.6g} ({signal['risk_pct']}% risk)", "inline": True},
        {"name": _tp_label(signal, 1), "value": f"${signal['take_profit_1']:.6g}", "inline": True},
        {"name": _tp_label(signal, 2), "value": f"${signal['take_profit_2']:.6g}", "inline": True},
        {"name": "Confidence", "value": signal["confidence"], "inline": True},
    ]

    news = signal.get("recent_news")
    if news:
        note = news_alignment_note(signal["direction"], news["impact"])
        news_value = f"[{news['category']}] {news['title'][:110]} — {note}"
        fields.append({"name": f"📰 Recent News ({news['impact']})", "value": news_value, "inline": False})

    embed = {
        "title": f"{emoji} {label}: {base}/USDT",
        "color": 15158332 if is_late_stage else (15105570 if is_short else 15844367),
        "fields": fields,
        "footer": {"text": "DeepPocket Tracker — typical historical tendency, not guaranteed, confirm on chart before acting"},
    }
    payload = {"embeds": [embed]}
    try:
        resp = _post_discord(payload)
        if resp.status_code >= 300:
            print(f"Discord send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Discord send error: {e}", file=sys.stderr)


def send_scan_summary(scanned, long_count, short_count, total_found, trend_long_count=0, trend_short_count=0,
                      trend_total_found=0, pressure_total_found=0, flow_total_found=0, strategy_total_found=0,
                      strategy_candidates=0, strategy_conflicts=0, strategy_cooldowns=0,
                      volume_long_count=0, volume_short_count=0, flow_long_count=0, flow_short_count=0,
                      strategy_long_count=0, strategy_short_count=0, recovered_hit_events=0,
                      strategy_watch_count=0, strategy_policy_filtered=0, movers_total_found=0,
                      backtest_rankings=None):
    if not DISCORD_WEBHOOK_URL:
        print(f"No DISCORD_WEBHOOK_URL set, skipping scan summary. "
              f"Scanned={scanned} Long={long_count} Short={short_count}")
        return

    whale_volume_total = total_found + pressure_total_found
    grand_total = (whale_volume_total + flow_total_found + trend_total_found + strategy_total_found
                   + movers_total_found)
    sess = signal_quality.session_info()
    if grand_total == 0:
        description = "Scan complete. Top-30 institutional pairs checked — none met the Smart-Money / Tier A-B sniper criteria this run."
    else:
        description = (f"Scan complete. {grand_total} high-conviction sniper signal(s) sent & tracked this run "
                        f"(Whale Flow: {flow_total_found}, Smart-Money & Strategies: {strategy_total_found}, "
                        f"Movers: {movers_total_found}).")
    if recovered_hit_events:
        description += (f" Restored {recovered_hit_events} earlier TP/SL event(s) from saved positions; "
                        "the Sheet was updated without repeating old individual alerts.")

    
       # ✅ NAYA CLEAN CODE (Sirf kaam ki cheezein dikhayega, fazool 0/0 boxes khatam):
    summary_fields = [
        {
            "name": "Universe & Timeframes",
            "value": f"Top {scanned} Liquid Pairs ({', '.join(SCAN_TIMEFRAMES)})",
            "inline": True,
        },
        {"name": "Market Session", "value": sess["label"], "inline": True},
        {
            "name": "Live Signals Sent",
            "value": f"{grand_total} ({long_count} LONG / {short_count} SHORT)",
            "inline": True,
        },
        {
            "name": "Raw Triggers Checked",
            "value": str(strategy_candidates),
            "inline": True,
        },
        {
            "name": "Filtered (Unprofitable / Risk Gates)",
            "value": str(strategy_policy_filtered),
            "inline": True,
        },
    ]
    if backtest_rankings:
        active_str, disabled_str = backtester.format_discord_league_table(backtest_rankings)
        summary_fields.append({
            "name": "✅ Active Live Strategies (Profitable on Previous Candles)",
            "value": active_str,
            "inline": False,
        })
        summary_fields.append({
            "name": "🛑 Kicked Out Strategies (Unprofitable on Previous Candles)",
            "value": disabled_str,
            "inline": False,
        })

    embed = {
        "title": "📊 DeepPocket Tracker — Scan Report & Backtest Auto-Selector",
        "color": 3447003,
        "description": description,
        "fields": summary_fields,
        "footer": {
            "text": "DeepPocket Tracker v5.0 — Dynamic 13-Strategy Historical Candle Auto-Selector"
        },
    }
    payload = {"embeds": [embed]}
    try:
        resp = _post_discord(payload)
        if resp.status_code >= 300:
            print(f"Discord scan-summary send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Discord scan-summary send error: {e}", file=sys.stderr)


def send_discord_strategy_signal(signal):
    """Distinct alert for Smart-Money, Strategy, Whale Flow, and Movers setups."""
    details = signal.get("details", {}) if isinstance(signal.get("details"), dict) else {}
    tier = signal.get("tier") or details.get("tier")
    if ONLY_ALERT_TIER_AB and tier == "C":
        print(f"[tier-filter] Suppressing Discord alert for Tier C setup (paper-tracked only): "
              f"{signal.get('symbol')} {signal.get('scanner')} (score {details.get('quality_score', 'n/a')})")
        return
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set, skipping strategy alert:", signal)
        return
    is_long = signal.get("direction") == "LONG"
    scanner = signal.get("scanner", "STRATEGY")
    watch_only = bool(signal.get("watch_only"))
    factor_names = {
        "WHALE": "WHALE FLOW · ON-CHAIN DEX",
        "WHALE_FLOW": "WHALE FLOW · ON-CHAIN DEX",
        "WHALE_VOLUME_PRESSURE": "WHALE VOLUME · BUY/SELL PRESSURE",
        "STRATEGY": f"STRATEGY · {signal.get('strategy_name', 'Winner rule')}",
        "MOVERS_PULLBACK": "MOVERS · PULLBACK (Gainers BUY / Losers SELL)",
        "MOVERS_ORDERBLOCK": "MOVERS · ORDER BLOCK (Gainers BUY / Losers SELL)",
    }
    factor = factor_names.get(scanner, scanner.replace("_", " "))
    is_flow = scanner in {"WHALE", "WHALE_FLOW"}
    fields = [
        {"name": "Factor", "value": factor, "inline": True},
        {"name": "Direction", "value": signal.get("action", "HOLD"), "inline": True},
        {"name": "Timeframe", "value": signal.get("timeframe", "n/a"), "inline": True},
        {"name": "Entry Price", "value": f"${signal['price']:.8g}", "inline": True},
        {"name": "Hard Stop Loss", "value": f"${signal['stop_loss']:.8g}", "inline": True},
        {"name": _tp_label(signal, 1), "value": f"${signal['take_profit_1']:.8g}", "inline": True},
        {"name": _tp_label(signal, 2), "value": f"${signal['take_profit_2']:.8g}", "inline": True},
        {"name": "Trigger / Orderflow Reason", "value": signal.get("confidence", "n/a"), "inline": False},
    ]
    if details.get("session_label"):
        fields.append({"name": "Market Session", "value": str(details["session_label"]), "inline": True})
    if details.get("taker_buy_share_pct") is not None:
        tb_pct = float(details["taker_buy_share_pct"])
        cvd_side = f"{100 - tb_pct:.1f}% Aggressive Sellers" if not is_long else f"{tb_pct:.1f}% Aggressive Buyers"
        fields.append({"name": "Spot CVD Orderflow", "value": cvd_side, "inline": True})
    if details.get("backtest_pf") is not None:
        fields.append({
            "name": "Historical Candle Backtest",
            "value": f"PF {details['backtest_pf']:.2f} · Win Rate {details.get('backtest_wr_pct', 0):.1f}% ({details.get('backtest_trades', 0)} setups)",
            "inline": False,
        })
    if isinstance(details, dict) and details.get("quality_score") is not None:
        families = details.get("independent_families", "n/a")
        fields.append({"name": "Quality Score",
                       "value": (f"{details['quality_score']:.0f}/100 · H4+D1 trend {details.get('trend_alignment', 'n/a')} · "
                                 f"{families} independent evidence famil{'y' if families == 1 else 'ies'}"),
                       "inline": False})
    if tier:
        label = {"A": "🥇 TIER A · LIVE SNIPER (Full 1% Risk)", "B": "🥈 TIER B · LIVE SNIPER (Half 0.5% Risk)",
                 "C": "🥉 TIER C · track only, no trade"}[tier]
        fields.insert(0, {"name": "Tier & Execution Status", "value": label, "inline": False})
    grade_risk = signal_quality.TIER_RISK_PCT.get(tier, 0.0) if tier else 0.0
    if grade_risk:
        size = risk_manager.suggested_position_usd(signal.get("price"), signal.get("stop_loss"), risk_pct=grade_risk)
        if size:
            fields.append({"name": f"Position Size ({grade_risk:g}% Risk)",
                           "value": (f"${size:,.2f} notional on ${risk_manager.ACCOUNT_EQUITY_USD:,.0f} account "
                                     f"(max loss ${risk_manager.ACCOUNT_EQUITY_USD * grade_risk / 100.0:.2f} if Stop Loss hits; "
                                     f"book 50% at TP1 and move SL to Entry)"),
                           "inline": False})
    if is_flow and details:
        evidence = (f"Pool: {details.get('pool', 'n/a')} · {details.get('network', 'n/a')}\n"
                    f"Buy ${details.get('buy_volume_usd', 0):,.0f} / Sell ${details.get('sell_volume_usd', 0):,.0f} · "
                    f"Net {details.get('net_buy_sell_imbalance_pct', 0):+.1f}% · {details.get('trade_count', 0)} swaps\n"
                    f"Liquidity ${details.get('pool_liquidity_usd', 0):,.0f}")
        fields.append({"name": "Flow Evidence", "value": evidence[:400], "inline": False})
    elif details and scanner in {"MOVERS_PULLBACK", "MOVERS_ORDERBLOCK"}:
        evidence = (f"24h {details.get('change_24h_pct', 'n/a')}% · vol ${details.get('volume_24h_usd', 0):,.0f}\n"
                    f"Risk {details.get('risk_pct', 'n/a')}% (≈ ${details.get('sl_usd', 'n/a')} on $100) · "
                    f"TP1 ≈ ${details.get('tp1_usd', 'n/a')} / TP2 ≈ ${details.get('tp2_usd', 'n/a')}\n"
                    f"{details.get('tp1_action', 'close 50% at TP1, move SL to entry')}")
        fields.append({"name": "Movers Evidence", "value": evidence[:400], "inline": False})
        if not is_long:
            fields.append({"name": "Note", "value": "Futures/margin only (SELL = short)", "inline": False})

    if details:
        prop_parts = []
        if details.get("taker_buy_share_pct") is not None:
            prop_parts.append(f"Spot Taker Buy: {details['taker_buy_share_pct']:.1f}%")
        oi_val = signal.get("oi_change_pct") if signal.get("oi_change_pct") is not None else details.get("oi_change_pct")
        fr_val = signal.get("funding_rate_pct") if signal.get("funding_rate_pct") is not None else details.get("funding_rate_pct")
        if oi_val is not None:
            prop_parts.append(f"Futures OI: {float(oi_val):+.2f}%")
        if fr_val is not None:
            prop_parts.append(f"Funding: {float(fr_val):+.4f}%")
        if details.get("oi_quadrant") and details.get("oi_quadrant") != "SPOT_CVD_ONLY":
            prop_parts.append(f"OI Matrix: {details['oi_quadrant']}")
        if details.get("rs_vs_btc_pct") is not None:
            prop_parts.append(f"RS vs BTC: {details['rs_vs_btc_pct']:+.2f}% ({details.get('rs_status', 'INLINE')})")
        if details.get("orderbook_bid_ask_ratio") is not None:
            prop_parts.append(f"L2 Bid/Ask: {details['orderbook_bid_ask_ratio']:.2f}x")
        if details.get("fvg_present") and details.get("fvg_midpoint"):
            prop_parts.append(f"FVG Mid: ${details['fvg_midpoint']:.6g}")
        if prop_parts:
            fields.append({"name": "🏛️ Prop-Desk Orderflow, OI & L2 Depth", "value": " · ".join(prop_parts), "inline": False})

        if details.get("liq_short_above") is not None and details.get("liq_long_below") is not None:
            liq_text = (
                f"Short Liq Above: **${float(details['liq_short_above']):.6g}** ({float(details.get('liq_short_dist_pct', 0)):+.1f}%) | "
                f"Long Liq Below: **${float(details['liq_long_below']):.6g}** ({float(details.get('liq_long_dist_pct', 0)):+.1f}%)\n"
                f"Bias: **{details.get('liq_magnet_bias', 'BALANCED')}** "
                f"({float(details.get('liq_short_share_pct', 50)):.0f}% Shorts Above / {float(details.get('liq_long_share_pct', 50)):.0f}% Longs Below)"
            )
            fields.append({"name": "🧲 Liquidation Magnet Clusters (Estimated)", "value": liq_text, "inline": False})

        if details.get("wz_vpf") is not None and details.get("wz_drz") is not None:
            wz_text = (
                f"VPF: **{details['wz_vpf']}** ({details.get('wz_vpf_zone', 'n/a')}) · "
                f"DRZ: **{details['wz_drz']:+.2f}** · "
                f"IBL: **${float(details.get('wz_ibl', 0)):.6g}** · "
                f"LML: **${float(details.get('wz_lml', 0)):.6g}**"
            )
            fields.append({"name": "📐 WZ Mathematical Decision System", "value": wz_text, "inline": False})

    news = signal.get("recent_news")
    if news:
        note = news_alignment_note(signal["direction"], news["impact"])
        news_value = f"[{news['category']}] {news['title'][:110]} — {note}"
        fields.append({"name": f"📰 Recent News ({news['impact']})", "value": news_value, "inline": False})

    payload = {"embeds": [{
        "title": f"{'🐋 ' if is_flow else '🎯 '}{factor}: {signal['symbol']} — {'BUY (LONG)' if is_long else 'SELL (SHORT)'}",
        "color": 3066993 if is_long else 15158332,
        "fields": fields,
        "footer": {"text": "DeepPocket Tracker v6.0 · WZ Math + Liquidation Magnet + Live OI/Funding"},
    }]}
    try:
        resp = _post_discord(payload)
        if resp.status_code >= 300:
            print(f"Discord strategy send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Discord strategy send error: {e}", file=sys.stderr)


def send_discord_position_update(event):
    if not DISCORD_WEBHOOK_URL:
        return
    if ONLY_ALERT_TIER_AB and str(event.get("tier") or "") == "C":
        return
    source = event.get("scanner", "Signal")
    outcome = event.get("outcome", "POSITION_UPDATE")
    hit_price = event.get("exit_price") or "n/a"
    embed = {
        "title": f"📍 {source}: {event.get('symbol', 'coin')} — {outcome}",
        "color": 3066993 if "TP" in outcome else (15158332 if "SL" in outcome else 15105570),
        "fields": [
            {"name": "Factor / Source", "value": source, "inline": True},
            {"name": "Direction", "value": event.get("action", "n/a"), "inline": True},
            {"name": "Hit Price", "value": f"${float(hit_price):.8g}" if isinstance(hit_price, (int, float)) else str(hit_price), "inline": True},
            {"name": "Entry", "value": f"${float(event.get('price') or 0):.8g}", "inline": True},
            {"name": "TP1 / TP2", "value": f"${float(event.get('take_profit_1') or 0):.8g} / ${float(event.get('take_profit_2') or 0):.8g}", "inline": True},
            {"name": "Stop Loss", "value": f"${float(event.get('stop_loss') or 0):.8g}", "inline": True},
        ],
        "footer": {"text": "Price touched based on Binance candles; not exchange fills"},
    }
    if outcome == "TP1_HIT":
        entry_price = float(event.get("price") or 0)
        embed["fields"] = [f for f in embed["fields"] if f["name"] != "Stop Loss"]
        embed["fields"].append({"name": "New Stop Loss (Break Even)",
                                "value": f"${entry_price:.8g} (= entry)", "inline": True})
        embed["fields"].append({
            "name": "Action",
            "value": (f"TP1 hit. Move the remaining position Stop Loss UP to entry ${entry_price:.8g} "
                      "(Break Even)." + (" Close 50% at TP1 if you are trading this setup."
                                         if str(event.get("scanner", "")).startswith("MOVERS") else "")),
            "inline": False,
        })
    elif outcome == "BE_HIT":
        embed["fields"].append({
            "name": "Position result",
            "value": "Break-even / Ratchet Stop Loss hit after TP1; 50% TP1 profit is locked in.",
            "inline": False,
        })
    try:
        response = _post_discord({"embeds": [embed]})
        if response.status_code >= 300:
            print(f"Discord TP/SL update failed ({response.status_code}): {response.text}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"Discord TP/SL update error: {exc}", file=sys.stderr)


# ----------------------------- MAIN ---------------------------------

def consolidate_strategy_candidates(strategy_candidates, profiles):
    profiles_by_id = {profile["item_id"]: profile for profile in profiles}
    profiles_by_id[9001] = {"item_id": 9001, "name": "Smart Money Liquidity Sweep", "rank": 1}
    profiles_by_id[9002] = {"item_id": 9002, "name": "Smart Money CVD Breakdown", "rank": 2}
    profiles_by_id[9003] = {"item_id": 9003, "name": "Smart Money EMA20 Pullback", "rank": 3}
    raw_count = sum(len(items) for items in strategy_candidates.values())
    by_direction = {"LONG": [], "SHORT": []}
    conflict_count = 0
    for item_id, candidates in strategy_candidates.items():
        directions = {candidate["direction"] for candidate in candidates}
        if len(directions) > 1:
            conflict_count += 1
            continue
        if directions:
            direction = next(iter(directions))
            by_direction[direction].extend(candidates)

    selected_groups = []
    for direction, candidates in by_direction.items():
        if not candidates:
            continue
        if CONSENSUS_SAME_TIMEFRAME:
            by_tf = {}
            for candidate in candidates:
                by_tf.setdefault(candidate["timeframe"], []).append(candidate)
            best_tf = max(by_tf, key=lambda tf: (len({c["strategy_id"] for c in by_tf[tf]}), tf == "4h"))
            candidates = by_tf[best_tf]
        agreeing_ids = sorted({candidate["strategy_id"] for candidate in candidates},
                              key=lambda item_id: profiles_by_id.get(item_id, {}).get("rank", 9999))
        names = [profiles_by_id.get(item_id, {"name": str(item_id)})["name"] for item_id in agreeing_ids]
        selected = max(candidates, key=lambda item: (item["timeframe"] == "4h",
                                                       -profiles_by_id.get(item["strategy_id"], {}).get("rank", 9999)))
        selected["strategy_ids"] = agreeing_ids
        selected["strategy_names"] = names
        selected["strategy_agreement_count"] = len(agreeing_ids)
        selected["strategy_name"] = ("Consensus: " + ", ".join(names)) if len(names) > 1 else names[0]
        selected_groups.append(selected)
    return selected_groups, raw_count, conflict_count


def main():
    from strategy_scanner import catalog as strategy_catalog, evaluate_all as evaluate_strategies
    import strategy_policy as strategy_policy_module
    from strategy_policy import classify_strategy_group
    from dex_onchain import get_onchain_dex_flows
    from trade_tracker import (acknowledge_sheet_rows, append_local, journal_row, load_open,
                               load_or_create_discord_cutover, pending_sheet_rows, queue_sheet_rows,
                               register, settle_open, silence_pre_cutover_positions,
                               apply_tp_policy)
    from google_sheets import (append_rows as append_google_rows,
                               initialize_fresh_dashboard, merge_sheet_position_state)

    synced_signal_ids = set()

    breaker_message = risk_manager.breaker_reason()
    if breaker_message:
        print(f"[risk] circuit breaker active: {breaker_message}")

    def live_risk_block(signal):
        if signal.get("watch_only"):
            return None
        return breaker_message or risk_manager.check_new(signal, load_open())

    def register_for_sync(signal):
        if signal.get("price") and signal.get("stop_loss"):
            is_l = str(signal.get("direction") or signal.get("action") or "LONG").upper() in {"LONG", "BUY"}
            entry_p = float(signal["price"])
            raw_sl = float(signal["stop_loss"])
            if entry_p > 0 and raw_sl > 0:
                risk_pct = abs(entry_p - raw_sl) / entry_p * 100.0
                if risk_pct > smart_money.MAX_STOP_PCT:
                    # Cap maximum stop-loss distance at 2.25% ($2.25 on $100 trade) so 1 SL never exceeds 1 TP2 (+$2.45)
                    raw_sl = round(
                        entry_p * (1.0 - smart_money.MAX_STOP_PCT / 100.0)
                        if is_l
                        else entry_p * (1.0 + smart_money.MAX_STOP_PCT / 100.0),
                        8,
                    )
                    signal["stop_loss"] = raw_sl
                    signal["risk_pct"] = smart_money.MAX_STOP_PCT
            s_tp1, s_tp2, _ = smart_money.compute_sniper_targets(
                entry_p, raw_sl, is_long=is_l
            )
            signal["take_profit_1"] = s_tp1
            signal["take_profit_2"] = s_tp2
        apply_tp_policy(signal)
        market_regime.annotate_details(signal, CURRENT_REGIME)
        if signal.get("scanner") == "STRATEGY" and not signal.get("watch_only") and signal.get("tier") in {"A", "B"}:
            signal["tier_risk_pct"] = signal_quality.TIER_RISK_PCT[signal["tier"]]
        row = register(signal)
        try:
            daily_board.record(signal)
        except OSError as exc:
            print(f"[board] could not update the daily board: {exc}", file=sys.stderr)
        journal_rows.append(row)
        locked_trade_groups.add(_trade_group_key(signal))

    print("DeepPocket Tracker v3.0: starting Smart-Money & Vectorized Backtester scan...")
    try:
        full_universe = get_usdt_universe()
        universe = select_top_institutional_universe(full_universe, limit=LIVE_SCAN_TOP_N)
        print(f"[universe-v3] Restricted live scan to Top {len(universe)} high-liquidity pairs: "
              f"{', '.join(universe[:12])} ...")
    except requests.RequestException as e:
        print(f"Failed to fetch symbol universe: {e}", file=sys.stderr)
        sys.exit(1)

    global CURRENT_REGIME
    try:
        CURRENT_REGIME = market_regime.get_regime()
    except Exception as exc:
        print(f"[regime] unavailable this run: {exc}", file=sys.stderr)
        CURRENT_REGIME = None
    if CURRENT_REGIME and CURRENT_REGIME.get("score") is not None:
        print(f"[regime] BTC / Market Regime: {CURRENT_REGIME['label']} ({CURRENT_REGIME['score']:+.0f}), "
              f"coverage {int(CURRENT_REGIME.get('coverage', 0) * 100)}%")
        if DISCORD_WEBHOOK_URL and market_regime.report_due(CURRENT_REGIME):
            try:
                resp = _post_discord(market_regime.build_report_embed(CURRENT_REGIME))
                if resp.status_code < 300:
                    market_regime.mark_reported(CURRENT_REGIME)
                else:
                    print(f"[regime] Discord report failed ({resp.status_code})", file=sys.stderr)
            except requests.RequestException as exc:
                print(f"[regime] Discord report error: {exc}", file=sys.stderr)

    btc_bearish_bias = None
    if isinstance(CURRENT_REGIME, dict) and CURRENT_REGIME.get("score") is not None:
        r_score = float(CURRENT_REGIME["score"])
        if r_score <= BEARISH_REGIME_NO_BUY_SCORE or CURRENT_REGIME.get("label") == "BEARISH":
            btc_bearish_bias = True
            print(f"[regime-priority] Market Regime is BEARISH ({r_score:+.1f}) -> Prioritizing SHORT setups & blocking BUY traps.")
        elif r_score >= BULLISH_REGIME_NO_SELL_SCORE or CURRENT_REGIME.get("label") == "BULLISH":
            btc_bearish_bias = False
            print(f"[regime-priority] Market Regime is BULLISH ({r_score:+.1f}) -> Prioritizing LONG setups & blocking SHORT traps.")

    print(f"Scanning Top {len(universe)} coins across {SCAN_TIMEFRAMES} timeframes...")
    try:
        fresh_start = initialize_fresh_dashboard()
        if fresh_start:
            with open(STATE_FILE, "w", encoding="utf-8"):
                pass
            print("[google-sheets] cleared old paper-test rows; starting a fresh forward-test dashboard.")
    except Exception as exc:
        print(f"[google-sheets] fresh-start reset will retry on sync: {exc}", file=sys.stderr)
    state = load_recent_signals()
    found = 0
    long_count = 0
    short_count = 0
    trend_found = 0
    trend_long_count = 0
    trend_short_count = 0
    strategy_count = 0
    strategy_watch_count = 0
    strategy_policy_filtered = 0
    strategy_candidate_count = 0
    strategy_conflict_count = 0
    strategy_cooldown_count = 0
    discord_cutover_ts = load_or_create_discord_cutover()
    try:
        recovered_positions = merge_sheet_position_state()
        if recovered_positions:
            print(f"[tracker] recovered {recovered_positions} open position(s) from Google Sheets.")
    except Exception as exc:
        print(f"[google-sheets] could not recover open positions; local tracker state will continue: {exc}",
              file=sys.stderr)
    silence_pre_cutover_positions(discord_cutover_ts)
    journal_rows = settle_open(get_tracker_klines)
    position_events = [row for row in journal_rows if row.get("outcome")]
    append_local(position_events)
    reportable_position_events = [row for row in position_events
                                  if not row.get("suppress_discord_updates")]
    queue_sheet_rows(reportable_position_events)
    pending_rows = pending_sheet_rows()
    try:
        reportable_journal_rows = [row for row in journal_rows
                                   if not row.get("suppress_discord_updates")]
        if append_google_rows(pending_rows + reportable_journal_rows):
            acknowledge_sheet_rows(pending_rows)
            synced_signal_ids.update(str(row.get("event_id")) for row in reportable_journal_rows
                                     if row.get("event_id"))
        else:
            print("[google-sheets] tracker updates queued for retry; continuing scan.")
    except Exception as exc:
        print(f"[google-sheets] tracker updates queued for retry: {exc}", file=sys.stderr)

    recovered_hit_events = 0
    for position_event in position_events:
        if position_event.get("cooldown_key") and position_event.get("outcome") in {
            "TP1_HIT", "TP2_HIT", "SL_HIT", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"
        }:
            state.pop(position_event["cooldown_key"], None)
        if position_event.get("outcome") and position_event.get("recovered_from_sheet"):
            recovered_hit_events += 1
            continue
        if position_event.get("outcome") and not position_event.get("suppress_discord_updates"):
            send_discord_position_update(position_event)

    locked_trade_groups = {
        _trade_group_key(trade) for trade in load_open()
        if not trade.get("tp1_hit") and not trade.get("suppress_discord_updates")
    }
    pressure_found = 0
    flow_found = 0
    volume_long_count = 0
    volume_short_count = 0
    flow_long_count = 0
    flow_short_count = 0
    strategy_long_count = 0
    strategy_short_count = 0
    if ONCHAIN_DEX_LIVE_ENABLED:
        try:
            market_flow_signals = get_onchain_dex_flows(universe)
            print(f"On-chain DEX: fresh flow signals for {len(market_flow_signals)} supported coins.")
        except Exception as exc:
            print(f"[onchain-dex] provider request failed: {exc}")
            market_flow_signals = {}
    else:
        market_flow_signals = {}
        print("[sniper-v5] On-chain DEX Whale Flow disabled; only Historical-Backtest-Approved strategies active.")

    historical_klines_dataset = {}
    raw_coin_candidates = []
    for symbol in universe:
        strategy_candidates = {}
        for timeframe in SCAN_TIMEFRAMES:
            try:
                klines = get_klines(symbol, timeframe)
                if klines is None:
                    continue
                historical_klines_dataset[(symbol, timeframe)] = klines

                # 1. Wall Street Smart-Money Liquidity Sweeps & CVD Breakdowns
                for sm_sig in smart_money.evaluate_latest(
                    symbol, klines, timeframe, btc_bearish_bias=btc_bearish_bias
                ):
                    strategy_candidates.setdefault(sm_sig["strategy_id"], []).append(sm_sig)

                # 2. Existing Technical Strategies
                for strategy_signal in evaluate_strategies(symbol, klines, timeframe):
                    strategy_candidates.setdefault(strategy_signal["strategy_id"], []).append(strategy_signal)

            except requests.RequestException as e:
                print(f"Error on {symbol} ({timeframe}): {e}", file=sys.stderr)
            time.sleep(SLEEP_BETWEEN_CALLS)
        raw_coin_candidates.append((symbol, strategy_candidates))

    # ---- Run Fast Vectorized Historical Candle Backtester on Fetched OHLCV Data ----
    backtest_rankings = {}
    if historical_klines_dataset:
        try:
            backtest_rankings = backtester.backtest_klines_dataset(
                historical_klines_dataset, btc_bearish_bias=btc_bearish_bias
            )
            backtester.save_rankings(backtest_rankings)
            backtester.write_markdown_report(backtest_rankings)
            try:
                from google_sheets import sync_historical_backtest_table
                sync_historical_backtest_table(backtest_rankings)
            except Exception as gs_exc:
                print(f"[google-sheets] backtest table sync warning: {gs_exc}", file=sys.stderr)
            print("[backtester] Historical Candle Backtest Rankings (last 120 closed 1h/4h bars across Top-30 universe):")
            for sname, st in sorted(
                backtest_rankings.items(),
                key=lambda kv: (kv[1].get("approved", False), kv[1].get("profit_factor", 0.0)),
                reverse=True,
            ):
                print(f"  - {sname:30s}: trades={st['trades']:3d} | WR={st['win_rate_pct']:5.1f}% | "
                      f"SL={st['sl_rate_pct']:5.1f}% | PF={st['profit_factor']:4.2f} | "
                      f"MaxDD={st['max_drawdown_pct']:4.1f}% | {st['verdict']}")
        except Exception as exc:
            print(f"[backtester] historical check warning: {exc}", file=sys.stderr)

    strategy_pool = []
    strategy_quality_filtered = 0
    rejection_reasons = {}

    def record_rejection(reason_str):
        key = reason_str.split("(")[0].strip()
        rejection_reasons[key] = rejection_reasons.get(key, 0) + 1

    for symbol, strategy_candidates in raw_coin_candidates:
        selected_groups, candidate_count, conflicts = consolidate_strategy_candidates(
            strategy_candidates, strategy_catalog())
        strategy_candidate_count += candidate_count
        strategy_conflict_count += conflicts
        for selected in selected_groups:
            policy = strategy_policy_module.classify_strategy_with_backtest(
                selected.get("strategy_names", []), selected["direction"], backtest_rankings
            )
            if policy is None:
                strategy_policy_filtered += 1
                record_rejection("unprofitable on historical candle backtest")
                continue
            selected["watch_only"] = policy == "WATCH_ONLY"
            selected["backtest_approved"] = policy == "ACTIVE"
            family = quality_gate.family_of(selected.get("strategy_names", []))
            reason = quality_gate.check(selected, family, CURRENT_REGIME)
            if reason:
                strategy_quality_filtered += 1
                record_rejection(reason)
                print(f"[quality] {symbol} {selected['direction']} {', '.join(selected['strategy_names'])} skipped: {reason}")
                continue
            families = strategy_policy_module.independent_family_count(
                selected.get("strategy_names", []), backtest_rankings=backtest_rankings
            )
            if selected.get("oi_change_pct") is None:
                try:
                    _, oi_chg, fund_pct, _ = enrich_with_derivatives(
                        selected.get("confidence", ""), symbol, selected.get("timeframe", "1h")
                    )
                    selected["oi_change_pct"] = oi_chg
                    selected["funding_rate_pct"] = fund_pct
                    if isinstance(selected.get("details"), dict):
                        if oi_chg is not None:
                            selected["details"]["oi_change_pct"] = oi_chg
                        if fund_pct is not None:
                            selected["details"]["funding_rate_pct"] = fund_pct
                except Exception:
                    pass
            assess_quality(selected, symbol, selected["direction"], families)
            tf_tok = str(selected.get("timeframe", "1h")).split("/")[0] if selected.get("timeframe") else "1h"
            coin_kl = historical_klines_dataset.get((symbol, tf_tok)) or historical_klines_dataset.get((symbol, "1h"))
            btc_kl = historical_klines_dataset.get(("BTCUSDT", tf_tok)) or historical_klines_dataset.get(("BTCUSDT", "1h"))
            ob_depth = get_orderbook_depth(symbol)
            all_news = []
            try:
                if os.path.exists(RECENT_NEWS_FILE):
                    with open(RECENT_NEWS_FILE, "r", encoding="utf-8") as nf:
                        _loaded = json.load(nf)
                        all_news = _loaded if isinstance(_loaded, list) else []
            except Exception:
                all_news = []
            ok_v3, v3_reason = passes_institutional_v3_gates(
                selected,
                backtest_rankings=backtest_rankings,
                btc_klines=btc_kl,
                coin_klines=coin_kl,
                orderbook=ob_depth,
                recent_news_items=all_news,
            )
            if not ok_v3:
                strategy_quality_filtered += 1
                record_rejection(v3_reason)
                print(f"[sniper-v3] {symbol} {selected['direction']} {', '.join(selected['strategy_names'])} rejected: {v3_reason}")
                continue
            if ONLY_ALERT_TIER_AB and selected.get("tier") == "C":
                strategy_quality_filtered += 1
                record_rejection("Tier C score < 50")
                print(f"[sniper-gate] {symbol} {selected['direction']} {', '.join(selected['strategy_names'])} "
                      f"held back (Tier C score {selected['details'].get('quality_score', 'n/a')} < 50)")
                continue
            selected["recent_news"] = load_news_context_for_symbol(symbol)
            selected["scanner"] = "STRATEGY"
            ids_key = ",".join(str(item) for item in selected.get("strategy_ids", []))
            selected["cooldown_key"] = f"{symbol}|STRATEGY|{selected['direction']}|{ids_key}"
            selected["_family"] = "WATCH" if selected.get("tier") == "C" else (family or "CONSENSUS")
            strategy_pool.append(selected)

        journal_rows.append(journal_row({"symbol": symbol, "scanner": "WHALE_VOLUME", "action": "HOLD", "details": {}}, status="SCAN"))
        journal_rows.append(journal_row({"symbol": symbol, "scanner": "TREND", "action": "HOLD", "details": {}}, status="SCAN"))

        flow = market_flow_signals.get(symbol)
        if flow:
            direction = flow["direction"]
            flow_klines = historical_klines_dataset.get((symbol, "1h")) or get_klines(symbol, "1h")
            levels = atr_trade_levels(flow_klines, direction)
            if levels:
                price = levels["price"]
                onchain_signal = {"symbol": symbol, "scanner": "WHALE_FLOW", "timeframe": "3h on-chain DEX",
                    "direction": direction, "action": "BUY" if direction == "LONG" else "SELL",
                    "price": price, "stop_loss": levels["stop_loss"],
                    "take_profit_1": levels["take_profit_1"], "take_profit_2": levels["take_profit_2"],
                    "confidence": "On-chain DEX buy/sell flow (pool-based confirmation)",
                    "details": dict(flow, atr_stop="1.7x ATR(14) 1h"),
                    "watch_only": strategy_policy_module.WHALE_FLOW_MODE != "ACTIVE",
                    "recent_news": load_news_context_for_symbol(symbol),
                    "cooldown_key": f"{symbol}|WHALE_FLOW|{direction}"}
                guard_reason = quality_gate.check(onchain_signal, "WHALE_FLOW", CURRENT_REGIME)
                if not guard_reason:
                    assess_quality(onchain_signal, symbol, direction, 1,
                                   flow_imbalance_pct=flow.get("net_buy_sell_imbalance_pct"))
                    ok_v3, v3_reason = passes_institutional_v3_gates(onchain_signal, backtest_rankings=None)
                    if not ok_v3:
                        guard_reason = v3_reason
                    elif ONLY_ALERT_TIER_AB and onchain_signal.get("tier") == "C":
                        guard_reason = f"Tier C score {onchain_signal['details'].get('quality_score', 'n/a')} < 50"
                if not guard_reason:
                    guard_reason = live_risk_block(onchain_signal)
                if guard_reason:
                    strategy_quality_filtered += 1
                    record_rejection(guard_reason)
                    print(f"[quality] {symbol} Whale Flow {direction} skipped: {guard_reason}")
                elif (_trade_group_key(onchain_signal) not in locked_trade_groups
                        and prune_and_check_with_hours(state, onchain_signal["cooldown_key"], 2.9)):
                    register_for_sync(onchain_signal)
                    send_discord_strategy_signal(onchain_signal)
                    flow_found += 1
                    if direction == "SHORT":
                        flow_short_count += 1
                        short_count += 1
                    else:
                        flow_long_count += 1
                        long_count += 1
        elif not flow:
            journal_rows.append(journal_row({"symbol": symbol, "scanner": "WHALE_FLOW", "action": "HOLD",
                "timeframe": "3h on-chain DEX", "details": {"status": "no fresh supported liquid-pool trigger"}}, status="SCAN"))

    # ---- Strategy & Smart-Money signals: best first, capped per scan and by open positions ----
    strategy_pool.sort(key=quality_gate.rank_key)
    sent_per_family = {}
    for selected in strategy_pool:
        family = selected.pop("_family")
        risk_reason = live_risk_block(selected)
        if risk_reason:
            strategy_quality_filtered += 1
            record_rejection(risk_reason)
            print(f"[risk] {selected['symbol']} {selected['direction']} skipped: {risk_reason}")
            continue
        open_family = [t for t in load_open() if quality_gate.family_of_trade(t) == family]
        if (sent_per_family.get(family, 0) >= min(2, quality_gate.MAX_NEW_PER_SCAN.get(family, 2))
                or len(open_family) >= quality_gate.MAX_OPEN.get(family, 6)):
            strategy_quality_filtered += 1
            record_rejection("per-scan or open-position cap")
            print(f"[quality] {selected['symbol']} {selected['direction']} {family} skipped: "
                  f"per-scan or open-position cap reached.")
            continue
        coin_dir_cd_key = f"{selected['symbol']}|COIN_DIR|{selected['direction']}"
        if (_trade_group_key(selected) not in locked_trade_groups
                and prune_and_check_with_hours(state, coin_dir_cd_key, 4)
                and prune_and_check_with_hours(state, selected["cooldown_key"], 6)):
            print(f"[SIGNAL SENT] {selected['symbol']} | Bias: {selected['direction']} | "
                  f"Trigger: {selected['strategy_name']} ({selected.get('confidence')}) | "
                  f"Entry: ${selected['price']:.6g} | Hard SL: ${selected['stop_loss']:.6g} | "
                  f"TP1: ${selected['take_profit_1']:.6g} | TP2: ${selected['take_profit_2']:.6g}")
            register_for_sync(selected)
            send_discord_strategy_signal(selected)
            sent_per_family[family] = sent_per_family.get(family, 0) + 1
            strategy_count += 1
            if selected.get("watch_only"):
                strategy_watch_count += 1
            if selected["direction"] == "SHORT":
                strategy_short_count += 1
                short_count += 1
            else:
                strategy_long_count += 1
                long_count += 1
        else:
            strategy_cooldown_count += 1

    if rejection_reasons:
        summary_str = ", ".join(f"{k}: {v}" for k, v in sorted(rejection_reasons.items(), key=lambda x: -x[1]))
        print(f"[diagnostics] Filter rejection breakdown this run: {summary_str}")

    # ---- Top Gainers / Top Losers movers strategies (separate signals) ----
    movers_sent = {"MOVERS_PULLBACK": 0, "MOVERS_ORDERBLOCK": 0}
    movers_skipped_caps = 0
    movers_skipped_regime = 0
    if movers_strategy.MOVERS_ENABLED and MOVERS_LIVE_SCAN_ENABLED:
        try:
            movers_signals = movers_strategy.scan_movers(
                universe, BINANCE_BASE, get_klines, sleep_seconds=SLEEP_BETWEEN_CALLS)
        except Exception as exc:
            print(f"[movers] scan failed; other strategies are unaffected: {exc}", file=sys.stderr)
            movers_signals = []
        movers_signals.sort(key=lambda sig: (sig["details"].get("quality") == "High",
                                             -sig.get("risk_pct", 99)), reverse=True)
        for sig in movers_signals:
            open_now = [t for t in load_open() if t.get("scanner") == sig["scanner"]]
            same_side = [t for t in open_now if t.get("direction") == sig["direction"]]
            if (len(open_now) >= movers_strategy.MAX_OPEN_PER_STRATEGY
                    or len(same_side) >= movers_strategy.MAX_OPEN_SAME_SIDE):
                movers_skipped_caps += 1
                print(f"[movers] {sig['symbol']} {sig['scanner']} skipped: open-position cap reached.")
                continue
            if _trade_group_key(sig) in locked_trade_groups:
                continue
            regime_guard = quality_gate.check(sig, "CONSENSUS", CURRENT_REGIME)
            if regime_guard and "regime" in regime_guard.lower():
                movers_skipped_regime += 1
                print(f"[movers] {sig['symbol']} {sig['direction']} skipped: {regime_guard}")
                continue
            assess_quality(sig, sig["symbol"], sig["direction"], 1)
            ok_v3, v3_reason = passes_institutional_v3_gates(sig, backtest_rankings=None)
            if not ok_v3:
                movers_skipped_caps += 1
                print(f"[sniper-v3] {sig['symbol']} {sig['scanner']} rejected: {v3_reason}")
                continue
            if ONLY_ALERT_TIER_AB and sig.get("tier") == "C":
                movers_skipped_caps += 1
                print(f"[sniper-gate] {sig['symbol']} {sig['scanner']} held back "
                      f"(Tier C score {sig['details'].get('quality_score', 'n/a')} < 50)")
                continue
            sig["recent_news"] = load_news_context_for_symbol(sig["symbol"])
            risk_reason = live_risk_block(sig)
            if risk_reason:
                movers_skipped_caps += 1
                print(f"[risk] {sig['symbol']} {sig['scanner']} skipped: {risk_reason}")
                continue
            if market_regime.yield_blocks_signal(CURRENT_REGIME, sig["direction"] == "LONG"):
                movers_skipped_regime += 1
                info = CURRENT_REGIME["us10y"]
                print(f"[movers] {sig['symbol']} {sig['direction']} skipped: US10Y moving against it "
                      f"({info['level']:.2f}%, {info['day_bps']:+.0f} bps 1d, {info['week_bps']:+.0f} bps 5d).")
                continue
            if market_regime.blocks_signal(CURRENT_REGIME, sig["direction"] == "LONG"):
                movers_skipped_regime += 1
                print(f"[movers] {sig['symbol']} {sig['direction']} skipped: against the BTC backdrop "
                      f"({CURRENT_REGIME['label']} {CURRENT_REGIME['score']:+.0f}).")
                continue
            if not prune_and_check_with_hours(state, sig["cooldown_key"], SIGNAL_COOLDOWN_HOURS):
                continue
            print(f"[MOVERS SIGNAL SENT] {sig['symbol']} | Bias: {sig['direction']} | "
                  f"Trigger: {sig.get('confidence')} | Entry: ${sig['price']:.6g} | "
                  f"Hard SL: ${sig['stop_loss']:.6g} | TP1: ${sig['take_profit_1']:.6g} | TP2: ${sig['take_profit_2']:.6g}")
            register_for_sync(sig)
            send_discord_strategy_signal(sig)
            movers_sent[sig["scanner"]] += 1
            if sig["direction"] == "SHORT":
                short_count += 1
            else:
                long_count += 1
        print(f"Movers: {movers_sent['MOVERS_PULLBACK']} pullback + {movers_sent['MOVERS_ORDERBLOCK']} "
              f"order-block signal(s) sent; {movers_skipped_caps} skipped by position/sniper caps, "
              f"{movers_skipped_regime} by the BTC regime gate.")

    save_recent_signals(state)
    append_local(journal_rows)
    try:
        unsynced_events = [row for row in journal_rows
                           if row.get("event_id")
                           and not row.get("suppress_discord_updates")
                           and str(row.get("event_id")) not in synced_signal_ids
                           and (row.get("status") == "SIGNAL" or row.get("outcome"))]
        queue_sheet_rows(unsynced_events)
        pending_rows = pending_sheet_rows()
        end_rows = pending_rows + [row for row in journal_rows
                                   if row.get("status") == "MARK"
                                   and not row.get("suppress_discord_updates")
                                   and str(row.get("event_id", "")) not in synced_signal_ids]
        sheets_synced = append_google_rows(end_rows)
        if sheets_synced is False:
            print("Google Sheets sync: skipped; scanner continues with local journal.")
        else:
            acknowledge_sheet_rows(pending_rows)
            synced_signal_ids.update(str(row.get("event_id")) for row in end_rows if row.get("event_id"))
            print("Google Sheets sync: complete.")
    except Exception as exc:
        print(f"[google-sheets] sync failed; scan data is saved locally: {exc}", file=sys.stderr)
    try:
        board_state = daily_board.load()
        slot = daily_board.due_slot(board_state)
        if slot is not None and DISCORD_WEBHOOK_URL:
            resp = _post_discord(daily_board.build_embed(board_state, slot))
            if resp.status_code < 300:
                daily_board.mark_posted(board_state, slot)
            else:
                print(f"[board] Discord post failed ({resp.status_code})", file=sys.stderr)
    except Exception as exc:
        print(f"[board] skipped: {exc}", file=sys.stderr)
    print(f"Scan complete. Total signals: {long_count + short_count} ({long_count} LONG, {short_count} SHORT). "
          f"Whale Flow: {flow_found}. Strategy & Smart-Money: {strategy_count} sent; "
          f"{strategy_candidate_count} timeframe trigger(s), {strategy_conflict_count} direction conflict(s), "
          f"{strategy_cooldown_count} cooldown-filtered, {strategy_policy_filtered} policy-filtered, "
          f"{strategy_quality_filtered} sniper/backtester-filtered.")

    if ALWAYS_SEND_SCAN_SUMMARY:
        send_scan_summary(len(universe), long_count, short_count, found,
                           trend_long_count, trend_short_count, trend_found,
                           pressure_found, flow_found, strategy_count,
                           strategy_candidate_count, strategy_conflict_count, strategy_cooldown_count,
                           volume_long_count, volume_short_count, flow_long_count, flow_short_count,
                           strategy_long_count, strategy_short_count, recovered_hit_events,
                           strategy_watch_count, strategy_policy_filtered + strategy_quality_filtered,
                           sum(movers_sent.values()), backtest_rankings=backtest_rankings)


if __name__ == "__main__":
    main()
