"""Validated hourly candles, content hashes, and public GET-only ingestion."""

import csv
import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import ASSETS, ALL_ASSETS

HOUR = 3_600_000
BASE = "https://data-api.binance.vision"


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def milliseconds(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must include a UTC offset")
    return int(dt.timestamp() * 1000)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate(bars):
    if not bars:
        raise ValueError("No bars: real data is required, synthetic fallback is forbidden")
    previous = None
    gaps = 0
    for b in bars:
        if b.time % HOUR or (previous is not None and b.time <= previous):
            raise ValueError("Bars must be unique, ascending and aligned to UTC hours")
        if not all(math.isfinite(x) for x in (b.open, b.high, b.low, b.close, b.volume)):
            raise ValueError("Non-finite market data")
        if min(b.open, b.high, b.low, b.close) <= 0 or b.volume < 0:
            raise ValueError("Invalid price or volume")
        if b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            raise ValueError("Invalid OHLC range")
        if previous is not None:
            gaps += (b.time - previous) // HOUR - 1
        previous = b.time
    return {"bars": len(bars), "missing_hours": gaps, "zero_volume_hours": sum(b.volume == 0 for b in bars), "first_open": iso(bars[0].time), "last_close": iso(bars[-1].time + HOUR)}


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as f:
        bars = [Bar(int(r["open_time"]), *(float(r[k]) for k in ("open", "high", "low", "close", "volume"))) for r in csv.DictReader(f)]
    validate(bars)
    return bars


def get_json(session, path, params=None):
    if path not in ("/api/v3/exchangeInfo", "/api/v3/klines", "/api/v3/time"):
        raise ValueError("Only allowlisted public market endpoints are permitted")
    for attempt in range(4):
        response = session.get(BASE + path, params=params, timeout=(10, 35))
        if response.status_code == 418:
            raise RuntimeError("Binance IP ban: stop, do not switch hosts")
        if response.status_code == 429 or response.status_code >= 500:
            delay = max(2 ** attempt, float(response.headers.get("Retry-After", 0)))
            if delay > 60:
                raise RuntimeError("Rate limit requires long backoff; stopped safely")
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError("Public data request retry limit reached")


def fetch_data(directory, start, end, progress=print, asset_keys=None):
    import requests
    assets = {k: ALL_ASSETS[k] for k in (asset_keys or ASSETS)}

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    first, cutoff = milliseconds(start), milliseconds(end)
    if first >= cutoff or first % HOUR or cutoff % HOUR:
        raise ValueError("Require an ascending hour-aligned range")
    session = requests.Session()
    server_time = get_json(session, "/api/v3/time")["serverTime"]
    if cutoff > server_time // HOUR * HOUR:
        raise ValueError("Requested range includes an unclosed or future candle")
    exchange = get_json(session, "/api/v3/exchangeInfo")
    selected = {}
    for key, asset in assets.items():
        rows = [s for s in exchange["symbols"] if s["symbol"] == asset["symbol"]]
        if len(rows) != 1 or rows[0]["status"] != "TRADING" or not rows[0].get("isSpotTradingAllowed"):
            raise ValueError(f"Verified spot instrument unavailable: {key} / {asset['symbol']}")
        selected[key] = rows[0]
    snapshot = {"serverTime": server_time, "source": BASE + "/api/v3/exchangeInfo", "symbols": selected}
    info_path = directory / "exchange_info.json"
    info_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {"downloaded_at": iso(server_time), "requested_start": start, "end_exclusive": end, "source": BASE, "interval": "1h", "timezone": "UTC", "exchange_info_sha256": digest(info_path), "assets": {}}
    for key, asset in assets.items():
        cursor, raw = first, []
        while cursor < cutoff:
            batch = get_json(session, "/api/v3/klines", {"symbol": asset["symbol"], "interval": "1h", "startTime": cursor, "endTime": cutoff - 1, "limit": 1000})
            if not isinstance(batch, list):
                raise ValueError(f"Unexpected API response: {batch}")
            if not batch:
                break
            closed = [r for r in batch if first <= int(r[0]) < cutoff and int(r[6]) < cutoff]
            raw.extend(closed)
            following = int(batch[-1][0]) + HOUR
            if following <= cursor:
                raise ValueError("Kline pagination did not advance")
            cursor = following
            progress(f"{asset['symbol']}: {len(raw)} hourly bars", flush=True)
            time.sleep(0.12)
        bars = [Bar(int(r[0]), *(float(r[i]) for i in (1, 2, 3, 4, 5))) for r in raw]
        quality = validate(bars)
        path = directory / f"{asset['symbol']}.csv"
        temp = path.with_suffix(".csv.tmp")
        with temp.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(("open_time", "open", "high", "low", "close", "volume"))
            for r in raw:
                writer.writerow([r[i] for i in (0, 1, 2, 3, 4, 5)])
        temp.replace(path)
        manifest["assets"][key] = {**asset, **quality, "file": path.name, "sha256": digest(path)}
    (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_dataset(directory, asset_keys=None):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    info_path = directory / "exchange_info.json"
    if digest(info_path) != manifest["exchange_info_sha256"]:
        raise ValueError("Exchange rule snapshot hash mismatch")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    result = {}
    for key in (asset_keys or ASSETS):
        asset = ALL_ASSETS[key]
        meta = manifest["assets"][key]
        if meta["symbol"] != asset["symbol"] or meta["file"] != asset["symbol"] + ".csv":
            raise ValueError("Asset identity mismatch; no automatic proxies allowed")
        path = directory / meta["file"]
        if digest(path) != meta["sha256"]:
            raise ValueError(f"Market data hash mismatch: {key}")
        bars = read_csv(path)
        if bars[-1].time + HOUR > milliseconds(manifest["end_exclusive"]):
            raise ValueError("Data extends beyond frozen cutoff")
        result[key] = bars
    return manifest, info, result
