import argparse
import functools
import http.server
from pathlib import Path

from .config import ASSETS, ALL_ASSETS, Config
from .adaptive import evaluate_adaptive
from .portfolio import evaluate_portfolio
from .evaluate import evaluate
from .market import fetch_data
from .vwap_research import evaluate_vwap
from .report import render_report


def main():
    parser = argparse.ArgumentParser(description="Binance spot DCA research. No API keys, no live trading.")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Fit SOL, fit seven assets, and build the comparison page")
    generate.add_argument("--data", default="data/seven-assets")
    generate.add_argument("--output", default="reports/generated")
    fetch = sub.add_parser("fetch", help="Download verified public spot candles")
    fetch.add_argument("--data", default="data/binance-2026-10-30")
    fetch.add_argument("--start", default="2023-01-01T00:00:00+00:00")
    fetch.add_argument("--end", default="2026-10-29T16:00:00+00:00")
    fetch.add_argument("--assets", nargs="+", choices=ALL_ASSETS)
    run = sub.add_parser("evaluate", help="Run fixed incremental ablations and generate offline report")
    run.add_argument("--data", default="data/binance-2026-10-30")
    run.add_argument("--output", default="reports/latest")
    run.add_argument("--daily-total", type=float, default=120)
    run.add_argument("--center", type=float, default=20)
    run.add_argument("--deviation", type=float, default=0.30)
    run.add_argument("--min-count", type=int, default=4)
    run.add_argument("--max-count", type=int, default=8)
    run.add_argument("--fee-bps", type=float, default=10)
    run.add_argument("--slippage-bps", type=float, default=5)
    adaptive = sub.add_parser("optimize", help="Evaluate three frozen 1--4/day modules; no search-until-win")
    adaptive.add_argument("--data", default="data/binance-2026-10-30")
    adaptive.add_argument("--output", default="reports/adaptive-001")
    adaptive.add_argument("--daily-total", type=float, default=120)
    adaptive.add_argument("--deviation", type=float, default=.30)
    adaptive.add_argument("--fee-bps", type=float, default=10)
    adaptive.add_argument("--slippage-bps", type=float, default=5)
    adaptive.add_argument("--family", choices=("adaptive", "reversal"), default="adaptive")
    portfolio = sub.add_parser("portfolio", help="Combined three-asset variable daily funding research")
    portfolio.add_argument("--data", default="data/binance-2026-10-30")
    portfolio.add_argument("--output", default="reports/portfolio-001")
    portfolio.add_argument("--center", type=float, default=30)
    portfolio.add_argument("--funding-deviation", type=float, default=.30)
    portfolio.add_argument("--assets", nargs="+", choices=ALL_ASSETS)
    portfolio.add_argument("--weights", type=float, nargs="+", metavar="WEIGHT")
    portfolio.add_argument("--family", choices=("original", "opportunity", "discount", "signal", "signal_v2", "indicator"), default="original")
    vwap = sub.add_parser("vwap", help="Fit six VWAP tier profiles per asset on development only, then test confirmations")
    vwap.add_argument("--data", default="data/five-assets-2026-10-31")
    vwap.add_argument("--output", default="reports/vwap-001")
    vwap.add_argument("--expanded", action="store_true", help="Bounded development-only shared-ledger tier search")
    vwap.add_argument("--targeted-seed", help="Prior report.json; fit GOOG/XAU/SOL with causal filters, freeze BTC/ETH profiles")
    execution = sub.add_parser("vwap-execution", help="Frozen-plan ablation: full VWAP amounts, no daily budget clipping")
    execution.add_argument("--data", default="data/five-assets-2026-10-31")
    execution.add_argument("--source", default="reports/vwap-005/report.json")
    execution.add_argument("--output", default="reports/vwap-006")
    unlimited = sub.add_parser("vwap-unlimited", help="On-demand cash and per-asset ex-post equal-final-budget ordinary DCA")
    unlimited.add_argument("--data", default="data/five-assets-2026-10-31")
    unlimited.add_argument("--source", default="reports/vwap-005/report.json")
    unlimited.add_argument("--output", default="reports/vwap-007")
    smart = sub.add_parser("smart-dca", help="Prespecified inverse1/2 and last-fill2% ladder templates")
    smart.add_argument("--data", default="data/five-assets-2026-10-31")
    smart.add_argument("--source", default="reports/vwap-007/report.json")
    smart.add_argument("--output", default="reports/smart-002")
    select_smart = sub.add_parser("smart-select", help="Select frozen template on development only; keep all simulation results unchanged")
    select_smart.add_argument("--source", default="reports/smart-002/report.json")
    select_smart.add_argument("--output", default="reports/smart-003")
    index = sub.add_parser("index", help="Build a static index of every saved report")
    index.add_argument("--reports", default="reports")
    index.add_argument("--output", default="reports/index.html")
    index.add_argument("--no-cache", action="store_true", help="Ignore the report summary cache")
    ma = sub.add_parser("ma-tiers", help="Per-asset moving-average deviation tiers, fitted on development only")
    ma.add_argument("--data", default="data/seven-assets-2026-11-01")
    ma.add_argument("--output", default="reports/ma-tiers-001")
    ma.add_argument("--center", type=float, default=42.0, help="Combined daily budget; 42U keeps 6U per asset")
    ma.add_argument("--fee-bps", type=float, default=10)
    ma.add_argument("--slip-bps", type=float, default=5)
    serve = sub.add_parser("serve", help="Serve a report directory on loopback only (default: the index)")
    serve.add_argument("--output", default="reports")
    serve.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.command == "generate":
        from .pipeline import generate as generate_all
        try:
            paths = generate_all(args.data, args.output)
        except (ValueError, FileExistsError) as exc:
            parser.error(str(exc))
        print(f"Report: {paths['report']}")
        print(f"Page: {paths['app'] / 'index.html'}")
        print(f'Run: python -m dca_lab.compact_app --source "{paths["report"]}" --output "{paths["app"]}" --port 59755')
    elif args.command == "fetch":
        fetch_data(args.data, args.start, args.end, asset_keys=args.assets)
    elif args.command == "evaluate":
        config = Config(daily_total=args.daily_total, center=args.center, max_deviation=args.deviation, alpha=min(args.deviation, .30), min_count=args.min_count, max_count=args.max_count, fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
        report = evaluate(args.data, args.output, config)
        path = render_report(report, args.output)
        for key, asset in report["assets"].items():
            for edge in asset["edges"]:
                print(f"{key}: {edge['previous']} -> {edge['current']}: {edge['status']}")
        print(f"Report: {path.resolve()}")
    elif args.command == "optimize":
        principal = args.daily_total / (1 + args.fee_bps / 10000)
        config = Config(daily_total=args.daily_total, center=principal / 3, min_count=1, max_count=4,
                        max_deviation=args.deviation, alpha=min(args.deviation, .3), fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
        report = evaluate_adaptive(args.data, args.output, config, family=args.family)
        print(f"Report: {render_report(report, args.output).resolve()}")
        for key, asset in report["assets"].items():
            for edge in asset["edges"]:
                d = edge["periods"]["holdout"]
                print(f"{key} {edge['current']}: {edge['status']}; cost gain vs four {d['vs_equal']['aggregate_cost_gain_bps']:.3f}bp / once {d['vs_once']['aggregate_cost_gain_bps']:.3f}bp")
    elif args.command == "portfolio":
        if args.weights and len(args.weights) != len(args.assets or ASSETS):
            parser.error("Provide exactly one weight per selected asset")
        weights = dict(zip(args.assets or ASSETS, args.weights)) if args.weights else None
        report = evaluate_portfolio(args.data, args.output, args.center, args.funding_deviation, weights, args.family, args.assets)
        print(f"Report: {render_report(report, args.output).resolve()}")
    elif args.command == "vwap":
        report = evaluate_vwap(args.data, args.output, expanded=args.expanded, targeted_seed=args.targeted_seed)
        print(f"Report: {render_report(report, args.output).resolve()}")
    elif args.command == "vwap-execution":
        from .vwap_execution import evaluate_execution
        report = evaluate_execution(args.data,args.source,args.output)
        print(f"Report: {render_report(report,args.output).resolve()}")
    elif args.command == "vwap-unlimited":
        from .vwap_unlimited import evaluate_unlimited
        report = evaluate_unlimited(args.data,args.source,args.output)
        print(f"Report: {render_report(report,args.output).resolve()}")
    elif args.command == "smart-dca":
        from .smart_dca import evaluate_smart
        report = evaluate_smart(args.data,args.source,args.output)
        print(f"Report: {render_report(report,args.output).resolve()}")
    elif args.command == "smart-select":
        from .smart_dca import select_smart_report
        report = select_smart_report(args.source,args.output)
        print(f"Selected: {report['default_policy']}; report: {render_report(report,args.output).resolve()}")
    elif args.command == "ma-tiers":
        from .ma_tiers import evaluate as evaluate_ma
        report = evaluate_ma(args.data, args.output, args.center, args.fee_bps, args.slip_bps)
        print(f"Report: {(Path(args.output) / 'index.html').resolve()}")
    elif args.command == "index":
        from .report_index import build_index
        print(f"Index: {build_index(args.reports, args.output, use_cache=not args.no_cache).resolve()}")
    elif args.command == "serve":
        root = Path(args.output).resolve()
        if not (root / "index.html").is_file():
            parser.error("No report exists; evaluate real data first")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        class LocalServer(http.server.ThreadingHTTPServer):
            allow_reuse_address = False

        server = LocalServer(("127.0.0.1", args.port), handler)
        print(f"Read-only report: http://127.0.0.1:{server.server_address[1]}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
