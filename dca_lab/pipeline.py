from pathlib import Path

from .chronological import run as fit_assets
from .compact_app import build


def generate(data, output, daily_total=30.):
    data = Path(data)
    output = Path(output)
    if not data.is_dir():
        raise ValueError("Download market data before generating reports")
    output.mkdir(parents=True, exist_ok=False)
    independent = output / "independent"
    app = output / "app"
    print("[1/2] Evaluate all seven assets", flush=True)
    fit_assets(data, independent, daily_total=daily_total)
    report = independent / "report.json"
    if not report.is_file():
        raise RuntimeError("Asset stage did not produce report.json")
    print("[2/2] Build comparison page", flush=True)
    build(report, app)
    if not (app / "index.html").is_file():
        raise RuntimeError("Page stage did not produce index.html")
    return {"report": report, "app": app}
