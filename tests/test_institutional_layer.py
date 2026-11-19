import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import quality_gate
import risk_manager as rm
import screener
import signal_quality as sq
import strategy_policy as sp
import strategy_stats as ss
import trade_tracker


def klines(closes, volumes=None, step=0.0):
    n = len(closes)
    return {"open_time": list(range(n)), "open": list(closes),
            "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
            "close": list(closes), "volume": volumes or [100.0] * n,
            "quote_volume": [1.0] * n, "taker_buy_quote_volume": [1.0] * n}


UP = klines([100 + i for i in range(120)])
DOWN = klines([220 - i for i in range(120)])


class PolicyFamilyTests(unittest.TestCase):
    def test_same_family_rules_are_one_vote(self):
        self.assertEqual(sp.independent_family_count(["Prime Edge", "Quant Prime"]), 1)       # WMA + DEMA
        self.assertEqual(sp.independent_family_count(["Elite Trend", "Smart Vector"]), 2)     # breakout + momentum
        self.assertEqual(sp.independent_family_count(["Elite Trend", "Quant Prime", "Signal Edge"]), 2)

    def test_three_way_group_is_watch_only_exception_but_signal_edge_stays_removed_elsewhere(self):
        self.assertEqual(sp.classify_strategy_group(["Elite Trend", "Quant Prime", "Signal Edge"], "LONG"), "WATCH_ONLY")
        self.assertIsNone(sp.classify_strategy_group(["Smart Vector", "Signal Edge"], "LONG"))
        self.assertIsNone(sp.classify_strategy_group(["Prime Edge", "Signal Edge"], "LONG"))
        self.assertIsNone(sp.classify_strategy_group(["Elite Trend", "Quant Prime", "Signal Edge", "Smart Vector"], "LONG"))


class SignalQualityTests(unittest.TestCase):
    def test_trend_alignment_labels(self):
        self.assertEqual(sq.trend_alignment("LONG", UP, UP)[0], "ALIGNED")
        self.assertEqual(sq.trend_alignment("SHORT", UP, UP)[0], "COUNTER-TREND")
        self.assertEqual(sq.trend_alignment("SHORT", DOWN, DOWN)[0], "ALIGNED")
        self.assertEqual(sq.trend_alignment("LONG", UP, DOWN)[0], "MIXED")
        self.assertIsNone(sq.trend_alignment("LONG", None, UP)[0])

    def test_components_are_bounded_so_one_spike_cannot_dominate(self):
        self.assertEqual(sq.volume_component(50.0), 1.0)
        self.assertEqual(sq.volume_component(0.5), 0.0)
        self.assertEqual(sq.confluence_component(9), 1.0)
        best = sq.score_signal("LONG", "ALIGNED", 1.0, 3, {"score": 80, "label": "BULLISH"})
        self.assertEqual(best["score"], 100.0)

    def test_regime_component_is_directional(self):
        bull = {"score": 40, "label": "BULLISH"}
        self.assertEqual(sq.regime_component(bull, True), 1.0)
        self.assertEqual(sq.regime_component(bull, False), 0.0)
        self.assertEqual(sq.regime_component(None, True), 0.5)

    def test_gate_for_live_vs_watch_only(self):
        strong = sq.score_signal("LONG", "ALIGNED", 1.0, 2, {"score": 20, "label": "BULLISH"})
        self.assertIsNone(sq.gate(strong, 2.6, live=True))
        weak = sq.score_signal("LONG", "PARTIAL", 0.2, 1, None)
        self.assertIn("below", sq.gate(weak, 2.0, live=True))
        self.assertIsNone(sq.gate(weak, 2.0, live=False))                      # watch-only is never blocked
        counter = sq.score_signal("LONG", "COUNTER-TREND", 1.0, 3, None)
        self.assertIn("oppose", sq.gate(counter, 3.0, live=True))
        unknown = sq.score_signal("LONG", None, 1.0, 3, None)
        self.assertIn("fail closed", sq.gate(unknown, 3.0, live=True))
        self.assertIn("volume ratio", sq.gate(strong, 1.2, live=True))

    def test_volume_ratio_uses_closed_candles_only(self):
        volumes = [100.0] * 21 + [300.0, 9999.0]          # last element is the forming candle
        self.assertAlmostEqual(sq.volume_ratio(klines([1.0] * 23, volumes)), 3.0)


