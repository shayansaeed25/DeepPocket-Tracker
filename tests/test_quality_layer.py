import csv, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import quality_gate as qg
import strategy_policy as sp
import google_sheets as gs


def trending(n=80, step=0.5, start=100.0, vol=1000.0):
    closes = [start + i * step for i in range(n)]
    return {"close": closes, "high": [c * 1.003 for c in closes], "low": [c * 0.997 for c in closes],
            "volume": [vol] * n}


class TrendAndVolumeTests(unittest.TestCase):
    def test_trend_direction(self):
        up, down = trending(step=0.5), trending(step=-0.5, start=200)
        self.assertEqual(qg.htf_trend(up, True), 1)
        self.assertEqual(qg.htf_trend(up, False), -1)
        self.assertEqual(qg.htf_trend(down, False), 1)
        self.assertEqual(qg.htf_trend(down, True), -1)
        self.assertEqual(qg.htf_trend({"close": [1.0] * 10}, True), 0)       # not enough data -> mixed
        self.assertEqual(qg.htf_trend(None, True), 0)

    def test_mixed_trend(self):
        k = trending(step=0.5)
        k["close"][-1] = 90.0                                                 # price drops under the slow EMA, EMAs still up
        self.assertEqual(qg.htf_trend(k, True), 0)

    def test_volume_ratio_uses_last_closed_candle(self):
        k = {"volume": [1000.0] * 30}
        k["volume"][-2] = 2000.0                                              # last closed candle doubles the average
        k["volume"][-1] = 10.0                                                # still-forming candle must be ignored
        self.assertAlmostEqual(qg.volume_ratio(k), 2.0, places=6)
        self.assertIsNone(qg.volume_ratio({"volume": [1.0] * 5}))

    def test_htf_block_only_when_both_oppose(self):
        self.assertTrue(qg.htf_blocks(-1, -1))
        self.assertFalse(qg.htf_blocks(-1, 0))
        self.assertFalse(qg.htf_blocks(-1, 1))


class ScoreTests(unittest.TestCase):
    def test_perfect_solo_signal(self):
        score, parts = qg.quality_score(True, 1, "ALIGNED", 1, 1, 2.0)
        self.assertEqual(parts, {"trend": 30.0, "volume": 25.0, "consensus": 10.0, "regime": 20.0})
        self.assertEqual(score, 85.0)

    def test_consensus_and_counter_trend(self):
        better, _ = qg.quality_score(True, 3, "ALIGNED", 1, 1, 1.5)
        worse, parts = qg.quality_score(True, 1, "COUNTER-TREND", -1, 0, 0.5)
        self.assertEqual(better, 100.0)
        self.assertLess(worse, qg.QUALITY_MIN_SCORE)
        self.assertEqual(parts["regime"], 0.0)

    def test_unknown_inputs_get_half_credit(self):
        score, parts = qg.quality_score(True, 2, "UNKNOWN", 0, 0, None)
        self.assertEqual(parts["volume"], 12.5)
        self.assertEqual(parts["regime"], 10.0)
        self.assertEqual(parts["trend"], 15.0)

    def test_solo_needs_real_confirmation_to_pass(self):
        # solo (10 pts) with mixed trend, average volume and neutral regime must NOT pass the default bar
        score, _ = qg.quality_score(True, 1, "NEUTRAL", 0, 0, 1.0)
        self.assertLess(score, qg.QUALITY_MIN_SCORE)


class StopAndSizeTests(unittest.TestCase):
    def test_atr_stop_is_clamped(self):
        calm = trending(step=0.0)                                             # range 0.6% -> tiny ATR
        self.assertEqual(qg.atr_stop_pct(calm), qg.WHALE_FLOW_MIN_STOP_PCT)
        wild = trending(step=0.0)
        wild["high"] = [c * 1.06 for c in wild["close"]]
        wild["low"] = [c * 0.94 for c in wild["close"]]
        self.assertEqual(qg.atr_stop_pct(wild), qg.WHALE_FLOW_MAX_STOP_PCT)
        self.assertEqual(qg.atr_stop_pct(None), qg.WHALE_FLOW_DEFAULT_STOP_PCT)
        self.assertEqual(qg.atr_stop_pct({"close": [1.0] * 5}), qg.WHALE_FLOW_DEFAULT_STOP_PCT)

    def test_atr_stop_in_between_scales_with_volatility(self):
        k = trending(step=0.0)
        k["high"] = [c * 1.006 for c in k["close"]]
        k["low"] = [c * 0.994 for c in k["close"]]                            # ATR ~1.2% -> 1.8%
        self.assertAlmostEqual(qg.atr_stop_pct(k), 1.8, delta=0.05)

    def test_size_for_one_percent_risk(self):
        self.assertEqual(qg.size_for_risk(100, 97.5), 60.0)                   # $1.5 risk / 2.5%
        self.assertEqual(qg.size_for_risk(100, 98.0), 75.0)
        self.assertIsNone(qg.size_for_risk(100, 100))

    def test_annotate_sizing(self):
        sig = qg.annotate_sizing({"price": 100.0, "stop_loss": 98.0, "details": {"x": 1}})
        self.assertEqual(sig["details"]["size_1pct_risk_usd"], 75.0)
        self.assertEqual(sig["details"]["x"], 1)
        bad = qg.annotate_sizing({"price": 0, "stop_loss": 1})
        self.assertNotIn("details", bad)


