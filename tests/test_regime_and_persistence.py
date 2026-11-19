import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import market_regime as mr
import screener
import trade_tracker


class RegimeScoringTests(unittest.TestCase):
    def test_label_thresholds_and_coverage(self):
        self.assertEqual(mr.label_for(55, 1.0), "STRONG BULLISH")
        self.assertEqual(mr.label_for(20, 1.0), "BULLISH")
        self.assertEqual(mr.label_for(0, 1.0), "NEUTRAL")
        self.assertEqual(mr.label_for(-20, 1.0), "BEARISH")
        self.assertEqual(mr.label_for(-70, 1.0), "STRONG BEARISH")
        self.assertEqual(mr.label_for(50, 0.2), "UNKNOWN")   # too few factors available

    def test_combine_renormalises_over_available_parts(self):
        comps = {"btc_momentum": (60, ""), "news": (None, ""), "macro": (-20, ""), "us10y": (None, ""),
                 "sentiment": (None, ""), "global": (None, ""), "derivatives": (None, "")}
        score, coverage = mr.combine(comps)
        w = mr.WEIGHTS
        self.assertAlmostEqual(score, (60 * w["btc_momentum"] - 20 * w["macro"]) / (w["btc_momentum"] + w["macro"]), places=6)
        self.assertAlmostEqual(coverage, w["btc_momentum"] + w["macro"], places=6)

    def test_one_failing_source_does_not_break_regime(self):
        def boom():
            raise RuntimeError("blocked")
        fetchers = {"btc_momentum": lambda: (40.0, "up"), "news": lambda: (10.0, "n"), "macro": boom,
                    "us10y": lambda: (-10.0, "y"), "sentiment": lambda: (20.0, "s"),
                    "global": lambda: (10.0, "g"), "derivatives": lambda: (0.0, "d")}
        reg = mr.compute_regime(fetchers=fetchers)
        self.assertIsNotNone(reg["score"])
        self.assertIsNone(reg["components"]["macro"]["score"])
        self.assertGreaterEqual(reg["coverage"], 0.8)

    def test_headline_scoring_direction_and_dedupe(self):
        bull = [{"title": f"Bitcoin ETF inflows surge, rate cut hopes {i}", "age_hours": 1} for i in range(4)]
        bear = [{"title": f"Exchange hacked, crypto crash fears {i}", "age_hours": 1} for i in range(4)]
        self.assertGreater(mr.score_headlines(bull)[0], 30)
        self.assertLess(mr.score_headlines(bear)[0], -30)
        dup = [{"title": "Bitcoin crash", "age_hours": 1}] * 5
        self.assertIsNone(mr.score_headlines(dup)[0])           # duplicates collapse -> not enough items

    def test_old_news_counts_less(self):
        fresh = mr.score_headlines([{"title": f"Bitcoin crash {i}", "age_hours": 0} for i in range(3)])[0]
        old = mr.score_headlines([{"title": f"Bitcoin crash {i}", "age_hours": 24} for i in range(3)])[0]
        self.assertLess(fresh, old)  # both negative; fresh is more negative

    def test_alignment_and_gate(self):
        bear = {"score": -50, "label": "STRONG BEARISH"}
        self.assertEqual(mr.alignment(bear, True), "COUNTER-TREND")
        self.assertEqual(mr.alignment(bear, False), "ALIGNED")
        self.assertTrue(mr.blocks_signal(bear, True))
        self.assertFalse(mr.blocks_signal(bear, False))
        self.assertFalse(mr.blocks_signal({"score": -50, "label": "UNKNOWN"}, True))
        self.assertFalse(mr.blocks_signal(None, True))
        self.assertFalse(mr.blocks_signal({"score": -30, "label": "BEARISH"}, True))   # below gate threshold

    def test_annotate_details(self):
        sig = {"action": "BUY", "details": {"x": 1}}
        mr.annotate_details(sig, {"score": -30, "label": "BEARISH"})
        self.assertEqual(sig["details"]["regime_alignment"], "COUNTER-TREND")
        self.assertEqual(sig["details"]["x"], 1)
        sig2 = {"action": "SELL"}
        mr.annotate_details(sig2, None)
        self.assertNotIn("details", sig2)

    def test_cache_and_report_cadence(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mr, "REGIME_FILE", os.path.join(tmp, "r.json")):
            reg = {"computed_at": time.time(), "score": 10.0, "label": "NEUTRAL", "coverage": 1.0}
            mr.save_regime(reg)
            with mock.patch.object(mr, "compute_regime", side_effect=AssertionError("must use cache")):
                self.assertEqual(mr.get_regime()["score"], 10.0)
            self.assertTrue(mr.report_due(reg))               # never reported yet
            mr.mark_reported(reg)
            self.assertFalse(mr.report_due(reg))              # same label, just reported
            self.assertTrue(mr.report_due(dict(reg, label="BEARISH")))   # label changed
            self.assertEqual(mr.get_regime()["last_report_label"], "NEUTRAL")   # report state survives rewrites


