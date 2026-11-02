"""Seven-asset extension with a finite, development-only indicator ablation."""

import json
from pathlib import Path

from .adaptive import freeze
from .config import ALL_ASSETS, Config
from .engine import Rules
from .indicators import features
from .ma_tiers import common_days, split_days
from .market import load_dataset
from .portfolio import compare_portfolios, trigger_diagnostics
from .report import render_report
from .signal_search import lock_selection
from .smart_dca import simulate_clock, ideal_comparison
from .vwap_search import target_quality, rank
from .vwap_unlimited import ordinary_final_budget

KEYS = ('GOOG','XAU','SOL','BTC','ETH','SPY','QQQ')
CANDIDATES = {
    'inverse_1': (1,None,'反比额度 rho=1'),
    'inverse_2': (2,None,'反比额度 rho=2（原策略）'),
    'inverse_2_rsi': (2,'rsi','rho=2 + RSI14≤50'),
    'inverse_2_bb': (2,'bb','rho=2 + 布林下轨'),
    'inverse_2_ema': (2,'ema','rho=2 + EMA168下方'),
    'inverse_2_bottom': (2,'bottom','rho=2 + 探底K线确认'),
}


def choose(development):
    rows = [{'policy':k,**v['target_quality']} for k,v in development.items()]
    rows.sort(key=rank,reverse=True)
    current = development[rows[0]['policy']]['current']
    return {'selected_policy':rows[0]['policy'],'ranking':rows,
            'eligible_count':sum(r['eligible'] for r in rows),'selection_period':'development',
            'start':current['start'],'end':current['end'],'independent_evidence':False,
            'status':'有限候选开发段研究首选，非长期最优或独立验证'}


def evaluation_periods(days, study=None):
    periods = split_days(days)
    if study and ('sol_targeted' in study or 'independent_assets' in study):
        periods['full'] = periods['validation'] + periods['holdout']
    return periods


