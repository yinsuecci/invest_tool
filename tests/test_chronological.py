import unittest
from copy import deepcopy
from dataclasses import replace

from dca_lab.chronological import CATALOGUE, choose, decision, fit, make_schedule, simulate
from dca_lab.engine import Rules
from dca_lab.indicators import Feature, features
from dca_lab.market import Bar, HOUR, milliseconds


class ChronologicalTests(unittest.TestCase):
    def test_fitted_legacy_full_period_excludes_development(self):
        from dca_lab.seven_smart import evaluation_periods
        days = [(str(i), (i,)) for i in range(80)]
        for study in ({'sol_targeted': {}}, {'independent_assets': {}}):
            periods = evaluation_periods(days, study)
            self.assertEqual(periods['full'], days[48:])
            self.assertTrue(set(d for d, _ in periods['development']).isdisjoint(d for d, _ in periods['full']))

    def fixture(self, days=16):
        f = Feature(0., 0., False, 1., 1000, 1000, rsi_value=40.,
                    ema168=105., ema72=104., atr14=2., vwap20=105.)
        market = {}
        for key in ('SOL', 'BTC'):
            market[key] = {i * HOUR: (Bar(i * HOUR, 100., 102., 98., 100., 100.), f)
                           for i in range(24 * (days + 1))}
            market[key]['rules'] = Rules(step='.000001', min_notional=5.)
        calendar = [(str(i), tuple(t * HOUR for t in range(24 * i, 24 * (i + 1))))
                    for i in range(1, days + 1)]
        return market, calendar

    def test_catalogue_is_bounded_and_has_no_specially_trained_asset(self):
        self.assertEqual(len(CATALOGUE), 24)
        self.assertEqual(len({str(p) for p in CATALOGUE}), 24)

    def test_features_are_prefix_invariant(self):
        bars = [Bar(i * HOUR, 100. + i % 17, 120. + i % 17, 90. + i % 17,
                    101. + i % 17, 10. + i % 5) for i in range(1100)]
        future = [Bar(i * HOUR, 1e6, 2e6, 1., 1e6, 1e9) for i in range(1100, 1200)]
        self.assertEqual(features(bars), features(bars + future)[:len(bars)])

    def test_fit_and_schedule_ignore_all_future_prices(self):
        market, days = self.fixture()
        cutoff = days[7][1][-1] + HOUR
        changed = deepcopy(market)
        for key in changed:
            for t in tuple(changed[key]):
                if t != 'rules' and t >= cutoff:
                    b, f = changed[key][t]
                    changed[key][t] = (replace(b, open=1e6, high=2e6, low=1., close=1e6, volume=1e9),
                                       replace(f, ema168=1., vwap20=1., rsi_value=100.))
        self.assertEqual(fit(market, days[:8]), fit(changed, days[:8]))
        original = make_schedule(market, days, train_days=8, test_days=4)
        mutation = make_schedule(changed, days, train_days=8, test_days=4)
        self.assertEqual(original[0], mutation[0])
        self.assertTrue(all(row['train_end'] < row['test_start'] for row in original))

    def test_frozen_schedule_never_trades_training_dates(self):
        market, days = self.fixture()
        schedule = make_schedule(market, days, train_days=8, test_days=4)
        result = simulate(market, days[8:], schedule=schedule)
        self.assertTrue(result['orders'])
        for order in result['orders']:
            self.assertGreaterEqual(milliseconds(order['time']), schedule[0]['test_start'])
            self.assertLess(milliseconds(order['plan_cutoff']), milliseconds(order['time']))
        with self.assertRaises(ValueError):
            simulate(market, days, schedule=schedule)

    def test_future_append_preserves_earlier_orders(self):
        market, days = self.fixture()
        plan = CATALOGUE[1]
        plans = dict.fromkeys(market, plan)
        prefix = simulate(market, days[:8], plans=plans)
        full = simulate(market, days, plans=plans)
        end = days[7][1][-1] + HOUR
        self.assertEqual(prefix['orders'], [o for o in full['orders'] if milliseconds(o['time']) < end])
        self.assertEqual(prefix['daily'], full['daily'][:8])

    def test_same_day_funding_and_cash_identity(self):
        market, days = self.fixture()
        strategy = simulate(market, days, plans=dict.fromkeys(market, CATALOGUE[1]))
        ordinary = simulate(market, days, ordinary='four')
        self.assertEqual(strategy['contributed'], ordinary['contributed'])
        self.assertEqual([d['contribution'] for d in strategy['daily']], [d['contribution'] for d in ordinary['daily']])
        for result in (strategy, ordinary):
            self.assertAlmostEqual(result['contributed'], result['spent'] + result['fees'] + result['cash'])
            self.assertGreaterEqual(result['cash'], -1e-8)
        self.assertTrue(all(d['fills'] <= 4 for d in strategy['daily']))

    def test_no_partial_fill_when_wallet_cannot_cover_intended_amount(self):
        market, days = self.fixture()
        plans = dict.fromkeys(market, CATALOGUE[1])
        result = simulate(market, days[:1], daily_total=1., plans=plans)
        self.assertFalse(result['orders'])
        self.assertEqual(result['cash'], 1.)

    def test_current_bar_extremes_do_not_change_the_open_decision(self):
        market, days = self.fixture()
        time = days[0][1][0]
        modified = deepcopy(market)
        b, f = modified['SOL'][time]
        modified['SOL'][time] = (replace(b, high=1e9, low=.001, close=1e8, volume=1e6), f)
        plans = dict.fromkeys(market, CATALOGUE[0])
        first = simulate(market, days[:1], plans=plans)['orders'][0]
        second = simulate(modified, days[:1], plans=plans)['orders'][0]
        self.assertEqual(first, second)

    def test_zero_volume_outcome_does_not_reschedule_to_another_asset(self):
        market, days = self.fixture()
        time = days[0][1][0]
        b, f = market['SOL'][time]
        market['SOL'][time] = (replace(b, volume=0.), f)
        result = simulate(market, days[:1], plans=dict.fromkeys(market, CATALOGUE[0]))
        first = result['orders'][0]
        self.assertEqual(first['asset'], 'SOL')
        self.assertEqual(first['quantity'], 0.)
        self.assertGreater(first['intended_gross'], 0.)

    def test_ordinary_once_and_four_have_distinct_frequencies(self):
        market, days = self.fixture()
        once = simulate(market, days[:1], daily_total=200., ordinary='once')
        four = simulate(market, days[:1], daily_total=200., ordinary='four')
        self.assertEqual(len(once['orders']), 2)
        self.assertEqual(len(four['orders']), 8)

    def test_ranking_rejects_cash_only_and_uses_equity(self):
        records = {k: {'current': {'equity': equity, 'execution_rate': rate, 'average_daily_spend': rate * 30}}
                   for k, equity, rate in [('cash', 1000., 0.), ('a', 950., .95), ('b', 960., .96)]}
        self.assertEqual(choose(records)[0]['policy'], 'b')

    def test_high_price_skips_and_deeper_discounts_size_up(self):
        market, _ = self.fixture()
        bar, f = market['SOL'][0]
        plan = CATALOGUE[1]
        self.assertIsNone(decision(bar, f, 106., None, HOUR, plan, 7.5))
        cheap = decision(replace(bar, close=90.), f, 90., None, HOUR, plan, 7.5)
        shallow = decision(bar, f, 100., None, HOUR, plan, 7.5)
        self.assertGreater(cheap['gross'], shallow['gross'])


if __name__ == '__main__':
    unittest.main()
