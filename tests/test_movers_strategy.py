import os, random, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import movers_strategy as m
import trade_tracker

H, Q = 3_600_000, 900_000
NOW = 1_790_000_000_000 // H * H
TICKER = {"symbol": "TESTUSDT", "pct": 8.0, "quote_volume": 2e8, "last": 1.0}


def series(n, start, step, p0, drift, seed, vol=1000.0):
    rnd = random.Random(seed)
    o, h, l, c, v, t, p = [], [], [], [], [], [], p0
    for i in range(n):
        q = p * (1 + drift + rnd.uniform(-0.0015, 0.0015))
        o.append(p); c.append(q); h.append(max(p, q) * 1.001); l.append(min(p, q) * 0.999)
        v.append(vol * rnd.uniform(.8, 1.2)); t.append(start + i * step); p = q
    return dict(open_time=t, open=o, high=h, low=l, close=c, volume=v)


def pullback_15m(is_long=True, bounce_vol=5000.0):
    n = 300
    k = series(n, NOW - n * Q, Q, 1.0, 0.0006 if is_long else -0.0006, 1)
    leg = 0.9965 if is_long else 1.0035
    for i in range(n - 7, n - 3):
        p = k["close"][i - 1]; q = p * leg
        k["open"][i], k["close"][i] = p, q
        k["high"][i] = max(p, q) * 1.0005; k["low"][i] = min(p, q) * 0.9995
    t = n - 3
    p = k["close"][t - 1]; q = p * (1.012 if is_long else 0.988)
    k["open"][t], k["close"][t] = p, q
    k["high"][t] = max(p, q) * (1.001 if is_long else 1.002)
    k["low"][t] = min(p, q) * (0.998 if is_long else 0.999)
    k["volume"][t] = bounce_vol
    for i in (n - 2, n - 1):
        p = k["close"][i - 1]
        k["open"][i] = p; k["close"][i] = p * (1.0005 if is_long else 0.9995)
        k["high"][i] = p * 1.001; k["low"][i] = p * 0.9995
    return k


def trend_1h(is_long=True):
    return series(300, NOW - 300 * H, H, 1.0, 0.002 if is_long else -0.002, 2)


class PullbackTests(unittest.TestCase):
    def test_long_signal_levels(self):
        sig = m.evaluate_pullback("TESTUSDT", True, TICKER, pullback_15m(True), trend_1h(True))
        self.assertIsNotNone(sig)
        self.assertEqual((sig["action"], sig["scanner"]), ("BUY", "MOVERS_PULLBACK"))
        risk = sig["price"] - sig["stop_loss"]
        self.assertGreater(risk, 0)
        self.assertAlmostEqual(sig["take_profit_1"], sig["price"] + 2.0 * risk, places=8)
        self.assertAlmostEqual(sig["take_profit_2"], sig["price"] + 3.5 * risk, places=8)
        self.assertLessEqual(sig["risk_pct"], m.MAX_RISK_PCT)
        self.assertTrue(sig["watch_only"])
        self.assertEqual(sig["partial_tp1_pct"], 0.5)

    def test_short_signal_levels(self):
        sig = m.evaluate_pullback("TESTUSDT", False, dict(TICKER, pct=-6.0),
                                  pullback_15m(False), trend_1h(False))
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "SELL")
        risk = sig["stop_loss"] - sig["price"]
        self.assertAlmostEqual(sig["take_profit_1"], sig["price"] - 2.0 * risk, places=8)
        self.assertAlmostEqual(sig["take_profit_2"], sig["price"] - 3.5 * risk, places=8)

    def test_requires_1h_trend_alignment(self):
        self.assertIsNone(m.evaluate_pullback("TESTUSDT", True, TICKER, pullback_15m(True), trend_1h(False)))
        self.assertIsNone(m.evaluate_pullback("TESTUSDT", False, TICKER, pullback_15m(False), trend_1h(True)))

    def test_requires_volume_confirmation(self):
        self.assertIsNone(m.evaluate_pullback("TESTUSDT", True, TICKER,
                                              pullback_15m(True, bounce_vol=1000.0), trend_1h(True)))

    def test_stale_signal_rejected(self):
        k = pullback_15m(True)
        i = len(k["close"]) - 2
        p = k["close"][i - 1]
        k["open"][i], k["close"][i], k["high"][i], k["low"][i] = p, p * 1.02, p * 1.021, p
        self.assertIsNone(m.evaluate_pullback("TESTUSDT", True, TICKER, k, trend_1h(True)))

    def test_wide_stop_rejected(self):
        with mock.patch.object(m, "MAX_RISK_PCT", 0.5):
            self.assertIsNone(m.evaluate_pullback("TESTUSDT", True, TICKER, pullback_15m(True), trend_1h(True)))


