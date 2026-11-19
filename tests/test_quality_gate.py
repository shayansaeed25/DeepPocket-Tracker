import os, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import quality_gate as qg
import screener
import movers_strategy


def sig(price=100.0, stop=98.0, action="BUY", agree=1, **extra):
    base = {"symbol": "TESTUSDT", "price": price, "stop_loss": stop, "action": action,
            "direction": "LONG" if action == "BUY" else "SHORT", "strategy_agreement_count": agree}
    base.update(extra)
    return base


class StopCapTests(unittest.TestCase):
    def test_smart_vector_cap(self):
        self.assertIsNone(qg.check(sig(stop=98.0), "SMART_VECTOR"))                  # 2.0% ok
        self.assertIsNone(qg.check(sig(stop=97.5), "SMART_VECTOR"))                  # 2.5% ok (cap)
        self.assertIn("wider", qg.check(sig(stop=96.0), "SMART_VECTOR"))             # 4.0% too wide
        self.assertIn("wider", qg.check(sig(stop=91.6), "SMART_VECTOR"))             # 8.4% (the WLD 4h case)

    def test_consensus_gets_a_looser_cap(self):
        self.assertIsNone(qg.check(sig(stop=96.0), "CONSENSUS"))                     # 4.0% ok
        self.assertIn("wider", qg.check(sig(stop=95.0), "CONSENSUS"))                # 5.0% too wide

    def test_micro_stops_are_noise(self):
        self.assertIn("tighter", qg.check(sig(price=0.999, stop=0.99859), "CONSENSUS"))   # 0.04%, the FDUSD case

    def test_bad_input(self):
        self.assertEqual(qg.check({"price": 0, "stop_loss": 1}, "CONSENSUS"), "no valid stop")

    def test_short_side_uses_absolute_distance(self):
        self.assertIsNone(qg.check(sig(stop=102.0, action="SELL"), "SMART_VECTOR"))
        self.assertIn("wider", qg.check(sig(stop=106.0, action="SELL"), "SMART_VECTOR"))


class BtcGuardTests(unittest.TestCase):
    BEAR = {"components": {"btc_momentum": {"score": -40}}}
    BULL = {"components": {"btc_momentum": {"score": 60}}}

    def test_buy_blocked_only_when_btc_momentum_is_bearish(self):
        self.assertIn("bearish", qg.check(sig(), "SMART_VECTOR", self.BEAR))
        self.assertIsNone(qg.check(sig(), "SMART_VECTOR", self.BULL))
        self.assertIsNone(qg.check(sig(), "SMART_VECTOR", None))                      # no regime data -> never blocks
        self.assertIsNone(qg.check(sig(), "SMART_VECTOR", {"components": {}}))

    def test_sells_are_guarded_only_when_btc_is_bullish(self):
        # v2: symmetric regime filter - no shorts into a clearly bullish BTC, shorts are fine in a bearish one.
        self.assertIsNone(qg.check(sig(stop=102.0, action="SELL"), "SMART_VECTOR", self.BEAR))
        self.assertIn("bullish", qg.check(sig(stop=102.0, action="SELL"), "SMART_VECTOR", self.BULL))
        self.assertIsNone(qg.check(sig(stop=102.0, action="SELL"), "SMART_VECTOR", None))

    def test_sell_guard_can_be_switched_off(self):
        with mock.patch.object(qg, "BTC_GUARD_SELL_ENABLED", False):
            self.assertIsNone(qg.check(sig(stop=102.0, action="SELL"), "SMART_VECTOR", self.BULL))

    def test_switch_and_threshold(self):
        with mock.patch.object(qg, "BTC_GUARD_ENABLED", False):
            self.assertIsNone(qg.check(sig(), "SMART_VECTOR", self.BEAR))
        mild = {"components": {"btc_momentum": {"score": -10}}}
        self.assertIsNone(qg.check(sig(), "SMART_VECTOR", mild))
        self.assertIn("bearish", qg.btc_guard({"action": "BUY"}, self.BEAR))
        self.assertIn("bearish", qg.btc_guard({"direction": "LONG"}, self.BEAR))


class FamilyAndRankTests(unittest.TestCase):
    def test_family(self):
        self.assertEqual(qg.family_of(["Smart Vector"]), "SMART_VECTOR")
        self.assertEqual(qg.family_of(["Elite Trend", "Smart Vector"]), "CONSENSUS")
        self.assertIsNone(qg.family_of([]))
        self.assertEqual(qg.family_of_trade({"scanner": "STRATEGY", "strategy_name": "Smart Vector"}), "SMART_VECTOR")
        self.assertEqual(qg.family_of_trade({"scanner": "STRATEGY", "strategy_name": "Consensus: Prime Edge, Quant Prime"}), "CONSENSUS")
        self.assertIsNone(qg.family_of_trade({"scanner": "WHALE_FLOW", "strategy_name": ""}))

    def test_rank_prefers_more_agreement_then_tighter_stop(self):
        items = [sig(stop=97.0, agree=1), sig(stop=98.0, agree=1), sig(stop=96.0, agree=2)]
        ranked = sorted(items, key=qg.rank_key)
        self.assertEqual([i["stop_loss"] for i in ranked], [96.0, 98.0, 97.0])

    def test_caps_are_sane(self):
        self.assertLessEqual(qg.MAX_NEW_PER_SCAN["SMART_VECTOR"], 3)
        self.assertLessEqual(qg.MAX_OPEN["SMART_VECTOR"], qg.MAX_OPEN["CONSENSUS"])


class ScreenerFlagsTests(unittest.TestCase):
    def test_whale_volume_is_off_and_movers_cap_tightened(self):
        self.assertFalse(screener.WHALE_VOLUME_ENABLED)
        self.assertEqual(movers_strategy.MAX_RISK_PCT, 4.0)


if __name__ == "__main__":
    unittest.main()
