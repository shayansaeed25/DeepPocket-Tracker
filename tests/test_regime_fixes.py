import os, sys, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import market_regime as mr


def stories(bull, bear, neutral):
    return ([{"title": f"Bitcoin ETF inflows rally story {i}", "age_hours": 3} for i in range(bull)]
            + [{"title": f"Exchange hacked crash fears story {i}", "age_hours": 3} for i in range(bear)]
            + [{"title": f"Crypto market quiet day number {i}", "age_hours": 3} for i in range(neutral)])


class NewsScoreFixTests(unittest.TestCase):
    def test_score_does_not_depend_on_headline_volume(self):
        scores = {round(mr.score_headlines(stories(6 * k, 3 * k, 30 * k))[0]) for k in (1, 2, 4, 8)}
        self.assertEqual(len(scores), 1)                       # same mix, same score, any number of feeds
        self.assertLess(list(scores)[0], 60)                   # and not pinned near +100

    def test_a_clearly_one_sided_day_still_scores_high(self):
        self.assertGreater(mr.score_headlines(stories(30, 0, 5))[0], 85)
        self.assertLess(mr.score_headlines(stories(0, 30, 5))[0], -85)

    def test_whole_word_matching(self):
        for text in ("Bank of Japan warns on yields", "Bitcoin miners hackathon in Warsaw",
                     "Banner ad for ETF", "Banking sector steady"):
            self.assertEqual(mr._term_score(text), 0.0, text)
        self.assertLess(mr._term_score("Tokyo bans crypto ads"), 0)
        self.assertLess(mr._term_score("Exchange hacked after exploit"), 0)
        self.assertLess(mr._term_score("Liquidations hit $1B as bitcoin crashes"), 0)
        self.assertGreater(mr._term_score("Bitcoin ETF inflows surge as Fed rate cut nears"), 0)

    def test_no_double_counting_of_etf_flow_terms(self):
        self.assertAlmostEqual(mr._term_score("Spot ETF inflows return"), 1.5, places=6)   # one flow term = one count
        self.assertAlmostEqual(mr._term_score("Bitcoin ETF outflows continue"), -1.5, places=6)

    def test_publisher_suffix_dedupes_same_story(self):
        items = [{"title": "Bitcoin crash fears grow - Reuters", "age_hours": 1},
                 {"title": "Bitcoin crash fears grow - CoinDesk", "age_hours": 1},
                 {"title": "Bitcoin crash fears grow - Cointelegraph", "age_hours": 1}]
        self.assertIsNone(mr.score_headlines(items)[0])        # one unique story -> not enough headlines

    def test_altcoin_specific_stories_are_skipped(self):
        self.assertFalse(mr._is_btc_relevant("Hyperliquid (HYPE) Surges 3.11% on Buy-and-Burn, ETF Inflows"))
        self.assertTrue(mr._is_btc_relevant("Bitcoin holds $86K as ETF inflows ease Fed hike bets"))
        self.assertTrue(mr._is_btc_relevant("Bitcoin (BTC) price forecast"))
        self.assertTrue(mr._is_btc_relevant("Crypto market slides as Fed signals higher rates"))


class MacroFixTests(unittest.TestCase):
    def _chart(self, closes):
        return {"chart": {"result": [{"indicators": {"quote": [{"close": closes}]}}]}}

    def test_change_is_last_two_daily_closes_even_when_price_differs_by_a_hair(self):
        with mock.patch.object(mr, "_get_json", return_value=self._chart([5700.0, 5710.0, 5720.55])):
            level, pct = mr._yahoo_change("^GSPC")
        self.assertAlmostEqual(level, 5720.55)
        self.assertAlmostEqual(pct, (5720.55 / 5710.0 - 1) * 100, places=6)   # +0.185%, not ~0.00%

    def test_all_flat_macro_is_unavailable_not_neutral(self):
        flat = self._chart([100.0, 100.0])
        with mock.patch.object(mr, "_get_json", return_value=flat):
            score, note = mr._fetch_macro()
        self.assertIsNone(score)
        self.assertIn("stale", note)

    def test_real_macro_moves_are_scored(self):
        def fake(url, params=None, headers=None):
            moves = {"^GSPC": [5700, 5757], "DX-Y.NYB": [104.0, 103.2], "^VIX": [18.0, 15.0]}
            for key, closes in moves.items():
                if key in url:
                    return self._chart(closes)
            raise ValueError("unexpected " + url)
        with mock.patch.object(mr, "_get_json", side_effect=fake):
            score, note = mr._fetch_macro()
        self.assertGreater(score, 25)                                          # risk-on: stocks up, dollar and VIX down
        self.assertIn("S&P", note)


class DerivativesFixTests(unittest.TestCase):
    def test_taker_share_46_percent_is_only_mildly_bearish(self):
        k = {"quote_volume": [1e6] * 30, "taker_buy_quote": [4.63e5] * 30}
        score, share = mr.score_taker_flow(k)
        self.assertAlmostEqual(share, 0.463, places=3)
        self.assertGreater(score, -25)
        self.assertLess(score, 0)

    def test_funding_falls_back_to_okx_when_binance_futures_is_blocked(self):
        import requests
        def fake(url, params=None, headers=None):
            if "fapi.binance.com" in url:
                raise requests.ConnectionError("451 geo block")
            if "okx.com" in url:
                return {"data": [{"fundingRate": "0.0003"}]}
            return [[0, 0, 0, 0, 100.0, 0, 0, 1e6, 0, 0, 5e5, 0]] * 30        # kline rows for the taker part
        with mock.patch.object(mr, "_get_json", side_effect=fake):
            score, note = mr._fetch_derivatives()
        self.assertIn("funding +0.0300%", note)


if __name__ == "__main__":
    unittest.main()
