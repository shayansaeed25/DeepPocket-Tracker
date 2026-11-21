"""Unit tests for DeepPocket Tracker v6.0:
  - WZ Mathematical Decision System (VPF, DRZ, IBL, LML) as Strategy #14 (ID 9004)
  - Counter-regime Oversold V-Bottom LONG bypass (catches $55k -> $60.3k bounces in bearish macro)
  - Free Institutional Liquidation Magnet Estimator (Swing pivots + OI/Funding skew + Extreme Trap guard)
  - OKX-first Futures OI & Funding Rate fallback chain (fixes US runner HTTP 451/403 SPOT_CVD_ONLY bug)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import backtester  # noqa: E402
import quality_gate  # noqa: E402
import screener  # noqa: E402
import smart_money  # noqa: E402
import strategy_policy  # noqa: E402


def _make_capitulation_bounce_klines(n: int = 80) -> dict[str, list[float]]:
    """Construct realistic OHLCV candles simulating a sharp dump into an oversold
    liquidation sweep (DRZ <= -2.0) followed by a high-volume bullish rejection bounce."""
    opens, highs, lows, closes, volumes, qv, tb = [], [], [], [], [], [], []
    price = 63000.0
    for i in range(n):
        if i < 65:
            o = price
            c = price - 30.0
            h = o + 120.0
            l = c - 120.0
            v = 1000.0
            buy_share = 0.48
        elif i < 77:
            # Steep liquidation flush (63k -> 55.5k)
            o = price
            c = price - 520.0
            h = o + 100.0
            l = c - 180.0
            v = 1800.0
            buy_share = 0.38
        elif i == 77:
            # Capitulation bottom wick at 55,100
            o = price
            c = price - 300.0
            h = o + 80.0
            l = c - 450.0
            v = 2600.0
            buy_share = 0.45
        elif i == 78:
            # Latest closed candle (idx = len - 2): Bullish reversal bounce with strong buyer CVD
            o = price
            c = price + 680.0
            h = c + 90.0
            l = o - 320.0
            v = 3400.0
            buy_share = 0.64
        else:
            # Forming candle (idx = len - 1)
            o = price
            c = price + 150.0
            h = c + 50.0
            l = o - 50.0
            v = 900.0
            buy_share = 0.55
        opens.append(round(o, 2))
        highs.append(round(h, 2))
        lows.append(round(l, 2))
        closes.append(round(c, 2))
        volumes.append(v)
        q = v * c
        qv.append(q)
        tb.append(q * buy_share)
        price = c
    return {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "quote_volume": qv,
        "taker_buy_quote_volume": tb,
    }


class V60WZAndLiquidationTests(unittest.TestCase):
    def test_wz_registered_in_policy_and_backtester(self):
        self.assertIn("WZ Reversal & Liquidity Magnet", strategy_policy.SMART_MONEY_STRATEGIES)
        self.assertIn("WZ Reversal & Liquidity Magnet", backtester.ALL_14_STRATEGY_NAMES)
        self.assertEqual(
            strategy_policy.classify_strategy_group(["WZ Reversal & Liquidity Magnet"], "LONG"),
            "ACTIVE",
        )

    def test_wz_metrics_and_oversold_v_bottom_long_signal(self):
        klines = _make_capitulation_bounce_klines()
        idx = len(klines["close"]) - 2
        wz = smart_money.compute_wz_metrics(klines, idx)
        self.assertIsNotNone(wz)
        self.assertLessEqual(wz["min_drz_3b"], -2.0)
        self.assertLess(wz["vpf"], smart_money.WZ_VPF_LONG_MAX)
        self.assertEqual(wz["vpf_zone"], "LONG_PRESSURE")
        self.assertGreater(wz["lml"], klines["close"][idx])

        signals = smart_money.evaluate_latest(
            "BTCUSDT", klines, "1h", btc_bearish_bias=True, funding_rate_pct=-0.02, oi_change_pct=3.5
        )
        wz_longs = [
            s for s in signals
            if s.get("strategy_name") == "WZ Reversal & Liquidity Magnet" and s.get("direction") == "LONG"
        ]
        self.assertEqual(len(wz_longs), 1)
        sig = wz_longs[0]
        self.assertTrue(sig["details"]["wz_reversal_override"])
        self.assertIsNotNone(sig["details"]["liq_short_above"])
        self.assertGreater(sig["details"]["liq_short_above"], sig["price"])

    def test_wz_oversold_long_bypasses_bearish_regime_and_htf_locks(self):
        klines = _make_capitulation_bounce_klines()
        signals = smart_money.evaluate_latest(
            "BTCUSDT", klines, "1h", btc_bearish_bias=True, funding_rate_pct=-0.025, oi_change_pct=4.0
        )
        sig = next(s for s in signals if s.get("strategy_name") == "WZ Reversal & Liquidity Magnet")
        sig["details"]["session_name"] = "NY_OVERLAP"
        sig["details"]["trend_alignment"] = "COUNTER-TREND"
        sig["details"]["adx"] = 34.0
        sig["details"]["plus_di"] = 14.0
        sig["details"]["minus_di"] = 38.0

        bearish_regime = {
            "label": "BEARISH",
            "score": -28.0,
            "components": {"btc_momentum": {"score": -35.0}},
        }
        # 1. Passes quality_gate.check even in BEARISH regime
        self.assertIsNone(quality_gate.check(sig, "CONSENSUS", bearish_regime))

        # 2. Passes screener.passes_institutional_v3_gates even in BEARISH regime
        old_regime = screener.CURRENT_REGIME
        try:
            screener.CURRENT_REGIME = bearish_regime
            ok, reason = screener.passes_institutional_v3_gates(
                sig,
                backtest_rankings=None,
                btc_klines=klines,
                coin_klines=klines,
            )
            self.assertTrue(ok, f"Expected WZ Oversold LONG to pass, got rejected: {reason}")
        finally:
            screener.CURRENT_REGIME = old_regime

    def test_liquidation_magnet_estimator_and_extreme_bottom_trap(self):
        klines = _make_capitulation_bounce_klines()
        idx = len(klines["close"]) - 3  # exact capitulation bottom bar
        mag = smart_money.estimate_liquidation_magnets(
            klines, idx=idx, oi_change_pct=2.5, funding_rate_pct=-0.035
        )
        self.assertGreater(mag["short_liq_above"], klines["close"][idx])
        self.assertLess(mag["long_liq_below"], klines["close"][idx])
        self.assertEqual(mag["magnet_bias"], "UP_SHORT_SQUEEZE")
        self.assertGreater(mag["short_liq_share_pct"], 60.0)
        self.assertTrue(mag["extreme_trap_short"])

        # Normal neutral funding does NOT cause analysis paralysis
        mag_normal = smart_money.estimate_liquidation_magnets(
            klines, idx=60, oi_change_pct=1.0, funding_rate_pct=0.005
        )
        self.assertFalse(mag_normal["extreme_trap_short"])
        self.assertFalse(mag_normal["extreme_trap_long"])

    def test_okx_first_oi_and_funding_fallback_succeeds_without_binance(self):
        def fake_get(url, params=None, timeout=15):
            resp = mock.MagicMock()
            if "okx.com/api/v5/rubik/stat/contracts/open-interest-volume" in url:
                resp.status_code = 200
                resp.json.return_value = {
                    "data": [
                        ["1700003600000", "1050000000", "500000000"],
                        ["1700000000000", "1000000000", "450000000"],
                    ]
                }
                return resp
            if "okx.com/api/v5/public/funding-rate" in url:
                resp.status_code = 200
                resp.json.return_value = {"data": [{"fundingRate": "-0.00018"}]}
                return resp
            resp.status_code = 451
            return resp

        with mock.patch("screener.requests.get", side_effect=fake_get):
            oi = screener.get_open_interest_change("BTCUSDT", "1h", limit=2)
            fr = screener.get_funding_rate("BTCUSDT")
        self.assertEqual(oi, 5.0)
        self.assertEqual(fr, -0.018)


if __name__ == "__main__":
    unittest.main()