class DiscordRegimeInjectionTests(unittest.TestCase):
    def test_signal_and_summary_embeds_get_regime_field_but_position_updates_do_not(self):
        regime = {"score": -45, "label": "STRONG BEARISH"}
        with mock.patch.object(screener, "CURRENT_REGIME", regime):
            buy = screener._inject_regime({"embeds": [{"title": "🧭 X — BUY", "fields": [
                {"name": "Direction", "value": "BUY"}]}]})
            sell = screener._inject_regime({"embeds": [{"title": "🧭 X — SELL", "fields": [
                {"name": "Direction", "value": "SHORT / SELL"}]}]})
            summary = screener._inject_regime({"embeds": [{"title": "📊 Scan Report", "fields": []}]})
            update = screener._inject_regime({"embeds": [{"title": "📍 TP1", "fields": []}]})
        self.assertIn("counter-trend", buy["embeds"][0]["fields"][-1]["value"])
        self.assertIn("aligned", sell["embeds"][0]["fields"][-1]["value"])
        self.assertEqual(summary["embeds"][0]["fields"][-1]["name"], "BTC / Market Regime")
        self.assertEqual(update["embeds"][0]["fields"], [])

    def test_no_regime_means_no_change(self):
        with mock.patch.object(screener, "CURRENT_REGIME", None):
            payload = {"embeds": [{"title": "🧭 X — BUY", "fields": []}]}
            self.assertEqual(screener._inject_regime(payload)["embeds"][0]["fields"], [])


class PersistenceSafetyTests(unittest.TestCase):
    def test_missing_cutover_file_silences_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "discord_alert_cutover.json")
            with mock.patch.object(trade_tracker, "DISCORD_CUTOVER_FILE", path):
                cutover = trade_tracker.load_or_create_discord_cutover()
                self.assertEqual(cutover, 0)
                trades = [{"event_id": "a", "created_ts": time.time() * 1000}]
                self.assertEqual(trade_tracker.silence_pre_cutover_positions(cutover, items=trades), 0)
                self.assertFalse(trades[0].get("suppress_discord_updates"))

    def test_workflow_stages_each_state_file_individually(self):
        for rel in (".github/workflows/scan.yml", "workflows/scan.yml"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("for f in", text)
            self.assertIn("market_regime.json", text)
            self.assertIn("--autostash", text)
            self.assertNotIn("dashboard_reset.json || true", text)

    def test_shipped_state_files_exist_and_are_clean(self):
        import json
        self.assertIsInstance(json.loads((ROOT / "open_trades.json").read_text()), list)
        self.assertEqual(json.loads((ROOT / "discord_alert_cutover.json").read_text())["cutover_ts"], 0)
        # dashboard_reset.json is written by the first successful run (Sheet cleared once per FRESH_START_VERSION);
        # when present it must be valid JSON that carries a reset_version.
        marker = ROOT / "dashboard_reset.json"
        if marker.exists():
            self.assertIn("reset_version", json.loads(marker.read_text()))


class BreakEvenFlowTests(unittest.TestCase):
    def test_tp1_moves_stop_to_entry_then_be_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            patches = (mock.patch.object(trade_tracker, "OPEN_FILE", os.path.join(tmp, "o.json")),
                       mock.patch.object(trade_tracker, "JOURNAL_FILE", os.path.join(tmp, "l.csv")),
                       mock.patch.object(trade_tracker, "PENDING_SHEET_FILE", os.path.join(tmp, "p.json")))
            for p in patches: p.start()
            try:
                trade_tracker.save_open([{
                    "event_id": "t1", "symbol": "TESTUSDT", "scanner": "WHALE_FLOW", "direction": "LONG",
                    "action": "BUY", "price": 100.0, "stop_loss": 95.0, "take_profit_1": 110.0,
                    "take_profit_2": 120.0, "created_ts": 0, "quantity": 1.0}])

                def candles(rows):
                    return {"open_time": [60_000 * (i + 1) for i in range(len(rows) + 1)],
                            "high": [r[0] for r in rows] + [0], "low": [r[1] for r in rows] + [0],
                            "close": [r[2] for r in rows] + [0]}
                # candle 1 reaches TP1; candle 2 falls back to entry -> BE_HIT
                rows = trade_tracker.settle_open(lambda *a, **k: candles([(111, 100.5, 108), (109, 99.5, 100)]))
                outcomes = [r["outcome"] for r in rows if r.get("outcome")]
                self.assertEqual(outcomes, ["TP1_HIT", "BE_HIT"])
                tp1 = next(r for r in rows if r.get("outcome") == "TP1_HIT")
                self.assertEqual(float(tp1["stop_loss"]), 100.0)       # SL already moved to entry on the TP1 event
                self.assertEqual(trade_tracker.load_open(), [])
            finally:
                for p in patches: p.stop()


if __name__ == "__main__":
    unittest.main()
