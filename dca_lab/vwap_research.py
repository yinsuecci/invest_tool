"""Development-only per-asset tier fitting and locked confirmation ablations."""

import json
import hashlib
from pathlib import Path
from .adaptive import freeze
from .config import ALL_ASSETS, Config
from .engine import Rules, day_groups
from .evaluate import SCENARIOS
from .indicators import features
from .market import HOUR, load_dataset
from .portfolio import blend, compare_portfolios, portfolio_simulate, trigger_diagnostics
from .schedule import daily_funding
from .signal_search import select_development, lock_selection
from .vwap_rules import LABELS, PROFILES, VWAP_POLICIES


def quality(current, four, once, minimum_fills=10):
    d = compare_portfolios(four, current)
    other = compare_portfolios(once, current)
    gains = [v for v in d['asset_cost_gain_pct'].values() if v is not None]
    checks = {'deployment': current['execution_rate'] >= .6,
              'fills': sum(x['fills'] for x in current['daily']) >= minimum_fills,
              'quantity': all(four['assets'][k]['quantity'] > 0 and a['quantity'] >= .5*four['assets'][k]['quantity'] for k,a in current['assets'].items()),
              'cost': len(gains) == len(current['assets'])}
    score = (min(d['equity_gain_pct_of_contributions'], other['equity_gain_pct_of_contributions'])
             + .25*(sum(gains)/len(gains) if gains else -100)
             - .5*max(0., -d['drawdown_delta_pp']) - 2*max(0., .8-current['execution_rate']))
    return {'score': score, 'eligible': all(checks.values()), 'checks': checks, 'execution_rate': current['execution_rate'],
            'cost_gain_pct': d['asset_cost_gain_pct'], 'equity_delta': d['equity_delta'], 'fills': sum(x['fills'] for x in current['daily']),
            'cash': current['cash'], 'assets': current['assets']}


