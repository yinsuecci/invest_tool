"""A finite signal catalogue and development-only research selection."""

import hashlib
import json


RULES = {
    "mix_rsi": ("RSI低位回升", "rsi"),
    "mix_bb": ("布林下轨回收", "bb"),
    "mix_ma": ("均线EMA72收复", "ma"),
    "mix_rsi_bb": ("RSI + 布林下轨", "rsi_bb"),
    "mix_rsi_ma": ("RSI + 均线结构", "rsi_ma"),
    "mix_bb_ma": ("布林下轨 + 均线结构", "bb_ma"),
    "mix_vote": ("RSI / 布林 / 均线两票", "vote"),
    "mix_all": ("RSI + 布林 + 均线共振", "all"),
}
INDICATOR_POLICIES = tuple(RULES)
FIVE_POLICIES = ("mix_or", "mix_guard", "mix_sizing")
RULES.update({"mix_or": ("五标的1：RSI或布林互补", "either"),
              "mix_guard": ("五标的2：+ 下跌趋势双确认", "guard"),
              "mix_sizing": ("五标的3：+ 偏离与趋势额度", "guard")})
ALL_INDICATOR_POLICIES = (*INDICATOR_POLICIES, *FIVE_POLICIES)
SELECTION_PROTOCOL = {
    "candidates": ["discount_only", "signal_scan", "signal_reserve", "signal_ladder", *INDICATOR_POLICIES],
    "selection_data": "First60% development only; never validation, holdout or full-period returns.",
    "minimum_deployment": .60, "minimum_fills": 20, "minimum_each_asset_quantity_ratio": .50,
    "score": "min(equity gain % contributions vs once, vs four) + .25*mean(asset cost gain % vs four) - .5*max(0,-drawdown delta pp vs four) - 2*max(0,.8-deployment ratio)",
    "ranking": "Eligible candidates first, then descending score, then fixed catalogue order. If none eligible, choose a labelled research-only fallback; never call it qualified.",
    "rsi": "Wilder RSI14<=40 and rises >=2 from preceding closed hour.",
    "bb": "20 consecutive hourly closes, SMA +/-2 population standard deviations. Closed lower-band touch with bullish reclaim OR prior close below prior lower band followed by closed reentry.",
    "ma": "Prior close below prior EMA72; current close reclaims current EMA72. Shared EMA168 discount still required.",
    "common": "Closed close must rise; every candidate still needs closed .5ATR discount and executable next-open quote<=cap. Same scan, cash sizing, 0--4 fills/day and 4h cooldown as signal_reserve. No base or compulsory buys.",
    "limitations": "All history already exposed; retrospective selection, not independent forward evidence. Indicators are correlated. Scores are heuristic and uncorrected for multiple testing; no live activation.",
    "references": ["https://www.tradingview.com/support/solutions/43000501840-bollinger-bands-bb/"],
}


def indicator_entry(feature, previous, close, previous_close, policy):
    if policy not in RULES or close <= previous_close:
        return False, None, feature.rsi_value
    rsi = (feature.rsi_value is not None and previous.rsi_value is not None
           and feature.rsi_value <= 40 and feature.rsi_value >= previous.rsi_value + 2)
    bb = feature.bb_touch_reclaim or feature.bb_reentry
    ma = (feature.ema72 is not None and feature.ema168 is not None and previous.ema72 is not None
          and previous_close < previous.ema72 and close >= feature.ema72)
    tests = {"rsi": rsi, "bb": bb, "ma": ma, "rsi_bb": rsi and bb,
             "rsi_ma": rsi and ma, "bb_ma": bb and ma,
             "vote": sum((rsi, bb, ma)) >= 2, "all": rsi and bb and ma,
             "either": rsi or bb, "guard": (rsi or bb) and (feature.downtrend is False or (rsi and bb))}
    hit = bool(tests[RULES[policy][1]])
    return hit, policy if hit else None, feature.rsi_value


def select_development(records, candidates=None):
    rows = []
    for index, key in enumerate(candidates or SELECTION_PROTOCOL["candidates"]):
        p = records[key]
        current, base, diff = p["current"], p["four"], p["vs_four"]
        gains = list(diff["asset_cost_gain_pct"].values())
        finite_costs = [v for v in gains if v is not None]
        checks = {"deployment": current["execution_rate"] >= .60,
                  "fills": sum(d["fills"] for d in current["daily"]) >= 20,
                  "all_assets": len(finite_costs) == len(gains),
                  "quantity": all(base["assets"][k]["quantity"] > 0 and a["quantity"] / base["assets"][k]["quantity"] >= .50 for k, a in current["assets"].items())}
        mean_cost = sum(finite_costs) / len(finite_costs) if finite_costs else -100.
        gain = min(diff["equity_gain_pct_of_contributions"], p["vs_once"]["equity_gain_pct_of_contributions"])
        score = gain + .25 * mean_cost - .5 * max(0., -diff["drawdown_delta_pp"]) - 2 * max(0., .8 - current["execution_rate"])
        rows.append({"policy": key, "score": score, "eligible": all(checks.values()), "checks": checks,
                     "mean_cost_gain_pct": mean_cost, "worst_cost_gain_pct": min(finite_costs) if finite_costs else None,
                     "development_equity_gain_pct": gain, "development_execution": current["execution_rate"], "catalogue_index": index})
    rows.sort(key=lambda r: (not r["eligible"], -r["score"], r["catalogue_index"]))
    return {"selected_policy": rows[0]["policy"], "eligible_count": sum(r["eligible"] for r in rows), "ranking": rows,
            "selection_period": "development", "independent_evidence": False,
            "status": "开发段相对最优研究候选，尚未验证" if rows[0]["eligible"] else "无候选通过覆盖门槛，仅展示研究回退"}


def lock_selection(output, selection, protocol_sha256, filename="selection-lock.json"):
    payload = {"selection": selection, "protocol_sha256": protocol_sha256}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    path = output / filename
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8"))["sha256"] != digest:
            raise ValueError("Selection changed; preserve the prior run and use another output directory")
    else:
        path.write_text(json.dumps({"sha256": digest, **payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    return digest
