"""Daily Top-5 board: the best-ranked setups of the UTC day, posted to Discord at 06:00 / 12:00 / 18:00 UTC.

Why a board and not a quota: forcing "at least 5 signals a day" regardless of market conditions means taking
trades the system itself rates as poor - that is how an account is bled. The professional equivalent is a
ranked opportunity list: every day you see the best five setups the scan found, each graded A / B / C, and you
size by grade (A full, B half, C none). On a quiet day fewer than five exist and the board says so.

The board only LISTS signals that were already registered (and are paper-tracked) by the scan; it never creates
extra trades.
"""
import json
import os
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BOARD_FILE = os.path.join(ROOT, "daily_board.json")
TOP_N = 5
SLOTS_UTC = (6, 12, 18)
TIER_RANK = {"A": 0, "B": 1, "C": 2}
TIER_LABEL = {"A": "🥇 A · full size", "B": "🥈 B · half size", "C": "🥉 C · track only"}
ONLY_DISPLAY_TIER_AB = True  # when Tier A/B setups exist today, hide Tier C noise from the Discord Top-5 board


def _today(now):
    return now.strftime("%Y-%m-%d")


def load(now=None, path=None):
    now = now or datetime.now(timezone.utc)
    try:
        with open(path or BOARD_FILE, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("date") == _today(now) and isinstance(state.get("entries"), list):
            return state
    except (OSError, ValueError):
        pass
    return {"date": _today(now), "entries": [], "posted_slots": []}


def save(state, path=None):
    path = path or BOARD_FILE
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def record(signal, now=None, path=None):
    """Add a registered, scored signal to today's board. Unscored signals are ignored."""
    details = signal.get("details") if isinstance(signal.get("details"), dict) else {}
    score = details.get("quality_score")
    if score is None:
        return False
    now = now or datetime.now(timezone.utc)
    state = load(now, path)
    key = signal.get("event_id") or f"{signal.get('symbol')}|{signal.get('direction')}|{signal.get('scanner')}"
    if any(e["key"] == key for e in state["entries"]):
        return False
    state["entries"].append({
        "key": key, "symbol": signal.get("symbol"), "direction": signal.get("direction"),
        "source": signal.get("strategy_name") or signal.get("scanner"), "score": float(score),
        "tier": details.get("tier") or signal.get("tier") or "C", "price": signal.get("price"),
        "stop": signal.get("stop_loss"), "timeframe": signal.get("timeframe"),
        "session": details.get("session_name") or "",
        "time": now.strftime("%H:%M")})
    save(state, path)
    return True


def top(entries, n=TOP_N):
    """Best first: tier A before B before C, then higher score. One line per coin+direction."""
    best = {}
    for e in entries:
        k = (e["symbol"], e["direction"])
        if k not in best or (TIER_RANK[e["tier"]], -e["score"]) < (TIER_RANK[best[k]["tier"]], -best[k]["score"]):
            best[k] = e
    return sorted(best.values(), key=lambda e: (TIER_RANK[e["tier"]], -e["score"]))[:n]


def due_slot(state, now=None):
    """The latest posting slot (UTC hour) that has passed today and was not posted yet, else None."""
    now = now or datetime.now(timezone.utc)
    for slot in sorted(SLOTS_UTC, reverse=True):
        if now.hour >= slot:
            return slot if slot not in state["posted_slots"] else None
    return None


def build_embed(state, slot, risk_pct_by_tier=None):
    all_ranked = top(state["entries"])
    if ONLY_DISPLAY_TIER_AB:
        ab_only = [e for e in all_ranked if e.get("tier") in {"A", "B"}]
        ranked = ab_only if ab_only else []
    else:
        ranked = all_ranked
    lines = []
    for i, e in enumerate(ranked, 1):
        side = "BUY" if e["direction"] == "LONG" else "SELL"
        price = f"{float(e['price']):.6g}" if e.get("price") else "n/a"
        stop = f"{float(e['stop']):.6g}" if e.get("stop") else "n/a"
        session_tag = f" · {e['session']}" if e.get("session") else ""
        lines.append(f"**{i}. {e['symbol']} {side}** · {TIER_LABEL.get(e['tier'], e['tier'])} · score {e['score']:.0f}\n"
                     f"entry {price} · stop {stop} · {e.get('timeframe') or ''} · {e['source']} · {e['time']} UTC{session_tag}")
    if not lines:
        lines = ["No setup has cleared the Tier A / Tier B sniper filters today yet."]
    footer = ("Ranked sniper setups (Tier A & B only), not guarantees. " if len(ranked) >= TOP_N else
              f"Only {len(ranked)} setup(s) cleared the filters so far today - that is the filter working, not a fault. ")
    return {"embeds": [{
        "title": f"📋 Daily Top {TOP_N} · {state['date']} · {slot:02d}:00 UTC board",
        "description": "\n\n".join(lines)[:3800],
        "color": 3447003,
        "footer": {"text": footer + "DeepPocket Tracker · not financial advice"}}]}


def mark_posted(state, slot, path=None):
    state["posted_slots"].append(slot)
    save(state, path)
