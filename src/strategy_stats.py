"""Strategy performance statistics + evidence-based promote / suspend rules.

Replaces the "weekly review: any strategy that keeps losing is switched off for two weeks" idea. Switching
rules on and off from a handful of trades is how a desk ends up fitting noise (a coin-flip system loses
five trades in a row about once in thirty tries). The rules here are fixed IN ADVANCE and need a sample:

    n < MIN_TRADES_TO_JUDGE                 -> INSUFFICIENT DATA (keep forward-testing, change nothing)
    n >= MIN, expectancy > 0, PF >= 1.3,
      t-stat >= PROMOTE_MIN_T_STAT          -> PROMOTE (watch-only -> live eligible)
    n >= MIN, PF < 0.8 or (expectancy < 0
      and t-stat <= -1.0)                   -> SUSPEND (stop taking it, keep logging it as shadow)
    otherwise                               -> CONTINUE

Everything is net of fees + slippage and measured in R. Win rate is shown with a Wilson 95% interval,
because "75% win rate" on 12 trades is really "somewhere between 47% and 91%".

Usage:  python src/strategy_stats.py            (reads deep_pocket_log.csv, writes reports/)
"""
import argparse
import csv
import json
import math
import os
from datetime import datetime
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JOURNAL_FILE = os.path.join(ROOT, "deep_pocket_log.csv")
REPORT_DIR = os.path.join(ROOT, "reports")

MIN_TRADES_TO_JUDGE = 30
PROMOTE_MIN_PROFIT_FACTOR = 1.3
PROMOTE_MIN_T_STAT = 1.5          # use 2.0 or more before real capital
SUSPEND_MAX_PROFIT_FACTOR = 0.8
SUSPEND_MAX_T_STAT = -1.0
FEE_ROUND_TRIP_PCT = 0.30         # fallback for old rows written before net_pnl_usd existed
CLOSED_OUTCOMES = {"SL_HIT", "TP2_HIT", "BE_HIT"}

COLUMNS = ["Strategy", "Mode", "Total Trades", "Wins", "Losses", "BE", "Win Rate", "Win Rate 95% CI",
           "Avg Win", "Avg Loss", "Profit Factor", "Expectancy (R)", "t-stat", "Net P&L",
           "Max Drawdown (R)", "Ambiguous", "Recommendation"]


def normalize_source(label):
    """('Consensus: A, B', 'WATCH_ONLY') from a journal scanner label."""
    label = str(label or "").strip()
    mode = "WATCH_ONLY" if "WATCH ONLY" in label else "LIVE"
    label = label.replace("STRATEGY · ", "").replace("WATCH ONLY · ", "").replace(" · WATCH ONLY", "")
    return label.strip() or "UNKNOWN", mode


