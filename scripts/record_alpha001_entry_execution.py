#!/usr/bin/env python3
"""Shadow Alpha #1 long-entry order attempts from the local Freqtrade log.

Only the exact Freqtrade message emitted immediately before an initial long
order is treated as a trigger.  Every trigger starts a public Binance L2
queue probe with the same requested USDT stake.  No exchange credentials are
read and no order endpoint is called.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manage_alpha001_forward_dryrun import protocol_digests


SIGNAL_PATTERN = re.compile(
    r"Long signal found: about create a new trade for "
    r"(?P<pair>[A-Z0-9]+/USDT:USDT) with stake_amount: "
    r"(?P<stake>[0-9.eE+-]+) and price: (?P<price>[0-9.eE+-]+)"
)
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
_stop_requested = threading.Event()
_children_lock = threading.Lock()
_active_children: list[subprocess.Popen[str]] = []


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def parse_order_attempt(line: str, *, offset: int, detected_at: datetime) -> dict | None:
    """Return a causal trigger from Freqtrade's initial-entry log message."""
    match = SIGNAL_PATTERN.search(line)
    if not match:
        return None
    pair = match.group("pair")
    symbol = pair.replace("/USDT:USDT", "USDT")
    stake = float(match.group("stake"))
    price = float(match.group("price"))
    if symbol not in DEFAULT_SYMBOLS or stake <= 0 or stake > 50.0 or price <= 0:
        return None
    line_digest = sha256(line.encode("utf-8")).hexdigest()
    return {
        "pair": pair,
        "symbol": symbol,
        "requested_stake_usdt": stake,
        "freqtrade_requested_limit_price": price,
        "trigger_log_offset": offset,
        "trigger_line_sha256": line_digest,
        "trigger_detected_at": detected_at.isoformat(),
        "signal_id": f"alpha001-{symbol}-{offset}-{line_digest[:12]}",
    }


def _manifest_payload(
    *,
    status: str,
    output: Path,
    log_path: Path,
    start_offset: int,
    started_at: str,
    protocol: dict[str, str],
    symbols: list[str],
    rows: int,
    last_offset: int,
    failure: str | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "evidence_kind": "public_l2_shadow",
        "evidence_purpose": "live_promotion_candidate",
        "order_scope": "entry_long_only",
        "required_side": "buy",
        "trigger_kind": "alpha001_live_order_attempt",
        "source_path": str(output.relative_to(PROJECT_ROOT)),
        "source_sha256": _digest(output),
        "trigger_log_path": str(log_path.relative_to(PROJECT_ROOT)),
        "trigger_log_start_offset": start_offset,
        "trigger_log_last_offset": last_offset,
        "protocol_sha256": protocol,
        "symbols": symbols,
        "maximum_notional_usdt": 50.0,
        "timeout_seconds": 300,
        "markout_seconds": 300,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "orders": rows,
        "failure": failure,
    }


def _run_probe(trigger: dict, output: Path) -> dict:
    temporary = output.with_name(f".{output.name}.{uuid4().hex}.probe")
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "shadow_maker_probe.py"),
        "--symbol", trigger["symbol"],
        "--side", "buy",
        "--trigger-kind", "alpha001_live_order_attempt",
        "--notional-usdt", str(trigger["requested_stake_usdt"]),
        "--limit-price", str(trigger["freqtrade_requested_limit_price"]),
        "--timeout-seconds", "300",
        "--markout-seconds", "300",
        "--signal-id", trigger["signal_id"],
        "--output", str(temporary),
    ]
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with _children_lock:
        _active_children.append(process)
    stdout, _ = process.communicate()
    with _children_lock:
        if process in _active_children:
            _active_children.remove(process)
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"shadow probe exited {process.returncode}: {stdout[-1000:]}")
    try:
        lines = [line for line in temporary.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) != 1:
            raise ValueError("shadow probe did not produce exactly one row")
        row = json.loads(lines[0])
    finally:
        temporary.unlink(missing_ok=True)
    row.update(
        {
            "trigger_source": "local_freqtrade_initial_entry_log",
            "trigger_pair": trigger["pair"],
            "trigger_log_offset": trigger["trigger_log_offset"],
            "trigger_line_sha256": trigger["trigger_line_sha256"],
            "trigger_detected_at": trigger["trigger_detected_at"],
            "requested_stake_usdt": trigger["requested_stake_usdt"],
            "freqtrade_requested_limit_price": trigger["freqtrade_requested_limit_price"],
        }
    )
    return row