class RiskManagerTests(unittest.TestCase):
    def test_suggested_size_risks_one_percent_and_is_never_levered(self):
        self.assertEqual(rm.suggested_position_usd(100, 98, equity=150, risk_pct=1.0), 75.0)
        self.assertEqual(rm.suggested_position_usd(100, 99.9, equity=150, risk_pct=1.0), 150.0)
        self.assertEqual(rm.suggested_position_usd(100, 110, equity=150, risk_pct=1.0), 15.0)
        self.assertIsNone(rm.suggested_position_usd(100, 100))

    def live(self, direction="LONG", **extra):
        return {"direction": direction, "action": "BUY" if direction == "LONG" else "SELL", **extra}

    def test_open_risk_caps_total_and_same_direction(self):
        book = [self.live("LONG") for _ in range(3)]
        self.assertIn("same-direction", rm.check_new(self.live("LONG"), book))
        self.assertIsNone(rm.check_new(self.live("SHORT"), book))
        full = [self.live("LONG")] * 3 + [self.live("SHORT")] * 2
        self.assertIn("max", rm.check_new(self.live("SHORT"), full))

    def test_break_even_positions_and_watch_only_do_not_use_risk(self):
        free = {**self.live("LONG"), "tp1_hit": True, "break_even_activated": True}
        book = [free] * 4 + [self.live("LONG", watch_only=True)] * 4
        self.assertIsNone(rm.check_new(self.live("LONG"), book))
        self.assertIsNone(rm.check_new(self.live("LONG", watch_only=True), [self.live("LONG")] * 9))

    def closed(self, outcome, minutes_ago, r, now, label="STRATEGY · Smart Vector"):
        when = (now - timedelta(minutes=minutes_ago)).isoformat()
        return {"status": "CLOSED", "outcome": outcome, "event_time_utc": when, "r_multiple": str(r), "scanner": label}

    def test_loss_streak_pause_and_recovery(self):
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        rows = [self.closed("SL_HIT", 100, -0.5, now), self.closed("SL_HIT", 60, -0.5, now), self.closed("SL_HIT", 30, -0.5, now)]
        self.assertIn("in a row", rm.breaker_reason(rows, now))
        self.assertIsNone(rm.breaker_reason(rows, now + timedelta(hours=3)))
        rows.append(self.closed("TP2_HIT", 5, 2.0, now))
        self.assertIsNone(rm.breaker_reason(rows, now))

    def test_daily_loss_limit(self):
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        rows = [self.closed("SL_HIT", 600, -1.5, now), self.closed("TP2_HIT", 500, 0.5, now),
                self.closed("SL_HIT", 400, -2.0, now)]
        self.assertIn("daily loss limit", rm.breaker_reason(rows, now))
        yesterday = [self.closed("SL_HIT", 60 * 30, -5, now)]
        self.assertIsNone(rm.breaker_reason(yesterday, now))

    def test_watch_only_rows_never_trip_the_breaker(self):
        now = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.csv")
            rows = [self.closed("SL_HIT", 10 * i, -1, now, "STRATEGY · WATCH ONLY · Smart Vector") for i in range(1, 6)]
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.assertEqual(rm.load_closed_live_rows(path), [])


class CostAndRMultipleTests(unittest.TestCase):
    def test_closed_stop_out_is_about_minus_one_r_after_costs(self):
        row = trade_tracker.journal_row({"event_id": "x", "symbol": "AAAUSDT", "action": "BUY", "price": 100.0,
                                         "stop_loss": 98.0, "position_size_usd": 100.0},
                                        status="CLOSED", outcome="SL_HIT", exit_price=98.0)
        self.assertAlmostEqual(row["realized_pnl_usd"], -2.0)
        self.assertAlmostEqual(row["cost_usd"], 0.30)
        self.assertAlmostEqual(row["net_pnl_usd"], -2.30)
        self.assertAlmostEqual(row["r_multiple"], -1.15, places=2)       # slightly worse than -1R: costs are real

    def test_r_uses_original_stop_after_break_even(self):
        row = trade_tracker.journal_row({"event_id": "y", "symbol": "AAAUSDT", "action": "BUY", "price": 100.0,
                                         "stop_loss": 100.0, "original_stop_loss": 98.0, "take_profit_1": 102.0,
                                         "tp1_hit": True, "partial_tp1_pct": 0.5, "position_size_usd": 100.0},
                                        status="CLOSED", outcome="BE_HIT", exit_price=100.0)
        self.assertAlmostEqual(row["realized_pnl_usd"], 1.0)             # half booked at TP1, rest at entry
        self.assertAlmostEqual(row["r_multiple"], (1.0 - 0.30) / 2.0, places=2)

    def test_open_positions_have_no_net_figures_and_score_is_journaled(self):
        row = trade_tracker.journal_row({"event_id": "z", "symbol": "AAAUSDT", "action": "BUY", "price": 100.0,
                                         "details": {"quality_score": 77.5}})
        self.assertEqual((row["cost_usd"], row["net_pnl_usd"], row["r_multiple"]), ("", "", ""))
        self.assertEqual(row["quality_score"], 77.5)
        self.assertIn("quality_score", trade_tracker.FIELDS)


class StrategyStatsTests(unittest.TestCase):
    def rows(self, outcomes, label="STRATEGY · WATCH ONLY · Consensus: A, B", r=None):
        out = []
        for i, outcome in enumerate(outcomes):
            net = {"TP2_HIT": 4.0, "SL_HIT": -2.3, "BE_HIT": 0.2}[outcome]
            out.append({"status": "CLOSED", "outcome": outcome, "scanner": label, "net_pnl_usd": str(net),
                        "r_multiple": str({"TP2_HIT": 1.8, "SL_HIT": -1.15, "BE_HIT": 0.1}[outcome]),
                        "event_time_utc": f"2026-10-09T{i % 24:02d}:00:00+00:00", "quality_score": "75"})
        return out

    def test_small_samples_never_get_a_verdict(self):
        s = ss.summarize(self.rows(["TP2_HIT"] * 12 + ["SL_HIT"] * 4))
        self.assertTrue(s["recommendation"].startswith("INSUFFICIENT DATA"))
        self.assertGreater(s["win_rate"], 0.7)
        self.assertLess(s["ci"][0], 0.55)                                # 75% on 16 trades is a wide interval

    def test_promote_suspend_continue(self):
        good = ss.summarize(self.rows(["TP2_HIT", "TP2_HIT", "SL_HIT"] * 12))
        self.assertTrue(good["recommendation"].startswith("PROMOTE"), good)
        bad = ss.summarize(self.rows(["SL_HIT", "SL_HIT", "SL_HIT", "TP2_HIT"] * 9))
        self.assertTrue(bad["recommendation"].startswith("SUSPEND"), bad)
        meh = ss.summarize(self.rows(["TP2_HIT", "SL_HIT", "SL_HIT", "BE_HIT"] * 8))
        self.assertTrue(meh["recommendation"].startswith(("CONTINUE", "SUSPEND")))

    def test_wilson_and_drawdown(self):
        low, high = ss.wilson_interval(9, 12)
        self.assertTrue(0.46 < low < 0.50 and 0.90 < high < 0.96, (low, high))
        self.assertEqual(ss.max_drawdown([1, -1, -1, 2, -3]), -3)

    def test_table_groups_by_strategy_and_mode_and_reports_write(self):
        rows = self.rows(["TP2_HIT", "SL_HIT"]) + self.rows(["SL_HIT"], label="WHALE FLOW · ON-CHAIN DEX")
        table = ss.build_table(rows)
        self.assertEqual({(t["name"], t["mode"]) for t in table},
                         {("Consensus: A, B", "WATCH_ONLY"), ("WHALE FLOW · ON-CHAIN DEX", "LIVE")})
        with tempfile.TemporaryDirectory() as tmp:
            md = ss.write_reports(rows, tmp)
            self.assertIn("Consensus: A, B", md)
            self.assertTrue(os.path.exists(os.path.join(tmp, "strategy_performance.csv")))

    def test_ambiguous_trades_are_excluded_from_stats(self):
        rows = self.rows(["TP2_HIT"]) + [{"status": "CLOSED", "outcome": "AMBIGUOUS_STOP_AND_TP_SAME_CANDLE",
                                          "scanner": "STRATEGY · WATCH ONLY · Consensus: A, B"}]
        s = ss.summarize(rows)
        self.assertEqual((s["n"], s["ambiguous"]), (1, 1))