def _f(value, default=None):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def wilson_interval(wins, n, z=1.96):
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def t_statistic(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def max_drawdown(values):
    peak = cum = worst = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    return worst


def read_closed(path=None):
    path = path or JOURNAL_FILE
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return [r for r in rows if r.get("status") == "CLOSED"
            and (r.get("outcome") in CLOSED_OUTCOMES or "AMBIGUOUS" in str(r.get("outcome")))]


def _parse(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def attach_exit_diagnostics(closed, path=None):
    """Add `_hold_hours`, `_peak_pct`, `_risk_pct` to closed rows (hold time = SIGNAL row -> CLOSED row)."""
    path = path or JOURNAL_FILE
    opened = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("status") == "SIGNAL" and row.get("event_id"):
                    opened[row["event_id"]] = _parse(row.get("event_time_utc"))
    except OSError:
        pass
    for row in closed:
        start, end = opened.get(row.get("event_id")), _parse(row.get("event_time_utc"))
        row["_hold_hours"] = (end - start).total_seconds() / 3600 if start and end else None
        try:
            details = json.loads(row.get("details") or "{}")
        except ValueError:
            details = {}
        row["_peak_pct"] = _f(details.get("peak_profit_pct")) if isinstance(details, dict) else None
        price, stop = _f(row.get("price")), _f(row.get("stop_loss"))
        row["_risk_pct"] = abs(price - stop) / price * 100 if price and stop else None
    return closed


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def exit_diagnostics(rows):
    """Evidence for exit rules (time stop, earlier TP1) instead of guessing them."""
    stopped = [r for r in rows if r.get("outcome") == "SL_HIT"]
    had_1r = [r for r in stopped if r.get("_peak_pct") is not None and r.get("_risk_pct")
              and r["_peak_pct"] >= r["_risk_pct"]]
    measurable = [r for r in stopped if r.get("_peak_pct") is not None and r.get("_risk_pct")]
    return {
        "median_hold_hours": _median([r.get("_hold_hours") for r in rows if r.get("outcome") in CLOSED_OUTCOMES]),
        "median_hold_hours_sl": _median([r.get("_hold_hours") for r in stopped]),
        "median_peak_pct_sl": _median([r.get("_peak_pct") for r in stopped]),
        "sl_measurable": len(measurable),
        "sl_were_1r_up": len(had_1r),
    }


def _net_pnl(row):
    net = _f(row.get("net_pnl_usd"))
    if net is not None:
        return net
    gross = _f(row.get("realized_pnl_usd"))
    size = _f(row.get("position_size_usd"), 100.0)
    return None if gross is None else gross - size * FEE_ROUND_TRIP_PCT / 100.0


def recommendation(n, expectancy_r, profit_factor, t_stat):
    if n < MIN_TRADES_TO_JUDGE:
        return f"INSUFFICIENT DATA ({n}/{MIN_TRADES_TO_JUDGE}) - keep forward-testing"
    pf = float("inf") if profit_factor is None else profit_factor
    t = 0.0 if t_stat is None else t_stat
    er = 0.0 if expectancy_r is None else expectancy_r
    if er > 0 and pf >= PROMOTE_MIN_PROFIT_FACTOR and t >= PROMOTE_MIN_T_STAT:
        return "PROMOTE - passes the pre-set evidence bar"
    if pf < SUSPEND_MAX_PROFIT_FACTOR or (er < 0 and t <= SUSPEND_MAX_T_STAT):
        return "SUSPEND - negative edge after costs (log as shadow)"
    return "CONTINUE - edge not proven either way"


def summarize(rows):
    """Statistics for one group of closed rows (oldest first)."""
    decided = [r for r in rows if r.get("outcome") in CLOSED_OUTCOMES]
    ambiguous = len(rows) - len(decided)
    wins = sum(1 for r in decided if r["outcome"] == "TP2_HIT")
    losses = sum(1 for r in decided if r["outcome"] == "SL_HIT")
    be = sum(1 for r in decided if r["outcome"] == "BE_HIT")
    pnls = [p for p in (_net_pnl(r) for r in decided) if p is not None]
    r_values = [v for v in (_f(r.get("r_multiple")) for r in decided) if v is not None]
    n = len(decided)
    gain = sum(p for p in pnls if p > 0)
    loss = -sum(p for p in pnls if p < 0)
    profit_factor = (gain / loss) if loss > 0 else None
    pos = [p for p in pnls if p > 0]
    neg = [p for p in pnls if p < 0]
    expectancy_r = sum(r_values) / len(r_values) if r_values else None
    t_stat = t_statistic(r_values)
    low, high = wilson_interval(wins, n)
    return {
        "n": n, "wins": wins, "losses": losses, "be": be, "ambiguous": ambiguous,
        "win_rate": wins / n if n else None, "ci": (low, high),
        "avg_win": sum(pos) / len(pos) if pos else 0.0,
        "avg_loss": sum(neg) / len(neg) if neg else 0.0,
        "profit_factor": profit_factor, "gross_gain": gain,
        "expectancy_r": expectancy_r, "t_stat": t_stat,
        "net_pnl": sum(pnls), "max_dd_r": max_drawdown(r_values),
        "recommendation": recommendation(n, expectancy_r, profit_factor, t_stat),
    }


def build_table(rows):
    groups = defaultdict(list)
    for row in sorted(rows, key=lambda r: str(r.get("event_time_utc", ""))):
        name, mode = normalize_source(row.get("scanner"))
        groups[(name, mode)].append(row)
    table = []
    for (name, mode), items in sorted(groups.items()):
        s = summarize(items)
        s["name"], s["mode"] = name, mode
        table.append(s)
    return table


def quality_buckets(rows, threshold=70.0):
    """Do high-score signals really do better? (validates the score threshold on forward data)"""
    buckets = {"score >= %g" % threshold: [], "score < %g" % threshold: [], "no score": []}
    for row in rows:
        q = _f(row.get("quality_score"))
        key = "no score" if q is None else ("score >= %g" % threshold if q >= threshold else "score < %g" % threshold)
        buckets[key].append(row)
    return {k: summarize(v) for k, v in buckets.items() if v}


def tier_buckets(rows):
    """Do Tier A / B / C really rank outcomes? (validates the grading on forward data)"""
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("tier") or "no tier")].append(row)
    return {f"Tier {k}" if k != "no tier" else k: summarize(v) for k, v in sorted(groups.items())}