def order_block_data(is_long=True):
    sign = 1 if is_long else -1
    n1, rnd = 120, random.Random(3)

    def flat(n, start, step):
        o, h, l, c, v, t, p = [], [], [], [], [], [], 1.0
        for i in range(n):
            q = p * (1 + rnd.uniform(-0.002, 0.002))
            o.append(p); c.append(q); h.append(max(p, q) * 1.0015); l.append(min(p, q) * 0.9985)
            v.append(1000.0); t.append(start + i * step); p = q
        return dict(open_time=t, open=o, high=h, low=l, close=c, volume=v)

    k1 = flat(n1, NOW - n1 * H, H)
    d = n1 - 10
    p = k1["close"][d - 2]
    # order block: last opposite candle before the displacement
    k1["open"][d - 1] = p
    k1["close"][d - 1] = p * (1 - 0.004 * sign)
    k1["high"][d - 1] = p * (1.0005 if is_long else 1.0055)
    k1["low"][d - 1] = p * (0.9945 if is_long else 0.9995)
    zone_lo, zone_hi = k1["low"][d - 1], k1["high"][d - 1]
    p = k1["close"][d - 1]
    k1["open"][d] = p; k1["close"][d] = p * (1 + 0.03 * sign)
    k1["high"][d] = max(p, k1["close"][d]) * 1.001; k1["low"][d] = min(p, k1["close"][d]) * 0.999
    k1["volume"][d] = 4000
    for i in range(d + 1, n1):
        pp = k1["close"][i - 1]
        k1["open"][i] = pp; k1["close"][i] = pp * (1 + 0.001 * sign)
        k1["high"][i] = pp * (1.003 if is_long else 1.0005); k1["low"][i] = pp * (0.9995 if is_long else 0.997)
    away = k1["close"][-2]
    n = 480
    k15 = flat(n, NOW + n1 * H - n * Q, Q)
    for i in range(n):
        k15["open"][i] = away; k15["close"][i] = away * 1.0002
        k15["high"][i] = away * 1.001; k15["low"][i] = away * 0.999; k15["volume"][i] = 500
    t, mid = n - 4, (zone_lo + zone_hi) / 2
    if is_long:
        k15["open"][t], k15["low"][t] = mid * 1.004, zone_hi * 0.999
        k15["close"][t], k15["high"][t] = mid * 1.006, mid * 1.0065
    else:
        k15["open"][t], k15["high"][t] = mid * 0.996, zone_lo * 1.001
        k15["close"][t], k15["low"][t] = mid * 0.994, mid * 0.9935
    for i in (n - 3, n - 2, n - 1):
        pp = k15["close"][i - 1]
        k15["open"][i] = pp; k15["close"][i] = pp * (1.0003 if is_long else 0.9997)
        k15["high"][i] = pp * 1.001; k15["low"][i] = pp * 0.9995
    return k15, k1


class OrderBlockTests(unittest.TestCase):
    def test_bullish_order_block(self):
        k15, k1 = order_block_data(True)
        sig = m.evaluate_order_block("TESTUSDT", True, TICKER, k15, k1)
        self.assertIsNotNone(sig)
        self.assertEqual((sig["action"], sig["scanner"]), ("BUY", "MOVERS_ORDERBLOCK"))
        risk = sig["price"] - sig["stop_loss"]
        self.assertAlmostEqual(sig["take_profit_1"], sig["price"] + 2 * risk, places=8)
        self.assertLess(sig["stop_loss"], sig["details"]["ob_zone_low"])
        self.assertIn(sig["details"]["quality"], {"High", "Medium"})

    def test_bearish_order_block(self):
        k15, k1 = order_block_data(False)
        sig = m.evaluate_order_block("TESTUSDT", False, dict(TICKER, pct=-7.0), k15, k1)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["action"], "SELL")
        self.assertGreater(sig["stop_loss"], sig["details"]["ob_zone_high"])

    def test_wrong_side_gives_nothing(self):
        k15, k1 = order_block_data(True)
        self.assertIsNone(m.evaluate_order_block("TESTUSDT", False, TICKER, k15, k1))

    def test_invalidated_block_rejected(self):
        k15, k1 = order_block_data(True)
        i = len(k1["close"]) - 3        # a later 1h candle closes below the block
        k1["close"][i] = k1["low"][i] = 0.5
        self.assertIsNone(m.evaluate_order_block("TESTUSDT", True, TICKER, k15, k1))