class ScreenerIntegrationTests(unittest.TestCase):
    def test_whale_flow_uses_volatility_stop_not_flat_two_and_a_half_percent(self):
        calm = klines([100.0] * 60)
        wild = klines([100 + (8 if i % 2 else -8) for i in range(60)])
        c, w = screener.atr_trade_levels(calm, "LONG"), screener.atr_trade_levels(wild, "LONG")
        self.assertLess(100 - c["stop_loss"], 100 - w["stop_loss"])
        short = screener.atr_trade_levels(wild, "SHORT")
        self.assertGreater(short["stop_loss"], short["price"])
        self.assertLess(short["take_profit_2"], short["take_profit_1"])
        self.assertIsNone(screener.atr_trade_levels(klines([100.0] * 10), "LONG"))

    def test_assess_quality_annotates_watch_only_and_blocks_weak_live_signals(self):
        def fake(symbol, interval, limit=120, start_time=None):
            return UP
        watch = {"symbol": "AAAUSDT", "direction": "SHORT", "timeframe": "4h", "watch_only": True, "details": {}}
        live = {"symbol": "AAAUSDT", "direction": "SHORT", "timeframe": "4h", "details": {}}
        with mock.patch.object(screener, "get_klines", side_effect=fake), mock.patch.object(screener, "CURRENT_REGIME", None):
            self.assertIsNone(screener.assess_quality(watch, "AAAUSDT", "SHORT", 1))
            self.assertEqual(watch["details"]["trend_alignment"], "COUNTER-TREND")
            self.assertFalse(watch["details"]["passes_live_gate"])
            self.assertIsNone(screener.assess_quality(live, "AAAUSDT", "SHORT", 3))
            self.assertTrue(live["watch_only"])                              # downgraded, still tracked
            self.assertEqual((live["tier"], live["tier_risk_pct"]), ("C", 0.0))
            self.assertIn("oppose", live["downgrade_reason"])

    def test_assess_quality_fails_closed_for_live_when_data_missing(self):
        live = {"symbol": "AAAUSDT", "direction": "LONG", "timeframe": "1h", "details": {}}
        with mock.patch.object(screener, "get_klines", return_value=None), mock.patch.object(screener, "CURRENT_REGIME", None):
            self.assertIsNone(screener.assess_quality(live, "AAAUSDT", "LONG", 2))
            self.assertTrue(live["watch_only"])
            self.assertIn("unavailable", live["downgrade_reason"])

    def test_flow_signal_gets_second_evidence_family_when_trend_agrees(self):
        live = {"symbol": "AAAUSDT", "direction": "LONG", "timeframe": "3h on-chain DEX", "details": {}}
        with mock.patch.object(screener, "get_klines", return_value=UP), mock.patch.object(screener, "CURRENT_REGIME", None):
            screener.assess_quality(live, "AAAUSDT", "LONG", 1, flow_imbalance_pct=45.0)
        self.assertEqual(live["details"]["independent_families"], 2)

    def test_rank_key_prefers_higher_quality_score(self):
        a = {"details": {"quality_score": 80}, "price": 100, "stop_loss": 98}
        b = {"details": {"quality_score": 60, "independent_families": 3}, "price": 100, "stop_loss": 99, "strategy_agreement_count": 3}
        self.assertEqual(sorted([b, a], key=quality_gate.rank_key)[0], a)


if __name__ == "__main__":
    unittest.main()