def daily_summary(rows, equity=150.0):
    """Net P&L per UTC day of closing - the honest answer to "how much per day does this make"."""
    days = defaultdict(float)
    for row in rows:
        when, pnl = _parse(row.get("event_time_utc")), _net_pnl(row)
        if when is not None and pnl is not None and row.get("outcome") in CLOSED_OUTCOMES:
            days[when.strftime("%Y-%m-%d")] += pnl
    if not days:
        return None
    values = list(days.values())
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
    return {"days": len(values), "mean": mean, "stdev": math.sqrt(var), "best": max(values), "worst": min(values),
            "positive_days": sum(1 for v in values if v > 0), "equity": equity,
            "mean_pct_of_equity": mean / equity * 100 if equity else None}


def _fmt(value, spec="{:.2f}", none="-"):
    return none if value is None else spec.format(value)


def table_rows(table):
    out = []
    for s in table:
        pf = "inf" if s["profit_factor"] is None and s["n"] and s["gross_gain"] > 0 else _fmt(s["profit_factor"])
        out.append([
            s["name"], s["mode"], s["n"], s["wins"], s["losses"], s["be"],
            _fmt(None if s["win_rate"] is None else s["win_rate"] * 100, "{:.0f}%"),
            "-" if not s["n"] else f"{s['ci'][0] * 100:.0f}-{s['ci'][1] * 100:.0f}%",
            _fmt(s["avg_win"]), _fmt(s["avg_loss"]), pf, _fmt(s["expectancy_r"], "{:+.2f}"),
            _fmt(s["t_stat"]), _fmt(s["net_pnl"], "{:+.2f}"), _fmt(s["max_dd_r"], "{:.1f}"),
            s["ambiguous"], s["recommendation"]])
    return out


