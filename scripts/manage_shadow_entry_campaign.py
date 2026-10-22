#!/usr/bin/env python3
"""Collect or inspect a credential-free Alpha #1 entry execution campaign.

The campaign launches five public-L2 shadow probes in parallel, one for each
live-whitelist symbol.  It never accepts credentials and never submits an
order.  Every probe models the candidate's 50 USDT long-entry limit order at
the best bid, then records either a five-minute post-fill markout or the
unfilled opportunity at timeout.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.monitoring.sleep_guard import inspect_caffeinate_guard
from scripts.manage_alpha001_forward_dryrun import protocol_digests


STATE_PATH = PROJECT_ROOT / "research" / "execution" / "shadow-entry-campaign-state.json"
RAW_ROOT = PROJECT_ROOT / "research" / "execution" / "raw"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
_stop_requested = threading.Event()
_active_lock = threading.Lock()
_active_children: list[subprocess.Popen[str]] = []


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_state() -> dict | None:
    if not STATE_PATH.is_file():
        return None
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _update_state(**changes) -> dict:
    state = _load_state() or {}
    state.update(changes)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write(STATE_PATH, state)
    return state


def _process_running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "manage_shadow_entry_campaign.py run" in completed.stdout


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def progress(state: dict) -> dict:
    campaign_dir = PROJECT_ROOT / str(state["campaign_dir"])
    target = int(state["orders_per_symbol"])
    by_symbol: dict[str, dict] = {}
    total = fills = coverage_failures = 0
    for symbol in state["symbols"]:
        rows = _rows(campaign_dir / f"{symbol}.jsonl")
        valid = [
            row
            for row in rows
            if row.get("symbol") == symbol
            and row.get("side") == "buy"
            and row.get("probe_kind") == "public_queue_estimate_no_order_submitted"
        ]
        filled = sum(float(row.get("filled_qty") or 0) > 0 for row in valid)
        incomplete = sum(
            (float(row.get("filled_qty") or 0) > 0 and not row.get("mid_after_5m"))
            or (
                float(row.get("filled_qty") or 0) < float(row.get("submitted_qty") or 0)
                and not row.get("mid_after_timeout")
            )
            for row in valid
        )
        by_symbol[symbol] = {
            "orders": len(valid),
            "target": target,
            "fills": filled,
            "coverage_failures": incomplete,
        }
        total += len(valid)
        fills += filled
        coverage_failures += incomplete
    return {
        "orders": total,
        "target": target * len(state["symbols"]),
        "fills": fills,
        "coverage_failures": coverage_failures,
        "by_symbol": by_symbol,
    }


def _probe(state: dict, symbol: str, sequence: int) -> dict:
    campaign_dir = PROJECT_ROOT / state["campaign_dir"]
    output = campaign_dir / f"{symbol}.jsonl"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "shadow_maker_probe.py"),
        "--symbol", symbol,
        "--side", "buy",
        "--trigger-kind", "periodic_calibration",
        "--notional-usdt", str(state["notional_usdt"]),
        "--timeout-seconds", str(state["timeout_seconds"]),
        "--markout-seconds", str(state["markout_seconds"]),
        "--poll-seconds", str(state["poll_seconds"]),
        "--signal-id", f"{state['campaign_id']}-{symbol}-{sequence:03d}-{uuid4().hex[:8]}",
        "--output", str(output),
    ]
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    with _active_lock:
        _active_children.append(process)
    stdout, _ = process.communicate()
    with _active_lock:
        if process in _active_children:
            _active_children.remove(process)
    return {"symbol": symbol, "returncode": process.returncode, "output": stdout[-2000:]}


def _handle_stop(_signum, _frame) -> None:
    _stop_requested.set()
    with _active_lock:
        for process in list(_active_children):
            if process.poll() is None:
                process.terminate()


def _seal(state: dict) -> tuple[Path, Path]:
    campaign_dir = PROJECT_ROOT / state["campaign_dir"]
    output = PROJECT_ROOT / state["output"]
    all_rows: list[dict] = []
    for symbol in state["symbols"]:
        rows = _rows(campaign_dir / f"{symbol}.jsonl")
        all_rows.extend(rows[: int(state["orders_per_symbol"])])
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in all_rows),
        encoding="utf-8",
    )
    os.replace(temporary, output)
    manifest = output.with_suffix(".manifest.json")
    _atomic_write(
        manifest,
        {
            "schema_version": 1,
            "status": "COMPLETE",
            "evidence_kind": "public_l2_shadow",
            "order_scope": "entry_long_only",
            "required_side": "buy",
            "evidence_purpose": "calibration_only_not_live_promotion",
            "trigger_kind": "periodic_calibration",
            "campaign_id": state["campaign_id"],
            "created_at": state["started_at"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "source_path": str(output.relative_to(PROJECT_ROOT)),
            "source_sha256": _digest(output),
            "protocol_sha256": state["protocol_sha256"],
            "symbols": state["symbols"],
            "orders_per_symbol": state["orders_per_symbol"],
            "notional_usdt": state["notional_usdt"],
            "timeout_seconds": state["timeout_seconds"],
            "markout_seconds": state["markout_seconds"],
            "poll_seconds": state["poll_seconds"],
            "orders": len(all_rows),
        },
    )
    return output, manifest


def run_campaign() -> None:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    state = _load_state()
    if not state:
        raise SystemExit("campaign state is missing")
    _update_state(status="RUNNING", pid=os.getpid())
    consecutive_failure_rounds = 0
    while not _stop_requested.is_set():
        state = _load_state() or state
        observed = progress(state)
        pending = [
            symbol
            for symbol, item in observed["by_symbol"].items()
            if item["orders"] < int(state["orders_per_symbol"])
        ]
        if not pending:
            output, manifest = _seal(state)
            _update_state(
                status="COMPLETE",
                completed_at=datetime.now(timezone.utc).isoformat(),
                output=str(output.relative_to(PROJECT_ROOT)),
                manifest=str(manifest.relative_to(PROJECT_ROOT)),
                progress=progress(state),
            )
            return
        _update_state(current_symbols=pending, progress=observed)
        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=len(pending)) as executor:
            futures = {
                executor.submit(
                    _probe,
                    state,
                    symbol,
                    observed["by_symbol"][symbol]["orders"] + 1,
                ): symbol
                for symbol in pending
            }
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as error:  # supervisor records and retries public endpoint failures
                    results.append(
                        {"symbol": futures[future], "returncode": -1, "output": f"{type(error).__name__}: {error}"}
                    )
        failures = [item for item in results if item["returncode"] != 0]
        consecutive_failure_rounds = consecutive_failure_rounds + 1 if failures else 0
        _update_state(
            current_symbols=[],
            last_round=results,
            consecutive_failure_rounds=consecutive_failure_rounds,
            progress=progress(state),
        )
        if consecutive_failure_rounds >= 5:
            _update_state(status="FAILED", failure="five consecutive probe rounds failed")
            raise SystemExit(2)
    _update_state(status="STOPPED", stopped_at=datetime.now(timezone.utc).isoformat(), progress=progress(state))


def start(args) -> None:
    existing = _load_state()
    if existing and _process_running(existing.get("pid")):
        raise SystemExit(f"campaign already running with pid {existing['pid']}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    campaign_id = f"alpha001-entry-{stamp}"
    campaign_dir = RAW_ROOT / campaign_id
    campaign_dir.mkdir(parents=True, exist_ok=False)
    output = RAW_ROOT / f"{campaign_id}.jsonl"
    log_path = PROJECT_ROOT / "research" / "execution" / f"{campaign_id}.log"
    state = {
        "schema_version": 1,
        "status": "STARTING",
        "campaign_id": campaign_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "symbols": list(args.symbols),
        "orders_per_symbol": args.orders_per_symbol,
        "notional_usdt": args.notional_usdt,
        "timeout_seconds": args.timeout_seconds,
        "markout_seconds": args.markout_seconds,
        "poll_seconds": args.poll_seconds,
        "campaign_dir": str(campaign_dir.relative_to(PROJECT_ROOT)),
        "output": str(output.relative_to(PROJECT_ROOT)),
        "log": str(log_path.relative_to(PROJECT_ROOT)),
        "protocol_sha256": protocol_digests(),
    }
    _atomic_write(STATE_PATH, state)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run"],
            cwd=PROJECT_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    guard_pid = None
    if shutil.which("caffeinate"):
        guard = subprocess.Popen(
            ["caffeinate", "-ims", "-w", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        guard_pid = guard.pid
    _update_state(pid=process.pid, caffeinate_pid=guard_pid)
    time.sleep(1)
    print(json.dumps(status_payload(), indent=2))


def status_payload() -> dict:
    state = _load_state()
    if not state:
        return {"status": "NOT_STARTED"}
    running = _process_running(state.get("pid"))
    guard = inspect_caffeinate_guard(
        watched_pid=state.get("pid"),
    )
    payload = dict(state)
    payload["running"] = running
    payload["sleep_guard"] = guard
    payload["protocol_current"] = state.get("protocol_sha256") == protocol_digests()
    payload["progress"] = progress(state)
    if state.get("status") == "COMPLETE" and payload["protocol_current"]:
        payload["health"] = "COMPLETE"
    elif state.get("status") == "COMPLETE":
        payload["health"] = "COMPLETE_LEGACY_PROTOCOL"
    elif state.get("status") in {"STARTING", "RUNNING"} and not running:
        payload["health"] = "SUPERVISOR_MISSING"
    elif running and not guard.get("running"):
        payload["health"] = "RUNNING_SLEEP_GUARD_MISSING"
    elif running and payload["protocol_current"]:
        payload["health"] = "RUNNING_HEALTHY"
    elif running:
        payload["health"] = "RUNNING_LEGACY_PROTOCOL"
    else:
        payload["health"] = "TERMINAL"
    return payload


def stop() -> None:
    state = _load_state()
    if not state or not _process_running(state.get("pid")):
        print(json.dumps({"status": "NOT_RUNNING"}, indent=2))
        return
    os.kill(int(state["pid"]), signal.SIGTERM)
    print(json.dumps({"status": "STOP_REQUESTED", "pid": state["pid"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    start_parser.add_argument("--orders-per-symbol", type=int, default=20)
    start_parser.add_argument("--notional-usdt", type=float, default=50.0)
    start_parser.add_argument("--timeout-seconds", type=int, default=300)
    start_parser.add_argument("--markout-seconds", type=int, default=300)
    start_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=15.0,
        help="Calibration REST polling interval; 15s keeps aggregate-trade weight conservative",
    )
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    subparsers.add_parser("run")
    args = parser.parse_args()
    if args.command == "start":
        if args.orders_per_symbol < 1 or args.notional_usdt <= 0 or args.poll_seconds < 10:
            raise SystemExit(
                "orders-per-symbol/notional-usdt must be positive and calibration poll >= 10s"
            )
        start(args)
    elif args.command == "status":
        print(json.dumps(status_payload(), indent=2))
    elif args.command == "stop":
        stop()
    else:
        run_campaign()


if __name__ == "__main__":
    main()