def evaluate(directory, output, candidates=None, opportunity_builder=None, study=None, selector=None):
    candidates = candidates or CANDIDATES
    manifest,info,data = load_dataset(directory,KEYS)
    for key in KEYS:
        symbol = info['symbols'][key]
        if symbol['status']!='TRADING' or not symbol.get('isSpotTradingAllowed'):
            raise ValueError('Verified spot instrument unavailable: '+key)
    days = common_days(data)
    if len(days)<30:
        raise ValueError('Insufficient common real history after 800-hour warmup')
    periods = evaluation_periods(days, study)
    market, asset_payload = {},{}
    first,last = days[0][1][0],days[-1][1][-1]
    for key,bars in data.items():
        fs = features(bars)
        market[key] = {b.time:(b,f) for b,f in zip(bars,fs)}
        market[key]['rules'] = Rules.from_symbol(info['symbols'][key])
        pairs = [(b,f) for b,f in zip(bars,fs) if first<=b.time<=last]
        asset_payload[key] = {'identity':ALL_ASSETS[key],
            'candles':[[b.time,b.open,b.high,b.low,b.close,b.volume] for b,f in pairs],
            'indicators':[[b.time,f.ema72,f.ema168,f.rsi_value,f.bb_middle,f.bb_lower,f.bb_upper] for b,f in pairs],
            'vwap_reference':[[b.time,f.vwap20] for b,f in pairs]}
    output = Path(output)
    output.mkdir(parents=True,exist_ok=True)
    protocol = freeze(output,{'version':'seven-smart-1.0','keys':KEYS,
        'data_sha256':{k:manifest['assets'][k]['sha256'] for k in KEYS},'candidates':candidates,
        'clock':'UTC+8 00/06/12/18; exactly four global opportunities/day, stable seven-asset rotation. Rejected signals do not create replacement orders.',
        'sizing':study['sizing'] if study else '7.5*(fixed pre-segment close with slippage / observed next open with slippage)^rho; no cash ceiling. Exact valid fill cost is external capital, not profit.',
        'confirmation':study['rules']+' '+study['flow']+' '+study['batching'] if study else 'Previous closed-hour RSI14<=50; or both closed price and executable quote<=BB20 lower / EMA168; or previous closed-hour sweep-reclaim / confirmed hammer. No future candle extremes in decisions.',
        'selection':study['selection'] if study else '60/20/20 common-day split. Development only; existing deployment, quantity and global fill gates plus >=5 successful fills per asset. Rank eligible, worst cost gain, mean cost gain, equity delta. Six candidates fixed before run.',
        'control':'Per-asset final actual gross budget divided by segment days. Small budgets accumulate to exchange minimum. Separate identical-clock paired control. Same final cash amounts, NOT same funding timing.',
        'evidence':'Historical retrospective research. Data already exposed; no independent evidence or guarantee. 10bp fees, 5bp assumed slippage; no dividends, spread, market impact or future rule changes.',
        **({'opportunity_study':study} if study else {})})
    config = Config(daily_total=30,center=10,min_count=1,max_count=4)
    report = {'version':'0.16.0','scope':'portfolio','family':'smart','manifest':manifest,
        'protocol':protocol,'assets':asset_payload,'chart_engine':True,
        'config':{'daily_center':30,'lower':0,'upper':None,'funding_deviation':None,
                  'weights':dict.fromkeys(KEYS,1/len(KEYS)),'max_count':4,'fee_bps':10,'slip_bps':5},
        'policies':{k:v[2] for k,v in candidates.items()},'results':{},'stress':{},
        'budget_matching_by_policy':{},'budget_matching':{},'smart':{},'execution_mode':'on_demand',
        'execution_ablation':{},'smart_selection':{'method':'development_minimum_cost'},
        'indicator_ablation':{'rules':{'rsi':'闭合小时RSI14≤50','bb':'闭合价与可执行价均≤BB20下轨',
            'ema':'闭合价与可执行价均≤EMA168','bottom':'前一闭合小时扫底收回或锤子确认'},
            'confirmations':{k:v[1] for k,v in candidates.items()}},
        'decisions':{k:{'status':'研究候选，未启用实盘','live':False,'checks':{}} for k in candidates}}
    if study:
        report['opportunity_study'] = study
    for period,segment in periods.items():
        records,matching,diagnostics = {},{},{}
        for policy,(rho,confirmation,label) in candidates.items():
            opportunity = opportunity_builder(data,market,policy) if opportunity_builder and policy.startswith('opportunity_') else None
            current = simulate_clock(market,segment,'inverse',{},rho=rho,confirmation=confirmation,opportunity=opportunity)
            amounts = {k:a['spent']+a['fees'] for k,a in current['assets'].items()}
            four = ordinary_final_budget(market,segment,amounts,config,'four',accumulate_small=True)
            once = ordinary_final_budget(market,segment,amounts,config,'once',accumulate_small=True)
            paired = simulate_clock(market,segment,'paired',{},allocations=amounts)
            quality = target_quality(current,four)
            fill_counts = {k:sum(o['asset']==k and o['quantity']>0 for o in current['orders']) for k in KEYS}
            quality['checks']['per_asset_fills'] = min(fill_counts.values())>=5
            quality['eligible'] = all(quality['checks'].values())
            quality['target_met'] = quality['eligible'] and quality['minimum_cost_gain_pct']>=1
            quality['per_asset_fills'] = fill_counts
            quality['score'] -= 1000*sum(max(0,5-n)/5 for n in fill_counts.values())
            records[policy] = {'current':current,'four':four,'once':once,'matched':paired,
                'fixed30':four,'matched_is_ex_post':True,'previous_policy':'four',
                'vs_four':compare_portfolios(four,current),'vs_once':compare_portfolios(once,current),
                'vs_matched':compare_portfolios(paired,current),'vs_previous':compare_portfolios(four,current),
                'vs_unconfirmed':compare_portfolios(current,current),'target_quality':quality,
                'trigger_diagnostics':trigger_diagnostics(current,market)}
            matching[policy] = {'total_budget':sum(amounts.values()),'ordinary_daily':sum(amounts.values())/len(segment),
                'assets':{k:{'final_budget':a,'ordinary_daily':a/len(segment),
                    'four_actual_spend':four['assets'][k]['spent']+four['assets'][k]['fees'],
                    'four_cash':a-four['assets'][k]['spent']-four['assets'][k]['fees']} for k,a in amounts.items()}}
            diagnostics[policy] = {'ideal':ideal_comparison(current) if confirmation is None and opportunity is None else {},
                'paired_cost_gain_pct':records[policy]['vs_matched']['asset_cost_gain_pct']}
        report['results'][period],report['budget_matching_by_policy'][period],report['smart'][period] = records,matching,diagnostics
        if period=='development':
            selectable = {k:r for k,r in records.items() if k.startswith('opportunity_')} if study else records
            report['selection'] = selector(records) if selector else choose(selectable)
            if not report['selection']['eligible_count']:
                report['selection']['status'] = '无合格候选；仅展示开发段覆盖惩罚最小的机会单研究回退，不宣称最佳'
            report['selection']['lock_sha256'] = lock_selection(output,report['selection'],protocol['sha256'])
        print(period+': '+str(len(segment))+' days evaluated',flush=True)
    selected = report['selection']['selected_policy']
    report['default_policy'] = selected
    report['budget_matching'] = {p:m[selected] for p,m in report['budget_matching_by_policy'].items()}
    report['limitations'] = [
        '新增SPYBUSDT为标普500 ETF代币，QQQBUSDT为纳斯达克100 ETF代币，不是纳斯达克综合指数、原生ETF份额或合约；地区与账户可用性未验证。',
        '七标的沿原五标的顺序追加SPY/QQQ，每日合计4个候选窗口，过滤可使实际成交为0；不是每标的4次。无强制补单、资金截断或追加上限，30U仅为额度尺度。',
        '重新计算真实小时数据、800小时预热后的共同完整日，按60%/20%/20%分段且各段重置；不得把新七标的结果与旧五标的结果当作同一实验。',
        '六种预先固定候选只在开发段选择，增加每标的至少5笔门槛，避免靠几乎不买入宣称更便宜；过滤版的纯反比理想公式不适用。',
        '普通对照每标的最终实际含费投入÷天数，微额累积到最小金额，现金计入净值；最终预算相同、入金时点不同。fixed30选项已禁用，不提供伪造的固定30U对照。',
        '不使用跨标的仓位门控。七等权仅为展示研究假设；轮转事件均等不代表最终资金等权。最低金额拒单会显著影响表面成本优势。',
        '10bp手续费和5bp滑点为保守研究假设，未计分红、真实点差、冲击与历史规则变化。无限追加可能带来较大资金需求，未接实盘或自动转账。',
        '历史已被反复查看，开发与后段展示均不能当作新的独立证据；不保证所有标的便宜1%或未来盈利。']
    if study:
        report['limitations'] = study['limitations'] + [s for s in report['limitations'] if not s.startswith('六种')]
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    render_report(report,output)
    return report


if __name__=='__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--data',default='data/seven-assets-2026-11-01')
    parser.add_argument('--output',default='reports/smart-seven-001')
    args = parser.parse_args()
    evaluate(args.data,args.output)
