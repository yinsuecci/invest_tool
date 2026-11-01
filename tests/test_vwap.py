import json
import unittest
from dataclasses import replace
from pathlib import Path

from dca_lab.indicators import Feature, features
from dca_lab.market import Bar, HOUR, milliseconds
from dca_lab.signal_search import select_development
from dca_lab.config import Config
from dca_lab.engine import Rules
from dca_lab.portfolio import portfolio_simulate
from dca_lab.vwap_rules import PROFILES, VWAP_POLICIES, rolling_vwap, tier_signal


class VwapRuleTests(unittest.TestCase):
    def bar(self, i, close=100., volume=1.):
        return Bar(i*HOUR,close,close,close,close,volume)

    def test_480_hours_not_20_bars_and_prefix_invariance(self):
        bars = [self.bar(i,100+i*.1,1+i%7) for i in range(700)]
        values = rolling_vwap(bars)
        self.assertTrue(all(v is None for v in values[:479]))
        expected = sum(b.close*b.volume for b in bars[220:])/sum(b.volume for b in bars[220:])
        self.assertAlmostEqual(values[-1],expected)
        self.assertEqual(values[:600],rolling_vwap(bars[:600]))
        self.assertEqual(features(bars)[:600],features(bars[:600]))

    def test_gap_resets_and_zero_volume_never_produces_reference(self):
        bars = [self.bar(i,100,.1+i%3) for i in range(480)]
        bars += [self.bar(i,100,0.) for i in range(480,960)]
        self.assertIsNone(rolling_vwap(bars)[-1])
        bars = [self.bar(i) for i in range(500)] + [self.bar(i) for i in range(501,1000)]
        values = rolling_vwap(bars)
        self.assertIsNone(values[500])
        self.assertIsNone(values[978])
        self.assertEqual(values[979],100.)
        with self.assertRaises(ValueError):
            rolling_vwap(bars,0)

    def test_tiers_increase_target_and_cap_at_closed_tier(self):
        f = Feature(0,0,False,1,1000,1000,rsi_value=30,vwap20=100)
        p = PROFILES['balanced_steady']
        self.assertIsNone(tier_signal(f,100.01,p,'vwap_fitted'))
        self.assertEqual([tier_signal(f,c,p,'vwap_fitted')['tier'] for c in (100,99,97,94,80)],[0,1,2,3,3])
        self.assertEqual([tier_signal(f,c,p,'vwap_fitted')['multiplier'] for c in (100,99,97,94)],[1,2,3,4])
        self.assertEqual([tier_signal(f,c,p,'vwap_fitted')['cap'] for c in (100,99,97,94)],[100,99,97,94])
        self.assertIsNone(tier_signal(replace(f,vwap20=None),90,p,'vwap_fitted'))

    def test_bottom_excludes_rsi_only_and_confirmation_truth_table(self):
        f = Feature(0,0,False,1,1000,1000,rsi_value=30,vwap20=100)
        for bottom in (False,True):
            for rsi in (False,True):
                current = replace(f,sweep_reclaim=bottom,rsi_rebound=rsi)
                for policy,expected in [('vwap_fitted',True),('vwap_bottom',bottom),('vwap_rsi',rsi),('vwap_either',bottom or rsi)]:
                    self.assertEqual(bool(tier_signal(current,90,PROFILES['tight_steady'],policy)),expected)

    def test_optional_filters_use_closed_indicator_caps_and_missing_rejects(self):
        f = Feature(0,0,False,1,1000,1000,rsi_value=39,vwap20=100,ema168=90,bb_lower=87)
        p = {**PROFILES['balanced_steady'],'ema_discount':.01,'rsi_max':40}
        self.assertIsNone(tier_signal(f,95,p,'vwap_fitted'))
        self.assertAlmostEqual(tier_signal(f,88,p,'vwap_fitted')['cap'],89.1)
        self.assertIsNone(tier_signal(replace(f,rsi_value=41),88,p,'vwap_fitted'))
        self.assertIsNone(tier_signal(replace(f,ema168=None),88,p,'vwap_fitted'))
        self.assertIsNone(tier_signal(replace(f,rsi_value=None),88,p,'vwap_fitted'))
        bb = {**p,'bb_cap':True}
        self.assertIsNone(tier_signal(f,88,bb,'vwap_fitted'))
        self.assertAlmostEqual(tier_signal(f,86,bb,'vwap_fitted')['cap'],87)
        self.assertIsNone(tier_signal(replace(f,bb_lower=None),86,bb,'vwap_fitted'))

    def test_next_open_must_pass_ema_cap_not_just_vwap_cap(self):
        f = Feature(0,0,False,2.,2000,1000,rsi_value=30,vwap20=100,ema168=90)
        day = '1970-01-02'
        selected = [(day,[i*HOUR for i in range(24,48)])]
        market = {'SOL':{i*HOUR:(Bar(i*HOUR,90,102,1,88,100),f) for i in range(48)}}
        market['SOL']['rules'] = Rules(step='.001',min_notional=5)
        config = Config(daily_total=30,center=10,min_count=1,max_count=4)
        plan = {'SOL':{**PROFILES['balanced_steady'],'ema_discount':.01}}
        result = portfolio_simulate(market,selected,{day:30},config,{'SOL':1.},'vwap_fitted',vwap_plan=plan)
        self.assertEqual(result['spent'],0)
        plan = {'SOL':PROFILES['balanced_steady']}
        other = portfolio_simulate(market,selected,{day:30},config,{'SOL':1.},'vwap_fitted',vwap_plan=plan)
        self.assertGreater(other['spent'],0)

    def test_next_open_quote_gate_never_fills_at_intrabar_low(self):
        keys = ('GOOG','XAU','SOL','BTC','ETH')
        f = Feature(0,0,False,2.,2000,1000,rsi_value=30,rsi_rebound=True,vwap20=100)
        day = '1970-01-02'
        selected = [(day,[i*HOUR for i in range(24,48)])]
        market = {k:{i*HOUR:(Bar(i*HOUR,101,102,1,95,100),f) for i in range(48)} for k in keys}
        for asset in market.values(): asset['rules']=Rules(step='.001',min_notional=5)
        config = Config(daily_total=30,center=10,min_count=1,max_count=4)
        plan = dict.fromkeys(keys,PROFILES['balanced_steady'])
        no_quote = portfolio_simulate(market,selected,{day:30},config,dict.fromkeys(keys,.2),'vwap_fitted',vwap_plan=plan)
        self.assertEqual(no_quote['spent'],0)
        self.assertEqual(no_quote['cash'],30)
        executable = {k:{t:(replace(value[0],open=90),value[1]) for t,value in v.items() if isinstance(t,int)} for k,v in market.items()}
        for asset in executable.values(): asset['rules']=Rules(step='.001',min_notional=5)
        current = portfolio_simulate(executable,selected,{day:30},config,dict.fromkeys(keys,.2),'vwap_fitted',vwap_plan=plan)
        self.assertGreater(current['spent'],0)
        altered = {k:{t:(replace(v[0],low=.001,high=1e6),v[1]) if isinstance(t,int) else v for t,v in asset.items()} for k,asset in executable.items()}
        other = portfolio_simulate(altered,selected,{day:30},config,dict.fromkeys(keys,.2),'vwap_fitted',vwap_plan=plan)
        self.assertEqual(current['orders'],other['orders'])


