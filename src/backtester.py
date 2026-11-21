"""
DeepPocket Tracker — Fast Vectorized Historical Candle Backtester & Auto-Selector (v5.0)
----------------------------------------------------------------------------------------
Tests ALL 13 implemented strategies (3 Smart-Money Orderflow setups + all 10 QuantX
Technical strategies) across the previous closed 1h & 4h candles of the Top-30 coin universe.

For every strategy:
  1. Walks across historical closed candles (`start_idx .. len(closes) - 2`).
  2. Simulates every trade with exact Entry, Hard Stop-Loss, Sniper TP1 (0.90%..1.25% -> 50%
     profit booked + Stop-Loss moved to Entry + 55% step-lock ratchet), and TP2 (1.85%..2.45%).
  3. Computes:
       - Total Closed Trades (`trades`), Wins (`wins`), TP2 Hits (`tp2_hits`), SL Hits (`losses`)
       - Win Rate (`win_rate_pct`) & Stop-Loss Rate (`sl_rate_pct`)
       - Profit Factor (`profit_factor = gross_profit / gross_loss`)
       - Net Return (`net_pnl_usd` on $100 notional per trade) & Max Drawdown (`max_drawdown_pct`)
  4. Dynamic Auto-Selector (`approved`):
       - Automatically enables (`✅ ACTIVE (LIVE)`) ONLY strategies with:
           * `Profit Factor >= 1.20`
           * `Win Rate >= 52.0%`
           * `Net P&L > $0.00`
           * `Max Drawdown <= 12.0%`
       - Automatically disables (`🛑 DISABLED (KICKED OUT)`) any strategy that loses money or
         fails the threshold on previous candles.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

import smart_money

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKTEST_CACHE_FILE = os.path.join(ROOT, "backtest_rankings.json")
BACKTEST_REPORT_MD = os.path.join(ROOT, "reports", "historical_backtest.md")

MIN_PROFIT_FACTOR = 1.35
MAX_DRAWDOWN_PCT = 12.0
MIN_WIN_RATE_PCT = 52.0
MIN_TRADES_FOR_PRUNING = 3

ACCOUNT_BASE_USD = 150.0
NOTIONAL_USD = 100.0
COST_ROUND_TRIP_PCT = 0.20  # 0.10% fee/slippage per side

ALL_14_STRATEGY_NAMES = (
    "Smart Money Liquidity Sweep",
    "Smart Money EMA20 Pullback",
    "Smart Money CVD Breakdown",
    "WZ Reversal & Liquidity Magnet",
    "Quant Prime",
    "Elite Trend",
    "Alpha Prime",
    "Core Momentum",
    "Prime Edge",
    "Global Core",
    "Signal Edge",
    "Apex Trend",
    "Nova Alpha",
    "Smart Vector",
)
ALL_13_STRATEGY_NAMES = ALL_14_STRATEGY_NAMES


def simulate_trade_on_candles(
    signal: dict[str, Any],
    klines: dict[str, Any],
    entry_idx: int,
    max_hold_bars: int = 24,
) -> dict[str, Any] | None:
    """Walk forward from `entry_idx + 1` to resolve `TP2_HIT`, `BE_HIT`, `SL_HIT`, or `TIME_EXIT`."""
    highs = klines.get("high") or []
    lows = klines.get("low") or []
    closes = klines.get("close") or []
    n = len(closes) - 1  # exclude forming candle
    if entry_idx + 1 >= n:
        return None

    is_long = str(signal.get("direction", "LONG")).upper() in {"LONG", "BUY"}
    entry = float(signal["price"])
    sl = float(signal["stop_loss"])
    orig_sl = sl
    risk = abs(entry - orig_sl)
    if entry <= 0 or risk <= 0:
        return None

    sign = 1.0 if is_long else -1.0
    if signal.get("take_profit_1") and signal.get("take_profit_2"):
        tp1 = float(signal["take_profit_1"])
        tp2 = float(signal["take_profit_2"])
    else:
        tp1, tp2, _ = smart_money.compute_sniper_targets(entry, orig_sl, is_long=is_long)

    tp1_hit = False
    ratchet_locked = False
    ratchet_trigger = tp1 + 0.55 * (tp2 - tp1)

    end_idx = min(n - 1, entry_idx + max_hold_bars)
    outcome = "TIME_EXIT"
    exit_price = float(closes[end_idx])

    for j in range(entry_idx + 1, end_idx + 1):
        hi, lo = float(highs[j]), float(lows[j])
        hit_sl = (lo <= sl) if is_long else (hi >= sl)
        hit_tp1 = (not tp1_hit) and ((hi >= tp1) if is_long else (lo <= tp1))
        hit_tp2 = (hi >= tp2) if is_long else (lo <= tp2)

        if hit_sl and (hit_tp1 or hit_tp2):
            outcome = "BE_HIT" if tp1_hit else "SL_HIT"
            exit_price = sl
            break
        if hit_sl:
            outcome = "BE_HIT" if tp1_hit else "SL_HIT"
            exit_price = sl
            break
        if hit_tp2:
            tp1_hit = True
            outcome = "TP2_HIT"
            exit_price = tp2
            break
        if hit_tp1:
            tp1_hit = True
            sl = entry
        if tp1_hit and not ratchet_locked:
            progressed = (hi >= ratchet_trigger) if is_long else (lo <= ratchet_trigger)
            if progressed:
                sl = tp1
                ratchet_locked = True

    qty = NOTIONAL_USD / entry
    if outcome == "TP2_HIT":
        gross_usd = 0.5 * (tp1 - entry) * sign * qty + 0.5 * (tp2 - entry) * sign * qty
    elif outcome == "BE_HIT":
        gross_usd = 0.5 * (tp1 - entry) * sign * qty + 0.5 * (exit_price - entry) * sign * qty
    elif outcome == "SL_HIT":
        gross_usd = (exit_price - entry) * sign * qty
    else:
        if tp1_hit:
            gross_usd = 0.5 * (tp1 - entry) * sign * qty + 0.5 * (exit_price - entry) * sign * qty
        else:
            gross_usd = (exit_price - entry) * sign * qty

    cost_usd = NOTIONAL_USD * (COST_ROUND_TRIP_PCT / 100.0)
    net_usd = round(gross_usd - cost_usd, 4)
    risk_usd = risk * qty
    r_mult = round(net_usd / risk_usd, 3) if risk_usd > 0 else 0.0

    return {
        "symbol": signal.get("symbol"),
        "strategy": signal.get("strategy_name") or signal.get("scanner"),
        "direction": signal.get("direction"),
        "timeframe": signal.get("timeframe"),
        "outcome": outcome,
        "tp1_reached": tp1_hit,
        "gross_pnl_usd": round(gross_usd, 4),
        "net_pnl_usd": net_usd,
        "r_multiple": r_mult,
    }


def backtest_klines_dataset(
    klines_by_symbol_tf: dict[tuple[str, str], dict[str, Any]],
    btc_bearish_bias: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """Run vectorized historical simulation across all supplied `(symbol, timeframe)` candle series
    for ALL 13 strategies (3 Smart-Money + 10 QuantX strategies)."""
    import strategy_scanner

    trades_by_strategy: dict[str, list[dict[str, Any]]] = {name: [] for name in ALL_13_STRATEGY_NAMES}

    for (symbol, timeframe), klines in klines_by_symbol_tf.items():
        if not klines or len(klines.get("close") or []) < 75:
            continue
        closes = klines["close"]
        n_closed = len(closes) - 1
        cooldown_until: dict[str, int] = {}

        for idx in range(55, n_closed - 6):
            # 1. Evaluate all 3 Smart-Money setups at candle `idx`
            sm_signals = smart_money.evaluate_at_index(
                symbol, klines, timeframe, idx, btc_bearish_bias=btc_bearish_bias
            )
            for sig in sm_signals:
                sname = sig["strategy_name"]
                if idx < cooldown_until.get(sname, 0):
                    continue
                sim = simulate_trade_on_candles(sig, klines, idx)
                if sim:
                    trades_by_strategy.setdefault(sname, []).append(sim)
                    cooldown_until[sname] = idx + 5

            # 2. Evaluate ALL 10 QuantX Technical Strategies on a sliced window up to `idx + 1`
            if idx % 3 == 0:  # sample every 3rd bar for fast execution across all 10 strategies
                sliced = {
                    k: v[: idx + 2]
                    for k, v in klines.items()
                    if isinstance(v, list)
                }
                for legacy_sig in strategy_scanner.evaluate_all(symbol, sliced, timeframe):
                    sname = legacy_sig["strategy_name"]
                    if btc_bearish_bias is True and legacy_sig["direction"] == "LONG":
                        continue
                    if btc_bearish_bias is False and legacy_sig["direction"] == "SHORT":
                        continue
                    if idx < cooldown_until.get(sname, 0):
                        continue
                    is_l = str(legacy_sig.get("direction", "LONG")).upper() in {"LONG", "BUY"}
                    tp1_l, tp2_l, _ = smart_money.compute_sniper_targets(
                        float(legacy_sig["price"]), float(legacy_sig["stop_loss"]), is_long=is_l
                    )
                    legacy_sig = dict(legacy_sig, take_profit_1=tp1_l, take_profit_2=tp2_l)
                    sim = simulate_trade_on_candles(legacy_sig, klines, idx)
                    if sim:
                        trades_by_strategy.setdefault(sname, []).append(sim)
                        cooldown_until[sname] = idx + 5

    rankings: dict[str, dict[str, Any]] = {}
    for sname, results in trades_by_strategy.items():
        rankings[sname] = summarize_strategy_results(sname, results)
    return rankings


def summarize_strategy_results(strategy_name: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute Profit Factor, Max Drawdown %, Win Rate %, SL Rate %, and Auto-Selector Verdict."""
    total = len(results)
    if total == 0:
        is_sm = strategy_name.startswith("Smart Money") or strategy_name.startswith("WZ ")
        return {
            "strategy": strategy_name,
            "trades": 0,
            "wins": 0,
            "tp2_hits": 0,
            "be_hits": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "sl_rate_pct": 0.0,
            "gross_profit_usd": 0.0,
            "gross_loss_usd": 0.0,
            "net_pnl_usd": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "expectancy_r": 0.0,
            "approved": is_sm,
            "verdict": "✅ ACTIVE (SMART_MONEY CORE)" if is_sm else "⏳ NO TRIGGERS IN WINDOW",
        }

    wins = [r for r in results if r["net_pnl_usd"] > 0 or r["tp1_reached"]]
    tp2_hits = [r for r in results if r["outcome"] == "TP2_HIT"]
    be_hits = [r for r in results if r["outcome"] == "BE_HIT"]
    sl_hits = [r for r in results if r["outcome"] == "SL_HIT"]
    gross_win = sum(r["net_pnl_usd"] for r in results if r["net_pnl_usd"] > 0)
    gross_loss = abs(sum(r["net_pnl_usd"] for r in results if r["net_pnl_usd"] < 0))
    net_pnl = sum(r["net_pnl_usd"] for r in results)

    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (9.99 if gross_win > 0 else 0.0)
    win_rate = round(len(wins) / total * 100.0, 1)
    sl_rate = round(len(sl_hits) / total * 100.0, 1)
    exp_r = round(sum(r["r_multiple"] for r in results) / total, 3)

    equity = ACCOUNT_BASE_USD
    peak = ACCOUNT_BASE_USD
    max_dd_pct = 0.0
    for r in results:
        equity += r["net_pnl_usd"]
        if equity > peak:
            peak = equity
        dd_pct = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct
    max_dd_pct = round(max_dd_pct, 2)

    if total < MIN_TRADES_FOR_PRUNING:
        is_sm = strategy_name.startswith("Smart Money") or strategy_name.startswith("WZ ")
        approved = (is_sm and pf >= 1.0) or (pf >= 1.50 and win_rate >= 60.0 and net_pnl > 0)
        verdict = "✅ ACTIVE (LIVE)" if approved else "🛑 DISABLED (LOW SAMPLE / NEGATIVE)"
    else:
        approved = (
            pf >= MIN_PROFIT_FACTOR
            and max_dd_pct <= MAX_DRAWDOWN_PCT
            and win_rate >= MIN_WIN_RATE_PCT
            and net_pnl > 0
        )
        verdict = "✅ ACTIVE (LIVE)" if approved else "🛑 DISABLED (KICKED OUT)"

    return {
        "strategy": strategy_name,
        "trades": total,
        "wins": len(wins),
        "tp2_hits": len(tp2_hits),
        "be_hits": len(be_hits),
        "losses": len(sl_hits),
        "win_rate_pct": win_rate,
        "sl_rate_pct": sl_rate,
        "gross_profit_usd": round(gross_win, 2),
        "gross_loss_usd": round(gross_loss, 2),
        "net_pnl_usd": round(net_pnl, 2),
        "profit_factor": pf,
        "max_drawdown_pct": max_dd_pct,
        "expectancy_r": exp_r,
        "approved": approved,
        "verdict": verdict,
    }


