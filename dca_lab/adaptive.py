"""Pre-registered finite ablations for 1--4 daily buys, never search-until-win."""

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import ASSETS, Config
from .engine import Rules, day_groups, simulate
from .evaluate import SCENARIOS, compact, comparison, confidence_interval, new_report, paired_gains, visual_orders
from .indicators import features
from .market import load_dataset
from .schedule import POLICIES, POLICY_LABELS, REVERSAL_LABELS, REVERSAL_POLICIES, THRESHOLDS, TIMING_THRESHOLD


def frozen_protocol(config, manifest, family="adaptive"):
    protocol = {
        "version": "adaptive-1.0", "counts": [1, 2, 3, 4], "thresholds": list(THRESHOLDS),
        "volatility": "At day start, previous closed 1h ATR14/price divided by median of preceding 720 closed-hour ATR/price values.",
        "timing": {"score": "0.7*deviation+0.3*momentum", "threshold": TIMING_THRESHOLD,
                   "rule": "First eligible closed-hour signal in each window, otherwise buy at final hour open. No future lows."},
        "amount": "Daily principal / planned count, bounded +/- configured deviation; no carry-forward of failed slots.",
        "policies": list(POLICIES), "benchmarks": ["fixed_four", "fixed_once_00"], "once_anchor_hours": [0, 6, 12, 18],
        "split": [0.6, 0.2, 0.2], "threshold_cost_bps": 50, "threshold_equity_over_contributions_pct": 0.5,
        "risk_limit_drawdown_pp": 0.2, "fresh_forward_days_required": 90,
        "evidence": "All downloaded history was already exposed. Retrospective only, never independent out-of-sample evidence.",
        "search": "None; exactly three fixed incremental modules, all results retained; no best-asset or best-period selection.",
        "config": config.to_dict(), "data_sha256": {k: a["sha256"] for k, a in manifest["assets"].items()},
        "source": "https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf",
    }
    if family == "reversal":
        protocol.update({"version": "reversal-1.0", "policies": list(REVERSAL_POLICIES),
                         "search": "Exactly four frozen modules; no parameter search, no selective result deletion.",
                         "rsi_rebound": "Within preceding 3 closed bars RSI<=35; current closed RSI rises by >=2 and close rises.",
                         "bottom": "Closed low sweeps previous 12 closed lows, close reclaims support and is bullish with non-falling RSI; OR previous hammer at RSI<=40 followed by closed close above its high.",
                         "hammer": "Previous lower wick >= max(2*body, 50% range), close in top40% of range; confirmation must already be closed.",
                         "allocation": "0.55*clip((EMA168-price)/(2*ATR14),-1,1)+0.30*RSI_momentum+0.15*confirmed_bottom, +/-30% feasible daily bounds.",
                         "position": "Daily soft target clip(0.8+0.1*long_deviation-0.15*(RSI-50)/50,0.6,0.95). Deploy min(new contribution,max(25% new contribution,target*equity-after-contribution-asset-value)). No sales or reserve catch-up.",
                         "matched_control": "Fixed-four control uses the candidate's causal day-start deployment budget; same full cash contributions, fees, and dates.",
                         "chart": "TradingView Lightweight Charts 5.2.1; local Binance data, no TradingView account or upload."})
    return protocol


