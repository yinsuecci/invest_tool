import unittest
import json
from pathlib import Path
from unittest.mock import patch

from dca_lab.vwap_search import catalogue, targeted_catalogue, joint_search, rank, target_quality


class JointSearchTests(unittest.TestCase):
    def portfolio(self, costs=(100., 100.), quantity=1., deployment=.9, fills=20):
        return {'assets': {k: {'unit_cost': c, 'quantity': quantity} for k, c in zip(('A','B'), costs)},
                'daily': [{'contribution': 100., 'return': 0., 'fills': fills}],
                'contributed': 100., 'equity': 100., 'profit': 0., 'max_drawdown': 0.,
                'cash': 10., 'execution_rate': deployment}

    def test_finite_catalogue_order_and_monotone_profiles(self):
        profiles = catalogue()
        self.assertEqual(len(profiles), 206)
        self.assertEqual(profiles, catalogue())
        for profile in profiles.values():
            thresholds, sizes = profile['discounts'], profile['multipliers']
            self.assertEqual(len(thresholds), 4)
            self.assertTrue(all(0 <= x < 1 for x in thresholds))
            self.assertTrue(all(a < b for a,b in zip(thresholds, thresholds[1:])))
            self.assertTrue(all(0 < a <= b for a,b in zip(sizes, sizes[1:])))

    def test_every_asset_target_not_average_and_coverage_not_optional(self):
        base = self.portfolio()
        good = target_quality(self.portfolio((98.,98.)),base)
        bad = target_quality(self.portfolio((90.,101.)),base)
        self.assertTrue(good['target_met'])
        self.assertFalse(bad['target_met'])
        self.assertGreater(rank(good),rank(bad))
        for kwargs in ({'quantity':.1}, {'deployment':.1}, {'fills':1}):
            row = target_quality(self.portfolio((90.,90.),**kwargs),base)
            self.assertFalse(row['eligible'])
            self.assertFalse(row['target_met'])
            self.assertGreater(rank(bad),rank(row))
        missing = target_quality(self.portfolio((None,98.)),base)
        self.assertFalse(missing['target_met'])

    def test_targeted_catalogue_and_protected_assets(self):
        seed = {'A':catalogue()['balanced_steady'],'B':catalogue()['tight_strong']}
        profiles = targeted_catalogue(seed)
        self.assertEqual(len(profiles),254)
        self.assertEqual(profiles['seed_A'],seed['A'])
        self.assertTrue(any(p.get('bb_cap') for p in profiles.values()))
        self.assertTrue(any(p.get('rsi_max') for p in profiles.values()))
        row = target_quality(self.portfolio((100.,90.)),self.portfolio(),protected=('A',))
        self.assertFalse(row['eligible'])
        self.assertFalse(row['checks']['protected'])
        self.assertFalse(row['target_met'])

    def test_inactive_parameters_never_change(self):
        profiles = {'old': {'discounts':[0,.01,.02,.03],'multipliers':[1,2,3,4]},
                    'new': {'discounts':[0,.02,.04,.06],'multipliers':[2,3,4,5]}}
        def simulate(*args, vwap_plan):
            self.assertEqual(vwap_plan['B'],profiles['old'])
            return self.portfolio((98.,98.))
        with patch('dca_lab.vwap_search.portfolio_simulate',side_effect=simulate):
            plan, fits, trials = joint_search({},[],{},None,{'A':.5,'B':.5},
                                             dict.fromkeys(('A','B'),profiles['old']),self.portfolio(),profiles,
                                             active_assets=('A',),protected=('B',))
        self.assertEqual(plan['B'],profiles['old'])
        self.assertEqual(set(fits),{'A'})
        self.assertEqual(len(trials),2)

    def test_search_only_receives_development_days_and_logs_context(self):
        profiles = {'old': {'discounts':[0,.01,.02,.03],'multipliers':[1,2,3,4]},
                    'new': {'discounts':[0,.02,.04,.06],'multipliers':[2,3,4,5]}}
        days, funding = [('development', [1])], {'development':30}
        def simulate(market, selected, cash, config, weights, policy, vwap_plan):
            self.assertIs(selected, days)
            self.assertIs(cash, funding)
            costs = tuple(98. if vwap_plan[k]==profiles['new'] else 100. for k in weights)
            return self.portfolio(costs)
        with patch('dca_lab.vwap_search.portfolio_simulate',side_effect=simulate):
            plan, fits, trials = joint_search({},days,funding,None,{'A':.5,'B':.5},
                                             dict.fromkeys(('A','B'),profiles['old']),self.portfolio(),profiles)
        self.assertEqual(plan,dict.fromkeys(('A','B'),profiles['new']))
        self.assertEqual(len(trials),4)
        self.assertEqual(trials[0]['context']['A'],profiles['old'])
        self.assertEqual(trials[1]['context']['A'],profiles['new'])
        self.assertTrue(all(f['selected_profile']=='new' for f in fits.values()))


