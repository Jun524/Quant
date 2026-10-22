#!/usr/bin/env python3
"""Run bounded, resumable historical-universe acquisition in the background."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "research" / "historical_universe"
LOCK_PATH = STATE_DIR / "scheduler.lock"
STATUS_PATH = STATE_DIR / "scheduler-status.json"
PROGRESS_PATH = PROJECT_ROOT / "reports" / "historical-universe-acquisition.json"
LOG_PATH = PROJECT_ROOT / "user_data" / "logs" / "historical-universe-scheduler.log"
STOP_REQUESTED = False
ACTIVE_CHILD: subprocess.Popen | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_name(f".{STATUS_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, STATUS_PATH)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
    return completed.returncode == 0 and "run_historical_universe_scheduler.py run" in completed.stdout


def _stop_requested(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    if ACTIVE_CHILD is not None and ACTIVE_CHILD.poll() is None:
        ACTIVE_CHILD.terminate()


def _run_collector(batch_size: int, *, timeout_seconds: int) -> int:
    """Run one bounded child and ensure signal-driven stops do not orphan it."""
    global ACTIVE_CHILD
    process = subprocess.Popen(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "collect_historical_daily_universe.py"), "--resume", "--archive-limit", str(batch_size)],
        cwd=PROJECT_ROOT,
    )
    ACTIVE_CHILD = process
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 124
    finally:
        ACTIVE_CHILD = None


def _progress_summary() -> dict:
    try:
        report = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"status": "NOT_YET_REPORTED"}
    return {
        key: report.get(key)
        for key in ("status", "expected_archives", "resolved_archives", "remaining_archives")
    }


def run(batch_size: int, interval_seconds: int, batch_timeout_seconds: int) -> None:
    if not 1 <= batch_size <= 250 or not 30 <= interval_seconds <= 900 or not 60 <= batch_timeout_seconds <= 1800:
        raise SystemExit("invalid batch-size, interval-seconds, or batch-timeout-seconds")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("historical universe scheduler is already running")
    signal.signal(signal.SIGTERM, _stop_requested)
    signal.signal(signal.SIGINT, _stop_requested)
    started_at = _now()
    last_exit: int | None = None
    while not STOP_REQUESTED:
        _write_status({
            "schema_version": 1, "state": "ACQUIRING", "pid": os.getpid(),
            "started_at": started_at, "heartbeat_at": _now(), "last_exit_code": last_exit,
            "batch_size": batch_size, "interval_seconds": interval_seconds,
            "batch_timeout_seconds": batch_timeout_seconds,
            "progress": _progress_summary(), "live_order_authorization": "CLOSED",
        })
        last_exit = _run_collector(batch_size, timeout_seconds=batch_timeout_seconds)
        progress = _progress_summary()
        if last_exit != 0:
            state = "RETRY_PENDING"
        elif progress.get("status") == "ACQUISITION_COMPLETE":
            final = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "collect_historical_daily_universe.py"), "--finalize"],
                cwd=PROJECT_ROOT,
                check=False,
            )
            audit = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "audit_historical_universe.py")],
                cwd=PROJECT_ROOT,
                check=False,
            ) if final.returncode == 0 else None
            final_exit = audit.returncode if audit is not None else final.returncode
            _write_status({
                "schema_version": 1, "state": "COMPLETED" if final_exit == 0 else "FINALIZE_OR_AUDIT_FAILED",
                "pid": os.getpid(), "started_at": started_at, "heartbeat_at": _now(),
                "last_exit_code": final_exit, "batch_size": batch_size,
                "interval_seconds": interval_seconds, "batch_timeout_seconds": batch_timeout_seconds,
                "progress": _progress_summary(),
                "live_order_authorization": "CLOSED",
            })
            return
        else:
            state = "WAITING_NEXT_BATCH"
        _write_status({
            "schema_version": 1, "state": state, "pid": os.getpid(), "started_at": started_at,
            "heartbeat_at": _now(), "last_exit_code": last_exit, "batch_size": batch_size,
            "interval_seconds": interval_seconds, "batch_timeout_seconds": batch_timeout_seconds,
            "progress": progress,
            "live_order_authorization": "CLOSED",
        })
        deadline = time.monotonic() + interval_seconds
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    _write_status({
        "schema_version": 1, "state": "STOPPED", "pid": os.getpid(), "started_at": started_at,
        "heartbeat_at": _now(), "last_exit_code": last_exit, "batch_size": batch_size,
        "interval_seconds": interval_seconds, "batch_timeout_seconds": batch_timeout_seconds,
        "progress": _progress_summary(),
        "live_order_authorization": "CLOSED",
    })


def start(batch_size: int, interval_seconds: int, batch_timeout_seconds: int) -> None:
    existing = _load_status()
    if existing and _alive(int(existing.get("pid") or -1)):
        print(f"Historical universe scheduler already running (pid={existing['pid']})")
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--batch-size", str(batch_size), "--interval-seconds", str(interval_seconds), "--batch-timeout-seconds", str(batch_timeout_seconds)],
            cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    if shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-ims", "-w", str(process.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(1)
    if not _alive(process.pid):
        raise SystemExit("historical universe scheduler failed to start")
    print(f"Historical universe scheduler started (pid={process.pid})")


def scheduler_health(payload: dict | None, *, running: bool, now: datetime | None = None) -> dict:
    """Classify a scheduler from both its process handle and heartbeat age."""
    if payload is None:
        return {"health": "NOT_STARTED", "heartbeat_age_seconds": None}
    state = str(payload.get("state") or "UNKNOWN")
    if state == "COMPLETED":
        return {"health": "COMPLETED", "heartbeat_age_seconds": 0.0}
    try:
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"])).astimezone(timezone.utc)
        age = (now or datetime.now(timezone.utc)) - heartbeat
        age_seconds: float | None = age.total_seconds()
    except (KeyError, TypeError, ValueError):
        age_seconds = None
    timeout = int(payload.get("batch_timeout_seconds") or 180)
    interval = int(payload.get("interval_seconds") or 60)
    fresh_limit = timeout + interval + 90
    if not running:
        health = "STOPPED"
    elif age_seconds is None or age_seconds < -60 or age_seconds > fresh_limit:
        health = "STALE"
    else:
        health = "RUNNING"
    return {"health": health, "heartbeat_age_seconds": age_seconds, "heartbeat_freshness_limit_seconds": fresh_limit}


def status() -> None:
    payload = _load_status()
    if payload is None:
        print(json.dumps({"health": "NOT_STARTED", "running": False}, indent=2))
        return
    pid = int(payload.get("pid") or -1)
    running = _alive(pid)
    print(json.dumps({**payload, "running": running, **scheduler_health(payload, running=running)}, indent=2))


def stop() -> None:
    payload = _load_status()
    pid = int((payload or {}).get("pid") or -1)
    if not _alive(pid):
        print("Historical universe scheduler is not running")
        return
    os.kill(pid, signal.SIGTERM)
    print(f"Historical universe scheduler stop requested (pid={pid})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "start"):
        command = subparsers.add_parser(name)
        command.add_argument("--batch-size", type=int, default=100)
        command.add_argument("--interval-seconds", type=int, default=60)
        command.add_argument("--batch-timeout-seconds", type=int, default=180)
    subparsers.add_parser("status")
    subparsers.add_parser("stop")
    args = parser.parse_args()
    if args.command == "run":
        run(args.batch_size, args.interval_seconds, args.batch_timeout_seconds)
    elif args.command == "start":
        start(args.batch_size, args.interval_seconds, args.batch_timeout_seconds)
    elif args.command == "status":
        status()
    else:
        stop()


if __name__ == "__main__":
    main()
