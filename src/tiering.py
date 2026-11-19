"""Tier A / B / C system, daily signal board and daily loss limit (added 2026-10-10).

Why this exists
  A hard "quality >= 60 or nothing" cut-off made quiet (NEUTRAL) days produce 0-1 signals. Instead of
  dropping weak signals, every signal that survives the HARD gates (stop cap, BTC drop guard, 4h+1d both
  against, loss-streak pause) is graded:

    Tier A  quality >= 65  -> FULL size  ($100)
    Tier B  quality >= floor (50, adaptive, never below 40) -> HALF size ($50)
    Tier C  everything else -> WATCH ONLY (paper-tracked so the thresholds can be tuned with real data)

  Downgrades (one tier each): counter-trend regime, and a single evidence family (one rule, no consensus).
  An A/B signal also becomes Tier C when today's realized loss limit is already hit.

What this does NOT do: it never overrides a hard gate, and it cannot make a losing strategy profitable.
The adaptive floor only widens the pool of HALF-size signals late in a quiet day; it is bounded below.
"""
import csv
import json
import os
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DAILY_STATE_FILE = os.path.join(ROOT, "daily_state.json")

TIER_A_MIN = 65.0
TIER_B_BASE_MIN = 50.0
TIER_B_HARD_FLOOR = 40.0          # the adaptive floor never goes below this
SIZE_MULTIPLIER = {"A": 1.0, "B": 0.5, "C": 0.0}   # real-money guidance; paper accounting stays $100 per signal so tiers are comparable
ACTION_TEXT = {"A": "TRADE FULL SIZE ($100)", "B": "TRADE HALF SIZE ($50)", "C": "WATCH ONLY (no trade)"}
TIER_ORDER = ["A", "B", "C"]

# Daily targets / limits
DAILY_TARGET_SIGNALS = 5          # A+B signals wanted per day (a target for the floor, not a guarantee)
DAILY_MAX_LOSS_USD = 10.0         # once today's realized P&L is at or below -10, new signals become watch-only
MAX_WATCH_PER_SCAN = 4            # Tier C alerts per scan (all are still journaled); stops Discord spam
# (hours since 00:00 UTC, A+B signals so far must be below this) -> lower the Tier B floor to this value
ADAPTIVE_STEPS = [(16, 4, 40.0), (8, 2, 45.0)]
DIGEST_HOUR_UTC = 17              # 22:00 Pakistan time
BOARD_SIZE = 5


def _now():
    return datetime.now(timezone.utc)


def _today(now=None):
    return (now or _now()).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------------- daily state
def load_state(path=None, now=None):
    path = path or DAILY_STATE_FILE
    today = _today(now)
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict) and state.get("date") == today:
            return state
    except (OSError, ValueError):
        pass
    return {"date": today, "tradable": 0, "watch": 0, "by_tier": {"A": 0, "B": 0, "C": 0},
            "board": [], "digest_sent": False}


def save_state(state, path=None):
    path = path or DAILY_STATE_FILE
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1)
    os.replace(tmp, path)


def record_signal(state, tier, entry):
    """Count a signal and keep it on today's board (best BOARD_SIZE*4 by score are retained)."""
    state["by_tier"][tier] = state["by_tier"].get(tier, 0) + 1
    if tier in ("A", "B"):
        state["tradable"] = state.get("tradable", 0) + 1
    else:
        state["watch"] = state.get("watch", 0) + 1
    board = [item for item in state.get("board", [])
             if not (item.get("symbol") == entry.get("symbol") and item.get("direction") == entry.get("direction"))]
    board.append(entry)
    board.sort(key=lambda item: -float(item.get("score") or 0))
    state["board"] = board[:BOARD_SIZE * 4]
    return state


# ----------------------------------------------------------------------------- grading
def tier_b_floor(state, now=None):
    """Adaptive Tier B floor: lower later in the day when too few A/B signals have appeared. Bounded."""
    now = now or _now()
    hours = now.hour + now.minute / 60
    have = int((state or {}).get("tradable", 0))
    for after_hour, below_count, floor in ADAPTIVE_STEPS:
        if hours >= after_hour and have < below_count:
            return max(TIER_B_HARD_FLOOR, floor)
    return TIER_B_BASE_MIN


def _down(tier, steps):
    return TIER_ORDER[min(len(TIER_ORDER) - 1, TIER_ORDER.index(tier) + steps)]


def assign_tier(score, agreement, regime_alignment, floor=TIER_B_BASE_MIN, loss_limit_hit=False):
    """Return (tier, reasons). Score thresholds first, then one-tier downgrades."""
    reasons = []
    if score >= TIER_A_MIN:
        tier = "A"
    elif score >= floor:
        tier = "B"
    else:
        tier = "C"
        reasons.append(f"quality {score:.0f} below Tier B floor {floor:.0f}")
    steps = 0
    if regime_alignment == "COUNTER-TREND":
        steps += 1
        reasons.append("counter-trend vs BTC regime")
    if int(agreement or 1) <= 1:
        steps += 1
        reasons.append("single evidence family")
    if steps and tier != "C":
        tier = _down(tier, steps)
    if loss_limit_hit and tier != "C":
        tier = "C"
        reasons.append(f"daily loss limit (-${DAILY_MAX_LOSS_USD:g}) reached")
    return tier, reasons


# ----------------------------------------------------------------------------- journal helpers
TERMINAL = {"SL_HIT", "TP2_HIT", "BE_HIT"}


