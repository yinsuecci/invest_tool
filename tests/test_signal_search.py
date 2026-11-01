import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from dca_lab.indicators import Feature, bollinger
from dca_lab.market import HOUR, milliseconds
from dca_lab.portfolio import opportunity_signal
from dca_lab.signal_search import INDICATOR_POLICIES, FIVE_POLICIES, SELECTION_PROTOCOL, indicator_entry, lock_selection, select_development


class CatalogueTests(unittest.TestCase):
    def test_bollinger_population_std_and_prefix(self):
        self.assertEqual(bollinger([5.] * 21)[-1], (5., 5., 5.))
        self.assertEqual(bollinger([1, 3], 2), [(None, None, None), (2., 0., 4.)])
        prices = list(range(30))
        self.assertEqual(bollinger(prices)[:25], bollinger(prices[:25]))
        for period, multiplier in [(1, 2), (20, 0)]:
            with self.assertRaises(ValueError):
                bollinger(prices, period, multiplier)

    def test_eight_truth_tables_and_common_bounce(self):
        prior = Feature(0, 0, False, 1, 1000, 1000, rsi_value=30, ema72=95, ema168=110, atr14=2)
        for rsi in (False, True):
            for bb in (False, True):
                for ma in (False, True):
                    current = replace(prior, rsi_value=33 if rsi else 50, bb_touch_reclaim=bb, ema72=95 if ma else 100)
                    expected = [rsi, bb, ma, rsi and bb, rsi and ma, bb and ma, sum((rsi, bb, ma)) >= 2, rsi and bb and ma]
                    for policy, hit in zip(INDICATOR_POLICIES, expected):
                        self.assertEqual(indicator_entry(current, prior, 96, 94, policy)[0], hit)
                        self.assertFalse(indicator_entry(current, prior, 93, 94, policy)[0])

    def test_signal_still_requires_discount(self):
        prior = Feature(0, 0, False, 1, 1000, 1000, rsi_value=30, ema168=100, atr14=2)
        current = replace(prior, rsi_value=33)
        self.assertTrue(opportunity_signal(current, prior, 98, 97, 'mix_rsi')[0])
        self.assertFalse(opportunity_signal(current, prior, 100, 97, 'mix_rsi')[0])

    def test_or_and_downtrend_guard_use_closed_confirmations(self):
        prior = Feature(0, 0, False, 1, 1000, 1000, rsi_value=30, ema168=100, atr14=2)
        for trend in (False, True, None):
            current = replace(prior, rsi_value=33, downtrend=trend)
            self.assertTrue(indicator_entry(current, prior, 98, 97, 'mix_or')[0])
            self.assertEqual(indicator_entry(current, prior, 98, 97, 'mix_guard')[0], trend is False)
            current = replace(current, bb_touch_reclaim=True)
            self.assertTrue(indicator_entry(current, prior, 98, 97, 'mix_guard')[0])
            self.assertEqual(indicator_entry(current, prior, 98, 97, 'mix_guard')[0], indicator_entry(current, prior, 98, 97, 'mix_sizing')[0])

    def records(self):
        record = {'current': {'execution_rate': .8, 'daily': [{'fills': 20}], 'assets': {k: {'quantity': 1} for k in ('GOOG', 'XAU', 'SOL')}},
                  'four': {'assets': {k: {'quantity': 1} for k in ('GOOG', 'XAU', 'SOL')}},
                  'vs_four': {'asset_cost_gain_pct': {k: 1 for k in ('GOOG', 'XAU', 'SOL')}, 'equity_gain_pct_of_contributions': 1, 'drawdown_delta_pp': 0},
                  'vs_once': {'equity_gain_pct_of_contributions': 1}}
        return {k: copy.deepcopy(record) for k in SELECTION_PROTOCOL['candidates']}

    def test_coverage_before_score_and_stable_tie(self):
        records = self.records()
        records['mix_all']['vs_four']['equity_gain_pct_of_contributions'] = 100
        records['mix_all']['vs_once']['equity_gain_pct_of_contributions'] = 100
        records['mix_all']['current']['execution_rate'] = .1
        chosen = select_development(records)
        self.assertEqual(chosen['selected_policy'], 'discount_only')
        self.assertFalse(chosen['ranking'][-1]['eligible'])
        for r in records.values():
            r['current']['daily'] = []
        self.assertEqual(select_development(records)['eligible_count'], 0)

    def test_lock_is_deterministic_and_refuses_overwrite(self):
        chosen = select_development(self.records())
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as directory:
            path = Path(directory)
            digest = lock_selection(path, chosen, 'protocol')
            self.assertEqual(digest, lock_selection(path, chosen, 'protocol'))
            with self.assertRaises(ValueError):
                lock_selection(path, chosen, 'changed')