class CircuitBreakerTests(unittest.TestCase):
    def _journal(self, outcomes_with_age_h):
        now = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="", encoding="utf-8")
        writer = csv.DictWriter(tmp, fieldnames=["event_id", "event_time_utc", "outcome"])
        writer.writeheader()
        for i, (outcome, age) in enumerate(outcomes_with_age_h):
            writer.writerow({"event_id": str(i), "event_time_utc": (now - timedelta(hours=age)).isoformat(),
                             "outcome": outcome})
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name, now

    def test_three_recent_stops_pause(self):
        path, now = self._journal([("TP2_HIT", 9), ("SL_HIT", 3), ("SL_HIT", 2), ("SL_HIT", 0.5)])
        self.assertIn("3 stop-outs in a row", qg.loss_streak_pause(path, now=now))

    def test_old_streak_does_not_pause(self):
        path, now = self._journal([("SL_HIT", 8), ("SL_HIT", 7), ("SL_HIT", 6)])
        self.assertIsNone(qg.loss_streak_pause(path, now=now))

    def test_a_win_in_the_last_three_resets(self):
        path, now = self._journal([("SL_HIT", 3), ("BE_HIT", 2), ("SL_HIT", 0.5)])
        self.assertIsNone(qg.loss_streak_pause(path, now=now))

    def test_tp1_events_are_not_terminal(self):
        path, now = self._journal([("SL_HIT", 3), ("SL_HIT", 2), ("TP1_HIT", 1), ("SL_HIT", 0.5)])
        self.assertIn("stop-outs", qg.loss_streak_pause(path, now=now))

    def test_events_from_this_run_count(self):
        path, now = self._journal([("SL_HIT", 3)])
        extra = [{"event_time_utc": (now - timedelta(minutes=20)).isoformat(), "outcome": "SL_HIT"},
                 {"event_time_utc": (now - timedelta(minutes=10)).isoformat(), "outcome": "SL_HIT"}]
        self.assertIsNotNone(qg.loss_streak_pause(path, now=now, extra_rows=extra))

    def test_missing_or_short_journal(self):
        self.assertIsNone(qg.loss_streak_pause("/nonexistent/file.csv"))
        path, now = self._journal([("SL_HIT", 0.2), ("SL_HIT", 0.1)])
        self.assertIsNone(qg.loss_streak_pause(path, now=now))


class PolicyAndTableTests(unittest.TestCase):
    def test_only_one_signal_edge_combo_survives(self):
        self.assertEqual(sp.classify_strategy_group(["Elite Trend", "Quant Prime", "Signal Edge"], "LONG"), "WATCH_ONLY")
        for combo in (["Smart Vector", "Signal Edge"], ["Prime Edge", "Signal Edge"], ["Quant Prime", "Signal Edge"],
                      ["Elite Trend", "Smart Vector", "Signal Edge"], ["Signal Edge"]):
            self.assertIsNone(sp.classify_strategy_group(combo, "SHORT"), combo)

    def test_table_has_profit_factor_and_verdict(self):
        headers = gs.ANALYTICS_STRATEGY_ROWS[0]
        self.assertIn("Profit factor", headers)
        self.assertTrue(headers[-1].startswith("Verdict"))
        solo = gs.ANALYTICS_STRATEGY_ROWS[1]
        self.assertIn('Backtesting!S:S,">0"', solo[9])
        self.assertIn("KEEP", solo[13])
        self.assertIn("NEED 20+ CLOSED", solo[13])

    def test_diff_row_subtracts_consensus_with_smart_vector(self):
        labels = [r[0] for r in gs.ANALYTICS_STRATEGY_ROWS]
        row = gs.ANALYTICS_STRATEGY_ROWS[labels.index("Consensus WITHOUT Smart Vector")]
        self.assertIn("-COUNTIFS", row[1])
        self.assertIn("Smart Vector*", row[1])
        self.assertIn("-SUMIFS", row[8])


if __name__ == "__main__":
    unittest.main()
