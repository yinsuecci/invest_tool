"""Independent, development-only asset fits, replayed on the shared global clock."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from .accumulation import STUDY, factory
from .adaptive import freeze
from .config import Config
from .engine import Rules
from .indicators import features
from .ma_tiers import common_days, split_days
from .market import HOUR, load_dataset
from .seven_smart import KEYS, evaluate
from .sol_research import CATALOGUE as SOL_CATALOGUE, sol_decide
from .smart_dca import simulate_clock
from .vwap_unlimited import ordinary_final_budget

CATALOGUE = tuple({**plan,'next_atr':gap} for plan in SOL_CATALOGUE for gap in (.25,.75))
PREVIOUS = 'opportunity_assets_previous'
FITTED = 'opportunity_assets_fitted'


def asset_decide(bar,feature,quote,last,time,plan):
    result=sol_decide(bar,feature,quote,None,time,plan)
    if result['reset_episode']:
        last=None
    result['batch']=0 if last is None else last['level']+1
    if last and result['atr14'] is not None:
        result['cap']=min(result['cap'],last['price']-plan['next_atr']*result['atr14'])
        if result['hit'] and (time-last['time']<24*HOUR or quote>result['cap']):
            result.update(hit=False,reason='wait_next_lower_batch')
    result.pop('sol_plan')
    result['asset_plan']=plan
    return result


def asset_quality(current,baseline,key):
    a,b=current['assets'][key],baseline['assets'][key]
    fills=sum(o['asset']==key and o['quantity']>0 for o in current['orders'])
    gain=(1-a['unit_cost']/b['unit_cost'])*100 if a['unit_cost'] and b['unit_cost'] else None
    ratio=a['quantity']/b['quantity'] if b['quantity'] else 0.
    checks={'fills':fills>=5,'quantity':ratio>=.5,'cost':gain is not None}
    value=gain if gain is not None else -100.
    return {'eligible':all(checks.values()),'checks':checks,'fills':fills,'quantity_ratio':ratio,
        'cost_gain_pct':gain,'score':value-1000*(max(0,5-fills)/5+max(0,.5-ratio)+(gain is None)),
        'unit_cost':a['unit_cost'],'ordinary_cost':b['unit_cost'],
        'budget':a['spent']+a['fees'],'quantity':a['quantity']}


def choose(rows):
    return sorted(rows,key=lambda r:(r['eligible'],r['score'],-r['index']),reverse=True)


def incumbent_builder(sol_plan):
    original=factory()
    def build(data,market,policy=None):
        base=original(data,market,'opportunity_flow_boost')
        def signal(key,time,quote,last):
            if key!='SOL':
                return base(key,time,quote,last)
            bar,f=market[key][time-HOUR]
            return sol_decide(bar,f,quote,last,time,sol_plan)
        return signal
    return build


def fitted_builder(plans,sol_plan):
    original=incumbent_builder(sol_plan)
    def build(data,market,policy):
        previous=original(data,market)
        if policy==PREVIOUS:
            return previous
        def signal(key,time,quote,last):
            plan=plans[key]
            if plan.get('mode')=='incumbent':
                return previous(key,time,quote,last)
            bar,f=market[key][time-HOUR]
            return asset_decide(bar,f,quote,last,time,plan)
        return signal
    return build


def fit_assets(data,market,development,sol_plan,output,hashes,source_hash):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    protocol=freeze(output,{'version':'independent-assets-1.0','catalogue':CATALOGUE,
        'incumbent':{'SOL':sol_plan,'other_six':'Frozen opportunity_flow_boost'},
        'data_sha256':hashes,'previous_report_sha256':source_hash,
        'clock':KEYS,'scope':'One asset at a time, projecting its EXACT original seven-asset events. Four total global events/day, no rescheduling or extra fills.',
        'selection':'Per asset, development48 only. At least5 successful fills and quantity>=50% ordinary same final gross budget. Eligible first, highest cost reduction, stable catalogue index. Incumbent index0 wins exact ties. 96 fixed variants plus incumbent per asset, 679 total trials. No validation/holdout/full results used for ranking.',
        'execution':'Same production simulator, quantity/minimum rules,10bp fee,5bp assumed slippage. Equal7.5 or7.5*(1+floor(discount/.02)) gross, no cash ceiling, no base orders.',
        'evidence':'All historical data and prior iterations exposed; retrospective parameter search, NOT fresh independent OOS proof.',
        'start':development[0][0],'end':development[-1][0]})
    previous=incumbent_builder(sol_plan)(data,market)
    config=Config(daily_total=30,center=10,min_count=1,max_count=4)
    study={'protocol_sha256':protocol['sha256'],'catalogue_size':len(CATALOGUE)+1,
        'trial_count':len(KEYS)*(len(CATALOGUE)+1),'fitting':{},'plans':{},
        'start':development[0][0],'end':development[-1][0],'independent_evidence':False}
    for key in KEYS:
        sleeve={key:market[key]}
        rows=[]
        baseline_cache={}
        for index,plan in enumerate(({'mode':'incumbent'},)+CATALOGUE):
            def signal(asset,time,quote,last):
                if plan.get('mode')=='incumbent':
                    return previous(asset,time,quote,last)
                bar,f=market[asset][time-HOUR]
                return asset_decide(bar,f,quote,last,time,plan)
            current=simulate_clock(sleeve,development,'inverse',{},rho=2,opportunity=signal,clock_keys=KEYS)
            a=current['assets'][key]
            amount=a['spent']+a['fees']
            if amount not in baseline_cache:
                baseline_cache[amount]=ordinary_final_budget(sleeve,development,{key:amount},config,'four',accumulate_small=True)
            rows.append({'index':index,'plan':plan,**asset_quality(current,baseline_cache[amount],key)})
        ranked=choose(rows)
        winner=ranked[0]
        study['fitting'][key]={'selected':winner,'ranking':ranked,
            'incumbent':rows[0],'eligible_count':sum(r['eligible'] for r in rows)}
        study['plans'][key]=winner['plan']
        print(f'{key}: development cost gain {winner["cost_gain_pct"]}; fills {winner["fills"]}; plan {winner["plan"]}',flush=True)
    (output/'asset-trials.json').write_text(json.dumps(study,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return study


def combined_selection(development):
    rows=[{'policy':k,**r['target_quality']} for k,r in development.items()]
    selected=development[FITTED]
    eligible=selected['target_quality']['eligible']
    return {'selected_policy':FITTED,'ranking':rows,'eligible_count':int(eligible),
        'selection_period':'development','start':selected['current']['start'],'end':selected['current']['end'],
        'independent_evidence':False,'status':'逐标的独立开发段锁参，组合覆盖通过，非未来最优' if eligible else '逐标的研究拟合；组合覆盖未通过，不宣称最优'}


def run(directory,source,output):
    raw=Path(source).read_bytes()
    prior=json.loads(raw)
    manifest,info,data=load_dataset(directory,KEYS)
    hashes={k:manifest['assets'][k]['sha256'] for k in KEYS}
    if hashes!={k:prior['manifest']['assets'][k]['sha256'] for k in KEYS}:
        raise ValueError('Compare the identical frozen dataset, not a changed sample')
    sol_plan=prior['opportunity_study']['sol_targeted']['selected']['plan']
    market={}
    for key,bars in data.items():
        market[key]={b.time:(b,f) for b,f in zip(bars,features(bars))}
        market[key]['rules']=Rules.from_symbol(info['symbols'][key])
    periods=split_days(common_days(data))
    selection=prior.get('selection',{})
    if selection.get('start')!=periods['development'][0][0] or selection.get('end')!=periods['development'][-1][0]:
        raise ValueError('Source selection window must match the current development window')
    fits=fit_assets(data,market,periods['development'],sol_plan,Path(output)/'asset-search',hashes,hashlib.sha256(raw).hexdigest())
    study=deepcopy(STUDY)
    study.update(version='independent-assets-1.0',independent_assets=fits,
        rules='七个标的独立选择参考价：EMA72、EMA168、VWAP20d与EMA168较低者、二者平均。闭合价和可执行报价均≤参考−entry_atr×ATR才可买入；-0.25允许上方0.25ATR噪声带，0/0.25为参考以下。可加RSI≤45，不买时没有基础单。保留当前策略候选，具体以各自锁参为准。',
        batching='每个标的独立批次状态；同轮后续成交价≤上次成交价−next_atr×ATR，next_atr=0.25或0.75，冷却24小时。前一闭合价>参考+0.5ATR才重置。保留方案沿用原0.5ATR规则。全组合每天仍只有四个轮转窗口。',
        sizing='每标的分别选择7.5U等额或7.5×(1+floor(相对折价/2%))U分档，无金额和入金上限、无预算截断。保留旧量价方案时仍用原ATR分档与代理加权。',
        flow=STUDY['flow']+' 新参数候选不使用量价加权；仅保留旧方案候选时沿用。',
        selection='每标的96组固定候选+1个当前方案，合计679次，仅开发段独立排名；至少成交5笔、持仓数量≥同最终预算普通定投50%。先资格再最大化自身成本降幅，同分优先当前方案；合并后复核同一组合。')
    study['limitations']=['每个标的独立调参，并未优化权重或增加买入时刻；每日合计最多4个候选机会，允许零成交。当前方案也进入候选，避免在同资格下开发段无谓回退。',
        '679次历史搜索带来多重尝试偏差；全部80日与前轮结果都已查看，中段、末段不参与排名，但不是新的独立样本外证据，不保证未来胜过普通定投。',
        '开发段仅48日，每资产约27–28个候选事件，至少5笔是最低覆盖门槛，仍可能样本稀疏；必须同时查看数量、现金与后段成本。',
        '新方案可能选择等额机会单而不是大跌加倍，也可能保留原方案；不为追求全期漂亮而手工更改后段或失败标的的参数。']+[s for s in STUDY['limitations'] if not s.startswith(('候选包含','默认只从','同一折价轮次'))]
    candidates={PREVIOUS:(2,None,'优化前：仅SOL已独立拟合'),FITTED:(2,None,'七标的分别独立拟合')}
    report=evaluate(directory,output,candidates=candidates,opportunity_builder=fitted_builder(fits['plans'],sol_plan),study=study,selector=combined_selection)
    for key in KEYS:
        development=report['results']['development'][FITTED]
        expected=fits['fitting'][key]['selected']
        actual=asset_quality(development['current'],development['four'],key)
        if abs(actual['budget']-expected['budget'])>1e-7 or actual['fills']!=expected['fills']:
            raise AssertionError('Independent fit / shared replay mismatch: '+key)
    return report


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',default='data/seven-assets-2026-11-01')
    parser.add_argument('--source',default='reports/sol-targeted-002/report.json')
    parser.add_argument('--output',default='reports/independent-assets-001')
    args=parser.parse_args()
    run(args.data,args.source,args.output)
