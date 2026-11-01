import json
import unittest
from copy import deepcopy
from pathlib import Path

from dca_lab.seven_smart import CANDIDATES, KEYS, choose
from dca_lab.smart_dca import simulate_clock
from dca_lab.market import Bar, HOUR
from dca_lab.indicators import Feature
from dca_lab.engine import Rules
from dataclasses import replace


class SevenSmartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(Path('reports/smart-seven-001/report.json').read_text(encoding='utf-8'))

    def test_asset_identity_and_development_lock(self):
        report = self.report
        self.assertEqual(tuple(report['assets']),KEYS)
        self.assertEqual(set(report['policies']),set(CANDIDATES))
        self.assertEqual(report['assets']['SPY']['identity']['symbol'],'SPYBUSDT')
        self.assertEqual(report['assets']['QQQ']['identity']['symbol'],'QQQBUSDT')
        self.assertEqual(choose(report['results']['development'])['selected_policy'],report['default_policy'])
        poisoned = deepcopy(report)
        poisoned['results']['holdout'] = None
        poisoned['results']['full'] = None
        self.assertEqual(choose(poisoned['results']['development'])['selected_policy'],report['default_policy'])

    def test_funding_and_clock_in_every_candidate(self):
        for records in self.report['results'].values():
            for record in records.values():
                current,base = record['current'],record['four']
                self.assertAlmostEqual(current['contributed'],base['contributed'],places=7)
                self.assertAlmostEqual(current['contributed'],current['spent']+current['fees']+current['cash'],places=7)
                self.assertEqual(len(current['orders']),current['days']*4)
                for asset in KEYS:
                    a,b = current['assets'][asset],base['assets'][asset]
                    self.assertLessEqual(b['spent']+b['fees'],a['spent']+a['fees']+1e-7)

    def test_sparse_filters_are_not_promoted(self):
        dev = self.report['results']['development']
        for policy in ('inverse_2_rsi','inverse_2_ema','inverse_2_bb','inverse_2_bottom'):
            self.assertFalse(dev[policy]['target_quality']['eligible'])
            self.assertFalse(dev[policy]['target_quality']['checks']['per_asset_fills'])
        for period in self.report['smart'].values():
            for policy in CANDIDATES:
                if CANDIDATES[policy][1]:
                    self.assertEqual(period[policy]['ideal'],{})

    def test_confirmations_use_closed_features_and_preserve_prefix(self):
        f = Feature(0,0,False,1.,2000,1000,rsi_value=30,bb_lower=101,ema168=101,sweep_reclaim=True)
        market = {'SOL':{i*HOUR:(Bar(i*HOUR,100,110,90,100,100),f) for i in range(73)}}
        market['SOL']['rules'] = Rules(step='.000001',min_notional=.5)
        days = [('day1',list(range(24*HOUR,48*HOUR,HOUR))),('day2',list(range(48*HOUR,72*HOUR,HOUR)))]
        for confirmation in ('rsi','bb','ema','bottom'):
            result = simulate_clock(market,days,'inverse',{},rho=2,confirmation=confirmation)
            prefix = simulate_clock(market,days[:1],'inverse',{},rho=2,confirmation=confirmation)
            self.assertEqual(result['orders'][:4],prefix['orders'])
            self.assertTrue(all(o['quantity']>0 for o in result['orders']))
            failed = deepcopy(market)
            # Only the first decision's preceding closed feature changes.
            bar,feature = failed['SOL'][23*HOUR]
            failed['SOL'][23*HOUR] = (bar,replace(feature,rsi_value=80,bb_lower=90,ema168=90,sweep_reclaim=False))
            changed = simulate_clock(failed,days,'inverse',{},rho=2,confirmation=confirmation)
            self.assertEqual(changed['orders'][0]['skip_reason'],'no_opportunity')
            self.assertEqual([(o['quantity'],o['skip_reason'],o['price']) for o in result['orders'][1:]],
                             [(o['quantity'],o['skip_reason'],o['price']) for o in changed['orders'][1:]])


if __name__=='__main__':
    unittest.main()