def evaluate_vwap(directory, output, expanded=False, targeted_seed=None):
    from .vwap_search import catalogue, targeted_catalogue, joint_search, target_quality, rank
    expanded = expanded or targeted_seed is not None
    search_profiles = catalogue() if expanded else PROFILES
    keys = ('GOOG','XAU','SOL','BTC','ETH')
    manifest, info, datasets = load_dataset(directory, keys)
    config, weights = Config(daily_total=30, center=10, min_count=1, max_count=4), dict.fromkeys(keys,.2)
    market, eligible = {}, {}
    for key, bars in datasets.items():
        values = features(bars)
        market[key] = {b.time:(b,f) for b,f in zip(bars,values)}
        market[key]['rules'] = Rules.from_symbol(info['symbols'][key])
        eligible[key] = {d:[bars[i].time for i in idx] for d,idx in day_groups(bars,values,'rsi',config)
                         if values[idx[0]-1].volatility_multiple is not None and values[idx[0]-1].vwap20 is not None}
    dates = sorted(set.intersection(*(set(x) for x in eligible.values())))
    if len(dates) < 30:
        raise ValueError('Require at least30 common complete days; no proxies')
    selected = [(d,eligible[keys[0]][d]) for d in dates]
    funding = {d:daily_funding(blend({k:market[k][ts[0]-HOUR][1] for k in keys},weights),30,.3) for d,ts in selected}
    n = len(selected)
    splits = {'development':selected[:int(.6*n)], 'validation':selected[int(.6*n):int(.8*n)], 'holdout':selected[int(.8*n):], 'full':selected}
    seed_report, seed_digest = None, None
    active_assets = ('GOOG','XAU','SOL') if targeted_seed else keys
    protected = ('BTC','ETH') if targeted_seed else ()
    if targeted_seed:
        raw = Path(targeted_seed).read_bytes()
        seed_digest = hashlib.sha256(raw).hexdigest()
        seed_report = json.loads(raw)
        if ({k:v['sha256'] for k,v in manifest['assets'].items()} !=
                {k:v['sha256'] for k,v in seed_report['manifest']['assets'].items()} or
                seed_report['vwap']['fit_period'] != {'start':splits['development'][0][0],'end':splits['development'][-1][0]} or
                seed_report['config']['weights'] != weights):
            raise ValueError('Targeted seed must use identical assets, data hashes, weights and development dates')
        search_profiles = targeted_catalogue(seed_report['vwap']['fitted_plan'])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    protocol = freeze(output, {
        'version':'vwap-targeted-3.0' if targeted_seed else 'vwap-joint-tiers-2.1' if expanded else 'vwap-tiers-1.1', 'assets':list(keys), 'weights':weights, 'center':30, 'funding_deviation':.3,
        **({'targeted':{'seed_report_sha256':seed_digest,'active_assets':list(active_assets),'protected':list(protected),
                    'rule':'For targeted search only, freeze BTC/ETH profile parameters; require their development cost gain>=1% in every eligible candidate. Shared cash can still change all assets performance. Only closed RSI/EMA168/BB20 lower values; close AND next-open with slippage must pass minimum(VWAP tier cap, optional EMA168 discount, optional lower Bollinger). Missing required indicators reject.'}} if targeted_seed else {}),
        'specification_note':'In expanded mode, six-profile isolated fit initializes only. joint_search defines actual fitting, coverage, objective and mode selection, overriding legacy fitting/score descriptions below.' if expanded else 'Legacy isolated fit.',
        'joint_search': {'enabled':expanded, 'catalogue':search_profiles, 'rounds':2,
                         'selection':'Development only. Shared ledger, stable GOOG/XAU/SOL/BTC/ETH coordinate order. Eligible first; maximize worst asset cost gain minus1000*coverage shortfall, then mean cost gain, then equity. >=60% deployment, >=20 fills, every asset quantity>=50% ordinary-four. Finite search, no global optimum guarantee. Target>=1% for EVERY asset.'},
        'data_sha256':{k:v['sha256'] for k,v in manifest['assets'].items()}, 'profiles':PROFILES,
        'vwap':'Rolling480 consecutive closed hours, sum(HLC3*base volume)/sum(base volume); not session anchored, not exact trade-level VWAP. Reset at gaps; no zero-volume window trading.',
        'fitting':'Exactly6 profiles per asset, only first60% development. Isolated sleeve receives20% of the same global daily funding, weights1 for that asset; not a joint optimizer. No confirmation while fitting. Keep every trial. Lock profile IDs before subsequent confirmation and later-period analysis.',
        'fit_eligibility':'Deployment>=60%, fills>=10, quantity>=50% of isolated ordinary-four, finite asset costs.',
        'score':'min(equity gain % contributions vs four,once)+.25*mean asset cost gain % vs four-.5*max(0,-drawdown delta pp)-2*max(0,.8-deployment). Eligible first, descending score, fixed catalogue tie break. Research fallback if none eligible.',
        'order':'Closed depth=1-close/VWAP; choose deepest threshold reached. AboveVWAP no buy. Amount=current combined contribution*asset target weight*multiplier/(1+fee); amount not rounded up to minimum, may skip. Same next-open quote incl slippage must remain<=previous VWAP*(1-current-tier threshold). No intrabar-low fills.',
        'portfolio':'1--4 planned windows, 0--4 actual fills, global4h fill cooldown, pre-buy weight<=target+.15, largest quote deficit then closed VWAP discount depth then stable asset order. Day budget<=2*current contribution and available cash; no base, forced cash expiry, leverage or sales.',
        'confirmations':{'none':'No rebound filter: buy qualifying tier', 'bottom':'Closed sweep-reclaim or confirmed prior hammer; excludes RSI-only rebound', 'rsi':'Existing RSI14 rebound: min prior3<=35, current increase>=2 and close rising', 'either':'bottom OR rsi'},
        'ablation':'Fit once without confirmations; same frozen profiles used for bottom/RSI/either. All receive identical daily contributions. Compare asset cost AND quantities, cash, equity and matched-spend diagnostic. Fixed balanced_steady control isolates per-asset fitting.',
        'mode_selection':'Five VWAP portfolio variants, first60% only; same score with>=20 total fills,>=60% deployment, every asset>=50% ordinary-four quantity. Default is research selection, never live activation.',
        'reference':'https://www.tradingview.com/support/solutions/43000502018-volume-weighted-average-price-vwap/',
        'evidence':'All80 days previously exposed. Multiple testing and isolated-sleeve approximation. Retrospective exploration only, not fresh out-of-sample; no search-until-positive.'})
    fitting, plan = {}, {}
    dev = splits['development']
    for key in keys:
        sleeve = {d:funding[d]*weights[key] for d,_ in dev}
        isolated = {key:market[key]}
        four = portfolio_simulate(isolated,dev,sleeve,config,{key:1.},'four')
        once = portfolio_simulate(isolated,dev,sleeve,config,{key:1.},'once')
        rows = []
        for index,(name,profile) in enumerate(PROFILES.items()):
            current = portfolio_simulate(isolated,dev,sleeve,config,{key:1.},'vwap_fitted',vwap_plan={key:profile})
            rows.append({'profile':name,'catalogue_index':index,**quality(current,four,once)})
        rows.sort(key=lambda x:(not x['eligible'],-x['score'],x['catalogue_index']))
        plan[key] = PROFILES[rows[0]['profile']]
        fitting[key] = {'selected_profile':rows[0]['profile'], 'ranking':rows, 'eligible_count':sum(x['eligible'] for x in rows)}
        print(f'VWAP fit {key}: {rows[0]["profile"]}',flush=True)
    if seed_report:
        plan = seed_report['vwap']['fitted_plan']
    seed_plan = dict(plan)
    joint_trials = []
    if expanded:
        baseline = portfolio_simulate(market,dev,funding,config,weights,'four')
        plan, fitting, joint_trials = joint_search(market,dev,funding,config,weights,plan,baseline,search_profiles,
                                                 active_assets=active_assets,protected=protected)
        if targeted_seed:
            final_dev = portfolio_simulate(market,dev,funding,config,weights,'vwap_fitted',vwap_plan=plan)
            for key in protected:
                name = f'seed_{key}'
                row = {'profile':name,'catalogue_index':0,**target_quality(final_dev,baseline,protected=protected)}
                fitting[key] = {'selected_profile':name,'ranking':[row],'eligible_count':int(row['eligible']),'frozen':True}
        lock_selection(output,joint_trials,protocol['sha256'],'joint-trials.json')
    fit_lock = lock_selection(output, fitting, protocol['sha256'],'tier-fit-lock.json')
    fixed_plan = dict.fromkeys(keys,PROFILES['balanced_steady'])
    policies = {'mix_sizing':'上一轮：偏离与趋势额度', **LABELS}
    parents = {'mix_sizing':'four','vwap_fixed':'mix_sizing','vwap_fitted':'vwap_fixed', **dict.fromkeys(VWAP_POLICIES[2:],'vwap_fitted')}
    results, mode_selection = {}, None
    for period,days in splits.items():
        print(f'VWAP portfolio {period}: {len(days)} days',flush=True)
        cache = {p:portfolio_simulate(market,days,funding,config,weights,p,vwap_plan=fixed_plan if p=='vwap_fixed' else plan)
                 for p in (*policies,'four','once')}
        cache['fixed30'] = portfolio_simulate(market,days,{d:30. for d,_ in days},config,weights,'four')
        results[period] = {}
        for policy in policies:
            current = cache[policy]
            matched = portfolio_simulate(market,days,funding,config,weights,'four',budgets={d['date']:d['spent'] for d in current['daily']})
            results[period][policy] = {'current':current,'four':cache['four'],'once':cache['once'],'fixed30':cache['fixed30'],'matched':matched,
                                      'previous_policy':parents[policy], 'matched_is_ex_post':True, 'trigger_diagnostics':trigger_diagnostics(current,market),
                                      **{f'vs_{base}':compare_portfolios(cache[base],current) for base in ('four','once')},
                                      'vs_matched':compare_portfolios(matched,current),'vs_previous':compare_portfolios(cache[parents[policy]],current),
                                      'vs_unconfirmed':compare_portfolios(cache['vwap_fitted'],current)}
        if period=='development':
            mode_selection = select_development(results[period],VWAP_POLICIES)
            if expanded:
                rows = [{'policy':p, **target_quality(cache[p],cache['four'],protected=protected)} for p in VWAP_POLICIES]
                rows.sort(key=rank,reverse=True)
                mode_selection.update(selected_policy=rows[0]['policy'],ranking=rows,
                                      eligible_count=sum(r['eligible'] for r in rows),
                                      status='开发段最弱标的成本优先；所有标的便宜1%目标'+('达到（仅历史开发段）' if rows[0]['target_met'] else '未达到'))
            mode_selection['start'],mode_selection['end'] = days[0][0],days[-1][0]
            mode_selection['lock_sha256'] = lock_selection(output,mode_selection,protocol['sha256'],'mode-selection.json')
    stress = {}
    for policy in policies:
        stress[policy] = {}
        for scenario,(fee,slip) in SCENARIOS.items():
            current = portfolio_simulate(market,splits['holdout'],funding,config,weights,policy,fee,slip,vwap_plan=fixed_plan if policy=='vwap_fixed' else plan)
            stress[policy][scenario] = {p:compare_portfolios(portfolio_simulate(market,splits['holdout'],funding,config,weights,p,fee,slip),current) for p in ('four','once')}
    lo,hi = selected[0][1][0],selected[-1][1][-1]
    assets = {k:{'identity':ALL_ASSETS[k], 'candles':[[b.time,b.open,b.high,b.low,b.close,b.volume] for b in bars if lo<=b.time<=hi],
                 'indicators':[[b.time,f.ema72,f.ema168,f.rsi_value,f.bb_middle,f.bb_lower,f.bb_upper] for b in bars if lo<=b.time<=hi for f in [market[k][b.time][1]]],
                 'vwap_reference':[[b.time,market[k][b.time-HOUR][1].vwap20] for b in bars if lo<=b.time<=hi],
                 'vwap_profiles':{'fixed':fixed_plan[k],'fitted':plan[k]}} for k,bars in datasets.items()}
    decisions = {p:{'status':'未证明明显优势 / VWAP历史探索','live':False,'checks':{}} for p in policies}
    report = {'version':'0.12.0','scope':'portfolio','family':'vwap','manifest':manifest,'protocol':protocol,'assets':assets,
              'config':{'daily_center':30,'funding_deviation':.3,'lower':21,'upper':39,'weights':weights,'max_count':4,'fee_bps':10,'slip_bps':5},
              'chart_engine':'tradingview-lightweight-charts-5.2.1','policies':policies,'results':results,'stress':stress,'decisions':decisions,
              'default_policy':mode_selection['selected_policy'],'selection':mode_selection,
              'vwap':{'fitting':fitting,'fitted_plan':plan,'fixed_plan':fixed_plan,'fit_lock_sha256':fit_lock,'fit_period':{'start':dev[0][0],'end':dev[-1][0]},'sleeve_not_joint':not expanded,
                      'joint_search':expanded,'targeted':bool(targeted_seed),'protected':list(protected),'active_assets':list(active_assets),'catalogue_size':len(search_profiles),'trial_count':len(joint_trials)*len(search_profiles),
                      'search_note':'共享账本两轮坐标搜索；最后一轮各标的试验发生在各自当时的组合上下文，并非最终组合的独立增益。' if expanded else '隔离子账户拟合'},
              'limitations':['五个标的合计资金中心30U，范围21–39U，研究等权20%，不是每标的30U。全部现金与费用纳入净值。',
                             '20d为连续480小时的HLC3成交量加权近似VWAP，不是会话锚定VWAP，也不是逐笔精确成交VWAP。只用上根闭合小时计算。',
                             '每标的仅6个预设档位方案，开发段隔离子账户按组合新增的20%拟合；它不是共享账本的联合最优，所有试验保留。',
                             '先冻结每标的档位，再用同一档位比较无确认、探底K线、RSI及两者择一。确认增益不能只看均价，需要同时检查数量、现金、净值和未成交。',
                             '不强制基础买入或到期补投；实际每天0–4笔，成功成交至少间隔4小时，单日支出最多当天新增两倍且不超过已有现金。最低金额会阻止小额档位成交，不为了成交擅自加大金额。',
                             '上根收盘所在档位决定目标额度；下根开盘含滑点仍须不高于同档位上限。不用当前小时最低价、最高价或未来成交量挑买点。',
                             '大跌大买指条件目标额度递增，不保证实际成交金额递增，实际还受现金、日预算、权重和交易过滤器限制。持续下跌可能越买越亏。',
                             '共同历史仍只有80日且已经看过，48日开发、16日中段、16日末段不是独立样本外证据。各段重置现金与持仓，不能把分段差相加代替全程结果。',
                             '确认方式也只按开发段评分选择默认展示，回测多重选择偏差仍存在。不启用实盘，不宣称未来最优。',
                             '普通1次/最多4次接收完全相同资金流；同动用预算对照按全天实际支出事后配平，仅诊断，网格舍入使实际支出仍有差异。',
                             '上一轮三/五标的实验目录完整保留，当前仅带入mix_sizing作参考；未覆盖旧输出。费用、滑点和规则快照为简化假设，未计分红、公司行动和现金收益。']}
    if expanded:
        report['limitations'] = [x for x in report['limitations'] if '仅6个' not in x and '开发段评分选择默认' not in x]
        report['limitations'].insert(0, f'本轮{len(search_profiles)}组档位、两轮共享账本坐标搜索，共{len(joint_trials)*len(search_profiles)}次联合试验。只优化开发段最弱标的成本，非全局最优；不承诺全部便宜1%。完整试验见joint-trials.json。')
        for period, records in results.items():
            for policy, record in records.items():
                record['target_quality'] = target_quality(record['current'],record['four'],protected=protected)
        report['vwap']['seed_comparison'] = {}
        report['vwap']['filter_ablation'] = {}
        for period, days in splits.items():
            seed_result = portfolio_simulate(market,days,funding,config,weights,'vwap_fitted',vwap_plan=seed_plan)
            baseline = results[period]['vwap_fitted']['four']
            report['vwap']['seed_comparison'][period] = compare_portfolios(baseline,seed_result)
            if targeted_seed:
                stripped = {k:{p:v for p,v in profile.items() if p in ('discounts','multipliers')} for k,profile in plan.items()}
                no_filter = portfolio_simulate(market,days,funding,config,weights,'vwap_fitted',vwap_plan=stripped)
                selected_result = results[period]['vwap_fitted']['current']
                report['vwap']['filter_ablation'][period] = {'comparison':compare_portfolios(no_filter,selected_result),
                    'no_filter_quality':target_quality(no_filter,baseline,protected=protected),
                    'quantity_ratio':{k:selected_result['assets'][k]['quantity']/no_filter['assets'][k]['quantity'] if no_filter['assets'][k]['quantity'] else None for k in keys},
                    'cash_delta':selected_result['cash']-no_filter['cash']}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
    return report
