"""One shared cash ledger, one contribution and at most four portfolio buys a day."""

import json
from math import isfinite
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .adaptive import freeze
from .config import ASSETS, ALL_ASSETS, Config
from .engine import Rules, amount_for_slot, day_groups
from .evaluate import SCENARIOS, confidence_interval
from .indicators import Feature, features, score
from .market import HOUR, iso, load_dataset, milliseconds
from .schedule import daily_funding, entry_signal, exposure_budget
from .signal_search import RULES as INDICATOR_RULES, INDICATOR_POLICIES, FIVE_POLICIES, ALL_INDICATOR_POLICIES, SELECTION_PROTOCOL, indicator_entry, select_development, lock_selection
from .vwap_rules import VWAP_POLICIES, tier_signal

KEYS = tuple(ASSETS)
POLICIES = ("four", "frequency", "rsi", "bottom", "ma", "position")
LABELS = {"four": "普通最多4次", "once": "普通每天1次", "frequency": "波动决定组合次数", "rsi": "+ RSI回升", "bottom": "+ 探底确认", "ma": "+ 均线偏离额度", "position": "+ 仓位软目标"}
OPPORTUNITY_POLICIES = ("core", "core_trend", "core_size", "core_select")
SIGNAL_UPGRADES = ("signal_scan", "signal_reserve", "signal_ladder")
SCAN_POLICIES = (*SIGNAL_UPGRADES, *ALL_INDICATOR_POLICIES)
PURE_POLICIES = ("discount_only", *SCAN_POLICIES)
DISCOUNT_POLICIES = ("core_discount", *PURE_POLICIES)
CASH_POLICIES = (*OPPORTUNITY_POLICIES, *DISCOUNT_POLICIES)
LABELS.update({"core": "新1：基础单 + 机会单", "core_trend": "新2：+ 趋势回踩", "core_size": "新3：+ 偏离额度", "core_select": "新4：+ 受限机会选标的"})
LABELS["core_discount"] = "本轮：+ 0.5ATR价格折扣门槛"
LABELS["discount_only"] = "纯机会单：无基础单 / 无强制补投"
LABELS.update({"signal_scan": "纯机会优化1：跨标的扫描", "signal_reserve": "纯机会优化2：+ 折扣现金加码", "signal_ladder": "纯机会优化3：+ 更低价加码间距"})
LABELS.update({k: v[0] for k, v in INDICATOR_RULES.items()})


def discount_ceiling(feature):
    if feature is None or feature.ema168 is None or feature.atr14 is None:
        return None
    if not isfinite(feature.ema168) or not isfinite(feature.atr14) or feature.atr14 <= 0:
        return None
    ceiling = feature.ema168 - .5 * feature.atr14
    return ceiling if ceiling > 0 else None


def signal_price_cap(feature, last_fill, time, policy):
    ceiling = discount_ceiling(feature)
    if ceiling is not None and policy == "signal_ladder" and last_fill and time - last_fill[0] < 24 * HOUR:
        ceiling = min(ceiling, last_fill[1] - .5 * feature.atr14)
    return ceiling if ceiling is not None and ceiling > 0 else None


def signal_reserve_amount(contribution, reserve, feature, policy):
    # Prior-day cash is never released just because it has been idle for a while.
    if policy not in ("signal_reserve", "signal_ladder", *ALL_INDICATOR_POLICIES):
        return 0.
    depth = max(0., min(1., feature.long_deviation))
    return min(.1 * max(0., reserve), .6 * contribution) * depth


def opportunity_signal(feature, previous, close, previous_close, policy):
    if policy in ALL_INDICATOR_POLICIES:
        hit, trigger, reference = indicator_entry(feature, previous, close, previous_close, policy)
        ceiling = discount_ceiling(feature)
        return (hit, trigger, reference) if ceiling is not None and close <= ceiling else (False, None, reference)
    hit, trigger, reference = entry_signal(feature, "bottom")
    if hit:
        if policy in DISCOUNT_POLICIES:
            ceiling = discount_ceiling(feature)
            if ceiling is None or close > ceiling:
                return False, None, reference
        return hit, trigger, reference
    if policy in ("core_trend", "core_size", "core_select") and feature.ema72 is not None and feature.ema168 is not None:
        if (feature.ema72 >= feature.ema168 and feature.long_deviation >= 0
                and 40 <= feature.rsi_value <= 60 and feature.rsi_value > previous.rsi_value
                and close > previous_close):
            return True, "trend_pullback", feature.rsi_value
    return False, None, feature.rsi_value


def blend(feature_map, weights):
    KEYS = tuple(weights)
    return Feature(0, sum(weights[k] * feature_map[k].momentum for k in KEYS), False,
                   sum(weights[k] * feature_map[k].volatility_multiple for k in KEYS), 800, 0,
                   rsi_value=sum(weights[k] * feature_map[k].rsi_value for k in KEYS),
                   long_deviation=sum(weights[k] * feature_map[k].long_deviation for k in KEYS))


def choose_asset(units, prices, equity, weights):
    KEYS = tuple(weights)
    # Buy-only rebalancing: largest quote-value target deficit, stable tie break.
    return max(KEYS, key=lambda k: (weights[k] * equity - units[k] * prices[k], -KEYS.index(k)))


