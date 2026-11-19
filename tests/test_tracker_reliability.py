import csv
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from src import google_sheets, trade_tracker, strategy_policy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import screener


class TrackerReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.state_dir = os.path.join(os.path.dirname(__file__), ".test_state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.open_file = os.path.join(self.state_dir, "open_trades.json")
        self.log_file = os.path.join(self.state_dir, "deep_pocket_log.csv")
        self.pending_file = os.path.join(self.state_dir, "sheet_sync_pending.json")
        self.cutover_file = os.path.join(self.state_dir, "discord_alert_cutover.json")
        self.dashboard_reset_file = os.path.join(self.state_dir, "dashboard_reset.json")
        self.paths = (
            mock.patch.object(trade_tracker, "OPEN_FILE", self.open_file),
            mock.patch.object(trade_tracker, "JOURNAL_FILE", self.log_file),
            mock.patch.object(trade_tracker, "PENDING_SHEET_FILE", self.pending_file),
            mock.patch.object(trade_tracker, "DISCORD_CUTOVER_FILE", self.cutover_file),
        )
        for patcher in self.paths:
            patcher.start()

    def tearDown(self):
        for patcher in self.paths:
            patcher.stop()
        for name in (self.open_file, self.open_file + ".tmp", self.log_file,
                     self.pending_file, self.pending_file + ".tmp", self.cutover_file,
                     self.cutover_file + ".tmp", self.dashboard_reset_file,
                     self.dashboard_reset_file + ".tmp"):
            if os.path.exists(name):
                os.unlink(name)
        if os.path.isdir(self.state_dir):
            os.rmdir(self.state_dir)

    def test_sync_outbox_is_stable_and_acknowledgeable(self):
        row = {"event_id": "trade-1", "status": "CLOSED", "outcome": "SL_HIT"}
        trade_tracker.queue_sheet_rows([row, row])
        self.assertEqual(trade_tracker.pending_sheet_rows(), [row])
        trade_tracker.acknowledge_sheet_rows([row])
        self.assertEqual(trade_tracker.pending_sheet_rows(), [])
        self.assertFalse(os.path.exists(self.pending_file))

    def test_local_outcomes_are_written_once_across_retries(self):
        row = {"event_id": "trade-1", "event_time_utc": "2026-09-29T00:00:00+00:00",
               "symbol": "BTCUSDT", "status": "CLOSED", "outcome": "SL_HIT"}
        trade_tracker.append_local([row])
        trade_tracker.append_local([row, row])
        with open(self.log_file, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "SL_HIT")

    def test_settle_logs_separate_tp1_then_tp2_snapshots(self):
        trade = {
            "event_id": "trade-2", "symbol": "BTCUSDT", "scanner": "STRATEGY",
            "strategy_name": "Test", "direction": "LONG", "action": "BUY",
            "price": 100.0, "stop_loss": 90.0, "take_profit_1": 110.0,
            "take_profit_2": 120.0, "created_ts": 0, "position_size_usd": 100.0,
            "quantity": 1.0, "last_price": 100.0,
        }
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump([trade], f)
        klines = {
            "open_time": [60_000, 120_000, 180_000],
            "open": [100.0, 110.0, 120.0],
            "high": [111.0, 121.0, 120.0],
            "low": [99.0, 109.0, 119.0],
            "close": [110.0, 120.0, 120.0],
        }

        rows = trade_tracker.settle_open(lambda *_args, **_kwargs: klines)
        outcomes = [row for row in rows if row.get("outcome")]
        self.assertEqual([row["outcome"] for row in outcomes], ["TP1_HIT", "TP2_HIT"])
        self.assertEqual([row["tp1_status"] for row in outcomes], ["HIT", "HIT"])
        self.assertEqual([row["tp2_status"] for row in outcomes], ["PENDING", "HIT"])
        self.assertEqual(trade_tracker.pending_sheet_rows(), outcomes)
        with open(self.open_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])
        with open(self.log_file, newline="", encoding="utf-8") as f:
            saved = list(csv.DictReader(f))
        self.assertEqual([row["outcome"] for row in saved], ["TP1_HIT", "TP2_HIT"])

    def test_tp1_moves_stop_to_entry_and_later_closes_at_break_even(self):
        trade = {
            "event_id": "trade-be", "symbol": "BTCUSDT", "scanner": "STRATEGY",
            "strategy_name": "Test", "direction": "LONG", "action": "BUY",
            "price": 100.0, "stop_loss": 90.0, "take_profit_1": 110.0,
            "take_profit_2": 120.0, "created_ts": 1_000_000,
        }
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump([trade], f)
        candles = {"open_time": [1_060_000, 1_120_000, 1_180_000],
                   "open": [100.0, 110.0, 100.0], "high": [111.0, 105.0, 100.0],
                   "low": [99.0, 100.0, 100.0], "close": [110.0, 100.0, 100.0]}
        rows = trade_tracker.settle_open(lambda *_args, **_kwargs: candles)
        outcomes = [row for row in rows if row.get("outcome")]
        self.assertEqual([row["outcome"] for row in outcomes], ["TP1_HIT", "BE_HIT"])
        self.assertEqual(outcomes[0]["stop_loss"], 100.0)
        self.assertEqual(outcomes[1]["realized_pnl_usd"], 0.0)
        self.assertEqual(trade_tracker.load_open(), [])

    def test_pre_cutover_positions_are_silenced_without_deleting_them(self):
        items = [{"event_id": "old", "created_ts": 99}, {"event_id": "new", "created_ts": 101}]
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump(items, f)
        self.assertEqual(trade_tracker.silence_pre_cutover_positions(100), 1)
        saved = trade_tracker.load_open()
        self.assertTrue(saved[0]["suppress_discord_updates"])
        self.assertFalse(saved[1].get("suppress_discord_updates", False))
        self.assertEqual(len(saved), 2)

    def test_fresh_dashboard_reset_clears_old_rows_once_and_preserves_layout(self):
        with open(self.pending_file, "w", encoding="utf-8") as f:
            json.dump([{"event_id": "old-pending"}], f)
        calls = []
        with mock.patch.object(google_sheets, "FRESH_START_MARKER", Path(self.dashboard_reset_file)), \
                mock.patch.object(google_sheets, "PENDING_SHEET_FILE", Path(self.pending_file)), \
                mock.patch.object(google_sheets, "_fresh_start_performed", False), \
                mock.patch.object(google_sheets, "_request", side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or {}):
            self.assertTrue(google_sheets._reset_paper_ledgers_once())
            self.assertFalse(google_sheets._reset_paper_ledgers_once())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ("POST", "values:batchClear"))
        ranges = calls[0][1]["json"]["ranges"]
        self.assertIn("'Dashboard'!A1:Z", ranges)
        self.assertIn("'Positions'!A2:Z", ranges)
        self.assertIn("'Backtesting'!A2:Z", ranges)
        self.assertTrue(os.path.exists(self.dashboard_reset_file))
        self.assertFalse(os.path.exists(self.pending_file))

    def test_stale_reset_marker_from_older_version_does_not_block_new_reset(self):
        with open(self.dashboard_reset_file, "w", encoding="utf-8") as f:
            json.dump({"reset_at_utc": "2026-09-01T00:00:00+00:00"}, f)
        with mock.patch.object(google_sheets, "FRESH_START_MARKER", Path(self.dashboard_reset_file)), \
                mock.patch.object(google_sheets, "PENDING_SHEET_FILE", Path(self.pending_file)), \
                mock.patch.object(google_sheets, "_fresh_start_performed", False), \
                mock.patch.object(google_sheets, "_request", return_value={}) as req:
            self.assertTrue(google_sheets._reset_paper_ledgers_once())
            self.assertFalse(google_sheets._reset_paper_ledgers_once())
        self.assertEqual(req.call_count, 1)

    def test_tp1_discord_update_includes_manual_break_even_instruction(self):
        response = mock.Mock(status_code=204, text="")
        with mock.patch.object(screener, "DISCORD_WEBHOOK_URL", "https://example.invalid/hook"), \
                mock.patch.object(screener, "_post_discord", return_value=response) as post:
            screener.send_discord_position_update({
                "outcome": "TP1_HIT", "symbol": "BTCUSDT", "scanner": "STRATEGY",
                "action": "BUY", "exit_price": 110, "price": 100,
                "take_profit_1": 110, "take_profit_2": 120, "stop_loss": 100,
            })
        fields = post.call_args.args[0]["embeds"][0]["fields"]
        action = next(field["value"] for field in fields if field["name"] == "Action")
        self.assertIn("Move the remaining position Stop Loss UP to entry", action)
        self.assertEqual(next(f["value"] for f in fields if f["name"] == "New Stop Loss (Break Even)"),
                         "$100 (= entry)")

    def test_recovered_sheet_position_uses_watermark_and_marks_historical_event(self):
        trade = {
            "event_id": "trade-recovered", "symbol": "ETHUSDT", "scanner": "STRATEGY",
            "strategy_name": "Recovered", "direction": "LONG", "action": "BUY",
            "price": 100.0, "stop_loss": 90.0, "take_profit_1": 110.0,
            "take_profit_2": 120.0, "created_ts": 1_000_000,
            "checked_through_ts": 1_060_000, "position_size_usd": 100.0,
            "quantity": 1.0, "_recovered_from_sheet": True,
        }
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump([trade], f)
        klines = {"open_time": [1_120_000, 1_180_000, 1_240_000],
                  "open": [100.0, 95.0, 95.0], "high": [101.0, 96.0, 95.0],
                  "low": [89.0, 94.0, 94.0], "close": [90.0, 95.0, 95.0]}
        calls = []

        def fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return klines

        rows = trade_tracker.settle_open(fetch)
        hit = next(row for row in rows if row.get("outcome"))
        self.assertEqual(calls[0][1], {"start_time": 1_060_000})
        self.assertEqual(hit["outcome"], "SL_HIT")
        self.assertTrue(hit["recovered_from_sheet"])

    def test_local_position_resumes_from_last_checked_candle(self):
        trade = {
            "event_id": "trade-checkpoint", "symbol": "BTCUSDT", "scanner": "STRATEGY",
            "strategy_name": "Checkpoint", "direction": "LONG", "action": "BUY",
            "price": 100.0, "stop_loss": 90.0, "take_profit_1": 110.0,
            "take_profit_2": 120.0, "created_ts": 1_000_000,
            "checked_through_ts": 1_060_000, "position_size_usd": 150.0,
            "quantity": 1.5,
        }
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump([trade], f)
        klines = {"open_time": [1_120_000, 1_180_000], "open": [100.0, 100.0],
                  "high": [101.0, 101.0], "low": [99.0, 99.0], "close": [100.0, 100.0]}
        calls = []

        def fetch(*args, **kwargs):
            calls.append((args, kwargs))
            return klines

        trade_tracker.settle_open(fetch)
        self.assertEqual(calls[0][1], {"start_time": 1_060_000})

    def test_sheet_sync_batches_signal_position_and_backtest_updates(self):
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                if "Signals" in path:
                    return {"values": [google_sheets.SIGNAL_HEADERS]}
                if "Positions" in path:
                    return {"values": [google_sheets.POSITION_HEADERS]}
                if "Backtesting" in path:
                    return {"values": [google_sheets.BACKTEST_HEADERS]}
            return {}

        signal = {"event_id": "atomic-event", "event_time_utc": "2026-09-29T10:00:00+00:00",
                  "symbol": "BTCUSDT", "scanner": "STRATEGY · Test", "action": "SELL",
                  "status": "SIGNAL", "price": 100.0, "stop_loss": 110.0,
                  "take_profit_1": 90.0, "take_profit_2": 80.0,
                  "last_price": 100.0, "position_size_usd": 150.0, "quantity": 1.5}
        stop_hit = {**signal, "status": "CLOSED", "outcome": "SL_HIT",
                    "event_time_utc": "2026-09-29T10:02:00+00:00", "exit_price": 110.0,
                    "last_price": 110.0}
        with mock.patch.object(google_sheets, "_client", return_value=True), \
                mock.patch.object(google_sheets, "_ensure_tabs"), \
                mock.patch.object(google_sheets, "_request", side_effect=request):
            self.assertTrue(google_sheets.append_rows([signal, stop_hit]))

        writes = [(method, path, kwargs) for method, path, kwargs in calls if method == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1], "values:batchUpdate")
        data = writes[0][2]["json"]["data"]
        ranges = {item["range"]: item["values"] for item in data}
        self.assertEqual(set(ranges), {"'Signals'!A2:U3", "'Positions'!A2:U2", "'Backtesting'!A2:U2"})
        self.assertEqual([row[4] for row in ranges["'Signals'!A2:U3"]], ["SIGNAL", "SL_HIT"])
        self.assertEqual(ranges["'Positions'!A2:U2"][0][11], "SL_HIT")
        self.assertEqual(ranges["'Backtesting'!A2:U2"][0][17], "SL_HIT")

    def test_paper_trade_notional_matches_requested_100_usd(self):
        self.assertEqual(trade_tracker.TRADE_NOTIONAL_USD, 100.0)
        self.assertEqual(google_sheets.TRADE_NOTIONAL_USD, 100.0)
        self.assertEqual(google_sheets.STARTING_ACCOUNT_USD, 150.0)
        self.assertEqual(google_sheets.PAPER_NOTIONAL_VERSION, "paper-notional-100-v1")

    def test_watch_only_strategy_is_labeled_and_sized_for_forward_test(self):
        row = trade_tracker.journal_row({
            "event_id": "watch-1", "symbol": "BTCUSDT", "scanner": "STRATEGY",
            "strategy_name": "Consensus: Prime Edge, Quant Prime", "watch_only": True,
            "action": "BUY", "price": 100.0,
        })
        self.assertEqual(row["scanner"], "STRATEGY · WATCH ONLY · Consensus: Prime Edge, Quant Prime")
        self.assertEqual(row["position_size_usd"], 100.0)

    def test_user_selected_strategy_policy(self):
        # Review-week policy (Smart Vector solo is a two-sided watch-only forward test since v2.2), only two consensus pairs survive.
        self.assertEqual(strategy_policy.classify_strategy_group(["Smart Vector"], "LONG"), "WATCH_ONLY")
        self.assertEqual(strategy_policy.classify_strategy_group(["Smart Vector"], "SHORT"), "WATCH_ONLY")
        self.assertEqual(strategy_policy.classify_strategy_group(["Prime Edge", "Quant Prime"], "LONG"), "WATCH_ONLY")
        self.assertEqual(strategy_policy.classify_strategy_group(["Elite Trend", "Smart Vector"], "SHORT"), "WATCH_ONLY")
        self.assertIsNone(strategy_policy.classify_strategy_group(["Signal Edge", "Prime Edge", "Quant Prime"], "LONG"))
        self.assertIsNone(strategy_policy.classify_strategy_group(["Smart Vector", "Signal Edge"], "SHORT"))
        self.assertIsNone(strategy_policy.classify_strategy_group(["Apex Trend", "Smart Vector"], "LONG"))
        self.assertIsNone(strategy_policy.classify_strategy_group(["Trend Momentum"], "LONG"))

    def test_backtesting_reconcile_adds_missing_ids_and_refreshes_existing(self):
        def position(signal_id, coin, latest):
            return [signal_id, "2026-09-29 01:00:00 PKT", "STRATEGY", coin, "BUY",
                    "100", "90", "110", "PENDING", "120", "PENDING", "OPEN",
                    str(latest), "2026-09-29 01:30:00 PKT", "", "", "100", "1", "", "0", "0"]

        first = position("existing", "BTCUSDT", 105)
        second = position("missing", "ETHUSDT", 101)
        stale = google_sheets._migrate_backtest_from_position(first)
        stale[8] = 100
        calls = []

        def request(method, path, **kwargs):
            if method == "GET" and "Positions" in path:
                return {"values": [google_sheets.POSITION_HEADERS, first, second]}
            if method == "GET" and "Backtesting" in path:
                return {"values": [google_sheets.BACKTEST_HEADERS, stale]}
            calls.append((method, path, kwargs))
            return {}

        with mock.patch.object(google_sheets, "_request", side_effect=request):
            google_sheets._reconcile_backtesting_from_positions()

        self.assertEqual(len(calls), 1)
        method, path, kwargs = calls[0]
        self.assertEqual((method, path), ("POST", "values:batchUpdate"))
        writes = kwargs["json"]["data"]
        self.assertEqual({write["range"] for write in writes},
                         {"'Backtesting'!A2:U2", "'Backtesting'!A3:U3"})
        self.assertEqual(writes[0]["values"][0][8], 105.0)
        self.assertEqual(writes[1]["values"][0][0], "missing")

    def test_signal_hit_repairs_stale_open_position_from_same_signal_id(self):
        signal = ["run", "2026-09-29 09:06:00 PKT", "STRATEGY · Apex Trend", "LINKUSDT",
                  "SL_HIT", "SELL", "14.68", "15", "14.68", "14", "PENDING",
                  "13", "PENDING", "SL_HIT", "150", "10", "3.2", "10", "20", "", "link-event"]
        position = ["link-event", "2026-09-29 08:00:00 PKT", "STRATEGY · Apex Trend", "LINKUSDT",
                    "SELL", "15", "14.68", "14", "PENDING", "13", "PENDING", "OPEN",
                    "15", "2026-09-29 09:20:00 PKT", "", "", "150", "10", "", "0", "0"]
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and "Signals" in path:
                return {"values": [google_sheets.SIGNAL_HEADERS, signal]}
            if method == "GET" and "Positions" in path:
                return {"values": [google_sheets.POSITION_HEADERS, position]}
            return {}

        with mock.patch.object(google_sheets, "_request", side_effect=request):
            google_sheets._reconcile_positions_from_signals()

        writes = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(writes), 1)
        updated = writes[0][2]["json"]["data"][0]["values"][0]
        self.assertEqual(updated[11], "SL_HIT")
        self.assertEqual(updated[14], 14.68)
        self.assertEqual(updated[15], "2026-09-29 09:06:00 PKT")
        self.assertAlmostEqual(updated[18], 3.2)
        self.assertEqual(updated[19], "")

    def test_legacy_signal_without_id_repairs_only_unique_full_match(self):
        signal = ["run", "2026-09-29 09:06:00 PKT", "STRATEGY · Apex Trend", "WLDUSDT",
                  "SL_HIT", "SELL", "0.517422", "0.56", "0.517422", "0.645156",
                  "PENDING", "0.709023", "PENDING", "SL_HIT", "150", "267.857", "11.12", "22", "40", ""]
        position = ["wld-event", "2026-09-29 08:00:00 PKT", "STRATEGY · Apex Trend", "WLDUSDT",
                    "SELL", "0.56", "0.517422", "0.645156", "PENDING", "0.709023", "PENDING",
                    "OPEN", "0.56", "2026-09-29 09:20:00 PKT", "", "", "150", "267.857", "", "0", "0"]
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and "Signals" in path:
                return {"values": [google_sheets.SIGNAL_HEADERS, signal]}
            if method == "GET" and "Positions" in path:
                return {"values": [google_sheets.POSITION_HEADERS, position]}
            return {}

        with mock.patch.object(google_sheets, "_request", side_effect=request):
            google_sheets._reconcile_positions_from_signals()

        writes = [call for call in calls if call[0] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]["json"]["data"][0]["values"][0][11], "SL_HIT")

    def test_signal_hit_upsert_uses_stable_signal_id_across_runs(self):
        headers = google_sheets.SIGNAL_HEADERS
        old = ["old-run", "time", "STRATEGY", "BTCUSDT", "SL_HIT", "BUY"] + [""] * 14 + ["stable-id"]
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return {"values": [headers, old]}
            return {}

        row = ["new-run", "new-time", "STRATEGY", "BTCUSDT", "SL_HIT", "BUY"] + [""] * 14 + ["stable-id"]
        with mock.patch.object(google_sheets, "_request", side_effect=request), \
                mock.patch.object(google_sheets, "_fit_rows"):
            google_sheets._upsert_scan_row(row)
        self.assertEqual(len(calls), 2)
        self.assertIn("A2%3AU2", calls[1][1])

    def test_sheet_positions_restore_missing_local_state_and_drop_sheet_closed(self):
        local_trade = {"event_id": "already-closed", "symbol": "ETHUSDT", "action": "BUY",
                       "price": 100.0, "position_size_usd": 100.0}
        with open(self.open_file, "w", encoding="utf-8") as f:
            json.dump([local_trade], f)
        recovered = {"event_id": "recover-me", "symbol": "BTCUSDT", "action": "BUY",
                     "price": 100.0, "position_size_usd": 100.0,
                     "checked_through_ts": 123, "_recovered_from_sheet": True}
        with mock.patch.object(google_sheets, "load_open_position_state",
                               return_value=([recovered], {"already-closed"})):
            self.assertEqual(google_sheets.merge_sheet_position_state(), 1)
        saved = trade_tracker.load_open()
        self.assertEqual([trade["event_id"] for trade in saved], ["recover-me"])
        self.assertTrue(saved[0]["_recovered_from_sheet"])

    def test_recovery_candle_fetch_pages_and_resumes_from_last_candle(self):
        first_times = [i * 60_000 for i in range(1000)]
        second_times = [i * 60_000 for i in range(1000, 1003)]

        def batch(times):
            return {"open_time": times, "open": [1.0] * len(times), "high": [2.0] * len(times),
                    "low": [0.5] * len(times), "close": [1.5] * len(times)}

        with mock.patch.object(screener, "get_klines", side_effect=[batch(first_times), batch(second_times)]) as fetch, \
                mock.patch.object(screener.time, "sleep"):
            result = screener.get_tracker_klines("BTCUSDT", "1m", start_time=0)
        self.assertEqual(len(result["open_time"]), 1003)
        self.assertFalse(result["truncated"])
        self.assertEqual(fetch.call_args_list[1].kwargs["start_time"], first_times[-1] + 60_000)


    def test_scan_workflows_are_scheduled_hourly_just_after_candle_close(self):
        root = Path(__file__).resolve().parents[1]
        expected = ['- cron: "3 * * * *"']
        for workflow in (root / ".github/workflows/scan.yml", root / "workflows/scan.yml"):
            lines = workflow.read_text(encoding="utf-8").splitlines()
            schedule_lines = [line.strip() for line in lines if line.strip().startswith("- cron:")]
            self.assertEqual(schedule_lines, expected, workflow.as_posix())


if __name__ == "__main__":
    unittest.main()
