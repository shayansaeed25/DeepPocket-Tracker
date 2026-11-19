import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import strategy_policy
from strategy_policy import classify_strategy_group as c


class StrategyPolicyTests(unittest.TestCase):
    def test_smart_vector_solo_both_sides_and_mode_switch(self):
        self.assertEqual(c(["Smart Vector"], "LONG"), "WATCH_ONLY")          # forward test until it re-earns LIVE
        self.assertEqual(c(["Smart Vector"], "SHORT"), "WATCH_ONLY")   # v2.2: regime guard decides direction
        from unittest import mock
        with mock.patch.object(strategy_policy, "SMART_VECTOR_MODE", "ACTIVE"):
            self.assertEqual(c(["Smart Vector"], "LONG"), "ACTIVE")

    def test_only_the_two_profitable_pairs_survive(self):
        for side in ("LONG", "SHORT"):
            self.assertEqual(c(["Prime Edge", "Quant Prime"], side), "WATCH_ONLY")
            self.assertEqual(c(["Elite Trend", "Smart Vector"], side), "WATCH_ONLY")

    def test_signal_edge_is_gone_everywhere(self):
        for combo in (["Signal Edge"], ["Signal Edge", "Prime Edge"], ["Smart Vector", "Signal Edge"],
                      ["Quant Prime", "Signal Edge"], ["Prime Edge", "Quant Prime", "Signal Edge"]):
            self.assertIsNone(c(combo, "LONG"), combo)
            self.assertIsNone(c(combo, "SHORT"), combo)

    def test_removed_standalones_and_apex_nova_combos(self):
        for name in ["Apex Trend", "Nova Alpha", "Elite Trend", "Global Core", "Alpha Prime",
                     "Trend Momentum", "Core Momentum", "Quant Prime", "Prime Edge"]:
            for side in ("LONG", "SHORT"):
                self.assertIsNone(c([name], side), name)
        for extra in ("Apex Trend", "Nova Alpha", "Global Core", "Alpha Prime"):
            self.assertIsNone(c(["Prime Edge", "Quant Prime", extra], "LONG"))
            self.assertIsNone(c(["Elite Trend", "Smart Vector", extra], "LONG"))

    def test_unselected_combos_and_empty(self):
        self.assertIsNone(c(["Prime Edge", "Smart Vector"], "LONG"))
        self.assertIsNone(c(["Elite Trend", "Quant Prime"], "LONG"))
        self.assertIsNone(c([], "LONG"))


if __name__ == "__main__":
    unittest.main()
