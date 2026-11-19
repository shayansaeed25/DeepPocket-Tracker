import csv, os, sys, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import tiering as t
import quality_gate as qg


def utc(h, m=0):
    return datetime(2026, 10, 10, h, m, tzinfo=timezone.utc)


class AssignTierTests(unittest.TestCase):
    def test_score_bands(self):
        self.assertEqual(t.assign_tier(72, 2, "ALIGNED")[0], "A")
        self.assertEqual(t.assign_tier(56, 2, "ALIGNED")[0], "B")
        self.assertEqual(t.assign_tier(40, 2, "ALIGNED")[0], "C")

    def test_counter_trend_and_single_family_each_drop_one_tier(self):
        self.assertEqual(t.assign_tier(72, 2, "COUNTER-TREND")[0], "B")
        self.assertEqual(t.assign_tier(72, 1, "ALIGNED")[0], "B")
        self.assertEqual(t.assign_tier(72, 1, "COUNTER-TREND")[0], "C")
        self.assertEqual(t.assign_tier(56, 1, "ALIGNED")[0], "C")      # the LINK example from the tier card

    def test_loss_limit_forces_watch_only(self):
        tier, reasons = t.assign_tier(90, 3, "ALIGNED", loss_limit_hit=True)
        self.assertEqual(tier, "C")
        self.assertTrue(any("loss limit" in r for r in reasons))

    def test_floor_changes_tier_b_boundary(self):
        self.assertEqual(t.assign_tier(46, 2, "ALIGNED", floor=50)[0], "C")
        self.assertEqual(t.assign_tier(46, 2, "ALIGNED", floor=45)[0], "B")


class AdaptiveFloorTests(unittest.TestCase):
    def test_floor_relaxes_late_in_a_quiet_day_but_is_bounded(self):
        self.assertEqual(t.tier_b_floor({"tradable": 0}, utc(3)), 50.0)
        self.assertEqual(t.tier_b_floor({"tradable": 1}, utc(9)), 45.0)
        self.assertEqual(t.tier_b_floor({"tradable": 3}, utc(17)), 40.0)
        self.assertEqual(t.tier_b_floor({"tradable": 5}, utc(20)), 50.0)       # target met -> normal floor
        self.assertGreaterEqual(t.tier_b_floor({"tradable": 0}, utc(23)), t.TIER_B_HARD_FLOOR)


class StateAndBoardTests(unittest.TestCase):
    def test_state_resets_on_a_new_day_and_board_keeps_best_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            state = t.load_state(path, utc(1))
            for i, (tier, score) in enumerate([("B", 55), ("A", 80), ("C", 30)]):
                t.record_signal(state, tier, {"symbol": f"C{i}USDT", "direction": "LONG", "side": "BUY",
                                              "tier": tier, "score": score, "price": 1, "stop": 0.9})
            t.save_state(state, path)
            again = t.load_state(path, utc(2))
            self.assertEqual((again["tradable"], again["watch"]), (2, 1))
            self.assertEqual(again["board"][0]["symbol"], "C1USDT")
            self.assertEqual(t.load_state(path, datetime(2026, 10, 11, 1, tzinfo=timezone.utc))["tradable"], 0)

    def test_same_symbol_direction_is_not_double_listed(self):
        state = t.load_state("/nonexistent/x.json", utc(1))
        for score in (50, 70):
            t.record_signal(state, "B", {"symbol": "AUSDT", "direction": "LONG", "side": "BUY", "tier": "B",
                                         "score": score})
        self.assertEqual(len(state["board"]), 1)
        self.assertEqual(state["board"][0]["score"], 70)

    def test_digest_due_once_after_the_hour(self):
        st = {"digest_sent": False}
        self.assertFalse(t.digest_due(st, utc(t.DIGEST_HOUR_UTC - 1)))
        self.assertTrue(t.digest_due(st, utc(t.DIGEST_HOUR_UTC)))
        st["digest_sent"] = True
        self.assertFalse(t.digest_due(st, utc(t.DIGEST_HOUR_UTC + 1)))


