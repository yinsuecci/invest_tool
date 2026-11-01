"""Bounded SOL-only development search; the other six entry rules stay frozen."""

import json
from copy import deepcopy
from pathlib import Path
from math import floor

from .accumulation import STUDY, factory
from .adaptive import freeze
from .config import Config
from .engine import Rules
from .indicators import features
from .ma_tiers import common_days, split_days
from .market import HOUR, load_dataset
from .seven_smart import KEYS, evaluate
from .smart_dca import simulate_clock
from .vwap_unlimited import ordinary_final_budget

CATALOGUE = tuple({'reference':reference,'entry_atr':entry,'sizing':sizing,'rsi_max':rsi}
    for reference in ('min','ema','blend','ema72') for entry in (-.25,0.,.25)
    for sizing in ('equal','percent') for rsi in (None,45.))


def sol_decide(bar,f,quote,last,time,plan):
    valid=all(v is not None and v>0 for v in (f.vwap20,f.ema168,f.atr14))
    reference=(f.ema72 if plan['reference']=='ema72' else f.ema168 if plan['reference']=='ema' else (f.ema168+f.vwap20)/2 if plan['reference']=='blend' else min(f.vwap20,f.ema168)) if valid else None
    valid=bool(valid and reference is not None and reference>0)
    cap=reference-plan['entry_atr']*f.atr14 if valid else None
    reset=bool(valid and bar.close>reference+.5*f.atr14)
    if reset:
        last=None
    depth=max(0.,1-max(bar.close,quote)/reference) if valid else 0.
    tier=floor((depth+1e-12)/.02)
    gross=7.5*(1+tier) if plan['sizing']=='percent' else 7.5
    reason=None
    if not valid:
        reason='indicator_warmup'
    elif max(bar.close,quote)>cap:
        reason='above_discount_entry_cap'
    elif plan['rsi_max'] is not None and (f.rsi_value is None or f.rsi_value>plan['rsi_max']):
        reason='rsi_filter'
    elif last and (time-last['time']<24*HOUR or quote>last['price']-.5*f.atr14):
        reason='wait_next_lower_batch'
    if last and valid:
        cap=min(cap,last['price']-.5*f.atr14)
    return {'hit':reason is None,'gross':gross,'cap':cap,'reference':reference,
        'atr14':f.atr14 if valid else None,'depth_atr':depth*reference/f.atr14 if valid else 0.,
        'depth_pct':depth*100,'tier':tier,'batch':0 if last is None else last['level']+1,
        'reset_episode':reset,'reason':reason,'cmf24':None,'obv24':None,
        'relative_volume':None,'accumulation_proxy':False,'sol_plan':plan}


def sol_quality(current,baseline):
    a,b=current['assets']['SOL'],baseline['assets']['SOL']
    fills=sum(o['asset']=='SOL' and o['quantity']>0 for o in current['orders'])
    gain=(1-a['unit_cost']/b['unit_cost'])*100 if a['unit_cost'] and b['unit_cost'] else None
    ratio=a['quantity']/b['quantity'] if b['quantity'] else 0.
    checks={'fills':fills>=5,'quantity':ratio>=.5,'cost':gain is not None}
    value=gain if gain is not None else -100.
    return {'eligible':all(checks.values()),'checks':checks,'fills':fills,'quantity_ratio':ratio,
        'cost_gain_pct':gain,'score':value-1000*(max(0,5-fills)/5+max(0,.5-ratio)+(gain is None)),
        'minimum_cost_gain_pct':value,'mean_cost_gain_pct':value,
        'equity_delta':a['market_value']-b['market_value'],
        'unit_cost':a['unit_cost'],'budget':a['spent']+a['fees']}


def targeted_factory(plan):
    original=factory()
    def build(data,market,policy):
        prior=original(data,market,'opportunity_flow_boost')
        if policy=='opportunity_sol_previous':
            return prior
        def signal(key,time,quote,last):
            if key!='SOL':
                return prior(key,time,quote,last)
            bar,f=market[key][time-HOUR]
            return sol_decide(bar,f,quote,last,time,plan)
        return signal
    return build


