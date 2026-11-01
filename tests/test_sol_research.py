import json
import unittest
from copy import deepcopy
from pathlib import Path

from dca_lab.sol_research import CATALOGUE, sol_decide, sol_quality
from dca_lab.indicators import Feature
from dca_lab.market import Bar,HOUR


class SolSignalTests(unittest.TestCase):
    def setUp(self):
        self.f=Feature(0,0,False,1,1000,1000,rsi_value=40,ema72=101,ema168=100,vwap20=90,atr14=2)
        self.bar=Bar(0,98,99,97,98,100)
        self.plan={'reference':'ema','entry_atr':0.,'sizing':'percent','rsi_max':None}

    def test_catalogue_is_bounded_and_deterministic(self):
        self.assertEqual(len(CATALOGUE),48)
        self.assertEqual(len({json.dumps(p,sort_keys=True) for p in CATALOGUE}),48)

    def test_reference_removes_old_vwap_gate_but_still_skips_high_prices(self):
        min_plan={**self.plan,'reference':'min'}
        self.assertFalse(sol_decide(self.bar,self.f,98,None,0,min_plan)['hit'])
        self.assertTrue(sol_decide(self.bar,self.f,98,None,0,self.plan)['hit'])
        self.assertFalse(sol_decide(self.bar,self.f,101,None,0,self.plan)['hit'])

    def test_percent_tiers_do_not_blow_up_when_atr_contracts(self):
        from dataclasses import replace
        a=sol_decide(self.bar,self.f,98,None,0,self.plan)
        b=sol_decide(self.bar,replace(self.f,atr14=.00001),98,None,0,self.plan)
        self.assertEqual(a['gross'],b['gross'])
        self.assertEqual(a['tier'],1)
        self.assertEqual(a['gross'],15)

    def test_rsi_filter_and_lower_batch_remain_causal(self):
        strict={**self.plan,'rsi_max':35}
        self.assertEqual(sol_decide(self.bar,self.f,98,None,0,strict)['reason'],'rsi_filter')
        last={'price':98,'time':0,'level':0}
        self.assertFalse(sol_decide(self.bar,self.f,98,last,48*HOUR,self.plan)['hit'])
        self.assertTrue(sol_decide(self.bar,self.f,97,last,48*HOUR,self.plan)['hit'])

    def test_quality_does_not_read_other_assets(self):
        def result(cost):
            return {'assets':{'SOL':{'unit_cost':cost,'quantity':1,'spent':cost,'fees':0,'market_value':100}},
                    'orders':[{'asset':'SOL','quantity':.2} for _ in range(5)]}
        current,base=result(90),result(100)
        a=sol_quality(current,base)
        current['assets']['BTC']={'future_return':1e99}
        base['assets']['BTC']={'future_return':-1e99}
        self.assertEqual(a,sol_quality(current,base))
        self.assertTrue(a['eligible'])
        self.assertAlmostEqual(a['cost_gain_pct'],10)


@unittest.skipUnless(Path('reports/sol-targeted-002/report.json').exists(),'Generate SOL report first')
class SolReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=json.loads(Path('reports/sol-targeted-002/report.json').read_text(encoding='utf-8'))
        cls.prior=json.loads(Path('reports/accumulation-002/report.json').read_text(encoding='utf-8'))

    def test_other_six_assets_and_old_policy_are_unchanged(self):
        for period,records in self.report['results'].items():
            before=self.prior['results'][period]['opportunity_flow_boost']['current']
            self.assertEqual(records['opportunity_sol_previous']['current'],before)
            after=records['opportunity_sol_fitted']['current']
            for key in before['assets']:
                if key=='SOL':
                    continue
                self.assertEqual(before['assets'][key],after['assets'][key])
                fields=('time','asset','price','quantity','notional','fee','skip_reason')
                self.assertEqual([[o[f] for f in fields] for o in before['orders'] if o['asset']==key],
                                 [[o[f] for f in fields] for o in after['orders'] if o['asset']==key])

    def test_selected_plan_is_exact_development_winner(self):
        report=self.report
        trial=report['opportunity_study']['sol_targeted']
        ranked=sorted(trial['ranking'],key=lambda r:(r['eligible'],r['score'],-r['index']),reverse=True)
        self.assertEqual(ranked[0],trial['selected'])
        self.assertTrue(trial['selected']['eligible'])
        self.assertEqual(report['selection']['selection_period'],'development')
        self.assertFalse(report['selection']['independent_evidence'])
        for records in report['results'].values():
            for o in records['opportunity_sol_fitted']['current']['orders']:
                if o['asset']=='SOL':
                    self.assertEqual(o['opportunity']['sol_plan'],trial['selected']['plan'])
        q=sol_quality(report['results']['development']['opportunity_sol_fitted']['current'],
                      report['results']['development']['opportunity_sol_fitted']['four'])
        self.assertAlmostEqual(q['cost_gain_pct'],trial['selected']['cost_gain_pct'])

    def test_equal_final_capital_and_no_forced_fills(self):
        for records in self.report['results'].values():
            for record in records.values():
                a=record['current']
                self.assertAlmostEqual(a['contributed'],record['four']['contributed'],places=7)
                self.assertAlmostEqual(a['contributed'],a['spent']+a['fees']+a['cash'],places=7)
                for order in a['orders']:
                    if order['quantity']:
                        self.assertLessEqual(order['price'],order['price_cap'])
                    else:
                        self.assertEqual(order['cash_topup'],0)


if __name__=='__main__':
    unittest.main()