def _parse(text):
    try:
        value = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_closed(journal_path, extra_rows=None):
    """Closed-trade rows (terminal outcome with a realized P&L), de-duplicated by event id + outcome."""
    rows, seen = [], set()
    try:
        with open(journal_path, encoding="utf-8", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    except OSError:
        pass
    rows.extend(extra_rows or [])
    out = []
    for row in rows:
        if row.get("outcome") not in TERMINAL:
            continue
        pnl = _num(row.get("realized_pnl_usd"))
        when = _parse(row.get("event_time_utc"))
        if pnl is None or when is None:
            continue
        key = (row.get("event_id"), row.get("outcome"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"time": when, "pnl": pnl, "outcome": row["outcome"], "source": row.get("scanner", ""),
                    "symbol": row.get("symbol", "")})
    return out


def daily_realized_pnl(journal_path, now=None, extra_rows=None):
    today = _today(now)
    return round(sum(r["pnl"] for r in read_closed(journal_path, extra_rows)
                     if r["time"].strftime("%Y-%m-%d") == today), 2)


def loss_limit_reason(journal_path, now=None, extra_rows=None, limit=None):
    limit = DAILY_MAX_LOSS_USD if limit is None else limit
    pnl = daily_realized_pnl(journal_path, now, extra_rows)
    if pnl <= -abs(limit):
        return f"today's realized P&L is {pnl:+.2f} USD (limit -{abs(limit):g}): new signals are watch-only"
    return None


# ----------------------------------------------------------------------------- scorecard
PROMOTE_MIN_TRADES = 30
PROMOTE_MIN_PROFIT_FACTOR = 1.3


def scorecard(journal_path, extra_rows=None):
    """Per-source results of CLOSED paper trades, with a promotion verdict for watch-only sources."""
    groups = {}
    for row in read_closed(journal_path, extra_rows):
        name = row["source"].replace("STRATEGY · WATCH ONLY · ", "FT · ").replace(" · WATCH ONLY", " (FT)")
        groups.setdefault(name or "unknown", []).append(row["pnl"])
    cards = []
    for name, pnls in groups.items():
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_loss = -sum(losses)
        pf = (sum(wins) / gross_loss) if gross_loss > 0 else (float("inf") if wins else 0.0)
        net = sum(pnls)
        enough = len(pnls) >= PROMOTE_MIN_TRADES
        if enough and net > 0 and pf >= PROMOTE_MIN_PROFIT_FACTOR:
            verdict = "PROMOTE-ELIGIBLE"
        elif enough and net <= 0:
            verdict = "CUT"
        else:
            verdict = f"TESTING ({len(pnls)}/{PROMOTE_MIN_TRADES})"
        cards.append({"name": name, "trades": len(pnls), "win_rate": round(100 * len(wins) / len(pnls), 1),
                      "net": round(net, 2), "profit_factor": None if pf == float("inf") else round(pf, 2),
                      "verdict": verdict})
    cards.sort(key=lambda c: -c["net"])
    return cards


# ----------------------------------------------------------------------------- digest
def build_digest(state, journal_path, regime_text="", extra_rows=None):
    """Discord embed payload: today's top-5 board, counts, realized P&L and the strategy scorecard."""
    board = sorted(state.get("board", []), key=lambda item: -float(item.get("score") or 0))[:BOARD_SIZE]
    lines = []
    for idx, item in enumerate(board, 1):
        lines.append(f"{idx}. **{item['symbol']} {item['side']}** · Tier {item['tier']} · Q{float(item['score']):.0f} · "
                     f"{item.get('source', '')} · entry {item.get('price')} SL {item.get('stop')} "
                     f"→ {ACTION_TEXT.get(item['tier'], '')}")
    if not lines:
        lines = ["No candidates passed the hard gates today."]
    pnl = daily_realized_pnl(journal_path, extra_rows=extra_rows)
    counts = state.get("by_tier", {})
    cards = scorecard(journal_path, extra_rows)[:8]
    card_lines = [f"{c['name']}: {c['trades']} closed · win {c['win_rate']}% · net ${c['net']:+.2f} · "
                  f"PF {c['profit_factor']} · {c['verdict']}" for c in cards] or ["No closed trades yet."]
    target_note = ("" if state.get("tradable", 0) >= DAILY_TARGET_SIGNALS else
                   f"\nOnly {state.get('tradable', 0)} A/B signals today; the target of {DAILY_TARGET_SIGNALS} is a goal, "
                   f"not a guarantee. Lower-quality setups are Tier C (watch only), never forced.")
    return {"embeds": [{
        "title": f"📋 DeepPocket Daily Board — {state.get('date')}",
        "color": 3447003,
        "fields": [
            {"name": f"Top {BOARD_SIZE} setups today", "value": "\n".join(lines)[:1000], "inline": False},
            {"name": "Counts", "inline": True,
             "value": f"A {counts.get('A', 0)} · B {counts.get('B', 0)} · C {counts.get('C', 0)}"},
            {"name": "Realized today (paper)", "inline": True, "value": f"${pnl:+.2f} (limit -${DAILY_MAX_LOSS_USD:g})"},
            {"name": "Market regime", "inline": True, "value": regime_text or "n/a"},
            {"name": "Strategy scorecard (closed paper trades)", "value": "\n".join(card_lines)[:1000], "inline": False},
        ],
        "description": target_note.strip() or None,
        "footer": {"text": "DeepPocket Tracker · paper results, not financial advice"},
    }]}


def digest_due(state, now=None):
    now = now or _now()
    return now.hour >= DIGEST_HOUR_UTC and not state.get("digest_sent")
