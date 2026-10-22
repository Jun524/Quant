#!/usr/bin/env python3
"""Collect one sealed full-market Binance USDT-M universe snapshot per UTC day."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timedelta, timezone
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
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document, protocol_digests
from quant.monitoring.sleep_guard import inspect_caffeinate_guard


STATE_DIR = PROJECT_ROOT / "research" / "universe_forward"
LOCK_PATH = STATE_DIR / "scheduler.lock"
STATUS_PATH = STATE_DIR / "scheduler-status.json"
LOG_PATH = PROJECT_ROOT / "user_data" / "logs" / "universe-forward-scheduler.log"
SNAPSHOT_DIR = PROJECT_ROOT / "research" / "universe_snapshots"
STOP_REQUESTED = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _atomic_status(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_name(f".{STATUS_PATH.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, STATUS_PATH)


def _read_status() -> dict | None:
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "run_universe_scheduler.py run" in completed.stdout


def _request_stop(_signum: int, _frame: object) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def _window(policy: dict, day: date) -> tuple[datetime, datetime]:
    start_time = datetime_time.fromisoformat(str(policy["sampling"]["start_utc"]))
    start = datetime.combine(day, start_time, tzinfo=timezone.utc)
    return start, start + timedelta(seconds=int(policy["sampling"]["window_seconds"]))


def _next_window(policy: dict, now: datetime) -> tuple[date, datetime, datetime]:
    eligible = date.fromisoformat(str(policy["eligible_start_date"]))
    candidate = max(now.date(), eligible)
    start, end = _window(policy, candidate)
    if now >= end:
        candidate += timedelta(days=1)
        start, end = _window(policy, candidate)
    return candidate, start, end


def _day_is_current(day: date, policy: dict) -> bool:
    report = assess_forward_universe_history(
        SNAPSHOT_DIR,
        expected_protocol=protocol_digests(),
        policy=policy,
    )
    return day.isoformat() in report["valid_days"]


def run_scheduler(poll_seconds: int) -> None:
    if poll_seconds < 30 or poll_seconds > 300:
        raise SystemExit("poll-seconds must be between 30 and 300")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("universe forward scheduler is already running")
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    policy = policy_document()
    started_at = _now()
    last_exit_code: int | None = None
    last_finished_at: str | None = None
    while not STOP_REQUESTED:
        now = _now()
        day, window_start, window_end = _next_window(policy, now)
        if now < window_start:
            state = "WAITING_WINDOW"
            next_run = window_start
        elif _day_is_current(day, policy):
            state = "TODAY_SEALED"
            next_day = day + timedelta(days=1)
            next_run, _ = _window(policy, next_day)
        else:
            state = "COLLECTING"
            next_run = now
            _atomic_status(
                {
                    "schema_version": 1,
                    "state": state,
                    "pid": os.getpid(),
                    "scheduler_started_at": started_at.isoformat(),
                    "heartbeat_at": now.isoformat(),
                    "scheduled_date": day.isoformat(),
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "last_finished_at": last_finished_at,
                    "last_exit_code": last_exit_code,
                    "poll_seconds": poll_seconds,
                }
            )
            command = [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "collect_universe_snapshot.py"),
                "--output-dir",
                str(SNAPSHOT_DIR),
                "--data-dir",
                str(PROJECT_ROOT / "user_data" / "data" / "binance"),
                "--all-usdt-perpetual",
                "--depth-pairs",
                str(policy["sampling"]["depth_pairs"]),
                "--evidence-role",
                "daily_forward_universe",
                "--scheduled-date",
                day.isoformat(),
            ]
            completed = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
            last_exit_code = completed.returncode
            last_finished_at = _now().isoformat()
            state = "TODAY_SEALED" if last_exit_code == 0 else "RETRY_PENDING"
            next_run = (
                _window(policy, day + timedelta(days=1))[0]
                if last_exit_code == 0
                else min(_now() + timedelta(seconds=poll_seconds), window_end)
            )
        _atomic_status(
            {
                "schema_version": 1,
                "state": state,
                "pid": os.getpid(),
                "scheduler_started_at": started_at.isoformat(),
                "heartbeat_at": _now().isoformat(),
                "scheduled_date": day.isoformat(),
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "last_finished_at": last_finished_at,
                "last_exit_code": last_exit_code,
                "next_run_at": next_run.isoformat(),
                "poll_seconds": poll_seconds,
            }
        )
        deadline = min(time.monotonic() + poll_seconds, time.monotonic() + max(0.0, (next_run - _now()).total_seconds()))
        while not STOP_REQUESTED and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    _atomic_status(
        {
            "schema_version": 1,
            "state": "STOPPED",
            "pid": os.getpid(),
            "scheduler_started_at": started_at.isoformat(),
            "heartbeat_at": _now().isoformat(),
            "last_finished_at": last_finished_at,
            "last_exit_code": last_exit_code,
            "poll_seconds": poll_seconds,
        }
    )


def start_scheduler(poll_seconds: int) -> None:
    existing = _read_status()
    if existing and _process_alive(int(existing.get("pid") or -1)):
        print(f"Universe forward scheduler already running (pid={existing['pid']})")
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "run",
                "--poll-seconds",
                str(poll_seconds),
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    if shutil.which("caffeinate"):
        subprocess.Popen(
            ["caffeinate", "-ims", "-w", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(1.0)
    if not _process_alive(process.pid):
        raise SystemExit("universe forward scheduler failed to stay running")
    print(f"Universe forward scheduler started (pid={process.pid})")


def scheduler_status_payload() -> dict:
    payload = _read_status()
    if payload is None:
        return {"health": "NOT_STARTED", "running": False}
    pid = int(payload.get("pid") or -1)
    running = _process_alive(pid)
    try:
        heartbeat = datetime.fromisoformat(str(payload["heartbeat_at"])).astimezone(timezone.utc)
        age = (_now() - heartbeat).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = None
    state = str(payload.get("state") or "UNKNOWN")
    freshness_limit = max(int(payload.get("poll_seconds") or 300) + 90, 390)
    if state == "COLLECTING":
        freshness_limit = max(
            freshness_limit,
            int(policy_document()["sampling"]["maximum_collection_duration_seconds"]) + 120,
        )
    fresh = age is not None and -60 <= age <= freshness_limit
    guard = inspect_caffeinate_guard(watched_pid=pid)
    if running and fresh and guard.get("running") and state in {"WAITING_WINDOW", "TODAY_SEALED", "COLLECTING"}:
        health = "RUNNING_HEALTHY"
    elif running and fresh and guard.get("running") and state == "RETRY_PENDING":
        health = "RUNNING_RETRY_PENDING"
    elif running and not guard.get("running"):
        health = "SLEEP_GUARD_MISSING"
    elif running:
        health = "RUNNING_STALE"
    else:
        health = "STOPPED"
    return {
        **payload,
        "health": health,
        "running": running,
        "heartbeat_age_seconds": age,
        "sleep_guard": guard,
    }


def show_status() -> None:
    payload = scheduler_status_payload()
    print(json.dumps(payload, indent=2))
    if payload.get("health") not in {"RUNNING_HEALTHY", "RUNNING_RETRY_PENDING"}:
        raise SystemExit(2)


def stop_scheduler() -> None:
    payload = _read_status()
    if payload is None:
        raise SystemExit("universe scheduler status is absent")
    pid = int(payload.get("pid") or -1)
    if not _process_alive(pid):
        raise SystemExit("universe scheduler is not running")
    os.kill(pid, signal.SIGTERM)
    print(f"Stop requested for universe forward scheduler (pid={pid})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "run", "status", "stop"))
    parser.add_argument("--poll-seconds", type=int, default=300)
    args = parser.parse_args()
    if args.action == "start":
        start_scheduler(args.poll_seconds)
    elif args.action == "run":
        run_scheduler(args.poll_seconds)
    elif args.action == "status":
        show_status()
    else:
        stop_scheduler()


if __name__ == "__main__":
    main()