class IndicatorReportTests(unittest.TestCase):
    @unittest.skipUnless(Path('reports/five-indicator-002/report.json').exists(), 'Generate five-asset report first')
    def test_five_assets_funding_selection_and_unchanged_prior_candidates(self):
        report = json.loads(Path('reports/five-indicator-002/report.json').read_text(encoding='utf-8'))
        old = json.loads(Path('reports/five-indicator-001/report.json').read_text(encoding='utf-8'))
        keys = ('GOOG', 'XAU', 'SOL', 'BTC', 'ETH')
        self.assertEqual(tuple(report['assets']), keys)
        self.assertEqual(report['config']['daily_center'], 30)
        self.assertEqual(report['config']['weights'], dict.fromkeys(keys, .2))
        candidates = report['protocol']['protocol']['indicator_selection']['candidates']
        self.assertEqual(len(candidates), 15)
        self.assertEqual(select_development(report['results']['development'], candidates)['ranking'], report['selection']['ranking'])
        for name, records in report['results'].items():
            for policy in old['policies']:
                self.assertEqual(records[policy]['current'], old['results'][name][policy]['current'])
            for policy in FIVE_POLICIES:
                p = records[policy]
                r = p['current']
                self.assertEqual(tuple(r['assets']), keys)
                self.assertEqual([d['contribution'] for d in r['daily']], [d['contribution'] for d in p['four']['daily']])
                self.assertTrue(all(21 <= d['contribution'] <= 39 and 0 <= d['fills'] <= d['count'] <= 4 for d in r['daily']))
                self.assertTrue(all(d['spent'] <= d['deployment']+1e-8 <= 2*d['contribution']+1e-8 for d in r['daily']))
                self.assertAlmostEqual(r['cash']+r['spent']+r['fees'],r['contributed'])
                filled = [o for o in r['orders'] if o['quantity']]
                self.assertTrue(all(o['price'] <= o['price_cap'] and o['asset'] in o['candidate_assets'] and o['cash_after'] >= -1e-8 for o in filled))
                self.assertTrue(all(milliseconds(b['time'])-milliseconds(a['time']) >= 4*HOUR for a,b in zip(filled,filled[1:])))
                self.assertFalse(report['decisions'][policy]['live'])
        html = Path('reports/five-indicator-002/index.html').read_text(encoding='utf-8')
        self.assertIn('<th>BTC成本改善 %</th><th>ETH成本改善 %</th>', html)
        self.assertIn('<th>BTC支出</th><th>ETH支出</th>', html)
        self.assertNotIn('三个标的', html)

    @unittest.skipUnless(Path('reports/indicator-001/report.json').exists(), 'Generate indicator report first')
    def test_selection_no_later_data_legacy_and_execution(self):
        report = json.loads(Path('reports/indicator-001/report.json').read_text(encoding='utf-8'))
        old = json.loads(Path('reports/signal-v2-001/report.json').read_text(encoding='utf-8'))
        chosen = select_development(report['results']['development'])
        self.assertEqual(chosen['ranking'], report['selection']['ranking'])
        altered = copy.deepcopy(report)
        for period in ('full', 'validation', 'holdout'):
            altered['results'][period] = {}
        self.assertEqual(select_development(altered['results']['development']), chosen)
        self.assertEqual(len(report['policies']), 22)
        for name, records in report['results'].items():
            for policy in old['policies']:
                self.assertEqual(records[policy]['current'], old['results'][name][policy]['current'])
            for policy in INDICATOR_POLICIES:
                r = records[policy]['current']
                self.assertAlmostEqual(r['cash'] + r['spent'] + r['fees'], r['contributed'])
                self.assertTrue(all(0 <= d['fills'] <= d['count'] <= 4 for d in r['daily']))
                self.assertTrue(all(d['spent'] <= d['deployment'] + 1e-8 <= 2*d['contribution'] + 1e-8 for d in r['daily']))
                filled = [o for o in r['orders'] if o['quantity']]
                for o in filled:
                    self.assertEqual(o['trigger'], policy)
                    self.assertLessEqual(o['price'], o['price_cap'])
                    self.assertIn(o['asset'], o['candidate_assets'])
                    self.assertGreaterEqual(o['cash_after'], -1e-8)
                self.assertTrue(all(milliseconds(b['time'])-milliseconds(a['time']) >= 4*HOUR for a,b in zip(filled,filled[1:])))
                self.assertFalse(report['decisions'][policy]['live'])
