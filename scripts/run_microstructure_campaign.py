#!/usr/bin/env python3
"""Run bounded, disk-guarded Alpha #7 collection chunks.

This supervisor only launches the public-data collector and report builder. It
contains no exchange credentials and cannot submit orders.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.campaign import microstructure_campaign_preflight


def atomic_write_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def chunk_wall_timeout_seconds(duration_seconds: int) -> int:
    """Allow normal finalization and short sleep, but never wait forever."""
    return duration_seconds + max(900, duration_seconds)


def run_chunk_with_watchdog(
    command: list[str],
    *,
    cwd: Path,
    wall_timeout_seconds: float,
    poll_interval_seconds: float = 1.0,
    terminate_grace_seconds: float = 10.0,
) -> dict:
    started_wall = time.time()
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    timed_out = False
    while process.poll() is None:
        if time.time() - started_wall > wall_timeout_seconds:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            break
        time.sleep(poll_interval_seconds)
    return {
        "returncode": 124 if timed_out else int(process.returncode),
        "collector_returncode": int(process.returncode),
        "watchdog_timed_out": timed_out,
        "wall_elapsed_seconds": time.time() - started_wall,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, required=True, help="Bounded wall-clock campaign length")
    parser.add_argument("--chunk-seconds", type=int, default=3600)
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--minimum-free-gb", type=float, default=25.0)
    parser.add_argument("--report-every-chunks", type=int, default=24)
    parser.add_argument("--output-root", type=Path, default=Path("research/microstructure"))
    args = parser.parse_args()
    if not 1 <= args.hours <= 24 * 31:
        raise ValueError("hours must be between 1 and 744")
    if not 300 <= args.chunk_seconds <= 86_400:
        raise ValueError("chunk-seconds must be between 300 and 86400")
    if args.minimum_free_gb < 5:
        raise ValueError("minimum-free-gb must be at least 5")
    if args.report_every_chunks < 1:
        raise ValueError("report-every-chunks must be positive")

    args.output_root.mkdir(parents=True, exist_ok=True)
    lock_handle = (args.output_root / ".campaign.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another microstructure campaign already holds the collection lock", file=sys.stderr)
        raise SystemExit(4)
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(json.dumps({"pid": os.getpid(), "locked_at": datetime.now(timezone.utc).isoformat()}))
    lock_handle.flush()

    started_at = datetime.now(timezone.utc)
    deadline_at = started_at + timedelta(hours=args.hours)
    campaign_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    campaign_log = args.output_root / f"campaign-{campaign_id}.jsonl"
    state_path = args.output_root / "campaign-state.json"
    state = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "pid": os.getpid(),
        "status": "PREFLIGHT",
        "started_at": started_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "ended_at": None,
        "hours": args.hours,
        "chunk_seconds": args.chunk_seconds,
        "symbol": args.symbol,
        "minimum_free_gb": args.minimum_free_gb,
        "completed_chunks": 0,
        "failed_chunks": 0,
        "current_chunk_started_at": None,
        "stop_requested": False,
        "campaign_log": str(campaign_log),
    }
    atomic_write_json(state_path, state)
    preflight = microstructure_campaign_preflight(
        args.output_root,
        campaign_hours=args.hours,
        minimum_free_after_gib=args.minimum_free_gb,
        disk_path=PROJECT_ROOT,
    )
    preflight_event = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": "campaign_preflight",
        "result": preflight,
    }
    with campaign_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(preflight_event) + "\n")
    if preflight["status"] != "READY":
        state.update(
            {
                "status": "PREFLIGHT_FAILED",
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "preflight": preflight,
            }
        )
        atomic_write_json(state_path, state)
        print(json.dumps(preflight_event, indent=2), file=sys.stderr)
        raise SystemExit(2)

    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        state["stop_requested"] = True
        state["status"] = "STOP_REQUESTED_WAITING_FOR_CHUNK"
        atomic_write_json(state_path, state)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    state["status"] = "RUNNING"
    state["preflight"] = preflight
    atomic_write_json(state_path, state)

    completed_chunks = 0
    failed_chunks = 0
    terminal_reason = "deadline_reached"
    while not stop_requested and datetime.now(timezone.utc) < deadline_at:
        free_gb = shutil.disk_usage(PROJECT_ROOT).free / 1024**3
        if free_gb < args.minimum_free_gb:
            event = {"at": datetime.now(timezone.utc).isoformat(), "event": "disk_guard_stop", "free_gb": free_gb}
            with campaign_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
            print(json.dumps(event), file=sys.stderr)
            terminal_reason = "disk_guard_stop"
            break
        remaining = max(0, int((deadline_at - datetime.now(timezone.utc)).total_seconds()))
        duration = min(args.chunk_seconds, remaining)
        if duration < 5:
            break
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "collect_microstructure.py"),
            "--symbol",
            args.symbol,
            "--duration-seconds",
            str(duration),
            "--output-root",
            str(args.output_root),
        ]
        started = datetime.now(timezone.utc).isoformat()
        state["current_chunk_started_at"] = started
        atomic_write_json(state_path, state)
        execution = run_chunk_with_watchdog(
            command,
            cwd=PROJECT_ROOT,
            wall_timeout_seconds=chunk_wall_timeout_seconds(duration),
        )
        returncode = int(execution["returncode"])
        completed_chunks += returncode == 0
        failed_chunks += returncode != 0
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "chunk_complete" if returncode == 0 else "chunk_failed",
            "started_at": started,
            "duration_seconds": duration,
            "returncode": returncode,
            "collector_returncode": execution["collector_returncode"],
            "watchdog_timed_out": execution["watchdog_timed_out"],
            "wall_elapsed_seconds": execution["wall_elapsed_seconds"],
            "watchdog_limit_seconds": chunk_wall_timeout_seconds(duration),
            "free_gb_before": free_gb,
        }
        with campaign_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        state.update(
            {
                "completed_chunks": completed_chunks,
                "failed_chunks": failed_chunks,
                "current_chunk_started_at": None,
                "last_chunk": event,
                "status": "STOP_REQUESTED_WAITING_FOR_CHUNK" if stop_requested else "RUNNING",
            }
        )
        atomic_write_json(state_path, state)
        if returncode == 0 and completed_chunks and completed_chunks % args.report_every_chunks == 0:
            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "analyze_microstructure.py")],
                cwd=PROJECT_ROOT,
                check=False,
            )
            subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_alpha007_registry.py")],
                cwd=PROJECT_ROOT,
                check=False,
            )
        if returncode != 0:
            time.sleep(30)
    if stop_requested:
        terminal_reason = "graceful_stop_requested"
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "analyze_microstructure.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "sync_alpha007_registry.py")],
        cwd=PROJECT_ROOT,
        check=False,
    )
    state.update(
        {
            "status": "STOPPED" if stop_requested else "FINISHED",
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "completed_chunks": completed_chunks,
            "failed_chunks": failed_chunks,
            "current_chunk_started_at": None,
            "terminal_reason": terminal_reason,
        }
    )
    atomic_write_json(state_path, state)
    print(json.dumps({"campaign_id": campaign_id, "completed_chunks": completed_chunks, "failed_chunks": failed_chunks, "terminal_reason": terminal_reason, "log": str(campaign_log)}))


if __name__ == "__main__":
    main()
