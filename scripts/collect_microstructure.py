#!/usr/bin/env python3
"""Collect API-key-free Binance USD-M depth, trades, and book features.

The command never calls an order or account endpoint. Every run creates an
immutable directory containing gzip JSONL streams and a hash manifest.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.orderbook import LocalOrderBook, OrderBookSequenceError
from quant.research.protocol import protocol_digests


REST_URL = "https://fapi.binance.com"
DEPTH_WS = "wss://fstream.binance.com/public/ws/{stream}@depth@100ms"
TRADE_WS = "wss://fstream.binance.com/market/ws/{stream}@aggTrade"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_depth_snapshot(symbol: str, limit: int = 1000) -> dict:
    url = f"{REST_URL}/fapi/v1/depth?{urlencode({'symbol': symbol, 'limit': limit})}"
    request = Request(url, headers={"User-Agent": "quant-microstructure-collector/1.0"})
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict) or not payload.get("bids") or not payload.get("asks"):
        raise RuntimeError("Binance snapshot did not contain a valid order book")
    return payload


def write_jsonl(handle, record: dict) -> None:
    handle.write(json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n")
    handle.flush()


async def websocket_reader(url: str, queue: asyncio.Queue, counters: dict, stop: asyncio.Event) -> None:
    import websockets

    reconnect_delay = 1.0
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
                reconnect_delay = 1.0
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    except TimeoutError:
                        continue
                    await queue.put((utc_now(), json.loads(raw)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            counters["reconnects"] += 1
            counters["last_error"] = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(stop.wait(), timeout=reconnect_delay)
            except TimeoutError:
                reconnect_delay = min(reconnect_delay * 2, 30.0)


async def synchronize_book(
    symbol: str,
    book: LocalOrderBook,
    queue: asyncio.Queue,
    raw_handle,
    counters: dict,
    stop: asyncio.Event,
) -> bool:
    """Fetch snapshots until a buffered event bridges one without a gap."""
    while not stop.is_set():
        snapshot = await asyncio.to_thread(get_depth_snapshot, symbol)
        counters["snapshots"] += 1
        write_jsonl(raw_handle, {"kind": "snapshot", "received_at": utc_now(), "payload": snapshot})
        book.load_snapshot(snapshot)
        while not stop.is_set():
            try:
                received_at, event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            counters["depth_events"] += 1
            write_jsonl(raw_handle, {"kind": "depth", "received_at": received_at, "payload": event})
            try:
                applied = book.apply(event)
            except OrderBookSequenceError:
                counters["sequence_resyncs"] += 1
                break
            if applied:
                return True
    return False


async def collect_depth(
    symbol: str,
    queue: asyncio.Queue,
    run_dir: Path,
    counters: dict,
    stop: asyncio.Event,
    depth_levels: int,
) -> None:
    book = LocalOrderBook()
    with gzip.open(run_dir / "depth.jsonl.gz", "wt", encoding="utf-8") as raw_handle, gzip.open(
        run_dir / "book_features.jsonl.gz", "wt", encoding="utf-8"
    ) as feature_handle:
        while not stop.is_set():
            if not await synchronize_book(symbol, book, queue, raw_handle, counters, stop):
                return
            while not stop.is_set():
                try:
                    received_at, event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                counters["depth_events"] += 1
                write_jsonl(raw_handle, {"kind": "depth", "received_at": received_at, "payload": event})
                try:
                    if not book.apply(event):
                        counters["stale_depth_events"] += 1
                        continue
                    features = book.features(depth_levels)
                except OrderBookSequenceError as exc:
                    counters["sequence_resyncs"] += 1
                    counters["last_sequence_error"] = str(exc)
                    break
                features.update(
                    {
                        "kind": "book_feature",
                        "symbol": symbol,
                        "received_at": received_at,
                        "event_time_ms": int(event.get("E") or 0),
                        "transaction_time_ms": int(event.get("T") or 0),
                    }
                )
                counters["feature_rows"] += 1
                write_jsonl(feature_handle, features)


async def collect_trades(queue: asyncio.Queue, run_dir: Path, counters: dict, stop: asyncio.Event) -> None:
    with gzip.open(run_dir / "agg_trades.jsonl.gz", "wt", encoding="utf-8") as handle:
        while not stop.is_set() or not queue.empty():
            try:
                received_at, event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            counters["aggregate_trades"] += 1
            write_jsonl(handle, {"kind": "agg_trade", "received_at": received_at, "payload": event})


def file_digest(path: Path) -> dict[str, int | str]:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


async def run_collection(args, run_dir: Path) -> dict:
    stop = asyncio.Event()
    depth_queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
    trade_queue: asyncio.Queue = asyncio.Queue(maxsize=50_000)
    depth_counters = {
        "connections": 0,
        "reconnects": 0,
        "last_error": None,
        "snapshots": 0,
        "sequence_resyncs": 0,
        "last_sequence_error": None,
        "depth_events": 0,
        "stale_depth_events": 0,
        "feature_rows": 0,
    }
    trade_counters = {"connections": 0, "reconnects": 0, "last_error": None, "aggregate_trades": 0}
    stream = args.symbol.lower()
    tasks = [
        asyncio.create_task(websocket_reader(DEPTH_WS.format(stream=stream), depth_queue, depth_counters, stop)),
        asyncio.create_task(websocket_reader(TRADE_WS.format(stream=stream), trade_queue, trade_counters, stop)),
        asyncio.create_task(collect_depth(args.symbol, depth_queue, run_dir, depth_counters, stop, args.depth_levels)),
        asyncio.create_task(collect_trades(trade_queue, run_dir, trade_counters, stop)),
    ]
    started_wall = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    active_ended_wall = started_wall
    active_ended_monotonic = started_monotonic
    try:
        await asyncio.sleep(args.duration_seconds)
    finally:
        active_ended_wall = datetime.now(timezone.utc)
        active_ended_monotonic = time.monotonic()
        stop.set()
        await asyncio.sleep(0)
        for task in tasks[:2]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    active_wall_seconds = (active_ended_wall - started_wall).total_seconds()
    active_monotonic_seconds = active_ended_monotonic - started_monotonic
    tolerance_seconds = max(5.0, args.duration_seconds * 0.05)
    timing_healthy = (
        abs(active_wall_seconds - active_monotonic_seconds) <= tolerance_seconds
        and abs(active_monotonic_seconds - args.duration_seconds) <= tolerance_seconds
    )
    return {
        "started_at": started_wall.isoformat(),
        "ended_at": utc_now(),
        "requested_duration_seconds": args.duration_seconds,
        "timing": {
            "active_wall_seconds": active_wall_seconds,
            "active_monotonic_seconds": active_monotonic_seconds,
            "clock_difference_seconds": active_wall_seconds - active_monotonic_seconds,
            "tolerance_seconds": tolerance_seconds,
            "healthy": timing_healthy,
        },
        "depth": depth_counters,
        "trades": trade_counters,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--duration-seconds", type=int, default=3600)
    parser.add_argument("--depth-levels", type=int, default=5)
    parser.add_argument("--output-root", type=Path, default=Path("research/microstructure"))
    args = parser.parse_args()
    args.symbol = args.symbol.upper()
    if not re.fullmatch(r"[A-Z0-9]{5,20}", args.symbol):
        raise ValueError("invalid symbol")
    if not 5 <= args.duration_seconds <= 86_400:
        raise ValueError("duration-seconds must be between 5 and 86400")
    if not 1 <= args.depth_levels <= 20:
        raise ValueError("depth-levels must be between 1 and 20")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = args.output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    result = asyncio.run(run_collection(args, run_dir))
    files = [file_digest(path) for path in sorted(run_dir.glob("*.jsonl.gz"))]
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "symbol": args.symbol,
        "probe_kind": "public_market_data_only_no_orders_or_api_keys",
        "depth_levels": args.depth_levels,
        "endpoints": {"depth": DEPTH_WS, "trades": TRADE_WS, "snapshot": f"{REST_URL}/fapi/v1/depth"},
        "official_sequence_spec": "https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly",
        "official_connection_spec": "https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect",
        "protocol_sha256": protocol_digests(PROJECT_ROOT),
        "result": result,
        "files": files,
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), **manifest}, indent=2))
    if not bool(result["timing"]["healthy"]):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
