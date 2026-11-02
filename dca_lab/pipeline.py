"""Generate the full research chain in a fresh output directory."""

from pathlib import Path

from .sol_research import run as fit_sol
from .asset_research import run as fit_assets
from .compact_app import build


def generate(data, output):
    data = Path(data)
    output = Path(output)
    if not data.is_dir():
        raise ValueError("Download market data before generating reports")
    # Never consume stale intermediate files from a failed or previous run.
    output.mkdir(parents=True, exist_ok=False)
    sol = output / "sol"
    independent = output / "independent"
    app = output / "app"
    print("[1/3] Fit SOL", flush=True)
    fit_sol(data, sol)
    sol_report = sol / "report.json"
    if not sol_report.is_file():
        raise RuntimeError("SOL stage did not produce report.json")
    print("[2/3] Fit all seven assets", flush=True)
    fit_assets(data, sol_report, independent)
    report = independent / "report.json"
    if not report.is_file():
        raise RuntimeError("Asset stage did not produce report.json")
    print("[3/3] Build comparison page", flush=True)
    build(report, app)
    if not (app / "index.html").is_file():
        raise RuntimeError("Page stage did not produce index.html")
    return {"report": report, "app": app}
