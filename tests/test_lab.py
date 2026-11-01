import json
import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dca_lab.config import ASSETS, Config
from dca_lab.engine import Rules, amount_for_slot, day_groups, simulate
from dca_lab.evaluate import comparison, confidence_interval, evidence_gate, visual_orders
from dca_lab.indicators import Feature, bottom_signals, features, ready, rsi, score, smooth
from dca_lab.market import Bar, HOUR, get_json, milliseconds, validate
from dca_lab.report import render_report
from dca_lab.adaptive import freeze, meaningful_gate
from dca_lab.schedule import daily_funding, daily_plan, entry_signal, exposure_budget
from dca_lab.portfolio import KEYS, CASH_POLICIES, OPPORTUNITY_POLICIES, PURE_POLICIES, SIGNAL_UPGRADES, blend, choose_asset, compare_portfolios, discount_ceiling, opportunity_signal, portfolio_simulate, signal_price_cap, signal_reserve_amount, trigger_diagnostics


def make_bars(n=5000):
    # Synthetic data is confined to tests, never evaluation reports.
    return [Bar(i * HOUR, 100 + math.sin(i / 17) * 2, 103 + math.sin(i / 17) * 2, 97 + math.sin(i / 17) * 2, 100 + math.sin((i + 1) / 17) * 2, 1000) for i in range(n)]


class ConfigTests(unittest.TestCase):
    def test_exact_spot_identities(self):
        self.assertEqual([a["symbol"] for a in ASSETS.values()], ["GOOGLBUSDT", "XAUTUSDT", "SOLUSDT"])

    def test_feasible_count(self):
        c = Config()
        self.assertEqual(c.count, 6)
        self.assertEqual(c.hours, (0, 4, 8, 12, 16, 20))
        self.assertLess(c.principal, c.daily_total)

    def test_reject_bad_configuration(self):
        for kwargs in ({"daily_total": 1}, {"max_deviation": 1}, {"daily_total": float("nan")}, {"fee_bps": -1}, {"warmup_4h": 200}, {"max_count": 25}, {"max_deviation": .1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Config(**kwargs)


class IndicatorTests(unittest.TestCase):
    def test_rsi_flat_rise_fall(self):
        self.assertEqual(rsi([100] * 30)[-1], 50)
        self.assertEqual(rsi(list(range(1, 31)))[-1], 100)
        self.assertEqual(rsi(list(range(30, 0, -1)))[-1], 0)

    def test_ema_seed(self):
        self.assertEqual(smooth([1, 2, 3, 4], 3), [None, None, 2, 3])

    def test_prefix_invariance_no_lookahead(self):
        bars = make_bars(1100)
        full, prefix = features(bars), features(bars[:900])
        self.assertEqual(full[:900], prefix)

    def test_four_hour_only_updates_when_closed(self):
        computed = features(make_bars(900))
        self.assertEqual(computed[898].count_4h, 224)
        self.assertEqual(computed[899].count_4h, 225)

    def test_warmup_not_shortened(self):
        f = Feature(1, .5, True, 2, 800, 200)
        self.assertTrue(ready(f, "rsi", Config()))
        self.assertFalse(ready(f, "trend", Config()))

    def test_signal_examples(self):
        f = Feature(1, .5, True, 2, 4000, 1000)
        self.assertAlmostEqual(score(f, "rsi"), .85)
        self.assertAlmostEqual(score(f, "trend"), .425)
        self.assertAlmostEqual(score(f, "volatility"), .2125)
        self.assertEqual(score(f, "equal"), 0)

    def test_missing_data_resets_readiness(self):
        bars = make_bars(1200)
        del bars[1000]
        self.assertFalse(ready(features(bars)[-1], "deviation", Config()))


class AllocationTests(unittest.TestCase):
    def test_random_budget_invariant(self):
        rng = random.Random(123)
        c = Config()
        for _ in range(500):
            remaining = c.principal
            amounts = []
            for k in range(c.count, 0, -1):
                a = amount_for_slot(remaining, k, rng.uniform(-1, 1), c)
                self.assertGreaterEqual(a + 1e-8, c.lower)
                self.assertLessEqual(a, c.upper + 1e-8)
                amounts.append(a)
                remaining -= a
            self.assertAlmostEqual(sum(amounts), c.principal)

    def test_last_slot_has_no_signal_freedom(self):
        self.assertEqual(amount_for_slot(18, 1, 1, Config()), 18)
        self.assertEqual(amount_for_slot(18, 1, -1, Config()), 18)

    def test_infeasible_does_not_force_buy(self):
        self.assertIsNone(amount_for_slot(100, 1, 0, Config()))

    def test_decimal_quantity_grid(self):
        r = Rules(step="0.001")
        self.assertEqual(r.quantity(20, 300), .066)
        self.assertEqual(r.bounded_quantity(14, 300, 14, 26), .047)
        self.assertEqual(r.bounded_quantity(20, 300, 20, 20), 0)

    def test_rules_from_snapshot(self):
        symbol = {"filters": [{"filterType": "LOT_SIZE", "stepSize": ".001", "minQty": ".001", "maxQty": "1000"}, {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "100"}, {"filterType": "NOTIONAL", "minNotional": "5", "maxNotional": "5000", "applyMinToMarket": True, "applyMaxToMarket": False}]}
        r = Rules.from_symbol(symbol)
        self.assertEqual(r.min_notional, 5)
        self.assertEqual(r.max_qty, 100)
        self.assertEqual(r.step, "0.001")


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = make_bars()
        cls.computed = features(cls.bars)

    def run_sample(self, stage="equal", bars=None, rules=None, config=None):
        bars = bars or self.bars
        config = config or Config()
        days = day_groups(bars, self.computed, "volatility", config)[-5:]
        return simulate(bars, self.computed, days, stage, config, rules or Rules(step=".000001"), True)

    def test_no_overspend_and_reconcile(self):
        for stage in ("equal", "deviation", "rsi", "trend", "volatility"):
            r = self.run_sample(stage)
            self.assertLessEqual(r["spent"] + r["fees"], r["contributed"] + 1e-8)
            self.assertGreaterEqual(r["cash"], -1e-8)
            self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])
            self.assertTrue(all(o["target"] is None or 14 - 1e-8 <= o["target"] <= 26 + 1e-8 for o in r["orders"]))
            self.assertTrue(all(o["skip_reason"] or o["notional"] <= o["target"] + 1e-8 for o in r["orders"]))

    def test_rounding_dust_does_not_reject_valid_planned_order(self):
        r = self.run_sample("deviation", rules=Rules(step=".01"))
        self.assertEqual(r["skipped_slots"], 0)
        self.assertEqual(len(r["orders"]), 5 * Config().count)
        self.assertTrue(all(o["unspent_slot_budget"] >= -1e-8 for o in r["orders"]))
        self.assertTrue(all(sum(o["target"] for o in r["orders"] if o["day"] == d["date"]) <= Config().principal + 1e-8 for d in r["daily"]))

    def test_flat_market_fee_free_zero_return(self):
        bars = [Bar(i * HOUR, 100, 101, 99, 100, 1000) for i in range(5000)]
        f = features(bars)
        c = Config(fee_bps=0, slippage_bps=0)
        days = day_groups(bars, f, "volatility", c)[-3:]
        r = simulate(bars, f, days, "equal", c, Rules())
        self.assertAlmostEqual(r["profit"], 0, places=7)
        self.assertAlmostEqual(r["twr_return"], 0, places=7)
        self.assertAlmostEqual(r["max_drawdown"], 0, places=7)

    def test_costs_reduce_equity(self):
        r = self.run_sample()
        free = self.run_sample(config=Config(fee_bps=0, slippage_bps=0))
        self.assertGreater(free["equity"], r["equity"])

    def test_closed_feature_cutoff(self):
        r = self.run_sample()
        self.assertTrue(all(milliseconds(o["feature_cutoff"]) <= milliseconds(o["time"]) for o in r["orders"]))

    def test_no_zero_volume_fills(self):
        bars = [replace(b, volume=0) for b in self.bars]
        r = self.run_sample(bars=bars)
        self.assertEqual(r["quantity"], 0)
        self.assertEqual(r["cash"], r["contributed"])
        self.assertGreater(r["skipped_slots"], 0)

    def test_same_stage_comparison_zero(self):
        r = self.run_sample()
        d = comparison(r, r)
        self.assertEqual(d["mean_daily_cost_gain_bps"], 0)
        self.assertEqual(d["aggregate_cost_gain_bps"], 0)

    def test_visual_orders_preserve_execution_and_signal(self):
        current, baseline = self.run_sample("rsi"), self.run_sample("equal")
        visual, equal = visual_orders(current), visual_orders(baseline)
        self.assertEqual([o[0] for o in visual], [o[0] for o in equal])
        self.assertAlmostEqual(sum(o[4] for o in visual), current["quantity"])
        self.assertAlmostEqual(sum(o[2] + o[3] for o in visual), current["spent"] + current["fees"])
        self.assertEqual(visual[0][6], current["orders"][0]["signal"])
        self.assertEqual(visual[0][7], current["orders"][0]["skip_reason"])

    def test_curve_reconciles_final_holdings_and_cost(self):
        result = self.run_sample("rsi")
        last = result["curve"][-1]
        self.assertEqual(last["quantity"], result["quantity"])
        self.assertEqual(last["cash"], result["cash"])
        self.assertEqual(last["spent_with_fees"], result["spent"] + result["fees"])
        self.assertEqual(last["equity"], result["equity"])


class AdaptiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = make_bars()
        cls.computed = features(cls.bars)
        cls.config = Config(center=40, min_count=1, max_count=4)
        cls.days = day_groups(cls.bars, cls.computed, "rsi", cls.config)[-5:]

    def test_volatility_count_thresholds_and_windows(self):
        for q, n in ((.5, 1), (.75, 2), (.99, 2), (1, 3), (1.49, 3), (1.5, 4), (5, 4)):
            with self.subTest(q=q):
                f = replace(self.computed[-1], volatility_multiple=q)
                local, windows, ratio = daily_plan(f, self.config, "frequency")
                self.assertEqual(local.count, n)
                self.assertEqual(windows[0][0], 0)
                self.assertEqual(windows[-1][1], 24)
                self.assertEqual(ratio, q)
                self.assertEqual(local.principal, self.config.principal)
                self.assertAlmostEqual(local.center * n, local.principal)

    def adaptive(self, q=1.2, policy="sizing", hit=False, bars=None):
        computed = [replace(f, deviation=1 if hit else 0, momentum=1 if hit else 0, volatility_multiple=q) if f else None for f in self.computed]
        return simulate(bars or self.bars, computed, self.days, "rsi" if policy == "sizing" else "equal", self.config, Rules(), True, schedule=policy)

    def test_all_counts_reconcile_budget_and_dynamic_bounds(self):
        for q, n in ((.5, 1), (.9, 2), (1.2, 3), (2, 4)):
            r = self.adaptive(q)
            self.assertEqual(len(r["orders"]), 5 * n)
            self.assertTrue(all(d["planned_count"] == d["attempted_count"] == n for d in r["daily"]))
            self.assertLessEqual(r["spent"] + r["fees"], r["contributed"] + 1e-8)
            self.assertAlmostEqual(r["spent"] + r["fees"] + r["cash"], r["contributed"])
            for o in r["orders"]:
                self.assertGreaterEqual(o["target"] + 1e-8, o["planned_lower"])
                self.assertLessEqual(o["target"], o["planned_upper"] + 1e-8)

    def test_waiting_only_uses_closed_signals_and_falls_back(self):
        for q, expected in ((.5, [23]), (.9, [11, 23]), (1.2, [7, 15, 23]), (2, [5, 11, 17, 23])):
            r = self.adaptive(q)
            self.assertEqual([(milliseconds(o["time"]) // HOUR + 8) % 24 for o in r["orders"][:len(expected)]], expected)
            self.assertTrue(all(o["trigger"] == "window_deadline" for o in r["orders"]))
            self.assertTrue(all(milliseconds(o["feature_cutoff"]) <= milliseconds(o["time"]) for o in r["orders"]))

    def test_signal_hit_buys_at_window_start(self):
        r = self.adaptive(1.2, hit=True)
        self.assertEqual([(milliseconds(o["time"]) // HOUR + 8) % 24 for o in r["orders"][:3]], [0, 8, 16])
        self.assertTrue(all(o["trigger"] == "low_signal" for o in r["orders"]))

    def test_count_frozen_at_day_start(self):
        computed = [replace(f, volatility_multiple=2) if f else None for f in self.computed]
        for _, indices in self.days:
            computed[indices[0] - 1] = replace(computed[indices[0] - 1], volatility_multiple=.5)
        r = simulate(self.bars, computed, self.days, "equal", self.config, Rules(), True, schedule="frequency")
        self.assertTrue(all(o["day_count"] == 1 and o["day_volatility"] == .5 for o in r["orders"]))

    def test_fixed_baselines_have_correct_counts_and_anchors(self):
        for policy, n, anchor in (("fixed_four", 4, 0), ("fixed_once", 1, 6)):
            r = simulate(self.bars, self.computed, self.days, "equal", self.config, Rules(), True, schedule=policy, once_hour=anchor)
            self.assertEqual(len(r["orders"]), 5 * n)
            if n == 1:
                self.assertTrue(all((milliseconds(o["time"]) // HOUR + 8) % 24 == 6 for o in r["orders"]))

    def test_zero_volume_not_backfilled_or_redistributed(self):
        r = self.adaptive(.5, bars=[replace(b, volume=0) for b in self.bars])
        self.assertEqual(r["quantity"], 0)
        self.assertEqual(len(r["orders"]), 5)
        self.assertEqual(r["cash"], r["contributed"])

    def test_future_price_cannot_rewrite_earlier_orders(self):
        first = self.days[:1]
        end = first[-1][1][-1] + 1
        full = simulate(self.bars, self.computed, first, "rsi", self.config, Rules(), True, schedule="sizing")
        truncated = simulate(self.bars[:end], features(self.bars[:end]), first, "rsi", self.config, Rules(), True, schedule="sizing")
        self.assertEqual(full["orders"], truncated["orders"])

    def test_freeze_rejects_silent_parameter_change(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            first = freeze(Path(d), {"threshold": .25})
            self.assertEqual(first, freeze(Path(d), {"threshold": .25}))
            with self.assertRaises(ValueError):
                freeze(Path(d), {"threshold": .3})

    def test_meaningful_gate_requires_both_benchmarks_and_fresh_data(self):
        metric = {"paired_days": 100, "mean_daily_cost_gain_bps": 6, "aggregate_cost_gain_bps": 60,
                  "ci95_bps": [1, 8], "drawdown_delta_pp": 0, "execution_delta_pp": 0}
        period = {"vs_equal": metric, "vs_once": metric,
                  "current": {"equity": 1010, "contributed": 1000}, "baseline": {"equity": 1000}, "baseline_once": {"equity": 1000}}
        checks = {"stress": {"four": metric, "once": metric}}
        gate = meaningful_gate({"validation": period, "holdout": period}, checks)
        self.assertEqual(gate["status"], "needs_fresh")
        self.assertFalse(gate["independent_evidence"])
        failed = {**period, "vs_once": {**metric, "aggregate_cost_gain_bps": 10}}
        self.assertEqual(meaningful_gate({"validation": period, "holdout": failed}, checks)["status"], "not_adopted")

    def test_invalid_daily_volatility_or_anchor_rejected(self):
        for q in (None, float("nan"), -1, float("inf")):
            with self.subTest(q=q), self.assertRaises(ValueError):
                daily_plan(replace(self.computed[-1], volatility_multiple=q), self.config, "frequency")
        with self.assertRaises(ValueError):
            daily_plan(self.computed[-1], self.config, "fixed_once", once_hour=24)


class ReversalTests(unittest.TestCase):
    def bars(self):
        return [Bar(i * HOUR, 100, 105, 95, 100, 1000) for i in range(20)]

    def test_rsi_rebound_is_closed_and_requires_price_recovery(self):
        bars, values = self.bars(), [50.] * 20
        values[-4:] = [30, 31, 32, 35]
        bars[-1] = replace(bars[-1], close=101)
        self.assertTrue(bottom_signals(bars, values, 19)[0])
        bars[-1] = replace(bars[-1], close=99)
        self.assertFalse(bottom_signals(bars, values, 19)[0])

    def test_sweep_requires_closed_reclaim_not_just_new_low(self):
        bars, values = self.bars(), [50.] * 20
        bars[-1] = replace(bars[-1], open=97, low=94, close=100)
        self.assertTrue(bottom_signals(bars, values, 19)[1])
        bars[-1] = replace(bars[-1], close=94.5)
        self.assertFalse(bottom_signals(bars, values, 19)[1])

    def test_hammer_needs_next_closed_bar_confirmation(self):
        bars, values = self.bars(), [50.] * 20
        bars[-2] = replace(bars[-2], open=100, high=101, low=90, close=100)
        bars[-1] = replace(bars[-1], close=102)
        values[-2:] = [35, 38]
        self.assertTrue(bottom_signals(bars, values, 19)[2])
        self.assertFalse(bottom_signals(bars[:19], values[:19], 18)[2])
        bars[-1] = replace(bars[-1], close=100.5)
        self.assertFalse(bottom_signals(bars, values, 19)[2])

    def test_gap_disables_bottom_pattern(self):
        bars, values = self.bars(), [50.] * 20
        bars[-1] = replace(bars[-1], time=bars[-1].time + HOUR)
        self.assertEqual(bottom_signals(bars, values, 19), (False, False, False))

    def test_rebound_and_bottom_are_separate_ablation_rules(self):
        f = Feature(0, 0, False, 1, 1000, 1000, rsi_value=40, sweep_reclaim=True)
        self.assertFalse(entry_signal(f, "rsi_rebound")[0])
        self.assertEqual(entry_signal(f, "bottom")[:2], (True, "sweep_reclaim"))

    def test_long_mean_deviation_controls_amount_direction(self):
        f = Feature(0, 0, False, 1, 1000, 1000, long_deviation=1)
        self.assertGreater(score(f, "ma_sizing"), 0)
        self.assertLess(score(replace(f, long_deviation=-1), "ma_sizing"), 0)

    def test_soft_position_budget_respects_new_cash_and_floor(self):
        f = Feature(0, 0, False, 1, 1000, 1000, rsi_value=50)
        low, target = exposure_budget(0, 1000, 100, 120, f)
        self.assertEqual(low, 30)
        self.assertEqual(target, .8)
        budget, _ = exposure_budget(10000, 0, 100, 120, f)
        self.assertEqual(budget, 120)
        cheap = exposure_budget(0, 0, 100, 120, replace(f, long_deviation=1, rsi_value=30))[0]
        expensive = exposure_budget(0, 0, 100, 120, replace(f, long_deviation=-1, rsi_value=70))[0]
        self.assertGreater(cheap, expensive)

    def test_exposure_and_matched_funding_control_reconcile(self):
        bars = make_bars()
        computed = features(bars)
        config = Config(center=40, min_count=1, max_count=4)
        days = day_groups(bars, computed, "rsi", config)[-5:]
        cur = simulate(bars, computed, days, "ma_sizing", config, Rules(), True, schedule="exposure")
        budgets = {d["date"]: d["planned_deployment"] for d in cur["daily"]}
        base = simulate(bars, computed, days, "equal", config, Rules(), True, schedule="fixed_four", deployment_budgets=budgets)
        self.assertEqual(cur["contributed"], base["contributed"])
        self.assertEqual(budgets, {d["date"]: d["planned_deployment"] for d in base["daily"]})
        self.assertTrue(all(30 <= value <= 120 for value in budgets.values()))
        self.assertTrue(all(o["day_decision_cutoff"] <= o["feature_cutoff"] <= o["time"] for o in cur["orders"]))
        self.assertLess(cur["spent"] + cur["fees"], cur["contributed"])
        self.assertAlmostEqual(cur["cash"] + cur["spent"] + cur["fees"], cur["contributed"])
        self.assertTrue(all(0 <= d["position_fraction"] <= 1 for d in cur["curve"]))
        self.assertEqual(cur["quantity"], cur["curve"][-1]["quantity"])


class PortfolioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = make_bars(1200)
        cls.computed = features(cls.bars)
        cls.config = Config(daily_total=30, center=30 / 3 / 1.001, min_count=1, max_count=4)
        cls.weights = {k: 1 / 3 for k in KEYS}
        cls.days = [(d, [cls.bars[i].time for i in idx]) for d, idx in day_groups(cls.bars, cls.computed, "rsi", cls.config)[-5:]]
        cls.market = {k: {b.time: (b, f) for b, f in zip(cls.bars, cls.computed)} for k in KEYS}
        for m in cls.market.values():
            m["rules"] = Rules(step=".001", min_notional=5)
        cls.funding = {d: (21., 30., 39., 25., 35.)[i] for i, (d, ts) in enumerate(cls.days)}

    def test_funding_center_and_bounds(self):
        f = Feature(0, 0, False, 1, 800, 0)
        self.assertEqual(daily_funding(f), 30)
        self.assertEqual(daily_funding(replace(f, long_deviation=1, momentum=1)), 39)
        self.assertEqual(daily_funding(replace(f, long_deviation=-1, momentum=-1)), 21)
        for value in (-.1, 1, float("nan")):
            with self.assertRaises(ValueError):
                daily_funding(f, deviation=value)

    def test_one_shared_ledger_not_three_daily_contributions(self):
        for policy in ("four", "frequency", "rsi", "bottom", "ma", "position"):
            r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, policy)
            self.assertEqual(r["contributed"], sum(self.funding.values()))
            self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])
            self.assertTrue(all(1 <= d["count"] <= 4 and d["fills"] <= d["count"] for d in r["daily"]))
            self.assertTrue(all(d["spent"] <= d["deployment"] + 1e-8 <= d["contribution"] + 1e-8 for d in r["daily"]))
            self.assertAlmostEqual(sum(a["spent"] for a in r["assets"].values()), r["spent"])
            self.assertAlmostEqual(sum(a["market_value"] for a in r["assets"].values()) + r["cash"], r["equity"])
            self.assertNotIn("unit_cost", r)

    def test_four_attempts_are_global_not_per_asset(self):
        r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four")
        self.assertLessEqual(len(r["orders"]), len(self.days) * 4)
        for day, _ in self.days:
            self.assertLessEqual(sum(o["day"] == day for o in r["orders"]), 4)

    def test_minimum_trade_guard_reduces_count(self):
        local, windows, q = daily_plan(replace(self.computed[-1], volatility_multiple=2), replace(self.config, daily_total=21, center=7), "ma_sizing", minimum_order=5.5)
        self.assertLess(local.count, 4)
        self.assertGreaterEqual(local.principal / local.count * (1 - local.max_deviation), 5.5)

    def test_matched_funding_and_unmatched_reference_are_distinguished(self):
        c = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "rsi")
        b = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four")
        fixed = portfolio_simulate(self.market, self.days, {d: 30. for d, _ in self.days}, self.config, self.weights, "four")
        self.assertTrue(compare_portfolios(b, c)["same_funding"])
        self.assertFalse(compare_portfolios(fixed, c)["same_funding"])
        self.assertIsNone(compare_portfolios(fixed, c)["equity_gain_pct_of_contributions"])

    def test_buy_only_deficit_selection_uses_known_prices(self):
        self.assertEqual(choose_asset({k: 0 for k in KEYS}, {k: 100 for k in KEYS}, 30, self.weights), "GOOG")
        self.assertEqual(choose_asset({"GOOG": 1, "XAU": 1, "SOL": 0}, {k: 100 for k in KEYS}, 300, self.weights), "SOL")

    def test_daily_nav_returns_include_overnight_marks(self):
        r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "rsi")
        self.assertAlmostEqual(math.prod(1 + d["return"] for d in r["daily"]), 1 + r["twr_return"])

    def test_matched_budget_reconciles_cash_and_avoids_future_funding(self):
        c = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "position")
        budgets = {d["date"]: d["deployment"] for d in c["daily"]}
        b = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four", budgets=budgets)
        self.assertEqual([d["deployment"] for d in c["daily"]], [d["deployment"] for d in b["daily"]])
        self.assertEqual(c["contributed"], b["contributed"])
        self.assertTrue(all(o["decision_cutoff"] == o["time"] for o in c["orders"]))


class OpportunityTests(unittest.TestCase):
    setUpClass = classmethod(PortfolioTests.setUpClass.__func__)

    def quiet_market(self):
        return {k: {t: (entry[0], replace(entry[1], volatility_multiple=2., rsi_value=50., rsi_rebound=False,
                                         sweep_reclaim=False, hammer_confirmed=False, ema72=100., ema168=100., long_deviation=0.))
                        if isinstance(t, int) and entry[1] is not None else entry for t, entry in m.items()}
                for k, m in self.market.items()}

    def test_no_signal_means_one_base_buy_not_deadline_buys(self):
        for policy in CASH_POLICIES:
            r = portfolio_simulate(self.quiet_market(), self.days, self.funding, self.config, self.weights, policy)
            filled = [o for o in r["orders"] if not o["skip_reason"]]
            self.assertEqual(len(filled), 0 if policy in PURE_POLICIES else len(self.days))
            self.assertTrue(all(o["trigger"] == "core_daily" for o in filled))
            self.assertTrue(any(o["skip_reason"] == "no_opportunity" for o in r["orders"]))
            self.assertNotIn("window_deadline", [o["trigger"] for o in r["orders"]])
            self.assertGreater(r["cash"], .35 * r["contributed"])
            self.assertEqual(r["skipped_slots"], 0)

    def test_all_new_policies_reconcile_global_limits_and_cooldown(self):
        for policy in CASH_POLICIES:
            r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, policy)
            self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])
            self.assertEqual(r["contributed"], sum(self.funding.values()))
            self.assertTrue(all(1 <= d["count"] <= 4 and (0 if policy in PURE_POLICIES else 1) <= d["fills"] <= d["count"] for d in r["daily"]))
            self.assertTrue(all(0 <= d["spent"] <= d["deployment"] + 1e-8 for d in r["daily"]))
            filled = [o for o in r["orders"] if not o["skip_reason"]]
            self.assertTrue(all(o["cash_after"] >= -1e-8 for o in r["orders"]))
            self.assertTrue(all(b["trigger"] == "core_daily" or milliseconds(b["time"]) - milliseconds(a["time"]) >= 4 * HOUR for a, b in zip(filled, filled[1:])))

    def test_trend_entry_is_a_separate_causal_ablation(self):
        f = Feature(0, 0, False, 1, 1000, 1000, rsi_value=50, ema72=101, ema168=100, long_deviation=.1)
        old = replace(f, rsi_value=49)
        self.assertFalse(opportunity_signal(f, old, 100, 99, "core")[0])
        self.assertEqual(opportunity_signal(f, old, 100, 99, "core_trend")[:2], (True, "trend_pullback"))
        self.assertFalse(opportunity_signal(replace(f, long_deviation=-.1), old, 100, 99, "core_trend")[0])
        self.assertFalse(opportunity_signal(f, old, 98, 99, "core_trend")[0])

    def test_prefix_orders_do_not_depend_on_future_days(self):
        for policy in CASH_POLICIES:
            full = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, policy)
            short = portfolio_simulate(self.market, self.days[:2], self.funding, self.config, self.weights, policy)
            self.assertEqual(short["orders"], [o for o in full["orders"] if o["day"] <= self.days[1][0]])

    def test_future_feature_mutation_does_not_change_prior_orders(self):
        cutoff = self.days[2][1][0]
        changed = {k: {t: (replace(v[0], open=v[0].open * 3), replace(v[1], long_deviation=-1, rsi_rebound=True))
                          if isinstance(t, int) and t >= cutoff and v[1] else v for t, v in m.items()}
                   for k, m in self.market.items()}
        for policy in CASH_POLICIES:
            original = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, policy)
            altered = portfolio_simulate(changed, self.days, self.funding, self.config, self.weights, policy)
            self.assertEqual([o for o in original["orders"] if milliseconds(o["time"]) < cutoff],
                             [o for o in altered["orders"] if milliseconds(o["time"]) < cutoff])

    def test_zero_matched_deployment_does_not_invent_fills(self):
        r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four", budgets={d: 0 for d, _ in self.days})
        self.assertEqual(r["spent"], 0)
        self.assertEqual(r["cash"], r["contributed"])
        self.assertTrue(all(d["fills"] == 0 for d in r["daily"]))
        self.assertEqual(r["skipped_slots"], 0)

    def test_matched_actual_spend_can_reuse_previously_contributed_cash(self):
        budgets = {d: (10. if i < 4 else 50.) for i, (d, _) in enumerate(self.days)}
        r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four", budgets=budgets)
        self.assertGreater(r["daily"][-1]["spent"], r["daily"][-1]["contribution"])
        self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])
        with self.assertRaises(ValueError):
            portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "four", budgets={d: 1000 for d, _ in self.days})

    def test_trigger_diagnostic_counts_and_segment_cutoff(self):
        r = portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "rsi")
        diagnostic = trigger_diagnostics(r, self.market)
        self.assertEqual(sum(d["attempts"] for d in diagnostic.values()), len(r["orders"]))
        self.assertAlmostEqual(sum(d["spent"] for d in diagnostic.values()), r["spent"] + r["fees"])
        single = portfolio_simulate(self.market, self.days[:1], self.funding, self.config, self.weights, "rsi")
        self.assertTrue(all(d["forward_24h_pct"] is None for d in trigger_diagnostics(single, self.market).values()))

    def test_reject_unknown_policy(self):
        with self.assertRaises(ValueError):
            portfolio_simulate(self.market, self.days, self.funding, self.config, self.weights, "unknown")


