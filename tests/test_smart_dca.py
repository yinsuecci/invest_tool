import json
import unittest
import tempfile
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from dca_lab.config import Config
from dca_lab.engine import Rules
from dca_lab.indicators import Feature
from dca_lab.market import Bar,HOUR,milliseconds
from dca_lab.smart_dca import simulate_clock,ideal_comparison,select_development_template,select_smart_report
from dca_lab.vwap_rules import PROFILES
from dca_lab.vwap_unlimited import ordinary_final_budget


class SmartTemplateTests(unittest.TestCase):
    def fixture(self):
        f=Feature(0,0,False,1.,2000,1000,rsi_value=30,vwap20=100)
        days=[(f'1970-01-0{d+1}',[i*HOUR for i in range(d*24,(d+1)*24)]) for d in (1,2,3)]
        values={i*HOUR:(Bar(i*HOUR,100-i*.15,110,70,100-i*.15,100),f) for i in range(96)}
        values['rules']=Rules(step='.000001',min_notional=.5)
        return {'SOL':values},days,{'SOL':PROFILES['balanced_steady']}

    def test_inverse_fixed_reference_quote_and_causal_prefix(self):
        market,days,plan=self.fixture()
        for rho in (1,2):
            result=simulate_clock(market,days,'inverse',plan,rho=rho)
            short=simulate_clock(market,days[:2],'inverse',plan,rho=rho)
            self.assertEqual(result['orders'][:8],short['orders'])
            anchor=market['SOL'][23*HOUR][0].close*1.0005
            for o in result['orders']:
                self.assertEqual(o['reference_price'],anchor)
                self.assertAlmostEqual(o['target']*1.001,7.5*(anchor/o['price'])**rho)
            poison={'SOL':{t:(replace(v[0],low=.0001,high=1e9),v[1]) if isinstance(t,int) else v for t,v in market['SOL'].items()}}
            self.assertEqual(result['orders'],simulate_clock(poison,days,'inverse',plan,rho=rho)['orders'])
            self.assertAlmostEqual(result['contributed'],result['spent']+result['fees']+result['cash'])
            self.assertAlmostEqual(result['contributed'],sum(o['cash_topup'] for o in result['orders']))
        one=ideal_comparison(simulate_clock(market,days,'inverse',plan,rho=1))['SOL']['ideal_cost_gain_pct']
        two=ideal_comparison(simulate_clock(market,days,'inverse',plan,rho=2))['SOL']['ideal_cost_gain_pct']
        self.assertGreaterEqual(one,0.)
        self.assertGreaterEqual(two,one)

    def test_ladder_only_success_advances_and_each_next_fill_is_lower(self):
        market,days,plan=self.fixture()
        result=simulate_clock(market,days,'ladder',plan)
        fills=[o for o in result['orders'] if o['quantity']]
        self.assertGreaterEqual(len(fills),2)
        for i,o in enumerate(fills):
            self.assertEqual(o['ladder_level'],i)
            self.assertAlmostEqual(o['target']*1.001,7.5*1.5**i)
        for a,b in zip(fills,fills[1:]):
            self.assertLessEqual(b['price'],a['price']*.98+1e-8)
            previous_close=market['SOL'][milliseconds(b['time'])-HOUR][0].close
            self.assertLessEqual(previous_close,a['price']*.98+1e-8)
        market['SOL']['rules']=Rules(step='.000001',min_notional=10.)
        failed=simulate_clock(market,days,'ladder',plan)
        self.assertTrue(all(o['ladder_level']==0 for o in failed['orders']))
        self.assertEqual(failed['spent'],0.)

    def test_paired_same_events_and_small_calendar_budget_carries(self):
        market,days,plan=self.fixture()
        candidate=simulate_clock(market,days,'inverse',plan)
        amount=candidate['contributed']
        paired=simulate_clock(market,days,'paired',plan,allocations={'SOL':amount})
        self.assertAlmostEqual(paired['contributed'],amount)
        self.assertEqual([(o['time'],o['asset']) for o in candidate['orders']],[(o['time'],o['asset']) for o in paired['orders']])
        self.assertAlmostEqual(paired['spent']+paired['fees']+paired['cash'],amount)
        market['SOL']['rules']=Rules(step='.001',min_notional=5.)
        baseline=ordinary_final_budget(market,days,{'SOL':8.},Config(),'four',accumulate_small=True)
        self.assertGreater(baseline['spent'],0.)
        self.assertAlmostEqual(baseline['contributed'],8.)
        self.assertAlmostEqual(baseline['spent']+baseline['fees']+baseline['cash'],8.)


