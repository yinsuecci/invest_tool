"""Incremental ablations with paired windows and explicit evidence gates."""

import json
import random
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from .config import ASSETS, LABELS, STAGES, Config
from .engine import Rules, day_groups, simulate
from .indicators import features
from .market import load_dataset

SCENARIOS = {"low": (5, 2), "standard": (10, 5), "stress": (20, 15)}


def paired_gains(previous, current):
    if [d["date"] for d in previous["daily"]] != [d["date"] for d in current["daily"]]:
        raise ValueError("Comparison periods must match exactly")
    return [(a["unit_cost"] - b["unit_cost"]) / a["unit_cost"] * 10000 for a, b in zip(previous["daily"], current["daily"]) if a["unit_cost"] and b["unit_cost"]]


def confidence_interval(values, seed=20261030, trials=1000):
    if len(values) < 7:
        return None
    rng = random.Random(seed)
    samples = []
    n = len(values)
    full_blocks, remainder = divmod(n, 7)
    doubled = values + values
    block_sums = [sum(doubled[i:i + 7]) for i in range(n)]
    tail_sums = [sum(doubled[i:i + remainder]) for i in range(n)]
    for _ in range(trials):
        total = sum(block_sums[rng.randrange(n)] for _ in range(full_blocks))
        if remainder:
            total += tail_sums[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    return [samples[int(trials * 0.025)], samples[min(trials - 1, int(trials * 0.975))]]


def comparison(previous, current):
    gains = paired_gains(previous, current)
    return {"paired_days": len(gains), "mean_daily_cost_gain_bps": mean(gains) if gains else None, "median_daily_cost_gain_bps": median(gains) if gains else None,
            "ci95_bps": confidence_interval(gains),
            "aggregate_cost_gain_bps": (previous["unit_cost"] - current["unit_cost"]) / previous["unit_cost"] * 10000 if previous["unit_cost"] and current["unit_cost"] else None,
            "quantity_gain_pct": (current["quantity"] / previous["quantity"] - 1) * 100 if previous["quantity"] else None,
            "profit_delta": current["profit"] - previous["profit"],
            "drawdown_delta_pp": (current["max_drawdown"] - previous["max_drawdown"]) * 100,
            "execution_delta_pp": (current["execution_rate"] - previous["execution_rate"]) * 100}


def compact(result):
    return {k: v for k, v in result.items() if k not in ("daily", "orders")}


def visual_orders(result):
    """Compact execution records; timestamps remain actual next-bar fills."""
    return [[o["time"], o["target"], o["notional"], o["fee"], o["quantity"],
             o["price"], o["signal"], o["skip_reason"], o.get("day_count"), o.get("day_volatility"),
             o.get("trigger"), o.get("timing_signal"), o.get("rsi_value"), o.get("ma_deviation"),
             o.get("day_deployment"), o.get("target_exposure"), o.get("day_contribution")] for o in result["orders"]]


def evidence_gate(periods, scenario_checks):
    validation = periods["validation"]["comparison"]
    holdout = periods["holdout"]["comparison"]
    if validation["paired_days"] < 28 or holdout["paired_days"] < 56:
        return {"status": "insufficient", "label": "样本不足，不启用", "reason": "至少需要 28 个验证配对日与 56 个留出配对日；不缩短预热凑结果。"}
    checks = {
        "validation_positive": validation["mean_daily_cost_gain_bps"] > 0,
        "holdout_gain_above_2bps": holdout["mean_daily_cost_gain_bps"] > 2,
        "holdout_ci_above_zero": bool(holdout["ci95_bps"] and holdout["ci95_bps"][0] > 0),
        "execution_not_lower_by_0_1pp": holdout["execution_delta_pp"] >= -0.1,
        "drawdown_not_worse_by_0_2pp": holdout["drawdown_delta_pp"] >= -0.2,
        "all_cost_scenarios_positive": all(c["mean_daily_cost_gain_bps"] is not None and c["mean_daily_cost_gain_bps"] > 0 for c in scenario_checks.values()),
    }
    if all(checks.values()):
        return {"status": "paper_candidate", "label": "仅进入模拟观察", "checks": checks, "reason": "通过探索性门槛，不代表统计确认；多次比较及公开查看留出集后，仍需新数据前向验证。"}
    return {"status": "not_adopted", "label": "未通过，不启用", "checks": checks, "reason": "改善幅度、稳定性或风险约束未全部通过。"}


def new_report(manifest, config):
    report = {
        "version": "0.2.0", "generated_at": datetime.now(timezone.utc).isoformat(), "mode": "research_only",
        "visual_order_fields": ["time", "target", "notional", "fee", "quantity", "price", "signal", "skip_reason", "day_count", "day_volatility", "trigger", "timing_signal", "rsi_value", "ma_deviation", "day_deployment", "target_exposure", "day_contribution"],
        "candle_fields": ["time_ms", "open", "high", "low", "close", "volume"],
        "config": config.to_dict(), "manifest": manifest, "assets": {},
        "method": {"split": "Chronological 60% development / 20% validation / 20% holdout; fixed parameters, no optimizer.", "execution": "Previous closed hour features, next hour open plus slippage. Quote-denominated fee. Current lot/notional rules approximated historically.", "inference": "Exploratory circular 7-day moving-block bootstrap, 1000 trials, fixed seed. No multiple-testing correction; no automatic live promotion.", "timezone": "Asia/Shanghai (UTC+08:00)", "default_strategy": "equal", "live_trading": False},
        "limitations": [
            "未接入账户、密钥或实盘下单；公共目录可交易不代表特定账户或地区有权限。",
            "GOOG 标签映射 GOOGLB 代币化股票，并非 GOOG 股票本身；XAU 标签映射 XAUT，不是金条或 XAU 合约。",
            "仅使用真实币安现货历史，无股票/合约代理或合成行情填补。上市前及缺失时段不补齐。",
            "固定预算模式仅重新分配日内额度，不控制已有持仓风险，也不保证亏损减少。",
            "小时开盘成交是简化假设，无历史盘口、真实排队或成交冲击；零成交量小时视为未成交结果。",
            "费用为情景假设，不是账户真实费率；未模拟 BNB 抵扣、返佣或最低佣金。",
            "历史采用当前数量/金额过滤器近似，无法证明每笔满足历史所有过滤器。",
            "单笔金额边界约束计划额度；实际成交按数量步长向下取整，尾差留在现金，未成交窗口预算不集中补买。",
            "代币分红/拆股/赎回和其他公司行动未建模；GOOGLB 结果仅是价格与买入执行研究，不是完整总回报。",
            "不跨标的择时分配；每标的 120 USDT/日只是独立实验规模，不是用户真实预算或资产配置建议。",
            "累计阶段为固定消融链，不代表每个中间模块被接受；默认始终为等额基准。不同增量的预热区间可能不同，不能横向直接排名。",
            "留出集被展示后不再视为未来独立证据；统计区间仅探索性使用，未作多重检验修正。",
        ],
    }
    return report


def evaluate(directory, output, config=None):
    config = config or Config()
    manifest, info, datasets = load_dataset(directory)
    report = new_report(manifest, config)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for key, bars in datasets.items():
        print(f"Evaluating {key} ({ASSETS[key]['symbol']})", flush=True)
        computed = features(bars)
        rules = Rules.from_symbol(info["symbols"][key])
        asset_report = {"identity": ASSETS[key], "data": manifest["assets"][key], "edges": [],
                        "candles": [[b.time, b.open, b.high, b.low, b.close, b.volume] for b in bars]}
        for previous_stage, current_stage in zip(STAGES, STAGES[1:]):
            days = day_groups(bars, computed, current_stage, config)
            edge = {"previous": previous_stage, "current": current_stage, "previous_label": LABELS[previous_stage], "label": LABELS[current_stage], "eligible_days": len(days)}
            if len(days) < 10:
                edge.update({"status": "insufficient", "label_status": "数据不足，不启用", "reason": "未达到完整指标预热及最少评估窗口要求。", "periods": {}, "scenarios": {}})
                asset_report["edges"].append(edge)
                continue
            cut1, cut2 = int(len(days) * 0.6), int(len(days) * 0.8)
            splits = {"development": days[:cut1], "validation": days[cut1:cut2], "holdout": days[cut2:]}
            periods = {}
            for name, selected in splits.items():
                prev = simulate(bars, computed, selected, previous_stage, config, rules)
                cur = simulate(bars, computed, selected, current_stage, config, rules, include_orders=True)
                baseline = simulate(bars, computed, selected, "equal", config, rules, include_orders=True)
                periods[name] = {"previous": compact(prev), "current": compact(cur), "baseline": compact(baseline), "comparison": comparison(prev, cur), "vs_equal": comparison(baseline, cur),
                                 "visual_orders": {"current": visual_orders(cur), "baseline": visual_orders(baseline)}}
                if name == "holdout":
                    log = output / f"{ASSETS[key]['symbol']}-{current_stage}-holdout-orders.json"
                    log.write_text(json.dumps(cur["orders"], ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            scenarios = {"configured": periods["holdout"]["comparison"]}
            for name, (fee, slip) in SCENARIOS.items():
                scenario_config = replace(config, fee_bps=fee, slippage_bps=slip)
                p = simulate(bars, computed, splits["holdout"], previous_stage, scenario_config, rules)
                c = simulate(bars, computed, splits["holdout"], current_stage, scenario_config, rules)
                scenarios[name] = comparison(p, c)
            decision = evidence_gate(periods, scenarios)
            edge.update({"periods": periods, "scenarios": scenarios, "decision": decision, "status": decision["status"], "label_status": decision["label"]})
            asset_report["edges"].append(edge)
        report["assets"][key] = asset_report
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    return report
