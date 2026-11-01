import json
import unittest
from pathlib import Path

from dca_lab.config import Config
from dca_lab.engine import Rules
from dca_lab.indicators import Feature
from dca_lab.market import Bar, HOUR, milliseconds
from dca_lab.portfolio import portfolio_simulate
from dca_lab.vwap_rules import PROFILES


class FullAmountTests(unittest.TestCase):
    def test_full_order_or_skip_no_daily_cap_or_overspending(self):
        f = Feature(0,0,False,2.,2000,1000,rsi_value=30,vwap20=100)
        market = {'SOL':{i*HOUR:(Bar(i*HOUR,90,95,80,90,100),f) for i in range(120)}}
        market['SOL']['rules'] = Rules(step='.001',min_notional=5)
        days = [(f'1970-01-0{d+1}',[i*HOUR for i in range(d*24,(d+1)*24)]) for d in range(1,5)]
        funding = {d:30. for d,_ in days}
        config = Config(daily_total=30,center=10,min_count=1,max_count=4)
        plan = {'SOL':PROFILES['balanced_steady']}
        full = portfolio_simulate(market,days,funding,config,{'SOL':1.},'vwap_fitted',vwap_plan=plan,full_vwap_amount=True)
        capped = portfolio_simulate(market,days,funding,config,{'SOL':1.},'vwap_fitted',vwap_plan=plan)
        self.assertTrue(all(d['fills']==0 for d in full['daily'][:3]))
        self.assertGreater(full['daily'][-1]['spent'],60.)
        self.assertGreater(capped['daily'][0]['spent'],0.)
        for order in full['orders']:
            if order['target']:
                self.assertAlmostEqual(order['target'],120/1.001)
            if order['skip_reason']=='cash_limit':
                self.assertEqual(order['quantity'],0)
                self.assertEqual(order['notional'],0)
                self.assertEqual(order['fee'],0)
            self.assertGreaterEqual(order['cash_after'],-1e-8)
        self.assertAlmostEqual(full['spent']+full['fees']+full['cash'],120.)
        with self.assertRaises(ValueError):
            portfolio_simulate(market,days,funding,config,{'SOL':1.},'four',full_vwap_amount=True)
        with self.assertRaises(ValueError):
            portfolio_simulate(market,days,funding,config,{'SOL':1.},'vwap_fitted',budgets={},vwap_plan=plan,full_vwap_amount=True)


@unittest.skipUnless(Path('reports/vwap-006/report.json').exists(),'Generate full-amount ablation first')
class FullAmountReportTests(unittest.TestCase):
    def test_frozen_controls_funding_and_exact_signal_amounts(self):
        report = json.loads(Path('reports/vwap-006/report.json').read_text(encoding='utf-8'))
        prior = json.loads(Path('reports/vwap-005/report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['vwap']['fitted_plan'],prior['vwap']['fitted_plan'])
        self.assertEqual(report['default_policy'],prior['default_policy'])
        self.assertEqual(report['execution_mode'],'full_signal')
        for period, records in report['results'].items():
            for policy in ('vwap_fixed','vwap_fitted','vwap_bottom','vwap_rsi','vwap_either'):
                record = records[policy]
                self.assertEqual(record['four'],prior['results'][period][policy]['four'])
                current = record['current']
                self.assertEqual([d['contribution'] for d in current['daily']], [d['contribution'] for d in record['four']['daily']])
                self.assertAlmostEqual(current['spent']+current['fees']+current['cash'],current['contributed'])
                self.assertTrue(all(0<=d['fills']<=d['count']<=4 for d in current['daily']))
                filled = [o for o in current['orders'] if o['quantity']]
                self.assertTrue(all(milliseconds(b['time'])-milliseconds(a['time'])>=4*HOUR for a,b in zip(filled,filled[1:])))
                for order in current['orders']:
                    self.assertGreaterEqual(order['cash_after'],-1e-8)
                    if order['target']:
                        expected = order['day_contribution']*report['config']['weights'][order['asset']]*order['vwap_multiplier']/1.001
                        self.assertAlmostEqual(order['target'],expected)
                    if order['skip_reason']=='cash_limit':
                        self.assertEqual(order['quantity'],0)
                self.assertFalse(report['decisions'][policy]['live'])