class CandidateTests(unittest.TestCase):
    def test_filters(self):
        tickers = [
            {"symbol": "A", "pct": 12.2, "quote_volume": 144e6, "last": 1},
            {"symbol": "B", "pct": 2.0, "quote_volume": 500e6, "last": 1},    # move too small
            {"symbol": "C", "pct": 9.0, "quote_volume": 10e6, "last": 1},     # volume too small
            {"symbol": "D", "pct": 45.0, "quote_volume": 500e6, "last": 1},   # late-stage
            {"symbol": "E", "pct": -9.1, "quote_volume": 92e6, "last": 1},
            {"symbol": "F", "pct": -3.5, "quote_volume": 40e6, "last": 1},    # volume too small
            {"symbol": "G", "pct": -5.0, "quote_volume": 300e6, "last": 1},
        ]
        gainers, losers = m.pick_candidates(tickers)
        self.assertEqual([t["symbol"] for t in gainers], ["A"])
        self.assertEqual([t["symbol"] for t in losers], ["E", "G"])


class AccountingTests(unittest.TestCase):
    def _signal(self, **extra):
        base = {"symbol": "TESTUSDT", "scanner": "MOVERS_PULLBACK", "direction": "LONG", "action": "BUY",
                "price": 100.0, "stop_loss": 98.0, "take_profit_1": 104.0, "take_profit_2": 107.0,
                "watch_only": True, "partial_tp1_pct": 0.5, "tp1_hit": True, "last_price": 102.0}
        base.update(extra)
        return base

    def test_source_labels(self):
        self.assertEqual(trade_tracker.journal_row(self._signal())["scanner"], "MOVERS PULLBACK · WATCH ONLY")
        self.assertEqual(trade_tracker.journal_row(self._signal(scanner="MOVERS_ORDERBLOCK", watch_only=False))["scanner"],
                         "MOVERS ORDER BLOCK")

    def test_break_even_after_tp1_banks_half(self):
        row = trade_tracker.journal_row(self._signal(), status="CLOSED", outcome="BE_HIT", exit_price=100.0)
        # $100 notional, 1 unit; half closed at +4 -> +2.0, rest at entry -> 0
        self.assertAlmostEqual(row["realized_pnl_usd"], 2.0, places=6)

    def test_tp2_after_tp1(self):
        row = trade_tracker.journal_row(self._signal(), status="CLOSED", outcome="TP2_HIT", exit_price=107.0)
        self.assertAlmostEqual(row["realized_pnl_usd"], 0.5 * 4 + 0.5 * 7, places=6)

    def test_stop_before_tp1_is_full_loss(self):
        row = trade_tracker.journal_row(self._signal(tp1_hit=False), status="CLOSED", outcome="SL_HIT", exit_price=98.0)
        self.assertAlmostEqual(row["realized_pnl_usd"], -2.0, places=6)

    def test_other_strategies_unchanged(self):
        row = trade_tracker.journal_row(self._signal(partial_tp1_pct=0, scanner="WHALE_FLOW"),
                                        status="CLOSED", outcome="BE_HIT", exit_price=100.0)
        self.assertAlmostEqual(row["realized_pnl_usd"], 0.0, places=6)


class ScanTests(unittest.TestCase):
    def test_scan_movers_end_to_end_with_mocked_data(self):
        tickers = [dict(TICKER, symbol="UPUSDT", pct=8.0), dict(TICKER, symbol="DNUSDT", pct=-6.0),
                   dict(TICKER, symbol="FLATUSDT", pct=0.5)]
        data = {"UPUSDT": (pullback_15m(True), trend_1h(True)),
                "DNUSDT": (pullback_15m(False), trend_1h(False))}

        def fake_klines(symbol, interval, limit=300):
            k15, k1h = data[symbol]
            return k15 if interval == "15m" else k1h

        logs = []
        with mock.patch.object(m, "fetch_movers", return_value=tickers):
            sigs = m.scan_movers(["UPUSDT", "DNUSDT", "FLATUSDT"], "http://x", fake_klines,
                                 sleep_seconds=0, log=logs.append)
        got = {(x["symbol"], x["scanner"], x["action"]) for x in sigs}
        self.assertIn(("UPUSDT", "MOVERS_PULLBACK", "BUY"), got)
        self.assertIn(("DNUSDT", "MOVERS_PULLBACK", "SELL"), got)
        self.assertFalse(any(x["symbol"] == "FLATUSDT" for x in sigs))
        self.assertTrue(logs)


if __name__ == "__main__":
    unittest.main()