def fit(data,market,development,output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    protocol=freeze(output,{'version':'sol-targeted-grid-1.1',
        'catalogue':CATALOGUE,'selection':'SOL only: >=5 successful fills, quantity>=50% equal-final-budget ordinary; eligible first, highest development cost gain, stable catalogue order.',
        'scope':'Only SOL reference, entry ATR and amount sizing/RSI change. All seven asset clock events unchanged; all other six rules exactly opportunity_flow_boost. Second bounded round extends archived24 to48 candidates after the first full report was seen; not independent research.',
        'sizing':'7.5U equal or 7.5*(1+floor(relative discount/.02))U. No amount ceiling; prior closed EMA168/VWAP20d. Same lower-next-fill0.5ATR and24h cooldown, reset above reference+.5ATR.',
        'evidence':'All periods previously exposed; retrospective development search, not independent evidence.',
        'development_start':development[0][0],'development_end':development[-1][0]})
    builder_cache={}
    rows=[]
    for index,plan in enumerate(CATALOGUE):
        # Share the causal volume maps across all candidates, never the account state.
        if 'original' not in builder_cache:
            builder_cache['original']=factory()(data,market,'opportunity_flow_boost')
        original=builder_cache['original']
        def signal(key,time,quote,last):
            if key!='SOL':
                return original(key,time,quote,last)
            bar,f=market[key][time-HOUR]
            return sol_decide(bar,f,quote,last,time,plan)
        current=simulate_clock(market,development,'inverse',{},rho=2,opportunity=signal)
        asset=current['assets']['SOL']
        baseline=ordinary_final_budget({'SOL':market['SOL']},development,
            {'SOL':asset['spent']+asset['fees']},Config(daily_total=30,center=10,min_count=1,max_count=4),'four',accumulate_small=True)
        rows.append({'index':index,'plan':plan,**sol_quality(current,baseline)})
    rows.sort(key=lambda r:(r['eligible'],r['score'],-r['index']),reverse=True)
    result={'protocol_sha256':protocol['sha256'],'selected':rows[0],'ranking':rows,
        'eligible_count':sum(r['eligible'] for r in rows),'independent_evidence':False}
    (Path(output)/'sol-trials.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return result


def run(directory,output):
    manifest,info,data=load_dataset(directory,KEYS)
    periods=split_days(common_days(data))
    market={}
    for key,bars in data.items():
        market[key]={b.time:(b,f) for b,f in zip(bars,features(bars))}
        market[key]['rules']=Rules.from_symbol(info['symbols'][key])
    output=Path(output)
    trials=fit(data,market,periods['development'],output/'sol-search')
    plan=trials['selected']['plan']
    study=deepcopy(STUDY)
    study.update(version='sol-targeted-1.0',sol_targeted=trials,
        rules=STUDY['rules']+' 仅SOL覆盖为开发段锁定参数：'+json.dumps(plan,ensure_ascii=False)+'。reference=ema只用EMA168，ema72为EMA72，blend为EMA168与VWAP20d算术平均，min为较低者；入场上限=参考−entry_atr×ATR，entry_atr=-0.25表示允许参考价上方0.25ATR噪声带，不是基本面低估。',
        sizing='SOL为7.5U等额或每折价2%增加7.5U，具体以锁定参数sizing为准，金额不设上限；可选RSI≤45。其他六个标的仍为7.5×(1+floor(折价/ATR))U，量价代理成立再乘1.5，完全不变。',
        flow=STUDY['flow']+' 本轮SOL不使用量价加权，其他六个标的保留该代理。',
        selection='SOL独立48组开发段固定候选，至少5笔成交且持仓数量≥该候选等最终预算普通定投的50%，优先资格后最大化SOL成本降幅；不使用全期、中段或末段排名。其他六个标的完全锁定；组合覆盖另行诊断。')
    study['limitations']=['本轮仅优化SOL，48组有限候选开发段搜索；首轮24组和全期结果已查看后扩大目录，存在研究者自由度，非独立验证。百分比分档不再用ATR波动单位无限放大档位。其他六个标的入场、额度、事件时点完全冻结。',
        '选择仅基于SOL开发段资格与成本；不是组合覆盖通过，旧方案SOL仅2次成交。全部80日之前已查看，不是新的独立样本外检验。']+[s for s in study['limitations'] if not s.startswith(('候选包含','默认只从'))]
    candidates={'opportunity_sol_previous':(2,None,'SOL优化前（其他六标的锁定）'),
                'opportunity_sol_fitted':(2,None,'SOL开发段拟合（其他六标的锁定）')}
    def selector(records):
        rows=[{'policy':k,**sol_quality(v['current'],v['four'])} for k,v in records.items()]
        # The actual fitted winner was already frozen by the development-only grid.
        row=next(r for r in rows if r['policy']=='opportunity_sol_fitted')
        return {'selected_policy':'opportunity_sol_fitted','ranking':rows,
            'eligible_count':int(row['eligible']),'selection_period':'development',
            'start':periods['development'][0][0],'end':periods['development'][-1][0],
            'independent_evidence':False,'status':'SOL开发段资格通过的有限候选首选；组合覆盖另行诊断，非未来最优' if row['eligible'] else 'SOL开发段无合格候选，仅研究回退，不宣称最佳'}
    report=evaluate(directory,output,candidates=candidates,opportunity_builder=targeted_factory(plan),study=study,selector=selector)
    print('SOL plan:',plan,'development gain:',trials['selected']['cost_gain_pct'],flush=True)
    return report


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',default='data/seven-assets-2026-11-01')
    parser.add_argument('--output',default='reports/sol-targeted-002')
    args=parser.parse_args()
    run(args.data,args.output)
