import unittest
from datetime import datetime, timedelta, timezone

from dca_lab.engine import Rules
from dca_lab.ma_tiers import (Plan, common_days, multiplier, simulate, tier)

ZONE = timezone(timedelta(hours=8))
HOUR = 3_600_000
RULE = Rules(step="0.001", min_qty=0.0, min_notional=5.0)


def make_bars(prices):
    start = int(datetime(2026, 8, 1, tzinfo=ZONE).timestamp() * 1000)
    from dca_lab.market import Bar
    return [Bar(start + i * HOUR, price, price * 1.001, price * 0.999, price, 1000.0)
            for i, price in enumerate(prices)]


def flat_then(floor=100.0, drop_to=80.0, days=40):
    prices = [floor] * (days * 24)
    prices[-1] = drop_to
    # the last closed hour of an eligible day is followed by a buy day
    prices += [drop_to] * 24
    return prices


class MultiplierTest(unittest.TestCase):
    def test_always_mode_keeps_buying_above_average(self):
        self.assertEqual(multiplier("always", 0.0, (0.01, 0.03, 0.06)), 1.0)
        self.assertEqual(multiplier("always", -0.02, (0.01, 0.03, 0.06)), 1.0)
        self.assertEqual(multiplier("always", 0.005, (0.01, 0.03, 0.06)), 1.5)
        self.assertEqual(multiplier("always", 0.02, (0.01, 0.03, 0.06)), 2.0)
        self.assertEqual(multiplier("always", 0.05, (0.01, 0.03, 0.06)), 3.0)
        self.assertEqual(multiplier("always", 0.09, (0.01, 0.03, 0.06)), 5.0)

    def test_dips_mode_skips_above_average(self):
        self.assertEqual(multiplier("dips", 0.0, (0.01, 0.03, 0.06)), 0.0)
        self.assertEqual(multiplier("dips", 0.005, (0.01, 0.03, 0.06)), 1.0)
        self.assertEqual(multiplier("dips", 0.02, (0.01, 0.03, 0.06)), 2.0)
        self.assertEqual(multiplier("dips", 0.05, (0.01, 0.03, 0.06)), 3.0)
        self.assertEqual(multiplier("dips", 0.09, (0.01, 0.03, 0.06)), 5.0)

    def test_tier_matches_multiplier(self):
        self.assertEqual(tier(0.0, (0.01, 0.03, 0.06)), 0)
        self.assertEqual(tier(0.02, (0.01, 0.03, 0.06)), 2)
        self.assertEqual(tier(0.09, (0.01, 0.03, 0.06)), 4)


class SimulateTest(unittest.TestCase):
    def test_dips_mode_skips_flat_days_and_buys_the_drop(self):
        bars = make_bars(flat_then())
        days = common_days({"A": bars})
        self.assertGreater(len(days), 0)
        plan = Plan("dips", 4, (0.01, 0.03, 0.06))
        result = simulate({"A": bars}, days, {"A": 6.0}, {"A": plan}, {"A": RULE}, "tier")
        orders = result["orders"]
        self.assertTrue(any(order["skip_reason"] == "above_average" for order in orders))
        fills = [order for order in orders if not order["skip_reason"]]
        self.assertTrue(fills)
        last = fills[-1]
        self.assertEqual(last["price"], 80.0 * 1.0005)
        self.assertGreater(last["target"], 6.0)
        self.assertAlmostEqual(last["quantity"], result["assets"]["A"]["quantity"])

    def test_minimum_notional_accumulates_cash(self):
        bars = make_bars([100.0] * (40 * 24 + 24))
        days = common_days({"A": bars})
        plan = Plan("always", 4, (0.01, 0.03, 0.06))
        result = simulate({"A": bars}, days, {"A": 4.0}, {"A": plan}, {"A": RULE}, "tier")
        orders = result["orders"]
        self.assertEqual(orders[0]["skip_reason"], "cash_below_minimum")
        self.assertFalse(orders[1]["skip_reason"])
        self.assertGreater(orders[1]["notional"], 5.0)

    def test_ordinary_catch_up_spends_the_budget(self):
        bars = make_bars([100.0] * (40 * 24 + 24))
        days = common_days({"A": bars})
        result = simulate({"A": bars}, days, {"A": 6.0}, {}, {"A": RULE}, "ordinary")
        self.assertAlmostEqual(result["contributed"], 6.0 * len(days), places=6)
        self.assertGreater(result["spent"], result["contributed"] * 0.99)
        self.assertLess(result["cash"], 2.0)


if __name__ == "__main__":
    unittest.main()
