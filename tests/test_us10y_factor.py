import os, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import market_regime as mr
import screener


def regime_with(closes, score=10.0, label="NEUTRAL"):
    s, note, info = mr.score_us10y(closes)
    return {"score": score, "label": label, "us10y": info}


class ScoreTests(unittest.TestCase):
    def test_flat_yield_is_neutral(self):
        score, note, info = mr.score_us10y([4.28] * 10)
        self.assertEqual(score, 0.0)
        self.assertEqual(info["level"], 4.28)

    def test_rising_yield_is_a_headwind(self):
        score, note, info = mr.score_us10y([4.10, 4.12, 4.15, 4.18, 4.22, 4.30])
        self.assertLess(score, -50)
        self.assertEqual(info["day_bps"], 8.0)
        self.assertEqual(info["week_bps"], 20.0)
        self.assertIn("headwind", note)

    def test_falling_yield_is_a_tailwind(self):
        score, _, _ = mr.score_us10y([4.40, 4.38, 4.34, 4.30, 4.25, 4.20])
        self.assertGreater(score, 30)

    def test_level_adjustment(self):
        high = mr.score_us10y([4.95] * 10)[0]
        low = mr.score_us10y([3.5] * 10)[0]
        self.assertEqual(high, -10.0)
        self.assertEqual(low, 10.0)
        self.assertEqual(mr.score_us10y([5.2] * 10)[0], -20.0)

    def test_feed_quoted_times_ten_is_normalised(self):
        _, _, info = mr.score_us10y([42.8, 42.9, 43.0, 43.1, 43.4, 43.8])
        self.assertAlmostEqual(info["level"], 4.38, places=3)

    def test_too_little_data(self):
        self.assertIsNone(mr.score_us10y([4.2])[0])
        self.assertIsNone(mr.score_us10y([])[0])
        self.assertIsNone(mr.score_us10y([None, None])[0])


class AlignmentAndGateTests(unittest.TestCase):
    RISING = [4.10, 4.12, 4.15, 4.18, 4.22, 4.30]
    FALLING = [4.50, 4.46, 4.40, 4.34, 4.26, 4.18]

    def test_rising_yields_hurt_longs_and_help_shorts(self):
        reg = regime_with(self.RISING)
        self.assertEqual(mr.yield_alignment(reg, True), "HEADWIND")
        self.assertEqual(mr.yield_alignment(reg, False), "TAILWIND")

    def test_falling_yields_help_longs(self):
        reg = regime_with(self.FALLING)
        self.assertEqual(mr.yield_alignment(reg, True), "TAILWIND")
        self.assertEqual(mr.yield_alignment(reg, False), "HEADWIND")

    def test_movers_gate_only_on_sharp_moves(self):
        rising = regime_with(self.RISING)
        self.assertTrue(mr.yield_blocks_signal(rising, True))
        self.assertFalse(mr.yield_blocks_signal(rising, False))
        mild = regime_with([4.28, 4.28, 4.29, 4.29, 4.30, 4.31])
        self.assertFalse(mr.yield_blocks_signal(mild, True))
        self.assertFalse(mr.yield_blocks_signal({"score": 5, "label": "NEUTRAL"}, True))   # no yield data
        self.assertFalse(mr.yield_blocks_signal(None, True))

    def test_annotate_details_records_yield(self):
        sig = {"action": "BUY", "details": {}}
        mr.annotate_details(sig, regime_with(self.RISING))
        d = sig["details"]
        self.assertEqual(d["us10y"], 4.3)
        self.assertEqual(d["us10y_day_bps"], 8.0)
        self.assertEqual(d["us10y_alignment"], "HEADWIND")

    def test_old_cached_regime_without_yield_still_works(self):
        sig = {"action": "SELL"}
        mr.annotate_details(sig, {"score": 10, "label": "NEUTRAL"})
        self.assertNotIn("us10y", sig["details"])
        self.assertEqual(mr.yield_text({"score": 10}, False), "unavailable")


class DiscordTests(unittest.TestCase):
    def test_signal_alert_gets_yield_field_per_direction(self):
        reg = regime_with(AlignmentAndGateTests.RISING)
        with mock.patch.object(screener, "CURRENT_REGIME", reg):
            buy = screener._inject_regime({"embeds": [{"title": "🧭 X — BUY", "fields": [
                {"name": "Direction", "value": "BUY"}]}]})
            sell = screener._inject_regime({"embeds": [{"title": "🧭 X — SELL", "fields": [
                {"name": "Direction", "value": "SELL"}]}]})
        by_name = lambda p: {f["name"]: f["value"] for f in p["embeds"][0]["fields"]}
        self.assertIn("moving against this trade", by_name(buy)["US 10Y Yield"])
        self.assertIn("support this trade", by_name(sell)["US 10Y Yield"])
        self.assertIn("4.30%", by_name(buy)["US 10Y Yield"])

    def test_scan_report_and_position_updates(self):
        reg = regime_with(AlignmentAndGateTests.RISING)
        with mock.patch.object(screener, "CURRENT_REGIME", reg):
            summary = screener._inject_regime({"embeds": [{"title": "📊 Scan Report", "fields": []}]})
            update = screener._inject_regime({"embeds": [{"title": "📍 TP1", "fields": []}]})
        self.assertEqual([f["name"] for f in summary["embeds"][0]["fields"]], ["BTC / Market Regime", "US 10Y Yield"])
        self.assertEqual(update["embeds"][0]["fields"], [])

    def test_report_embed_lists_us10y_component(self):
        reg = mr.compute_regime(fetchers={"btc_momentum": lambda: (10.0, "b"), "us10y": lambda: (-30.0, "4.3% rising")})
        names = [f["name"] for f in mr.build_report_embed(reg)["embeds"][0]["fields"]]
        self.assertTrue(any(n.startswith("US 10Y Yield") for n in names))


class FetchTests(unittest.TestCase):
    def test_yahoo_primary(self):
        chart = {"chart": {"result": [{"indicators": {"quote": [{"close": [4.1, 4.12, 4.15, 4.18, 4.22, 4.30]}]}}]}}
        with mock.patch.object(mr, "_get_json", return_value=chart):
            score, note = mr._fetch_us10y()
        self.assertLess(score, -50)
        self.assertEqual(mr._fetch_us10y.info["level"], 4.3)

    def test_fred_fallback_when_yahoo_fails(self):
        import requests
        class Resp:
            text = "observation_date,DGS10\n2026-09-29,4.20\n2026-09-30,.\n2026-10-01,4.22\n2026-10-02,4.25\n" \
                   "2026-10-03,4.27\n2026-10-04,4.30\n2026-10-05,4.34\n"
            def raise_for_status(self): pass
        with mock.patch.object(mr, "_get_json", side_effect=requests.ConnectionError("blocked")), \
             mock.patch.object(mr.requests, "get", return_value=Resp()):
            score, note = mr._fetch_us10y()
        self.assertIsNotNone(score)
        self.assertEqual(mr._fetch_us10y.info["level"], 4.34)     # the '.' (holiday) row is skipped

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(mr.WEIGHTS.values()), 1.0, places=6)
        self.assertIn("us10y", mr.FETCHERS)


if __name__ == "__main__":
    unittest.main()
