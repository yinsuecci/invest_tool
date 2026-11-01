"""Event-driven, next-bar spot execution with bounded daily allocation."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from math import isfinite

from .indicators import clip, ready, score
from .market import HOUR, iso
from .schedule import REVERSAL_POLICIES, daily_plan, entry_signal, exposure_budget


@dataclass(frozen=True)
class Rules:
    step: str = "0.000001"
    min_qty: float = 0.0
    max_qty: float = 1e30
    min_notional: float = 5.0
    max_notional: float = 1e30

    @classmethod
    def from_symbol(cls, symbol):
        filters = {f["filterType"]: f for f in symbol["filters"]}
        lot = filters["LOT_SIZE"]
        market = filters.get("MARKET_LOT_SIZE", {})
        steps = [Decimal(lot["stepSize"])]
        if Decimal(market.get("stepSize", "0")) > 0:
            steps.append(Decimal(market["stepSize"]))
        step = max(steps)
        if step <= 0 or any(step % x != 0 for x in steps):
            raise ValueError("Unsupported quantity step combination")
        minimum = 0.0
        maximum = 1e30
        for kind in ("MIN_NOTIONAL", "NOTIONAL"):
            f = filters.get(kind, {})
            if f.get("applyToMarket", f.get("applyMinToMarket", False)):
                minimum = max(minimum, float(f["minNotional"]))
            if f.get("applyMaxToMarket", False):
                maximum = min(maximum, float(f["maxNotional"]))
        max_quantities = [float(lot["maxQty"])]
        if float(market.get("maxQty", 0)) > 0:
            max_quantities.append(float(market["maxQty"]))
        return cls(str(step), max(float(lot["minQty"]), float(market.get("minQty", 0))), min(max_quantities), minimum, maximum)

    def quantity(self, amount, price):
        step = Decimal(self.step)
        q = (Decimal(str(amount)) / Decimal(str(price)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(q)

    def bounded_quantity(self, target, price, lower, upper):
        step, px = Decimal(self.step), Decimal(str(price))
        minimum = max(Decimal(str(lower)), Decimal(str(self.min_notional)))
        maximum = min(Decimal(str(upper)), Decimal(str(self.max_notional)))
        low_steps = max((minimum / px / step).to_integral_value(rounding=ROUND_CEILING), (Decimal(str(self.min_qty)) / step).to_integral_value(rounding=ROUND_CEILING))
        high_steps = min((maximum / px / step).to_integral_value(rounding=ROUND_DOWN), (Decimal(str(self.max_qty)) / step).to_integral_value(rounding=ROUND_DOWN))
        if low_steps > high_steps:
            return 0.0
        desired = (Decimal(str(target)) / px / step).to_integral_value(rounding=ROUND_DOWN)
        return float(min(max(desired, low_steps), high_steps) * step)


def amount_for_slot(remaining, count, signal, config):
    if count < 1 or remaining < 0:
        raise ValueError("Invalid remaining allocation")
    lo = max(config.lower, remaining - (count - 1) * config.upper)
    hi = min(config.upper, remaining - (count - 1) * config.lower)
    if lo > hi + 1e-8:
        return None
    raw = remaining / count * (1 + config.alpha * clip(signal, -1, 1))
    return clip(raw, lo, hi)


def day_groups(bars, feature_list, stage, config):
    zone = timezone(timedelta(hours=config.timezone_offset_hours))
    groups = {}
    for i, bar in enumerate(bars):
        date = datetime.fromtimestamp(bar.time / 1000, zone).date().isoformat()
        groups.setdefault(date, []).append(i)
    eligible = []
    for date, indices in groups.items():
        if len(indices) != 24 or any(bars[b].time != bars[a].time + HOUR for a, b in zip(indices, indices[1:])):
            continue
        if any(i == 0 or bars[i].time != bars[i - 1].time + HOUR or not ready(feature_list[i - 1], stage, config) for i in indices if datetime.fromtimestamp(bars[i].time / 1000, zone).hour in config.hours):
            continue
        eligible.append((date, indices))
    return eligible


def simulate(bars, feature_list, days, stage, config, rules, include_orders=False, schedule=None, once_hour=0, deployment_budgets=None, contribution_schedule=None, enforce_trade_minimum=False):
    cash = units = contributed = spent = fees = 0.0
    nav = peak = 1.0
    max_drawdown = 0.0
    previous_equity = None
    daily, orders, curve = [], [], []
    skipped = 0
    offset = config.timezone_offset_hours

    def revalue(before, after):
        nonlocal nav, peak, max_drawdown
        if before > 0:
            nav *= after / before
            peak = max(peak, nav)
            max_drawdown = min(max_drawdown, nav / peak - 1)

    for date, indices in days:
        start = bars[indices[0]]
        decision_feature = feature_list[indices[0] - 1]
        contribution = contribution_schedule[date] if contribution_schedule is not None else config.daily_total
        if not isfinite(contribution) or contribution <= 0:
            raise ValueError("Daily contributions must be positive and finite")
        deploy, target_exposure = contribution, None
        if schedule == "exposure":
            deploy, target_exposure = exposure_budget(cash, units, bars[indices[0] - 1].close, contribution, decision_feature)
        elif deployment_budgets is not None:
            deploy = deployment_budgets[date]
            if not 0 < deploy <= contribution:
                raise ValueError("Matched deployment must stay within the daily contribution")
        minimum_order = rules.min_notional + float(rules.step) * bars[indices[0] - 1].close * (1 + config.slippage_bps / 10000) if enforce_trade_minimum else 0
        if minimum_order and deployment_budgets is None:
            deploy = min(contribution, max(deploy, minimum_order * (1 + config.fee)))
        day_config = replace(config, daily_total=deploy, center=deploy / (1 + config.fee) / config.count) if deploy != config.daily_total else config
        local, windows, day_volatility = daily_plan(decision_feature, day_config, schedule, once_hour, minimum_order) if schedule else (day_config, tuple((h, h + 1) for h in day_config.hours), None)
        next_slot = 0
        daily_order_count = 0
        if previous_equity is not None:
            revalue(previous_equity, cash + units * start.open)
        cash += contribution
        contributed += contribution
        remaining = local.principal
        daily_spent = daily_fee = daily_units = 0.0
        previous_close_equity = cash + units * start.open
        for index in indices:
            bar = bars[index]
            pretrade = cash + units * bar.open
            revalue(previous_close_equity, pretrade)
            hour = (bar.time // HOUR + offset) % 24
            in_window = next_slot < len(windows) and windows[next_slot][0] <= hour < windows[next_slot][1]
            waiting = schedule in (*REVERSAL_POLICIES, "sizing")
            deadline = in_window and hour == windows[next_slot][1] - 1
            hit, trigger, timing_signal = entry_signal(feature_list[index - 1], schedule) if in_window and waiting else (False, None, None)
            if in_window and (not waiting or hit or deadline):
                slot = next_slot
                next_slot += 1
                daily_order_count += 1
                signal = score(feature_list[index - 1], stage)
                target = amount_for_slot(remaining, local.count - slot, signal, local)
                reason = None
                quantity = notional = fee = 0.0
                fill_price = bar.open * (1 + config.slippage_bps / 10000)
                if target is None:
                    reason = "remaining_budget_infeasible"
                elif bar.volume == 0:
                    # A no-trade hour cannot substantiate a market fill. This is
                    # an execution outcome, never an input to the signal.
                    reason = "no_historical_trades_in_execution_hour"
                else:
                    # Bounds apply to the planned quoteOrderQty budget. Actual
                    # fills may be smaller due to the base-quantity grid; keep
                    # this dust as cash instead of falsely rejecting the order.
                    quantity = rules.quantity(min(target, cash / (1 + config.fee)), fill_price)
                    notional = quantity * fill_price
                    fee = notional * config.fee
                    if quantity <= 0 or quantity < rules.min_qty or quantity > rules.max_qty or notional < rules.min_notional - 1e-8 or notional > rules.max_notional + 1e-8:
                        reason = "quantity_or_notional_filter"
                    elif notional + fee > cash + 1e-8 or notional > remaining + 1e-8:
                        reason = "budget_limit"
                if reason:
                    skipped += 1
                else:
                    cash -= notional + fee
                    units += quantity
                    spent += notional
                    fees += fee
                    daily_spent += notional
                    daily_fee += fee
                    daily_units += quantity
                if target is not None:
                    # A slot owns its planned budget even if unfilled. Do not
                    # concentrate failed slots or rounding dust in later orders.
                    remaining = max(0.0, remaining - target)
                if include_orders:
                    f = feature_list[index - 1]
                    orders.append({"day": date, "time": iso(bar.time), "feature_cutoff": iso(bars[index - 1].time + HOUR), "stage": stage, "signal": signal, "target": target, "notional": notional if not reason else 0, "fee": fee if not reason else 0, "quantity": quantity if not reason else 0, "price": fill_price, "unspent_slot_budget": target - notional if target is not None and not reason else target, "deviation": f.deviation, "momentum": f.momentum, "downtrend": f.downtrend, "volatility_multiple": f.volatility_multiple, "skip_reason": reason,
                                   "day_count": local.count, "day_volatility": day_volatility, "trigger": trigger if waiting and hit else "window_deadline" if waiting else "scheduled", "timing_signal": timing_signal,
                                   "day_decision_cutoff": iso(start.time), "planned_lower": local.lower, "planned_upper": local.upper,
                                   "rsi_value": f.rsi_value, "ma_deviation": f.long_deviation, "day_deployment": deploy, "target_exposure": target_exposure,
                                   "day_contribution": contribution, "minimum_order_guard": minimum_order,
                                   "bottom_flags": {"rsi_rebound": f.rsi_rebound, "sweep_reclaim": f.sweep_reclaim, "hammer_confirmed": f.hammer_confirmed}})
            posttrade = cash + units * bar.open
            revalue(pretrade, posttrade)
            equity = cash + units * bar.close
            revalue(posttrade, equity)
            previous_close_equity = equity
        previous_equity = previous_close_equity
        daily.append({"date": date, "spent": daily_spent + daily_fee, "quantity": daily_units, "unit_cost": (daily_spent + daily_fee) / daily_units if daily_units else None,
                      "planned_count": local.count, "attempted_count": daily_order_count, "volatility_ratio": day_volatility,
                      "planned_deployment": deploy, "target_exposure": target_exposure, "contribution": contribution})
        curve.append({"date": date, "equity": previous_equity, "contributions": contributed, "twr_index": nav * 100,
                      "quantity": units, "cash": cash, "spent_with_fees": spent + fees, "daily_contribution": contribution,
                      "position_fraction": units * bars[indices[-1]].close / previous_equity if previous_equity else 0})
    last_price = bars[days[-1][1][-1]].close if days else 0
    equity = cash + units * last_price
    return {
        "stage": stage, "days": len(days), "start": days[0][0] if days else None, "end": days[-1][0] if days else None,
        "contributed": contributed, "spent": spent, "fees": fees, "cash": cash, "quantity": units,
        "unit_cost": (spent + fees) / units if units else None,
        "equity": equity, "profit": equity - contributed,
        "position_fraction": units * last_price / equity if equity else 0,
        "simple_return": equity / contributed - 1 if contributed else 0,
        "twr_return": nav - 1, "max_drawdown": max_drawdown,
        "execution_rate": (spent + fees) / contributed if contributed else 0,
        "skipped_slots": skipped, "daily": daily, "curve": curve, "orders": orders,
    }