class DiscountTests(unittest.TestCase):
    setUpClass = classmethod(PortfolioTests.setUpClass.__func__)

    def prepared_market(self, open_price):
        return {k: {t: (replace(v[0], open=open_price, close=98., low=1., high=200.),
                         replace(v[1], rsi_rebound=True, sweep_reclaim=False, hammer_confirmed=False,
                                 volatility_multiple=2., ema168=100., atr14=2., long_deviation=.5))
                        if isinstance(t, int) and v[1] is not None else v for t, v in m.items()}
                for k, m in self.market.items()}

    def test_discount_ceiling_boundary_and_missing_invalid_inputs(self):
        f = Feature(0, 0, False, 1, 1000, 1000, rsi_rebound=True, rsi_value=40, ema168=100, atr14=2)
        self.assertEqual(discount_ceiling(f), 99)
        self.assertTrue(opportunity_signal(f, f, 99, 98, "core_discount")[0])
        self.assertFalse(opportunity_signal(f, f, 99.000001, 98, "core_discount")[0])
        for invalid in (None, 0, -1, float("nan"), float("inf")):
            bad = replace(f, atr14=invalid)
            self.assertIsNone(discount_ceiling(bad))
            self.assertFalse(opportunity_signal(bad, f, 98, 97, "core_discount")[0])
        self.assertIsNone(discount_ceiling(None))
        self.assertIsNone(discount_ceiling(replace(f, ema168=None)))

    def test_discount_does_not_add_trend_signals(self):
        f = Feature(0, 0, False, 1, 1000, 1000, rsi_value=50, ema72=101, ema168=100, atr14=2)
        old = replace(f, rsi_value=49)
        self.assertTrue(opportunity_signal(f, old, 98, 97, "core_trend")[0])
        self.assertFalse(opportunity_signal(f, old, 98, 97, "core_discount")[0])

    def test_gap_above_limit_never_fills_even_when_hour_low_touches(self):
        r = portfolio_simulate(self.prepared_market(100), self.days, self.funding, self.config, self.weights, "core_discount")
        filled = [o for o in r["orders"] if not o["skip_reason"]]
        self.assertEqual(len(filled), len(self.days))
        self.assertTrue(all(o["trigger"] == "core_daily" and o["price_cap"] is None for o in filled))
        self.assertGreater(sum(d["discount_gate"]["quote_rejections"] for d in r["daily"]), 0)
        self.assertEqual(sum(d["discount_gate"]["discount_fills"] for d in r["daily"]), 0)

    def test_open_quote_below_limit_fills_with_slippage_and_fee(self):
        r = portfolio_simulate(self.prepared_market(98), self.days, self.funding, self.config, self.weights, "core_discount")
        opportunities = [o for o in r["orders"] if not o["skip_reason"] and o["trigger"] != "core_daily"]
        self.assertTrue(opportunities)
        self.assertTrue(all(o["price"] <= o["price_cap"] == 99 for o in opportunities))
        self.assertTrue(all(abs(o["price"] - 98 * 1.0005) < 1e-9 for o in opportunities))
        self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])

    def test_gap_rejection_waits_for_later_eligible_quote(self):
        m = self.prepared_market(98)
        rejected = self.days[0][1][6]
        for k in KEYS:
            b, f = m[k][rejected]
            m[k][rejected] = replace(b, open=100), f
        r = portfolio_simulate(m, self.days[:1], self.funding, self.config, self.weights, "core_discount")
        opportunities = [o for o in r["orders"] if not o["skip_reason"] and o["trigger"] != "core_daily"]
        self.assertTrue(opportunities)
        self.assertEqual(milliseconds(opportunities[0]["time"]), rejected + HOUR)
        self.assertEqual(r["daily"][0]["discount_gate"]["quote_rejections"], 1)

    def test_current_hour_extremes_do_not_influence_execution(self):
        m = self.prepared_market(98)
        changed = {k: {t: (replace(v[0], low=.001, high=1e6), v[1]) if isinstance(t, int) else v for t, v in x.items()} for k, x in m.items()}
        original = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, "core_discount")
        altered = portfolio_simulate(changed, self.days, self.funding, self.config, self.weights, "core_discount")
        self.assertEqual(original["orders"], altered["orders"])

    def test_pure_opportunity_never_buys_without_discount_even_at_midnight(self):
        r = portfolio_simulate(self.prepared_market(100), self.days, self.funding, self.config, self.weights, "discount_only")
        self.assertEqual(r["spent"], 0)
        self.assertEqual(r["cash"], r["contributed"])
        self.assertTrue(all(d["fills"] == 0 for d in r["daily"]))
        self.assertNotIn("core_daily", [o["trigger"] for o in r["orders"]])

    def test_pure_midnight_buy_requires_real_signal_and_cap_and_cooldown(self):
        r = portfolio_simulate(self.prepared_market(98), self.days, self.funding, self.config, self.weights, "discount_only")
        filled = [o for o in r["orders"] if not o["skip_reason"]]
        self.assertTrue(filled)
        self.assertEqual(milliseconds(filled[0]["time"]), self.days[0][1][0])
        self.assertTrue(all(o["trigger"] == "rsi_rebound" and o["price"] <= o["price_cap"] == 99 for o in filled))
        self.assertTrue(all(milliseconds(b["time"]) - milliseconds(a["time"]) >= 4 * HOUR for a, b in zip(filled, filled[1:])))
        self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"])

    def test_cash_does_not_expire_into_forced_buys_after_many_days(self):
        bars = make_bars(2400)
        values = features(bars)
        days = [(d, [bars[i].time for i in idx]) for d, idx in day_groups(bars, values, "rsi", self.config)[-30:]]
        market = {k: {b.time: (b, replace(f, volatility_multiple=2, rsi_rebound=False, sweep_reclaim=False, hammer_confirmed=False))
                      if f is not None else (b, f) for b, f in zip(bars, values)} for k in KEYS}
        for m in market.values():
            m["rules"] = Rules(step=".001", min_notional=5)
        self.assertGreater(len(days), 7)
        r = portfolio_simulate(market, days, {d: 30. for d, _ in days}, self.config, self.weights, "discount_only")
        self.assertEqual(r["spent"], 0)
        self.assertEqual(r["cash"], 30 * len(days))
        self.assertTrue(all(d["deployment"] <= 60 for d in r["daily"]))