def write_journal(path, rows):
    cols = ["event_id", "event_time_utc", "symbol", "scanner", "outcome", "realized_pnl_usd"]
    with open(path, "w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


class JournalTests(unittest.TestCase):
    def test_daily_loss_limit_and_scorecard(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.csv")
            rows = [{"event_id": f"e{i}", "event_time_utc": "2026-10-10T05:00:00+00:00", "symbol": "XUSDT",
                     "scanner": "STRATEGY · WATCH ONLY · Smart Vector", "outcome": "SL_HIT",
                     "realized_pnl_usd": "-3"} for i in range(4)]
            rows.append({"event_id": "old", "event_time_utc": "2026-10-09T05:00:00+00:00", "symbol": "YUSDT",
                         "scanner": "WHALE FLOW · ON-CHAIN DEX", "outcome": "TP2_HIT", "realized_pnl_usd": "9"})
            rows.append(dict(rows[0]))                                  # duplicate row must not double count
            write_journal(path, rows)
            self.assertEqual(t.daily_realized_pnl(path, utc(12)), -12.0)
            self.assertIn("watch-only", t.loss_limit_reason(path, utc(12)))
            cards = {c["name"]: c for c in t.scorecard(path)}
            self.assertEqual(cards["FT · Smart Vector"]["trades"], 4)
            self.assertTrue(cards["FT · Smart Vector"]["verdict"].startswith("TESTING"))
            self.assertIsNone(t.loss_limit_reason(path, utc(12), limit=50))

    def test_promotion_needs_30_trades_and_profit_factor(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.csv")
            rows = [{"event_id": f"w{i}", "event_time_utc": "2026-10-10T05:00:00+00:00", "symbol": "A",
                     "scanner": "MOVERS PULLBACK · WATCH ONLY", "outcome": "TP2_HIT", "realized_pnl_usd": "4"}
                    for i in range(20)]
            rows += [{"event_id": f"l{i}", "event_time_utc": "2026-10-10T06:00:00+00:00", "symbol": "A",
                      "scanner": "MOVERS PULLBACK · WATCH ONLY", "outcome": "SL_HIT", "realized_pnl_usd": "-2"}
                     for i in range(10)]
            write_journal(path, rows)
            self.assertEqual(t.scorecard(path)[0]["verdict"], "PROMOTE-ELIGIBLE")

    def test_digest_payload_is_well_formed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "j.csv")
            write_journal(path, [])
            state = t.load_state("/nonexistent/x.json", utc(1))
            payload = t.build_digest(state, path, "NEUTRAL (-9)")
            self.assertEqual(len(payload["embeds"]), 1)
            self.assertIn("No candidates", payload["embeds"][0]["fields"][0]["value"])


class WatchFamilyTests(unittest.TestCase):
    def test_tier_c_trades_do_not_consume_ab_slots(self):
        trade = {"scanner": "STRATEGY", "strategy_name": "Smart Vector", "details": {"tier": "C"}}
        self.assertEqual(qg.family_of_trade(trade), "WATCH")
        trade["details"] = {"tier": "B"}
        self.assertEqual(qg.family_of_trade(trade), "SMART_VECTOR")
        self.assertIn("WATCH", qg.MAX_OPEN)
        self.assertIn("WATCH", qg.MAX_NEW_PER_SCAN)


if __name__ == "__main__":
    unittest.main()


class UniverseTopUpTests(unittest.TestCase):
    def test_universe_is_topped_up_to_about_200_coins(self):
        from unittest import mock
        import screener
        bases = [f"C{i}" for i in range(260)]
        tickers = [{"symbol": f"{b}USDT", "quoteVolume": str(1e9 - i * 1e6)} for i, b in enumerate(bases)]
        tickers.append({"symbol": "USDCUSDT", "quoteVolume": "9e12"})
        tickers.append({"symbol": "PEPEUSDT", "quoteVolume": "9e12"})
        info = {"symbols": [{"symbol": r["symbol"], "status": "TRADING", "quoteAsset": "USDT",
                             "isSpotTradingAllowed": True} for r in tickers]}

        class Resp:
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): pass
            def json(self): return self.payload

        def fake_get(url, **kw):
            return Resp(tickers if "ticker/24hr" in url else info)

        allowed = set(bases[:120])                       # CoinGecko list only matches 120 of them
        with mock.patch.object(screener.requests, "get", fake_get), \
             mock.patch.object(screener, "get_quality_symbols", return_value=allowed):
            uni = screener.get_usdt_universe()
        self.assertEqual(len(uni), 200)
        self.assertNotIn("USDCUSDT", uni)
        self.assertNotIn("PEPEUSDT", uni)
        self.assertEqual(len(set(uni)), 200)
