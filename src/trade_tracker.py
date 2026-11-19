"""Persistent signal journal and TP/SL outcome tracking (v2.3 with Step-Lock Ratchet Stop)."""
import csv
import json
import os
import time
import uuid
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OPEN_FILE = os.path.join(ROOT, "open_trades.json")
JOURNAL_FILE = os.path.join(ROOT, "deep_pocket_log.csv")
PENDING_SHEET_FILE = os.path.join(ROOT, "sheet_sync_pending.json")
DISCORD_CUTOVER_FILE = os.path.join(ROOT, "discord_alert_cutover.json")
FIELDS = ["event_id", "event_time_utc", "symbol", "scanner", "action", "status",
          "price", "stop_loss", "take_profit_1", "take_profit_2", "exit_price",
          "outcome", "timeframe", "confidence", "tp1_status", "tp2_status", "last_price",
          "position_size_usd", "quantity", "realized_pnl_usd", "unrealized_pnl_usd", "return_pct",
          "checked_through_ts", "details",
          "cost_usd", "net_pnl_usd", "r_multiple", "quality_score", "tier"]
TRADE_NOTIONAL_USD = 100.0

# ---- Execution-cost model (v2) -----------------------------------------------------------------
FEE_PER_SIDE_PCT = 0.10
SLIPPAGE_PER_SIDE_PCT = 0.05
ROUND_TRIP_COST_PCT = 2 * (FEE_PER_SIDE_PCT + SLIPPAGE_PER_SIDE_PCT)


def round_trip_cost_usd(size):
    return size * ROUND_TRIP_COST_PCT / 100.0


def risk_usd(signal, size):
    """Dollars at risk on the ORIGINAL stop (the stop moves to entry after TP1, so use the saved one)."""
    entry = _number(signal.get("price"))
    stop = _number(signal.get("original_stop_loss")) or _number(signal.get("stop_loss"))
    if entry <= 0 or stop <= 0 or size <= 0:
        return 0.0
    return size * abs(entry - stop) / entry

# ---- Dollar-based TP policy + Step-Lock Ratchet Stop (v2.3) ------------------------------------
TP_POLICY_VERSION = "usd-tp1-v1"
TP_POLICY_START_MS = 1_791_201_600_000      # positions opened after this use the policy accounting
TP1_MIN_USD = 2.0                           # TP1 profit on a full $100 position, lower bound
TP1_MAX_USD = 5.0                           # ... upper bound
TP1_RISK_MULT = 1.0                         # TP1 distance = 1.0 x stop distance, then clamped
TP2_TP1_MULT = 2.5                          # TP2 distance = 2.5 x TP1 distance ...
TP2_MIN_EXTRA_USD = 2.0                     # ... and at least $2 beyond TP1
TP1_PARTIAL_PCT = 0.5                       # paper accounting: this share is closed (booked) at TP1
RATCHET_LOCK_ENABLED = True                 # v2.3: lock SL at TP1 once price reaches 55% of the way from TP1 to TP2
RATCHET_PROGRESS_FRACTION = 0.55