def render_markdown(table, buckets, total_rows, diagnostics=None, tiers=None, daily=None):
    lines = ["# DeepPocket - Strategy Performance (net of fees + slippage, in R)", "",
             f"Closed trades analysed: **{total_rows}**. Decisions need >= {MIN_TRADES_TO_JUDGE} closed trades "
             "per strategy; below that the table is information, not a verdict.", "",
             "| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for row in table_rows(table):
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    if buckets:
        lines += ["", "## Does the Quality Score work? (forward data)", "",
                  "| Bucket | Trades | Win Rate | Expectancy (R) | Profit Factor |", "|---|---|---|---|---|"]
        for name, s in buckets.items():
            lines.append(f"| {name} | {s['n']} | {_fmt(None if s['win_rate'] is None else s['win_rate'] * 100, '{:.0f}%')} "
                         f"| {_fmt(s['expectancy_r'], '{:+.2f}')} | {_fmt(s['profit_factor'])} |")
    if tiers and len(tiers) > 0 and any(k != "no tier" for k in tiers):
        lines += ["", "## Do the tiers work? (A = full size, B = half, C = track only)", "",
                  "| Tier | Trades | Win Rate | Expectancy (R) | Profit Factor | Net P&L |", "|---|---|---|---|---|---|"]
        for name, s_ in tiers.items():
            lines.append(f"| {name} | {s_['n']} | {_fmt(None if s_['win_rate'] is None else s_['win_rate'] * 100, '{:.0f}%')} "
                         f"| {_fmt(s_['expectancy_r'], '{:+.2f}')} | {_fmt(s_['profit_factor'])} | {_fmt(s_['net_pnl'], '{:+.2f}')} |")
        lines += ["", "_If Tier A does not beat Tier B and Tier B does not beat Tier C after 30+ trades each, the score weights are wrong and must be changed (as a new, versioned forward test)._"]
    if daily:
        lines += ["", "## Reality check: net P&L per day", "",
                  f"- Days with closed trades: **{daily['days']}**; positive days: **{daily['positive_days']}**",
                  f"- Mean net P&L per day: **{daily['mean']:+.2f} USD** ({_fmt(daily['mean_pct_of_equity'], '{:+.1f}')}% of the "
                  f"{daily['equity']:.0f} USD reference account); day-to-day stdev {daily['stdev']:.2f} USD",
                  f"- Best day {daily['best']:+.2f} USD, worst day {daily['worst']:+.2f} USD",
                  "- A daily target is an outcome, not something a system can promise: with a stdev this large, a few days "
                  "say almost nothing about the next one."]
    if diagnostics:
        lines += ["", "## Exit diagnostics (is a time stop or an earlier TP1 justified?)", "",
                  "| Strategy | Mode | Median hours held | Median hours held (stopped) | Median peak profit before SL | Stopped trades that were >= 1R up first |",
                  "|---|---|---|---|---|---|"]
        for (name, mode), d in diagnostics.items():
            share = "-" if not d["sl_measurable"] else f"{d['sl_were_1r_up']}/{d['sl_measurable']}"
            lines.append(f"| {name} | {mode} | {_fmt(d['median_hold_hours'], '{:.1f}')} | {_fmt(d['median_hold_hours_sl'], '{:.1f}')} "
                         f"| {_fmt(d['median_peak_pct_sl'], '{:.2f}%')} | {share} |")
        lines += ["", "_Read: if many stopped trades were already >= 1R in profit, TP1/break-even is too far away; if stopped trades "
                  "are held far longer than winners, a time stop is worth a versioned forward test. Do not decide on < 30 stopped trades._"]
    if not table:
        lines += ["", "_No closed trades yet - nothing to analyse._"]
    return "\n".join(lines) + "\n"


def diagnostics_by_group(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[normalize_source(row.get("scanner"))].append(row)
    return {key: exit_diagnostics(items) for key, items in sorted(groups.items())}


def write_reports(rows, out_dir=None):
    out_dir = out_dir or REPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    table = build_table(rows)
    with open(os.path.join(out_dir, "strategy_performance.csv"), "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        writer.writerows(table_rows(table))
    markdown = render_markdown(table, quality_buckets(rows), len(rows), diagnostics_by_group(rows),
                               tier_buckets(rows), daily_summary(rows))
    with open(os.path.join(out_dir, "strategy_performance.md"), "w", encoding="utf-8") as f:
        f.write(markdown)
    return markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--journal", default=JOURNAL_FILE)
    parser.add_argument("--out", default=REPORT_DIR)
    args = parser.parse_args(argv)
    print(write_reports(attach_exit_diagnostics(read_closed(args.journal), args.journal), args.out))


if __name__ == "__main__":
    main()
