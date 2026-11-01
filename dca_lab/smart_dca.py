"""Frozen-clock inverse sizing and last-fill ladders, without fitted parameters."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .adaptive import freeze
from .config import Config
from .engine import Rules, day_groups
from .indicators import features
from .market import HOUR, iso, load_dataset
from .portfolio import compare_portfolios, trigger_diagnostics
from .signal_search import lock_selection
from .vwap_rules import tier_signal
from .vwap_search import target_quality, rank
from .vwap_unlimited import ordinary_final_budget

LABELS = {'inverse_1':'固定参考价反比 · rho=1','inverse_2':'固定参考价反比 · rho=2',
          'ladder_2pct':'上次成交价再跌2% · 额度乘1.5'}


def simulate_clock(market, days, mode, plan, rho=1, allocations=None, fee_bps=10, slip_bps=5, confirmation=None, opportunity=None, clock_keys=None):
    keys = tuple(market)
    clock_keys = tuple(clock_keys or keys)
    if len(set(clock_keys))!=len(clock_keys) or not set(keys).issubset(clock_keys):
        raise ValueError('Clock must contain each market asset exactly once')
    if mode not in ('inverse','ladder','paired') or not days:
        raise ValueError('A fixed clock mode and nonempty days are required')
    if mode=='inverse' and rho not in (1,2):
        raise ValueError('Only the two prespecified exponents are supported')
    if confirmation not in (None, 'rsi', 'bb', 'ema', 'bottom') or (confirmation and mode!='inverse'):
        raise ValueError('Confirmation is only supported for inverse sizing')
    if opportunity and (mode!='inverse' or confirmation):
        raise ValueError('Opportunity sizing cannot be mixed with other modes')
    if mode=='paired' and (allocations is None or set(allocations)!=set(keys)):
        raise ValueError('Paired control needs every asset final allocation')
    fee, slip = fee_bps/10000, slip_bps/10000
    anchors = {k:market[k][days[0][1][0]-HOUR][0].close*(1+slip) for k in keys}
    counts = {k:sum(clock_keys[(i*4+s)%len(clock_keys)]==k for i in range(len(days)) for s in range(4)) for k in keys}
    units, wallet, spent_by, fees_by = [dict.fromkeys(keys,0.) for _ in range(4)]
    cash = total = spent = fees = 0.
    nav = peak = 1.
    drawdown = 0.
    previous = None
    last, orders, daily, curve = {}, [], [], []
    def value(prices):
        return cash+sum(units[k]*prices[k] for k in keys)
    def revalue(before, after):
        nonlocal nav, peak, drawdown
        if before>0:
            nav *= after/before
            peak = max(peak,nav)
            drawdown = min(drawdown,nav/peak-1)
    for day_index,(day,ts) in enumerate(days):
        prior_nav, added, paid_day, fills = nav,0.,0.,0
        if len(ts)!=24:
            raise ValueError('Fixed clock requires complete24-hour days')
        for hour,t in enumerate(ts):
            opens = {k:market[k][t][0].open for k in keys}
            before = value(opens)
            if previous is not None:
                revalue(previous,before)
            if hour in (0,6,12,18) and clock_keys[(day_index*4+hour//6)%len(clock_keys)] in market:
                slot = hour//6
                k = clock_keys[(day_index*4+slot)%len(clock_keys)]
                bar, f = market[k][t-HOUR]
                current_bar, rules = market[k][t][0],market[k]['rules']
                price = current_bar.open*(1+slip)
                cap, level, hit = None,None,True
                decision = {}
                if mode=='inverse':
                    gross = 7.5*(anchors[k]/price)**rho
                    if opportunity:
                        decision = opportunity(k,t,price,last.get(k))
                        if decision.pop('reset_episode',False):
                            last.pop(k,None)
                        gross,cap,hit = decision['gross'],decision['cap'],decision['hit']
                    elif confirmation=='rsi':
                        hit = f.rsi_value is not None and f.rsi_value<=50
                    elif confirmation in ('bb','ema'):
                        cap = f.bb_lower if confirmation=='bb' else f.ema168
                        hit = cap is not None and bar.close<=cap and price<=cap
                    elif confirmation=='bottom':
                        hit = f.sweep_reclaim or f.hammer_confirmed
                elif mode=='paired':
                    deposit = allocations[k]/counts[k]
                    wallet[k] += deposit
                    cash += deposit
                    total += deposit
                    added += deposit
                    before += deposit
                    gross = wallet[k]
                elif k not in last:
                    details = tier_signal(f,bar.close,plan[k],'vwap_fitted')
                    cap = details['cap'] if details else None
                    hit = cap is not None and price<=cap
                    gross, level = 7.5,0
                else:
                    cap = last[k]['price']*.98
                    level = last[k]['level']+1
                    gross = 7.5*1.5**level
                    hit = bar.close<=cap and price<=cap
                target = gross/(1+fee) if hit else 0.
                q = rules.quantity(target,price)
                notional, charge = q*price,q*price*fee
                reason = None
                if not hit:
                    reason = 'no_opportunity'
                elif current_bar.volume<=0:
                    reason = 'no_historical_trades'
                elif q<=0 or q<rules.min_qty or q>rules.max_qty or notional<rules.min_notional-1e-8 or notional>rules.max_notional:
                    reason = 'exchange_minimum_or_quantity'
                elif mode=='paired' and notional+charge>wallet[k]+1e-8:
                    reason = 'cash_limit'
                deposit = 0.
                if reason:
                    q = notional = charge = 0.
                else:
                    if mode!='paired':
                        deposit = max(0.,notional+charge-cash)
                        cash += deposit
                        total += deposit
                        added += deposit
                        before += deposit
                    else:
                        wallet[k] -= notional+charge
                    units[k] += q
                    cash -= notional+charge
                    spent += notional
                    fees += charge
                    spent_by[k] += notional
                    fees_by[k] += charge
                    paid_day += notional+charge
                    fills += 1
                    if mode=='ladder':
                        last[k] = {'price':price,'level':level}
                    elif opportunity:
                        last[k] = {'price':price,'time':t,'level':last.get(k,{}).get('level',-1)+1}
                orders.append({'day':day,'time':iso(t),'asset':k,'target':target,'notional':notional,'fee':charge,
                    'quantity':q,'price':price,'skip_reason':reason,'trigger':mode,'signal':0.,'day_count':4,
                    'day_volatility':f.volatility_multiple,'timing_signal':f.rsi_value,'rsi_value':f.rsi_value,
                    'ma_deviation':f.long_deviation,'window_start':iso(t),'price_cap':cap,'reserve_budget':0.,
                    'day_deployment':added,'target_exposure':None,'day_contribution':30.,'sizing_reference':30.,
                    'cash_topup':deposit,'cash_after':cash,'decision_cutoff':iso(t),'wanted_count':4,
                    'reference_price':anchors[k] if mode=='inverse' and not opportunity else None,'rho':rho if mode=='inverse' and not opportunity else None,
                    'ladder_level':level,'historical_volume':current_bar.volume,
                    **({'opportunity':decision,'trigger':'opportunity'} if opportunity else {})})
            after = value(opens)
            revalue(before,after)
            previous = value({k:market[k][t][0].close for k in keys})
            revalue(after,previous)
        daily.append({'date':day,'contribution':added,'deployment':added,'spent':paid_day,'count':4,'wanted_count':4,
            'fills':fills,'return':nav/prior_nav-1,'volatility':None,'sizing_reference':30.})
        curve.append({'date':day,'equity':previous,'cash':cash,'contributions':total,'twr_index':nav*100,
            'position_fraction':(previous-cash)/previous if previous else 0.,'daily_contribution':added})
    assets = {k:{'quantity':units[k],'spent':spent_by[k],'fees':fees_by[k],
        'unit_cost':(spent_by[k]+fees_by[k])/units[k] if units[k] else None,
        'market_value':units[k]*market[k][days[-1][1][-1]][0].close} for k in keys}
    return {'start':days[0][0],'end':days[-1][0],'days':len(days),'contributed':total,'spent':spent,'fees':fees,
        'cash':cash,'equity':previous,'profit':previous-total,'twr_return':nav-1,'max_drawdown':drawdown,
        'execution_rate':(spent+fees)/total if total else 0.,'skipped_slots':sum(bool(o['skip_reason']) for o in orders),
        'assets':assets,'orders':orders,'daily':daily,'curve':curve,'anchors':anchors}


def ideal_comparison(result):
    out = {}
    for key in result['assets']:
        events = [o for o in result['orders'] if o['asset']==key and o['historical_volume']>0]
        if not events:
            out[key] = {'ideal_cost_gain_pct':None,'events':0,'actual_fills':0}
            continue
        prices = [o['price'] for o in events]
        gross = [7.5*(o['reference_price']/o['price'])**o['rho'] for o in events]
        ordinary = len(prices)/sum(1/p for p in prices)
        inverse = sum(gross)/sum(a/p for a,p in zip(gross,prices))
        out[key] = {'ideal_cost_gain_pct':(1-inverse/ordinary)*100,
                    'events':len(prices),'actual_fills':sum(o['quantity']>0 for o in events)}
    return out


def select_development_template(development):
    """This selector deliberately cannot access full/validation/holdout records."""
    rows=[]
    for index,policy in enumerate(LABELS):
        record=development[policy]
        rows.append({'policy':policy,'catalogue_index':index,
                     **target_quality(record['current'],record['four'])})
    rows.sort(key=lambda r:(*rank(r),-r['catalogue_index']),reverse=True)
    return {'selected_policy':rows[0]['policy'],'ranking':rows,
        'eligible_count':sum(r['eligible'] for r in rows),'selection_period':'development',
        'start':development[rows[0]['policy']]['current']['start'],
        'end':development[rows[0]['policy']]['current']['end'],
        'independent_evidence':False,'status':'开发段现有覆盖与最弱成本改善标准首选；不是经验证的长期最优'}


def select_smart_report(source,output):
    raw=Path(source).read_bytes()
    prior=json.loads(raw)
    if prior.get('family')!='smart' or set(prior['policies'])!=set(LABELS):
        raise ValueError('Selection requires the three frozen smart templates')
    report=deepcopy(prior)
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    selection=select_development_template(prior['results']['development'])
    report['protocol']=freeze(output,{'version':'smart-development-selection-1.0',
        'source_report_sha256':hashlib.sha256(raw).hexdigest(),'strategy_protocol_sha256':prior['protocol']['sha256'],
        'change':'Only select display default. All strategies, results, fees, sizing, clocks and controls remain byte-equivalent JSON data.',
        'selection':'Development48 days only. Eligible first: deployment>=60%, global fills>=20, each asset quantity>=50% ordinary-four, all costs present. Then highest worst asset cost gain minus1000*coverage shortfall, mean cost gain, equity delta, stable catalogue order. No validation/holdout/full inputs to selector.',
        'evidence':'Historical retrospective selection, all80 days already exposed; no independent out-of-sample proof or minimum-fill-per-asset gate. Execution minimum filters can create large apparent advantages.'})
    selection['lock_sha256']=lock_selection(output,selection,report['protocol']['sha256'])
    selected=selection['selected_policy']
    report.update(version='0.15.1',selection=selection,default_policy=selected,
        smart_selection={'previous_default':prior['default_policy'],'method':'development_minimum_cost',
                         'strategy_protocol_sha256':prior['protocol']['sha256']})
    for period in report['results']:
        report['budget_matching'][period]=report['budget_matching_by_policy'][period][selected]
    report['decisions'][selected]['status']='开发段标准研究首选，未获独立验证'
    report['limitations']=['本轮只改默认选择：沿用三种冻结策略与全部回测结果，按前48日现有覆盖资格和最弱标的成本改善选研究首选，不用后段或全期重新排名。',
        '覆盖资格没有要求每个标的的成交事件比例；全局成交足够仍可能有个别资产大量最低金额拒单。低成本必须结合成交次数、数量和理想公式对照，不等于公式本身的全部增益。']+[s for s in prior['limitations'] if not s.startswith('三种预设模板独立回测')]
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return report


def evaluate_smart(directory, source, output):
    raw = Path(source).read_bytes()
    prior = json.loads(raw)
    report = deepcopy(prior)
    keys,plan = tuple(prior['assets']),prior['vwap']['fitted_plan']
    manifest,info,datasets = load_dataset(directory,keys)
    hashes = {k:v['sha256'] for k,v in manifest['assets'].items()}
    if hashes!={k:v['sha256'] for k,v in prior['manifest']['assets'].items()}:
        raise ValueError('Frozen templates require identical data')
    config = Config(daily_total=30,center=10,min_count=1,max_count=4)
    market,groups = {},{}
    for k,bars in datasets.items():
        fs=features(bars)
        market[k]={b.time:(b,f) for b,f in zip(bars,fs)}
        market[k]['rules']=Rules.from_symbol(info['symbols'][k])
        groups[k]={d:[bars[i].time for i in ix] for d,ix in day_groups(bars,fs,'rsi',config)}
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    report['protocol']=freeze(output,{'version':'smart-fixed-templates-1.1','source_sha256':hashlib.sha256(raw).hexdigest(),
        'data_sha256':hashes,'source_plan':plan,'policies':LABELS,'default':'inverse_1',
        'clock':'UTC+8 hours0,6,12,18 every day; five assets rotate in stable key order. Four total planned events/day. Each segment resets state. No fitting or winner search.',
        'inverse':'Gross7.5U*(fixed pre-segment closed price incl assumed slippage/current executable open quote incl slippage)^rho. rho1 and2 only. No RSI/VWAP/weight gate. Reference fixed before first event, no future mean.',
        'ladder':'First entry only after prior frozen VWAP opportunity and next-open price cap. Gross7.5U first fill; subsequent closed AND executable quote<=last fill*.98; gross7.5U*1.5^level. Last fill changes only on successful execution. No automatic reset, forced base, indicator filter after first fill, or size ceiling.',
        'funding':'Inject exact actual cash shortfall per valid order, book external deposits; no debt or live funding.',
        'controls':'Per asset final actual gross budgetA. Calendar ordinaryA/N per day once or up to4 times via independent sleeves; subminimum daily budgets accumulate until estimated minimum plus one quantity step can execute, otherwise wait. Paired controlA/event_count allocated on EXACT same rotating event clock, with asset-isolated cash carry. Same final budgets, not same cashflow timing. Quantity/minimum filters apply and can break ideal theorem.',
        'evidence':'All80 historical days previously exposed. Ideal inverse diagnostic assumes fractional trades and same price events, not guarantee>=1% or realized execution advantage.'})
    report.update(family='smart',version='0.15.0',policies=LABELS,default_policy='inverse_1',stress={},
                  results={},budget_matching={},budget_matching_by_policy={},smart={},execution_ablation={})
    for period,old_records in prior['results'].items():
        old=old_records['vwap_fitted']
        days=[(d['date'],groups[keys[0]][d['date']]) for d in old['current']['daily']]
        if any(groups[k].get(d)!=ts for d,ts in days for k in keys):
            raise ValueError('Unaligned fixed clock data')
        report['results'][period],report['budget_matching_by_policy'][period],report['smart'][period]={},{},{}
        for policy in LABELS:
            current=simulate_clock(market,days,'ladder' if policy=='ladder_2pct' else 'inverse',plan,rho=2 if policy=='inverse_2' else 1)
            amounts={k:a['spent']+a['fees'] for k,a in current['assets'].items()}
            four=ordinary_final_budget(market,days,amounts,config,'four',accumulate_small=True)
            once=ordinary_final_budget(market,days,amounts,config,'once',accumulate_small=True)
            paired=simulate_clock(market,days,'paired',plan,allocations=amounts)
            q=target_quality(current,four)
            report['results'][period][policy]={'current':current,'four':four,'once':once,'matched':paired,
                'fixed30':old['fixed30'],'matched_is_ex_post':True,'previous_policy':'four',
                'vs_four':compare_portfolios(four,current),'vs_once':compare_portfolios(once,current),
                'vs_matched':compare_portfolios(paired,current),'vs_previous':compare_portfolios(four,current),
                'vs_unconfirmed':compare_portfolios(current,current),'target_quality':q,
                'trigger_diagnostics':trigger_diagnostics(current,market)}
            matching={'total_budget':sum(amounts.values()),'ordinary_daily':sum(amounts.values())/len(days),
                'assets':{k:{'final_budget':a,'ordinary_daily':a/len(days),
                    'four_actual_spend':four['assets'][k]['spent']+four['assets'][k]['fees'],
                    'four_cash':a-four['assets'][k]['spent']-four['assets'][k]['fees']} for k,a in amounts.items()}}
            report['budget_matching_by_policy'][period][policy]=matching
            report['smart'][period][policy]={'ideal':ideal_comparison(current) if policy.startswith('inverse') else {},
                'paired_cost_gain_pct':compare_portfolios(paired,current)['asset_cost_gain_pct']}
        report['budget_matching'][period]=report['budget_matching_by_policy'][period]['inverse_1']
        if period=='development':
            rows=[{'policy':k,**r['target_quality']} for k,r in report['results'][period].items()]
            report['selection']={'selected_policy':'inverse_1','ranking':rows,'eligible_count':sum(r['eligible'] for r in rows),
                'status':'预先指定rho=1为展示默认；不按历史全胜选优','start':days[0][0],'end':days[-1][0],
                'selection_period':'prespecified','independent_evidence':False}
            report['selection']['lock_sha256']=lock_selection(output,report['selection'],report['protocol']['sha256'])
    report['decisions']={k:{'status':'固定模板历史研究，非独立验证','live':False,'checks':{}} for k in LABELS}
    report['limitations']=['三种预设模板独立回测，不混合调参；默认rho=1事前确定，不按全期结果挑赢家。',
        '新模板每日固定4个窗口、五标的轮转；反比版定时持续买入，与原纯机会单不同。30U仅是每日四次各7.5U的基础尺度，不是投入上限。',
        '反比参考价在每段开始前固定；额度使用当时可观察的开盘报价含假设滑点，不使用未来收盘、最高最低或全期均价。',
        '新模板不使用跨标的仓位软目标，以隔离金额函数；阶梯首次等原VWAP信号，之后每次要求比上次成交再低2%，金额乘1.5且不重置。上涨中可能长期不再买。',
        '同钟对照按每标的最终预算均分到相同事件时点；日历普通对照按最终预算÷天数。两者都为事后预算配平，不是先验预测入金。',
        '交易所最小金额、数量步长、零成交量会改变理想公式结果；未投入现金计入资产。理想反比不等式不能保证实际执行至少便宜1%。',
        '所有外部追加记为投入，不算利润；无实盘、无自动转账。80日历史已看过，分段重置，没有新的独立样本外证据。']
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return report
