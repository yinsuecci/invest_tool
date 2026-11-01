"""Bounded shared-ledger coordinate search; development data only."""

from .portfolio import compare_portfolios, portfolio_simulate
from .vwap_rules import PROFILES


def catalogue():
    profiles = dict(PROFILES)
    for start in (0., .005, .01, .02, .03, .05, .08, .12):
        for gap in (.0025, .005, .01, .02, .04):
            for name, sizes in (('steady', [1., 2., 3., 4.]),
                                ('strong', [1., 2., 4., 6.]),
                                ('large', [2., 3., 5., 8.]),
                                ('front', [4., 5., 6., 8.]),
                                ('max', [8., 10., 12., 16.])):
                key = f's{start:g}_g{gap:g}_{name}'
                profiles[key] = {'discounts': [start, start+gap, start+3*gap, start+6*gap],
                                 'multipliers': sizes}
    return profiles


def targeted_catalogue(seed):
    profiles = {f'seed_{k}':p for k,p in seed.items()}
    filters = {'none':{}, 'rsi40':{'rsi_max':40}, 'rsi50':{'rsi_max':50},
               'ema0':{'ema_discount':0.}, 'ema1':{'ema_discount':.01},
               'bb':{'bb_cap':True}, 'ema_rsi':{'ema_discount':.005,'rsi_max':45}}
    for start in (0., .005, .01, .02, .03, .05):
        for gap in (.0025, .005, .01):
            for name, sizes in (('large',[2.,3.,5.,8.]),('front',[4.,5.,6.,8.])):
                for filter_name, rule in filters.items():
                    profiles[f's{start:g}_g{gap:g}_{name}_{filter_name}'] = {
                        'discounts':[start,start+gap,start+3*gap,start+6*gap],
                        'multipliers':sizes, **rule}
    return profiles


def target_quality(current, baseline, minimum_fills=20, protected=()):
    comparison = compare_portfolios(baseline, current)
    gains = comparison['asset_cost_gain_pct']
    ratios = [current['assets'][k]['quantity']/a['quantity'] if a['quantity'] else 0.
              for k, a in baseline['assets'].items()]
    fills = sum(d['fills'] for d in current['daily'])
    checks = {'deployment': current['execution_rate'] >= .6, 'fills': fills >= minimum_fills,
              'quantity': min(ratios) >= .5, 'cost': all(v is not None for v in gains.values())}
    values = [v if v is not None else -100. for v in gains.values()]
    penalty = (max(0., .6-current['execution_rate']) + max(0., .5-min(ratios))
               + max(0., 1-fills/minimum_fills) + sum(v is None for v in gains.values()))
    if protected:
        checks['protected'] = all(gains[k] is not None and gains[k] >= 1. for k in protected)
        penalty += sum(max(0.,1.-(gains[k] if gains[k] is not None else -100.))/100 for k in protected)
    score = min(values)-1000*penalty
    return {'score': score, 'eligible': all(checks.values()), 'checks': checks,
            'execution_rate': current['execution_rate'], 'fills': fills, 'cost_gain_pct': gains,
            'equity_delta': comparison['equity_delta'], 'cash': current['cash'],
            'assets': current['assets'], 'minimum_cost_gain_pct': min(values),
            'target_met': all(checks.values()) and min(values) >= 1.,
            'mean_cost_gain_pct': sum(values)/len(values)}


def rank(row):
    return (row['eligible'], row['score'], row['mean_cost_gain_pct'], row['equity_delta'])


def joint_search(market, days, funding, config, weights, seed, baseline, profiles, rounds=2, active_assets=None, protected=()):
    plan = dict(seed)
    trials, fitting = [], {}
    for iteration in range(rounds):
        for asset in (active_assets if active_assets is not None else weights):
            rows = []
            context = {k: v for k, v in plan.items()}
            for index, (name, profile) in enumerate(profiles.items()):
                candidate = {**plan, asset: profile}
                result = portfolio_simulate(market, days, funding, config, weights,
                                            'vwap_fitted', vwap_plan=candidate)
                row = {'asset': asset, 'round': iteration+1, 'profile': name,
                       'catalogue_index': index, **target_quality(result, baseline,protected=protected)}
                rows.append(row)
            rows.sort(key=rank, reverse=True)
            winner = rows[0]
            plan[asset] = profiles[winner['profile']]
            fitting[asset] = {'selected_profile': winner['profile'], 'ranking': rows,
                              'eligible_count': sum(r['eligible'] for r in rows)}
            trials.append({'round': iteration+1, 'asset': asset, 'context': context,
                           'selected_profile': winner['profile'], 'ranking': rows})
            print(f'Joint round {iteration+1} {asset}: {winner["profile"]}; worst cost gain {winner["minimum_cost_gain_pct"]:.3f}%', flush=True)
    return plan, fitting, trials