@unittest.skipUnless(Path('reports/vwap-004/report.json').exists(),'Generate expanded report first')
class JointReportTests(unittest.TestCase):
    def test_locks_full_trial_count_and_unmodified_seed_control(self):
        root = Path('reports/vwap-004')
        report = json.loads((root/'report.json').read_text(encoding='utf-8'))
        prior = json.loads(Path('reports/vwap-002/report.json').read_text(encoding='utf-8'))
        trials = json.loads((root/'joint-trials.json').read_text(encoding='utf-8'))['selection']
        self.assertEqual(len(trials),10)
        self.assertEqual(sum(len(t['ranking']) for t in trials),2060)
        frozen = json.loads((root/'tier-fit-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(frozen['selection'],report['vwap']['fitting'])
        self.assertEqual(frozen['sha256'],report['vwap']['fit_lock_sha256'])
        lock = json.loads((root/'mode-selection.json').read_text(encoding='utf-8'))
        self.assertEqual(lock['selection']['selected_policy'],report['default_policy'])
        self.assertEqual(lock['selection']['ranking'],report['selection']['ranking'])
        for period, records in report['results'].items():
            if 'seed_comparison' in report['vwap']:
                self.assertEqual(report['vwap']['seed_comparison'][period]['asset_cost_gain_pct'],
                                 prior['results'][period]['vwap_fitted']['vs_four']['asset_cost_gain_pct'])
            for policy in ('vwap_fitted','vwap_bottom','vwap_rsi','vwap_either'):
                record = records[policy]
                self.assertEqual(record['target_quality'],target_quality(record['current'],record['four']))
                current = record['current']
                self.assertAlmostEqual(current['spent']+current['fees']+current['cash'],current['contributed'])
                self.assertTrue(all(0<=d['fills']<=d['count']<=4 for d in current['daily']))
                self.assertTrue(all(d['spent']<=d['deployment']+1e-8<=2*d['contribution']+1e-8 for d in current['daily']))


@unittest.skipUnless(Path('reports/vwap-005/report.json').exists(),'Generate targeted report first')
class TargetedReportTests(unittest.TestCase):
    def test_frozen_parameters_controls_and_filtered_execution(self):
        root = Path('reports/vwap-005')
        report = json.loads((root/'report.json').read_text(encoding='utf-8'))
        prior = json.loads(Path('reports/vwap-004/report.json').read_text(encoding='utf-8'))
        trials = json.loads((root/'joint-trials.json').read_text(encoding='utf-8'))['selection']
        self.assertEqual(len(trials),6)
        self.assertEqual(sum(len(t['ranking']) for t in trials),1542)
        for t in trials:
            self.assertIn(t['asset'],('GOOG','XAU','SOL'))
            for k in ('BTC','ETH'):
                self.assertEqual(t['context'][k],prior['vwap']['fitted_plan'][k])
        for k in ('BTC','ETH'):
            self.assertEqual(report['vwap']['fitted_plan'][k],prior['vwap']['fitted_plan'][k])
        ema = {row[0]:row[2] for row in report['assets']['GOOG']['indicators']}
        from dca_lab.market import HOUR, milliseconds
        for period, records in report['results'].items():
            self.assertEqual(report['vwap']['seed_comparison'][period]['asset_cost_gain_pct'],
                             prior['results'][period]['vwap_fitted']['vs_four']['asset_cost_gain_pct'])
            self.assertIn(period,report['vwap']['filter_ablation'])
            for record in records.values():
                self.assertEqual(record['target_quality'],target_quality(record['current'],record['four'],protected=('BTC','ETH')))
            for order in records['vwap_fitted']['current']['orders']:
                if order['asset']=='GOOG' and order['quantity']:
                    t = milliseconds(order['time'])-HOUR
                    if t in ema:
                        self.assertLessEqual(order['price'],ema[t]*.99+1e-8)
        self.assertEqual(report['selection']['ranking'][0]['policy'],report['default_policy'])
