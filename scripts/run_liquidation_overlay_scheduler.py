#!/usr/bin/env python3
"""Persistently refresh coverage and open validation only after development PASS."""

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
STATE_DIR = PROJECT_ROOT / "research" / "liquidation_telemetry"
STATUS_PATH = STATE_DIR / "overlay-scheduler-status.json"
LOCK_PATH = STATE_DIR / ".overlay-scheduler.lock"
VALIDATION_LOCK = STATE_DIR / "validation-lock.json"
LOG_PATH = PROJECT_ROOT / "user_data" / "logs" / "liquidation-overlay-scheduler.log"
STOP_REQUESTED = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(STATUS_PATH.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def _read() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and "run_liquidation_overlay_scheduler.py run" in result.stdout


def _stop(_signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _report_status(path: Path) -> str:
    try:
        return str(json.loads(path.read_text(encoding="utf-8"))["status"])
    except (OSError, KeyError, json.JSONDecodeError):
        return "MISSING_OR_INVALID"


def _cycle() -> tuple[int, str]:
    commands = [
        PROJECT_ROOT / "scripts" / "liquidation_telemetry_status.py",
        PROJECT_ROOT / "scripts" / "analyze_liquidation_overlay.py",
    ]
    exit_code = 0
    for script in commands:
        completed = subprocess.run([sys.executable, str(script)], cwd=PROJECT_ROOT, check=False)
        exit_code = max(exit_code, completed.returncode)
    development_status = _report_status(
        PROJECT_ROOT / "reports" / "liquidation-overlay-development.json"
    )
    if development_status == "DEVELOPMENT_PASS_REQUIRES_EXPLICIT_LOCK_OPEN":
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "open_liquidation_overlay_validation.py")],
            cwd=PROJECT_ROOT,
            check=False,
        )
        exit_code = max(exit_code, completed.returncode)
    if VALIDATION_LOCK.is_file():
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "analyze_liquidation_overlay_validation.py")],
            cwd=PROJECT_ROOT,
            check=False,
        )
        validation_status = _report_status(
            PROJECT_ROOT / "reports" / "liquidation-overlay-validation.json"
        )
        if validation_status in {"FAIL", "PASS_RISK_OVERLAY_ONLY_NOT_LIVE_AUTHORIZATION"}:
            completed_code = 0
        else:
            completed_code = completed.returncode
        exit_code = max(exit_code, completed_code)
        research_status = validation_status
    else:
        research_status = development_status
    # A preregistered research FAIL is terminal evidence, not an operational
    # scheduler error. Integrity failures remain nonzero and retry-pending.
    if research_status == "FAIL":
        exit_code = 0
    return exit_code, research_status


def run(interval_seconds: int) -> None:
    if interval_seconds < 300:
        raise SystemExit("interval-seconds must be at least 300")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("liquidation overlay scheduler is already running")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    started = _now()
    last_exit = None
    research_status = "UNKNOWN"
    while not STOP_REQUESTED:
        cycle_started = _now()
        _atomic(
            {
                "schema_version": 1,
                "state": "UPDATING",
                "pid": os.getpid(),
                "scheduler_started_at": started.isoformat(),
                "heartbeat_at": cycle_started.isoformat(),
                "interval_seconds": interval_seconds,
                "last_exit_code": last_exit,
                "research_status": research_status,
            }
        )
        last_exit, research_status = _cycle()
        finished = _now()
        next_run = finished + timedelta(seconds=interval_seconds)
        _atomic(
            {
                "schema_version": 1,
                "state": "SLEEPING" if last_exit == 0 else "RETRY_PENDING",
                "pid": os.getpid(),
                "scheduler_started_at": started.isoformat(),
                "heartbeat_at": finished.isoformat(),
                "last_finished_at": finished.isoformat(),
                "next_run_at": next_run.isoformat(),
                "interval_seconds": interval_seconds,
                "last_exit_code": last_exit,
                "research_status": research_status,
                "validation_lock_present": VALIDATION_LOCK.is_file(),
            }
        )
        deadline = time.monotonic() + interval_seconds
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(60, max(0.1, deadline - time.monotonic())))
    _atomic(
        {
            "schema_version": 1,
            "state": "STOPPED",
            "pid": os.getpid(),
            "heartbeat_at": _now().isoformat(),
            "interval_seconds": interval_seconds,
            "last_exit_code": last_exit,
            "research_status": research_status,
        }
    )


def status_payload() -> dict:
    payload = _read() or {"state": "MISSING", "pid": -1}
    pid = int(payload.get("pid") or -1)
    running = _running(pid)
    try:
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"])).astimezone(timezone.utc)
        age = (_now() - heartbeat).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    fresh = age is not None and -60 <= age <= int(payload.get("interval_seconds") or 0) + 1_800
    if running and fresh and payload.get("state") in {"UPDATING", "SLEEPING"}:
        health = "RUNNING_HEALTHY"
    elif running and fresh and payload.get("state") == "RETRY_PENDING":
        health = "RUNNING_RETRY_PENDING"
    elif not running:
        health = "PROCESS_DOWN"
    else:
        health = "HEARTBEAT_OR_STATE_INVALID"
    return {**payload, "running": running, "heartbeat_age_seconds": age, "health": health}


def start(interval_seconds: int) -> None:
    existing = _read()
    if existing and _running(int(existing.get("pid") or -1)):
        print(json.dumps(status_payload(), indent=2))
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("ab", buffering=0) as handle:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run", "--interval-seconds", str(interval_seconds)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(1)
    if not _running(process.pid):
        raise SystemExit("liquidation overlay scheduler failed to stay running")
    print(json.dumps(status_payload(), indent=2))


def stop() -> None:
    payload = _read()
    if not payload or not _running(int(payload.get("pid") or -1)):
        raise SystemExit("liquidation overlay scheduler is not running")
    os.kill(int(payload["pid"]), signal.SIGTERM)
    print(json.dumps({"status": "STOP_REQUESTED", "pid": payload["pid"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "run", "status", "stop"))
    parser.add_argument("--interval-seconds", type=int, default=21_600)
    args = parser.parse_args()
    if args.action == "start":
        start(args.interval_seconds)
    elif args.action == "run":
        run(args.interval_seconds)
    elif args.action == "status":
        print(json.dumps(status_payload(), indent=2))
    else:
        stop()


if __name__ == "__main__":
    main()
