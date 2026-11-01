"""Per-asset moving-average deviation tiers, fitted independently on development.

The strategy buys once per local day.  The amount is the asset's daily budget
times a multiplier that grows with how far the last closed price sits below its
moving average: a small dip buys a little more, a deep dip buys a lot more.
Every asset gets its own moving-average window and threshold set, chosen on the
development segment only; validation and holdout are evaluated after the fit is
frozen.

No fitting uses future bars: the signal is the last closed hour before the buy
hour, and the fill is the next hour's open plus the assumed slippage.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import mean

from .adaptive import freeze
from .config import Config
from .engine import Rules
from .evaluate import SCENARIOS
from .market import HOUR, iso, load_dataset
from .portfolio import compare_portfolios
from .signal_search import lock_selection

ZONE = timezone(timedelta(hours=8))
WARMUP = 800
MODES = ("always", "dips")
LADDERS = {
    "always": (1.0, 1.5, 2.0, 3.0, 5.0),
    "dips": (0.0, 1.0, 2.0, 3.0, 5.0),
}
WINDOWS = (72, 168, 336, 720)
THRESHOLD_SETS = (
    (0.005, 0.02, 0.05),
    (0.005, 0.03, 0.08),
    (0.01, 0.02, 0.05),
    (0.01, 0.03, 0.06),
    (0.01, 0.03, 0.08),
    (0.01, 0.05, 0.10),
    (0.02, 0.04, 0.08),
    (0.02, 0.05, 0.10),
    (0.02, 0.06, 0.12),
    (0.03, 0.06, 0.12),
)
CATALOGUE = tuple((mode, window, thresholds) for mode in MODES for window in WINDOWS for thresholds in THRESHOLD_SETS)


@dataclass(frozen=True)
class Plan:
    mode: str
    window: int
    thresholds: tuple


def complete_days(bars):
    """Only days with 24 contiguous, aligned hours are eligible."""
    groups = {}
    for index, bar in enumerate(bars):
        date = datetime.fromtimestamp(bar.time / 1000, ZONE).date().isoformat()
        groups.setdefault(date, []).append(index)
    days = []
    for date in sorted(groups):
        indices = groups[date]
        if len(indices) != 24:
            continue
        if any(bars[b].time != bars[a].time + HOUR for a, b in zip(indices, indices[1:])):
            continue
        days.append((date, tuple(bars[i].time for i in indices)))
    return days


def common_days(market):
    """Days present for every asset, with WARMUP closed bars before the open."""
    indexes = {key: {bar.time: index for index, bar in enumerate(bars)} for key, bars in market.items()}
    calendars = {key: dict(complete_days(bars)) for key, bars in market.items()}
    first = next(iter(market))
    common = []
    for date, times in calendars[first].items():
        if any(calendars[key].get(date) != times for key in market):
            continue
        if any(indexes[key][times[0]] < WARMUP for key in market):
            continue
        common.append((date, times))
    return common


def split_days(days):
    count = len(days)
    development = int(count * 0.6)
    validation = int(count * 0.2)
    return {
        "development": days[:development],
        "validation": days[development:development + validation],
        "holdout": days[development + validation:],
        "full": days,
    }


def multiplier(mode, deviation, thresholds):
    ladder = LADDERS[mode]
    if deviation <= 0:
        return ladder[0]
    if deviation <= thresholds[0]:
        return ladder[1]
    if deviation <= thresholds[1]:
        return ladder[2]
    if deviation <= thresholds[2]:
        return ladder[3]
    return ladder[4]


def tier(deviation, thresholds):
    if deviation <= 0:
        return 0
    if deviation <= thresholds[0]:
        return 1
    if deviation <= thresholds[1]:
        return 2
    if deviation <= thresholds[2]:
        return 3
    return 4


def _signal(bars, index, window):
    closes = [bars[i].close for i in range(index - window + 1, index + 1)]
    ma = mean(closes)
    return ma, (ma - bars[index].close) / ma


def simulate(market, days, budgets, plans, rules, mode, fee_bps=10, slip_bps=5, matched_totals=None):
    """Simulate independent per-asset sleeves; mode is tier, ordinary or matched."""
    fee, slip = fee_bps / 10000, slip_bps / 10000
    sleeves = {}
    for key, bars in market.items():
        rule = rules[key]
        index = {bar.time: i for i, bar in enumerate(bars)}
        cash = units = spent = fees = 0.0
        contributed = 0.0
        nav, peak, drawdown = 1.0, 1.0, 0.0
        previous = None
        orders, daily = [], []
        plan = plans.get(key)
        budget = budgets[key]
        spend_target = matched_totals[key] / len(days) if mode == "matched" else None
        pending = 0.0
        for date, times in days:
            start = index[times[0]]
            last = index[times[-1]]
            price = bars[start].open * (1 + slip)
            cash += budget
            contributed += budget
            multiplier_used, deviation, ma, skip = 1.0, None, None, None
            if mode == "tier":
                if start < plan.window:
                    skip = "warmup"
                else:
                    ma, deviation = _signal(bars, start - 1, plan.window)
                    multiplier_used = multiplier(plan.mode, deviation, plan.thresholds)
                gross = budget * multiplier_used
                floor_gross = (rule.min_notional + float(rule.step) * price) * (1 + fee)
                if 0 < gross < floor_gross and cash >= floor_gross:
                    gross = floor_gross
            else:
                pending += budget if mode == "ordinary" else spend_target
                gross = min(cash, pending)
            available = min(gross, cash)
            quantity, notional, charge = 0.0, 0.0, 0.0
            if skip is None and gross <= 0:
                skip = "above_average"
            if skip is None:
                if cash < rule.min_notional * (1 + fee):
                    skip = "cash_below_minimum"
                else:
                    quantity = rule.quantity(available / (1 + fee), price)
                    notional = quantity * price
                    charge = notional * fee
                    if quantity < rule.min_qty or notional < rule.min_notional - 1e-8:
                        skip = "exchange_minimum"
                        quantity, notional, charge = 0.0, 0.0, 0.0
            if skip is None:
                units += quantity
                cash -= notional + charge
                spent += notional
                fees += charge
                if mode != "tier":
                    pending -= notional + charge
            orders.append({
                "day": date, "time": iso(times[0]), "asset": key,
                "deviation": deviation, "multiplier": multiplier_used, "tier": tier(deviation, plan.thresholds) if deviation is not None else None,
                "ma": ma, "target": gross, "notional": notional, "fee": charge,
                "quantity": quantity, "price": price, "skip_reason": skip,
                "cash_after": cash, "window": plan.window if plan else None,
            })
            end_equity = cash + units * bars[last].close
            base = (previous if previous is not None else 0.0) + budget
            if base > 0:
                nav *= end_equity / base
                peak = max(peak, nav)
                drawdown = min(drawdown, nav / peak - 1)
            previous = end_equity
            daily.append({"date": date, "contribution": budget, "equity": end_equity,
                          "spent": notional + charge, "fills": int(skip is None),
                          "return": (end_equity / base - 1) if base > 0 else 0.0,
                          "cash": cash, "units": units})
        last_close = bars[index[days[-1][1][-1]]].close
        equity = cash + units * last_close
        sleeves[key] = {
            "start": days[0][0], "end": days[-1][0], "days": len(days),
            "contributed": contributed, "spent": spent, "fees": fees, "cash": cash,
            "equity": equity, "profit": equity - contributed,
            "twr_return": nav - 1, "max_drawdown": drawdown,
            "execution_rate": (spent + fees) / contributed if contributed else 0.0,
            "skipped_slots": sum(1 for order in orders if order["skip_reason"]),
            "assets": {key: {"quantity": units, "spent": spent, "fees": fees,
                             "unit_cost": (spent + fees) / units if units else None,
                             "market_value": units * last_close}},
            "orders": orders, "daily": daily,
        }
    return aggregate(sleeves, days, budgets)


def aggregate(sleeves, days, budgets):
    keys = list(sleeves)
    contributed = sum(item["contributed"] for item in sleeves.values())
    spent = sum(item["spent"] for item in sleeves.values())
    fees = sum(item["fees"] for item in sleeves.values())
    cash = sum(item["cash"] for item in sleeves.values())
    equity = sum(item["equity"] for item in sleeves.values())
    by_day = {date: 0.0 for date, _ in days}
    contribution = {date: 0.0 for date, _ in days}
    for item in sleeves.values():
        for row in item["daily"]:
            by_day[row["date"]] += row["equity"]
            contribution[row["date"]] += row["contribution"]
    nav, peak, drawdown, previous = 1.0, 1.0, 0.0, None
    daily, curve = [], []
    for date, _ in days:
        base = (previous if previous is not None else 0.0) + contribution[date]
        end = by_day[date]
        nav *= end / base if base > 0 else 1.0
        peak = max(peak, nav)
        drawdown = min(drawdown, nav / peak - 1)
        previous = end
        daily.append({"date": date, "contribution": contribution[date], "equity": end,
                      "return": (end / base - 1) if base > 0 else 0.0,
                      "fills": sum(1 for key in keys for row in sleeves[key]["daily"] if row["date"] == date and row["fills"])})
        curve.append({"date": date, "twr_index": nav * 100, "equity": end, "cash": sum(sleeves[key]["cash"] for key in keys)})
    return {
        "start": days[0][0], "end": days[-1][0], "days": len(days),
        "contributed": contributed, "spent": spent, "fees": fees, "cash": cash,
        "equity": equity, "profit": equity - contributed, "twr_return": nav - 1,
        "max_drawdown": drawdown, "execution_rate": (spent + fees) / contributed if contributed else 0.0,
        "skipped_slots": sum(item["skipped_slots"] for item in sleeves.values()),
        "assets": {key: item["assets"][key] for key, item in sleeves.items()},
        "orders": sorted((order for item in sleeves.values() for order in item["orders"]),
                         key=lambda order: (order["time"], order["asset"])),
        "daily": daily, "curve": curve, "sleeves": sleeves,
    }


def fit_development(market, days, budgets, rules, fee_bps, slip_bps):
    """Choose one (window, thresholds) per asset using development days only."""
    plans, trials = {}, {}
    for key in market:
        ordinary = simulate({key: market[key]}, days, {key: budgets[key]}, {}, {key: rules[key]},
                            "ordinary", fee_bps, slip_bps)["sleeves"][key]
        rows = []
        for position, (mode, window, thresholds) in enumerate(CATALOGUE):
            plan = Plan(mode, window, thresholds)
            result = simulate({key: market[key]}, days, {key: budgets[key]}, {key: plan},
                              {key: rules[key]}, "tier", fee_bps, slip_bps)["sleeves"][key]
            comparison = compare_portfolios(ordinary, result)
            score = comparison["equity_gain_pct_of_contributions"] or 0.0
            if result["execution_rate"] < 0.85:
                score -= 1000 * (0.85 - result["execution_rate"])
            rows.append({"mode": mode, "window": window, "thresholds": list(thresholds),
                         "position": position, "score": score,
                         "equity_delta": comparison["equity_delta"],
                         "cost_gain_pct": comparison["asset_cost_gain_pct"][key],
                         "execution_rate": result["execution_rate"],
                         "fills": sum(row["fills"] for row in result["daily"])})
        rows.sort(key=lambda row: (-row["score"], row["position"]))
        best = rows[0]
        plans[key] = Plan(best["mode"], best["window"], tuple(best["thresholds"]))
        trials[key] = {"catalogue_size": len(CATALOGUE), "best": best, "top": rows[:5]}
    return plans, trials


def evaluate(directory, output, center=42.0, fee_bps=10.0, slip_bps=5.0):
    keys = ("GOOG", "SPY", "QQQ", "BTC", "ETH", "SOL", "XAU")
    manifest, info, datasets = load_dataset(directory, keys)
    market = {key: datasets[key] for key in keys}
    rules = {key: Rules.from_symbol(info["symbols"][key]) for key in keys}
    days = common_days(market)
    if len(days) < 40:
        raise ValueError("Not enough common, complete days after warmup")
    periods = split_days(days)
    budget = center / len(keys)
    budgets = {key: budget for key in keys}
    config = Config(daily_total=center, center=budget, min_count=1, max_count=24,
                    fee_bps=fee_bps, slippage_bps=slip_bps)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    protocol = freeze(output, {
        "version": "ma-deviation-tiers-1.0",
        "data_sha256": {key: manifest["assets"][key]["sha256"] for key in keys},
        "center": center, "per_asset_budget": budget, "fee_bps": fee_bps, "slip_bps": slip_bps,
        "clock": "One decision per UTC+8 day at 00:00, fill at that hour's open plus slippage.",
        "signal": "deviation=(MA_n - last_closed_close)/MA_n, positive below the average; only bars closed before 00:00 are used.",
        "tiers": "always mode buys 1.0x at or above the average and 1.5/2.0/3.0/5.0x as the deviation passes t1/t2/t3; dips mode buys nothing at or above the average and 1.0/2.0/3.0/5.0x below it. Amount is capped by the sleeve's available cash.",
        "cash": "Each asset keeps its own cash; the daily budget accrues when it cannot meet the 5 USDT minimum, and a positive target below that minimum is raised to the minimum only when the sleeve can afford it.",
        "fit": "Per-asset grid of 2 modes x 4 windows x 10 threshold sets on development only; score is equity gain vs ordinary DCA minus a deployment penalty; validation/holdout never used for selection.",
        "controls": "ordinary DCA receives the same per-asset daily budget; matched control spreads each asset's actual gross spend evenly over the same days.",
        "evidence": "All 80 common days were already exposed in earlier rounds; this is retrospective research, not out-of-sample proof.",
    })
    plans, trials = fit_development(market, periods["development"], budgets, rules, fee_bps, slip_bps)
    plans_json = {key: {"mode": plan.mode, "window": plan.window, "thresholds": list(plan.thresholds)} for key, plan in plans.items()}
    results, stress = {}, {}
    for name, segment in periods.items():
        current = simulate(market, segment, budgets, plans, rules, "tier", fee_bps, slip_bps)
        ordinary = simulate(market, segment, budgets, {}, rules, "ordinary", fee_bps, slip_bps)
        totals = {key: current["assets"][key]["spent"] + current["assets"][key]["fees"] for key in keys}
        matched = simulate(market, segment, budgets, plans, rules, "matched", fee_bps, slip_bps, matched_totals=totals)
        results[name] = {
            "ma_tiers": {
                "current": current, "four": ordinary, "once": ordinary, "matched": matched,
                "matched_is_ex_post": True, "previous_policy": "four",
                "vs_four": compare_portfolios(ordinary, current),
                "vs_once": compare_portfolios(ordinary, current),
                "vs_matched": compare_portfolios(matched, current),
                "vs_previous": compare_portfolios(ordinary, current),
                "per_asset": {key: {
                    "equity": current["sleeves"][key]["equity"],
                    "ordinary_equity": ordinary["sleeves"][key]["equity"],
                    "equity_delta": current["sleeves"][key]["equity"] - ordinary["sleeves"][key]["equity"],
                    "cost_gain_pct": compare_portfolios(ordinary, current)["asset_cost_gain_pct"][key],
                    "execution_rate": current["sleeves"][key]["execution_rate"],
                    "fills": sum(row["fills"] for row in current["sleeves"][key]["daily"]),
                } for key in keys},
            }
        }
        if name == "holdout":
            stress = {}
            for scenario, (scenario_fee, scenario_slip) in SCENARIOS.items():
                current_s = simulate(market, segment, budgets, plans, rules, "tier", scenario_fee, scenario_slip)
                ordinary_s = simulate(market, segment, budgets, {}, rules, "ordinary", scenario_fee, scenario_slip)
                stress[scenario] = compare_portfolios(ordinary_s, current_s)
    results["full"]["ma_tiers"]["stress"] = stress
    report = {
        "version": "0.16.0", "scope": "portfolio", "family": "ma_tiers",
        "chart_engine": None, "manifest": manifest,
        "assets": {key: manifest["assets"][key] for key in keys},
        "config": {"daily_center": center, "per_asset": budget, "weights": {key: 1 / len(keys) for key in keys},
                   "fee_bps": fee_bps, "slip_bps": slip_bps, "timezone": "UTC+8", "warmup_hours": WARMUP},
        "protocol": protocol, "plans": plans_json, "trials": trials,
        "periods": {name: [segment[0][0], segment[-1][0], len(segment)] for name, segment in periods.items()},
        "selection": {"selected_policy": "ma_tiers", "period": "development",
                      "start": periods["development"][0][0], "end": periods["development"][-1][0],
                      "method": "per-asset grid, development only, deployment-penalized equity gain",
                      "trials": len(CATALOGUE) * len(keys)},
        "default_policy": "ma_tiers", "policies": {"ma_tiers": "均线偏离分档 · 逐标的独立拟合"},
        "baseline_label": "普通每日1次",
        "results": results,
        "decisions": {"ma_tiers": {"status": "历史研究，未获独立验证", "live": False, "checks": {}}},
        "limitations": [
            "七个标的等权，每标的每日预算=总中心/7；总中心42U是为了保持每标的6U，30U总预算会使单标的低于5U最低下单额，只能隔日买入。",
            "信号只用00:00前最后一根已收盘小时K线；成交按该小时开盘价加5bp滑点模拟，不使用当天最低价或未来收盘。",
            "每个标的只用前60%开发段在“每日买/只在跌破买 × 4个均线窗口 × 10组阈值”中选一个，共80个组合/标的；验证与末段不参与选择。全部历史已看过，不是独立前向证据。",
            "“只在跌破买”允许长期不买、现金累积，净值可能被现金拖累；两种模式都由逐标的开发段得分决定，不按全期表现改选。",
            "分档倍数固定，不做倍数搜索；大跌时最多动用该标的已累积现金，不借款、不跨标的重分配。",
            "普通定投对照收到完全相同的逐日预算；同支出对照按各标的实际含费支出事后均摊，属于诊断口径，不是可提前执行的资金计划。",
            "最低下单额5U、数量步长和零成交量会让部分日期无法成交，现金保留并计入净值；不卖出、不补投。",
        ],
    }
    report["selection"]["lock_sha256"] = lock_selection(output, report["selection"], protocol["sha256"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    (output / "index.html").write_text(render(report), encoding="utf-8")
    return report


def _mode_label(mode):
    return {"always": "每日买", "dips": "只在跌破买"}.get(mode, mode)


def _fmt(value, digits=2, signed=False):
    if value is None:
        return "—"
    text = f"{value:+,.{digits}f}" if signed else f"{value:,.{digits}f}"
    return text.replace("-", "−")


def _pct(value, digits=2):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _chart(curves, width=1080, height=260):
    series = [(label, [point["twr_index"] for point in curve]) for label, curve in curves]
    values = [value for _, data in series for value in data]
    low, high = min(values), max(values)
    span = (high - low) or 1
    colors = ("#175bd4", "#8a9bb5", "#b07a2a")
    lines, legends = [], []
    for position, (label, data) in enumerate(series):
        step = width / max(1, len(data) - 1)
        points = " ".join(f"{x * step:.1f},{height - (value - low) / span * (height - 30) - 10:.1f}"
                          for x, value in enumerate(data))
        lines.append(f'<polyline fill="none" stroke="{colors[position]}" stroke-width="2" points="{points}"/>')
        legends.append(f'<span class="legend"><i style="background:{colors[position]}"></i>{escape(label)}</span>')
    return (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="资金流调整净值曲线">'
            f'<rect x="0" y="0" width="{width}" height="{height}" fill="#fff"/>'
            f'<line x1="0" y1="{height - 10}" x2="{width}" y2="{height - 10}" stroke="#dce5f0"/>'
            + "".join(lines) + "</svg>" + "".join(legends))


def render(report):
    keys = list(report["assets"])
    full = report["results"]["full"]["ma_tiers"]
    current, ordinary, matched = full["current"], full["four"], full["matched"]
    versus, vs_matched = full["vs_four"], full["vs_matched"]
    stress = full.get("stress", {})
    plan_rows = "".join(
        f'<tr><td>{escape(key)}</td><td>{_mode_label(plan["mode"])}</td><td class="num">{plan["window"]}</td>'
        f'<td class="num">{plan["thresholds"][0] * 100:.2f}% / {plan["thresholds"][1] * 100:.2f}% / {plan["thresholds"][2] * 100:.2f}%</td>'
        f'<td class="num">{_fmt(report["trials"][key]["best"]["equity_delta"], signed=True)}</td></tr>'
        for key, plan in report["plans"].items()
    )
    asset_rows = "".join(
        f'<tr><td>{escape(key)}</td><td class="num">{_pct(full["per_asset"][key]["execution_rate"], 1)}</td>'
        f'<td class="num">{full["per_asset"][key]["fills"]}</td>'
        f'<td class="num">{_fmt(full["per_asset"][key]["equity"])}</td>'
        f'<td class="num">{_fmt(full["per_asset"][key]["equity_delta"], signed=True)}</td>'
        f'<td class="num">{_fmt(full["per_asset"][key]["cost_gain_pct"], 3)}</td></tr>'
        for key in keys
    )
    period_rows = "".join(
        f'<tr><td>{label}</td><td class="num">{item["current"]["days"]}</td>'
        f'<td class="num">{_fmt(item["current"]["equity"])}</td>'
        f'<td class="num">{_fmt(item["vs_four"]["equity_delta"], signed=True)}</td>'
        f'<td class="num">{_fmt(item["vs_matched"]["equity_delta"], signed=True)}</td>'
        f'<td class="num">{_pct(item["current"]["twr_return"])}</td>'
        f'<td class="num">{_pct(item["current"]["max_drawdown"])}</td></tr>'
        for label, item in (("开发 60%", report["results"]["development"]["ma_tiers"]),
                            ("验证 20%", report["results"]["validation"]["ma_tiers"]),
                            ("末段 20%", report["results"]["holdout"]["ma_tiers"]),
                            ("全期", full))
    )
    chart = _chart([("策略", current["curve"]), ("普通每日1次", ordinary["curve"]), ("同支出对照", matched["curve"])])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>均线偏离分档 | 七标的独立拟合</title>
<style>
:root{{--paper:#f5f8fc;--white:#fff;--ink:#172f50;--muted:#536b86;--accent:#175bd4;--line:#dce5f0;
--num:"Bahnschrift","DIN Alternate",Consolas,monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.7 "Microsoft YaHei UI","PingFang SC",sans-serif}}
main{{max-width:1180px;margin:auto;padding:28px 32px 48px}}
header{{border-top:3px solid var(--accent);padding:16px 0 10px}}
.eyebrow{{font-size:13px;font-weight:600;color:var(--accent)}}
h1{{font:600 28px/1.4 "Microsoft YaHei UI","PingFang SC",sans-serif;margin:6px 0}}
header p{{color:var(--muted);max-width:82ch}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);background:var(--white);border:1px solid var(--line);
border-radius:12px;margin:20px 0;overflow:hidden}}
.stat{{padding:14px 18px;border-right:1px solid var(--line)}}.stat:last-child{{border:0}}
.stat span{{display:block;font-size:12px;color:var(--muted)}}
.stat b{{display:block;font:500 22px/1.5 var(--num);white-space:nowrap}}
.panel{{background:var(--white);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:18px 0}}
h2{{font-size:17px;margin:0 0 10px}}h3{{font-size:14px;margin:16px 0 6px}}
table{{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}}
th{{text-align:left;font-size:12px;color:#3e5979;background:#f0f5fc;padding:9px 12px}}
td{{padding:9px 12px;border-top:1px solid var(--line)}}.num{{text-align:right;font-family:var(--num)}}
.pos{{color:#1c7a46}}.neg{{color:#a43838}}
.legend{{display:inline-flex;align-items:center;gap:6px;margin-right:16px;font-size:12px;color:var(--muted)}}
.legend i{{width:12px;height:3px;display:inline-block;border-radius:2px}}
.note{{font-size:12px;color:var(--muted);margin:8px 0 0}}
ul{{margin:6px 0 0 18px;padding:0}}li{{margin:4px 0}}
svg{{width:100%;height:auto;border:1px solid var(--line);border-radius:8px}}
</style></head><body><main>
<header><div class="eyebrow">定投实验室 · 均线偏离分档</div>
<h1>小跌小买，大跌大买 · 七标的独立拟合</h1>
<p>{escape(report["config"]["timezone"])} 每天 00:00 决策一次，按上一根已收盘小时相对均线的偏离程度决定倍数；
每个标的独立选择均线窗口与三档阈值，只用前 60% 开发段，验证与末段不参与选择。</p></header>
<section class="stats">
<div class="stat"><span>全期期末净值（{current["days"]} 日）</span><b>{_fmt(current["equity"])} U</b></div>
<div class="stat"><span>较普通每日1次</span><b class="{'pos' if versus["equity_delta"] > 0 else 'neg'}">{_fmt(versus["equity_delta"], signed=True)} U</b></div>
<div class="stat"><span>较同支出对照</span><b class="{'pos' if vs_matched["equity_delta"] > 0 else 'neg'}">{_fmt(vs_matched["equity_delta"], signed=True)} U</b></div>
<div class="stat"><span>最大回撤</span><b>{_pct(current["max_drawdown"])}</b></div>
</section>
<section class="panel"><h2>资金流调整净值（全期）</h2>{chart}
<p class="note">三条曲线都接收相同的逐日新增资金；同支出对照把策略各标的实际支出事后均摊，仅用于分解现金与投入差异。</p></section>
<section class="panel"><h2>逐标的参数（开发段选择）</h2>
<table><thead><tr><th>标的</th><th>买入模式</th><th class="num">均线窗口（小时）</th><th class="num">三档偏离阈值</th><th class="num">开发段净值差（U）</th></tr></thead>
<tbody>{plan_rows}</tbody></table>
<p class="note">“每日买”在价格高于均线时仍买 1 倍，跌破后 1.5 / 2 / 3 / 5 倍；“只在跌破买”在高于均线时不买，跌破后 1 / 2 / 3 / 5 倍，未买的预算留作现金。
每个标的有 2 种模式 × 4 个均线窗口 × 10 组阈值 = 80 个组合，共 {report["selection"]["trials"]} 次开发段试验。</p></section>
<section class="panel"><h2>分段结果</h2>
<table><thead><tr><th>区间</th><th class="num">天数</th><th class="num">期末净值</th><th class="num">较普通1次</th><th class="num">较同支出</th><th class="num">TWR</th><th class="num">最大回撤</th></tr></thead>
<tbody>{period_rows}</tbody></table></section>
<section class="panel"><h2>分标的（全期）</h2>
<table><thead><tr><th>标的</th><th class="num">动用率</th><th class="num">成交笔数</th><th class="num">期末市值</th><th class="num">较普通定投</th><th class="num">含费成本改善%</th></tr></thead>
<tbody>{asset_rows}</tbody></table></section>
<section class="panel"><h2>边界与说明</h2><ul>{''.join(f'<li>{escape(item)}</li>' for item in report["limitations"])}</ul>
<p class="note">本报告只读、离线，不含实盘下单能力；全部历史已在其他轮次看过，结论属于回顾性探索。</p></section>
</main></body></html>"""
