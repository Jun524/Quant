#!/usr/bin/env python3
"""Run the Alpha #11 archive updater periodically with a single-process lock."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = PROJECT_ROOT / "research" / "alpha011"
LOCK_PATH = STATE_DIR / "update-scheduler.lock"
STATUS_PATH = STATE_DIR / "update-scheduler-status.json"
STOP_REQUESTED = False
LOG_PATH = PROJECT_ROOT / "user_data" / "logs" / "alpha011-scheduler.log"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_status(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_name(f".{STATUS_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def _request_stop(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _scheduler_process_alive(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0 and "run_alpha011_scheduler.py run" in completed.stdout


def _read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run_scheduler(interval_seconds: int) -> None:
    if interval_seconds < 300:
        raise SystemExit("interval-seconds must be at least 300")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Alpha #11 update scheduler is already running")

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    started_at = _now()
    last_exit_code: int | None = None
    last_finished_at: str | None = None
    while not STOP_REQUESTED:
        run_started = _now()
        _write_status(
            {
                "schema_version": 1,
                "state": "UPDATING",
                "pid": os.getpid(),
                "scheduler_started_at": started_at.isoformat(),
                "heartbeat_at": run_started.isoformat(),
                "last_finished_at": last_finished_at,
                "last_exit_code": last_exit_code,
                "interval_seconds": interval_seconds,
            }
        )
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "update_alpha011_data.py")],
            cwd=PROJECT_ROOT,
            check=False,
        )
        last_exit_code = completed.returncode
        finished = _now()
        last_finished_at = finished.isoformat()
        next_run = finished + timedelta(seconds=interval_seconds)
        _write_status(
            {
                "schema_version": 1,
                "state": "SLEEPING" if last_exit_code == 0 else "RETRY_PENDING",
                "pid": os.getpid(),
                "scheduler_started_at": started_at.isoformat(),
                "heartbeat_at": finished.isoformat(),
                "last_finished_at": last_finished_at,
                "last_exit_code": last_exit_code,
                "next_run_at": next_run.isoformat(),
                "interval_seconds": interval_seconds,
            }
        )
        deadline = time.monotonic() + interval_seconds
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(60.0, max(0.0, deadline - time.monotonic())))

    _write_status(
        {
            "schema_version": 1,
            "state": "STOPPED",
            "pid": os.getpid(),
            "scheduler_started_at": started_at.isoformat(),
            "heartbeat_at": _now().isoformat(),
            "last_finished_at": last_finished_at,
            "last_exit_code": last_exit_code,
            "interval_seconds": interval_seconds,
        }
    )


def start_scheduler(interval_seconds: int) -> None:
    existing = _read_status()
    if existing is not None and _scheduler_process_alive(int(existing.get("pid") or -1)):
        print(f"Alpha #11 update scheduler is already running (pid={existing['pid']})")
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--interval-seconds",
                str(interval_seconds),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(1.0)
    if not _scheduler_process_alive(process.pid):
        raise SystemExit("Alpha #11 update scheduler failed to stay running")
    print(f"Alpha #11 update scheduler started (pid={process.pid})")


def show_status() -> None:
    payload = _read_status()
    if payload is None:
        raise SystemExit("Alpha #11 scheduler status is absent or invalid")
    payload["process_running"] = _scheduler_process_alive(int(payload.get("pid") or -1))
    print(json.dumps(payload, indent=2))
    if not payload["process_running"]:
        raise SystemExit(2)


def stop_scheduler() -> None:
    payload = _read_status()
    if payload is None:
        raise SystemExit("Alpha #11 scheduler status is absent or invalid")
    pid = int(payload.get("pid") or -1)
    if not _scheduler_process_alive(pid):
        raise SystemExit("Alpha #11 scheduler process is not running")
    os.kill(pid, signal.SIGTERM)
    print(f"Stop requested for Alpha #11 update scheduler (pid={pid})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "run", "status", "stop"))
    parser.add_argument("--interval-seconds", type=int, default=21_600)
    args = parser.parse_args()
    if args.action == "start":
        start_scheduler(args.interval_seconds)
    elif args.action == "run":
        run_scheduler(args.interval_seconds)
    elif args.action == "status":
        show_status()
    else:
        stop_scheduler()


if __name__ == "__main__":
    main()
