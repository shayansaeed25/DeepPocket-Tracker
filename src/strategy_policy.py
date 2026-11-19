"""Strategy policy for DeepPocket Tracker (v5.0 Dynamic Backtest Auto-Selector Edition).

Includes:
  - Institutional Smart-Money setups (`Smart Money Liquidity Sweep`, `Smart Money CVD Breakdown`,
    `Smart Money EMA20 Pullback`) -> ACTIVE when approved by `backtester.py`.
  - All 10 QuantX Technical Strategies (`Alpha Prime`, `Core Momentum`, `Prime Edge`, `Elite Trend`,
    `Nova Alpha`, `Smart Vector`, `Global Core`, `Apex Trend`, `Quant Prime`, `Signal Edge`) ->
    dynamically enabled for LIVE trading whenever `backtester.py` proves they are profitable on
    previous historical candles (`approved == True`), and automatically disabled ("kicked out")
    whenever they are unprofitable on previous candles (`approved == False`).
"""

import json
from pathlib import Path

WHALE_FLOW_MODE = "WATCH_ONLY"
SMART_VECTOR_MODE = "WATCH_ONLY"
SMART_VECTOR = "Smart Vector"
SMART_MONEY_STRATEGIES = frozenset({
    "Smart Money Liquidity Sweep",
    "Smart Money CVD Breakdown",
    "Smart Money EMA20 Pullback",
})

WATCH_EXACT_CONSENSUSES = frozenset({
    frozenset({"Elite Trend", "Smart Vector"}),
    frozenset({"Prime Edge", "Quant Prime"}),
    frozenset({"Elite Trend", "Quant Prime", "Signal Edge"}),
})

REMOVED_EXCEPTIONS = frozenset({
    frozenset({"Elite Trend", "Quant Prime", "Signal Edge"}),
})

_PRIMARY_TO_FAMILY = {
    "donchian": "BREAKOUT",
    "momentum": "MOMENTUM",
    "rsi": "MEAN_REVERSION",
    "wma": "TREND_MA", "dema": "TREND_MA", "ema": "TREND_MA", "vwma": "TREND_MA",
}
_PROFILE_FILE = Path(__file__).resolve().parent.parent / "strategies" / "strategy_profiles.json"
try:
    STRATEGY_FAMILY = {
        p["name"]: _PRIMARY_TO_FAMILY.get(str(p.get("primary")).lower(), str(p.get("primary")).upper())
        for p in json.loads(_PROFILE_FILE.read_text(encoding="utf-8"))["profiles"]
    }
except (OSError, ValueError, KeyError):
    STRATEGY_FAMILY = {}

STRATEGY_FAMILY["Smart Money Liquidity Sweep"] = "LIQUIDITY_SWEEP"
STRATEGY_FAMILY["Smart Money CVD Breakdown"] = "CVD_BREAKDOWN"
STRATEGY_FAMILY["Smart Money EMA20 Pullback"] = "EMA_PULLBACK"

REMOVED_STRATEGIES = frozenset({
    "Apex Trend", "Nova Alpha", "Global Core", "Alpha Prime",
    "Core Momentum", "Trend Momentum", "Signal Edge",
})


def classify_strategy_group(names, direction):
    """Static baseline policy (preserved for backward-compatible unit tests)."""
    members = frozenset(str(name).strip() for name in names if str(name).strip())
    side = str(direction).upper()
    if not members:
        return None
    if members & SMART_MONEY_STRATEGIES:
        return "ACTIVE" if side in ("LONG", "SHORT") else None
    if members & REMOVED_STRATEGIES and members not in REMOVED_EXCEPTIONS:
        return None
    if members == frozenset({SMART_VECTOR}):
        return SMART_VECTOR_MODE if side in ("LONG", "SHORT") else None
    if members in WATCH_EXACT_CONSENSUSES:
        return "WATCH_ONLY"
    return None


def classify_strategy_with_backtest(names, direction, backtest_rankings=None):
    """Dynamic v5.0 Auto-Selector:
    Checks `backtest_rankings` (computed from previous historical candles across the Top-30 universe).
    - If ANY strategy in `names` is marked `approved == True` in `backtest_rankings`, returns `"ACTIVE"`
      so profitable strategies (e.g., `Quant Prime`, `Smart Money Liquidity Sweep`, `Smart Money EMA20 Pullback`,
      `Elite Trend`, etc.) trade LIVE even solo!
    - If all strategies in `names` were evaluated in `backtest_rankings` and NONE is `approved`,
      returns `None` (kicks out unprofitable strategies automatically!).
    """
    members = frozenset(str(name).strip() for name in names if str(name).strip())
    side = str(direction).upper()
    if not members or side not in ("LONG", "SHORT"):
        return None

    if backtest_rankings and isinstance(backtest_rankings, dict):
        approved_members = []
        evaluated_members = []
        for m in members:
            stat = backtest_rankings.get(m)
            if isinstance(stat, dict) and stat.get("trades", 0) > 0:
                evaluated_members.append(m)
                if stat.get("approved", False):
                    approved_members.append(m)
            elif m in SMART_MONEY_STRATEGIES and (not stat or stat.get("trades", 0) == 0):
                # Smart-Money core strategies remain active if sample size is still building
                approved_members.append(m)

        if approved_members:
            return "ACTIVE"
        if evaluated_members:
            # Every evaluated strategy in this trigger failed the historical candle backtest -> kick it out!
            return None

    return classify_strategy_group(names, direction)


def independent_family_count(names, backtest_rankings=None):
    """How many DIFFERENT indicator/orderflow families are in a group.
    Strategies proven profitable on historical candles (`approved == True`) combine technical
    structure + empirical historical edge (minimum 2 evidence families)."""
    families = set()
    for name in names:
        name = str(name).strip()
        if name:
            if name in SMART_MONEY_STRATEGIES:
                families.add(STRATEGY_FAMILY[name])
                families.add("ORDERFLOW_CVD")
            else:
                families.add(STRATEGY_FAMILY.get(name, f"UNKNOWN:{name}"))
                if isinstance(backtest_rankings, dict) and backtest_rankings.get(name, {}).get("approved"):
                    families.add("HISTORICAL_BACKTEST_EDGE")
    return len(families)
