"""On-demand external cash with ex-post, per-asset equal-final-budget DCA."""

import hashlib
import json
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from .adaptive import freeze
from .config import Config
from .engine import Rules, day_groups
from .indicators import features
from .market import load_dataset, milliseconds
from .portfolio import portfolio_simulate, compare_portfolios, trigger_diagnostics
from .signal_search import lock_selection
from .vwap_search import target_quality


def ordinary_final_budget(market, days, amounts, config, policy, accumulate_small=False):
    """Independent sleeves fix EACH asset's final allocation, including residual cash."""
    sleeves = {}
    for key, amount in amounts.items():
        budgets = None
        if accumulate_small and amount>0:
            budgets, reserve = {},0.
            rules = market[key]['rules']
            for day,ts in days:
                reserve += amount/len(days)
                quote = market[key][ts[0]][0].open*1.0005
                minimum = (rules.min_notional+float(rules.step)*quote)*1.001
                budgets[day] = reserve if reserve>=minimum else 0.
                if budgets[day]:
                    reserve = 0.
        sleeves[key] = portfolio_simulate({key:market[key]},days,
            {d:amount/len(days) for d,_ in days},config,{key:1.},policy,budgets=budgets) if amount>0 else None
    orders = sorted([o for s in sleeves.values() if s for o in s['orders']],key=lambda o:(o['time'],o['asset']))
    by_time = defaultdict(list)
    for order in orders:
        by_time[milliseconds(order['time'])].append(order)
    units = dict.fromkeys(amounts,0.)
    cash = total = spent = fees = 0.
    nav = peak = 1.
    drawdown = 0.
    previous = None
    daily, curve = [], []
    contribution = sum(amounts.values())/len(days)
    def value(prices):
        return cash+sum(units[k]*prices[k] for k in units)
    def revalue(before, after):
        nonlocal nav, peak, drawdown
        if before>0:
            nav *= after/before
            peak = max(peak,nav)
            drawdown = min(drawdown,nav/peak-1)
    for day, ts in days:
        prior_nav = nav
        opens = {k:market[k][ts[0]][0].open for k in units}
        if previous is not None:
            revalue(previous,value(opens))
        cash += contribution
        total += contribution
        previous = value(opens)
        day_spent, fills = 0., 0
        for t in ts:
            opens = {k:market[k][t][0].open for k in units}
            before = value(opens)
            revalue(previous,before)
            for order in by_time[t]:
                units[order['asset']] += order['quantity']
                paid = order['notional']+order['fee']
                cash -= paid
                spent += order['notional']
                fees += order['fee']
                day_spent += paid
                fills += order['quantity']>0
            after = value(opens)
            revalue(before,after)
            previous = value({k:market[k][t][0].close for k in units})
            revalue(after,previous)
        count = sum(next(d['count'] for d in s['daily'] if d['date']==day) for s in sleeves.values() if s)
        daily.append({'date':day,'contribution':contribution,'deployment':contribution,'spent':day_spent,
            'count':count,'wanted_count':count,'fills':fills,'return':nav/prior_nav-1,'volatility':None})
        curve.append({'date':day,'equity':previous,'cash':cash,'contributions':total,'twr_index':nav*100,
            'position_fraction':(previous-cash)/previous if previous else 0.,'daily_contribution':contribution})
    empty = {'quantity':0.,'spent':0.,'fees':0.,'unit_cost':None,'market_value':0.}
    return {'start':days[0][0],'end':days[-1][0],'days':len(days),'contributed':total,'spent':spent,'fees':fees,
        'cash':cash,'equity':previous,'profit':previous-total,'twr_return':nav-1,'max_drawdown':drawdown,
        'execution_rate':(spent+fees)/total if total else 0.,'skipped_slots':sum(s['skipped_slots'] for s in sleeves.values() if s),
        'assets':{k:sleeves[k]['assets'][k] if sleeves[k] else dict(empty) for k in amounts},
        'orders':orders,'daily':daily,'curve':curve}