class SignalUpgradeTests(unittest.TestCase):
    setUpClass = classmethod(PortfolioTests.setUpClass.__func__)
    prepared_market = DiscountTests.prepared_market

    def sol_only_market(self):
        m = self.prepared_market(98)
        for k in ("GOOG", "XAU"):
            for t, v in list(m[k].items()):
                if isinstance(t, int) and v[1] is not None:
                    m[k][t] = v[0], replace(v[1], rsi_rebound=False)
        return m

    def test_scan_does_not_wait_for_ineligible_locked_asset(self):
        m = self.sol_only_market()
        old = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, "discount_only")
        new = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, "signal_scan")
        self.assertEqual(old["spent"], 0)
        filled = [o for o in new["orders"] if not o["skip_reason"]]
        self.assertTrue(filled)
        self.assertTrue(all(o["asset"] == "SOL" and o["candidate_assets"] == ["SOL"] for o in filled))
        self.assertNotIn("core_daily", [o["trigger"] for o in new["orders"]])

    def test_scan_prefers_deficit_then_stable_order(self):
        r = portfolio_simulate(self.prepared_market(98), self.days, self.funding, self.config, self.weights, "signal_scan")
        filled = [o for o in r["orders"] if not o["skip_reason"]]
        self.assertEqual([o["asset"] for o in filled[:2]], ["GOOG", "XAU"])

    def test_gap_on_one_asset_does_not_block_other_assets(self):
        m = self.prepared_market(98)
        for t, v in list(m["GOOG"].items()):
            if isinstance(t, int):
                m["GOOG"][t] = replace(v[0], open=100), v[1]
        r = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, "signal_scan")
        filled = [o for o in r["orders"] if not o["skip_reason"]]
        self.assertTrue(filled)
        self.assertEqual(filled[0]["asset"], "XAU")
        self.assertTrue(all("GOOG" not in o["candidate_assets"] for o in filled))

    def test_reserve_amount_is_bounded_and_discount_dependent(self):
        f = Feature(0, 0, False, 1, 1000, 1000, long_deviation=.5)
        self.assertEqual(signal_reserve_amount(30, 100, f, "signal_reserve"), 5)
        self.assertEqual(signal_reserve_amount(30, 10000, replace(f, long_deviation=1), "signal_reserve"), 18)
        self.assertEqual(signal_reserve_amount(30, 0, f, "signal_reserve"), 0)
        self.assertEqual(signal_reserve_amount(30, -1e-9, f, "signal_reserve"), 0)
        self.assertEqual(signal_reserve_amount(30, 100, f, "signal_scan"), 0)
        self.assertEqual(signal_reserve_amount(30, 100, replace(f, long_deviation=-1), "signal_reserve"), 0)

    def test_ladder_boundary_and_expiry(self):
        f = Feature(0, 0, False, 1, 1000, 1000, ema168=100, atr14=2)
        self.assertEqual(signal_price_cap(f, (0, 98), 23 * HOUR, "signal_ladder"), 97)
        self.assertEqual(signal_price_cap(f, (0, 98), 24 * HOUR, "signal_ladder"), 99)
        self.assertEqual(signal_price_cap(f, (0, 98), 23 * HOUR, "signal_reserve"), 99)
        self.assertIsNone(signal_price_cap(f, (0, .5), HOUR, "signal_ladder"))

    def test_ladder_requires_lower_successive_same_asset_quotes(self):
        m = self.sol_only_market()
        day, times = self.days[0]
        for hour, t in enumerate(times):
            b, f = m["SOL"][t]
            m["SOL"][t] = replace(b, open=98 - 2 * min(hour // 6, 2)), f
        weights = {"GOOG": .01, "XAU": .01, "SOL": .98}
        r = portfolio_simulate(m, self.days[:1], {day: 39.}, self.config, weights, "signal_ladder")
        filled = [o for o in r["orders"] if not o["skip_reason"]]
        self.assertGreaterEqual(len(filled), 2)
        self.assertTrue(all(o["price"] <= o["price_cap"] for o in filled))
        self.assertTrue(all(b["price"] <= a["price"] - .5 * b["feature_atr14"] for a, b in zip(filled, filled[1:])))

    def test_soft_weight_guard_is_checked_using_only_known_prices(self):
        m = self.prepared_market(98)
        for policy in SIGNAL_UPGRADES:
            r = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, policy)
            units, cash, day = {k: 0. for k in KEYS}, 0., None
            for o in r["orders"]:
                if o["day"] != day:
                    day = o["day"]
                    cash += self.funding[day]
                if o["skip_reason"]:
                    continue
                t = milliseconds(o["time"])
                prices = {k: m[k][t - HOUR][0].close for k in KEYS}
                equity = cash + sum(units[k] * prices[k] for k in KEYS)
                self.assertLessEqual(units[o["asset"]] * prices[o["asset"]] / equity, self.weights[o["asset"]] + .15)
                self.assertIn(o["asset"], o["candidate_assets"])
                units[o["asset"]] += o["quantity"]
                cash -= o["notional"] + o["fee"]

    def test_intrabar_extremes_never_select_a_better_fill(self):
        m = self.prepared_market(98)
        changed = {k: {t: (replace(v[0], low=.001, high=1e6), v[1]) if isinstance(t, int) else v for t, v in x.items()} for k, x in m.items()}
        for policy in SIGNAL_UPGRADES:
            original = portfolio_simulate(m, self.days, self.funding, self.config, self.weights, policy)
            altered = portfolio_simulate(changed, self.days, self.funding, self.config, self.weights, policy)
            self.assertEqual(original["orders"], altered["orders"])

    def test_future_hour_volume_is_not_used_to_pick_an_asset(self):
        m = self.prepared_market(98)
        t = self.days[0][1][0]
        b, f = m["GOOG"][t]
        m["GOOG"][t] = replace(b, volume=0), f
        r = portfolio_simulate(m, self.days[:1], self.funding, self.config, self.weights, "signal_scan")
        self.assertEqual(r["orders"][0]["asset"], "GOOG")
        self.assertEqual(r["orders"][0]["skip_reason"], "no_historical_trades")


class GeneratedReportTests(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).parents[1] / "reports/signal-v2-001/report.json").exists(), "Generate signal-v2 report first")
    def test_signal_upgrades_keep_no_base_and_all_prior_results(self):
        root = Path(__file__).parents[1]
        report = json.loads((root / "reports/signal-v2-001/report.json").read_text(encoding="utf-8"))
        old = json.loads((root / "reports/signal-only-001/report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["policies"]), 14)
        self.assertEqual(report["default_policy"], "signal_reserve")
        for name, section in report["results"].items():
            for key in old["policies"]:
                for field in ("spent", "fees", "cash", "equity", "max_drawdown"):
                    self.assertAlmostEqual(section[key]["current"][field], old["results"][name][key]["current"][field], places=8)
            for key in SIGNAL_UPGRADES:
                r = section[key]["current"]
                self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"], places=6)
                self.assertTrue(all(0 <= d["fills"] <= d["count"] <= 4 for d in r["daily"]))
                self.assertTrue(all(d["spent"] <= d["deployment"] + 1e-8 <= 2 * d["contribution"] + 1e-8 for d in r["daily"]))
                last = {}
                filled = [o for o in r["orders"] if not o["skip_reason"]]
                for o in filled:
                    self.assertNotEqual(o["trigger"], "core_daily")
                    self.assertIn(o["asset"], o["candidate_assets"])
                    self.assertLessEqual(o["price"], o["price_cap"])
                    t, prior = milliseconds(o["time"]), last.get(o["asset"])
                    if key == "signal_ladder" and prior and t - prior[0] < 24 * HOUR:
                        self.assertLessEqual(o["price"], prior[1] - .5 * o["feature_atr14"])
                    last[o["asset"]] = t, o["price"]
                self.assertTrue(all(milliseconds(b["time"]) - milliseconds(a["time"]) >= 4 * HOUR for a, b in zip(filled, filled[1:])))
                self.assertFalse(report["decisions"][key]["live"])

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/signal-only-001/report.json").exists(), "Generate pure opportunity report first")
    def test_pure_opportunity_report_has_no_base_fills_and_keeps_prior_controls(self):
        root = Path(__file__).parents[1]
        report = json.loads((root / "reports/signal-only-001/report.json").read_text(encoding="utf-8"))
        old = json.loads((root / "reports/discount-001/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["default_policy"], "discount_only")
        self.assertEqual(len(report["policies"]), 11)
        for name, section in report["results"].items():
            p = section["discount_only"]
            self.assertEqual(p["previous_policy"], "core_discount")
            r = p["current"]
            self.assertTrue(all(0 <= d["fills"] <= d["count"] <= 4 for d in r["daily"]))
            self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"], places=6)
            self.assertTrue(all(o["trigger"] != "core_daily" for o in r["orders"]))
            filled = [o for o in r["orders"] if not o["skip_reason"]]
            self.assertTrue(all(o["price_cap"] is not None and o["price"] <= o["price_cap"] for o in filled))
            self.assertTrue(all(milliseconds(b["time"]) - milliseconds(a["time"]) >= 4 * HOUR for a, b in zip(filled, filled[1:])))
            for key in old["policies"]:
                self.assertAlmostEqual(section[key]["current"]["equity"], old["results"][name][key]["current"]["equity"], places=8)
            self.assertFalse(report["decisions"]["discount_only"]["live"])

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/discount-001/report.json").exists(), "Generate discount report first")
    def test_discount_ablation_rules_and_legacy_regression(self):
        root = Path(__file__).parents[1]
        report = json.loads((root / "reports/discount-001/report.json").read_text(encoding="utf-8"))
        old = json.loads((root / "reports/opportunity-002/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["family"], "discount")
        self.assertEqual(len(report["policies"]), 10)
        self.assertEqual(report["default_policy"], "core_discount")
        for name, section in report["results"].items():
            p = section["core_discount"]
            self.assertEqual(p["previous_policy"], "core")
            self.assertTrue(p["vs_previous"]["same_funding"])
            r = p["current"]
            self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"], places=6)
            self.assertTrue(all(1 <= d["fills"] <= d["count"] <= 4 for d in r["daily"]))
            self.assertTrue(all(o["price_cap"] is None or o["skip_reason"] or o["price"] <= o["price_cap"] for o in r["orders"]))
            for k in old["policies"]:
                for field in ("spent", "fees", "cash", "equity", "max_drawdown", "twr_return"):
                    self.assertAlmostEqual(section[k]["current"][field], old["results"][name][k]["current"][field], places=8)
        self.assertTrue(all(not d["live"] for d in report["decisions"].values()))

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/opportunity-002/report.json").exists(), "Generate opportunity report first")
    def test_opportunity_report_retains_all_experiments_and_reconciles(self):
        report = json.loads((Path(__file__).parents[1] / "reports/opportunity-002/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["family"], "opportunity")
        self.assertEqual(report["default_policy"], "core")
        self.assertEqual(len(report["policies"]), 9)
        self.assertTrue(all(not d["live"] for d in report["decisions"].values()))
        for section in report["results"].values():
            for policy in OPPORTUNITY_POLICIES:
                p = section[policy]
                self.assertTrue(p["matched_is_ex_post"])
                for key in ("current", "four", "once", "matched"):
                    r = p[key]
                    self.assertAlmostEqual(r["cash"] + r["spent"] + r["fees"], r["contributed"], places=6)
                    self.assertTrue(all(1 <= d["count"] <= 4 and d["fills"] <= 4 for d in r["daily"]))
                self.assertEqual(p["current"]["contributed"], p["four"]["contributed"])
                self.assertEqual(p["current"]["contributed"], p["matched"]["contributed"])

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/portfolio-001/report.json").exists(), "Generate portfolio report first")
    def test_generated_shared_portfolio_report(self):
        report = json.loads((Path(__file__).parents[1] / "reports/portfolio-001/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["config"]["daily_center"], 30)
        self.assertEqual(report["scope"], "portfolio")
        for period in report["results"].values():
            for candidate in period.values():
                flows = [d["contribution"] for d in candidate["current"]["daily"]]
                for key in ("current", "four", "once", "matched"):
                    r = candidate[key]
                    self.assertEqual([d["contribution"] for d in r["daily"]], flows)
                    self.assertAlmostEqual(r["contributed"], sum(flows))
                    self.assertAlmostEqual(r["contributed"], r["cash"] + r["spent"] + r["fees"], places=6)
                    self.assertTrue(all(21 <= d["contribution"] <= 39 and 1 <= d["count"] <= 4 for d in r["daily"]))
                    self.assertTrue(all(d["spent"] <= d["deployment"] + 1e-8 <= d["contribution"] + 1e-8 for d in r["daily"]))
                    self.assertLessEqual(len(r["orders"]), r["days"] * 4)
                    self.assertNotIn("unit_cost", r)
                self.assertTrue(candidate["vs_four"]["same_funding"])

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/reversal-001/report.json").exists(), "Generate reversal report first")
    def test_reversal_report_controls_cash_and_orders_reconcile(self):
        root = Path(__file__).parents[1]
        report = json.loads((root / "reports/reversal-001/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["chart_engine"], "tradingview-lightweight-charts-5.2.1")
        for asset in report["assets"].values():
            self.assertEqual(len(asset["edges"]), 4)
            self.assertTrue(all(0 <= row[3] <= 100 for row in asset["indicators"]))
            for edge in asset["edges"]:
                self.assertFalse(edge["decision"]["independent_evidence"])
                for period in edge["periods"].values():
                    self.assertEqual(sum(period["trigger_distribution"].values()), len(period["visual_orders"]["current"]))
                    for result_name, order_name in (("current", "current"), ("baseline", "baseline"), ("baseline_once", "baseline_once"), ("matched_baseline", "matched")):
                        r, orders = period[result_name], period["visual_orders"][order_name]
                        self.assertEqual(r["contributed"], period["current"]["contributed"])
                        self.assertAlmostEqual(sum(o[4] for o in orders), r["quantity"], places=7)
                        self.assertAlmostEqual(sum(o[2] + o[3] for o in orders), r["spent"] + r["fees"], places=6)
                        self.assertAlmostEqual(r["contributed"], r["cash"] + r["spent"] + r["fees"], places=6)
                        self.assertTrue(all(0 <= p["position_fraction"] <= 1 for p in r["curve"]))
                    if edge["current"] == "exposure":
                        self.assertTrue(all(30 <= o[14] <= 120 and .6 <= o[15] <= .95 for o in period["visual_orders"]["current"]))

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/latest/report.json").exists(), "Generate real-data report first")
    def test_real_report_visual_comparisons_reconcile(self):
        path = Path(__file__).parents[1] / "reports/latest/report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        periods = 0
        for asset in report["assets"].values():
            self.assertTrue(all(len(b) == 6 for b in asset["candles"]))
            self.assertEqual(len({b[0] for b in asset["candles"]}), len(asset["candles"]))
            for edge in asset["edges"]:
                for period in edge["periods"].values():
                    current = period["visual_orders"]["current"]
                    baseline = period["visual_orders"]["baseline"]
                    self.assertEqual([o[0] for o in current], [o[0] for o in baseline])
                    for name, orders in (("current", current), ("baseline", baseline)):
                        result = period[name]
                        self.assertAlmostEqual(sum(o[4] for o in orders), result["quantity"], places=7)
                        self.assertAlmostEqual(sum(o[2] + o[3] for o in orders), result["spent"] + result["fees"], places=6)
                        self.assertEqual(result["curve"][-1]["quantity"], result["quantity"])
                    self.assertEqual(period["current"]["contributed"], period["baseline"]["contributed"])
                    periods += 1
        self.assertGreater(periods, 0)

    @unittest.skipUnless((Path(__file__).parents[1] / "reports/adaptive-001/report.json").exists(), "Generate adaptive report first")
    def test_adaptive_report_and_benchmarks_reconcile(self):
        report = json.loads((Path(__file__).parents[1] / "reports/adaptive-001/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["config"]["count_mode"], "adaptive_1_4")
        for a in report["assets"].values():
            self.assertEqual(len(a["edges"]), 3)
            for e in a["edges"]:
                self.assertFalse(e["decision"]["independent_evidence"])
                for p in e["periods"].values():
                    self.assertEqual(sum(p["count_distribution"].values()), p["current"]["days"])
                    self.assertTrue(set(p["count_distribution"]).issubset({"1", "2", "3", "4"}))
                    for name in ("current", "baseline", "baseline_once"):
                        result, orders = p[name], p["visual_orders"][name]
                        self.assertEqual(result["contributed"], p["current"]["contributed"])
                        self.assertAlmostEqual(sum(o[4] for o in orders), result["quantity"], places=7)
                        self.assertAlmostEqual(sum(o[2]+o[3] for o in orders), result["spent"]+result["fees"], places=6)


class ValidationTests(unittest.TestCase):
    def test_invalid_ohlc_duplicates_nan(self):
        b = Bar(0, 100, 101, 99, 100, 10)
        for bars in ([b, b], [replace(b, close=200)], [replace(b, close=float("nan"))], [replace(b, time=1)]):
            with self.subTest(bars=bars), self.assertRaises(ValueError):
                validate(bars)

    def test_gaps_are_reported(self):
        bars = make_bars(4)
        del bars[1]
        self.assertEqual(validate(bars)["missing_hours"], 1)

    def test_timestamp_requires_offset(self):
        with self.assertRaises(ValueError):
            milliseconds("2026-10-30T00:00:00")

    def test_write_endpoints_are_unavailable(self):
        with self.assertRaises(ValueError):
            get_json(None, "/api/v3/order")

    def test_bootstrap_deterministic(self):
        values = list(range(-10, 10))
        self.assertEqual(confidence_interval(values), confidence_interval(values))
        self.assertEqual(confidence_interval([2] * 20), [2, 2])
        self.assertIsNone(confidence_interval([2] * 3))

    def test_gate_rejects_short_period(self):
        metric = {"comparison": {"paired_days": 15}}
        self.assertEqual(evidence_gate({"validation": metric, "holdout": metric}, {})["status"], "insufficient")

    def test_report_escapes_script_payload(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            path = render_report({"malicious": "</script><script>alert(1)</script>"}, d)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn('"malicious": "</script>', text)
            self.assertNotIn("__REPORT_PAYLOAD__", text)

    def test_report_tokens_inside_payload_are_not_replaced_by_scripts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            text = render_report({"literal": "__CHART_LIBRARY__ __CHART_ADAPTER__"}, d).read_text(encoding="utf-8")
            self.assertIn('"literal":"__CHART_LIBRARY__ __CHART_ADAPTER__"', text)


if __name__ == "__main__":
    unittest.main()
