"""Google Sheets signal ledger, position tracker, paper backtest view, and visual Analytics tab.

Actionable and watch-only paper signals plus their later price-touch events
are written to Signals. Positions and Backtesting are keyed by signal ID and refreshed as
the local candle watcher tracks TP/SL. No exchange orders are placed.
Dashboard and Analytics provide executive KPIs, breakdown tables, sparklines, and charts.
System Health stores provider/feed diagnostics separately from decisions.
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any
from urllib.parse import quote

import requests

_PKT_TZ = ZoneInfo("Asia/Karachi")


def format_pkt(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_PKT_TZ).strftime("%Y-%m-%d %H:%M:%S PKT")


STARTING_ACCOUNT_USD = 150.0
TRADE_NOTIONAL_USD = 100.0
TP_POLICY_START_MS = 1_791_201_600_000  # keep equal to trade_tracker.TP_POLICY_START_MS (positions after this book 50% at TP1)
PAPER_NOTIONAL_VERSION = "paper-notional-100-v1"
FRESH_START_VERSION = "zero-reset-v2-2-tiers"  # bump this to force another full Sheet clear
FRESH_START_MARKER = Path(__file__).resolve().parent.parent / "dashboard_reset.json"
PENDING_SHEET_FILE = Path(__file__).resolve().parent.parent / "sheet_sync_pending.json"
SIGNAL_HEADERS = ["Run ID", "Time (PKT)", "Source", "Coin", "Result", "Trade", "Market Price (USDT)", "Entry Price (USDT)", "Stop Loss (USDT)", "TP1 Price (USDT)", "TP1 Status", "TP2 Price (USDT)", "TP2 Status", "Position Status", "Position Size (USD)", "Quantity", "SL Risk (USD)", "TP1 Profit If Hit (USD)", "TP2 Profit If Hit (USD)", "Reason", "Signal ID"]
POSITION_HEADERS = ["Signal ID", "Opened (PKT)", "Source", "Coin", "Side", "Entry Price (USDT)", "Stop Loss (USDT)", "TP1 Price (USDT)", "TP1 Status", "TP2 Price (USDT)", "TP2 Status", "Position Status", "Latest Price (USDT)", "Last Checked (PKT)", "Exit Price (USDT)", "Closed (PKT)", "Position Size (USD)", "Quantity", "Realized P&L (USD)", "Unrealized P&L (USD)", "Return (%)"]
BACKTEST_HEADERS = ["Signal ID", "Opened (PKT)", "Last Checked (PKT)", "Closed (PKT)", "Source", "Coin", "Side", "Entry Price (USDT)", "Latest Price (USDT)", "Exit Price (USDT)", "Position Size (USD)", "Quantity", "Stop Loss (USDT)", "TP1 (USDT)", "TP1 Status", "TP2 (USDT)", "TP2 Status", "Final Status", "Realized P&L (USD)", "Unrealized P&L (USD)", "Return (%)"]
HEALTH_HEADERS = ["Run ID", "Time (PKT)", "Source", "Coin", "Severity", "Category", "Diagnostics"]
DASHBOARD_ROWS = [
    ["DeepPocket · $100 Paper-Trade Dashboard", "Value"],
    ["Starting account (USD)", STARTING_ACCOUNT_USD],
    ["Paper notional per trade (USD)", TRADE_NOTIONAL_USD],
    ["Open positions", '=COUNTIF(Backtesting!R:R,"OPEN")+COUNTIF(Backtesting!R:R,"TP1_HIT")'],
    ["Closed positions", '=COUNTIF(Backtesting!R:R,"TP2_HIT")+COUNTIF(Backtesting!R:R,"TP3_HIT")+COUNTIF(Backtesting!R:R,"SL_HIT")+COUNTIF(Backtesting!R:R,"BE_HIT")'],
    ["Break-even exits", '=COUNTIF(Backtesting!R:R,"BE_HIT")'],
    ["TP1 touched", '=COUNTIF(Backtesting!O:O,"HIT")'],
    ["TP2 reached", '=COUNTIF(Backtesting!Q:Q,"HIT")'],
    ["Stop losses hit", '=COUNTIF(Backtesting!R:R,"SL_HIT")'],
    ["Winning closed trades", '=COUNTIFS(Backtesting!R:R,"TP2_HIT",Backtesting!S:S,">0")+COUNTIFS(Backtesting!R:R,"TP3_HIT",Backtesting!S:S,">0")+COUNTIFS(Backtesting!R:R,"BE_HIT",Backtesting!S:S,">0")'],
    ["Losing closed trades", '=COUNTIFS(Backtesting!R:R,"SL_HIT",Backtesting!S:S,"<0")'],
    ["Win rate (closed trades with P&L)", '=IFERROR(B10/(B10+B11),0)'],
    ["Realized P&L · all time (USD)", '=SUM(Backtesting!S:S)'],
    ["Realized P&L · this month (USD)", '=SUMIF(Backtesting!D:D,TEXT(TODAY(),"yyyy-mm")&"*",Backtesting!S:S)'],
    ["Closed trades · this month", '=COUNTIF(Backtesting!D:D,TEXT(TODAY(),"yyyy-mm")&"*")'],
    ["Unrealized P&L · open (USD)", '=SUM(Backtesting!T:T)'],
    ["Estimated equity (USD)", '=B2+B13+B16'],
    ["Open paper exposure (USD)", '=B4*B3'],
    ["Monthly return on $150 account", '=IFERROR(B14/B2,0)'],
    ["Estimated TP1 P&L if all signal targets hit (USD)", '=SUMIFS(Signals!R:R,Signals!E:E,"SIGNAL")'],
    ["Estimated TP2 P&L if all signal targets hit (USD)", '=SUMIFS(Signals!S:S,Signals!E:E,"SIGNAL")'],
    ["Watch-only strategy signals", '=COUNTIFS(Signals!C:C,"STRATEGY · WATCH ONLY · *",Signals!E:E,"SIGNAL")'],
    ["Watch-only SL hits", '=COUNTIFS(Backtesting!E:E,"STRATEGY · WATCH ONLY · *",Backtesting!R:R,"SL_HIT")'],
    ["SL hits by signal family", "Count"],
    ["Whale Volume · SL hits", '=COUNTIFS(Backtesting!E:E,"WHALE VOLUME*",Backtesting!R:R,"SL_HIT")'],
    ["Whale Flow · SL hits", '=COUNTIFS(Backtesting!E:E,"WHALE FLOW*",Backtesting!R:R,"SL_HIT")'],
    ["Trend Momentum · SL hits", '=COUNTIFS(Backtesting!E:E,"TREND MOMENTUM*",Backtesting!R:R,"SL_HIT")'],
    ["Movers Pullback · signals", '=COUNTIFS(Signals!C:C,"MOVERS PULLBACK*",Signals!E:E,"SIGNAL")'],
    ["Movers Pullback · SL hits", '=COUNTIFS(Backtesting!E:E,"MOVERS PULLBACK*",Backtesting!R:R,"SL_HIT")'],
    ["Movers Order Block · signals", '=COUNTIFS(Signals!C:C,"MOVERS ORDER BLOCK*",Signals!E:E,"SIGNAL")'],
    ["Movers Order Block · SL hits", '=COUNTIFS(Backtesting!E:E,"MOVERS ORDER BLOCK*",Backtesting!R:R,"SL_HIT")'],
    ["Strategies · SL hits", '=COUNTIFS(Backtesting!E:E,"STRATEGY · *",Backtesting!R:R,"SL_HIT")'],
    ["SL hit signals by strategy (consensus membership)", "Count"],
]
try:
    _strategy_profiles = json.loads((Path(__file__).resolve().parent.parent / "strategies" / "strategy_profiles.json").read_text(encoding="utf-8")).get("profiles", [])
except (OSError, ValueError, TypeError):
    _strategy_profiles = []
for _profile in _strategy_profiles:
    _strategy_name = str(_profile.get("name", "")).strip()
    if _strategy_name:
        _criterion = _strategy_name.replace('"', '""')
        DASHBOARD_ROWS.append([
            f"{_strategy_name} · SL hit signals containing strategy",
            f'=COUNTIFS(Backtesting!E:E,"*{_criterion}*",Backtesting!R:R,"SL_HIT")',
        ])
DASHBOARD_ROWS.extend([
    ["Model assumptions", "$100 fixed notional per paper trade; $150 starting account reference. Multiple positions can exceed account exposure. TP1/TP2 are gross estimates; fees, funding, slippage, and exchange execution are excluded."],
    ["Important", "These are candle-based paper results, not exchange fills or guaranteed revenue."],
    ["Paper trade sizing update", PAPER_NOTIONAL_VERSION],
])

# ---------------------------------------------------------------------------------
# "Trades by strategy" table: lives in Dashboard columns D:L next to the existing A:B
# KPIs, so nothing that is already on the Dashboard is moved or deleted.
# ---------------------------------------------------------------------------------
STRATEGY_TABLE_HEADERS = ["Trades by strategy / source", "Trades", "Open", "TP1 touched", "TP2 hit",
                          "SL hit", "Break-even exits", "TP1 hit rate (TP1 ÷ (TP1+SL))", "Realized P&L (USD)"]
STRATEGY_TABLE_GROUPS = [
    ("Smart Vector · solo", "STRATEGY · Smart Vector"),
    ("Smart Vector · inside any consensus", "STRATEGY · *Consensus:*Smart Vector*"),
    ("Smart Vector · TOTAL (solo + consensus)", "STRATEGY · *Smart Vector*"),
    ("All consensus (watch-only)", "STRATEGY · *Consensus:*"),
    ("Consensus WITHOUT Smart Vector", "DIFF"),
    ("Whale Flow · on-chain DEX", "WHALE FLOW*"),
    ("Whale Volume", "WHALE VOLUME*"),
    ("Movers Pullback", "MOVERS PULLBACK*"),
    ("Movers Order Block", "MOVERS ORDER BLOCK*"),
    ("Trend Momentum", "TREND MOMENTUM*"),
    ("ALL SOURCES", "*"),
]


def _strategy_table_rows() -> list[list[Any]]:
    """Columns: D label | E trades | F open | G TP1 touched | H TP2 hit | I SL hit | J BE | K TP1 rate | L P&L."""
    rows: list[list[Any]] = [list(STRATEGY_TABLE_HEADERS)]
    bt = "Backtesting"
    sheet_row = {label: index for index, (label, _) in enumerate(STRATEGY_TABLE_GROUPS, start=2)}
    all_consensus = sheet_row["All consensus (watch-only)"]
    sv_consensus = sheet_row["Smart Vector · inside any consensus"]
    for label, pattern in STRATEGY_TABLE_GROUPS:
        r = sheet_row[label]
        if pattern == "DIFF":   # all consensus minus the consensus rows that contain Smart Vector
            counts = [f"={col}{all_consensus}-{col}{sv_consensus}" for col in "EFGHIJ"]
            pnl = f"=L{all_consensus}-L{sv_consensus}"
        elif label == "Smart Vector · solo":
            # Count both live ("STRATEGY · Smart Vector") and watch-only ("STRATEGY · WATCH ONLY · Smart Vector") solo rows
            e1 = f'{bt}!E:E,"STRATEGY · Smart Vector"'
            e2 = f'{bt}!E:E,"STRATEGY · WATCH ONLY · Smart Vector"'
            counts = [
                f'=COUNTIFS({e1})+COUNTIFS({e2})',
                f'=COUNTIFS({e1},{bt}!R:R,"OPEN")+COUNTIFS({e1},{bt}!R:R,"TP1_HIT")+COUNTIFS({e2},{bt}!R:R,"OPEN")+COUNTIFS({e2},{bt}!R:R,"TP1_HIT")',
                f'=COUNTIFS({e1},{bt}!O:O,"HIT")+COUNTIFS({e2},{bt}!O:O,"HIT")',
                f'=COUNTIFS({e1},{bt}!Q:Q,"HIT")+COUNTIFS({e2},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({e1},{bt}!R:R,"SL_HIT")+COUNTIFS({e2},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({e1},{bt}!R:R,"BE_HIT")+COUNTIFS({e2},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{e1})+SUMIFS({bt}!S:S,{e2})'
        else:
            crit = pattern.replace('"', '""')
            e = f'{bt}!E:E,"{crit}"'
            counts = [
                f'=COUNTIFS({e})',
                f'=COUNTIFS({e},{bt}!R:R,"OPEN")+COUNTIFS({e},{bt}!R:R,"TP1_HIT")',
                f'=COUNTIFS({e},{bt}!O:O,"HIT")',
                f'=COUNTIFS({e},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({e},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({e},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{e})'
        rows.append([label] + counts + [f'=IFERROR(G{r}/(G{r}+I{r}),"-")', pnl])
    return rows


STRATEGY_TABLE_ROWS = _strategy_table_rows()

# ---------------------------------------------------------------------------------
# Lower Dashboard Section (D15:L21): Directional (LONG vs SHORT) & Execution Mode Breakdown
# plus live visual sparklines right below the Strategy Scoreboard on the Dashboard tab.
# ---------------------------------------------------------------------------------
DASHBOARD_LOWER_START_ROW = 15


def _dashboard_lower_section_rows() -> list[list[Any]]:
    bt = "Backtesting"
    headers = [
        "Execution Breakdown · Direction & Mode", "Trades", "Open", "TP1 touched", "TP2 hit",
        "SL hit", "Break-even exits", "TP1 hit rate (TP1 ÷ (TP1+SL))", "Realized P&L (USD)"
    ]
    groups = [
        ("BUY / LONG Setups", f'{bt}!G:G,"BUY"'),
        ("SELL / SHORT Setups", f'{bt}!G:G,"SELL"'),
        ("Watch-Only Forward Tests", f'{bt}!E:E,"*WATCH ONLY*"'),
        ("Live / Active Setups", "LIVE_DIFF"),
        ("Visual Bar · TP1 Hits (Green) vs SL Hits (Red)", "SPARKLINE"),
    ]
    rows: list[list[Any]] = [headers]
    for offset, (label, spec) in enumerate(groups, start=DASHBOARD_LOWER_START_ROW + 1):
        if spec == "LIVE_DIFF":
            # Row 12 is ALL SOURCES in STRATEGY_TABLE_ROWS; Row 18 is Watch-Only Forward Tests
            wo_row = DASHBOARD_LOWER_START_ROW + 3
            counts = [f"={col}12-{col}{wo_row}" for col in "EFGHIJ"]
            pnl = f"=L12-L{wo_row}"
            rows.append([label] + counts + [f'=IFERROR(G{offset}/(G{offset}+I{offset}),"-")', pnl])
        elif spec == "SPARKLINE":
            rows.append([
                label,
                '=SPARKLINE({G12,I12},{"charttype","bar";"color1","#1E8E3E";"color2","#D93025"})',
                '=SPARKLINE({H12,J12,I12},{"charttype","bar";"color1","#1E8E3E";"color2","#1A73E8";"color3","#D93025"})',
                "Green = TP1 Reached",
                "Blue = Break-Even Saved",
                "Red = Stop-Loss Hit",
                '=IFERROR(G12/(G12+I12),0)',
                "Net P&L:",
                "=L12",
            ])
        else:
            counts = [
                f'=COUNTIFS({spec})',
                f'=COUNTIFS({spec},{bt}!R:R,"OPEN")+COUNTIFS({spec},{bt}!R:R,"TP1_HIT")',
                f'=COUNTIFS({spec},{bt}!O:O,"HIT")',
                f'=COUNTIFS({spec},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({spec},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({spec},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{spec})'
            rows.append([label] + counts + [f'=IFERROR(G{offset}/(G{offset}+I{offset}),"-")', pnl])
    return rows


DASHBOARD_LOWER_ROWS = _dashboard_lower_section_rows()

# ---------------------------------------------------------------------------------
# Dedicated "Analytics" tab: Full 14-column Institutional Strategy Evaluation Table
# (including Gross Profit, Gross Loss, Avg P&L, Profit Factor, and Automated Verdict)
# plus Outcome Distribution & Direction tables that power 3 native Google Sheets charts.
# ---------------------------------------------------------------------------------
ANALYTICS_STRATEGY_HEADERS = [
    "Trades by strategy / source", "Trades", "Open", "TP1 touched", "TP2 hit",
    "SL hit", "Break-even exits", "TP1 hit rate (TP1 ÷ (TP1+SL))", "Realized P&L (USD)",
    "Gross profit (USD)", "Gross loss (USD)", "Avg P&L / closed trade",
    "Profit factor", "Verdict (after 20+ closed trades)",
]


def _analytics_strategy_rows() -> list[list[Any]]:
    rows: list[list[Any]] = [list(ANALYTICS_STRATEGY_HEADERS)]
    bt = "Backtesting"
    sheet_row = {label: index for index, (label, _) in enumerate(STRATEGY_TABLE_GROUPS, start=2)}
    for label, pattern in STRATEGY_TABLE_GROUPS:
        r = sheet_row[label]
        if pattern == "DIFF":
            all_c = f'{bt}!E:E,"STRATEGY · *Consensus:*"'
            sv_c = f'{bt}!E:E,"STRATEGY · *Consensus:*Smart Vector*"'
            counts = [
                f'=COUNTIFS({all_c})-COUNTIFS({sv_c})',
                f'=(COUNTIFS({all_c},{bt}!R:R,"OPEN")+COUNTIFS({all_c},{bt}!R:R,"TP1_HIT"))-(COUNTIFS({sv_c},{bt}!R:R,"OPEN")+COUNTIFS({sv_c},{bt}!R:R,"TP1_HIT"))',
                f'=COUNTIFS({all_c},{bt}!O:O,"HIT")-COUNTIFS({sv_c},{bt}!O:O,"HIT")',
                f'=COUNTIFS({all_c},{bt}!Q:Q,"HIT")-COUNTIFS({sv_c},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({all_c},{bt}!R:R,"SL_HIT")-COUNTIFS({sv_c},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({all_c},{bt}!R:R,"BE_HIT")-COUNTIFS({sv_c},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{all_c})-SUMIFS({bt}!S:S,{sv_c})'
            gross_win = f'=SUMIFS({bt}!S:S,{all_c},{bt}!S:S,">0")-SUMIFS({bt}!S:S,{sv_c},{bt}!S:S,">0")'
            gross_loss = f'=ABS(SUMIFS({bt}!S:S,{all_c},{bt}!S:S,"<0")-SUMIFS({bt}!S:S,{sv_c},{bt}!S:S,"<0"))'
        elif label == "Smart Vector · solo":
            e1 = f'{bt}!E:E,"STRATEGY · Smart Vector"'
            e2 = f'{bt}!E:E,"STRATEGY · WATCH ONLY · Smart Vector"'
            counts = [
                f'=COUNTIFS({e1})+COUNTIFS({e2})',
                f'=COUNTIFS({e1},{bt}!R:R,"OPEN")+COUNTIFS({e1},{bt}!R:R,"TP1_HIT")+COUNTIFS({e2},{bt}!R:R,"OPEN")+COUNTIFS({e2},{bt}!R:R,"TP1_HIT")',
                f'=COUNTIFS({e1},{bt}!O:O,"HIT")+COUNTIFS({e2},{bt}!O:O,"HIT")',
                f'=COUNTIFS({e1},{bt}!Q:Q,"HIT")+COUNTIFS({e2},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({e1},{bt}!R:R,"SL_HIT")+COUNTIFS({e2},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({e1},{bt}!R:R,"BE_HIT")+COUNTIFS({e2},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{e1})+SUMIFS({bt}!S:S,{e2})'
            gross_win = f'=SUMIFS({bt}!S:S,{e1},{bt}!S:S,">0")+SUMIFS({bt}!S:S,{e2},{bt}!S:S,">0")'
            gross_loss = f'=ABS(SUMIFS({bt}!S:S,{e1},{bt}!S:S,"<0")+SUMIFS({bt}!S:S,{e2},{bt}!S:S,"<0"))'
        else:
            crit = pattern.replace('"', '""')
            e = f'{bt}!E:E,"{crit}"'
            counts = [
                f'=COUNTIFS({e})',
                f'=COUNTIFS({e},{bt}!R:R,"OPEN")+COUNTIFS({e},{bt}!R:R,"TP1_HIT")',
                f'=COUNTIFS({e},{bt}!O:O,"HIT")',
                f'=COUNTIFS({e},{bt}!Q:Q,"HIT")',
                f'=COUNTIFS({e},{bt}!R:R,"SL_HIT")',
                f'=COUNTIFS({e},{bt}!R:R,"BE_HIT")',
            ]
            pnl = f'=SUMIFS({bt}!S:S,{e})'
            gross_win = f'=SUMIFS({bt}!S:S,{e},{bt}!S:S,">0")'
            gross_loss = f'=ABS(SUMIFS({bt}!S:S,{e},{bt}!S:S,"<0"))'

        tp1_rate = f'=IFERROR(D{r}/(D{r}+F{r}),0)'
        avg_pnl = f'=IFERROR(I{r}/(E{r}+F{r}+G{r}),0)'
        profit_factor = f'=IF(K{r}=0,IF(J{r}>0,9.99,0),ROUND(J{r}/K{r},2))'
        verdict = (
            f'=IF((E{r}+F{r}+G{r})<20,"⏳ NEED 20+ CLOSED ("&(E{r}+F{r}+G{r})&"/20)",'
            f'IF(AND(M{r}>=1.3,H{r}>=0.55),"✅ KEEP / PROMOTE TO LIVE",'
            f'IF(M{r}>=1.0,"🟡 WATCH ONLY · MARGINAL","🛑 DROP / SUSPEND")))'
        )
        rows.append([label] + counts + [tp1_rate, pnl, gross_win, gross_loss, avg_pnl, profit_factor, verdict])
    return rows


ANALYTICS_STRATEGY_ROWS = _analytics_strategy_rows()


HISTORICAL_BACKTEST_START_ROW = 22
HISTORICAL_BACKTEST_HEADERS = [
    "Previous Candles Backtest (Auto-Selector · All 15 Strategies)",
    "Past Trades",
    "Wins (TP Hit)",
    "SL Hits",
    "Win Rate (%)",
    "Profit Factor",
    "Max Drawdown (%)",
    "Net Return ($100/trade)",
    "Auto-Selector Status (LIVE vs KICKED OUT)",
]


def build_historical_backtest_rows(rankings: dict[str, Any] | None = None) -> list[list[Any]]:
    """Build a 9-column table of all 13 strategies evaluated on previous closed candles
    for the Dashboard (D22:L36) and Analytics (A22:I36) tabs."""
    if rankings is None:
        try:
            bt_path = Path(__file__).resolve().parent.parent / "backtest_rankings.json"
            if bt_path.exists():
                rankings = json.loads(bt_path.read_text(encoding="utf-8")).get("rankings") or {}
            else:
                rankings = {}
        except Exception:
            rankings = {}

    rows: list[list[Any]] = [list(HISTORICAL_BACKTEST_HEADERS)]
    if not rankings:
        rows.append([
            "Waiting for first scan to populate historical candle backtest...",
            0, 0, 0, "0.0%", 0.0, "0.0%", 0.0, "⏳ PENDING FIRST SCAN",
        ])
        return rows

    sorted_items = sorted(
        rankings.values(),
        key=lambda x: (x.get("approved", False), x.get("profit_factor", 0.0), x.get("net_pnl_usd", 0.0)),
        reverse=True,
    )
    for st in sorted_items:
        rows.append([
            str(st.get("strategy", "")),
            int(st.get("trades", 0)),
            int(st.get("wins", 0)),
            int(st.get("losses", 0)),
            round(float(st.get("win_rate_pct", 0.0)) / 100.0, 4),
            round(float(st.get("profit_factor", 0.0)), 2),
            round(float(st.get("max_drawdown_pct", 0.0)) / 100.0, 4),
            round(float(st.get("net_pnl_usd", 0.0)), 2),
            str(st.get("verdict", "")),
        ])
    return rows


def sync_historical_backtest_table(rankings: dict[str, Any] | None = None) -> bool:
    """Write the Previous Candles Backtest Auto-Selector table to Dashboard (D22:L) and Analytics (A22:I)."""
    if not _client():
        return False
    try:
        _ensure_tabs()
        bt_rows = build_historical_backtest_rows(rankings)
        dash_dest = quote(f"'Dashboard'!D{HISTORICAL_BACKTEST_START_ROW}", safe="!'")
        _request("PUT", f"values/{dash_dest}?valueInputOption=USER_ENTERED", json={"values": bt_rows})
        ana_dest = quote(f"'Analytics'!A{HISTORICAL_BACKTEST_START_ROW}", safe="!'")
        _request("PUT", f"values/{ana_dest}?valueInputOption=USER_ENTERED", json={"values": bt_rows})
        return True
    except Exception as exc:
        print(f"[google-sheets] historical backtest table sync warning: {exc}")
        return False

ANALYTICS_OUTCOME_START_ROW = 15
ANALYTICS_OUTCOME_ROWS = [
    ["Outcome Category (Chart Data)", "Trade Count", "Share of Total", "Realized P&L (USD)", "",
     "Direction & Mode Summary", "Total Trades", "Open", "TP1 Hit", "TP2 Hit", "SL Hit", "Break-Even", "TP1 Accuracy", "Realized P&L (USD)"],
    ["TP2 Full Target Hit", '=COUNTIF(Backtesting!R:R,"TP2_HIT")', '=IFERROR(B16/SUM($B$16:$B$19),0)', '=SUMIFS(Backtesting!S:S,Backtesting!R:R,"TP2_HIT")', "",
     "BUY / LONG Setups", '=COUNTIF(Backtesting!G:G,"BUY")', '=COUNTIFS(Backtesting!G:G,"BUY",Backtesting!R:R,"OPEN")+COUNTIFS(Backtesting!G:G,"BUY",Backtesting!R:R,"TP1_HIT")',
     '=COUNTIFS(Backtesting!G:G,"BUY",Backtesting!O:O,"HIT")', '=COUNTIFS(Backtesting!G:G,"BUY",Backtesting!Q:Q,"HIT")',
     '=COUNTIFS(Backtesting!G:G,"BUY",Backtesting!R:R,"SL_HIT")', '=COUNTIFS(Backtesting!G:G,"BUY",Backtesting!R:R,"BE_HIT")',
     '=IFERROR(I16/(I16+K16),0)', '=SUMIFS(Backtesting!S:S,Backtesting!G:G,"BUY")'],
    ["TP1 + Break-Even Protected", '=COUNTIF(Backtesting!R:R,"BE_HIT")', '=IFERROR(B17/SUM($B$16:$B$19),0)', '=SUMIFS(Backtesting!S:S,Backtesting!R:R,"BE_HIT")', "",
     "SELL / SHORT Setups", '=COUNTIF(Backtesting!G:G,"SELL")', '=COUNTIFS(Backtesting!G:G,"SELL",Backtesting!R:R,"OPEN")+COUNTIFS(Backtesting!G:G,"SELL",Backtesting!R:R,"TP1_HIT")',
     '=COUNTIFS(Backtesting!G:G,"SELL",Backtesting!O:O,"HIT")', '=COUNTIFS(Backtesting!G:G,"SELL",Backtesting!Q:Q,"HIT")',
     '=COUNTIFS(Backtesting!G:G,"SELL",Backtesting!R:R,"SL_HIT")', '=COUNTIFS(Backtesting!G:G,"SELL",Backtesting!R:R,"BE_HIT")',
     '=IFERROR(I17/(I17+K17),0)', '=SUMIFS(Backtesting!S:S,Backtesting!G:G,"SELL")'],
    ["Stop Loss (SL Hit)", '=COUNTIF(Backtesting!R:R,"SL_HIT")', '=IFERROR(B18/SUM($B$16:$B$19),0)', '=SUMIFS(Backtesting!S:S,Backtesting!R:R,"SL_HIT")', "",
     "Watch-Only Forward Tests", '=COUNTIF(Backtesting!E:E,"*WATCH ONLY*")', '=COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!R:R,"OPEN")+COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!R:R,"TP1_HIT")',
     '=COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!O:O,"HIT")', '=COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!Q:Q,"HIT")',
     '=COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!R:R,"SL_HIT")', '=COUNTIFS(Backtesting!E:E,"*WATCH ONLY*",Backtesting!R:R,"BE_HIT")',
     '=IFERROR(I18/(I18+K18),0)', '=SUMIFS(Backtesting!S:S,Backtesting!E:E,"*WATCH ONLY*")'],
    ["Open / In-Progress", '=COUNTIF(Backtesting!R:R,"OPEN")+COUNTIF(Backtesting!R:R,"TP1_HIT")', '=IFERROR(B19/SUM($B$16:$B$19),0)', '=SUM(Backtesting!T:T)', "",
     "Live / Active Setups", '=B12-G18', '=C12-H18', '=D12-I18', '=E12-J18', '=F12-K18', '=G12-L18',
     '=IFERROR(I19/(I19+K19),0)', '=I12-N18'],
]

_session: dict[str, Any] | None = None
_sheet_ids: dict[str, int] = {}
_legacy_migration_checked = False
_tabs_ready = False
_notional_migration_checked = False
_batch_sync: dict[str, Any] | None = None
_fresh_start_performed = False


def _begin_batch_sync() -> dict[str, Any]:
    """Read the three ledgers once so a scan can sync hundreds of rows cheaply."""
    tables = {}
    for tab in ("Signals", "Positions", "Backtesting"):
        target = quote(f"'{tab}'!A1:U", safe="!'")
        tables[tab] = _request("GET", f"values/{target}").get("values", [])
    ids = {}
    for tab in ("Positions", "Backtesting"):
        ids[tab] = {str(row[0]): index + 1 for index, row in enumerate(tables[tab][1:], start=1)
                    if row and row[0]}
    return {"tables": tables, "ids": ids, "writes": {tab: {} for tab in tables}}


def _batch_write_row(tab: str, row_number: int, values: list[Any]) -> None:
    batch = _batch_sync
    if batch is None:
        raise RuntimeError("batch sheet sync is not active")
    table = batch["tables"][tab]
    while len(table) < row_number:
        table.append([])
    table[row_number - 1] = values
    batch["writes"][tab][row_number] = values
    if tab in batch["ids"] and values and values[0]:
        batch["ids"][tab][str(values[0])] = row_number


def _flush_batch_sync(batch: dict[str, Any]) -> None:
    """Apply ledger changes in bounded API batches, so a scan cannot half-sync from per-row calls."""
    writes = []
    end_columns = {"Signals": "U", "Positions": "U", "Backtesting": "U"}
    for tab, rows in batch["writes"].items():
        row_numbers = sorted(rows)
        if not row_numbers:
            continue
        first = previous = row_numbers[0]
        group = [rows[first]]
        for row_number in row_numbers[1:]:
            if row_number == previous + 1:
                group.append(rows[row_number])
            else:
                writes.append({"range": f"'{tab}'!A{first}:{end_columns[tab]}{previous}", "values": group})
                first, group = row_number, [rows[row_number]]
            previous = row_number
        writes.append({"range": f"'{tab}'!A{first}:{end_columns[tab]}{previous}", "values": group})

    chunk = []
    chunk_bytes = 0
    for item in writes:
        item_bytes = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if chunk and (len(chunk) >= 200 or chunk_bytes + item_bytes > 1_000_000):
            _request("POST", "values:batchUpdate", json={"valueInputOption": "RAW", "data": chunk})
            chunk, chunk_bytes = [], 0
        chunk.append(item)
        chunk_bytes += item_bytes
    if chunk:
        _request("POST", "values:batchUpdate", json={"valueInputOption": "RAW", "data": chunk})


def _batch_existing_row(tab: str, signal_id: str) -> tuple[int | None, list[Any]]:
    batch = _batch_sync
    if batch is None:
        return None, []
    row_number = batch["ids"][tab].get(str(signal_id))
    if row_number is None:
        return None, []
    table = batch["tables"][tab]
    return row_number, table[row_number - 1] if row_number <= len(table) else []


def _number(value, default=0.0):
    try:
        return float(str(value).replace(",", "").strip()) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _signed_pnl(side, entry, exit_price, quantity):
    return (exit_price - entry) * quantity if str(side).upper() == "BUY" else (entry - exit_price) * quantity


def _be_hit_realized_pnl(side: str, entry: float, tp1: float, exit_price: float, quantity: float, tp1_was_hit: bool) -> float:
    """Calculate realized P&L on BE_HIT: 50% booked at TP1 + 50% closed at exit_price when TP1 was reached."""
    if entry <= 0 or quantity <= 0:
        return 0.0
    eff_exit = exit_price if exit_price > 0 else entry
    if tp1_was_hit and tp1 > 0:
        return 0.5 * _signed_pnl(side, entry, tp1, quantity) + 0.5 * _signed_pnl(side, entry, eff_exit, quantity)
    return _signed_pnl(side, entry, eff_exit, quantity)


def _migrate_signal_row(old_headers, row):
    old = dict(zip(old_headers, row))
    action = str(old.get("Trade", "")).upper()
    action = {"LONG": "BUY", "SHORT": "SELL", "SHORT / SELL": "SELL"}.get(action, action)
    entry = _number(old.get("Entry Price (USDT)"), _number(old.get("Market Price (USDT)")))
    stop = _number(old.get("Stop Loss (USDT)"))
    tp1 = _number(old.get("TP1 Price (USDT)"))
    tp2 = _number(old.get("TP2 Price (USDT)"))
    trade_row = str(old.get("Result", "")) == "SIGNAL" and action in ("BUY", "SELL") and entry > 0
    size = TRADE_NOTIONAL_USD if trade_row else ""
    quantity = size / entry if trade_row else ""
    direction_sign = 1 if action == "BUY" else -1
    risk = abs(entry - stop) * quantity if trade_row and stop else ""
    tp1_pnl = (tp1 - entry) * quantity * direction_sign if trade_row and tp1 else ""
    tp2_pnl = (tp2 - entry) * quantity * direction_sign if trade_row and tp2 else ""
    return [old.get("Run ID", ""), old.get("Time (PKT)", ""), old.get("Source", ""), old.get("Coin", ""),
            old.get("Result", ""), action, old.get("Market Price (USDT)", ""), entry or "",
            old.get("Stop Loss (USDT)", ""), old.get("TP1 Price (USDT)", ""), old.get("TP1 Status", "N/A"),
            old.get("TP2 Price (USDT)", ""), old.get("TP2 Status", "N/A"), old.get("Position Status", ""),
            size, quantity, risk, tp1_pnl, tp2_pnl, old.get("Reason", ""), old.get("Signal ID", "")]


def _migrate_position_row(old_headers, row):
    old = dict(zip(old_headers, row))
    entry = _number(old.get("Entry Price (USDT)"))
    latest = _number(old.get("Latest Price (USDT)"), entry)
    exit_price = _number(old.get("Exit Price (USDT)"))
    tp1 = _number(old.get("TP1 Price (USDT)"))
    tp1_status = str(old.get("TP1 Status", "PENDING")).upper()
    side = str(old.get("Trade", "")).upper()
    side = {"LONG": "BUY", "SHORT": "SELL", "SHORT / SELL": "SELL"}.get(side, side)
    status = old.get("Position Status", "PENDING")
    quantity = TRADE_NOTIONAL_USD / entry if entry > 0 else 0
    closed = status in {"TP2_HIT", "TP3_HIT", "SL_HIT", "BE_HIT", "AMBIGUOUS", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"}
    if closed and entry and "AMBIGUOUS" not in status:
        if status == "BE_HIT":
            realized = _be_hit_realized_pnl(side, entry, tp1, exit_price, quantity, tp1_status == "HIT")
        elif exit_price:
            realized = _signed_pnl(side, entry, exit_price, quantity)
        else:
            realized = ""
    else:
        realized = ""
    unrealized = _signed_pnl(side, entry, latest, quantity) if entry and latest and not closed else ""
    ret = (realized if realized != "" else unrealized) / TRADE_NOTIONAL_USD if (realized != "" or unrealized != "") else ""
    return [old.get("Signal ID", ""), old.get("Created (PKT)", ""), old.get("Source", ""), old.get("Coin", ""),
            side, entry or "", old.get("Stop Loss (USDT)", ""), old.get("TP1 Price (USDT)", ""),
            old.get("TP1 Status", "PENDING"), old.get("TP2 Price (USDT)", ""), old.get("TP2 Status", "PENDING"),
            status, latest or "", old.get("Last Checked (PKT)", ""), old.get("Exit Price (USDT)", ""),
            old.get("Closed (PKT)", ""), TRADE_NOTIONAL_USD if entry else "", quantity or "", realized,
            unrealized, ret]


def _migrate_backtest_from_position(position_row):
    signal_id, opened, source, coin, side, entry, stop, tp1, tp1_status, tp2, tp2_status, status, latest, checked, exit_price, closed, size, quantity, realized, unrealized, ret = position_row
    return [signal_id, opened, checked, closed, source, coin, side, entry, latest, exit_price, size, quantity,
            stop, tp1, tp1_status, tp2, tp2_status, status, realized, unrealized, ret]


def _seed_backtesting_from_positions(position_rows):
    """Copy existing Positions into a newly added, still-empty Backtesting tab."""
    if "Backtesting" not in _sheet_ids:
        return
    rows = [row[:len(POSITION_HEADERS)] for row in position_rows
            if row and row[0] and str(row[0]) != POSITION_HEADERS[0]]
    if not rows:
        return
    backtest_range = quote("'Backtesting'!A1:Z", safe="!'")
    current = _request("GET", f"values/{backtest_range}").get("values", [])
    if len(current) > 1:
        return
    bt_rows = [_migrate_backtest_from_position(row + [""] * (len(POSITION_HEADERS) - len(row))) for row in rows]
    backtest_start = quote("'Backtesting'!A2", safe="!'")
    _request("PUT", f"values/{backtest_start}?valueInputOption=RAW", json={"values": bt_rows})


def _reconcile_backtesting_from_positions() -> None:
    """Keep every Positions signal represented with its latest state in Backtesting."""
    positions_range = quote("'Positions'!A1:U", safe="!'")
    backtest_range = quote("'Backtesting'!A1:U", safe="!'")
    position_rows = _request("GET", f"values/{positions_range}").get("values", [])
    backtest_rows = _request("GET", f"values/{backtest_range}").get("values", [])
    if len(position_rows) < 2:
        return

    def padded(row, width):
        return list(row[:width]) + [""] * max(0, width - len(row))

    existing = {}
    for index, row in enumerate(backtest_rows[1:], start=2):
        if row and row[0]:
            normalized = padded(row, len(BACKTEST_HEADERS))
            for column in (7, 8, 9, 10, 11, 12, 13, 15, 18, 19, 20):
                if normalized[column] not in (None, ""):
                    normalized[column] = _number(normalized[column])
            existing[str(row[0])] = (index, normalized)

    writes = []
    additions = []
    for row in position_rows[1:]:
        if not row or not row[0]:
            continue
        position = padded(row, len(POSITION_HEADERS))
        converted = _migrate_backtest_from_position(position)
        for column in (7, 8, 9, 10, 11, 12, 13, 15, 18, 19, 20):
            if converted[column] not in (None, ""):
                converted[column] = _number(converted[column])
        key = str(position[0])
        if key not in existing:
            additions.append(converted)
        else:
            row_number, previous = existing[key]
            if previous != converted:
                writes.append({"range": f"'Backtesting'!A{row_number}:U{row_number}", "values": [converted]})

    next_row = max(2, len(backtest_rows) + 1)
    for offset in range(0, len(additions), 250):
        chunk = additions[offset:offset + 250]
        first = next_row + offset
        last = first + len(chunk) - 1
        writes.append({"range": f"'Backtesting'!A{first}:U{last}", "values": chunk})
    if writes:
        _request("POST", "values:batchUpdate", json={"valueInputOption": "RAW", "data": writes})


def _reconcile_positions_from_signals() -> None:
    """Repair a position when its stable-ID TP/SL event reached Signals first."""
    signal_range = quote("'Signals'!A1:U", safe="!'")
    position_range = quote("'Positions'!A1:U", safe="!'")
    signal_rows = _request("GET", f"values/{signal_range}").get("values", [])
    position_rows = _request("GET", f"values/{position_range}").get("values", [])
    if len(signal_rows) < 2 or len(position_rows) < 2:
        return

    position_by_id = {str(row[0]): row for row in position_rows[1:] if row and row[0]}
    positions_without_id = [row for row in position_rows[1:] if row and row[0]]

    def same_price(first, second):
        left, right = _number(first, -1.0), _number(second, -2.0)
        return left > 0 and right > 0 and abs(left - right) <= max(abs(left), abs(right), 1.0) * 1e-8

    def uniquely_match_legacy_event(event):
        if len(event) < 12:
            return ""
        candidates = []
        event_time = str(event[1]) if len(event) > 1 else ""
        for position in positions_without_id:
            if len(position) < len(POSITION_HEADERS):
                position = list(position) + [""] * (len(POSITION_HEADERS) - len(position))
            if (str(event[2]) != str(position[2]) or str(event[3]) != str(position[3])
                    or str(event[5]).upper() != str(position[4]).upper()):
                continue
            opened = str(position[1] or "")
            if opened and event_time and event_time < opened:
                continue
            if all(same_price(event[e], position[p]) for e, p in ((7, 5), (8, 6), (9, 7), (11, 9))):
                candidates.append(str(position[0]))
        return candidates[0] if len(candidates) == 1 else ""

    outcomes_by_id: dict[str, list[list[Any]]] = {}
    valid_outcomes = {"TP1_HIT", "TP2_HIT", "SL_HIT", "BE_HIT", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"}
    for row in signal_rows[1:]:
        if len(row) <= 12 or str(row[4]) not in valid_outcomes:
            continue
        signal_id = str(row[20]) if len(row) > 20 and row[20] else uniquely_match_legacy_event(row)
        if signal_id and signal_id in position_by_id:
            outcomes_by_id.setdefault(signal_id, []).append(row)

    writes = []
    terminal_statuses = {"TP2_HIT", "SL_HIT", "BE_HIT", "AMBIGUOUS"}
    for row_number, raw_position in enumerate(position_rows[1:], start=2):
        if not raw_position or not raw_position[0]:
            continue
        events = outcomes_by_id.get(str(raw_position[0]), [])
        if not events:
            continue
        priority = {"TP1_HIT": 1, "TP2_HIT": 2, "SL_HIT": 2, "BE_HIT": 2,
                    "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE": 3}
        latest = max(events, key=lambda item: (str(item[1]) if len(item) > 1 else "",
                                               priority.get(str(item[4]), 0)))
        outcome = str(latest[4])
        event_time = str(latest[1]) if len(latest) > 1 else ""
        position = list(raw_position[:len(POSITION_HEADERS)]) + [""] * max(0, len(POSITION_HEADERS) - len(raw_position))
        current_status = str(position[11]).upper()
        current_closed = str(position[15] or "")
        if current_status in terminal_statuses and current_closed and event_time and current_closed > event_time:
            continue

        updated = list(position)
        entry = _number(updated[5])
        tp1_price = _number(updated[7])
        side = str(updated[4]).upper()
        size = _number(updated[16], TRADE_NOTIONAL_USD)
        quantity = _number(updated[17], size / entry if entry else 0.0)
        hit_price = _number(latest[6]) if len(latest) > 6 else 0.0
        tp1_hit = any(str(event[4]) in {"TP1_HIT", "TP2_HIT", "BE_HIT"} for event in events)
        tp2_hit = any(str(event[4]) == "TP2_HIT" for event in events)
        tp1_hit = tp1_hit or any(len(event) > 10 and str(event[10]).upper() == "HIT" for event in events)
        tp2_hit = tp2_hit or any(len(event) > 12 and str(event[12]).upper() == "HIT" for event in events)
        if tp1_hit:
            updated[8] = "HIT"
        if tp2_hit:
            updated[10] = "HIT"

        status_from_signal = "AMBIGUOUS" if "AMBIGUOUS" in outcome else outcome
        if outcome in {"SL_HIT", "TP2_HIT", "BE_HIT", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"}:
            updated[11] = status_from_signal
            if event_time:
                updated[15] = event_time
            if hit_price:
                updated[14] = hit_price
            if status_from_signal == "AMBIGUOUS":
                updated[18], updated[19], updated[20] = "", "", ""
            elif status_from_signal == "BE_HIT" and entry:
                existing_realized = _number(updated[18], 0.0)
                realized = existing_realized if existing_realized > 0 else _be_hit_realized_pnl(
                    side, entry, tp1_price, hit_price, quantity, tp1_hit or str(updated[8]).upper() == "HIT"
                )
                updated[18], updated[19] = realized, ""
                updated[20] = realized / size if size else ""
            elif entry and hit_price:
                realized = _signed_pnl(side, entry, hit_price, quantity)
                updated[18], updated[19] = realized, ""
                updated[20] = realized / size if size else ""
        elif outcome == "TP1_HIT" and current_status == "OPEN":
            updated[11] = "TP1_HIT"
        if event_time and (not updated[13] or str(updated[13]) < event_time):
            updated[13] = event_time
        if updated != position:
            writes.append({"range": f"'Positions'!A{row_number}:U{row_number}", "values": [updated]})

    for offset in range(0, len(writes), 200):
        _request("POST", "values:batchUpdate", json={"valueInputOption": "RAW", "data": writes[offset:offset + 200]})


def _migrate_tab(name, headers, converter=None):
    target = quote(f"'{name}'!A1:Z", safe="!'")
    values = _request("GET", f"values/{target}").get("values", [])
    old_headers = values[0] if values else []
    if old_headers == headers:
        if name == "Positions":
            _seed_backtesting_from_positions(values[1:])
        return values
    converted = []
    for row in values[1:]:
        if not any(str(value).strip() for value in row):
            continue
        if converter:
            converted.append(converter(old_headers, row))
        elif name == "Backtesting" and len(row) >= len(headers):
            converted.append(row[:len(headers)])
    clear_range = f"'{name}'!A1:Z"
    _request("POST", "values:batchClear", json={"ranges": [clear_range]})
    destination = quote(f"'{name}'!A1", safe="!'")
    _request("PUT", f"values/{destination}?valueInputOption=RAW",
             json={"values": [headers] + converted})
    if name == "Positions":
        _seed_backtesting_from_positions(converted)
    return [headers] + converted


def _migrate_paper_notional() -> None:
    """Recalculate existing paper rows once when the configured notional changes."""
    global _notional_migration_checked
    if _notional_migration_checked:
        return
    marker_row = len(DASHBOARD_ROWS)
    marker_target = quote(f"'Dashboard'!B{marker_row}", safe="!'")
    marker_values = _request("GET", f"values/{marker_target}").get("values", [])
    if marker_values and marker_values[0] and str(marker_values[0][0]) == PAPER_NOTIONAL_VERSION:
        _notional_migration_checked = True
        return

    def pad(row, width):
        return list(row[:width]) + [""] * max(0, width - len(row))

    def normalize_side(value):
        value = str(value).upper()
        return {"LONG": "BUY", "SHORT": "SELL", "SHORT / SELL": "SELL"}.get(value, value)

    def migrate_signal_rows(rows):
        output = []
        for source in rows[1:]:
            row = pad(source, len(SIGNAL_HEADERS))
            action, entry = normalize_side(row[5]), _number(row[7])
            row[5] = action
            if action in {"BUY", "SELL"} and entry > 0:
                quantity = TRADE_NOTIONAL_USD / entry
                sign = 1 if action == "BUY" else -1
                stop, tp1, tp2 = _number(row[8]), _number(row[9]), _number(row[11])
                row[14:19] = [TRADE_NOTIONAL_USD, quantity,
                              abs(entry - stop) * quantity if stop else "",
                              (tp1 - entry) * quantity * sign if tp1 else "",
                              (tp2 - entry) * quantity * sign if tp2 else ""]
            output.append(row)
        return output

    def migrate_position_rows(rows, is_backtest=False):
        width = len(BACKTEST_HEADERS if is_backtest else POSITION_HEADERS)
        output = []
        for source in rows[1:]:
            row = pad(source, width)
            if is_backtest:
                side, entry = normalize_side(row[6]), _number(row[7])
                row[6] = side
                latest, exit_price, status = _number(row[8]), _number(row[9]), str(row[17])
                tp1_price, tp1_status = _number(row[13]), str(row[14]).upper()
                size_index, quantity_index, realized_index, unrealized_index, return_index = 10, 11, 18, 19, 20
            else:
                side, entry = normalize_side(row[4]), _number(row[5])
                row[4] = side
                latest, exit_price, status = _number(row[12]), _number(row[14]), str(row[11])
                tp1_price, tp1_status = _number(row[7]), str(row[8]).upper()
                size_index, quantity_index, realized_index, unrealized_index, return_index = 16, 17, 18, 19, 20
            if side not in {"BUY", "SELL"} or entry <= 0:
                output.append(row)
                continue
            quantity = TRADE_NOTIONAL_USD / entry
            terminal = status in {"TP2_HIT", "TP3_HIT", "SL_HIT", "BE_HIT"}
            ambiguous = "AMBIGUOUS" in status
            if terminal and status == "BE_HIT":
                realized = _be_hit_realized_pnl(side, entry, tp1_price, exit_price, quantity, tp1_status == "HIT")
            elif terminal and exit_price:
                realized = _signed_pnl(side, entry, exit_price, quantity)
            else:
                realized = ""
            unrealized = _signed_pnl(side, entry, latest, quantity) if latest and not terminal and not ambiguous else ""
            basis = realized if realized != "" else unrealized
            row[size_index] = TRADE_NOTIONAL_USD
            row[quantity_index] = quantity
            row[realized_index] = realized
            row[unrealized_index] = unrealized
            row[return_index] = basis / TRADE_NOTIONAL_USD if basis != "" else ""
            output.append(row)
        return output

    tabs = (
        ("Signals", "U", migrate_signal_rows),
        ("Positions", "U", lambda rows: migrate_position_rows(rows, False)),
        ("Backtesting", "U", lambda rows: migrate_position_rows(rows, True)),
    )
    for tab, end_col, transform in tabs:
        target = quote(f"'{tab}'!A1:{end_col}", safe="!'")
        rows = _request("GET", f"values/{target}").get("values", [])
        converted = transform(rows)
        for offset in range(0, len(converted), 250):
            chunk = converted[offset:offset + 250]
            first_row = offset + 2
            last_row = first_row + len(chunk) - 1
            destination = quote(f"'{tab}'!A{first_row}:{end_col}{last_row}", safe="!'")
            _request("PUT", f"values/{destination}?valueInputOption=RAW", json={"values": chunk})
    _notional_migration_checked = True


def _client():
    global _session
    if _session:
        return _session
    spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "").strip()
    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not spreadsheet_id or not credentials_json:
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.service_account import Credentials
    credentials = Credentials.from_service_account_info(json.loads(credentials_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    credentials.refresh(Request())
    _session = {"id": spreadsheet_id, "credentials": credentials, "token": credentials.token, "headers": {"Authorization": f"Bearer {credentials.token}"}}
    return _session


def _url(path: str) -> str:
    client = _client()
    if not client:
        raise RuntimeError("Google Sheets credentials are not configured")
    separator = "" if path.startswith(":") else "/"
    return f"https://sheets.googleapis.com/v4/spreadsheets/{client['id']}{separator}{path}"


def _request(method: str, path: str, **kwargs):
    client = _client()
    if client["credentials"].expired:
        from google.auth.transport.requests import Request
        client["credentials"].refresh(Request())
        client["token"] = client["credentials"].token
        client["headers"] = {"Authorization": f"Bearer {client['token']}"}
    response = requests.request(method, _url(path), headers=client["headers"], timeout=20, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else {}


def _analytics_chart_requests(analytics_id: int, existing_charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build native Google Sheets chart requests for the Analytics tab (idempotent: replaces old charts cleanly)."""
    reqs: list[dict[str, Any]] = []
    for ch in existing_charts:
        cid = ch.get("chartId")
        if cid is not None:
            reqs.append({"deleteEmbeddedObject": {"objectId": cid}})

    # Chart 1: Column Chart of Realized P&L (USD) by Strategy / Source (Rows 1..11, Col A vs Col I)
    reqs.append({
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Realized P&L (USD) by Strategy & Signal Source",
                    "basicChart": {
                        "chartType": "COLUMN",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Strategy / Signal Source"},
                            {"position": "LEFT_AXIS", "title": "Realized P&L (USD)"},
                        ],
                        "domains": [{
                            "domain": {
                                "sourceRange": {
                                    "sources": [{
                                        "sheetId": analytics_id,
                                        "startRowIndex": 0,
                                        "endRowIndex": 11,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 1,
                                    }]
                                }
                            }
                        }],
                        "series": [{
                            "series": {
                                "sourceRange": {
                                    "sources": [{
                                        "sheetId": analytics_id,
                                        "startRowIndex": 0,
                                        "endRowIndex": 11,
                                        "startColumnIndex": 8,
                                        "endColumnIndex": 9,
                                    }]
                                }
                            },
                            "targetAxis": "LEFT_AXIS",
                        }],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": analytics_id, "rowIndex": 21, "columnIndex": 0},
                        "offsetXPixels": 8,
                        "offsetYPixels": 8,
                        "widthPixels": 620,
                        "heightPixels": 360,
                    }
                },
            }
        }
    })

    # Chart 2: Donut Pie Chart of Trade Outcome Distribution (Rows 15..19, Col A vs Col B)
    reqs.append({
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Trade Outcome Distribution (TP2 / Break-Even / SL / Open)",
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "pieHole": 0.45,
                        "domain": {
                            "sourceRange": {
                                "sources": [{
                                    "sheetId": analytics_id,
                                    "startRowIndex": 14,
                                    "endRowIndex": 19,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": 1,
                                }]
                            }
                        },
                        "series": {
                            "sourceRange": {
                                "sources": [{
                                    "sheetId": analytics_id,
                                    "startRowIndex": 14,
                                    "endRowIndex": 19,
                                    "startColumnIndex": 1,
                                    "endColumnIndex": 2,
                                }]
                            }
                        },
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": analytics_id, "rowIndex": 21, "columnIndex": 5},
                        "offsetXPixels": 8,
                        "offsetYPixels": 8,
                        "widthPixels": 560,
                        "heightPixels": 360,
                    }
                },
            }
        }
    })

    # Chart 3: Grouped Bar Chart comparing TP1 Touched vs SL Hit by Strategy (Rows 1..11, Col A vs Col D & Col F)
    reqs.append({
        "addChart": {
            "chart": {
                "spec": {
                    "title": "Strategy Win vs Stop-Out Comparison (TP1 Reached vs SL Hit)",
                    "basicChart": {
                        "chartType": "BAR",
                        "legendPosition": "BOTTOM_LEGEND",
                        "axis": [
                            {"position": "BOTTOM_AXIS", "title": "Number of Trades"},
                            {"position": "LEFT_AXIS", "title": "Strategy / Signal Source"},
                        ],
                        "domains": [{
                            "domain": {
                                "sourceRange": {
                                    "sources": [{
                                        "sheetId": analytics_id,
                                        "startRowIndex": 0,
                                        "endRowIndex": 11,
                                        "startColumnIndex": 0,
                                        "endColumnIndex": 1,
                                    }]
                                }
                            }
                        }],
                        "series": [
                            {
                                "series": {
                                    "sourceRange": {
                                        "sources": [{
                                            "sheetId": analytics_id,
                                            "startRowIndex": 0,
                                            "endRowIndex": 11,
                                            "startColumnIndex": 3,
                                            "endColumnIndex": 4,
                                        }]
                                    }
                                },
                                "targetAxis": "BOTTOM_AXIS",
                            },
                            {
                                "series": {
                                    "sourceRange": {
                                        "sources": [{
                                            "sheetId": analytics_id,
                                            "startRowIndex": 0,
                                            "endRowIndex": 11,
                                            "startColumnIndex": 5,
                                            "endColumnIndex": 6,
                                        }]
                                    }
                                },
                                "targetAxis": "BOTTOM_AXIS",
                            },
                        ],
                        "headerCount": 1,
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": analytics_id, "rowIndex": 40, "columnIndex": 0},
                        "offsetXPixels": 8,
                        "offsetYPixels": 8,
                        "widthPixels": 1180,
                        "heightPixels": 380,
                    }
                },
            }
        }
    })
    return reqs


