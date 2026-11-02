import json
from itertools import product
from math import floor, isfinite
from pathlib import Path

from .config import ALL_ASSETS
from .adaptive import freeze
from .engine import Rules
from .indicators import features
from .ma_tiers import common_days
from .market import HOUR, iso, load_dataset
from .portfolio import compare_portfolios
from .seven_smart import KEYS
from .signal_search import lock_selection

CATALOGUE = tuple(
    {"reference": reference, "discount": discount, "confirmation": confirmation, "sizing": sizing}
    for reference, discount, confirmation, sizing in product(
        ("ema", "blend"), (0., .01, .03), ("none", "rsi"), ("equal", "tiers")
    )
)
MIN_DEPLOYMENT = .9


def prepare(data, info):
    market = {}
    for key, bars in data.items():
        market[key] = {b.time: (b, f) for b, f in zip(bars, features(bars))}
        market[key]["rules"] = Rules.from_symbol(info["symbols"][key])
    return market


def decision(bar, feature, quote, last, time, plan, base):
    if feature is None or feature.count_1h < 800 or feature.ema168 is None or feature.vwap20 is None:
        return None
    if bar.volume <= 0:
        return None
    reference = feature.ema168 if plan["reference"] == "ema" else (feature.ema168 + feature.vwap20) / 2
    cap = reference * (1 - plan["discount"])
    if max(bar.close, quote) > cap:
        return None
    if plan["confirmation"] == "rsi" and (feature.rsi_value is None or feature.rsi_value > 45):
        return None
    if last and time - last["time"] < 24 * HOUR:
        return None
    depth = max(0., 1 - max(bar.close, quote) / reference)
    multiplier = 1 + floor((depth + 1e-12) / .02) if plan["sizing"] == "tiers" else 1
    return {"gross": base * multiplier, "cap": cap, "depth": depth,
            "reference": reference, "signal_time": iso(bar.time + HOUR)}