class VwapReportTests(unittest.TestCase):
    @unittest.skipUnless(Path('reports/vwap-002/report.json').exists(),'Generate VWAP report first')
    def test_all_trials_locks_and_execution_constraints(self):
        report = json.loads(Path('reports/vwap-002/report.json').read_text(encoding='utf-8'))
        old = json.loads(Path('reports/five-indicator-002/report.json').read_text(encoding='utf-8'))
        self.assertEqual(len(PROFILES),6)
        self.assertEqual(len(report['vwap']['fitting']),5)
        for key,fit in report['vwap']['fitting'].items():
            self.assertEqual(len(fit['ranking']),6)
            top = sorted(fit['ranking'],key=lambda r:(not r['eligible'],-r['score'],r['catalogue_index']))[0]
            self.assertEqual(fit['selected_profile'],top['profile'])
            self.assertEqual(report['vwap']['fitted_plan'][key],PROFILES[top['profile']])
        lock = json.loads(Path('reports/vwap-002/tier-fit-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(lock['selection'],report['vwap']['fitting'])
        self.assertEqual(lock['sha256'],report['vwap']['fit_lock_sha256'])
        choice = select_development(report['results']['development'],VWAP_POLICIES)
        self.assertEqual(choice['ranking'],report['selection']['ranking'])
        self.assertEqual(choice['selected_policy'],report['default_policy'])
        for name,records in report['results'].items():
            self.assertEqual(records['mix_sizing']['current'],old['results'][name]['mix_sizing']['current'])
            for policy in VWAP_POLICIES:
                p = records[policy]
                current = p['current']
                self.assertEqual([d['contribution'] for d in current['daily']],[d['contribution'] for d in p['four']['daily']])
                self.assertAlmostEqual(current['spent']+current['fees']+current['cash'],current['contributed'])
                self.assertTrue(all(0<=d['fills']<=d['count']<=4 for d in current['daily']))
                self.assertTrue(all(d['spent']<=d['deployment']+1e-8<=2*d['contribution']+1e-8 for d in current['daily']))
                filled = [o for o in current['orders'] if o['quantity']]
                for o in filled:
                    profile = report['vwap']['fixed_plan' if policy=='vwap_fixed' else 'fitted_plan'][o['asset']]
                    index = o['vwap_tier']-1
                    self.assertEqual(o['trigger'],policy)
                    self.assertEqual(o['vwap_multiplier'],profile['multipliers'][index])
                    self.assertAlmostEqual(o['price_cap'],o['vwap20']*(1-profile['discounts'][index]))
                    self.assertLessEqual(o['price'],o['price_cap'])
                    self.assertGreaterEqual(o['vwap_depth']+1e-12,profile['discounts'][index])
                    self.assertLessEqual(o['target'],o['day_contribution']*.2*o['vwap_multiplier']/1.001+1e-8)
                    self.assertGreaterEqual(o['cash_after'],-1e-8)
                    if policy=='vwap_rsi': self.assertTrue(o['vwap_rsi_confirmed'])
                    if policy=='vwap_bottom': self.assertTrue(o['vwap_bottom_confirmed'])
                    if policy=='vwap_either': self.assertTrue(o['vwap_rsi_confirmed'] or o['vwap_bottom_confirmed'])
                self.assertTrue(all(milliseconds(b['time'])-milliseconds(a['time'])>=4*HOUR for a,b in zip(filled,filled[1:])))
                self.assertFalse(report['decisions'][policy]['live'])