def apply_tp_policy(signal):
    """Re-place TP1/TP2 of a new signal using the dollar policy. Never widens a target,
    never touches the stop. Idempotent. Returns the same dict (mutated)."""
    if signal.get("tp_policy") == TP_POLICY_VERSION:
        return signal
    entry = _number(signal.get("price"))
    stop = _number(signal.get("stop_loss"))
    old_tp1 = _number(signal.get("take_profit_1"))
    old_tp2 = _number(signal.get("take_profit_2"))
    if entry <= 0 or stop <= 0 or old_tp1 <= 0 or old_tp2 <= 0:
        return signal
    action = str(signal.get("action") or signal.get("direction") or "").upper()
    is_long = ("BUY" in action) or ("LONG" in action)
    sign = 1 if is_long else -1
    risk_pct = abs(entry - stop) / entry * 100
    old_tp1_pct = abs(old_tp1 - entry) / entry * 100
    old_tp2_pct = abs(old_tp2 - entry) / entry * 100
    if risk_pct <= 0 or old_tp1_pct <= 0:
        return signal
    min_pct = TP1_MIN_USD / TRADE_NOTIONAL_USD * 100
    max_pct = TP1_MAX_USD / TRADE_NOTIONAL_USD * 100
    tp1_pct = min(old_tp1_pct, max(min_pct, min(max_pct, risk_pct * TP1_RISK_MULT)))
    extra_pct = TP2_MIN_EXTRA_USD / TRADE_NOTIONAL_USD * 100
    tp2_pct = max(tp1_pct * TP2_TP1_MULT, tp1_pct + extra_pct)
    tp2_pct = max(min(old_tp2_pct, tp2_pct), tp1_pct * 1.2)     # never farther than before, always beyond TP1
    signal["original_take_profit_1"] = old_tp1
    signal["original_take_profit_2"] = old_tp2
    signal["take_profit_1"] = round(entry * (1 + sign * tp1_pct / 100), 8)
    signal["take_profit_2"] = round(entry * (1 + sign * tp2_pct / 100), 8)
    signal["partial_tp1_pct"] = TP1_PARTIAL_PCT
    signal["tp_policy"] = TP_POLICY_VERSION
    details = dict(signal.get("details") or {}) if isinstance(signal.get("details"), dict) else {}
    details.update({
        "tp_policy": TP_POLICY_VERSION,
        "tp1_usd": round(tp1_pct / 100 * TRADE_NOTIONAL_USD, 2),
        "tp2_usd": round(tp2_pct / 100 * TRADE_NOTIONAL_USD, 2),
        "sl_usd": round(risk_pct / 100 * TRADE_NOTIONAL_USD, 2),
        "tp1_booked_pct": int(TP1_PARTIAL_PCT * 100),
        "original_tp1_pct": round(old_tp1_pct, 2),
        "original_tp2_pct": round(old_tp2_pct, 2),
    })
    signal["details"] = details
    return signal


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_open():
    try:
        with open(OPEN_FILE, encoding="utf-8") as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    changed = False
    for trade in items:
        entry = _number(trade.get("price"))
        action = trade.get("action") or ("BUY" if trade.get("direction") == "LONG" else "SELL")
        if entry > 0 and action in {"BUY", "SELL"}:
            quantity = TRADE_NOTIONAL_USD / entry
            if _number(trade.get("position_size_usd")) != TRADE_NOTIONAL_USD:
                trade["position_size_usd"] = TRADE_NOTIONAL_USD
                changed = True
            if _number(trade.get("quantity")) != quantity:
                trade["quantity"] = quantity
                changed = True
    if changed:
        save_open(items)
    return items


def save_open(items):
    _atomic_json(OPEN_FILE, items)