class WorkflowTests(unittest.TestCase):
    def test_weekly_review_workflow_exists_in_both_copies_and_is_identical(self):
        a = (ROOT / ".github/workflows/weekly_review.yml").read_text(encoding="utf-8")
        b = (ROOT / "workflows/weekly_review.yml").read_text(encoding="utf-8")
        self.assertEqual(a, b)
        self.assertIn("python src/strategy_stats.py", a)
        self.assertIn('cron: "10 0 * * 1"', a)

    def test_workflow_copies_stay_in_sync(self):
        for name in ("scan.yml", "news.yml"):
            self.assertEqual((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"),
                             (ROOT / "workflows" / name).read_text(encoding="utf-8"), name)


class WhaleFlowModeTests(unittest.TestCase):
    def test_whale_flow_is_a_labelled_forward_test_by_default(self):
        self.assertEqual(sp.WHALE_FLOW_MODE, "WATCH_ONLY")
        base = {"event_id": "w", "symbol": "AAAUSDT", "scanner": "WHALE_FLOW", "action": "BUY", "price": 100.0}
        self.assertEqual(trade_tracker.journal_row({**base, "watch_only": True})["scanner"],
                         "WHALE FLOW · ON-CHAIN DEX · WATCH ONLY")
        self.assertEqual(trade_tracker.journal_row(base)["scanner"], "WHALE FLOW · ON-CHAIN DEX")
        name, mode = ss.normalize_source("WHALE FLOW · ON-CHAIN DEX · WATCH ONLY")
        self.assertEqual((name, mode), ("WHALE FLOW · ON-CHAIN DEX", "WATCH_ONLY"))


class V21Tests(unittest.TestCase):
    def cand(self, sid, name, tf, direction="LONG"):
        return {"strategy_id": sid, "timeframe": tf, "direction": direction, "symbol": "AAAUSDT"}

    PROFILES = [{"item_id": 119, "name": "Elite Trend", "rank": 4}, {"item_id": 486, "name": "Smart Vector", "rank": 6},
                {"item_id": 3713, "name": "Quant Prime", "rank": 9}]

    def test_consensus_needs_same_timeframe(self):
        mixed = {119: [self.cand(119, "E", "1h")], 486: [self.cand(486, "S", "4h")]}
        groups, _, _ = screener.consolidate_strategy_candidates(mixed, self.PROFILES)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["strategy_agreement_count"], 1)          # 1h + 4h no longer a "consensus"
        same = {119: [self.cand(119, "E", "4h")], 486: [self.cand(486, "S", "4h"), self.cand(486, "S", "1h")]}
        groups, _, _ = screener.consolidate_strategy_candidates(same, self.PROFILES)
        self.assertEqual(groups[0]["strategy_names"], ["Elite Trend", "Smart Vector"])
        self.assertEqual(groups[0]["timeframe"], "4h")

    def test_switch_restores_old_behaviour(self):
        mixed = {119: [self.cand(119, "E", "1h")], 486: [self.cand(486, "S", "4h")]}
        with mock.patch.object(screener, "CONSENSUS_SAME_TIMEFRAME", False):
            groups, _, _ = screener.consolidate_strategy_candidates(mixed, self.PROFILES)
        self.assertEqual(groups[0]["strategy_agreement_count"], 2)

    def test_universe_drops_illiquid_pairs(self):
        class R:
            def __init__(self, data): self.data = data
            def raise_for_status(self): pass
            def json(self): return self.data
        ticker = [{"symbol": "AAAUSDT", "quoteVolume": "9000000"}, {"symbol": "BBBUSDT", "quoteVolume": "100000"}]
        info = {"symbols": [{"symbol": s, "status": "TRADING", "quoteAsset": "USDT", "isSpotTradingAllowed": True}
                            for s in ("AAAUSDT", "BBBUSDT")]}
        with mock.patch.object(screener.requests, "get", side_effect=[R(ticker), R(info)]), \
                mock.patch.object(screener, "get_quality_symbols", return_value=None):
            self.assertEqual(screener.get_usdt_universe(), ["AAAUSDT"])

    def test_exit_diagnostics_from_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log.csv")
            fields = ["event_id", "event_time_utc", "status", "outcome", "scanner", "price", "stop_loss", "details"]
            rows = [
                {"event_id": "a", "event_time_utc": "2026-10-09T00:00:00+00:00", "status": "SIGNAL", "scanner": "STRATEGY · Smart Vector"},
                {"event_id": "a", "event_time_utc": "2026-10-10T09:00:00+00:00", "status": "CLOSED", "outcome": "SL_HIT",
                 "scanner": "STRATEGY · Smart Vector", "price": "100", "stop_loss": "98", "details": '{"peak_profit_pct": 2.5}'}]
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
            closed = ss.attach_exit_diagnostics(ss.read_closed(path), path)
            d = ss.exit_diagnostics(closed)
        self.assertAlmostEqual(d["median_hold_hours_sl"], 33.0)
        self.assertEqual((d["sl_were_1r_up"], d["sl_measurable"]), (1, 1))


class TierTests(unittest.TestCase):
    def test_tier_grading(self):
        self.assertEqual(sq.tier(75, "ALIGNED", 2, "ALIGNED"), "A")
        self.assertEqual(sq.tier(75, "ALIGNED", 1, "ALIGNED"), "B")          # one evidence family caps it at B
        self.assertEqual(sq.tier(60, "MIXED", 2, "NEUTRAL"), "B")
        self.assertEqual(sq.tier(45, "ALIGNED", 3, "ALIGNED"), "C")
        self.assertEqual(sq.tier(90, "COUNTER-TREND", 3, "ALIGNED"), "C")
        self.assertEqual(sq.tier(90, "ALIGNED", 3, "COUNTER-TREND"), "C")    # BTC regime against the trade
        self.assertEqual(sq.tier(90, None, 3, "ALIGNED"), "C")

    def test_score_signal_carries_tier_and_regime_alignment(self):
        bear = {"score": -30, "label": "BEARISH"}
        r = sq.score_signal("LONG", "ALIGNED", 1.0, 3, bear)
        self.assertEqual((r["regime_alignment"], r["tier"]), ("COUNTER-TREND", "C"))
        self.assertEqual(sq.score_signal("SHORT", "ALIGNED", 1.0, 3, bear)["tier"], "A")

    def test_neutral_regime_can_still_produce_tier_b(self):
        neutral = {"score": -9, "label": "NEUTRAL"}
        r = sq.score_signal("SHORT", "ALIGNED", 0.6, 2, neutral)
        self.assertIn(r["tier"], {"A", "B"})

    def test_tier_risk_flows_into_open_risk_and_limits(self):
        half = {"direction": "LONG", "tier_risk_pct": 0.5}
        self.assertEqual(rm.open_risk_pct([half, half]), 1.0)
        book = [{"direction": "LONG", "tier_risk_pct": 1.0}] * 2
        self.assertIsNone(rm.check_new({"direction": "LONG", "tier_risk_pct": 0.5}, book))
        self.assertIn("same-direction", rm.check_new({"direction": "LONG", "tier_risk_pct": 1.5}, book))

    def test_journal_records_tier(self):
        row = trade_tracker.journal_row({"event_id": "t", "symbol": "AAAUSDT", "action": "BUY", "price": 100.0,
                                         "tier": "B", "details": {"quality_score": 55}})
        self.assertEqual(row["tier"], "B")
        self.assertIn("tier", trade_tracker.FIELDS)


