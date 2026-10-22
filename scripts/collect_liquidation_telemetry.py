#!/usr/bin/env python3
"""Collect key-free, forward-only liquidation snapshots and normalization data."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.liquidation_telemetry import (
    DEFAULT_SYMBOLS,
    liquidation_events,
    mark_prices,
    protocol_digests,
)


LIQUIDATION_WS = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
MARK_PRICE_WS = "wss://fstream.binance.com/market/ws/!markPrice@arr@1s"
OPEN_INTEREST_URL = "https://fapi.binance.com/fapi/v1/openInterest"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _open_interest(symbol: str) -> dict:
    url = f"{OPEN_INTEREST_URL}?{urlencode({'symbol': symbol})}"
    request = Request(url, headers={"User-Agent": "quant-liquidation-telemetry/1.0"})
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    if str(payload.get("symbol") or "").upper() != symbol:
        raise RuntimeError(f"open-interest symbol mismatch for {symbol}")
    value = float(payload["openInterest"])
    server_time_ms = int(payload["time"])
    if value < 0 or server_time_ms <= 0:
        raise RuntimeError(f"invalid open interest for {symbol}")
    return {
        "kind": "open_interest",
        "symbol": symbol,
        "event_time_ms": server_time_ms,
        "open_interest_base_qty": value,
    }


async def _websocket_producer(
    *, kind: str, url: str, output: asyncio.Queue, stop: asyncio.Event, counters: dict
) -> None:
    import websockets

    delay = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                open_timeout=15,
                close_timeout=5,
                max_queue=20_000,
                ping_interval=120,
                ping_timeout=600,
            ) as websocket:
                counters["connections"] += 1
                delay = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(websocket.recv(), timeout=15)
                    counters["messages"] += 1
                    await output.put((kind, _now().isoformat(), json.loads(raw)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters["reconnects"] += 1
            counters["last_error"] = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(30.0, delay * 2)


async def _open_interest_producer(
    *, symbols: tuple[str, ...], interval: int, output: asyncio.Queue, stop: asyncio.Event, counters: dict
) -> None:
    while not stop.is_set():
        started = time.monotonic()
        results = await asyncio.gather(
            *(asyncio.to_thread(_open_interest, symbol) for symbol in symbols),
            return_exceptions=True,
        )
        received_at = _now().isoformat()
        for symbol, result in zip(symbols, results):
            if isinstance(result, Exception):
                counters["failures"] += 1
                counters["last_error"] = f"{symbol}: {type(result).__name__}: {result}"
            else:
                counters["rows"] += 1
                await output.put(("open_interest", received_at, result))
        remaining = max(0.0, interval - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop.wait(), timeout=remaining)
        except TimeoutError:
            pass


async def _writer(
    *, path: Path, symbols: set[str], queue: asyncio.Queue, stop: asyncio.Event, counters: dict
) -> None:
    seen_liquidations: set[str] = set()
    since_flush = 0
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        while not stop.is_set() or not queue.empty():
            try:
                kind, received_at, payload = await asyncio.wait_for(queue.get(), timeout=1)
            except TimeoutError:
                continue
            if kind == "liquidation":
                rows = liquidation_events(payload, symbols=symbols)
                unique = []
                for row in rows:
                    if row["fingerprint"] in seen_liquidations:
                        counters["duplicate_liquidation_snapshots"] += 1
                        continue
                    seen_liquidations.add(row["fingerprint"])
                    unique.append(row)
                rows = unique
                counters["liquidation_rows"] += len(rows)
            elif kind == "mark":
                rows = mark_prices(payload, symbols=symbols)
                counters["mark_rows"] += len(rows)
            else:
                rows = [payload]
            for row in rows:
                row = {"received_at": received_at, **row}
                handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
                counters["written_rows"] += 1
                since_flush += 1
            if since_flush >= 100:
                handle.flush()
                since_flush = 0


def _file_record(path: Path) -> dict:
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path.read_bytes()).hexdigest()}


async def _collect(args, run_dir: Path) -> dict:
    symbols = tuple(args.symbols)
    symbol_set = set(symbols)
    queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
    stop = asyncio.Event()
    stream_counters = {
        "liquidation": {"connections": 0, "reconnects": 0, "messages": 0, "last_error": None},
        "mark": {"connections": 0, "reconnects": 0, "messages": 0, "last_error": None},
    }
    oi_counters = {"rows": 0, "failures": 0, "last_error": None}
    row_counters = {
        "written_rows": 0,
        "liquidation_rows": 0,
        "duplicate_liquidation_snapshots": 0,
        "mark_rows": 0,
    }
    output = run_dir / "telemetry.jsonl.gz"
    tasks = [
        asyncio.create_task(_websocket_producer(kind="liquidation", url=LIQUIDATION_WS, output=queue, stop=stop, counters=stream_counters["liquidation"])),
        asyncio.create_task(_websocket_producer(kind="mark", url=MARK_PRICE_WS, output=queue, stop=stop, counters=stream_counters["mark"])),
        asyncio.create_task(_open_interest_producer(symbols=symbols, interval=args.open_interest_poll_seconds, output=queue, stop=stop, counters=oi_counters)),
        asyncio.create_task(_writer(path=output, symbols=symbol_set, queue=queue, stop=stop, counters=row_counters)),
    ]
    started_wall = _now()
    started_monotonic = time.monotonic()
    try:
        await asyncio.sleep(args.duration_seconds)
    finally:
        ended_wall = _now()
        ended_monotonic = time.monotonic()
        stop.set()
        for task in tasks[:3]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    wall_seconds = (ended_wall - started_wall).total_seconds()
    monotonic_seconds = ended_monotonic - started_monotonic
    tolerance = max(5.0, args.duration_seconds * 0.05)
    timing_healthy = abs(wall_seconds - monotonic_seconds) <= tolerance
    minimum_marks = args.duration_seconds * len(symbols) * 0.70
    coverage_healthy = row_counters["mark_rows"] >= minimum_marks and oi_counters["rows"] >= len(symbols)
    return {
        "started_at": started_wall.isoformat(),
        "ended_at": ended_wall.isoformat(),
        "requested_duration_seconds": args.duration_seconds,
        "timing": {
            "active_wall_seconds": wall_seconds,
            "active_monotonic_seconds": monotonic_seconds,
            "tolerance_seconds": tolerance,
            "healthy": timing_healthy,
        },
        "coverage": {
            "minimum_mark_rows": minimum_marks,
            "healthy": coverage_healthy,
        },
        "streams": stream_counters,
        "open_interest": oi_counters,
        "rows": row_counters,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=3600)
    parser.add_argument("--open-interest-poll-seconds", type=int, default=300)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--output-root", type=Path, default=Path("research/liquidation_telemetry"))
    args = parser.parse_args()
    args.symbols = [str(symbol).upper() for symbol in args.symbols]
    if tuple(args.symbols) != DEFAULT_SYMBOLS:
        raise ValueError("symbols must exactly match the preregistered fixed universe and order")
    if not 30 <= args.duration_seconds <= 86_400:
        raise ValueError("duration-seconds must be between 30 and 86400")
    if not 60 <= args.open_interest_poll_seconds <= 900:
        raise ValueError("open-interest-poll-seconds must be between 60 and 900")
    run_id = _now().strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(_collect(args, run_dir))
    files = [_file_record(path) for path in sorted(run_dir.glob("*.jsonl.gz"))]
    manifest = {
        "schema_version": 1,
        "status": "PASS" if result["timing"]["healthy"] and result["coverage"]["healthy"] else "FAIL",
        "run_id": run_id,
        "purpose": "forward_risk_overlay_telemetry_not_promotion_evidence",
        "feed_semantics": "censored_latest_snapshot_not_total_liquidation_volume",
        "symbols": args.symbols,
        "endpoints": {
            "liquidation": LIQUIDATION_WS,
            "mark_price": MARK_PRICE_WS,
            "open_interest": OPEN_INTEREST_URL,
        },
        "open_interest_poll_seconds": args.open_interest_poll_seconds,
        "protocol_sha256": protocol_digests(PROJECT_ROOT),
        "result": result,
        "files": files,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), **manifest}, indent=2))
    if manifest["status"] != "PASS":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
