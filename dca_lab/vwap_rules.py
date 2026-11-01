"""Causal rolling HLC3 VWAP tiers; finite catalogue, not hindsight lows."""

from collections import deque
from .market import HOUR

THRESHOLDS = {"tight": (0., .005, .015, .03), "balanced": (0., .01, .03, .06), "wide": (0., .02, .05, .10)}
SIZES = {"steady": (1., 2., 3., 4.), "strong": (1., 2., 4., 6.)}
PROFILES = {f"{band}_{size}": {"discounts": list(discounts), "multipliers": list(multipliers)}
            for band, discounts in THRESHOLDS.items() for size, multipliers in SIZES.items()}
VWAP_POLICIES = ("vwap_fixed", "vwap_fitted", "vwap_bottom", "vwap_rsi", "vwap_either")
LABELS = {"vwap_fixed": "VWAP：统一默认档位", "vwap_fitted": "VWAP：逐标的开发段拟合", "vwap_bottom": "VWAP：+ 探底K线确认", "vwap_rsi": "VWAP：+ RSI确认", "vwap_either": "VWAP：+ 探底或RSI确认"}


def rolling_vwap(bars, hours=480):
    if hours < 1:
        raise ValueError("Positive rolling window required")
    window, result = deque(), []
    numerator = denominator = 0.
    nonzero = 0
    previous = None
    for bar in bars:
        if previous is not None and bar.time != previous + HOUR:
            window.clear()
            numerator = denominator = 0.
            nonzero = 0
        value = (bar.high + bar.low + bar.close) / 3 * bar.volume
        window.append((value, bar.volume))
        numerator += value
        denominator += bar.volume
        nonzero += bar.volume > 0
        if len(window) > hours:
            old_value, old_volume = window.popleft()
            numerator -= old_value
            denominator -= old_volume
            nonzero -= old_volume > 0
        if not nonzero:
            numerator = denominator = 0.
        result.append(numerator / denominator if len(window) == hours and nonzero and denominator > 0 else None)
        previous = bar.time
    return result


def tier_signal(feature, close, profile, policy):
    reference = feature.vwap20
    if reference is None or reference <= 0:
        return None
    depth = 1 - close / reference
    tiers = [i for i, threshold in enumerate(profile["discounts"]) if depth + 1e-12 >= threshold]
    if not tiers:
        return None
    if profile.get('rsi_max') is not None and (feature.rsi_value is None or feature.rsi_value > profile['rsi_max']):
        return None
    bottom = feature.sweep_reclaim or feature.hammer_confirmed
    rsi = feature.rsi_rebound
    confirmed = policy in ("vwap_fixed", "vwap_fitted") or (policy == "vwap_bottom" and bottom) or (policy == "vwap_rsi" and rsi) or (policy == "vwap_either" and (bottom or rsi))
    if not confirmed:
        return None
    tier = max(tiers)
    cap = reference * (1 - profile['discounts'][tier])
    if profile.get('ema_discount') is not None:
        if feature.ema168 is None or feature.ema168 <= 0:
            return None
        cap = min(cap, feature.ema168 * (1-profile['ema_discount']))
    if profile.get('bb_cap'):
        if feature.bb_lower is None or feature.bb_lower <= 0:
            return None
        cap = min(cap, feature.bb_lower)
    if close > cap + 1e-12:
        return None
    return {"tier": tier, "depth": depth, "reference": reference,
            "cap": cap, "multiplier": profile["multipliers"][tier]}
