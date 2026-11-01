"""Single-factor full-signal-size ablation using an unchanged frozen tier plan."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from .adaptive import freeze
from .config import Config
from .engine import Rules, day_groups
from .evaluate import SCENARIOS
from .indicators import features
from .market import load_dataset
from .portfolio import compare_portfolios, portfolio_simulate, trigger_diagnostics
from .signal_search import lock_selection
from .vwap_rules import VWAP_POLICIES
from .vwap_search import target_quality, rank


def evaluate_execution(directory, source, output):
    raw = Path(source).read_bytes()
    prior = json.loads(raw)
    report = deepcopy(prior)
    keys = tuple(prior['assets'])
    manifest, info, datasets = load_dataset(directory, keys)
    if {k:v['sha256'] for k,v in manifest['assets'].items()} != {k:v['sha256'] for k,v in prior['manifest']['assets'].items()}:
        raise ValueError('Execution ablation requires identical historical data')
    weights, plan = prior['config']['weights'], prior['vwap']['fitted_plan']
    config = Config(daily_total=30,center=10,min_count=1,max_count=4)
    market, groups = {}, {}
    for key, bars in datasets.items():
        fs = features(bars)
        market[key] = {b.time:(b,f) for b,f in zip(bars,fs)}
        market[key]['rules'] = Rules.from_symbol(info['symbols'][key])
        groups[key] = {d:[bars[i].time for i in ix] for d,ix in day_groups(bars,fs,'rsi',config)}
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    report['protocol'] = freeze(output,{'version':'full-vwap-amount-1.0',
        'source_report_sha256':hashlib.sha256(raw).hexdigest(),'data_sha256':{k:v['sha256'] for k,v in manifest['assets'].items()},
        'plan':plan,'default_policy':prior['default_policy'],
        'change':'Only remove per-day and per-window amount clipping for VWAP policies. Full pre-rounding principal = daily combined funding * asset weight * tier multiplier / (1+fee). No cap by remaining budget or available cash. Round down only to exchange quantity step; skip ENTIRE order if cash insufficient. No debt, synthetic funding or live trades.',
        'unchanged':'Identical funding, weights, indicators, profiles, confirmations, signal windows, global4h successful-fill cooldown and pre-buy weight guard. Failed attempts still consume a signal window. No parameter re-fitting or winner re-selection.',
        'evaluation':'Same exposed80-day history and four reset-account segments. Compare previous clipped engine, ordinary1/four and ex-post matched-spend diagnostic. All cash and fees counted.'})
    report['results'], ablation = {}, {}
    for period, records in prior['results'].items():
        baseline = records['vwap_fitted']['four']
        days = [(d['date'],groups[keys[0]][d['date']]) for d in baseline['daily']]
        funding = {d['date']:d['contribution'] for d in baseline['daily']}
        for day, ts in days:
            if any(groups[k].get(day)!=ts for k in keys):
                raise ValueError('Nonaligned common historical hours')
        capped = portfolio_simulate(market,days,funding,config,weights,'vwap_fitted',vwap_plan=plan)
        if capped != records['vwap_fitted']['current']:
            raise ValueError('Frozen source cannot be reproduced by the capped control')
        cache = {'mix_sizing':records['mix_sizing']['current']}
        for policy in VWAP_POLICIES:
            cache[policy] = portfolio_simulate(market,days,funding,config,weights,policy,
                vwap_plan=prior['vwap']['fixed_plan'] if policy=='vwap_fixed' else plan,full_vwap_amount=True)
        report['results'][period] = {}
        for policy in prior['policies']:
            current = cache[policy]
            parent = records[policy]['previous_policy']
            previous = baseline if parent=='four' else cache[parent]
            matched = portfolio_simulate(market,days,funding,config,weights,'four',budgets={d['date']:d['spent'] for d in current['daily']})
            report['results'][period][policy] = {**{b:records[policy][b] for b in ('four','once','fixed30')},
                'current':current,'matched':matched,'previous_policy':parent,'matched_is_ex_post':True,
                'trigger_diagnostics':trigger_diagnostics(current,market),
                **{f'vs_{b}':compare_portfolios(records[policy][b],current) for b in ('four','once')},
                'vs_previous':compare_portfolios(previous,current),'vs_matched':compare_portfolios(matched,current),
                'vs_unconfirmed':compare_portfolios(cache['vwap_fitted'],current),
                'target_quality':target_quality(current,baseline)}
        current = cache['vwap_fitted']
        ablation[period] = {'comparison':compare_portfolios(capped,current),
            'prior_cost_gain_pct':records['vwap_fitted']['vs_four']['asset_cost_gain_pct'],
            'cash_rejections':sum(o['skip_reason']=='cash_limit' for o in current['orders']),
            'prior_cash_rejections':sum(o['skip_reason']=='cash_limit' for o in capped['orders']),
            'fills':sum(d['fills'] for d in current['daily']),'prior_fills':sum(d['fills'] for d in capped['daily']),
            'reasons':dict(Counter(o['skip_reason'] for o in current['orders'] if o['skip_reason']))}
        if period=='development':
            quality = target_quality(current,baseline)
            fitting = {k:{'selected_profile':prior['vwap']['fitting'][k]['selected_profile'],
                          'ranking':[{'profile':prior['vwap']['fitting'][k]['selected_profile'],**quality}],
                          'eligible_count':int(quality['eligible']),'frozen':True} for k in keys}
            report['vwap']['fitting'] = fitting
            report['vwap']['fit_lock_sha256'] = lock_selection(output,fitting,report['protocol']['sha256'],'tier-fit-lock.json')
            rows = [{'policy':p,**target_quality(cache[p],baseline)} for p in VWAP_POLICIES]
            rows.sort(key=rank,reverse=True)
            report['selection'] = {'selected_policy':prior['default_policy'],'ranking':rows,
                'eligible_count':sum(r['eligible'] for r in rows),'status':'沿用原信号与档位，仅取消预算截断；未重新选优',
                'selection_period':'development','independent_evidence':False,'start':days[0][0],'end':days[-1][0]}
            report['selection']['lock_sha256'] = lock_selection(output,report['selection'],report['protocol']['sha256'],'mode-selection.json')
        if period=='holdout':
            report['stress'] = {}
            for policy in prior['policies']:
                report['stress'][policy] = {}
                for name,(fee,slip) in SCENARIOS.items():
                    current = portfolio_simulate(market,days,funding,config,weights,policy,fee,slip,
                        vwap_plan=prior['vwap']['fixed_plan'] if policy=='vwap_fixed' else plan,
                        full_vwap_amount=policy in VWAP_POLICIES)
                    report['stress'][policy][name] = {b:compare_portfolios(portfolio_simulate(market,days,funding,config,weights,b,fee,slip),current) for b in ('four','once')}
    report['version'] = '0.13.0'
    report['execution_mode'] = 'full_signal'
    report['execution_ablation'] = ablation
    report['vwap'].update(joint_search=True,targeted=False,trial_count=0,catalogue_size=0,
        filter_ablation={},seed_comparison={},search_note='本轮没有搜索；冻结上一轮档位和信号，仅取消日预算/剩余额度截断。')
    report['limitations'] = ['单因素实验：完整信号额度、不截断；可用现金不足时整笔跳过，不缩小下单额，不借款。交易所步长向下取整仍保留。',
        '无基础单、无强制补投；每天0–4笔成交，成功成交间隔至少4小时；仓位与价格过滤保持原样。',
        '资金仍是五标的合计中心30U、21–39U。去掉预算截断不是无限资金；可能更频繁因现金不足错过机会。',
        '保留原档位、过滤和默认策略，不根据新回测重新选优。80日历史已暴露，不是独立前向证据；分段账户重置。',
        '普通定投接收完全相同资金流，含费均价必须结合数量、现金与净值理解；未启用实盘。']
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return report