def portfolio_simulate(market, selected, funding, config, weights, policy, fee_bps=10, slip_bps=5, budgets=None, vwap_plan=None, full_vwap_amount=False, top_up_cash=False):
    KEYS = tuple(weights)
    vwap = policy in VWAP_POLICIES
    if full_vwap_amount and (not vwap or budgets is not None):
        raise ValueError('Full signal amount requires VWAP policy without matched daily budgets')
    if top_up_cash and not full_vwap_amount:
        raise ValueError('On-demand funding requires full VWAP signal amounts')
    if policy not in (*POLICIES, *CASH_POLICIES, "once", *VWAP_POLICIES):
        raise ValueError("Unknown portfolio policy")
    if vwap and (not vwap_plan or set(vwap_plan) != set(KEYS)):
        raise ValueError("Every VWAP asset requires a frozen profile")
    opportunity = policy in CASH_POLICIES or vwap
    signal_only = policy in PURE_POLICIES or vwap
    fee = fee_bps / 10000
    cash = total = spent = fees = 0.
    units = {k: 0. for k in KEYS}
    asset_spent, asset_fees = units.copy(), units.copy()
    nav = peak = 1.
    drawdown, previous_equity = 0., None
    orders, curve, daily = [], [], []
    missed = 0
    last_fill = -100 * HOUR
    last_asset_fill = {}
    recent_funding = []

    def value(prices):
        return cash + sum(units[k] * prices[k] for k in KEYS)

    def revalue(before, after):
        nonlocal nav, peak, drawdown
        if before > 0:
            nav *= after / before
            peak = max(peak, nav)
            drawdown = min(drawdown, nav / peak - 1)

    for day, timestamps in selected:
        prior_nav = nav
        start = timestamps[0]
        day_features = {k: market[k][start - HOUR][1] for k in KEYS}
        prior_prices = {k: market[k][start - HOUR][0].close for k in KEYS}
        opens = {k: market[k][start][0].open for k in KEYS}
        if previous_equity is not None:
            revalue(previous_equity, value(opens))
        composite = blend(day_features, weights)
        contribution = funding[day]
        if not isfinite(contribution) or contribution <= 0:
            raise ValueError("Portfolio contribution must be positive and finite")
        deploy, target_exposure = contribution, None
        reserve_at_start = cash
        if opportunity:
            # Aggregate cash is bounded to six recent contributions where feasible.
            mandatory = 0. if signal_only else max(.6 * contribution, cash - sum(recent_funding[-6:]))
        if policy == "position":
            asset_value = sum(units[k] * prior_prices[k] for k in KEYS)
            deploy, target_exposure = exposure_budget(cash, asset_value, 1., contribution, composite)
        if budgets is not None:
            deploy = budgets[day]
            if not isfinite(deploy) or not 0 <= deploy <= cash + contribution + 1e-8:
                raise ValueError("Invalid matched portfolio deployment")
        minimum = max(market[k]["rules"].min_notional + float(market[k]["rules"].step) * prior_prices[k] * (1 + slip_bps / 10000) for k in KEYS)
        if budgets is None:
            if opportunity:
                deploy = min(cash + contribution, max(2 * contribution, mandatory, minimum * (1 + fee)))
                if not signal_only:
                    mandatory = min(deploy, max(mandatory, minimum * (1 + fee)))
            else:
                deploy = min(contribution, max(deploy, minimum * (1 + fee)))
        wanted = 1 if policy == "once" else 4 if policy == "four" else 1 + sum(composite.volatility_multiple >= q for q in (.75, 1., 1.5))
        count = wanted
        factor = 1 - config.max_deviation if policy in ("ma", "position") else 1
        while count > 1 and deploy / (1 + fee) / count * factor < minimum:
            count -= 1
        if full_vwap_amount:
            # Preserve signal windows; available cash is diagnostic, never an amount cap.
            deploy = cash + contribution
        local_budget = max(deploy, 1e-9)
        local = replace(config, daily_total=local_budget, center=local_budget / (1 + fee) / count, min_count=count, max_count=count, fee_bps=fee_bps, slippage_bps=slip_bps)
        windows = [(i * 24 // count, (i + 1) * 24 // count) for i in range(count)]
        waiting = policy in ("rsi", "bottom", "ma", "position") or opportunity
        slot, selected_asset = 0, None
        remaining = local.principal
        day_added = 0. if top_up_cash else contribution
        cash += day_added
        total += day_added
        previous_mark = value(opens)
        day_spent, day_fills = 0., 0
        gate = {"confirmed_checks": 0, "discount_pass_checks": 0, "quote_rejections": 0, "discount_fills": 0,
                "scanned_assets": 0, "eligible_assets": 0, "weight_rejections": 0}
        for hour, t in enumerate(timestamps):
            opens = {k: market[k][t][0].open for k in KEYS}
            before = value(opens)
            revalue(previous_mark, before)
            if slot < count and hour >= windows[slot][0]:
                if selected_asset is None:
                    known_prices = {k: market[k][t - HOUR][0].close for k in KEYS}
                    selected_asset = choose_asset(units, known_prices, value(known_prices), weights)
                f = market[selected_asset][t - HOUR][1]
                price_cap = None
                candidate_assets = []
                vwap_details = None
                hit, trigger, reference = entry_signal(f, "rsi_rebound" if policy == "rsi" else "bottom") if waiting else (True, "scheduled", None)
                if opportunity:
                    if slot == 0 and not signal_only:
                        hit, trigger, reference = True, "core_daily", None
                    else:
                        def eligible(k):
                            bar, feat = market[k][t - HOUR]
                            old_bar, old_feat = market[k][t - 2 * HOUR]
                            if vwap:
                                details = tier_signal(feat, bar.close, vwap_plan[k], policy)
                                return bool(details), policy if details else None, feat.rsi_value
                            return opportunity_signal(feat, old_feat, bar.close, old_bar.close, policy)
                        if policy == "core_select":
                            known_prices = {k: market[k][t - HOUR][0].close for k in KEYS}
                            equity = value(known_prices)
                            available = [k for k in KEYS if eligible(k)[0] and units[k] * known_prices[k] / equity <= weights[k] + .15]
                            if available:
                                selected_asset = max(available, key=lambda k: weights[k] - units[k] * known_prices[k] / equity + .25 * score(market[k][t - HOUR][1], "ma_sizing"))
                        if policy in SCAN_POLICIES or vwap:
                            known_prices = {k: market[k][t - HOUR][0].close for k in KEYS}
                            equity = value(known_prices)
                            for candidate in KEYS:
                                gate["scanned_assets"] += 1
                                feat = market[candidate][t - HOUR][1]
                                details = tier_signal(feat, known_prices[candidate], vwap_plan[candidate], policy) if vwap else None
                                cap = details["cap"] if details else None if vwap else signal_price_cap(feat, last_asset_fill.get(candidate), t, policy)
                                if not eligible(candidate)[0] or cap is None or market[candidate][t][0].open * (1 + slip_bps / 10000) > cap:
                                    continue
                                if equity > 0 and units[candidate] * known_prices[candidate] / equity > weights[candidate] + .15:
                                    gate["weight_rejections"] += 1
                                    continue
                                candidate_assets.append(candidate)
                            gate["eligible_assets"] += len(candidate_assets)
                            if candidate_assets:
                                selected_asset = max(candidate_assets, key=lambda k: (weights[k] * equity - units[k] * known_prices[k],
                                                     tier_signal(market[k][t-HOUR][1], known_prices[k], vwap_plan[k], policy)["depth"] if vwap else market[k][t - HOUR][1].long_deviation, -KEYS.index(k)))
                        f = market[selected_asset][t - HOUR][1]
                        hit, trigger, reference = eligible(selected_asset)
                        if vwap:
                            vwap_details = tier_signal(f, market[selected_asset][t-HOUR][0].close, vwap_plan[selected_asset], policy)
                            price_cap = vwap_details["cap"] if vwap_details else None
                        if policy in DISCOUNT_POLICIES:
                            price_cap = signal_price_cap(f, last_asset_fill.get(selected_asset), t, policy)
                            old_bar, old_feat = market[selected_asset][t - 2 * HOUR]
                            gate["confirmed_checks"] += (indicator_entry(f, old_feat, market[selected_asset][t-HOUR][0].close, old_bar.close, policy)[0]
                                                         if policy in ALL_INDICATOR_POLICIES else entry_signal(f, "bottom")[0])
                            gate["discount_pass_checks"] += bool(hit)
                            # Observe only the next-open quote, never this hour's low/high.
                            if hit and (price_cap is None or market[selected_asset][t][0].open * (1 + slip_bps / 10000) > price_cap):
                                hit = False
                                gate["quote_rejections"] += 1
                        if policy == "core_select":
                            hit = hit and selected_asset in available
                        if policy in SCAN_POLICIES or vwap:
                            hit = hit and selected_asset in candidate_assets
                        hit = hit and t - last_fill >= 4 * HOUR
                deadline = hour == windows[slot][1] - 1
                if hour >= windows[slot][0] and (hit or deadline):
                    k = selected_asset
                    signal = score(f, "ma_sizing") if policy in ("ma", "position", "core_size", "core_select") else 0.
                    target = amount_for_slot(remaining, count - slot, signal, local)
                    reserve_budget = 0.
                    if deploy == 0:
                        target = 0.
                    if opportunity:
                        if slot == 0 and not signal_only:
                            target = mandatory / (1 + fee)
                        elif hit:
                            factor = 1 + .3 * score(f, "ma_sizing") if policy in ("core_size", "core_select") else 1.
                            reserve_budget = signal_reserve_amount(contribution, reserve_at_start, f, policy)
                            target = min(remaining, max(minimum, .4 * contribution / (1 + fee) * factor + reserve_budget / (1 + fee)))
                            if vwap:
                                signal_amount = contribution * weights[k] * vwap_details["multiplier"] / (1 + fee)
                                target = signal_amount if full_vwap_amount else min(remaining, signal_amount)
                            if policy == "mix_sizing":
                                # Same signals as guard; change only conditional opportunity size.
                                factor = max(.7, min(1.3, 1 + .3 * max(0., f.long_deviation) - .3 * bool(f.downtrend)))
                                target = min(remaining, max(minimum, target * factor))
                                reserve_budget *= factor
                            reserve_budget = max(0., min(reserve_budget, target * (1 + fee) - .4 * contribution * factor))
                        else:
                            target = 0.
                    b, rules = market[k][t][0], market[k]["rules"]
                    price = b.open * (1 + slip_bps / 10000)
                    q = rules.quantity(target, price)
                    notional, charge = q * price, q * price * fee
                    reason = None
                    cash_topup = 0.
                    if deploy == 0:
                        reason = "no_deployment"
                    elif opportunity and not hit:
                        reason = "no_opportunity"
                    elif b.volume == 0:
                        reason = "no_historical_trades"
                    elif q <= 0 or q < rules.min_qty or q > rules.max_qty or notional < rules.min_notional - 1e-8 or notional > rules.max_notional:
                        reason = "exchange_minimum_or_quantity"
                    elif notional + charge > cash + 1e-8:
                        if top_up_cash:
                            cash_topup = notional + charge - cash
                            cash += cash_topup
                            total += cash_topup
                            day_added += cash_topup
                            # External deposits change capital, not investment returns.
                            before += cash_topup
                        else:
                            reason = "cash_limit"
                    if reason:
                        missed += reason not in ("no_opportunity", "no_deployment")
                        q = notional = charge = 0.
                    else:
                        units[k] += q
                        cash -= notional + charge
                        spent += notional
                        fees += charge
                        asset_spent[k] += notional
                        asset_fees[k] += charge
                        day_spent += notional + charge
                        day_fills += 1
                        last_fill = t
                        last_asset_fill[k] = (t, price)
                        gate["discount_fills"] += price_cap is not None
                    orders.append({"day": day, "time": iso(t), "asset": k, "target": target, "notional": notional, "fee": charge, "quantity": q, "price": price,
                                   "signal": signal, "skip_reason": reason, "day_count": count, "day_volatility": composite.volatility_multiple,
                                   "trigger": trigger if hit else "opportunity_expired" if opportunity else "window_deadline", "timing_signal": reference, "rsi_value": f.rsi_value, "ma_deviation": f.long_deviation,
                                   "window_start": iso(timestamps[windows[slot][0]]),
                                   "price_cap": price_cap, "feature_atr14": f.atr14,
                                   "candidate_assets": candidate_assets, "reserve_budget": reserve_budget,
                                   **({"vwap20": vwap_details["reference"] if vwap_details else f.vwap20,
                                       "vwap_depth": vwap_details["depth"] if vwap_details else None,
                                       "vwap_tier": vwap_details["tier"] + 1 if vwap_details else None,
                                       "vwap_multiplier": vwap_details["multiplier"] if vwap_details else None,
                                       "vwap_rsi_confirmed": f.rsi_rebound,
                                       "vwap_bottom_confirmed": f.sweep_reclaim or f.hammer_confirmed} if vwap else {}),
                                   **({'cash_topup':cash_topup,'sizing_reference':contribution} if top_up_cash else {}),
                                   "day_deployment": day_added if top_up_cash else deploy, "target_exposure": target_exposure, "day_contribution": contribution,
                                   "decision_cutoff": iso(t), "cash_after": cash, "wanted_count": wanted, "minimum_guard": minimum})
                    if not full_vwap_amount:
                        remaining -= target
                    slot += 1
                    selected_asset = None
            after = value(opens)
            revalue(before, after)
            closes = {k: market[k][t][0].close for k in KEYS}
            previous_mark = value(closes)
            revalue(after, previous_mark)
        previous_equity = previous_mark
        curve.append({"date": day, "equity": previous_equity, "cash": cash, "contributions": total, "twr_index": nav * 100,
                      "position_fraction": (previous_equity - cash) / previous_equity if previous_equity else 0., "daily_contribution": day_added})
        daily.append({"date": day, "contribution": day_added, "deployment": day_added if top_up_cash else deploy, "spent": day_spent, "count": count, "wanted_count": wanted,
                      **({'sizing_reference':contribution} if top_up_cash else {}),
                      "fills": day_fills, "return": nav / prior_nav - 1, "volatility": composite.volatility_multiple, "discount_gate": gate})
        recent_funding.append(contribution)
    equity = previous_equity or 0.
    last = selected[-1][1][-1] if selected else None
    per_asset = {k: {"quantity": units[k], "spent": asset_spent[k], "fees": asset_fees[k],
                     "unit_cost": (asset_spent[k] + asset_fees[k]) / units[k] if units[k] else None,
                     "market_value": units[k] * market[k][last][0].close if last else 0} for k in KEYS}
    return {"start": selected[0][0] if selected else None, "end": selected[-1][0] if selected else None, "days": len(selected), "contributed": total,
            "spent": spent, "fees": fees, "cash": cash, "equity": equity, "profit": equity - total, "twr_return": nav - 1,
            "max_drawdown": drawdown, "execution_rate": (spent + fees) / total if total else 0, "skipped_slots": missed,
            "assets": per_asset, "orders": orders, "curve": curve, "daily": daily}


def compare_portfolios(base, current):
    KEYS = tuple(base["assets"])
    same = [d["contribution"] for d in base["daily"]] == [d["contribution"] for d in current["daily"]]
    returns = [(b["return"] - a["return"]) * 10000 for a, b in zip(base["daily"], current["daily"])]
    return {"same_funding": same, "contribution_delta": current["contributed"] - base["contributed"],
            "equity_delta": current["equity"] - base["equity"], "profit_delta": current["profit"] - base["profit"],
            "equity_gain_pct_of_contributions": (current["equity"] - base["equity"]) / current["contributed"] * 100 if same and current["contributed"] else None,
            "daily_return_gain_bps": sum(returns) / len(returns) if returns else None, "ci95_bps": confidence_interval(returns),
            "drawdown_delta_pp": (current["max_drawdown"] - base["max_drawdown"]) * 100,
            "asset_cost_gain_pct": {k: (1 - current["assets"][k]["unit_cost"] / base["assets"][k]["unit_cost"]) * 100
                                    if base["assets"][k]["unit_cost"] and current["assets"][k]["unit_cost"] else None for k in KEYS}}


def trigger_diagnostics(result, market):
    groups = {}
    segment_last = milliseconds(result["end"] + "T23:00:00+08:00") if result["end"] else None
    for order in result["orders"]:
        group = groups.setdefault(order["trigger"], {"attempts": 0, "fills": 0, "spent": 0., "anchor_gain_sum": 0., "forward_24h_sum": 0., "forward_spent": 0.})
        group["attempts"] += 1
        if order["skip_reason"]:
            continue
        group["fills"] += 1
        paid = order["notional"] + order["fee"]
        group["spent"] += paid
        t = milliseconds(order["time"])
        anchor = milliseconds(order.get("window_start", order["time"]))
        price = market[order["asset"]][anchor][0].open
        group["anchor_gain_sum"] += paid * (price / order["price"] - 1) * 10000
        future = market[order["asset"]].get(t + 24 * HOUR)
        # Never read beyond this evaluation segment, even if raw data has later bars.
        if future and t + 24 * HOUR <= segment_last:
            group["forward_24h_sum"] += paid * (future[0].open / order["price"] - 1) * 100
            group["forward_spent"] += paid
    return {trigger: {**g, "anchor_gain_bps": g["anchor_gain_sum"] / g["spent"] if g["spent"] else None,
                      "forward_24h_pct": g["forward_24h_sum"] / g["forward_spent"] if g["forward_spent"] else None} for trigger, g in groups.items()}


def evaluate_portfolio(directory, output, center=30., funding_deviation=.30, weights=None, family="original", asset_keys=None):
    KEYS = tuple(asset_keys or ASSETS)
    if not KEYS or len(set(KEYS)) != len(KEYS) or any(k not in ALL_ASSETS for k in KEYS):
        raise ValueError("Require unique supported spot assets")
    if family not in ("original", "opportunity", "discount", "signal", "signal_v2", "indicator"):
        raise ValueError("Unknown portfolio research family")
    indicator_policies = (*POLICIES, *OPPORTUNITY_POLICIES, "core_discount", "discount_only", *SIGNAL_UPGRADES, *INDICATOR_POLICIES)
    if len(KEYS) == 5:
        indicator_policies = (*indicator_policies, *FIVE_POLICIES)
    selection_protocol = {**SELECTION_PROTOCOL}
    if len(KEYS) == 5:
        selection_protocol.update(candidates=[*SELECTION_PROTOCOL["candidates"], *FIVE_POLICIES],
                                  extra="Exactly3 five-asset additions: RSI OR BB; same OR except four-hour downtrend/unknown requires both RSI AND BB; same guard signals with amount multiplier clip(1+.3*clip(long_deviation,0,1)-.3*downtrend,.7,1.3), exchange minimum and remaining daily cash caps unchanged. Unknown trend is not bullish. All earlier versions retained; first five-asset results already exposed, retrospective only.")
    policies = indicator_policies if family == "indicator" else (*POLICIES, *OPPORTUNITY_POLICIES, "core_discount", "discount_only", *SIGNAL_UPGRADES) if family == "signal_v2" else (*POLICIES, *OPPORTUNITY_POLICIES, "core_discount", "discount_only") if family == "signal" else (*POLICIES, *OPPORTUNITY_POLICIES, "core_discount") if family == "discount" else (*POLICIES, *OPPORTUNITY_POLICIES) if family == "opportunity" else POLICIES
    previous_policy = {p: prev for prev, p in zip(POLICIES, POLICIES[1:])}
    previous_policy.update(dict(zip(OPPORTUNITY_POLICIES, ("rsi", *OPPORTUNITY_POLICIES[:-1]))))
    previous_policy["core_discount"] = "core"
    previous_policy["discount_only"] = "core_discount"
    previous_policy.update(dict(zip(SIGNAL_UPGRADES, ("discount_only", *SIGNAL_UPGRADES[:-1]))))
    previous_policy.update({k: "signal_reserve" for k in INDICATOR_POLICIES})
    previous_policy.update({"mix_rsi_bb": "mix_rsi", "mix_rsi_ma": "mix_rsi", "mix_bb_ma": "mix_bb", "mix_vote": "mix_rsi_bb", "mix_all": "mix_vote"})
    previous_policy.update({"mix_or": "mix_rsi", "mix_guard": "mix_or", "mix_sizing": "mix_guard"})
    weights = weights or {k: 1 / len(KEYS) for k in KEYS}
    if set(weights) != set(KEYS) or any(not isfinite(v) or v <= 0 for v in weights.values()) or abs(sum(weights.values()) - 1) > 1e-8:
        raise ValueError("Portfolio weights must be positive and sum to one")
    config = Config(daily_total=center, center=center / 3 / 1.001, min_count=1, max_count=4)
    manifest, info, datasets = load_dataset(directory, KEYS)
    market, eligible, asset_data = {}, {}, {}
    for k, bars in datasets.items():
        f = features(bars)
        market[k] = {b.time: (b, feature) for b, feature in zip(bars, f)}
        market[k]["rules"] = Rules.from_symbol(info["symbols"][k])
        eligible[k] = {d: [bars[i].time for i in indices] for d, indices in day_groups(bars, f, "rsi", config)
                       if f[indices[0] - 1].volatility_multiple is not None}
    dates = sorted(set.intersection(*(set(d) for d in eligible.values())))
    if len(dates) < 10:
        raise ValueError("Fewer than 10 common complete days; no proxy histories are used")
    selected = [(d, eligible[KEYS[0]][d]) for d in dates]
    funding = {d: daily_funding(blend({k: market[k][ts[0] - HOUR][1] for k in KEYS}, weights), center, funding_deviation) for d, ts in selected}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    protocol = freeze(output, {"version": "portfolio-signal-v2-1.0" if family == "signal_v2" else "portfolio-signal-only-1.0" if family == "signal" else "portfolio-discount-1.0" if family == "discount" else "portfolio-opportunity-1.1" if family == "opportunity" else "portfolio-funding-1.0", "scope": "All three assets combined", "center": center, "deviation": funding_deviation,
                               "weights": weights, "policies": policies, "funding_formula": "round(center*(1+deviation*clip(sum(weight*(.65*long_deviation+.35*RSI_momentum)),-1,1)),2), prior closed hour only",
                               "count": "1--4 TOTAL portfolio attempts/day, reduced for current exchange minimum plus prior-close one-lot buffer",
                               "asset_selection": "At each window start, largest target-weight quote-value deficit using previous closed prices; no sales",
                               "comparisons": "Identical variable contributions for all scored benchmarks. Fixed30/day reference is unscored. No mixed-asset unit cost.",
                               "data_sha256": {k: v["sha256"] for k, v in manifest["assets"].items()}, "evidence": "Previously exposed history; retrospective only",
                               **({"search": "Exactly three new fixed cumulative pure-opportunity variants; no threshold search; all prior candidates retained. Default research display signal_reserve is preselected, not a winning-version claim." if family == "signal_v2" else "One user-requested no-base variant against core_discount; no threshold search; all prior results retained." if family == "signal" else "One new pre-specified .5ATR gate against core; all nine prior experiments retained; no threshold search." if family == "discount" else "Exactly four fixed cumulative experiments; all original and failed results retained. No repeated search until positive.",
                                   "core": "At 00:00 buy max(60% contribution, prior cash minus last six contributions, exchange minimum). Daily cap min(available cash,max(2*contribution,mandatory)); adaptive 1--4 total slots; opportunity slots expire without buying; opportunity fills require >=4 hours after the preceding fill; daily base at 00:00 is exempt.",
                                   "trend": "Add EMA72>=EMA168, long_deviation>=0, RSI40--60 rising and closed price rising.",
                                   "size": "Opportunity amount 40% contribution*(1+.3*ma_score), capped by remaining budget; base unchanged.",
                                   "select": "Opportunity assets with confirmed signals and weight<=target+.15; maximize weight deficit+.25*ma_score. Base remains deficit selection.",
                                   "matched": "Ex-post actual daily spend matched control. Diagnostic only: future intraday spending is not a deployable baseline. Rounding can differ slightly.",
                                   "meaningful": "Historical screening only: equity advantage >=1% of contributions vs both ordinary controls, every asset cost gain >=.5%, drawdown not worse by >.2pp, matched-spend advantage positive, validation positive, stress positive; 90 holdout days and 28 validation days required, plus fresh forward evidence."} if family != "original" else {}),
                               **({"discount": "Opportunity only: previous closed price <= previous EMA168-.5*ATR14 AND existing bottom/RSI confirmation; next-open executable quote including assumed slippage must also <= that ceiling. No fills at intrabar lows or at an untouched limit. A rejected quote waits until another eligible hour or expires. Base/release, funding, counts, selection, amounts unchanged from core.",
                                   "comparison_parent": "core; not core_select", "fresh_evidence": False} if family in ("discount", "signal", "signal_v2") else {}),
                               **({"signal_only": "Remove both fixed daily base buy and compulsory stale-cash release from core_discount. All 1--4 planned windows are opportunity windows; zero fills/day allowed. Every fill requires prior closed confirmation plus .5ATR discount AND next-open executable quote<=cap, >=4h since last fill. Opportunity amount remains 40% current contribution, day cap=min(available cash,2*contribution), no leverage, no sales, no forced cash expiry.",
                                   "signal_comparison_parent": "core_discount"} if family in ("signal", "signal_v2") else {}),
                               **({"scan": "Every eligible hour scan all 3 assets: require existing confirmation, closed .5ATR discount, executable next-open quote<=cap and pre-buy asset weight<=target+.15. Prefer largest quote-value deficit, then deeper discount, then stable key order. No future bar lows/volume used to choose assets.",
                                   "reserve": "Add min(.10*day-start pre-contribution cash,.60*current contribution)*clip(long_deviation,0,1) to the existing .40*contribution opportunity amount. Same day budget<=2*contribution and actual cash. Only deploy on a qualifying signal; no expiry or compulsory release.",
                                   "ladder": "Same asset filled within prior24h: executable quote must also <= last actual fill price-.5*current closed ATR14. Combine with EMA cap by min(); reset ladder after24h, global4h fill cooldown unchanged.",
                                   "default_policy": "signal_reserve", "fresh_evidence": False} if family == "signal_v2" else {}),
                               **({"indicator_selection": selection_protocol, "version": "portfolio-indicator-1.0"} if family == "indicator" else {}),
                               **({"scope": "All five assets combined", "assets": list(KEYS), "version": "portfolio-five-indicator-1.0", "allocation": "Default equal weights20% each unless explicitly configured above; one shared30U funding center. At most4 total buys/day; no requirement to buy all assets daily. Existing fixed catalogue rerun; no new threshold search.", "scan": "Scan all five assets each eligible hour; require closed signal and discount, executable next-open quote<=cap, pre-buy weight<=target+.15. Largest quote deficit first, discount depth second, stable asset order third. No future lows or volume in selection."} if len(KEYS) == 5 else {})})
    splits = {"development": selected[:int(.6 * len(selected))], "validation": selected[int(.6 * len(selected)):int(.8 * len(selected))], "holdout": selected[int(.8 * len(selected)):], "full": selected}
    results = {}
    selection = None
    for name, days in splits.items():
        print(f"Portfolio {name}: {len(days)} days", flush=True)
        cache = {p: portfolio_simulate(market, days, funding, config, weights, p) for p in (*policies, "once")}
        cache["fixed30"] = portfolio_simulate(market, days, {d: center for d, _ in days}, config, weights, "four")
        results[name] = {}
        for p in policies[1:]:
            previous = previous_policy[p]
            cur = cache[p]
            matched = portfolio_simulate(market, days, funding, config, weights, "four", budgets={d["date"]: d["spent"] if p in CASH_POLICIES else d["deployment"] for d in cur["daily"]}) if p == "position" or p in CASH_POLICIES else cache["four"]
            results[name][p] = {"current": cur, "four": cache["four"], "once": cache["once"], "fixed30": cache["fixed30"], "matched": matched,
                                "previous_policy": previous, "trigger_diagnostics": trigger_diagnostics(cur, market),
                                "matched_is_ex_post": p in CASH_POLICIES,
                                "vs_previous": compare_portfolios(cache[previous], cur), "vs_four": compare_portfolios(cache["four"], cur),
                                "vs_once": compare_portfolios(cache["once"], cur), "vs_matched": compare_portfolios(matched, cur)}
        if name == "development" and family == "indicator":
            selection = select_development(results[name], selection_protocol["candidates"])
            selection["start"], selection["end"] = days[0][0], days[-1][0]
            selection["lock_sha256"] = lock_selection(output, selection, protocol["sha256"])
    stress = {}
    for policy in policies[1:]:
        stress[policy] = {}
        for scenario, (fee, slip) in SCENARIOS.items():
            cur = portfolio_simulate(market, splits["holdout"], funding, config, weights, policy, fee, slip)
            stress[policy][scenario] = {p: compare_portfolios(portfolio_simulate(market, splits["holdout"], funding, config, weights, p, fee, slip), cur) for p in ("four", "once")}
    for k, bars in datasets.items():
        lo, hi = selected[0][1][0], selected[-1][1][-1]
        asset_data[k] = {"identity": ALL_ASSETS[k], "candles": [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in bars if lo <= b.time <= hi],
                         "indicators": [[b.time, f.ema72, f.ema168, f.rsi_value] for b in bars if lo <= b.time <= hi for f in [market[k][b.time][1]] if f is not None]}
        if family == "indicator":
            asset_data[k]["indicators"] = [[b.time, f.ema72, f.ema168, f.rsi_value, f.bb_middle, f.bb_lower, f.bb_upper]
                                            for b in bars if lo <= b.time <= hi for f in [market[k][b.time][1]] if f is not None]
        if family in ("discount", "signal", "signal_v2", "indicator"):
            asset_data[k]["discount_caps"] = [[b.time, discount_ceiling(market[k].get(b.time - HOUR, (None, None))[1])]
                                               for b in bars if lo <= b.time <= hi]
    decisions = {}
    for p in policies[1:]:
        h, v = results["holdout"][p], results["validation"][p]
        checks = {base: {"days": h["current"]["days"] >= 90 and v["current"]["days"] >= 28,
                         "same_funding": h[f"vs_{base}"]["same_funding"],
                         "equity_gain_over_half_pct": h[f"vs_{base}"]["equity_gain_pct_of_contributions"] >= .5,
                         "ci_above_zero": bool(h[f"vs_{base}"]["ci95_bps"] and h[f"vs_{base}"]["ci95_bps"][0] > 0),
                         "drawdown": h[f"vs_{base}"]["drawdown_delta_pp"] >= -.2,
                         "validation": v[f"vs_{base}"]["profit_delta"] > 0,
                         "stress": all(s[base]["profit_delta"] > 0 for s in stress[p].values())} for base in ("four", "once")}
        decisions[p] = {"status": "仅历史达标，待新数据" if all(all(c.values()) for c in checks.values()) else "样本不足 / 未达启用门槛", "checks": checks, "live": False}
        if family != "original":
            extra = {base: {"equity_gain_1pct": h[f"vs_{base}"]["equity_gain_pct_of_contributions"] >= 1,
                            "every_asset_cost_gain_half_pct": all(v is not None and v >= .5 for v in h[f"vs_{base}"]["asset_cost_gain_pct"].values()),
                            "matched_advantage": h["vs_matched"]["profit_delta"] > 0} for base in ("four", "once")}
            for base in extra:
                checks[base].update(extra[base])
            decisions[p]["status"] = "仅历史达标，待新数据" if all(all(c.values()) for c in checks.values()) else "未证明明显优势 / 仅探索"
    report = {"version": "0.9.0" if family == "signal_v2" else "0.8.0" if family == "signal" else "0.7.0" if family == "discount" else "0.6.0" if family == "opportunity" else "0.5.0", "scope": "portfolio", "chart_engine": "tradingview-lightweight-charts-5.2.1", "manifest": manifest, "protocol": protocol,
              "config": {"daily_center": center, "funding_deviation": funding_deviation, "lower": center * (1 - funding_deviation), "upper": center * (1 + funding_deviation), "weights": weights, "max_count": 4, "fee_bps": 10, "slip_bps": 5},
              "family": family, "default_policy": "signal_reserve" if family == "signal_v2" else "discount_only" if family == "signal" else "core_discount" if family == "discount" else "core" if family == "opportunity" else "rsi",
              "policies": {p: LABELS[p] for p in policies[1:]}, "assets": asset_data, "results": results, "stress": stress, "decisions": decisions,
              "limitations": ["组合总新增资金中心30U，并非每标的30U。±30%沿用为研究默认值，可调整。", "30U是计算中心，不保证历史实际日均正好30U，也不是自动充值授权。", "三等权是可调整的研究对照，不是用户真实资产配置建议；买入侧补足低配标的，不卖出。", "每天1–4次是整个组合的计划次数，部分日期只买其中1–2个标的，按权重缺口轮换。", "最低下单额和数量步长可能使次数进一步减少；突发跳价、停牌或无成交仍可导致实际未成交。", "只比较三标的共有且完成预热的真实现货历史，不用其他资产补齐；共同历史较短，全部视为探索。", "普通1次/最多4次及同动用预算对照均接收完全相同的逐日新增资金。固定30U参考的资金流不同，不直接以净值差宣称策略优势。", "不把SOL数量、黄金代币数量、股票代币数量相加计算一个混合均价；只展示各标的含费单位成本。", "手续费/滑点和现有交易过滤器仍为简化假设；不计现金利息、分红及其他公司行动；不下真实订单。"]}
    if family != "original":
        report["limitations"].extend(["新四个实验在同一段已看过的历史上探索，未作独立样本外验证；不以挑选最高收益版本证明有效。", "基础单保留60%新增资金，机会单无信号不买；每天1--4是计划槽位，最低金额或无成交可能导致实际成交更少。", "机会单可使用历史现金；日支出可能超过当天新增，全部来自已有现金，不借款。现金超出最近六日新增总额的部分在基础单中释放，并非逐笔现金的严格七日到期。", "同动用预算对照使用候选全天实际支出事后配平，只用于分解现金效应；不是可实盘执行的先验基准。", "触发分类和24小时后涨跌仅用于诊断，存在选样和标的分配差异，不能作为择时因果收益。"])
    if family in ("discount", "signal", "signal_v2"):
        report["limitations"].extend(["本轮只新增机会单0.5ATR折扣门槛，基础单和现金释放仍不受门槛限制，不能保证整个组合都低价买入。", "折扣线由上一根闭合小时EMA168与ATR14计算；模拟只在下一小时开盘可执行报价（含假设滑点）不高于上限时成交，不使用该小时最低价假装限价成交。实盘盘口、延迟和订单队列尚未建模。", "折扣不是低于普通定投均价的保证；剩余基础单、日期和资产分配变化都可能拉高整体成本。图上的折扣线是各小时决策前可知的上限；4小时/日线显示桶内最后一小时的上限，单笔依据以订单明细为准。"])
    if family in ("signal", "signal_v2", "indicator"):
        report["limitations"] = ["当前默认纯机会单：没有基础单，也没有现金到期强制买入；可以全天0成交，1--4仅为计划窗口数。", "所有买入均需价格折扣和原确认信号同时成立；每日最多4笔，任意两笔成功成交至少间隔4小时。", "闲置资金保留且计入净值，不会为了改善成本而删除现金或只展示成交日。高现金占比、上涨踏空和不足买入数量必须同时评估。", "旧版基础单与现金释放规则仅用于历史对照，仍保留在下拉框中，不属于当前默认策略。", *report["limitations"][:9], "现有历史全部已看过，本轮仍是探索，未获得独立前向证据。", "同动用预算对照使用候选全天实际支出作为事后目标预算，非先验可执行策略，数量网格使实际支出仍有偏差。", "图中折扣虚线来自上一闭合小时；大周期显示桶内最后一小时上限，每笔依据以订单记录为准。不根据小时最低价制造限价成交。"]
    if family == "signal_v2":
        report["limitations"].extend(["本轮预设三个累进实验：跨标的扫描、信号成立时折扣现金加码、同标的24小时内更低价加码。默认显示现金加码版只是研究预选，不表示最优或已验证。", "新三版均无基础单、无现金到期补投，旧含基础单版本仅作对照。权重上限在买入前检查，是软约束，不保证成交后不会超过目标+15个百分点。", "阶梯版单笔上限可能低于图中EMA折扣参考线；以订单price_cap为准。对照资金动用计划为事后配平，且有数量步长偏差，不能证明因果择时收益。"])
    if family == "indicator":
        report.update(version="0.10.0", selection=selection, default_policy=selection["selected_policy"])
        report["limitations"].extend(["八个固定指标模板，连同四个旧纯机会版，只在前60%开发段按冻结评分选优；中段和末段不参与选择。全部历史已暴露，不是独立样本外验证。", "新模板共用现金加码、折扣报价上限及冷却规则；不强制买入。RSI14<=40且回升2点；布林20小时SMA±2倍总体标准差；均线要求从下方收复EMA72；所有模板额外要求收盘回升。", "开发段覆盖门槛：动用率>=60%、至少20笔成交、各标的数量>=普通4次的50%。评分偏好资金效率及成本，但属于人为研究权重，不保证未来表现。"])
    if len(KEYS) == 5:
        report["version"] = "0.11.0"
        report["limitations"] = [s.replace("三等权", "五标的等权20%（研究假设）").replace("三标的", "五标的").replace("三个标的", "五个标的") for s in report["limitations"]]
        report["limitations"].append("增加BTC/ETH改变了资产组合、权重和日新增信号，不能直接与旧三标的组合净值差归因为择时改善；基准也同时扩为五标的并采用相同资金流。BTC/ETH/SOL高度相关，五等权不是风险平价。")
        report["limitations"].append("额外3个固定实验：RSI或布林互补触发；4小时下降趋势或未知时要求RSI和布林同时确认；在相同确认规则上按EMA偏离和趋势调节额度至0.7–1.3倍，仍受现金、最低金额及日预算约束。五标的首轮结果已查看，本轮不是独立验证。原3个标的沿用10月30日规则快照，BTC/ETH使用10月31日快照；当前过滤器用于历史模拟，不代表逐时历史规则。")
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    return report
