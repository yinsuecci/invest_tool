"""Causal features: a feature at index i only consumes bars <= i."""

from collections import deque
from dataclasses import dataclass
from statistics import median
from math import sqrt

from .market import HOUR
from .vwap_rules import rolling_vwap


def clip(value, low, high):
    return min(max(value, low), high)


def smooth(values, period, wilder=False):
    output, state, seed = [], None, []
    alpha = 1 / period if wilder else 2 / (period + 1)
    for value in values:
        if state is None:
            seed.append(value)
            if len(seed) == period:
                state = sum(seed) / period
        else:
            state += alpha * (value - state)
        output.append(state)
    return output


def rsi(closes, period=14):
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ups = smooth([max(x, 0) for x in changes], period, True)
    downs = smooth([max(-x, 0) for x in changes], period, True)
    result = [None]
    for up, down in zip(ups, downs):
        if up is None:
            result.append(None)
        elif up == down == 0:
            result.append(50.0)
        elif down == 0:
            result.append(100.0)
        else:
            result.append(100 - 100 / (1 + up / down))
    return result


def bollinger(closes, period=20, multiplier=2.):
    if period < 2 or multiplier <= 0:
        raise ValueError("Invalid Bollinger parameters")
    window, result = deque(maxlen=period), []
    for value in closes:
        window.append(value)
        if len(window) < period:
            result.append((None, None, None))
            continue
        mean = sum(window) / period
        spread = multiplier * sqrt(sum((x - mean) ** 2 for x in window) / period)
        result.append((mean, mean - spread, mean + spread))
    return result


@dataclass(frozen=True)
class Feature:
    deviation: float
    momentum: float
    downtrend: bool | None
    volatility_multiple: float | None
    count_1h: int
    count_4h: int
    rsi_value: float | None = None
    rsi_rebound: bool = False
    sweep_reclaim: bool = False
    hammer_confirmed: bool = False
    ema72: float | None = None
    ema168: float | None = None
    long_deviation: float = 0.0
    atr14: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_upper: float | None = None
    bb_touch_reclaim: bool = False
    bb_reentry: bool = False
    vwap20: float | None = None


def features(bars):
    closes = [b.close for b in bars]
    ema = smooth(closes, 24)
    true_ranges = [max(b.high - b.low, abs(b.high - bars[i - 1].close), abs(b.low - bars[i - 1].close)) if i else b.high - b.low for i, b in enumerate(bars)]
    atr = smooth(true_ranges, 14, True)
    rsis = rsi(closes)
    medium, long = smooth(closes, 72), smooth(closes, 168)
    bands = bollinger(closes)
    vwaps = rolling_vwap(bars)
    # Complete UTC four-hour candles only. A partial bucket never updates trend.
    four_closes, completed_at = [], {}
    bucket = []
    for i, bar in enumerate(bars):
        if bar.time % (4 * HOUR) == 0:
            bucket = [bar]
        elif bucket and bar.time == bucket[-1].time + HOUR:
            bucket.append(bar)
        else:
            bucket = []
        if len(bucket) == 4:
            four_closes.append(bar.close)
            completed_at[i] = len(four_closes) - 1
            bucket = []
    fast, slow = smooth(four_closes, 50), smooth(four_closes, 200)
    history = deque(maxlen=720)
    output, n4, trend, consecutive = [], 0, None, 0
    for i, bar in enumerate(bars):
        if i and bar.time != bars[i - 1].time + HOUR:
            # Do not trade after missing data until a fresh warmup has accumulated.
            consecutive = 0
            history.clear()
            n4 = 0
        consecutive += 1
        if i in completed_at:
            j = completed_at[i]
            n4 += 1
            trend = None if slow[j] is None else fast[j] < slow[j]
        v = atr[i] / bar.close if atr[i] is not None else None
        reference = median(history) if len(history) == 720 else None
        q = v / reference if v is not None and reference and reference > 0 else None
        if v is not None:
            history.append(v)
        if ema[i] is None or rsis[i] is None or atr[i] is None or atr[i] <= 0:
            output.append(None)
            continue
        rebound, sweep, hammer = bottom_signals(bars, rsis, i)
        middle, lower, upper = bands[i] if consecutive >= 20 else (None, None, None)
        touch = lower is not None and bar.low <= lower <= bar.close and bar.close > bar.open
        reentry = (lower is not None and consecutive >= 21 and bands[i - 1][1] is not None
                   and bars[i - 1].close < bands[i - 1][1] and bar.close >= lower and bar.close > bars[i - 1].close)
        output.append(Feature(clip((ema[i] - bar.close) / (2 * atr[i]), -1, 1), clip((50 - rsis[i]) / 30, -1, 1), trend, q, consecutive, n4,
                              rsis[i], rebound, sweep, hammer, medium[i], long[i],
                              clip((long[i] - bar.close) / (2 * atr[i]), -1, 1) if long[i] is not None else 0, atr[i],
                              middle, lower, upper, touch, reentry, vwaps[i]))
    return output


def ready(feature, stage, config):
    if feature is None or feature.count_1h < config.warmup_1h:
        return False
    if stage in ("trend", "volatility") and (feature.count_4h < config.warmup_4h or feature.downtrend is None):
        return False
    return stage != "volatility" or feature.volatility_multiple is not None


def score(feature, stage):
    if stage == "equal":
        return 0.0
    if stage == "ma_sizing":
        bottom = feature.rsi_rebound or feature.sweep_reclaim or feature.hammer_confirmed
        return clip(.55 * feature.long_deviation + .30 * feature.momentum + .15 * bottom, -1, 1)
    value = feature.deviation
    if stage in ("rsi", "trend", "volatility"):
        value = 0.7 * value + 0.3 * feature.momentum
    if stage in ("trend", "volatility") and feature.downtrend and value > 0:
        value *= 0.5
    if stage == "volatility":
        value *= clip(1 / max(feature.volatility_multiple, 1), 0.25, 1)
    return value


def bottom_signals(bars, rsis, i):
    """Only closed bars through i. A hammer requires a later closed confirmation."""
    if i < 12 or any(bars[j].time != bars[j - 1].time + HOUR for j in range(i - 11, i + 1)):
        return False, False, False
    b, prev, current, prior = bars[i], bars[i - 1], rsis[i], rsis[i - 1]
    if current is None or prior is None or any(v is None for v in rsis[i - 3:i]):
        return False, False, False
    rebound = min(rsis[i - 3:i]) <= 35 and current >= prior + 2 and b.close > prev.close
    support = min(x.low for x in bars[i - 12:i])
    sweep = b.low < support and b.close > support and b.close > b.open and current >= prior
    body = abs(prev.close - prev.open)
    span = prev.high - prev.low
    lower = min(prev.open, prev.close) - prev.low
    hammer_shape = span > 0 and lower >= max(2 * body, .5 * span) and (prev.close - prev.low) / span >= .6
    hammer = hammer_shape and prior <= 40 and b.close > prev.high and current > prior
    return rebound, sweep, hammer
