"""Portfolio-level risk limits, measured in R (1R = the amount risked on one trade).

Why R and not dollars: the paper account books a fixed $100 per signal (the Sheets dashboard is built on
that). A fixed notional means a 4% stop risks 4x what a 1% stop does, so dollar P&L hides how risky a trade
was. Everything here (limits, circuit breaker, statistics) therefore uses R, and the alert tells you the
position size that would have risked exactly RISK_PER_TRADE_PCT of YOUR equity.

Limits apply to LIVE-eligible signals only. WATCH ONLY signals exist to collect forward-test data, so they
are never starved by portfolio caps (they keep the older per-family burst caps).

    1. Per trade        RISK_PER_TRADE_PCT of equity  (suggested size = risk / stop distance, never levered)
    2. Total open risk  MAX_TOTAL_OPEN_RISK_PCT. A position that already banked TP1 and sits at break-even
                        is risk-free and no longer counts.
    3. Correlation      altcoins move together, so same-direction risk is capped lower
                        (MAX_SAME_DIRECTION_RISK_PCT) - five longs are mostly ONE bet.
    4. Circuit breakers consecutive stop-outs pause new live signals; a daily loss limit halts them for the
                        rest of the UTC day.
"""
import csv
import os
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JOURNAL_FILE = os.path.join(ROOT, "deep_pocket_log.csv")

ACCOUNT_EQUITY_USD = 150.0            # same reference the dashboard uses; change to your real equity
RISK_PER_TRADE_PCT = 1.0
MAX_TOTAL_OPEN_RISK_PCT = 5.0
MAX_SAME_DIRECTION_RISK_PCT = 3.0
MAX_OPEN_POSITIONS = 5
LOSS_STREAK_LIMIT = 3
LOSS_STREAK_PAUSE_HOURS = 2.0
DAILY_LOSS_LIMIT_R = 3.0


def suggested_position_usd(entry, stop, equity=None, risk_pct=None):
    """Position value that risks exactly risk_pct of equity if the stop is hit (capped at 1x equity)."""
    equity = ACCOUNT_EQUITY_USD if equity is None else equity
    risk_pct = RISK_PER_TRADE_PCT if risk_pct is None else risk_pct
    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or stop <= 0 or entry == stop:
        return None
    stop_fraction = abs(entry - stop) / entry
    return round(min(equity, equity * risk_pct / 100.0 / stop_fraction), 2)


def is_live_eligible(trade):
    return not trade.get("watch_only") and not trade.get("suppress_discord_updates")


def _risk_free(trade):
    return bool(trade.get("tp1_hit") and trade.get("break_even_activated"))


def _trade_risk(trade):
    """Risk this position carries: the tier's share (A 1%, B 0.5%) or the default per-trade risk."""
    try:
        return float(trade.get("tier_risk_pct", RISK_PER_TRADE_PCT))
    except (TypeError, ValueError):
        return RISK_PER_TRADE_PCT


def open_risk_pct(open_trades, direction=None):
    total = 0.0
    for trade in open_trades:
        if not is_live_eligible(trade) or _risk_free(trade):
            continue
        if direction and str(trade.get("direction", "")).upper() != str(direction).upper():
            continue
        total += _trade_risk(trade)
    return total


def check_new(signal, open_trades):
    """None when a LIVE-eligible signal fits inside the limits, else a short reason."""
    if signal.get("watch_only"):
        return None
    live_open = [t for t in open_trades if is_live_eligible(t) and not _risk_free(t)]
    if len(live_open) >= MAX_OPEN_POSITIONS:
        return f"{len(live_open)} live positions already open (max {MAX_OPEN_POSITIONS})"
    new_risk = _trade_risk(signal)
    if open_risk_pct(open_trades) + new_risk > MAX_TOTAL_OPEN_RISK_PCT + 1e-9:
        return f"total open risk would exceed {MAX_TOTAL_OPEN_RISK_PCT:g}%"
    direction = str(signal.get("direction") or ("LONG" if signal.get("action") == "BUY" else "SHORT")).upper()
    if open_risk_pct(open_trades, direction) + new_risk > MAX_SAME_DIRECTION_RISK_PCT + 1e-9:
        return f"same-direction ({direction}) risk would exceed {MAX_SAME_DIRECTION_RISK_PCT:g}% (correlated)"
    return None


# ----------------------------- circuit breakers -----------------------------

def _to_float(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _parse_time(text):
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_closed_live_rows(path=None):
    """Closed, LIVE-eligible journal rows (watch-only rows are excluded), oldest first."""
    path = path or JOURNAL_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    closed = [r for r in rows
              if r.get("status") == "CLOSED" and r.get("outcome") in {"SL_HIT", "TP2_HIT", "BE_HIT"}
              and "WATCH ONLY" not in str(r.get("scanner", ""))]
    closed.sort(key=lambda r: str(r.get("event_time_utc", "")))
    return closed


def breaker_reason(rows=None, now=None):
    """None when new live signals are allowed, else why they are paused."""
    rows = load_closed_live_rows() if rows is None else rows
    now = now or datetime.now(timezone.utc)
    streak, last_loss_time = 0, None
    for row in rows:
        if row["outcome"] == "SL_HIT":
            streak += 1
            last_loss_time = _parse_time(row.get("event_time_utc"))
        elif row["outcome"] == "TP2_HIT":
            streak = 0                         # break-even exits are neutral: they neither extend nor reset
    if streak >= LOSS_STREAK_LIMIT and last_loss_time is not None:
        resume = last_loss_time + timedelta(hours=LOSS_STREAK_PAUSE_HOURS)
        if now < resume:
            return f"{streak} stop-outs in a row - live signals paused until {resume:%H:%M} UTC"
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_r = sum(_to_float(r.get("r_multiple")) for r in rows
                  if (_parse_time(r.get("event_time_utc")) or day_start - timedelta(days=1)) >= day_start)
    if today_r <= -DAILY_LOSS_LIMIT_R:
        return f"daily loss limit reached ({today_r:+.1f}R) - live signals halted until 00:00 UTC"
    return None