@unittest.skipUnless(Path('reports/smart-002/report.json').exists(),'Generate frozen templates first')
class SmartReportTests(unittest.TestCase):
    def test_development_selection_and_later_results_do_not_choose_winner(self):
        source=Path('reports/smart-002/report.json')
        prior=json.loads(source.read_text(encoding='utf-8'))
        selection=select_development_template(prior['results']['development'])
        self.assertEqual(selection['selected_policy'],'inverse_2')
        self.assertEqual(selection['selection_period'],'development')
        self.assertFalse(next(r for r in selection['ranking'] if r['policy']=='ladder_2pct')['eligible'])
        changed=deepcopy(prior)
        for period in ('full','validation','holdout'):
            for asset in changed['results'][period]['inverse_1']['current']['assets'].values():
                asset['unit_cost']=1e-30
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root=Path(tmp).resolve()
            self.assertTrue(root.is_relative_to(Path.cwd().resolve()))
            altered=root/'future.json'
            altered.write_text(json.dumps(changed),encoding='utf-8')
            promoted=select_smart_report(altered,root/'selected')
            self.assertEqual(promoted['default_policy'],selection['selected_policy'])
            self.assertEqual(promoted['selection']['ranking'],selection['ranking'])

    @unittest.skipUnless(Path('reports/smart-003/report.json').exists(),'Generate selected report first')
    def test_selected_report_preserves_every_simulation_and_matching_budget(self):
        root=Path('reports/smart-003')
        report=json.loads((root/'report.json').read_text(encoding='utf-8'))
        prior=json.loads(Path('reports/smart-002/report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['results'],prior['results'])
        self.assertEqual(report['smart'],prior['smart'])
        self.assertEqual(report['default_policy'],'inverse_2')
        self.assertEqual(report['selection']['selection_period'],'development')
        self.assertFalse(report['selection']['independent_evidence'])
        lock=json.loads((root/'selection-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(lock['sha256'],report['selection']['lock_sha256'])
        self.assertEqual(lock['selection']['selected_policy'],report['default_policy'])
        for period in report['results']:
            self.assertEqual(report['budget_matching'][period],report['budget_matching_by_policy'][period]['inverse_2'])

    def test_controls_funding_clock_and_default_not_retrospective_winner(self):
        root=Path('reports/smart-002')
        report=json.loads((root/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['default_policy'],'inverse_1')
        self.assertEqual(report['selection']['selection_period'],'prespecified')
        lock=json.loads((root/'selection-lock.json').read_text(encoding='utf-8'))
        self.assertEqual(lock['selection']['selected_policy'],'inverse_1')
        for period,records in report['results'].items():
            for policy,r in records.items():
                c=r['current']
                self.assertEqual(len(c['orders']),c['days']*4)
                self.assertTrue(all(0<=d['fills']<=4 for d in c['daily']))
                self.assertAlmostEqual(c['contributed'],c['spent']+c['fees']+c['cash'])
                self.assertAlmostEqual(c['contributed'],sum(o['cash_topup'] for o in c['orders']))
                for name in ('four','once','matched'):
                    b=r[name]
                    self.assertAlmostEqual(c['contributed'],b['contributed'])
                    self.assertAlmostEqual(b['contributed'],b['spent']+b['fees']+b['cash'])
                self.assertEqual([(o['time'],o['asset']) for o in c['orders']],[(o['time'],o['asset']) for o in r['matched']['orders']])
                self.assertAlmostEqual(report['budget_matching_by_policy'][period][policy]['total_budget'],c['contributed'])
                self.assertFalse(report['decisions'][policy]['live'])
