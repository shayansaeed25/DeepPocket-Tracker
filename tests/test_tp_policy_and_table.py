import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import trade_tracker as tt
import google_sheets as gs
import screener


def sig(entry, sl, tp1, tp2, action="BUY", **extra):
    base = {"symbol": "TESTUSDT", "scanner": "STRATEGY", "action": action,
            "direction": "LONG" if action == "BUY" else "SHORT",
            "price": entry, "stop_loss": sl, "take_profit_1": tp1, "take_profit_2": tp2}
    base.update(extra)
    return base


class TpPolicyTests(unittest.TestCase):
    def test_wide_stop_gets_capped_at_five_dollars(self):
        s = tt.apply_tp_policy(sig(100, 91.6, 116.8, 129.4))         # 8.4% stop, old TP1 +16.8%
        self.assertAlmostEqual((s["take_profit_1"] - 100), 5.0, places=4)
        self.assertAlmostEqual((s["take_profit_2"] - 100), 12.5, places=4)
        self.assertEqual(s["stop_loss"], 91.6)                       # stop is never touched
        self.assertEqual(s["details"]["tp1_usd"], 5.0)

    def test_tight_stop_gets_two_dollar_floor(self):
        s = tt.apply_tp_policy(sig(100, 98.3, 103.4, 106.0))         # 1.7% stop, old TP1 +3.4%
        self.assertAlmostEqual(s["take_profit_1"] - 100, 2.0, places=4)
        self.assertGreater(s["take_profit_2"], s["take_profit_1"])
        self.assertLessEqual(s["take_profit_2"] - 100, 6.0 + 1e-6)   # never farther than the old TP2

    def test_mid_stop_uses_stop_distance(self):
        s = tt.apply_tp_policy(sig(100, 96.25, 107.5, 113.1))        # 3.75% stop
        self.assertAlmostEqual(s["take_profit_1"] - 100, 3.75, places=4)

    def test_never_widens_a_target(self):
        s = tt.apply_tp_policy(sig(100, 99.2, 101.6, 102.8))         # tiny stop, old TP1 only +1.6%
        self.assertAlmostEqual(s["take_profit_1"] - 100, 1.6, places=4)
        self.assertLessEqual(s["take_profit_2"], 102.8 + 1e-9)
        self.assertGreater(s["take_profit_2"], s["take_profit_1"])

    def test_short_side_mirrors(self):
        s = tt.apply_tp_policy(sig(100, 103.75, 92.5, 86.9, action="SELL"))
        self.assertAlmostEqual(100 - s["take_profit_1"], 3.75, places=4)
        self.assertLess(s["take_profit_2"], s["take_profit_1"])
        self.assertEqual(s["stop_loss"], 103.75)

    def test_idempotent_and_marks_partial(self):
        s = tt.apply_tp_policy(sig(100, 96, 108, 114))
        again = tt.apply_tp_policy(dict(s))
        self.assertEqual(again["take_profit_1"], s["take_profit_1"])
        self.assertEqual(s["partial_tp1_pct"], tt.TP1_PARTIAL_PCT)
        self.assertEqual(s["tp_policy"], tt.TP_POLICY_VERSION)

    def test_bad_input_is_left_alone(self):
        s = tt.apply_tp_policy({"price": 0, "stop_loss": 1, "take_profit_1": 2, "take_profit_2": 3})
        self.assertNotIn("tp_policy", s)

    def test_discord_labels_show_dollars(self):
        s = tt.apply_tp_policy(sig(100, 96, 108, 114))
        self.assertIn("on $100", screener._tp_label(s, 1))
        self.assertIn("SL → entry", screener._tp_label(s, 1))
        self.assertEqual(screener._tp_label({}, 1), "Take Profit 1 (2R)")