def freeze(output, protocol):
    encoded = json.dumps(protocol, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    path = output / "adaptive-protocol.json"
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior["sha256"] != digest:
            raise ValueError("Protocol changed. Preserve the existing run and use a separate output directory; never overwrite failed experiments.")
    else:
        path.write_text(json.dumps({"frozen_at": datetime.now(timezone.utc).isoformat(), "sha256": digest, "protocol": protocol}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"sha256": digest, "protocol": protocol}


def meaningful_gate(periods, stress_checks):
    checks = {}
    for name, key in (("four", "vs_equal"), ("once", "vs_once")):
        p, v = periods["holdout"], periods["validation"]
        d, validation = p[key], v[key]
        baseline = p["baseline"] if name == "four" else p["baseline_once"]
        checks[name] = {
            "enough_days": validation["paired_days"] >= 28 and d["paired_days"] >= 90,
            "validation_positive": validation["mean_daily_cost_gain_bps"] is not None and validation["mean_daily_cost_gain_bps"] > 0,
            "cost_gain_at_least_50bps": d["aggregate_cost_gain_bps"] is not None and d["aggregate_cost_gain_bps"] >= 50,
            "mean_gain_ci_positive": bool(d["ci95_bps"] and d["ci95_bps"][0] > 0),
            "equity_gain_at_least_half_pct_of_contributions": (p["current"]["equity"] - baseline["equity"]) / p["current"]["contributed"] >= 0.005,
            "drawdown_not_worse": d["drawdown_delta_pp"] >= -0.2,
            "execution_not_worse": d["execution_delta_pp"] >= -0.1,
            "stress_positive": all(s[name]["aggregate_cost_gain_bps"] is not None and s[name]["aggregate_cost_gain_bps"] > 0 for s in stress_checks.values()),
        }
    enough = all(c["enough_days"] for c in checks.values())
    passes = all(all(c.values()) for c in checks.values())
    return {"status": "needs_fresh" if passes else "not_adopted" if enough else "insufficient",
            "label": "仅历史达标，待新数据验证" if passes else "未达显著优势目标" if enough else "样本不足，不启用",
            "reason": "所有现有历史已经展示，只能用于探索。必须相对固定1次及4次双基准同时满足成本、净值、回撤和压力情景目标；历史达标后还需至少90个全新前向日。",
            "checks": checks, "independent_evidence": False}


def walkforward_summary(previous, current, block=60):
    gains = paired_gains(previous, current)
    dates = [d["date"] for a, d in zip(previous["daily"], current["daily"]) if a["unit_cost"] and d["unit_cost"]]
    return [{"start": dates[i], "end": dates[min(i + block, len(dates)) - 1], "days": len(gains[i:i + block]),
             "mean_cost_gain_bps": sum(gains[i:i + block]) / len(gains[i:i + block]), "ci95_bps": confidence_interval(gains[i:i + block])}
            for i in range(0, len(gains), block)]


def evaluate_adaptive(directory, output, config=None, family="adaptive"):
    config = config or Config(center=40, min_count=1, max_count=4)
    manifest, info, datasets = load_dataset(directory)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if family not in ("adaptive", "reversal"):
        raise ValueError("Unknown experiment family")
    policies = REVERSAL_POLICIES if family == "reversal" else POLICIES
    labels = REVERSAL_LABELS if family == "reversal" else POLICY_LABELS
    protocol = freeze(output, frozen_protocol(config, manifest, family))
    report = new_report(manifest, config)
    report.update({"version": "0.3.0", "adaptive": protocol, "mode": "retrospective_adaptive_research"})
    if family == "reversal":
        report.update({"version": "0.4.0", "reversal": True, "chart_engine": "tradingview-lightweight-charts-5.2.1"})
    report["config"].update({"count_mode": "adaptive_1_4", "count": "1–4", "center_mode": "daily_principal_divided_by_count"})
    report["method"].update({"inference": "Retrospective exposed data only; no optimizer. Fixed 60-day sequential stability blocks, no re-fitting. Fresh forward evidence required.", "default_strategy": "fixed_four"})
    report["limitations"] = [s for s in report["limitations"] if not s.startswith(("固定预算模式仅", "累计阶段为固定"))]
    report["limitations"][:0] = [
        "本轮为已公开历史上的探索，不是新的独立留出验证；禁止反复搜索直到获胜。所有三个固定模块结果均保留。",
        "每天1–4次由当日00:00之前已闭合小时的ATR相对波动决定，日内次数不事后改写。窗口内低位等待仅用上一小时指标，不是最低点买入。",
        "日预算保持120 USDT实验规模；单笔中心改为当日本金/次数，±30%是当日动态中心边界，已不再是旧版14–26 USDT。",
        "普通定投同时比较固定4次（00/06/12/18时）及固定1次（00时），并另列06/12/18时单次基准敏感性，不挑最弱基准。",
        "较高波动增加分批次数不保证成本下降。所有策略仍保持每日相同资金注入，尾差留现金，不增加预算或杠杆。",
    ]
    if family == "reversal":
        report["limitations"][:0] = [
            "探底回收、锤线确认和RSI回升都只基于已经收盘的小时数据，不使用未来最低价或尚未确认的拐点。",
            "仓位软目标仅控制当天新增资金的动用量，至少动用25%、最多100%；不卖出已有持仓、不动用累积现金追补，因此不是硬仓位上限。上涨可导致实际仓位超过软目标。",
            "现金实验每天仍注入同样120 USDT；净值含全部闲置现金且现金不计利息。降低动用量后的单位成本不可单独当作择时优势，需检查相同动用预算对照及完整账户净值。",
            "TradingView Lightweight Charts为官方开源图表库，不是tradingview.com网站；图上为本地模拟买入记录，没有真实下单或上传账户。",
        ]
    for key, bars in datasets.items():
        print(f"Adaptive evaluation: {key}", flush=True)
        computed = features(bars)
        rules = Rules.from_symbol(info["symbols"][key])
        days = [d for d in day_groups(bars, computed, "rsi", config)
                if computed[d[1][0] - 1].volatility_multiple is not None]
        a = {"identity": ASSETS[key], "data": manifest["assets"][key], "edges": [],
             "candles": [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in bars]}
        if family == "reversal":
            a["indicators"] = [[b.time, f.ema72, f.ema168, f.rsi_value] for b, f in zip(bars, computed) if f is not None]
        if len(days) < 10:
            for previous, policy in zip(policies, policies[1:]):
                a["edges"].append({"previous": previous, "current": policy, "previous_label": labels[previous], "label": labels[policy],
                                   "eligible_days": len(days), "periods": {}, "scenarios": {}, "status": "insufficient", "label_status": "数据不足，不启用",
                                   "reason": "不足10个完成800小时预热及历史相对波动计算的完整日，不缩短预热。"})
            report["assets"][key] = a
            continue
        cut1, cut2 = int(len(days) * .6), int(len(days) * .8)
        splits = {"development": days[:cut1], "validation": days[cut1:cut2], "holdout": days[cut2:]}
        cache = {}
        def run(selected, policy, cfg=config, orders=True, anchor=0, budgets=None):
            stage = "ma_sizing" if policy in ("ma_sizing", "exposure") else "rsi" if policy == "sizing" else "equal"
            return simulate(bars, computed, selected, stage, cfg, rules,
                            include_orders=orders, schedule=policy, once_hour=anchor, deployment_budgets=budgets)
        for name, selected in splits.items():
            cache[name] = {policy: run(selected, policy) for policy in dict.fromkeys((*policies, "fixed_once", "fixed_four"))}
        for previous_policy, policy in zip(policies, policies[1:]):
            periods = {}
            for name in splits:
                results = cache[name]
                prev, cur, base, once = (results[k] for k in (previous_policy, policy, "fixed_four", "fixed_once"))
                matched = run(splits[name], "fixed_four", budgets={d["date"]: d["planned_deployment"] for d in cur["daily"]}) if policy == "exposure" else base
                periods[name] = {"previous": compact(prev), "current": compact(cur), "baseline": compact(base), "baseline_once": compact(once),
                                 "comparison": comparison(prev, cur), "vs_equal": comparison(base, cur), "vs_once": comparison(once, cur),
                                 "visual_orders": {"current": visual_orders(cur), "baseline": visual_orders(base), "baseline_once": visual_orders(once)},
                                 "count_distribution": dict(sorted(Counter(d["planned_count"] for d in cur["daily"]).items())),
                                 "stability": {"four": walkforward_summary(base, cur), "once": walkforward_summary(once, cur)}}
                if family == "reversal":
                    periods[name].update({"matched_baseline": compact(matched), "vs_matched": comparison(matched, cur),
                                          "trigger_distribution": dict(Counter(o["trigger"] for o in cur["orders"])),
                                          "day_deployment_mean": sum(d["planned_deployment"] for d in cur["daily"]) / len(cur["daily"])})
                    periods[name]["visual_orders"]["matched"] = visual_orders(matched)
            scenarios, stress = {}, {}
            for name, (fee, slip) in SCENARIOS.items():
                cfg = replace(config, fee_bps=fee, slippage_bps=slip)
                cur, prev, base, once = (run(splits["holdout"], k, cfg, False) for k in (policy, previous_policy, "fixed_four", "fixed_once"))
                scenarios[name] = comparison(prev, cur)
                stress[name] = {"four": comparison(base, cur), "once": comparison(once, cur)}
            anchor_checks = {str(hour): comparison(run(splits["holdout"], "fixed_once", orders=False, anchor=hour), cache["holdout"][policy]) for hour in (0, 6, 12, 18)}
            decision = meaningful_gate(periods, stress)
            if policy == "exposure":
                for checks in decision["checks"].values():
                    checks.pop("execution_not_worse")
                    checks["deployment_above_25pct_less_rounding"] = periods["holdout"]["current"]["execution_rate"] >= .24
                    checks["matched_budget_cost_and_equity_positive"] = periods["holdout"]["vs_matched"]["aggregate_cost_gain_bps"] > 0 and periods["holdout"]["vs_matched"]["profit_delta"] > 0
                enough = all(c["enough_days"] for c in decision["checks"].values())
                passed = all(all(c.values()) for c in decision["checks"].values())
                decision.update({"status": "needs_fresh" if passed else "not_adopted" if enough else "insufficient", "label": "仅历史达标，待新数据验证" if passed else "未达显著优势目标" if enough else "样本不足，不启用",
                                 "reason": decision["reason"] + "现金模块允许主动保留现金，但须对相同动用预算对照有正成本及净值增益；仍不构成独立验证。"})
            edge = {"previous": previous_policy, "current": policy, "previous_label": labels[previous_policy], "label": labels[policy],
                    "eligible_days": len(days), "periods": periods, "scenarios": scenarios, "benchmark_scenarios": stress,
                    "once_anchor_sensitivity": anchor_checks, "decision": decision, "status": decision["status"], "label_status": decision["label"]}
            a["edges"].append(edge)
            (output / f"{ASSETS[key]['symbol']}-{policy}-holdout-orders.json").write_text(json.dumps(cache["holdout"][policy]["orders"], ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        report["assets"][key] = a
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    return report
