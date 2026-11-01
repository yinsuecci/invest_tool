import json
import unittest
from pathlib import Path

from dca_lab.asset_research import CATALOGUE, PREVIOUS, FITTED, asset_decide, asset_quality, choose
from dca_lab.engine import Rules
from dca_lab.indicators import Feature
from dca_lab.market import Bar,HOUR
from dca_lab.smart_dca import simulate_clock


class AssetResearchTests(unittest.TestCase):
    def fixture(self):
        feature=Feature(0,0,False,1,1000,1000,rsi_value=40,ema72=102,ema168=102,vwap20=102,atr14=2)
        market={key:{i*HOUR:(Bar(i*HOUR,99,103,98,100,100),feature) for i in range(96)} for key in ('A','B','C')}
        for values in market.values():
            values['rules']=Rules(step='.000001',min_notional=.5)
        days=[('day1',list(range(24*HOUR,48*HOUR,HOUR))),('day2',list(range(48*HOUR,72*HOUR,HOUR)))]
        return market,days

    def test_single_asset_projection_preserves_original_global_events(self):
        market,days=self.fixture()
        def signal(key,time,quote,last):
            return {'gross':7.5,'cap':110,'hit':True,'reset_episode':False}
        full=simulate_clock(market,days,'inverse',{},opportunity=signal)
        sleeve=simulate_clock({'B':market['B']},days,'inverse',{},opportunity=signal,clock_keys=tuple(market))
        fields=('time','asset','quantity','price','notional','fee','skip_reason','cash_topup')
        self.assertEqual([[o[f] for f in fields] for o in full['orders'] if o['asset']=='B'],
                         [[o[f] for f in fields] for o in sleeve['orders']])
        self.assertEqual(full['assets']['B'],sleeve['assets']['B'])
        self.assertLess(len(sleeve['orders']),8)
        short=simulate_clock({'B':market['B']},days[:1],'inverse',{},opportunity=signal,clock_keys=tuple(market))
        self.assertEqual(short['orders'],sleeve['orders'][:len(short['orders'])])

    def test_invalid_projected_clock_is_rejected(self):
        market,days=self.fixture()
        for keys in (('A','A','B','C'),('A','B')):
            with self.assertRaises(ValueError):
                simulate_clock(market,days,'inverse',{},clock_keys=keys)

    def test_catalogue_has_96_unique_bounded_variants(self):
        self.assertEqual(len(CATALOGUE),96)
        self.assertEqual(len({json.dumps(p,sort_keys=True) for p in CATALOGUE}),96)

    def test_independent_batch_spacing_and_high_price_gate(self):
        market,_=self.fixture()
        bar,f=market['A'][0]
        plan={'reference':'ema','entry_atr':0,'sizing':'equal','rsi_max':45,'next_atr':.25}
        last={'price':100,'time':0,'level':0}
        loose=asset_decide(bar,f,99.2,last,48*HOUR,plan)
        strict=asset_decide(bar,f,99.2,last,48*HOUR,{**plan,'next_atr':.75})
        self.assertTrue(loose['hit'])
        self.assertFalse(strict['hit'])
        self.assertEqual(strict['reason'],'wait_next_lower_batch')
        self.assertFalse(asset_decide(bar,f,103,None,48*HOUR,plan)['hit'])
        self.assertIn('asset_plan',loose)
        self.assertNotIn('sol_plan',loose)

    def test_selector_prefers_eligible_and_incumbent_on_ties(self):
        rows=[{'index':2,'eligible':False,'score':99},
              {'index':1,'eligible':True,'score':2},
              {'index':0,'eligible':True,'score':2}]
        self.assertEqual(choose(rows)[0]['index'],0)
        self.assertEqual(choose(rows)[-1]['index'],2)

    def test_quality_reads_only_the_target_asset(self):
        def result(cost):
            return {'assets':{'XAU':{'quantity':1,'unit_cost':cost,'spent':cost,'fees':0}},
                    'orders':[{'asset':'XAU','quantity':.2} for _ in range(5)]}
        current,base=result(95),result(100)
        expected=asset_quality(current,base,'XAU')
        current['assets']['SOL']={'future_profit':1e99}
        base['assets']['SOL']={'future_profit':-1e99}
        self.assertEqual(expected,asset_quality(current,base,'XAU'))
        self.assertAlmostEqual(expected['cost_gain_pct'],5)


@unittest.skipUnless(Path('reports/independent-assets-001/report.json').exists(),'Generate independent fit first')
class IndependentReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report=json.loads(Path('reports/independent-assets-001/report.json').read_text(encoding='utf-8'))

    def test_679_trials_and_development_only_ranking(self):
        study=self.report['opportunity_study']['independent_assets']
        self.assertEqual(study['trial_count'],679)
        self.assertEqual(study['catalogue_size'],97)
        for key,fit in study['fitting'].items():
            self.assertEqual(len(fit['ranking']),97)
            self.assertEqual(choose(fit['ranking'])[0],fit['selected'])
            self.assertEqual(fit['selected']['plan'],study['plans'][key])
            current=self.report['results']['development'][FITTED]
            actual=asset_quality(current['current'],current['four'],key)
            for field in ('fills','budget','cost_gain_pct','quantity','unit_cost'):
                self.assertAlmostEqual(actual[field],fit['selected'][field])
            if fit['incumbent']['eligible']:
                self.assertGreaterEqual(fit['selected']['score']+1e-9,fit['incumbent']['score'])
        self.assertEqual(self.report['selection']['selection_period'],'development')
        self.assertFalse(self.report['selection']['independent_evidence'])

    def test_old_combined_strategy_is_unchanged(self):
        prior=json.loads(Path('reports/sol-targeted-002/report.json').read_text(encoding='utf-8'))
        for period,records in self.report['results'].items():
            self.assertEqual(records[PREVIOUS]['current'],prior['results'][period]['opportunity_sol_fitted']['current'])
            self.assertEqual(records[PREVIOUS]['four'],prior['results'][period]['opportunity_sol_fitted']['four'])

    def test_shared_capital_order_limit_and_price_caps(self):
        for records in self.report['results'].values():
            for record in records.values():
                current=record['current']
                self.assertEqual(len(current['orders']),4*current['days'])
                self.assertAlmostEqual(current['contributed'],record['four']['contributed'],places=7)
                self.assertAlmostEqual(current['contributed'],current['spent']+current['fees']+current['cash'],places=7)
                for order in current['orders']:
                    if order['quantity']:
                        self.assertLessEqual(order['price'],order['price_cap'])
                    else:
                        self.assertEqual(order['cash_topup'],0)


if __name__=='__main__':
    unittest.main()