def evaluate_unlimited(directory, source, output):
    raw = Path(source).read_bytes()
    prior = json.loads(raw)
    report = deepcopy(prior)
    keys, weights, plan = tuple(prior['assets']),prior['config']['weights'],prior['vwap']['fitted_plan']
    manifest, info, datasets = load_dataset(directory,keys)
    hashes = {k:v['sha256'] for k,v in manifest['assets'].items()}
    if hashes != {k:v['sha256'] for k,v in prior['manifest']['assets'].items()}:
        raise ValueError('Source and on-demand simulation must have identical data')
    config = Config(daily_total=30,center=10,min_count=1,max_count=4)
    market, groups = {}, {}
    for key,bars in datasets.items():
        fs = features(bars)
        market[key] = {b.time:(b,f) for b,f in zip(bars,fs)}
        market[key]['rules'] = Rules.from_symbol(info['symbols'][key])
        groups[key] = {d:[bars[i].time for i in ix] for d,ix in day_groups(bars,fs,'rsi',config)}
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    report['protocol'] = freeze(output,{'version':'on-demand-equal-final-budget-1.0',
        'source_report_sha256':hashlib.sha256(raw).hexdigest(),'data_sha256':hashes,'plan':plan,
        'execution':'No daily cash deposit, budget cap, or cash rejection. At each otherwise valid full-size VWAP order, inject exactly the cash shortfall incl fee; book as external contribution BEFORE fill and exclude deposit from TWR. No borrowing, live transfers or fabricated profits.',
        'sizing':'Prior daily21--39U schedule is ONLY the sizing reference, center30; not an actual funding budget. Preserve profiles, filters, weight guard, windows and successful-fill4h cooldown.',
        'comparison':'Freeze signal plan. For EACH asset A=sum(actual principal+fees). Ordinary independent sleeve deposits A/N each day, buys once or up to4 times/day subject to same quantity/minimum rules, carries dust. All asset allocations and total final contributions match, not daily cashflows or actual rounded spend. Baseline uses ex-post final spend, NOT a deployable predictive funding rule.',
        'evidence':'Same previously exposed80-day history; no fitting, future signal data or claim of global optimum.'})
    report['results'], report['budget_matching'] = {}, {}
    policy = 'vwap_fitted'
    report['policies'] = {policy:'VWAP：冻结档位 / 按需追加资金'}
    report['default_policy'] = policy
    report['stress'] = {}
    for period, old_records in prior['results'].items():
        old = old_records[policy]
        days = [(d['date'],groups[keys[0]][d['date']]) for d in old['four']['daily']]
        funding = {d['date']:d['contribution'] for d in old['four']['daily']}
        if any(groups[k].get(day)!=ts for day,ts in days for k in keys):
            raise ValueError('Unaligned common hourly data')
        current = portfolio_simulate(market,days,funding,config,weights,policy,vwap_plan=plan,
                                     full_vwap_amount=True,top_up_cash=True)
        amounts = {k:a['spent']+a['fees'] for k,a in current['assets'].items()}
        four = ordinary_final_budget(market,days,amounts,config,'four')
        once = ordinary_final_budget(market,days,amounts,config,'once')
        quality = target_quality(current,four)
        report['results'][period] = {policy:{'current':current,'four':four,'once':once,'matched':four,
            'fixed30':old['fixed30'],'matched_is_ex_post':True,'previous_policy':'four',
            'vs_four':compare_portfolios(four,current),'vs_once':compare_portfolios(once,current),
            'vs_matched':compare_portfolios(four,current),'vs_previous':compare_portfolios(four,current),
            'vs_unconfirmed':compare_portfolios(current,current),'target_quality':quality,
            'trigger_diagnostics':trigger_diagnostics(current,market)}}
        report['budget_matching'][period] = {'total_budget':sum(amounts.values()),'ordinary_daily':sum(amounts.values())/len(days),
            'sizing_reference_total':sum(funding.values()),'assets':{k:{'final_budget':a,'ordinary_daily':a/len(days),
                'four_actual_spend':four['assets'][k]['spent']+four['assets'][k]['fees'],
                'four_cash':a-four['assets'][k]['spent']-four['assets'][k]['fees']} for k,a in amounts.items()}}
        if period=='development':
            fitting = {k:{'selected_profile':prior['vwap']['fitting'][k]['selected_profile'],'frozen':True,
                'eligible_count':int(quality['eligible']),'ranking':[{'profile':prior['vwap']['fitting'][k]['selected_profile'],**quality}]} for k in keys}
            report['vwap']['fitting'] = fitting
            report['vwap']['fit_lock_sha256'] = lock_selection(output,fitting,report['protocol']['sha256'],'tier-fit-lock.json')
            report['selection'] = {'selected_policy':policy,'ranking':[{'policy':policy,**quality}],
                'eligible_count':int(quality['eligible']),'status':'冻结档位、按需追加；对标逐标的相同最终预算，资金时序不同',
                'start':days[0][0],'end':days[-1][0],'selection_period':'development','independent_evidence':False}
            report['selection']['lock_sha256'] = lock_selection(output,report['selection'],report['protocol']['sha256'],'mode-selection.json')
    report.update(version='0.14.0',execution_mode='on_demand',execution_ablation={},
        decisions={policy:{'status':'相同最终预算历史对照，未证明普遍优势','live':False,'checks':{}}})
    report['vwap'].update(joint_search=True,targeted=False,trial_count=0,catalogue_size=0,
        filter_ablation={},seed_comparison={},search_note='仅改资金来源与对标口径，未重拟合档位。')
    report['limitations'] = ['无限追加是回测资金假设，不是连接账户自动转账。每笔实际追加明确记为外部投入，不当作盈利。无实盘操作。',
        '30U中心、21–39U仅用于计算信号额度，不再是实际每日资金上限。仍保留档位倍数、仓位过滤、0–4笔/日和4小时成交间隔。',
        '每标的实际含费支出A确定普通定投A/N U每日预算；最终分配资金相同，资金进入时间不同。对标金额事后确定，不是可先验执行的预测规则。',
        '普通每标的每日1次/最多4次在各自独立子账户执行；数量步长、最小金额造成未投入现金，完整计入期末资产。并非保证两边实际买入支出逐分相等。',
        '沿用已看过的80日历史，各段重置账户；没有新样本外证据，没有重新优化或费用压力测试。均价变化不能脱离数量、投入规模与现金比较。']
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return report
