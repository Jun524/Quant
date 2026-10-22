#!/usr/bin/env python3
"""Run bounded hourly chunks of key-free liquidation-risk telemetry."""

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
POLICY_PATH = PROJECT_ROOT / "research" / "liquidation-telemetry-policy.json"


def _atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _run_chunk(command: list[str], *, duration: int) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=PROJECT_ROOT, start_new_session=True)
    timeout = duration + max(300, duration // 2)
    timed_out = False
    while process.poll() is None:
        if time.monotonic() - started > timeout:
            timed_out = True
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            break
        time.sleep(1)
    return {
        "returncode": 124 if timed_out else int(process.returncode),
        "collector_returncode": int(process.returncode),
        "watchdog_timed_out": timed_out,
        "wall_elapsed_seconds": time.monotonic() - started,
        "watchdog_limit_seconds": timeout,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, required=True)
    parser.add_argument("--chunk-seconds", type=int, default=3600)
    parser.add_argument("--minimum-free-gb", type=float, default=10.0)
    parser.add_argument("--output-root", type=Path, default=Path("research/liquidation_telemetry"))
    args = parser.parse_args()
    if not 1 <= args.hours <= 24 * 100:
        raise ValueError("hours must be between 1 and 2400")
    if not 300 <= args.chunk_seconds <= 86_400:
        raise ValueError("chunk-seconds must be between 300 and 86400")
    if args.minimum_free_gb < 5:
        raise ValueError("minimum-free-gb must be at least 5")

    args.output_root.mkdir(parents=True, exist_ok=True)
    lock = (args.output_root / ".campaign.lock").open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("liquidation telemetry campaign lock is already held")
    lock.seek(0)
    lock.truncate()
    lock.write(json.dumps({"pid": os.getpid(), "locked_at": datetime.now(timezone.utc).isoformat()}))
    lock.flush()

    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    eligible_start = datetime.fromisoformat(policy["eligible_start_utc"].replace("Z", "+00:00"))
    started_at = datetime.now(timezone.utc)
    deadline = max(started_at, eligible_start) + timedelta(hours=args.hours)
    state_path = args.output_root / "campaign-state.json"
    log_path = args.output_root / f"campaign-{started_at.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    state = {
        "schema_version": 1,
        "status": "WAITING_ELIGIBLE_START" if started_at < eligible_start else "RUNNING",
        "pid": os.getpid(),
        "started_at": started_at.isoformat(),
        "eligible_start": eligible_start.isoformat(),
        "deadline_at": deadline.isoformat(),
        "hours": args.hours,
        "chunk_seconds": args.chunk_seconds,
        "minimum_free_gb": args.minimum_free_gb,
        "completed_chunks": 0,
        "failed_chunks": 0,
        "current_chunk_started_at": None,
        "campaign_log": str(log_path),
    }
    _atomic(state_path, state)
    stop_requested = False

    def _request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True
        state["stop_requested"] = True
        state["status"] = "STOP_REQUESTED_WAITING_FOR_CHUNK"
        _atomic(state_path, state)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    while not stop_requested and datetime.now(timezone.utc) < eligible_start:
        state["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        _atomic(state_path, state)
        time.sleep(min(30, max(0.1, (eligible_start - datetime.now(timezone.utc)).total_seconds())))

    completed = failed = 0
    while not stop_requested and datetime.now(timezone.utc) < deadline:
        free_gb = shutil.disk_usage(PROJECT_ROOT).free / 1024**3
        if free_gb < args.minimum_free_gb:
            state["status"] = "DISK_GUARD_STOP"
            break
        remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
        duration = min(args.chunk_seconds, remaining)
        if duration < 30:
            break
        chunk_started = datetime.now(timezone.utc)
        state.update(
            {
                "status": "RUNNING",
                "heartbeat_at": chunk_started.isoformat(),
                "current_chunk_started_at": chunk_started.isoformat(),
            }
        )
        _atomic(state_path, state)
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "collect_liquidation_telemetry.py"),
            "--duration-seconds",
            str(duration),
            "--output-root",
            str(args.output_root),
        ]
        execution = _run_chunk(command, duration=duration)
        success = execution["returncode"] == 0
        completed += int(success)
        failed += int(not success)
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": "chunk_complete" if success else "chunk_failed",
            "started_at": chunk_started.isoformat(),
            "duration_seconds": duration,
            "free_gb_before": free_gb,
            **execution,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        state.update(
            {
                "status": "STOP_REQUESTED_WAITING_FOR_CHUNK" if stop_requested else "RUNNING",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "current_chunk_started_at": None,
                "completed_chunks": completed,
                "failed_chunks": failed,
                "last_chunk": event,
            }
        )
        _atomic(state_path, state)
        if not success and not stop_requested:
            time.sleep(30)
    state.update(
        {
            "status": "STOPPED" if stop_requested else state.get("status", "FINISHED"),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "current_chunk_started_at": None,
            "completed_chunks": completed,
            "failed_chunks": failed,
        }
    )
    if state["status"] == "RUNNING":
        state["status"] = "FINISHED"
    _atomic(state_path, state)


if __name__ == "__main__":
    main()