class BoardAndReportTests(unittest.TestCase):
    def test_board_ranks_by_tier_then_score_and_dedupes(self):
        import daily_board as db
        now = datetime(2026, 10, 10, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "board.json")
            def sig(sym, score, tier, key=None, side="SHORT"):
                return {"event_id": key or sym, "symbol": sym, "direction": side, "scanner": "STRATEGY", "price": 10.0,
                        "stop_loss": 10.4, "timeframe": "4h", "details": {"quality_score": score, "tier": tier}}
            for s in (sig("AAAUSDT", 55, "B"), sig("BBBUSDT", 90, "A"), sig("CCCUSDT", 30, "C"), sig("DDDUSDT", 72, "A"),
                      sig("EEEUSDT", 52, "B"), sig("FFFUSDT", 51, "B")):
                self.assertTrue(db.record(s, now, path))
            self.assertFalse(db.record(sig("AAAUSDT", 55, "B"), now, path))          # duplicate event
            self.assertFalse(db.record({"symbol": "X", "details": {}}, now, path))    # unscored is ignored
            state = db.load(now, path)
            ranked = db.top(state["entries"])
            self.assertEqual([e["symbol"] for e in ranked], ["BBBUSDT", "DDDUSDT", "AAAUSDT", "EEEUSDT", "FFFUSDT"])
            self.assertEqual(db.due_slot(state, now), 12)
            db.mark_posted(state, 12, path)
            self.assertIsNone(db.due_slot(db.load(now, path), now))
            self.assertIn("Daily Top 5", db.build_embed(state, 12)["embeds"][0]["title"])
            # a new UTC day starts empty
            self.assertEqual(db.load(now + timedelta(days=1), path)["entries"], [])

    def test_board_is_honest_on_a_quiet_day(self):
        import daily_board as db
        state = {"date": "2026-10-10", "entries": [], "posted_slots": []}
        text = db.build_embed(state, 6)["embeds"][0]
        self.assertIn("No setup", text["description"])
        self.assertIn("filter working", text["footer"]["text"])

    def test_slots_before_06_utc_are_not_due(self):
        import daily_board as db
        early = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
        self.assertIsNone(db.due_slot({"posted_slots": []}, early))

    def test_leveraged_token_filter_keeps_real_coins(self):
        for real in ("JUP", "SUPER", "UP", "TRUMP"):
            self.assertFalse(screener.is_leveraged_token(real), real)
        for fake in ("BTCUP", "ETHDOWN", "BNBBULL", "XRPBEAR"):
            self.assertTrue(screener.is_leveraged_token(fake), fake)

    def test_tier_buckets_and_daily_summary(self):
        rows = []
        for i, (tier, outcome, net, r) in enumerate([("A", "TP2_HIT", 5.0, 2.0), ("A", "SL_HIT", -2.3, -1.15),
                                                      ("C", "SL_HIT", -2.3, -1.15), ("B", "TP2_HIT", 4.0, 1.8)]):
            rows.append({"status": "CLOSED", "outcome": outcome, "scanner": "STRATEGY · WATCH ONLY · Smart Vector",
                         "net_pnl_usd": str(net), "r_multiple": str(r), "tier": tier,
                         "event_time_utc": f"2026-10-{9 + i % 2:02d}T10:00:00+00:00"})
        tiers = ss.tier_buckets(rows)
        self.assertEqual({k: v["n"] for k, v in tiers.items()}, {"Tier A": 2, "Tier B": 1, "Tier C": 1})
        daily = ss.daily_summary(rows)
        self.assertEqual(daily["days"], 2)
        self.assertAlmostEqual(sum([5.0, -2.3, -2.3, 4.0]) / 2, daily["mean"])
        md = ss.render_markdown(ss.build_table(rows), {}, len(rows), {}, tiers, daily)
        self.assertIn("Do the tiers work", md)
        self.assertIn("Reality check", md)

    def test_dashboard_reset_version_was_bumped(self):
        from src import google_sheets
        self.assertEqual(google_sheets.FRESH_START_VERSION, "zero-reset-v2-2-tiers")