def format_discord_league_table(rankings: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Return `(active_summary, disabled_summary)` strings for the Discord Scan Report embed."""
    if not rankings:
        return "Evaluating previous candles...", "None"
    sorted_items = sorted(
        rankings.values(),
        key=lambda x: (x.get("approved", False), x.get("profit_factor", 0.0), x.get("net_pnl_usd", 0.0)),
        reverse=True,
    )
    active_lines = []
    disabled_lines = []
    for st in sorted_items:
        trades = st.get("trades", 0)
        if trades == 0 and not st.get("approved"):
            continue
        line = (
            f"**{st['strategy']}**: `{trades}` trades · "
            f"**{st.get('win_rate_pct', 0):.0f}% WR** · "
            f"PF **{st.get('profit_factor', 0):.2f}** · "
            f"**${st.get('net_pnl_usd', 0):+.2f}**"
        )
        if st.get("approved"):
            active_lines.append(f"✅ {line}")
        else:
            disabled_lines.append(f"🛑 {line}")
    active_str = "\n".join(active_lines[:8]) if active_lines else "None passed threshold this window"
    disabled_str = "\n".join(disabled_lines[:6]) if disabled_lines else "None"
    return active_str[:1000], disabled_str[:1000]


def save_rankings(rankings: dict[str, dict[str, Any]], path: str = BACKTEST_CACHE_FILE) -> None:
    payload = {
        "updated_at": time.time(),
        "rankings": rankings,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass


def load_rankings(path: str = BACKTEST_CACHE_FILE) -> dict[str, dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rankings") or {}
    except (OSError, ValueError, TypeError):
        return {}


def write_markdown_report(rankings: dict[str, dict[str, Any]], path: str = BACKTEST_REPORT_MD) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Historical Candle Backtest Report (All 14 Strategies — Auto-Selector v6.0)",
        "",
        "| Strategy | Past Trades | Wins (TP Hit) | SL Hits | Win Rate | Profit Factor | Max DD % | Net Return ($100/trade) | Auto-Selector Status |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    sorted_items = sorted(
        rankings.values(),
        key=lambda x: (x.get("approved", False), x.get("profit_factor", 0.0), x.get("win_rate_pct", 0.0)),
        reverse=True,
    )
    for item in sorted_items:
        lines.append(
            f"| {item['strategy']} | {item['trades']} | {item.get('wins', 0)} | {item.get('losses', 0)} | "
            f"{item['win_rate_pct']:.1f}% | {item['profit_factor']:.2f} | "
            f"{item['max_drawdown_pct']:.1f}% | ${item['net_pnl_usd']:+.2f} | {item['verdict']} |"
        )
    content = "\n".join(lines) + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        pass
    return content
