"""Vendor a pinned TradingView release without running package install scripts."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import requests

VERSION = "5.2.1"
INTEGRITY = "IVwoK1RLFiLPubaKIjNbtjWLnpPMqiABSrTay6whmNa8L1+19292VtHJ+BWyPUuLCwF0tcQlhEWd1CLB2a1nsQ=="
URL = f"https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-{VERSION}.tgz"
FILES = {"package/dist/lightweight-charts.standalone.production.js": "lightweight-charts.js",
         "package/LICENSE": "LICENSE", "package/README.md": "README.md", "package/dist/typings.d.ts": "typings.d.ts"}


def main():
    response = requests.get(URL, timeout=60)
    response.raise_for_status()
    if base64.b64encode(hashlib.sha512(response.content).digest()).decode() != INTEGRITY:
        raise ValueError("NPM release integrity mismatch")
    output = Path(__file__).resolve().parents[1] / "dca_lab/vendor"
    output.mkdir(exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        for source, destination in FILES.items():
            data = archive.extractfile(source).read()
            (output / destination).write_bytes(data)
    notice = requests.get(f"https://raw.githubusercontent.com/tradingview/lightweight-charts/v{VERSION}/NOTICE", timeout=30)
    notice.raise_for_status()
    (output / "NOTICE").write_bytes(notice.content)
    (output / "source.json").write_text(json.dumps({"name": "TradingView Lightweight Charts", "version": VERSION,
        "url": URL, "integrity": "sha512-" + INTEGRITY,
        "js_sha256": hashlib.sha256((output / "lightweight-charts.js").read_bytes()).hexdigest()}, indent=2), encoding="utf-8")
    print(f"Vendored TradingView Lightweight Charts {VERSION}")


if __name__ == "__main__":
    main()
