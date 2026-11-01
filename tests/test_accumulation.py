import json
import unittest
from dataclasses import replace
from pathlib import Path

from dca_lab.accumulation import decide, volume_features
from dca_lab.indicators import Feature
from dca_lab.market import Bar,HOUR


class AccumulationSignalsTests(unittest.TestCase):
    def setUp(self):
        self.feature=Feature(0,0,False,1,1000,1000,ema168=100,vwap20=102,atr14=2)
        self.bar=Bar(0,98,99,97,98,100)
        self.flow={'cmf24':.1,'obv24':10,'relative_volume':1.2,'accumulation_proxy':True}

    def signal(self,quote=98,last=None,policy='opportunity_tiers',time=48*HOUR,bar=None):
        return decide(bar or self.bar,self.feature,quote,self.flow,last,time,policy)

    def test_expensive_quote_or_close_never_buys(self):
        self.assertFalse(self.signal(quote=100)['hit'])
        self.assertFalse(self.signal(bar=replace(self.bar,close=100))['hit'])
        self.assertEqual(self.signal(quote=100)['reason'],'above_discount_entry_cap')

    def test_deeper_discount_scales_and_proxy_is_only_optional_boost(self):
        shallow=self.signal()
        deep=self.signal(quote=92,bar=replace(self.bar,close=92))
        self.assertGreater(deep['gross'],shallow['gross'])
        boosted=self.signal(policy='opportunity_flow_boost')
        self.assertEqual(boosted['gross'],shallow['gross']*1.5)
        no_flow={**self.flow,'accumulation_proxy':False}
        strict=decide(self.bar,self.feature,98,no_flow,None,48*HOUR,'opportunity_flow_only')
        self.assertFalse(strict['hit'])

    def test_batch_requires_lower_price_and_cooldown(self):
        last={'price':98,'time':24*HOUR,'level':0}
        self.assertFalse(self.signal(quote=98,last=last)['hit'])
        lower=self.signal(quote=97,last=last)
        self.assertTrue(lower['hit'])
        self.assertEqual(lower['batch'],1)
        self.assertEqual(lower['cap'],97)
        self.assertFalse(self.signal(quote=97,last=last,time=25*HOUR)['hit'])

    def test_reset_does_not_create_a_high_price_fill(self):
        reset=self.signal(quote=103,last={'price':98,'time':0,'level':1},bar=replace(self.bar,close=103))
        self.assertTrue(reset['reset_episode'])
        self.assertFalse(reset['hit'])

    def test_volume_features_are_prefix_causal_and_handle_zero_volume_gap(self):
        bars=[Bar(i*HOUR,100,102,99,101,10+i) for i in range(60)]
        full=volume_features(bars)
        self.assertEqual(volume_features(bars[:40]),{b.time:full[b.time] for b in bars[:40]})
        zeros=volume_features([replace(b,volume=0) for b in bars])
        self.assertFalse(any(v['accumulation_proxy'] for v in zeros.values()))
        gap=volume_features(bars[:30]+bars[31:])
        self.assertIsNone(gap[bars[31].time]['cmf24'])


@unittest.skipUnless(Path('reports/accumulation-002/report.json').exists(),'Generate opportunity report first')
class AccumulationReportTests(unittest.TestCase):
    def test_no_forced_orders_real_funding_and_honest_fallback(self):
        report=json.loads(Path('reports/accumulation-002/report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['default_policy'].startswith('opportunity_'))
        self.assertNotIn('inverse_2',[r['policy'] for r in report['selection']['ranking']])
        for records in report['results'].values():
            for policy,record in records.items():
                current=record['current']
                self.assertAlmostEqual(current['contributed'],current['spent']+current['fees']+current['cash'],places=7)
                self.assertAlmostEqual(current['contributed'],record['four']['contributed'],places=7)
                if not policy.startswith('opportunity_'):
                    continue
                self.assertEqual(len(current['orders']),current['days']*4)
                self.assertTrue(any(o['quantity']==0 for o in current['orders']))
                for order in current['orders']:
                    if order['quantity']:
                        self.assertTrue(order['opportunity']['hit'])
                        self.assertLessEqual(order['price'],order['price_cap'])
                    else:
                        self.assertEqual(order['cash_topup'],0)
        if not report['selection']['eligible_count']:
            self.assertIn('无合格',report['selection']['status'])


if __name__=='__main__':
    unittest.main()