def simulate(market, days, daily_total=30., plans=None, schedule=None, ordinary=None, max_fills=4, base_amount=None):
    if not days or not isfinite(daily_total) or daily_total <= 0:
        raise ValueError("Positive daily funding and complete days required")
    keys = tuple(market)
    cash, units, spent, fees = [dict.fromkeys(keys, 0.) for _ in range(4)]
    total = 0.
    nav = peak = 1.
    drawdown = 0.
    previous = None
    last, orders, daily, curve = {}, [], [], []
    fee, slip = .001, .0005
    events = {row["test_start"]: row for row in schedule or []}
    active = None

    def value(prices):
        return sum(cash.values()) + sum(units[k] * prices[k] for k in keys)

    def revalue(before, after):
        nonlocal nav, peak, drawdown
        if before > 0:
            nav *= after / before
            peak = max(peak, nav)
            drawdown = min(drawdown, nav / peak - 1)

    for date, times in days:
        if len(times) != 24:
            raise ValueError("Complete trading days required")
        if times[0] in events:
            active = events[times[0]]
            if active["train_end"] >= active["test_start"]:
                raise ValueError("Invalid plan cutoff")
        prior_nav, paid_day, count = nav, 0., 0
        opens = {k: market[k][times[0]][0].open for k in keys}
        if previous is not None:
            revalue(previous, value(opens))
        for key in keys:
            cash[key] += daily_total / len(keys)
        total += daily_total
        previous = value(opens)
        ordinary_plans = {}
        if ordinary:
            for key in keys:
                rules = market[key]['rules']
                prior_price = market[key][times[0] - HOUR][0].close
                minimum = max(rules.min_notional, rules.min_qty * prior_price) + float(rules.step) * prior_price
                n = 1 if ordinary == 'once' else max(1, min(4, int(cash[key] / max(minimum * (1 + fee), 1e-9))))
                ordinary_plans[key] = {'hours': tuple(i * 24 // n for i in range(n)), 'gross': cash[key] / n}
        for hour, time in enumerate(times):
            opens = {k: market[k][time][0].open for k in keys}
            before = value(opens)
            revalue(previous, before)
            slots = tuple(range(24)) if ordinary else (0, 6, 12, 18)
            candidates = []
            if hour in slots:
                for key in keys:
                    bar, feature = market[key][time - HOUR]
                    quote = opens[key] * (1 + slip)
                    if ordinary:
                        if hour not in ordinary_plans[key]['hours']:
                            continue
                        signal = {"gross": ordinary_plans[key]['gross'], "cap": None, "depth": 0.,
                                  "signal_time": iso(time)}
                        if bar.volume <= 0:
                            continue
                    else:
                        if schedule and active is None:
                            raise ValueError("No frozen plan for trade")
                        plan = active["plans"][key] if schedule else plans[key]
                        signal = decision(bar, feature, quote, last.get(key), time, plan,
                                          daily_total / 4 if base_amount is None else base_amount)
                        if signal is None:
                            continue
                    rules = market[key]["rules"]
                    quantity = rules.quantity(signal["gross"] / (1 + fee), quote)
                    notional = quantity * quote
                    charge = notional * fee
                    if quantity <= 0 or quantity < rules.min_qty or quantity > rules.max_qty:
                        continue
                    if notional < rules.min_notional - 1e-8 or notional > rules.max_notional:
                        continue
                    if notional + charge > cash[key] + 1e-8:
                        continue
                    candidates.append((key, signal, quantity, quote, notional, charge))
                candidates.sort(key=lambda x: (-cash[x[0]], -x[1]["depth"], keys.index(x[0])))
                chosen = candidates if ordinary else candidates[:1] if count < max_fills else []
                for key, signal, quantity, quote, notional, charge in chosen:
                    if market[key][time][0].volume <= 0:
                        orders.append({"day": date, "time": iso(time), "asset": key,
                                       "quantity": 0., "price": quote, "notional": 0., "fee": 0.,
                                       "skip_reason": "no_historical_trades", "price_cap": signal["cap"],
                                       "decision_cutoff": signal["signal_time"], "intended_gross": signal['gross'],
                                       "plan_cutoff": iso(active['train_end']) if active and not ordinary else None})
                        continue
                    paid = notional + charge
                    cash[key] -= paid
                    units[key] += quantity
                    spent[key] += notional
                    fees[key] += charge
                    paid_day += paid
                    count += 1
                    last[key] = {"time": time, "price": quote}
                    orders.append({"day": date, "time": iso(time), "asset": key,
                                   "quantity": quantity, "price": quote, "notional": notional,
                                   "fee": charge, "skip_reason": None, "price_cap": signal["cap"],
                                   "decision_cutoff": signal["signal_time"],
                                   "intended_gross": signal['gross'],
                                   "plan_cutoff": iso(active["train_end"]) if active and not ordinary else None})
            after = value(opens)
            revalue(before, after)
            previous = value({k: market[k][time][0].close for k in keys})
            revalue(after, previous)
        daily.append({"date": date, "contribution": daily_total, "spent": paid_day,
                      "fills": count, "count": count, "return": nav / prior_nav - 1})
        curve.append({"date": date, "equity": previous, "cash": sum(cash.values()),
                      "contributions": total, "daily_contribution": daily_total, "twr_index": nav * 100})
    assets = {k: {"quantity": units[k], "spent": spent[k], "fees": fees[k],
                  "unit_cost": (spent[k] + fees[k]) / units[k] if units[k] else None,
                  "market_value": units[k] * market[k][days[-1][1][-1]][0].close}
              for k in keys}
    paid = sum(spent.values()) + sum(fees.values())
    return {"start": days[0][0], "end": days[-1][0], "days": len(days), "contributed": total,
            "spent": sum(spent.values()), "fees": sum(fees.values()), "cash": sum(cash.values()),
            "equity": previous, "profit": previous - total, "twr_return": nav - 1,
            "max_drawdown": drawdown, "execution_rate": paid / total,
            "average_daily_spend": paid / len(days), "assets": assets, "orders": orders,
            "daily": daily, "curve": curve}


def training_rank(result, index):
    return (result["execution_rate"] >= MIN_DEPLOYMENT, result["equity"], -index)


def fit(market, training, daily_total=30.):
    cutoff = training[-1][1][-1] + HOUR
    prefix = {k: {t: value for t, value in rows.items() if t == "rules" or t < cutoff}
              for k, rows in market.items()}
    plans, rankings = {}, {}
    for key in market:
        rows = []
        for index, plan in enumerate(CATALOGUE):
            result = simulate({key: prefix[key]}, training, daily_total / len(market),
                              plans={key: plan}, base_amount=daily_total / 4)
            rows.append({"index": index, "plan": plan, "equity": result["equity"],
                         "execution_rate": result["execution_rate"],
                         "average_daily_spend": result["average_daily_spend"]})
        rows.sort(key=lambda row: training_rank(row, row['index']), reverse=True)
        plans[key], rankings[key] = rows[0]["plan"], rows
    return plans, rankings


def make_schedule(market, days, train_days=32, test_days=16, daily_total=30.):
    if train_days < 8 or test_days < 1 or len(days) <= train_days:
        raise ValueError("Not enough common days for the requested windows")
    schedule = []
    for start in range(train_days, len(days), test_days):
        training = days[start - train_days:start]
        test = days[start:start + test_days]
        plans, ranking = fit(market, training, daily_total)
        schedule.append({"train_start": training[0][1][0],
                         "train_end": training[-1][1][-1] + HOUR - 1,
                         "test_start": test[0][1][0], "test_end": test[-1][1][-1] + HOUR,
                         "plans": plans, "ranking": ranking})
    return schedule


def choose(records):
    rows = [{"policy": key, "eligible": record["current"]["execution_rate"] >= MIN_DEPLOYMENT,
             "equity": record["current"]["equity"],
             "average_daily_spend": record["current"]["average_daily_spend"],
             "execution_rate": record["current"]["execution_rate"]}
            for key, record in records.items()]
    rows.sort(key=lambda row: (row["eligible"], row["equity"], row["policy"] == "ordinary"), reverse=True)
    return rows


def run(directory, output, daily_total=30., train_days=32, test_days=16):
    if not isfinite(daily_total) or daily_total <= 0:
        raise ValueError("Daily total must be positive and finite")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest, info, data = load_dataset(directory, KEYS)
    protocol = freeze(output, {'version': 'chronological-1.0', 'catalogue': CATALOGUE,
                              'train_days': train_days, 'test_days': test_days, 'daily_total': daily_total,
                              'minimum_deployment': MIN_DEPLOYMENT, 'fee_bps': 10, 'slip_bps': 5,
                              'max_fills': 4, 'data_sha256': {k: manifest['assets'][k]['sha256'] for k in KEYS}})
    market = prepare(data, info)
    days = common_days(data)
    schedule = make_schedule(market, days, train_days, test_days, daily_total)
    (output / 'schedule.json').write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding='utf-8')
    test = days[train_days:]
    print("Evaluate " + str(len(test)) + " trading days", flush=True)
    four = simulate(market, test, daily_total, ordinary="four")
    once = simulate(market, test, daily_total, ordinary="once")
    records = {}
    policies = {"ordinary": "普通定投", "initial_fit": "逐标的固定参数", "rolling_fit": "逐标的滚动参数"}
    sources = {"ordinary": {"ordinary": "four"},
               "initial_fit": {"schedule": [{**schedule[0], "test_end": schedule[-1]["test_end"]}]},
               "rolling_fit": {"schedule": schedule}}
    for index, plan in enumerate(CATALOGUE):
        name = "fixed_" + str(index)
        reference = 'EMA168' if plan['reference'] == 'ema' else '均线+VWAP'
        confirmation = ' / RSI≤45' if plan['confirmation'] == 'rsi' else ''
        sizing = '等额' if plan['sizing'] == 'equal' else '分档'
        policies[name] = f'{reference} / 折价{plan["discount"]:.0%}{confirmation} / {sizing}'
        sources[name] = {"plans": dict.fromkeys(KEYS, plan)}
    for key, args in sources.items():
        current = four if key == "ordinary" else simulate(market, test, daily_total, **args)
        records[key] = {"current": current, "four": four, "once": once,
                        "vs_four": compare_portfolios(four, current),
                        "vs_once": compare_portfolios(once, current)}
    ranking = choose(records)
    selected = ranking[0]["policy"]
    selection = {"selected_policy": selected, "ranking": ranking, "eligible_count": sum(r["eligible"] for r in ranking),
                 "selection_period": "historical_test_ranking", "independent_evidence": False}
    selection["lock_sha256"] = lock_selection(output, selection, protocol["sha256"])
    selected_plan = CATALOGUE[int(selected.split('_')[1])] if selected.startswith('fixed_') else None
    (output / 'selected-strategy.json').write_text(json.dumps({'policy': selected, 'plan': selected_plan,
        'daily_total': daily_total, 'max_fills': 4, 'ranking': ranking[0]}, ensure_ascii=False, indent=2), encoding='utf-8')
    payload = {}
    for key, bars in data.items():
        fs = features(bars)
        pairs = [(bar, feature) for bar, feature in zip(bars, fs) if test[0][1][0] <= bar.time <= test[-1][1][-1]]
        payload[key] = {"identity": ALL_ASSETS[key],
                        "candles": [[b.time, b.open, b.high, b.low, b.close, b.volume] for b, f in pairs],
                        "indicators": [[b.time, f.ema72, f.ema168, f.rsi_value, f.bb_middle, f.bb_lower, f.bb_upper] for b, f in pairs]}
    report = {"version": "0.18.0", "scope": "portfolio", "family": "chronological",
              "manifest": manifest, "protocol": protocol, "assets": payload,
              "config": {"daily_center": daily_total, "fee_bps": 10, "slip_bps": 5, "max_count": 4},
              "default_policy": selected, "policies": policies, "results": {"full": records},
              "selection": selection, "schedule": schedule, "period_labels": {"full": "回测区间"},
              "comparison_note": "同日入金 · 现金计入资产 · 未计理财收益",
              "daily_funding": daily_total}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print("Selected: " + selected + " | equity " + str(records[selected]["current"]["equity"]), flush=True)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data/seven-assets")
    parser.add_argument("--output", default="reports/chronological")
    parser.add_argument("--daily-total", type=float, default=30.)
    parser.add_argument("--train-days", type=int, default=32)
    parser.add_argument("--test-days", type=int, default=16)
    args = parser.parse_args()
    run(args.data, args.output, args.daily_total, args.train_days, args.test_days)