def load_or_create_discord_cutover():
    """Persist the rollout boundary so positions already open stay alert-silent."""
    try:
        with open(DISCORD_CUTOVER_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return _number(state.get("cutover_ts"))
    except (OSError, ValueError, AttributeError):
        _atomic_json(DISCORD_CUTOVER_FILE, {"cutover_ts": 0})
        return 0.0


def silence_pre_cutover_positions(cutover_ts, items=None):
    """Keep old positions for ledger reconciliation, but never alert their hits."""
    current = load_open() if items is None else items
    changed = 0
    for trade in current:
        created = _number(trade.get("created_ts"))
        if created <= cutover_ts and not trade.get("suppress_discord_updates"):
            trade["suppress_discord_updates"] = True
            changed += 1
    if changed:
        save_open(current)
    return changed


def _atomic_json(path, value):
    """Replace persisted state atomically so interrupted runners cannot truncate it."""
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sheet_event_key(row):
    event_id = str(row.get("event_id", "")).strip()
    outcome = str(row.get("outcome", "")).strip()
    status = str(row.get("status", "")).strip()
    if not event_id:
        return ""
    if outcome:
        return f"{event_id}|{outcome}"
    return f"{event_id}|SIGNAL" if status == "SIGNAL" else ""


def queue_sheet_rows(rows):
    """Durably queue signal/outcome rows until Sheets confirms the write."""
    try:
        with open(PENDING_SHEET_FILE, encoding="utf-8") as f:
            pending = json.load(f)
    except (OSError, ValueError):
        pending = []
    keys = {_sheet_event_key(row) for row in pending}
    changed = False
    for row in rows:
        key = _sheet_event_key(row)
        if key and key not in keys:
            pending.append(row)
            keys.add(key)
            changed = True
    if changed:
        _atomic_json(PENDING_SHEET_FILE, pending)


def pending_sheet_rows():
    try:
        with open(PENDING_SHEET_FILE, encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def acknowledge_sheet_rows(rows):
    acknowledged = {_sheet_event_key(row) for row in rows}
    acknowledged.discard("")
    if not acknowledged:
        return
    remaining = [row for row in pending_sheet_rows() if _sheet_event_key(row) not in acknowledged]
    if remaining:
        _atomic_json(PENDING_SHEET_FILE, remaining)
    elif os.path.exists(PENDING_SHEET_FILE):
        os.unlink(PENDING_SHEET_FILE)


def journal_row(signal, status="SIGNAL", outcome="", exit_price=""):
    action = signal.get("action") or ("BUY" if signal.get("direction") == "LONG" else
                                       "SELL" if signal.get("direction") == "SHORT" else "HOLD")
    if action in ("SHORT / SELL", "SHORT"):
        action = "SELL"
    scanner = signal.get("scanner", "")
    source = {
        "WHALE": "WHALE FLOW · ON-CHAIN DEX" if "on-chain" in str(signal.get("timeframe", "")).lower() else "WHALE VOLUME",
        "WHALE_VOLUME": "WHALE VOLUME",
        "WHALE_VOLUME_PRESSURE": "WHALE VOLUME · BUY/SELL PRESSURE",
        "WHALE_FLOW": ("WHALE FLOW · ON-CHAIN DEX · WATCH ONLY" if signal.get("watch_only")
                       else "WHALE FLOW · ON-CHAIN DEX"),
        "TREND": "TREND MOMENTUM",
        "MOVERS_PULLBACK": ("MOVERS PULLBACK · WATCH ONLY" if signal.get("watch_only") else "MOVERS PULLBACK"),
        "MOVERS_ORDERBLOCK": ("MOVERS ORDER BLOCK · WATCH ONLY" if signal.get("watch_only") else "MOVERS ORDER BLOCK"),
        "STRATEGY": (f"STRATEGY · WATCH ONLY · {signal.get('strategy_name', 'Winner rule')}"
                     if signal.get("watch_only") else
                     f"STRATEGY · {signal.get('strategy_name', 'Winner rule')}"),
    }.get(scanner, signal.get("strategy_name") or scanner)
    entry = _number(signal.get("price"))
    last_price = _number(signal.get("last_price"), entry)
    size = _number(signal.get("position_size_usd"), TRADE_NOTIONAL_USD) if action in ("BUY", "SELL") and entry else 0.0
    quantity = size / entry if size and entry else 0.0
    closed = status == "CLOSED" and outcome in {"SL_HIT", "TP2_HIT", "BE_HIT"}
    realized = _pnl(action, entry, _number(exit_price), quantity) if closed and entry and exit_price not in ("", None) else ""
    unrealized = _pnl(action, entry, last_price, quantity) if entry and last_price and not closed else ""
    partial = _number(signal.get("partial_tp1_pct"))
    tp1_price = _number(signal.get("take_profit_1"))
    if 0 < partial < 1 and entry and tp1_price and signal.get("tp1_hit") and quantity:
        locked = _pnl(action, entry, tp1_price, quantity * partial)
        if realized != "":
            realized = locked + _pnl(action, entry, _number(exit_price), quantity * (1 - partial))
        elif unrealized != "":
            unrealized = locked + _pnl(action, entry, last_price, quantity * (1 - partial))
    basis = realized if realized != "" else unrealized
    cost = round_trip_cost_usd(size) if realized != "" and size else ""
    net = realized - cost if cost != "" else ""
    risk = risk_usd(signal, size)
    r_multiple = round(net / risk, 3) if net != "" and risk > 0 else ""
    quality = signal.get("details", {}).get("quality_score", "") if isinstance(signal.get("details"), dict) else ""
    return {"event_id": signal.get("event_id", uuid.uuid4().hex),
            "event_time_utc": signal.get("_event_time_utc", _now()),
            "symbol": signal.get("symbol", ""), "scanner": source,
            "action": action, "status": status,
            "position_status": signal.get("position_status",
                                           outcome if outcome in {"TP1_HIT", "TP2_HIT", "SL_HIT", "BE_HIT"} else
                                           "AMBIGUOUS" if "AMBIGUOUS" in outcome else "OPEN"),
            "price": signal.get("price", ""), "stop_loss": signal.get("stop_loss", ""),
            "take_profit_1": signal.get("take_profit_1", ""), "take_profit_2": signal.get("take_profit_2", ""),
            "exit_price": exit_price, "outcome": outcome, "timeframe": signal.get("timeframe", ""),
            "confidence": signal.get("confidence", ""),
            "tp1_status": "HIT" if signal.get("tp1_hit") else "PENDING",
            "tp2_status": "HIT" if signal.get("tp2_hit") else "PENDING",
            "last_price": last_price,
            "position_size_usd": size,
            "quantity": quantity,
            "realized_pnl_usd": realized,
            "unrealized_pnl_usd": unrealized,
            "return_pct": basis / size if basis != "" and size else "",
            "cost_usd": round(cost, 4) if cost != "" else "",
            "net_pnl_usd": round(net, 4) if net != "" else "",
            "r_multiple": r_multiple,
            "quality_score": quality,
            "tier": signal.get("tier") or (signal.get("details", {}).get("tier", "") if isinstance(signal.get("details"), dict) else ""),
            "checked_through_ts": signal.get("checked_through_ts", ""),
            "cooldown_key": signal.get("cooldown_key", ""),
            "recovered_from_sheet": bool(signal.get("_recovered_from_sheet")),
            "suppress_discord_updates": bool(signal.get("suppress_discord_updates")),
            "details": json.dumps(_details_with_excursion(signal), ensure_ascii=False, separators=(",", ":"))}


def _details_with_excursion(signal):
    """Journal details plus how far the trade moved for/against us (for later analysis)."""
    details = signal.get("details", {})
    if not isinstance(details, dict) or "max_favorable_pct" not in signal:
        return details
    details = dict(details)
    details["peak_profit_pct"] = round(_number(signal.get("max_favorable_pct")), 2)
    details["max_drawdown_pct"] = round(_number(signal.get("max_adverse_pct")), 2)
    if signal.get("ratchet_locked_tp1"):
        details["ratchet_locked_tp1"] = True
    return details


def _number(value, default=0.0):
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _pnl(action, entry, exit_price, quantity):
    return (exit_price - entry) * quantity if action == "BUY" else (entry - exit_price) * quantity


def append_local(rows):
    if not rows:
        return
    exists = os.path.exists(JOURNAL_FILE)
    prior_event_keys = set()
    if exists:
        with open(JOURNAL_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            old_fields = reader.fieldnames or []
            prior_rows = list(reader)
            prior_event_keys = {f"{row.get('event_id', '')}|{row.get('outcome') or 'SIGNAL'}"
                                for row in prior_rows
                                if row.get("event_id") and
                                (row.get("outcome") or row.get("status") == "SIGNAL")}
        if old_fields != FIELDS:
            with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(prior_rows)
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            key = f"{row.get('event_id', '')}|{row.get('outcome') or 'SIGNAL'}"
            if row.get("event_id") and (row.get("outcome") or row.get("status") == "SIGNAL"):
                if key in prior_event_keys:
                    continue
                prior_event_keys.add(key)
            writer.writerow(row)


def register(signal):
    """Store each distinct scanner signal once and return its journal row."""
    signal = dict(signal)
    signal.setdefault("event_id", uuid.uuid4().hex)
    signal.setdefault("created_ts", time.time() * 1000)
    signal["position_size_usd"] = TRADE_NOTIONAL_USD
    entry = _number(signal.get("price"))
    if entry > 0:
        signal["quantity"] = TRADE_NOTIONAL_USD / entry
    signal.setdefault("last_price", signal.get("price", ""))
    current = load_open()
    current.append(signal)
    row = journal_row(signal)
    queue_sheet_rows([row])
    append_local([row])
    save_open(current)
    return row


def settle_open(fetch_klines):
    """Track TP1 as a milestone, ratchet SL to TP1 after 55% progress toward TP2, and close at TP2 or SL/BE."""
    open_items = load_open()
    still_open, rows = [], []
    for trade in open_items:
        try:
            watermark = max(_number(trade.get("created_ts")),
                            _number(trade.get("checked_through_ts")))
            klines = fetch_klines(trade["symbol"], "1m", 1000, start_time=int(watermark))
        except Exception as exc:
            print(f"[tracker] Could not check {trade.get('event_id')}: {exc}")
            still_open.append(trade)
            continue
        if not klines:
            still_open.append(trade)
            continue
        latest_idx = max(0, len(klines.get("close", [])) - 2)
        if klines.get("close"):
            trade["last_price"] = klines["close"][latest_idx]
            trade["last_checked_ts"] = klines.get("open_time", [0])[latest_idx]
        entry_ts = trade.get("created_ts", 0)
        hits = []
        direction = trade.get("direction", "LONG")
        tp1, tp2 = (float(trade[k]) for k in ("take_profit_1", "take_profit_2"))
        closed_count = max(0, len(klines.get("open_time", [])) - 1)  # ignore the in-progress candle
        for i, candle_ts in enumerate(klines.get("open_time", [])[:closed_count]):
            if candle_ts <= max(entry_ts, trade.get("checked_through_ts", 0)):
                continue
            hi, lo = klines["high"][i], klines["low"][i]
            stop = float(trade["stop_loss"])
            entry_px = _number(trade.get("price"))
            if entry_px > 0:   # peak profit / worst drawdown since entry, in % of entry
                fav = ((hi / entry_px - 1) if direction == "LONG" else (1 - lo / entry_px)) * 100
                adv = ((lo / entry_px - 1) if direction == "LONG" else (1 - hi / entry_px)) * 100
                trade["max_favorable_pct"] = max(_number(trade.get("max_favorable_pct")), fav)
                trade["max_adverse_pct"] = min(_number(trade.get("max_adverse_pct")), adv)
            stop_hit = lo <= stop if direction == "LONG" else hi >= stop
            tp1_hit = hi >= tp1 if direction == "LONG" else lo <= tp1
            tp2_hit = hi >= tp2 if direction == "LONG" else lo <= tp2
            fresh_tp1 = tp1_hit and not trade.get("tp1_hit")
            if stop_hit and (fresh_tp1 or tp2_hit):
                hits.append((candle_ts, "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE", ""))
                break
            if tp1_hit and not trade.get("tp1_hit"):
                hits.append((candle_ts, "TP1_HIT", tp1))
                trade["tp1_hit"] = True
                trade["original_stop_loss"] = stop
                trade["stop_loss"] = float(trade["price"])
                trade["break_even_activated"] = True
            # Step-Lock Ratchet Stop: once TP1 was already hit in an earlier candle and price reaches
            # 55% of the way from TP1 toward TP2 without hitting stop in this candle, ratchet SL to TP1!
            if (RATCHET_LOCK_ENABLED and trade.get("tp1_hit") and not fresh_tp1
                    and not stop_hit and not tp2_hit and not trade.get("ratchet_locked_tp1")):
                ratchet_lvl = (tp1 + RATCHET_PROGRESS_FRACTION * (tp2 - tp1) if direction == "LONG"
                               else tp1 - RATCHET_PROGRESS_FRACTION * (tp1 - tp2))
                reached_ratchet = hi >= ratchet_lvl if direction == "LONG" else lo <= ratchet_lvl
                if reached_ratchet:
                    trade["stop_loss"] = tp1
                    trade["ratchet_locked_tp1"] = True
            if stop_hit:
                outcome = "BE_HIT" if trade.get("break_even_activated") else "SL_HIT"
                hits.append((candle_ts, outcome, stop))
                break
            if tp2_hit:
                if not trade.get("tp1_hit"):
                    hits.append((candle_ts, "TP1_HIT", tp1))
                    trade["tp1_hit"] = True
                trade["tp2_hit"] = True
                hits.append((candle_ts, "TP2_HIT", tp2))
                break

        if hits and hits[-1][1] in {"SL_HIT", "BE_HIT"}:
            trade["position_status"] = hits[-1][1]
        elif hits and hits[-1][1] == "TP2_HIT":
            trade["position_status"] = "TP2_HIT"
        elif hits and hits[-1][1] == "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE":
            trade["position_status"] = "AMBIGUOUS"
        elif trade.get("tp1_hit"):
            trade["position_status"] = "TP1_HIT"

        hit_rows = []
        for candle_ts, outcome, price in hits:
            event_status = "CLOSED" if outcome in {"SL_HIT", "TP2_HIT", "BE_HIT", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"} else "UPDATE"
            snapshot = dict(trade)
            snapshot["_event_time_utc"] = datetime.fromtimestamp(
                (candle_ts + 60_000) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
            snapshot["tp1_hit"] = trade.get("tp1_hit", False) or outcome in {"TP1_HIT", "TP2_HIT"}
            snapshot["tp2_hit"] = outcome == "TP2_HIT"
            snapshot["suppress_discord_updates"] = trade.get("suppress_discord_updates", False)
            event_row = journal_row(snapshot, status=event_status, outcome=outcome, exit_price=price)
            rows.append(event_row)
            hit_rows.append(event_row)
        if hit_rows:
            append_local(hit_rows)
            queue_sheet_rows(hit_rows)
        if hits and hits[-1][1] in {"SL_HIT", "TP2_HIT", "BE_HIT", "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE"}:
            continue
        closed_times = klines.get("open_time", [])[:closed_count]
        trade["checked_through_ts"] = max(closed_times or [trade.get("checked_through_ts", 0)])
        if klines and not klines.get("truncated"):
            trade.pop("_recovered_from_sheet", None)
        elif trade.get("_recovered_from_sheet"):
            print(f"[tracker] historical catch-up still pending for {trade.get('symbol')}.")
        rows.append(journal_row(trade, status="MARK"))
        still_open.append(trade)
    durable_events = [row for row in rows if row.get("outcome")]
    append_local(durable_events)
    queue_sheet_rows(durable_events)
    save_open(still_open)
    return rows
