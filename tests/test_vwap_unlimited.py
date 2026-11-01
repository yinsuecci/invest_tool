import json
import unittest
from pathlib import Path

from dca_lab.config import Config
from dca_lab.engine import Rules
from dca_lab.indicators import Feature
from dca_lab.market import Bar, HOUR
from dca_lab.portfolio import portfolio_simulate
from dca_lab.vwap_rules import PROFILES
from dca_lab.vwap_unlimited import ordinary_final_budget


class OnDemandTests(unittest.TestCase):
    def fixture(self, close=90.):
        feature = Feature(0,0,False,2.,2000,1000,rsi_value=30,vwap20=100)
        market = {'SOL':{i*HOUR:(Bar(i*HOUR,close,close,close,close,100),feature) for i in range(72)}}
        market['SOL']['rules'] = Rules(step='.001',min_notional=5)
        days = [(f'1970-01-0{d+1}',[i*HOUR for i in range(d*24,(d+1)*24)]) for d in (1,2)]
        config = Config(daily_total=30,center=10,min_count=1,max_count=4)
        return market,days,config

    def test_topups_are_external_capital_not_profits_or_nominal_daily_deposits(self):
        market,days,config = self.fixture()
        result = portfolio_simulate(market,days,{d:30. for d,_ in days},config,{'SOL':1.},'vwap_fitted',
            vwap_plan={'SOL':PROFILES['balanced_steady']},full_vwap_amount=True,top_up_cash=True)
        self.assertGreater(result['contributed'],60.)
        self.assertAlmostEqual(result['contributed'],sum(o['cash_topup'] for o in result['orders']))
        self.assertAlmostEqual(result['cash'],0.)
        self.assertAlmostEqual(result['contributed'],result['spent']+result['fees'])
        self.assertLess(result['profit'],0.)
        self.assertLess(result['twr_return'],0.)
        self.assertTrue(all(o['skip_reason']!='cash_limit' for o in result['orders']))
        for day in result['daily']:
            self.assertAlmostEqual(day['contribution'],sum(o['cash_topup'] for o in result['orders'] if o['day']==day['date']))
            self.assertEqual(day['sizing_reference'],30.)

    def test_no_signal_means_no_external_contribution_and_zero_equity_is_safe(self):
        market,days,config = self.fixture(close=101.)
        result = portfolio_simulate(market,days,{d:30. for d,_ in days},config,{'SOL':1.},'vwap_fitted',
            vwap_plan={'SOL':PROFILES['balanced_steady']},full_vwap_amount=True,top_up_cash=True)
        self.assertEqual(result['contributed'],0.)
        self.assertEqual(result['cash'],0.)
        self.assertEqual(result['equity'],0.)
        self.assertEqual(result['twr_return'],0.)

    def test_independent_baseline_replay_matches_single_asset_engine(self):
        market,days,config = self.fixture()
        amount = 123.456
        ordinary = ordinary_final_budget(market,days,{'SOL':amount},config,'four')
        direct = portfolio_simulate(market,days,{d:amount/len(days) for d,_ in days},config,{'SOL':1.},'four')
        for field in ('contributed','spent','fees','cash','equity','twr_return','max_drawdown'):
            self.assertAlmostEqual(ordinary[field],direct[field])
        zero = ordinary_final_budget(market,days,{'SOL':0.},config,'four')
        self.assertEqual(zero['equity'],0.)
        self.assertEqual(zero['contributed'],0.)


@unittest.skipUnless(Path('reports/vwap-007/report.json').exists(),'Generate on-demand report first')
class OnDemandReportTests(unittest.TestCase):
    def test_equal_final_asset_allocations_and_cash_conservation(self):
        report = json.loads(Path('reports/vwap-007/report.json').read_text(encoding='utf-8'))
        prior = json.loads(Path('reports/vwap-005/report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['vwap']['fitted_plan'],prior['vwap']['fitted_plan'])
        for period, records in report['results'].items():
            record = records['vwap_fitted']
            current = record['current']
            match = report['budget_matching'][period]
            self.assertAlmostEqual(current['contributed'],current['spent']+current['fees']+current['cash'])
            self.assertAlmostEqual(current['contributed'],sum(o['cash_topup'] for o in current['orders']))
            self.assertAlmostEqual(current['contributed'],match['ordinary_daily']*current['days'])
            self.assertTrue(all(o['skip_reason']!='cash_limit' for o in current['orders']))
            for name in ('four','once'):
                base = record[name]
                self.assertAlmostEqual(base['contributed'],current['contributed'])
                self.assertAlmostEqual(base['contributed'],base['spent']+base['fees']+base['cash'])
                self.assertAlmostEqual(base['equity'],base['cash']+sum(a['market_value'] for a in base['assets'].values()))
            for key, asset in match['assets'].items():
                self.assertAlmostEqual(asset['final_budget'],current['assets'][key]['spent']+current['assets'][key]['fees'])
                self.assertAlmostEqual(asset['final_budget'],asset['ordinary_daily']*current['days'])
                self.assertAlmostEqual(asset['final_budget'],asset['four_actual_spend']+asset['four_cash'])
        self.assertFalse(report['results']['full']['vwap_fitted']['vs_four']['same_funding'])