def _ensure_tabs() -> None:
    global _sheet_ids, _tabs_ready, _fresh_start_performed
    if _tabs_ready:
        return
    metadata = _request("GET", "?fields=sheets(properties(sheetId,title,gridProperties(rowCount)),bandedRanges(bandedRangeId),conditionalFormats,charts(chartId))")
    sheet_metadata = {item["properties"]["title"]: item for item in metadata.get("sheets", [])}
    sheets = {name: item["properties"]["sheetId"] for name, item in sheet_metadata.items()}
    required_tabs = ("Signals", "Positions", "Backtesting", "Dashboard", "Analytics", "System Health")
    requests_to_add = [{"addSheet": {"properties": {"title": name}}} for name in required_tabs if name not in sheets]
    if requests_to_add:
        result = _request("POST", ":batchUpdate", json={"requests": requests_to_add})
        for item in result.get("replies", []):
            prop = item.get("addSheet", {}).get("properties", {})
            if prop.get("title"):
                sheets[prop["title"]] = prop["sheetId"]
        metadata = _request("GET", "?fields=sheets(properties(sheetId,title,gridProperties(rowCount)),bandedRanges(bandedRangeId),conditionalFormats,charts(chartId))")
        sheet_metadata = {item["properties"]["title"]: item for item in metadata.get("sheets", [])}
        sheets = {name: item["properties"]["sheetId"] for name, item in sheet_metadata.items()}
    _sheet_ids = {name: int(tab_id) for name, tab_id in sheets.items()}
    _reset_paper_ledgers_once()
    _migrate_tab("Backtesting", BACKTEST_HEADERS)
    _migrate_tab("Signals", SIGNAL_HEADERS, _migrate_signal_row)
    _migrate_tab("Positions", POSITION_HEADERS, _migrate_position_row)
    _migrate_tab("System Health", HEALTH_HEADERS)
    _move_legacy_health_rows()
    _migrate_paper_notional()
    _reconcile_positions_from_signals()
    _reconcile_backtesting_from_positions()

    # 1. Populate Dashboard (A:B KPIs, D1:L12 Strategy Scoreboard, D15:L20 Lower Execution Breakdown + Sparklines)
    dashboard_start = quote("'Dashboard'!A1", safe="!'")
    _request("PUT", f"values/{dashboard_start}?valueInputOption=USER_ENTERED", json={"values": DASHBOARD_ROWS})
    strategy_start = quote("'Dashboard'!D1", safe="!'")
    _request("PUT", f"values/{strategy_start}?valueInputOption=USER_ENTERED", json={"values": STRATEGY_TABLE_ROWS})
    lower_start = quote(f"'Dashboard'!D{DASHBOARD_LOWER_START_ROW}", safe="!'")
    _request("PUT", f"values/{lower_start}?valueInputOption=USER_ENTERED", json={"values": DASHBOARD_LOWER_ROWS})

    # 2. Populate Analytics Tab (A1:N12 Full Strategy Evaluation + Profit Factor + Verdict, A15:N19 Outcome & Direction Breakdown)
    analytics_start = quote("'Analytics'!A1", safe="!'")
    _request("PUT", f"values/{analytics_start}?valueInputOption=USER_ENTERED", json={"values": ANALYTICS_STRATEGY_ROWS})
    analytics_outcomes_start = quote(f"'Analytics'!A{ANALYTICS_OUTCOME_START_ROW}", safe="!'")
    _request("PUT", f"values/{analytics_outcomes_start}?valueInputOption=USER_ENTERED", json={"values": ANALYTICS_OUTCOME_ROWS})

    requests_to_format = []
    requests_to_format.append({"updateSpreadsheetProperties": {
        "properties": {"timeZone": "Asia/Karachi"}, "fields": "timeZone"
    }})
    sheet_layouts = (
        ("Signals", sheets["Signals"], [165, 190, 190, 105, 115, 90, 125, 125, 125, 125, 95, 125, 95, 120, 125, 125, 120, 155, 155, 330, 245]),
        ("Positions", sheets["Positions"], [210, 190, 200, 105, 85, 125, 125, 125, 95, 125, 95, 120, 125, 190, 125, 190, 125, 125, 140, 145, 105]),
        ("Backtesting", sheets["Backtesting"], [210, 190, 190, 190, 200, 105, 85, 125, 125, 125, 125, 125, 125, 125, 95, 125, 95, 135, 140, 145, 105]),
        ("System Health", sheets["System Health"], [155, 175, 150, 105, 105, 180, 680]),
    )
    for name, tab_id, widths in sheet_layouts:
        count = len(widths)
        requests_to_format.extend([
            {"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                    "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP",
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)"}},
            {"updateSheetProperties": {"properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
            {"setBasicFilter": {"filter": {"range": {"sheetId": tab_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": count}}}},
            {"updateDimensionProperties": {"range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 48}, "fields": "pixelSize"}},
            {"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 0},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP",
                    "textFormat": {"fontSize": 10}}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,textFormat.fontSize)"}},
        ])
        price_columns = ((6, 7, 8, 9, 11) if name == "Signals"
                         else (5, 6, 7, 9, 12, 14) if name == "Positions"
                         else (7, 8, 9, 12, 13, 15) if name == "Backtesting" else ())
        for index in price_columns:
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1,
                "startColumnIndex": index, "endColumnIndex": index + 1},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00####"}}},
                "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}})
        metric_columns = ((14, "#,##0.00"), (15, "#,##0.########"), (16, "#,##0.00"), (17, "#,##0.00"), (18, "#,##0.00")) if name == "Signals" else ((16, "#,##0.00"), (17, "#,##0.########"), (18, "#,##0.00"), (19, "#,##0.00"), (20, "0.00%")) if name == "Positions" else ((10, "#,##0.00"), (11, "#,##0.########"), (18, "#,##0.00"), (19, "#,##0.00"), (20, "0.00%")) if name == "Backtesting" else ()
        for index, pattern in metric_columns:
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1,
                "startColumnIndex": index, "endColumnIndex": index + 1},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT",
                    "numberFormat": {"type": "NUMBER", "pattern": pattern}}},
                "fields": "userEnteredFormat(horizontalAlignment,numberFormat)"}})
        for index, width in enumerate(widths):
            requests_to_format.append({"updateDimensionProperties": {"range": {"sheetId": tab_id, "dimension": "COLUMNS", "startIndex": index, "endIndex": index + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
        requests_to_format.append({"updateDimensionProperties": {"range": {"sheetId": tab_id, "dimension": "ROWS", "startIndex": 1,
            "endIndex": max(2, int(sheet_metadata[name]["properties"].get("gridProperties", {}).get("rowCount", 1000)))},
            "properties": {"pixelSize": 50 if name == "Signals" else 50 if name == "Positions" else 60}, "fields": "pixelSize"}})
        for index in ((4, 5, 10, 12, 13) if name == "Signals" else (4, 8, 10, 11) if name == "Positions" else (4, 5, 6, 14, 16, 17) if name == "Backtesting" else (2, 3, 4, 5)):
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": index, "endColumnIndex": index + 1}, "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}}, "fields": "userEnteredFormat.horizontalAlignment"}})
        for existing_band in sheet_metadata[name].get("bandedRanges", []):
            if existing_band.get("bandedRangeId") is not None:
                requests_to_format.append({"deleteBanding": {"bandedRangeId": existing_band["bandedRangeId"]}})
        requests_to_format.append({"addBanding": {"bandedRange": {"range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": count}, "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1}, "secondBandColor": {"red": 244 / 255, "green": 247 / 255, "blue": 251 / 255}}}}})
        if name == "System Health":
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 5, "endColumnIndex": 7},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}})
        elif name == "Signals":
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1,
                "startColumnIndex": 18, "endColumnIndex": 20},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}})
        elif name in ("Positions", "Backtesting"):
            requests_to_format.append({"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": count},
                "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}})

    green = ({"red": 0.84, "green": 0.94, "blue": 0.86}, {"red": 0.12, "green": 0.42, "blue": 0.2})
    blue = ({"red": 0.84, "green": 0.92, "blue": 0.99}, {"red": 0.12, "green": 0.32, "blue": 0.58})
    red = ({"red": 1, "green": 0.87, "blue": 0.87}, {"red": 0.65, "green": 0.12, "blue": 0.12})
    amber = ({"red": 1, "green": 0.95, "blue": 0.82}, {"red": 0.5, "green": 0.32, "blue": 0.02})
    neutral = ({"red": 0.93, "green": 0.95, "blue": 0.97}, {"red": 0.3, "green": 0.34, "blue": 0.39})
    rule_specs = []
    for value, colors in (("SIGNAL", green), ("NO_SIGNAL", neutral), ("NO_TRADE", neutral),
                          ("DATA_HEALTH_WARNING", amber), ("ANALYSIS_ERROR", red)):
        rule_specs.append(("Signals", 4, value, colors))
    for value, colors in (("BUY", green), ("SELL", red), ("PENDING", blue), ("NO TRADE", neutral), ("REVIEW", amber)):
        rule_specs.append(("Signals", 5, value, colors))
    for value, colors in (("PENDING", blue), ("TP1_HIT", blue), ("TP2_HIT", green),
                          ("SL_HIT", red), ("BE_HIT", neutral), ("AMBIGUOUS", amber), ("NO_SIGNAL", neutral)):
        rule_specs.append(("Signals", 13, value, colors))
        rule_specs.append(("Positions", 11, value, colors))
        rule_specs.append(("Backtesting", 17, value, colors))
    for column in (8, 10):
        rule_specs.extend(("Positions", column, value, colors)
                          for value, colors in (("PENDING", blue), ("HIT", green), ("N/A", neutral)))
    for column in (14, 16):
        rule_specs.extend(("Backtesting", column, value, colors)
                          for value, colors in (("PENDING", blue), ("HIT", green), ("N/A", neutral)))
    for value, colors in (("ACTION", red), ("REVIEW", amber), ("INFO", green)):
        rule_specs.append(("System Health", 4, value, colors))

    def has_rule(name, column, value):
        for existing_rule in sheet_metadata[name].get("conditionalFormats", []):
            condition = existing_rule.get("booleanRule", {}).get("condition", {})
            condition_values = [item.get("userEnteredValue") for item in condition.get("values", [])]
            if condition.get("type") != "TEXT_EQ" or value not in condition_values:
                continue
            if any(int(item.get("startColumnIndex", 0)) <= column < int(item.get("endColumnIndex", 0))
                   for item in existing_rule.get("ranges", [])):
                return True
        return False

    for name, column, value, colors in rule_specs:
        if has_rule(name, column, value):
            continue
        background, foreground = colors
        requests_to_format.append({"addConditionalFormatRule": {"rule": {
            "ranges": [{"sheetId": sheets[name], "startRowIndex": 1,
                        "startColumnIndex": column, "endColumnIndex": column + 1}],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]},
                            "format": {"backgroundColor": background,
                                       "textFormat": {"foregroundColor": foreground, "bold": True}}}},
            "index": 0}})

    # Format Dashboard tab (A:B KPIs, D1:L12 Strategy Table, D15:L20 Lower Breakdown Section)
    dashboard_id = sheets["Dashboard"]
    table_rows = len(STRATEGY_TABLE_ROWS)
    lower_start_idx = DASHBOARD_LOWER_START_ROW - 1
    lower_end_idx = lower_start_idx + len(DASHBOARD_LOWER_ROWS)
    requests_to_format.extend([
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 13},
                "verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 290}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 240}, "fields": "pixelSize"}},
        {"updateSheetProperties": {"properties": {"sheetId": dashboard_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": len(DASHBOARD_ROWS), "startColumnIndex": 0, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP", "padding": {"top": 8, "bottom": 8, "left": 10, "right": 10}}}, "fields": "userEnteredFormat(verticalAlignment,wrapStrategy,padding)"}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": dashboard_id, "dimension": "ROWS", "startIndex": 0, "endIndex": len(DASHBOARD_ROWS)}}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": len(DASHBOARD_ROWS), "startColumnIndex": 1, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"horizontalAlignment": "RIGHT", "textFormat": {"bold": True, "fontSize": 12}}}, "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 0, "endRowIndex": 1,
                                  "startColumnIndex": 3, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 11},
                "verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": table_rows,
                                  "startColumnIndex": 3, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER",
                "textFormat": {"fontSize": 11}}},
            "fields": "userEnteredFormat(verticalAlignment,horizontalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": table_rows,
                                  "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT", "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": table_rows,
                                  "startColumnIndex": 10, "endColumnIndex": 11},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": table_rows,
                                  "startColumnIndex": 11, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": table_rows - 1, "endRowIndex": table_rows,
                                  "startColumnIndex": 3, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 232 / 255, "green": 240 / 255, "blue": 254 / 255},
                                           "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
        # Lower breakdown section on Dashboard (D15:L20)
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": lower_start_idx, "endRowIndex": lower_start_idx + 1,
                                  "startColumnIndex": 3, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 11},
                "verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": lower_start_idx + 1, "endRowIndex": lower_end_idx,
                                  "startColumnIndex": 3, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER",
                "textFormat": {"fontSize": 11}}},
            "fields": "userEnteredFormat(verticalAlignment,horizontalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": lower_start_idx + 1, "endRowIndex": lower_end_idx,
                                  "startColumnIndex": 3, "endColumnIndex": 4},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT", "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": lower_start_idx + 1, "endRowIndex": lower_end_idx - 1,
                                  "startColumnIndex": 10, "endColumnIndex": 11},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": lower_start_idx + 1, "endRowIndex": lower_end_idx,
                                  "startColumnIndex": 11, "endColumnIndex": 12},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                                       "properties": {"pixelSize": 28}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard_id, "dimension": "COLUMNS", "startIndex": 3, "endIndex": 4},
                                       "properties": {"pixelSize": 300}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": dashboard_id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 12},
                                       "properties": {"pixelSize": 118}, "fields": "pixelSize"}},
    ])
    for existing_band in sheet_metadata["Dashboard"].get("bandedRanges", []):
        if existing_band.get("bandedRangeId") is not None:
            requests_to_format.append({"deleteBanding": {"bandedRangeId": existing_band["bandedRangeId"]}})
    requests_to_format.append({"addBanding": {"bandedRange": {"range": {"sheetId": dashboard_id, "startRowIndex": 1, "endRowIndex": len(DASHBOARD_ROWS), "startColumnIndex": 0, "endColumnIndex": 2}, "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1}, "secondBandColor": {"red": 244 / 255, "green": 247 / 255, "blue": 251 / 255}}}}})
    for row_index in (1, 2, 12, 13, 15, 16, 17, 19, 20):
        requests_to_format.append({"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 1, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}}, "fields": "userEnteredFormat.numberFormat"}})
    for row_index in (11, 18):
        requests_to_format.append({"repeatCell": {"range": {"sheetId": dashboard_id, "startRowIndex": row_index, "endRowIndex": row_index + 1, "startColumnIndex": 1, "endColumnIndex": 2}, "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}}, "fields": "userEnteredFormat.numberFormat"}})

    # Format Analytics tab (A1:N12 Strategy Table + A15:N19 Breakdown + 3 Native Charts)
    if "Analytics" in sheets:
        analytics_id = sheets["Analytics"]
        a_rows = len(ANALYTICS_STRATEGY_ROWS)
        for existing_band in sheet_metadata.get("Analytics", {}).get("bandedRanges", []):
            if existing_band.get("bandedRangeId") is not None:
                requests_to_format.append({"deleteBanding": {"bandedRangeId": existing_band["bandedRangeId"]}})
        requests_to_format.extend([
            {"updateSheetProperties": {"properties": {"sheetId": analytics_id, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 14},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 11},
                    "verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 0, "endColumnIndex": 14},
                "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "textFormat": {"fontSize": 10}}},
                "fields": "userEnteredFormat(verticalAlignment,horizontalAlignment,textFormat)"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 0, "endColumnIndex": 1},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "LEFT", "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(horizontalAlignment,textFormat)"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 7, "endColumnIndex": 8},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 8, "endColumnIndex": 12},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 12, "endColumnIndex": 13},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": a_rows - 1, "endRowIndex": a_rows, "startColumnIndex": 0, "endColumnIndex": 14},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 232 / 255, "green": 240 / 255, "blue": 254 / 255}, "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 14, "endRowIndex": 15, "startColumnIndex": 0, "endColumnIndex": 14},
                "cell": {"userEnteredFormat": {"backgroundColor": {"red": 19 / 255, "green": 41 / 255, "blue": 75 / 255},
                    "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True, "fontSize": 10},
                    "verticalAlignment": "MIDDLE", "horizontalAlignment": "CENTER", "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 15, "endRowIndex": 19, "startColumnIndex": 2, "endColumnIndex": 3},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 15, "endRowIndex": 19, "startColumnIndex": 3, "endColumnIndex": 4},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 15, "endRowIndex": 19, "startColumnIndex": 12, "endColumnIndex": 13},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"repeatCell": {"range": {"sheetId": analytics_id, "startRowIndex": 15, "endRowIndex": 19, "startColumnIndex": 13, "endColumnIndex": 14},
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "$#,##0.00;-$#,##0.00"}}},
                "fields": "userEnteredFormat.numberFormat"}},
            {"updateDimensionProperties": {"range": {"sheetId": analytics_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": analytics_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 13}, "properties": {"pixelSize": 115}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": analytics_id, "dimension": "COLUMNS", "startIndex": 13, "endIndex": 14}, "properties": {"pixelSize": 230}, "fields": "pixelSize"}},
            {"addBanding": {"bandedRange": {"range": {"sheetId": analytics_id, "startRowIndex": 1, "endRowIndex": a_rows, "startColumnIndex": 0, "endColumnIndex": 14},
                "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1}, "secondBandColor": {"red": 244 / 255, "green": 247 / 255, "blue": 251 / 255}}}}},
        ])
        existing_charts = sheet_metadata.get("Analytics", {}).get("charts", [])
        if not existing_charts:
            requests_to_format.extend(_analytics_chart_requests(analytics_id, existing_charts))

    for name in ("Signals", "Backtesting", "System Health"):
        requests_to_format.append({"updateSheetProperties": {
            "properties": {"sheetId": sheets[name], "hidden": True}, "fields": "hidden"
        }})
    for name in ("Positions", "Dashboard", "Analytics"):
        if name in sheets:
            requests_to_format.append({"updateSheetProperties": {
                "properties": {"sheetId": sheets[name], "hidden": False}, "fields": "hidden"
            }})
    _request("POST", ":batchUpdate", json={"requests": requests_to_format})
    _tabs_ready = True


def _fresh_marker_current() -> bool:
    """True only when the marker was written by this reset version."""
    if not FRESH_START_MARKER.exists():
        return False
    try:
        data = json.loads(FRESH_START_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("reset_version") == FRESH_START_VERSION


def _reset_paper_ledgers_once() -> bool:
    """Clear the previous paper-test period once, preserving sheet formatting."""
    global _fresh_start_performed
    if _fresh_marker_current():
        return False
    reset_ranges = [f"'{name}'!A2:Z"
                    for name in ("Signals", "Positions", "Backtesting", "System Health")]
    reset_ranges.append("'Dashboard'!A1:Z")
    if "Analytics" in _sheet_ids:
        reset_ranges.append("'Analytics'!A1:Z")
    _request("POST", "values:batchClear", json={"ranges": reset_ranges})
    if PENDING_SHEET_FILE.exists():
        PENDING_SHEET_FILE.unlink()
    marker_tmp = FRESH_START_MARKER.with_suffix(".json.tmp")
    marker_tmp.write_text(json.dumps({"reset_version": FRESH_START_VERSION,
                                      "reset_at_utc": datetime.now(timezone.utc).isoformat()}),
                          encoding="utf-8")
    os.replace(marker_tmp, FRESH_START_MARKER)
    _fresh_start_performed = True
    return True


def initialize_fresh_dashboard() -> bool:
    """Ensure the one-time clean-start reset runs before positions are settled."""
    if _tabs_ready:
        return _fresh_start_performed
    _ensure_tabs()
    return _fresh_start_performed


def _append(tab: str, values: list[Any]) -> None:
    target = quote(f"'{tab}'!A1", safe="!'")
    result = _request("POST", f"values/{target}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS", json={"values": [values]})
    updated_range = result.get("updates", {}).get("updatedRange", "")
    match = re.search(r"![A-Z]+(\d+):[A-Z]+(\d+)$", updated_range)
    if match:
        _fit_rows(tab, int(match.group(1)) - 1, int(match.group(2)))


def _upsert_scan_row(row: list[Any]) -> None:
    """Make a repeated GitHub workflow attempt idempotent per decision event."""
    run_id, ticker, event_name = (str(row[index]) for index in (0, 3, 4))
    if not run_id:
        _append("Signals", row)
        return
    signal_id = str(row[20]) if len(row) > 20 else ""
    if _batch_sync is not None:
        existing = _batch_sync["tables"]["Signals"]
        row_number = next((index + 1 for index, current in enumerate(existing[1:], start=1)
                           if signal_id and len(current) > 20 and str(current[20]) == signal_id
                           and len(current) > 4 and str(current[4]) == event_name), None)
        if row_number is None:
            row_number = next((index + 1 for index, current in enumerate(existing[1:], start=1)
                               if len(current) > 4 and str(current[0]) == run_id
                               and str(current[2]) == str(row[2])
                               and str(current[3]) == ticker and str(current[4]) == event_name
                               and (len(current) <= 5 or str(current[5]) == str(row[5]))), None)
        if row_number is None:
            row_number = max(2, len(existing) + 1)
        _batch_write_row("Signals", row_number, row)
        return
    target = quote("'Signals'!A1:U", safe="!'")
    existing = _request("GET", f"values/{target}").get("values", [])
    row_number = next((index + 1 for index, current in enumerate(existing[1:], start=1)
                       if signal_id and len(current) > 20 and str(current[20]) == signal_id
                       and len(current) > 4 and str(current[4]) == event_name), None)
    if row_number is None:
        row_number = next((index + 1 for index, current in enumerate(existing[1:], start=1)
                           if len(current) > 4 and str(current[0]) == run_id
                           and str(current[2]) == str(row[2])
                           and str(current[3]) == ticker and str(current[4]) == event_name
                           and (len(current) <= 5 or str(current[5]) == str(row[5]))), None)
    if row_number:
        destination = quote(f"'Signals'!A{row_number}:U{row_number}", safe="!'")
        _request("PUT", f"values/{destination}?valueInputOption=RAW", json={"values": [row]})
        _fit_rows("Signals", row_number - 1, row_number)
    else:
        _append("Signals", row)


def _fit_rows(tab: str, start_index: int, end_index: int) -> None:
    """Use consistent compact row heights instead of content expansion."""
    sheet_id = _sheet_ids.get(tab)
    if sheet_id is None or end_index <= start_index:
        return
    try:
        _request("POST", ":batchUpdate", json={"requests": [{
            "updateDimensionProperties": {"range": {
                "sheetId": sheet_id, "dimension": "ROWS",
                "startIndex": start_index, "endIndex": end_index,
            }, "properties": {"pixelSize": 50 if tab in ("Signals", "Positions") else 60}, "fields": "pixelSize"}
        }]})
    except Exception as exc:
        print(f"[!] Google Sheets row auto-fit failed for {tab}: {exc}")


def _position_rows():
    target = quote("'Positions'!A1:U", safe="!'")
    return _request("GET", f"values/{target}").get("values", [])


def load_open_position_state() -> tuple[list[dict[str, Any]], set[str]]:
    """Read live paper positions so a failed runner cannot orphan them locally."""
    if not _client():
        return [], set()
    _ensure_tabs()
    rows = _position_rows()
    if len(rows) < 2:
        return [], set()

    def pkt_millis(value):
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S PKT").replace(tzinfo=_PKT_TZ)
            raw = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
            return max(0, (raw // 60_000) * 60_000 - 60_000)
        except (TypeError, ValueError, OverflowError):
            return 0

    active = []
    closed_ids = set()
    for row in rows[1:]:
        if not row or not row[0]:
            continue
        values = list(row) + [""] * max(0, len(POSITION_HEADERS) - len(row))
        signal_id, opened, source, symbol, action = map(str, values[:5])
        status = str(values[11]).upper()
        watch_only = False
        if status in {"SL_HIT", "TP2_HIT", "TP3_HIT", "BE_HIT", "AMBIGUOUS", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"}:
            closed_ids.add(signal_id)
            continue
        if status not in {"OPEN", "TP1_HIT"} or action not in {"BUY", "SELL"}:
            continue
        if source.startswith("STRATEGY"):
            scanner = "STRATEGY"
            strategy_name = source.partition("·")[2].strip() or "Winner rule"
            watch_only = strategy_name.startswith("WATCH ONLY ·")
            if watch_only:
                strategy_name = strategy_name.partition("·")[2].strip()
        elif source.startswith("TREND MOMENTUM"):
            scanner, strategy_name = "TREND", ""
        elif source.startswith("MOVERS PULLBACK") or source.startswith("MOVERS ORDER BLOCK"):
            is_pullback = source.startswith("MOVERS PULLBACK")
            scanner = "MOVERS_PULLBACK" if is_pullback else "MOVERS_ORDERBLOCK"
            strategy_name = "Movers Pullback" if is_pullback else "Movers Order Block"
            watch_only = "WATCH ONLY" in source
        elif source.startswith("WHALE FLOW"):
            scanner, strategy_name = "WHALE_FLOW", ""
            watch_only = "WATCH ONLY" in source
        elif source.startswith("WHALE VOLUME"):
            scanner = "WHALE_VOLUME_PRESSURE" if "PRESSURE" in source else "WHALE_VOLUME"
            strategy_name = ""
        else:
            scanner, strategy_name = source, ""
        created_ts = pkt_millis(opened)
        checked_ts = pkt_millis(values[13])
        entry = _number(values[5])
        active.append({
            "event_id": signal_id, "symbol": symbol, "scanner": scanner,
            "strategy_name": strategy_name, "watch_only": watch_only, "action": action,
            "direction": "LONG" if action == "BUY" else "SHORT",
            "price": entry, "stop_loss": _number(values[6]),
            "take_profit_1": _number(values[7]), "take_profit_2": _number(values[9]),
            "tp1_hit": status == "TP1_HIT" or str(values[8]).upper() == "HIT",
            "tp2_hit": str(values[10]).upper() == "HIT",
            "position_size_usd": _number(values[16], TRADE_NOTIONAL_USD),
            "quantity": _number(values[17]), "last_price": _number(values[12], entry),
            "created_ts": created_ts, "last_checked_ts": checked_ts,
            "checked_through_ts": checked_ts, "_recovered_from_sheet": True,
            "partial_tp1_pct": (0.5 if (scanner.startswith("MOVERS") or created_ts >= TP_POLICY_START_MS) else 0.0),
        })
    return active, closed_ids


def merge_sheet_position_state() -> int:
    """Merge sheet-only open positions into local state and remove sheet-closed IDs."""
    active, closed_ids = load_open_position_state()
    if not active and not closed_ids:
        return 0
    try:
        from .trade_tracker import load_open, save_open
    except ImportError:
        from trade_tracker import load_open, save_open

    local = load_open()
    by_id = {str(trade.get("event_id")): trade for trade in local if trade.get("event_id")}
    for signal_id in closed_ids:
        by_id.pop(signal_id, None)
    recovered = 0
    for sheet_trade in active:
        signal_id = str(sheet_trade["event_id"])
        if signal_id not in by_id:
            by_id[signal_id] = sheet_trade
            recovered += 1
            continue
        trade = by_id[signal_id]
        if sheet_trade.get("tp1_hit") and not trade.get("tp1_hit"):
            trade["tp1_hit"] = True
            trade["position_status"] = "TP1_HIT"
        local_watermark = max(_number(trade.get("created_ts")),
                              _number(trade.get("checked_through_ts")))
        sheet_watermark = _number(sheet_trade.get("checked_through_ts"))
        if sheet_watermark > local_watermark + 90_000:
            trade["_recovered_from_sheet"] = True
        trade["checked_through_ts"] = max(local_watermark, sheet_watermark)
    merged = list(by_id.values())
    if len(merged) != len(local) or recovered or closed_ids:
        save_open(merged)
    return recovered


def _find_id_row(tab: str, signal_id: str, end_col: str):
    target = quote(f"'{tab}'!A1:{end_col}", safe="!'")
    rows = _request("GET", f"values/{target}").get("values", [])
    row_no = next((i + 1 for i, row in enumerate(rows[1:], start=1) if row and str(row[0]) == signal_id), None)
    return rows, row_no


def _save_position(position: dict[str, Any]) -> None:
    signal_id = str(position.get("signal_id", ""))
    if not signal_id:
        return
    if _batch_sync is not None:
        row_no, previous = _batch_existing_row("Positions", signal_id)
        rows = _batch_sync["tables"]["Positions"]
    else:
        rows, row_no = _find_id_row("Positions", signal_id, "U")
        previous = rows[row_no - 1] if row_no and row_no - 1 < len(rows) else []
    entry = _number(position.get("entry"), _number(previous[5]) if len(previous) > 5 else 0.0)
    side = position.get("action", previous[4] if len(previous) > 4 else "")
    latest = _number(position.get("last_price"), _number(previous[12], entry) if len(previous) > 12 else entry)
    size = _number(position.get("position_size_usd"), TRADE_NOTIONAL_USD)
    quantity = _number(position.get("quantity"), size / entry if entry else 0.0)
    status = position.get("status", "OPEN")
    exit_price = position.get("exit_price", previous[14] if len(previous) > 14 else "")
    closed = position.get("closed_at_utc", "")
    realized = position.get("realized_pnl_usd", "")
    unrealized = position.get("unrealized_pnl_usd", "")
    ret = position.get("return_pct", "")
    tp1_val = _number(position.get("tp1", previous[7] if len(previous) > 7 else 0.0))
    tp1_stat = str(position.get("tp1_status", previous[8] if len(previous) > 8 else "PENDING")).upper()
    if realized == "" and status == "BE_HIT":
        realized = _be_hit_realized_pnl(side, entry, tp1_val, _number(exit_price, entry), quantity, tp1_stat == "HIT")
    if realized == "" and status in {"SL_HIT", "TP2_HIT"} and entry and _number(exit_price):
        realized = _signed_pnl(side, entry, _number(exit_price), quantity)
    if unrealized == "" and entry and latest and status not in {"SL_HIT", "TP2_HIT", "BE_HIT", "AMBIGUOUS"}:
        unrealized = _signed_pnl(side, entry, latest, quantity)
    if ret == "":
        pnl_for_return = realized if realized != "" else unrealized
        ret = _number(pnl_for_return) / size if size and pnl_for_return != "" else ""
    created = position.get("created_at_utc", "")
    checked = position.get("last_checked_utc", "")
    cells = [
        signal_id,
        format_pkt(datetime.fromisoformat(created)) if created else (previous[1] if len(previous) > 1 else ""),
        position.get("source", previous[2] if len(previous) > 2 else ""),
        position.get("ticker", previous[3] if len(previous) > 3 else ""),
        side, entry, position.get("sl", previous[6] if len(previous) > 6 else ""),
        position.get("tp1", previous[7] if len(previous) > 7 else ""),
        position.get("tp1_status", previous[8] if len(previous) > 8 else "PENDING"),
        position.get("tp2", previous[9] if len(previous) > 9 else ""),
        position.get("tp2_status", previous[10] if len(previous) > 10 else "PENDING"),
        status,
        latest,
        format_pkt(datetime.fromisoformat(checked)) if checked else format_pkt(),
        exit_price,
        format_pkt(datetime.fromisoformat(closed)) if closed else (previous[15] if len(previous) > 15 else ""),
        size, quantity, realized, unrealized, ret,
    ]
    if _batch_sync is not None:
        _batch_write_row("Positions", row_no or max(2, len(rows) + 1), cells)
    elif row_no:
        target = quote(f"'Positions'!A{row_no}:U{row_no}", safe="!'")
        _request("PUT", f"values/{target}?valueInputOption=RAW", json={"values": [cells]})
        _fit_rows("Positions", row_no - 1, row_no)
    else:
        _append("Positions", cells)


def _save_backtest(position: dict[str, Any]) -> None:
    signal_id = str(position.get("signal_id", ""))
    if not signal_id:
        return
    if _batch_sync is not None:
        row_no, previous = _batch_existing_row("Backtesting", signal_id)
        rows = _batch_sync["tables"]["Backtesting"]
    else:
        rows, row_no = _find_id_row("Backtesting", signal_id, "U")
        previous = rows[row_no - 1] if row_no and row_no - 1 < len(rows) else []
    entry = _number(position.get("entry"), _number(previous[7]) if len(previous) > 7 else 0.0)
    side = position.get("action", previous[6] if len(previous) > 6 else "")
    latest = _number(position.get("last_price"), _number(previous[8], entry) if len(previous) > 8 else entry)
    size = _number(position.get("position_size_usd"), TRADE_NOTIONAL_USD)
    quantity = _number(position.get("quantity"), size / entry if entry else 0.0)
    status = position.get("status", "OPEN")
    exit_price = position.get("exit_price", previous[9] if len(previous) > 9 else "")
    realized = position.get("realized_pnl_usd", "")
    unrealized = position.get("unrealized_pnl_usd", "")
    tp1_val = _number(position.get("tp1", previous[13] if len(previous) > 13 else 0.0))
    tp1_stat = str(position.get("tp1_status", previous[14] if len(previous) > 14 else "PENDING")).upper()
    if realized == "" and status == "BE_HIT":
        realized = _be_hit_realized_pnl(side, entry, tp1_val, _number(exit_price, entry), quantity, tp1_stat == "HIT")
    if realized == "" and status in {"SL_HIT", "TP2_HIT"} and entry and _number(exit_price):
        realized = _signed_pnl(side, entry, _number(exit_price), quantity)
    if unrealized == "" and entry and latest and status not in {"SL_HIT", "TP2_HIT", "BE_HIT", "AMBIGUOUS"}:
        unrealized = _signed_pnl(side, entry, latest, quantity)
    basis = realized if realized != "" else unrealized
    return_pct = position.get("return_pct", _number(basis) / size if basis != "" and size else "")
    opened = position.get("created_at_utc", "")
    checked = position.get("last_checked_utc", "")
    closed = position.get("closed_at_utc", previous[3] if len(previous) > 3 else "")
    cells = [signal_id, format_pkt(datetime.fromisoformat(opened)) if opened else (previous[1] if len(previous) > 1 else ""),
             format_pkt(datetime.fromisoformat(checked)) if checked else format_pkt(),
             format_pkt(datetime.fromisoformat(closed)) if closed else "",
             position.get("source", previous[4] if len(previous) > 4 else ""),
             position.get("ticker", previous[5] if len(previous) > 5 else ""), side, entry,
             latest, exit_price, size, quantity,
             position.get("sl", previous[12] if len(previous) > 12 else ""),
             position.get("tp1", previous[13] if len(previous) > 13 else ""),
             position.get("tp1_status", previous[14] if len(previous) > 14 else "PENDING"),
             position.get("tp2", previous[15] if len(previous) > 15 else ""),
             position.get("tp2_status", previous[16] if len(previous) > 16 else "PENDING"),
             status, realized, unrealized, return_pct]
    if _batch_sync is not None:
        _batch_write_row("Backtesting", row_no or max(2, len(rows) + 1), cells)
    elif row_no:
        target = quote(f"'Backtesting'!A{row_no}:U{row_no}", safe="!'")
        _request("PUT", f"values/{target}?valueInputOption=RAW", json={"values": [cells]})
        _fit_rows("Backtesting", row_no - 1, row_no)
    else:
        _append("Backtesting", cells)


def record_scan_event(event: dict[str, Any]) -> bool:
    """Append a compatible manual/scanner event using the compact ledger schema."""
    try:
        if not _client():
            return False
        _ensure_tabs()
        now_utc = datetime.now(timezone.utc).isoformat()
        action = event.get("action", "NO TRADE")
        entry = _number(event.get("entry", event.get("price")))
        size = TRADE_NOTIONAL_USD if action in ("BUY", "SELL") and entry else ""
        quantity = size / entry if size and entry else ""
        stop, tp1, tp2 = _number(event.get("sl")), _number(event.get("tp1", event.get("tp"))), _number(event.get("tp2"))
        row = [event.get("run_id", ""), format_pkt(), event.get("source", "watchlist"), event.get("ticker", ""),
               event.get("event", "SIGNAL" if action in ("BUY", "SELL") else "NO_SIGNAL"), action,
               event.get("price", entry), entry, stop or "", tp1 or "", event.get("tp1_status", "PENDING"),
               tp2 or "", event.get("tp2_status", "PENDING"), event.get("status", "OPEN"), size, quantity,
               abs(entry - stop) * quantity if stop and quantity else "",
               _signed_pnl(action, entry, tp1, quantity) if tp1 and quantity else "",
               _signed_pnl(action, entry, tp2, quantity) if tp2 and quantity else "", event.get("reason", "")]
        _upsert_scan_row(row)
        if event.get("signal_id") and event.get("action") in ("BUY", "SELL"):
            position = {**event, "signal_id": event["signal_id"], "created_at_utc": event.get("created_at_utc") or now_utc,
                        "last_checked_utc": now_utc, "last_price": event.get("price", entry), "status": event.get("status", "OPEN"),
                        "source": event.get("source", "watchlist"), "ticker": event.get("ticker", ""), "entry": entry,
                        "sl": stop, "tp1": tp1, "tp2": tp2, "position_size_usd": TRADE_NOTIONAL_USD, "quantity": quantity}
            _save_position(position)
            _save_backtest(position)
        return True
    except Exception as exc:
        print(f"[!] Google Sheets sync failed: {exc}")
        return False


def _health_severity(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("critical:", "analysis/feed failed", "no price source", "google sheets", "no usable scoring value")):
        return "ACTION"
    if "recovered via" in lowered or any(token in lowered for token in ("429", "fred_api_key", "vendor 'fred'", "reddit rss", "yfinance", "optional macro_data")):
        return "INFO"
    return "REVIEW"


def _upsert_health_row(row: list[Any]) -> None:
    target = quote("'System Health'!A1:G", safe="!'")
    existing = _request("GET", f"values/{target}").get("values", [])
    run_id, ticker = str(row[0]), str(row[3])
    row_number = next((index + 1 for index, current in enumerate(existing[1:], start=1)
                       if len(current) > 3 and str(current[0]) == run_id and str(current[3]) == ticker), None)
    if row_number:
        destination = quote(f"'System Health'!A{row_number}:G{row_number}", safe="!'")
        _request("PUT", f"values/{destination}?valueInputOption=RAW", json={"values": [row]})
        _fit_rows("System Health", row_number - 1, row_number)
    else:
        _append("System Health", row)


def _move_legacy_health_rows() -> None:
    """Move old DATA_HEALTH_WARNING pseudo-signals to the diagnostics tab."""
    global _legacy_migration_checked
    if _legacy_migration_checked:
        return
    target = quote("'Signals'!A1:T", safe="!'")
    rows = _request("GET", f"values/{target}").get("values", [])
    delete_rows = []
    legacy = {}
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) < 5 or str(row[4]) != "DATA_HEALTH_WARNING":
            continue
        message = row[19] if len(row) > 19 else "Legacy data-health row (no details recorded)."
        key = (str(row[0]) if row else "", str(row[3]) if len(row) > 3 else "")
        legacy[key] = [key[0], row[1] if len(row) > 1 else "",
                       row[2] if len(row) > 2 else "watchlist", key[1],
                       _health_severity(str(message)), "LEGACY_FEED_DIAGNOSTICS", str(message)[:4000]]
        delete_rows.append(row_number - 1)
    if legacy:
        health_target = quote("'System Health'!A1:G", safe="!'")
        health_rows = _request("GET", f"values/{health_target}").get("values", [])
        existing = {(str(row[0]), str(row[3])): index + 1 for index, row in enumerate(health_rows[1:], start=1)
                    if len(row) > 3}
        updates, appends = [], []
        for key, health_row in legacy.items():
            if key in existing:
                updates.append({"range": f"'System Health'!A{existing[key]}:G{existing[key]}",
                                "values": [health_row]})
            else:
                appends.append(health_row)
        if updates:
            _request("POST", "values:batchUpdate", json={"valueInputOption": "RAW", "data": updates})
        if appends:
            append_target = quote("'System Health'!A1", safe="!'")
            _request("POST", f"values/{append_target}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                     json={"values": appends})
    if delete_rows:
        requests_to_delete = [{"deleteDimension": {"range": {"sheetId": _sheet_ids["Signals"],
                              "dimension": "ROWS", "startIndex": index, "endIndex": index + 1}}}
                              for index in reversed(delete_rows)]
        _request("POST", ":batchUpdate", json={"requests": requests_to_delete})
    _legacy_migration_checked = True


def record_health_event(event: dict[str, Any]) -> bool:
    """Store one consolidated diagnostic row per run and coin."""
    try:
        if not _client():
            return False
        _ensure_tabs()
        message = "\n".join(str(item) for item in event.get("messages", []) if item)
        if not message:
            return True
        _upsert_health_row([
            event.get("run_id", ""), format_pkt(datetime.fromisoformat(event["time_utc"])) if event.get("time_utc") else format_pkt(),
            event.get("source", "watchlist"), event.get("ticker", ""),
            _health_severity(message), event.get("category", "FEED_DIAGNOSTICS"), message[:4000],
        ])
        return True
    except Exception as exc:
        print(f"[!] Google Sheets health logging failed: {exc}")
        return False


def update_position(event: dict[str, Any]) -> bool:
    """Update the matching position row and append a monitor/exit ledger event."""
    try:
        if not _client():
            return False
        _ensure_tabs()
        now_utc = datetime.now(timezone.utc).isoformat()
        append_rows([{
            "event_id": event.get("signal_id", ""), "event_time_utc": now_utc,
            "symbol": event.get("ticker", ""), "scanner": event.get("source", ""),
            "action": event.get("action", ""), "price": event.get("entry", event.get("price", "")),
            "last_price": event.get("price", event.get("last_price", "")), "stop_loss": event.get("sl", ""),
            "take_profit_1": event.get("tp1", event.get("tp", "")), "take_profit_2": event.get("tp2", ""),
            "tp1_status": event.get("tp1_status", "PENDING"), "tp2_status": event.get("tp2_status", "PENDING"),
            "status": "UPDATE", "outcome": event.get("event", "POSITION_MONITOR"),
            "position_status": event.get("status", "OPEN"), "exit_price": event.get("exit_price", ""),
        }])
        return True
    except Exception as exc:
        print(f"[!] Google Sheets position update failed: {exc}")
        return False


def append_rows(rows: list[dict[str, Any]]) -> bool:
    """Write only actionable signals/events; maintain live Positions and Backtesting rows."""
    global _batch_sync
    if not rows:
        return True
    if not _client():
        print("[google-sheets] credentials missing; skipped spreadsheet sync")
        return False
    _ensure_tabs()
    if _batch_sync is not None:
        raise RuntimeError("nested Google Sheets batch sync is not supported")
    _batch_sync = _begin_batch_sync()
    try:
        run_id = os.getenv("GITHUB_RUN_ID", "local-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        for row in rows:
            status = str(row.get("status", "SCAN"))
            outcome = str(row.get("outcome", ""))
            if status == "SCAN":
                continue
            action = row.get("action", "")
            if action in ("SHORT", "SHORT / SELL"):
                action = "SELL"
            if action not in ("BUY", "SELL"):
                continue
            signal_id = str(row.get("event_id", ""))
            entry = _number(row.get("price"))
            if not signal_id or entry <= 0:
                continue
            size = _number(row.get("position_size_usd"), TRADE_NOTIONAL_USD)
            quantity = _number(row.get("quantity"), size / entry if entry else 0.0)
            stop = _number(row.get("stop_loss"))
            tp1 = _number(row.get("take_profit_1"))
            tp2 = _number(row.get("take_profit_2"))
            latest = _number(row.get("last_price"), entry)
            exit_price = _number(row.get("exit_price"))
            terminal = status == "CLOSED" and outcome in {"TP2_HIT", "SL_HIT", "BE_HIT"}
            ambiguous = "AMBIGUOUS" in outcome
            realized = _number(row.get("realized_pnl_usd")) if terminal else ""
            if terminal and realized == 0 and outcome != "BE_HIT":
                realized = _signed_pnl(action, entry, exit_price, quantity)
            if terminal or ambiguous:
                unrealized = ""
            elif row.get("unrealized_pnl_usd") not in ("", None):
                unrealized = _number(row.get("unrealized_pnl_usd"))
            else:
                unrealized = _signed_pnl(action, entry, latest, quantity)
            return_pct = (realized if terminal else unrealized) / size if size else ""
            result = outcome or ("SIGNAL" if status == "SIGNAL" else "POSITION UPDATE")
            position_status = ("OPEN" if status == "SIGNAL" else
                               "AMBIGUOUS" if ambiguous else
                               outcome if outcome in {"TP1_HIT", "TP2_HIT", "SL_HIT", "BE_HIT"} else row.get("position_status", "OPEN"))
            tp1_status = row.get("tp1_status", "PENDING")
            tp2_status = row.get("tp2_status", "PENDING")
            source = row.get("scanner", "")
            timestamp = row.get("event_time_utc", datetime.now(timezone.utc).isoformat())
            try:
                timestamp_pkt = format_pkt(datetime.fromisoformat(timestamp))
            except (TypeError, ValueError):
                timestamp_pkt = format_pkt()

            if status != "MARK":
                risk_usd = abs(entry - stop) * quantity if stop else ""
                tp1_pnl = _signed_pnl(action, entry, tp1, quantity) if tp1 else ""
                tp2_pnl = _signed_pnl(action, entry, tp2, quantity) if tp2 else ""
                market_price = exit_price or latest
                reason = row.get("details", "")
                cells = [run_id, timestamp_pkt, source, row.get("symbol", ""), result, action,
                         market_price, entry, stop or "", tp1 or "", tp1_status, tp2 or "", tp2_status,
                         position_status, size, quantity, risk_usd, tp1_pnl, tp2_pnl, str(reason)[:3000], signal_id]
                _upsert_scan_row(cells)

            checked_through = row.get("checked_through_ts")
            checked_timestamp = (timestamp if terminal or ambiguous else
                                 datetime.fromtimestamp(_number(checked_through) / 1000, tz=timezone.utc).isoformat()
                                 if checked_through not in (None, "", 0, "0") else timestamp)
            trade_update = {
                "signal_id": signal_id,
                "created_at_utc": timestamp if status == "SIGNAL" else "",
                "last_checked_utc": checked_timestamp,
                "closed_at_utc": timestamp if terminal or ambiguous else "",
                "source": source,
                "ticker": row.get("symbol", ""),
                "action": action,
                "entry": entry,
                "sl": stop,
                "tp1": tp1,
                "tp1_status": tp1_status,
                "tp2": tp2,
                "tp2_status": tp2_status,
                "status": position_status,
                "last_price": latest,
                "exit_price": exit_price if terminal else "",
                "position_size_usd": size,
                "quantity": quantity,
                "realized_pnl_usd": realized,
                "unrealized_pnl_usd": unrealized,
                "return_pct": return_pct,
            }
            _save_position(trade_update)
            _save_backtest(trade_update)
        _flush_batch_sync(_batch_sync)
    finally:
        _batch_sync = None
    return True