class PolicyAccountingFlowTests(unittest.TestCase):
    def _run(self, candle_rows, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            patches = (mock.patch.object(tt, "OPEN_FILE", os.path.join(tmp, "o.json")),
                       mock.patch.object(tt, "JOURNAL_FILE", os.path.join(tmp, "l.csv")),
                       mock.patch.object(tt, "PENDING_SHEET_FILE", os.path.join(tmp, "p.json")))
            for p in patches: p.start()
            try:
                s = tt.apply_tp_policy(sig(100, 96, 108, 114, created_ts=0, event_id="t"))
                tt.save_open([dict(s, quantity=1.0)])
                def klines(*a, **k):
                    return {"open_time": [60_000 * (i + 1) for i in range(len(candle_rows) + 1)],
                            "high": [r[0] for r in candle_rows] + [0], "low": [r[1] for r in candle_rows] + [0],
                            "close": [r[2] for r in candle_rows] + [0]}
                return s, tt.settle_open(klines)
            finally:
                for p in patches: p.stop()

    def test_tp1_then_reversal_is_a_small_profit_not_zero(self):
        s, rows = self._run([(s_hi, 99.0, 103) for s_hi in (104.5,)] + [(103, 99.5, 100)])
        events = [r for r in rows if r.get("outcome")]
        self.assertEqual([r["outcome"] for r in events], ["TP1_HIT", "BE_HIT"])
        closed = events[-1]
        # 4% stop -> TP1 = 4% = $4 on a full $100 position; half booked -> +$2, rest closed at entry -> 0
        self.assertAlmostEqual(closed["realized_pnl_usd"], 0.5 * (s["take_profit_1"] - 100), places=4)
        self.assertAlmostEqual(closed["realized_pnl_usd"], 2.0, places=4)

    def test_candle_after_tp1_touching_both_tp1_and_entry_is_not_ambiguous(self):
        # candle 1 takes TP1; candle 2 wicks above TP1 again AND back to entry -> must be BE_HIT, not AMBIGUOUS
        s, rows = self._run([(104.5, 99.5, 103), (104.0, 99.9, 100)])
        outcomes = [r["outcome"] for r in rows if r.get("outcome")]
        self.assertEqual(outcomes, ["TP1_HIT", "BE_HIT"])

    def test_peak_profit_and_drawdown_are_recorded(self):
        s, rows = self._run([(102.0, 99.0, 101), (103.0, 98.0, 99), (97.0, 95.0, 96)])   # ends at the stop
        closed = [r for r in rows if r.get("outcome") == "SL_HIT"][0]
        import json
        d = json.loads(closed["details"])
        self.assertAlmostEqual(d["peak_profit_pct"], 3.0, places=2)
        self.assertLess(d["max_drawdown_pct"], -3.9)


class DashboardTableTests(unittest.TestCase):
    def test_shape_and_columns(self):
        rows = gs.STRATEGY_TABLE_ROWS
        self.assertEqual(len(rows), 1 + len(gs.STRATEGY_TABLE_GROUPS))
        self.assertTrue(all(len(r) == 9 for r in rows))                      # D..L
        labels = [r[0] for r in rows]
        for needed in ("Smart Vector · solo", "Whale Flow · on-chain DEX", "Movers Pullback", "ALL SOURCES"):
            self.assertIn(needed, labels)

    def test_formulas_use_the_right_backtesting_columns(self):
        solo = gs.STRATEGY_TABLE_ROWS[1]
        self.assertIn('Backtesting!E:E,"STRATEGY · Smart Vector"', solo[1])
        self.assertIn('Backtesting!O:O,"HIT"', solo[3])          # TP1 touched
        self.assertIn('Backtesting!Q:Q,"HIT"', solo[4])          # TP2 hit
        self.assertIn('"SL_HIT"', solo[5])
        self.assertIn('"BE_HIT"', solo[6])
        self.assertEqual(solo[7], '=IFERROR(G2/(G2+I2),"-")')     # TP1 ÷ (TP1 + SL)
        self.assertIn("SUMIFS(Backtesting!S:S", solo[8])

    def test_consensus_without_smart_vector_is_a_difference_row(self):
        labels = [r[0] for r in gs.STRATEGY_TABLE_ROWS]
        row = gs.STRATEGY_TABLE_ROWS[labels.index("Consensus WITHOUT Smart Vector")]
        all_row = labels.index("All consensus (watch-only)") + 1
        sv_row = labels.index("Smart Vector · inside any consensus") + 1
        self.assertEqual(row[1], f"=E{all_row}-E{sv_row}")
        self.assertEqual(row[8], f"=L{all_row}-L{sv_row}")

    def test_existing_dashboard_rows_are_untouched(self):
        labels = [r[0] for r in gs.DASHBOARD_ROWS]
        self.assertEqual(labels[0], "DeepPocket · $100 Paper-Trade Dashboard")
        self.assertIn("Movers Pullback · SL hits", labels)
        self.assertEqual(labels[-1], "Paper trade sizing update")           # marker row stays last


class UniverseTests(unittest.TestCase):
    def test_stablecoins_are_excluded(self):
        for base in ("FDUSD", "USDC", "TUSD", "USD1"):
            self.assertIn(base, screener.EXCLUDED_BASE_ASSETS)
        self.assertNotIn("BTC", screener.EXCLUDED_BASE_ASSETS)


class FreshStartSafetyTests(unittest.TestCase):
    def test_reset_version_is_set_and_marker_logic_exists(self):
        self.assertTrue(gs.FRESH_START_VERSION)
        self.assertTrue(callable(gs._fresh_marker_current))


if __name__ == "__main__":
    unittest.main()