def _handle_stop(_signum, _frame) -> None:
    _stop_requested.set()
    with _children_lock:
        for process in list(_active_children):
            if process.poll() is None:
                process.terminate()


def record(
    *,
    log_path: Path,
    output: Path,
    manifest_path: Path,
    start_offset: int,
    poll_seconds: float,
    duration_seconds: int,
    symbols: list[str],
) -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(exist_ok=False)
    started_at = datetime.now(timezone.utc).isoformat()
    protocol = protocol_digests()
    offset = start_offset
    rows = 0
    pending: dict[Future, dict] = {}
    seen: set[str] = set()
    deadline = time.monotonic() + duration_seconds
    last_manifest_heartbeat = time.monotonic()
    failure: str | None = None
    _atomic_write(
        manifest_path,
        _manifest_payload(
            status="ACTIVE",
            output=output,
            log_path=log_path,
            start_offset=start_offset,
            started_at=started_at,
            protocol=protocol,
            symbols=symbols,
            rows=rows,
            last_offset=offset,
        ),
    )
    try:
        with ThreadPoolExecutor(max_workers=len(symbols)) as executor:
            while not _stop_requested.is_set() and time.monotonic() < deadline:
                if protocol_digests() != protocol:
                    raise RuntimeError("research protocol changed while entry execution recorder was active")
                size = log_path.stat().st_size
                if size < offset:
                    raise RuntimeError("Freqtrade log was truncated while entry recorder was active")
                if size > offset:
                    with log_path.open("rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(size - offset)
                    cursor = offset
                    for raw_line in chunk.splitlines(keepends=True):
                        if not raw_line.endswith((b"\n", b"\r")):
                            break
                        line_offset = cursor
                        cursor += len(raw_line)
                        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                        trigger = parse_order_attempt(
                            line,
                            offset=line_offset,
                            detected_at=datetime.now(timezone.utc),
                        )
                        if trigger and trigger["symbol"] in symbols and trigger["signal_id"] not in seen:
                            seen.add(trigger["signal_id"])
                            pending[executor.submit(_run_probe, trigger, output)] = trigger
                    offset = cursor
                for future in list(pending):
                    if not future.done():
                        continue
                    trigger = pending.pop(future)
                    try:
                        row = future.result()
                    except Exception as error:
                        raise RuntimeError(
                            f"entry shadow probe failed for {trigger['signal_id']}: {type(error).__name__}: {error}"
                        ) from error
                    with output.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    rows += 1
                    _atomic_write(
                        manifest_path,
                        _manifest_payload(
                            status="ACTIVE",
                            output=output,
                            log_path=log_path,
                            start_offset=start_offset,
                            started_at=started_at,
                            protocol=protocol,
                            symbols=symbols,
                            rows=rows,
                            last_offset=offset,
                        ),
                    )
                    last_manifest_heartbeat = time.monotonic()
                if time.monotonic() - last_manifest_heartbeat >= 60:
                    _atomic_write(
                        manifest_path,
                        _manifest_payload(
                            status="ACTIVE",
                            output=output,
                            log_path=log_path,
                            start_offset=start_offset,
                            started_at=started_at,
                            protocol=protocol,
                            symbols=symbols,
                            rows=rows,
                            last_offset=offset,
                        ),
                    )
                    last_manifest_heartbeat = time.monotonic()
                time.sleep(poll_seconds)
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
        raise
    finally:
        status = "FAILED" if failure else "SEALED_ON_STOP"
        _atomic_write(
            manifest_path,
            _manifest_payload(
                status=status,
                output=output,
                log_path=log_path,
                start_offset=start_offset,
                started_at=started_at,
                protocol=protocol,
                symbols=symbols,
                rows=rows,
                last_offset=offset,
                failure=failure,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--start-offset", type=int, required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--duration-seconds", type=int, default=366 * 24 * 3600)
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    args = parser.parse_args()
    if args.start_offset < 0 or args.poll_seconds <= 0 or args.duration_seconds < 1:
        parser.error("start-offset must be non-negative and durations must be positive")
    record(
        log_path=args.log_path.resolve(),
        output=args.output.resolve(),
        manifest_path=args.manifest.resolve(),
        start_offset=args.start_offset,
        poll_seconds=args.poll_seconds,
        duration_seconds=args.duration_seconds,
        symbols=[symbol.upper() for symbol in args.symbols],
    )


if __name__ == "__main__":
    main()