class StateAndWorkflowV22Tests(unittest.TestCase):
    def test_new_state_files_are_persisted_by_the_scan_workflow(self):
        for name in (".github/workflows/scan.yml", "workflows/scan.yml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("daily_board.json", text)
            self.assertIn("quality_symbols_cache.json", text)

    def test_zero_state_ships_in_the_zip(self):
        import json
        self.assertEqual(json.loads((ROOT / "open_trades.json").read_text(encoding="utf-8")), [])
        self.assertEqual((ROOT / "last_signals.txt").read_text(encoding="utf-8").strip(), "")
        header = (ROOT / "deep_pocket_log.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(header), 1)
        self.assertEqual(header[0].split(","), trade_tracker.FIELDS)


class V23InstitutionalSniperTests(unittest.TestCase):
    def test_session_intelligence_identifies_overlap_and_dead_zone(self):
        overlap = datetime(2026, 10, 17, 14, 30, tzinfo=timezone.utc)
        dead = datetime(2026, 10, 17, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(sq.session_info(overlap)["name"], "LONDON_NY_OVERLAP")
        self.assertGreater(sq.session_info(overlap)["score_bonus"], 0)
        self.assertEqual(sq.session_info(dead)["name"], "ASIA_DEAD_ZONE")
        self.assertLess(sq.session_info(dead)["score_bonus"], 0)

    def test_step_lock_ratchet_moves_stop_to_tp1_after_55_pct_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            open_f = os.path.join(tmp, "o.json")
            log_f = os.path.join(tmp, "l.csv")
            pend_f = os.path.join(tmp, "p.json")
            with mock.patch.object(trade_tracker, "OPEN_FILE", open_f), \
                 mock.patch.object(trade_tracker, "JOURNAL_FILE", log_f), \
                 mock.patch.object(trade_tracker, "PENDING_SHEET_FILE", pend_f):
                trade = {
                    "event_id": "ratchet-1", "symbol": "SOLUSDT", "scanner": "STRATEGY",
                    "strategy_name": "Smart Vector", "direction": "LONG", "action": "BUY",
                    "price": 100.0, "stop_loss": 97.0, "take_profit_1": 103.0, "take_profit_2": 109.0,
                    "partial_tp1_pct": 0.5, "position_size_usd": 100.0, "quantity": 1.0, "created_ts": 0,
                }
                trade_tracker.save_open([trade])
                # Candle 1 hits TP1 (103.5); Candle 2 reaches 107.0 (> 106.3 = 55% from 103 to 109) then pulls back to 103.0 (TP1)
                candles = {
                    "open_time": [60_000, 120_000, 180_000, 240_000],
                    "open": [100.0, 103.5, 106.5, 103.0],
                    "high": [103.5, 107.0, 106.5, 103.0],
                    "low": [99.5, 103.2, 102.8, 102.5],
                    "close": [103.2, 106.5, 103.0, 103.0],
                }
                rows = trade_tracker.settle_open(lambda *a, **k: candles)
                closed = [r for r in rows if r.get("outcome") == "BE_HIT"][0]
                # Both halves exited at TP1 (103.0) -> full $3.00 profit instead of $1.50!
                self.assertAlmostEqual(closed["realized_pnl_usd"], 3.0, places=2)

    def test_news_monitor_matches_full_coin_names_and_macro_headlines(self):
        import news_monitor as nm
        universe = {"BTC", "ETH", "SOL", "LINK"}
        cat = nm.find_category("Chainlink partners with major bank for tokenized assets")
        self.assertEqual(cat, "Partnership")
        self.assertEqual(nm.find_mentioned_coin("Chainlink partners with major bank", universe, category=cat), "LINK")
        macro_cat = nm.find_category("Federal Reserve announces rate cut after CPI inflation cools")
        self.assertEqual(macro_cat, "Macro / Fed / CPI")
        self.assertEqual(nm.find_mentioned_coin("Federal Reserve announces rate cut", universe, category=macro_cat), "BTC")

    def test_tier_c_signals_are_suppressed_from_discord_strategy_alerts(self):
        sig = {"symbol": "XRPUSDT", "scanner": "MOVERS_PULLBACK", "direction": "SHORT", "action": "SELL",
               "price": 1.29, "stop_loss": 1.32, "take_profit_1": 1.26, "take_profit_2": 1.21,
               "tier": "C", "details": {"quality_score": 38, "tier": "C"}}
        with mock.patch.object(screener, "DISCORD_WEBHOOK_URL", "https://example.invalid/hook"), \
             mock.patch.object(screener, "_post_discord") as post:
            screener.send_discord_strategy_signal(sig)
        post.assert_not_called()

    def test_analytics_tab_and_charts_are_configured(self):
        from src import google_sheets as gs
        self.assertGreaterEqual(len(gs.ANALYTICS_STRATEGY_ROWS), 12)
        self.assertEqual(len(gs.ANALYTICS_STRATEGY_ROWS[0]), 14)
        charts = gs._analytics_chart_requests(99, [])
        self.assertEqual(len(charts), 3)


class V30SmartMoneyAndBacktesterTests(unittest.TestCase):
    def test_synthetic_stock_and_gold_tickers_are_blocked_in_top_30_selector(self):
        from quality_filter import TOP_30_INSTITUTIONAL_BASES, MANUAL_MEME_BLACKLIST
        for bad in ("NVDAB", "TSLAB", "SPCXB", "EWYB", "XAUT", "PUMP"):
            self.assertIn(bad, MANUAL_MEME_BLACKLIST)
            self.assertNotIn(bad, TOP_30_INSTITUTIONAL_BASES)
        coins = ["PAXGUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT"]
        selected = screener.select_top_institutional_universe(coins, limit=3)
        self.assertEqual(selected[:3], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    def test_smart_money_cvd_breakdown_detects_impulsive_market_selloff(self):
        import smart_money
        opens = [100.0] * 61
        highs = [100.6] * 61
        lows = [99.4] * 61
        closes = [100.0] * 61
        qvols = [1_000_000.0] * 61
        tb_qvols = [500_000.0] * 61
        # Closed candle at idx=59 breaks below 99.4 support floor with 1.8% risk and heavy seller CVD (37.5% taker buy)
        opens[59] = 99.6
        highs[59] = 99.7
        lows[59] = 98.0
        closes[59] = 98.2
        qvols[59] = 3_200_000.0
        tb_qvols[59] = 1_200_000.0
        klines = {
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": qvols, "quote_volume": qvols, "taker_buy_quote_volume": tb_qvols,
        }
        sigs = smart_money.evaluate_latest("SOLUSDT", klines, "1h", btc_bearish_bias=True)
        names = [s["strategy_name"] for s in sigs]
        self.assertIn("Smart Money CVD Breakdown", names)
        bd = [s for s in sigs if s["strategy_name"] == "Smart Money CVD Breakdown"][0]
        self.assertEqual(bd["direction"], "SHORT")
        self.assertGreater(bd["stop_loss"], bd["price"])
        self.assertLess(bd["take_profit_1"], bd["price"])

    def test_vectorized_backtester_simulates_and_ranks_strategies(self):
        import backtester
        sig = {
            "symbol": "ETHUSDT",
            "strategy_name": "Smart Money CVD Breakdown",
            "direction": "SHORT",
            "timeframe": "1h",
            "price": 100.0,
            "stop_loss": 102.0,
        }
        klines = {
            "open": [100.0, 99.5, 97.5, 94.5, 94.5],
            "high": [100.5, 99.8, 98.2, 96.0, 95.0],
            "low": [99.5, 97.8, 94.6, 94.0, 94.0],
            "close": [100.0, 97.9, 94.8, 94.2, 94.2],
        }
        res = backtester.simulate_trade_on_candles(sig, klines, 0)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "TP2_HIT")
        self.assertTrue(res["tp1_reached"])
        self.assertGreater(res["net_pnl_usd"], 1.4)
        summary = backtester.summarize_strategy_results("Smart Money CVD Breakdown", [res] * 5)
        self.assertTrue(summary["approved"])
        self.assertGreater(summary["profit_factor"], 2.0)

    def test_institutional_v3_gates_reject_asia_dead_zone_and_counter_cvd(self):
        good_sig = {
            "symbol": "SOLUSDT",
            "direction": "SHORT",
            "action": "SELL",
            "details": {
                "session_name": "LONDON_NY_OVERLAP",
                "trend_alignment": "ALIGNED",
                "volume_ratio": 1.65,
                "taker_buy_share_pct": 42.0,
            },
        }
        ok, reason = screener.passes_institutional_v3_gates(good_sig)
        self.assertTrue(ok, reason)

        bad_session = dict(good_sig, details=dict(good_sig["details"], session_name="ASIA_DEAD_ZONE"))
        ok2, reason2 = screener.passes_institutional_v3_gates(bad_session)
        self.assertFalse(ok2)
        self.assertIn("Asia Dead-Zone", reason2)

        bad_cvd = dict(good_sig, details=dict(good_sig["details"], taker_buy_share_pct=58.0))
        ok3, reason3 = screener.passes_institutional_v3_gates(bad_cvd)
        self.assertFalse(ok3)
        self.assertIn("opposing spot buyer absorption", reason3)

    def test_oi_price_cvd_quadrant_blocks_bottom_capitulation_shorts(self):
        import smart_money
        # Price dropped -5.2% while Open Interest flushed -4.5% (Longs liquidated at bottom)
        q_cap = smart_money.classify_oi_price_cvd_quadrant(
            price_change_pct=-5.2, oi_change_pct=-4.5, taker_buy_share=0.48, rsi_val=23.0
        )
        self.assertEqual(q_cap["quadrant"], "BOTTOM_CAPITULATION_TRAP")
        self.assertTrue(q_cap["blocks_short"])

        # Price dropped -1.8% while Open Interest rose +2.8% with seller CVD -> Fresh Shorts Building
        q_fresh = smart_money.classify_oi_price_cvd_quadrant(
            price_change_pct=-1.8, oi_change_pct=2.8, taker_buy_share=0.41, rsi_val=42.0
        )
        self.assertEqual(q_fresh["quadrant"], "NEW_SHORTS_BUILDING")
        self.assertFalse(q_fresh["blocks_short"])
        self.assertTrue(q_fresh["blocks_long"])

    def test_l2_orderbook_wall_filter_blocks_short_into_giant_bid_wall(self):
        import smart_money
        # Entry at 100.0, TP1 at 98.0, giant bid wall sitting at 99.0
        orderbook = {
            "bids": [["99.8", "10"], ["99.5", "10"], ["99.0", "500"], ["98.5", "10"]],
            "asks": [["100.2", "10"], ["100.5", "10"], ["101.0", "10"], ["101.5", "10"]],
        }
        res = smart_money.analyze_l2_orderbook_wall(orderbook, 100.0, "SHORT", tp1_price=98.0)
        self.assertFalse(res["ok"])
        self.assertTrue(res["wall_blocked"])

        # Balanced/ask-heavy orderbook allows SHORT
        good_ob = {
            "bids": [["99.8", "10"], ["99.5", "10"], ["99.0", "12"], ["98.5", "10"]],
            "asks": [["100.2", "20"], ["100.5", "20"], ["101.0", "20"], ["101.5", "20"]],
        }
        res_ok = smart_money.analyze_l2_orderbook_wall(good_ob, 100.0, "SHORT", tp1_price=98.0)
        self.assertTrue(res_ok["ok"])
        self.assertGreater(res_ok["score_bonus"], 0)

    def test_relative_strength_vs_btc_blocks_shorting_strong_bullish_decoupler(self):
        import smart_money
        btc_kl = {
            "close": [100.0, 100.0, 99.5, 99.0, 98.5, 98.0, 98.0],
            "quote_volume": [1000.0] * 7,
            "taker_buy_quote_volume": [420.0] * 7,
        }
        strong_coin_kl = {
            "close": [10.0, 10.0, 10.1, 10.2, 10.3, 10.25, 10.25],
            "quote_volume": [1000.0] * 7,
            "taker_buy_quote_volume": [600.0] * 7,
        }
        rs = smart_money.relative_strength_vs_btc(strong_coin_kl, btc_kl, lookback=4)
        self.assertEqual(rs["status"], "STRONG_LEADER")
        self.assertTrue(rs["blocks_short"])

        weak_coin_kl = {
            "close": [10.0, 10.0, 9.8, 9.6, 9.5, 9.4, 9.4],
            "quote_volume": [1000.0] * 7,
            "taker_buy_quote_volume": [380.0] * 7,
        }
        rs_weak = smart_money.relative_strength_vs_btc(weak_coin_kl, btc_kl, lookback=4)
        self.assertEqual(rs_weak["status"], "WEAK_LAGGARD")
        self.assertFalse(rs_weak["blocks_short"])
        self.assertGreater(rs_weak["score_bonus_short"], 0)

    def test_macro_whipsaw_blackout_blocks_entries_during_breaking_fed_cpi_shock(self):
        import smart_money
        now = datetime(2026, 10, 27, 12, 45, tzinfo=timezone.utc)
        news = [{
            "title": "US CPI inflation comes in hotter than expected, Federal Reserve signals caution",
            "category": "Macro / Fed / CPI",
            "published_ts": now.timestamp() - 15 * 60,  # 15 minutes ago
        }]
        chk = smart_money.check_macro_whipsaw_blackout(news, now_utc=now)
        self.assertTrue(chk["blackout"])

        sig = {
            "symbol": "SOLUSDT",
            "direction": "SHORT",
            "action": "SELL",
            "details": {
                "session_name": "LONDON_NY_OVERLAP",
                "trend_alignment": "ALIGNED",
                "volume_ratio": 1.65,
                "taker_buy_share_pct": 42.0,
            },
        }
        ok, reason = screener.passes_institutional_v3_gates(sig, recent_news_items=news, now_utc=now)
        self.assertFalse(ok)
        self.assertIn("Macro Whipsaw Blackout", reason)

    def test_v50_dynamic_auto_selector_promotes_profitable_solo_strategies_and_kicks_out_losers(self):
        import strategy_policy as sp
        rankings = {
            "Quant Prime": {"strategy": "Quant Prime", "trades": 36, "wins": 22, "losses": 13, "win_rate_pct": 61.1, "profit_factor": 1.40, "net_pnl_usd": 19.88, "approved": True, "verdict": "✅ ACTIVE (LIVE)"},
            "Smart Vector": {"strategy": "Smart Vector", "trades": 88, "wins": 30, "losses": 51, "win_rate_pct": 34.1, "profit_factor": 0.58, "net_pnl_usd": -83.59, "approved": False, "verdict": "🛑 DISABLED (KICKED OUT)"},
        }
        # Previously Quant Prime solo was blocked (None); now Dynamic Auto-Selector promotes it to ACTIVE!
        self.assertEqual(sp.classify_strategy_with_backtest(["Quant Prime"], "SHORT", rankings), "ACTIVE")
        self.assertGreaterEqual(sp.independent_family_count(["Quant Prime"], backtest_rankings=rankings), 2)
        # Smart Vector failed the historical candle backtest -> automatically kicked out (None)!
        self.assertIsNone(sp.classify_strategy_with_backtest(["Smart Vector"], "SHORT", rankings))

    def test_v50_smart_money_ema20_pullback_catches_stair_step_downtrend_dumps(self):
        import smart_money
        # Build a steady stair-step downtrend from 120 down to 96 (EMA20 < EMA50), then a pullback to EMA20 at idx=58 and bearish rejection at idx=59
        closes = [120.0 - i * 0.4 for i in range(61)]
        opens = [c + 0.25 for c in closes]
        highs = [o + 0.20 for o in opens]
        lows = [c - 0.20 for c in closes]
        qvols = [1_000_000.0] * 61
        tb_qvols = [440_000.0] * 61  # 44% taker buy -> 56% aggressive seller CVD

        ema20_val = smart_money._ema(closes[:60], 20)[-1]
        # Candle 58 pulls back to touch EMA20; Candle 59 rejects below EMA20 and closes bearish
        highs[58] = ema20_val * 1.001
        opens[59] = ema20_val * 0.999
        highs[59] = ema20_val * 1.001
        closes[59] = ema20_val * 0.990
        lows[59] = closes[59] - 0.1
        qvols[59] = 1_800_000.0
        tb_qvols[59] = 750_000.0

        klines = {
            "open": opens, "high": highs, "low": lows, "close": closes,
            "volume": qvols, "quote_volume": qvols, "taker_buy_quote_volume": tb_qvols,
        }
        sigs = smart_money.evaluate_latest("BTCUSDT", klines, "1h", btc_bearish_bias=True)
        names = [s["strategy_name"] for s in sigs]
        self.assertIn("Smart Money EMA20 Pullback", names)

    def test_v50_google_sheets_and_discord_backtest_league_tables_populate_all_strategies(self):
        import backtester
        from src import google_sheets as gs
        rankings = {
            "Smart Money Liquidity Sweep": {"strategy": "Smart Money Liquidity Sweep", "trades": 12, "wins": 10, "losses": 2, "win_rate_pct": 83.3, "profit_factor": 9.22, "max_drawdown_pct": 1.54, "net_pnl_usd": 27.71, "approved": True, "verdict": "✅ ACTIVE (LIVE)"},
            "Quant Prime": {"strategy": "Quant Prime", "trades": 36, "wins": 22, "losses": 13, "win_rate_pct": 61.1, "profit_factor": 1.40, "max_drawdown_pct": 7.34, "net_pnl_usd": 19.88, "approved": True, "verdict": "✅ ACTIVE (LIVE)"},
            "Smart Vector": {"strategy": "Smart Vector", "trades": 88, "wins": 30, "losses": 51, "win_rate_pct": 34.1, "profit_factor": 0.58, "max_drawdown_pct": 68.19, "net_pnl_usd": -83.59, "approved": False, "verdict": "🛑 DISABLED (KICKED OUT)"},
        }
        rows = gs.build_historical_backtest_rows(rankings)
        self.assertEqual(len(rows), 4)  # 1 header + 3 strategy rows
        self.assertEqual(rows[1][0], "Smart Money Liquidity Sweep")
        self.assertEqual(rows[2][0], "Quant Prime")
        self.assertEqual(rows[3][0], "Smart Vector")
        active_str, disabled_str = backtester.format_discord_league_table(rankings)
        self.assertIn("Smart Money Liquidity Sweep", active_str)
        self.assertIn("Quant Prime", active_str)
        self.assertIn("Smart Vector", disabled_str)
