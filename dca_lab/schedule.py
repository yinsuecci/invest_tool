"""Frozen, causal daily schedules. No parameter search or future-price selection."""

from dataclasses import replace
from math import isfinite


POLICIES = ("fixed_four", "frequency", "timing", "sizing")
POLICY_LABELS = {"fixed_four": "普通定投：固定 4 次", "fixed_once": "普通定投：固定 1 次",
                 "frequency": "波动自适应 1–4 次", "timing": "+ 窗口内低位等待", "sizing": "+ 偏离 / RSI 额度"}
THRESHOLDS = (0.75, 1.0, 1.5)
TIMING_THRESHOLD = 0.25
REVERSAL_POLICIES = ("timing", "rsi_rebound", "bottom", "ma_sizing", "exposure")
REVERSAL_LABELS = {"timing": "上一轮：窗口低位等待", "rsi_rebound": "+ RSI 超卖回升", "bottom": "+ 探底回收 / 锤线确认",
                   "ma_sizing": "+ EMA168 偏离额度", "exposure": "+ 现金 / 仓位软目标"}


def daily_plan(feature, config, policy, once_hour=0, minimum_order=0):
    if policy not in (*POLICIES, *REVERSAL_POLICIES, "fixed_once"):
        raise ValueError("Unknown schedule policy")
    if policy == "fixed_once":
        if not isinstance(once_hour, int) or not 0 <= once_hour <= 23:
            raise ValueError("Single-buy anchor must be a local hour from 0 to 23")
        count, ratio = 1, None
        windows = ((once_hour, once_hour + 1),)
    elif policy == "fixed_four":
        count, ratio = 4, None
        windows = tuple((h, h + 1) for h in (0, 6, 12, 18))
    else:
        ratio = feature.volatility_multiple
        if ratio is None or not isfinite(ratio) or ratio < 0:
            raise ValueError("Adaptive scheduling requires a closed volatility reference")
        count = 1 + sum(ratio >= threshold for threshold in THRESHOLDS)
        boundaries = [i * 24 // count for i in range(count + 1)]
        windows = tuple(zip(boundaries, boundaries[1:]))
    if minimum_order:
        while count > 1 and config.principal / count * (1 if policy in ("fixed_once", "fixed_four", "frequency", "timing", "rsi_rebound", "bottom") else 1 - config.max_deviation) < minimum_order:
            count -= 1
        if policy == "fixed_once":
            windows = ((once_hour, once_hour + 1),)
        elif policy == "fixed_four":
            windows = tuple((i * 24 // count, i * 24 // count + 1) for i in range(count))
        else:
            boundaries = [i * 24 // count for i in range(count + 1)]
            windows = tuple(zip(boundaries, boundaries[1:]))
    local = replace(config, center=config.principal / count, min_count=count, max_count=count)
    return local, windows, ratio


def entry_signal(feature, policy):
    if policy in ("timing", "sizing"):
        value = .7 * feature.deviation + .3 * feature.momentum
        return value >= TIMING_THRESHOLD, "low_signal", value
    if feature.rsi_rebound:
        return True, "rsi_rebound", feature.rsi_value
    if policy in ("bottom", "ma_sizing", "exposure"):
        if feature.sweep_reclaim:
            return True, "sweep_reclaim", feature.rsi_value
        if feature.hammer_confirmed:
            return True, "hammer_confirmation", feature.rsi_value
    return False, None, feature.rsi_value


def exposure_budget(cash, units, last_closed_price, contribution, feature):
    """A buy-only soft target, not a hard cap or an instruction to liquidate."""
    rsi_value = feature.rsi_value if feature.rsi_value is not None else 50
    target = min(.95, max(.60, .80 + .10 * feature.long_deviation - .15 * (rsi_value - 50) / 50))
    asset_value = units * last_closed_price
    after_contribution = cash + contribution + asset_value
    headroom = target * after_contribution - asset_value
    budget = min(contribution, max(.25 * contribution, headroom))
    return budget, target


def daily_funding(feature, center=30., deviation=.30):
    if not isfinite(center) or center <= 0 or not isfinite(deviation) or not 0 <= deviation < 1:
        raise ValueError("Invalid daily funding center or deviation")
    signal = max(-1., min(1., .65 * feature.long_deviation + .35 * feature.momentum))
    return round(center * (1 + deviation * signal), 2)
