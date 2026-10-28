#!/usr/bin/env python3
"""Start, inspect, or gracefully stop the detached Alpha #7 campaign."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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

from quant.research.campaign import latest_microstructure_run_audit, microstructure_campaign_preflight
from quant.monitoring.sleep_guard import inspect_caffeinate_guard


DEFAULT_ROOT = Path("research/microstructure")


def state_path(root: Path) -> Path:
    return root / "campaign-state.json"


def load_state(root: Path) -> dict | None:
    path = state_path(root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def process_running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and "run_microstructure_campaign.py" in completed.stdout


def collector_processes(supervisor_pid: int | None) -> list[dict]:
    """Return live collector children without trusting the campaign state file."""
    if not supervisor_pid or supervisor_pid < 1:
        return []
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    collectors = []
    for raw_line in completed.stdout.splitlines():
        fields = raw_line.strip().split(maxsplit=3)
        if len(fields) != 4:
            continue
        try:
            pid, parent_pid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if parent_pid != supervisor_pid or "collect_microstructure.py" not in fields[3]:
            continue
        collectors.append({"pid": pid, "elapsed": fields[2], "command": fields[3]})
    return collectors


def derive_status_health(
    payload: dict,
    *,
    supervisor_running: bool,
    collectors: list[dict],
    latest_run_audit: dict | None = None,
    collector_output_age_seconds: float | None = None,
    now: datetime | None = None,
) -> dict:
    """Derive an operational health state from process and state-file evidence."""
    now = now or datetime.now(timezone.utc)
    started_at = payload.get("current_chunk_started_at")
    chunk_age_seconds = None
    if started_at:
        try:
            parsed = datetime.fromisoformat(started_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            chunk_age_seconds = max(0.0, (now - parsed).total_seconds())
        except (TypeError, ValueError):
            pass

    state_status = payload.get("status", "UNKNOWN")
    chunk_seconds = max(0, int(payload.get("chunk_seconds") or 0))
    stale_after_seconds = chunk_seconds + max(300, int(chunk_seconds * 0.10)) if chunk_seconds else None
    terminal = state_status in {"FINISHED", "STOPPED", "PREFLIGHT_FAILED"}

    if not supervisor_running:
        health = "TERMINAL" if terminal else "SUPERVISOR_MISSING"
    elif terminal:
        health = "STATE_PROCESS_MISMATCH"
    elif collectors and chunk_age_seconds is None:
        health = "STATE_LAG"
    elif (
        collectors
        and stale_after_seconds is not None
        and chunk_age_seconds is not None
        and chunk_age_seconds > stale_after_seconds
        and collector_output_age_seconds is not None
        and collector_output_age_seconds <= 120
    ):
        health = "RUNNING_ACTIVE_EXTENDED"
    elif collectors and stale_after_seconds is not None and chunk_age_seconds is not None and chunk_age_seconds > stale_after_seconds:
        health = "RUNNING_CHUNK_STALE"
    elif not collectors and started_at and chunk_age_seconds is not None and chunk_age_seconds <= 30:
        health = "RUNNING_COLLECTOR_STARTING"
    elif not collectors and started_at:
        health = "RUNNING_COLLECTOR_MISSING"
    elif latest_run_audit and latest_run_audit.get("status") == "FAIL":
        health = "LATEST_CHUNK_AUDIT_FAILED"
    elif latest_run_audit and latest_run_audit.get("status") == "QUALITY_WARNING":
        health = "RUNNING_WITH_DATA_WARNING"
    elif collectors:
        health = "RUNNING_HEALTHY"
    else:
        health = "RUNNING_BETWEEN_CHUNKS"

    return {
        "health": health,
        "supervisor_running": supervisor_running,
        "collector_running": bool(collectors),
        "collectors": collectors,
        "current_chunk_age_seconds": round(chunk_age_seconds, 3) if chunk_age_seconds is not None else None,
        "chunk_stale_after_seconds": stale_after_seconds,
        "collector_output_age_seconds": (
            round(collector_output_age_seconds, 3)
            if collector_output_age_seconds is not None
            else None
        ),
    }


def active_collector_output_age(output_root: Path, *, now: datetime | None = None) -> float | None:
    """Return the freshest mtime age among unsealed collector outputs."""
    now = now or datetime.now(timezone.utc)
    mtimes: list[float] = []
    for run_dir in output_root.iterdir() if output_root.is_dir() else ():
        if not run_dir.is_dir() or (run_dir / "manifest.json").exists():
            continue
        for path in run_dir.glob("*.jsonl.gz"):
            try:
                mtimes.append(path.stat().st_mtime)
            except OSError:
                continue
    if not mtimes:
        return None
    return max(0.0, now.timestamp() - max(mtimes))


def campaign_coverage_budget(
    payload: dict,
    *,
    project_root: Path = PROJECT_ROOT,
    now: datetime | None = None,
    required_complete_days: int = 17,
    minimum_coverage_per_day: float = 0.95,
) -> dict:
    """Compute an upper bound on whether the preregistered sample is still reachable."""
    now = now or datetime.now(timezone.utc)
    completed_seconds = 0
    failed_seconds = 0
    log_value = payload.get("campaign_log")
    log_path = project_root / str(log_value) if log_value else None
    if log_path and log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            duration = max(0, int(event.get("duration_seconds") or 0))
            if event.get("event") == "chunk_complete":
                completed_seconds += duration
            elif event.get("event") == "chunk_failed":
                failed_seconds += duration
    try:
        started = datetime.fromisoformat(str(payload.get("started_at")))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if payload.get("deadline_at"):
            deadline = datetime.fromisoformat(str(payload["deadline_at"]))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            deadline = deadline.astimezone(timezone.utc)
        else:
            deadline = started.astimezone(timezone.utc) + timedelta(hours=float(payload.get("hours") or 0))
        remaining_wall_seconds = max(0.0, (deadline - now).total_seconds())
    except (TypeError, ValueError):
        deadline = None
        remaining_wall_seconds = 0.0
    required_seconds = required_complete_days * 86_400 * minimum_coverage_per_day
    maximum_possible = completed_seconds + remaining_wall_seconds
    terminal = str(payload.get("status") or "") in {
        "FINISHED",
        "STOPPED",
        "PREFLIGHT_FAILED",
    }
    return {
        "basis": "upper bound only; complete UTC days are determined from observed feature-second coverage",
        "terminal": terminal,
        "required_complete_days": required_complete_days,
        "minimum_coverage_per_day": minimum_coverage_per_day,
        "required_eligible_seconds": required_seconds,
        "sealed_success_seconds": completed_seconds,
        "excluded_failed_chunk_seconds": failed_seconds,
        "remaining_wall_seconds": round(remaining_wall_seconds, 3),
        "maximum_possible_eligible_seconds": round(maximum_possible, 3),
        "campaign_deadline": deadline.isoformat() if deadline else None,
        # A terminal campaign cannot acquire another complete UTC day.  Do not
        # present a raw-duration upper bound as a future opportunity after the
        # deadline; completion is instead decided by the frozen day-coverage
        # evaluator and, where applicable, its sealed terminal receipt.
        "target_still_reachable": bool(
            not terminal
            and log_path
            and log_path.is_file()
            and maximum_possible >= required_seconds
        ),
    }


def write_launch_state(root: Path, payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = state_path(root)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def start(args) -> None:
    existing = load_state(args.output_root)
    if existing and process_running(existing.get("pid")):
        raise SystemExit(f"campaign already running with pid {existing['pid']}")
    preflight = microstructure_campaign_preflight(
        args.output_root,
        campaign_hours=args.hours,
        minimum_free_after_gib=args.minimum_free_gb,
        disk_path=PROJECT_ROOT,
    )
    if preflight["status"] != "READY":
        print(json.dumps({"status": "NOT_STARTED", "preflight": preflight}, indent=2))
        raise SystemExit(2)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_log = args.output_root / f"campaign-supervisor-{timestamp}.log"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_microstructure_campaign.py"),
        "--hours",
        str(args.hours),
        "--chunk-seconds",
        str(args.chunk_seconds),
        "--symbol",
        args.symbol,
        "--minimum-free-gb",
        str(args.minimum_free_gb),
        "--output-root",
        str(args.output_root),
    ]
    args.output_root.mkdir(parents=True, exist_ok=True)
    with output_log.open("ab", buffering=0) as handle:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    write_launch_state(
        args.output_root,
        {
            "schema_version": 1,
            "status": "LAUNCHING",
            "pid": process.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "hours": args.hours,
            "chunk_seconds": args.chunk_seconds,
            "symbol": args.symbol,
            "minimum_free_gb": args.minimum_free_gb,
            "supervisor_output": str(output_log),
            "preflight": preflight,
        },
    )
    caffeinate_pid = None
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        keeper = subprocess.Popen(
            [caffeinate, "-ims", "-w", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        caffeinate_pid = keeper.pid
    time.sleep(1.0)
    running = process_running(process.pid)
    payload = load_state(args.output_root) or {}
    payload.update({"running": running, "caffeinate_pid": caffeinate_pid, "supervisor_output": str(output_log)})
    print(json.dumps(payload, indent=2))
    if not running:
        raise SystemExit(3)


def status_payload(output_root: Path = DEFAULT_ROOT) -> dict:
    payload = load_state(output_root) or {"status": "NEVER_STARTED", "pid": None}
    supervisor_running = process_running(payload.get("pid"))
    collectors = collector_processes(payload.get("pid")) if supervisor_running else []
    latest_audit = latest_microstructure_run_audit(output_root, project_root=PROJECT_ROOT)
    output_age = active_collector_output_age(output_root)
    payload["running"] = supervisor_running
    payload["operational"] = derive_status_health(
        payload,
        supervisor_running=supervisor_running,
        collectors=collectors,
        latest_run_audit=latest_audit,
        collector_output_age_seconds=output_age,
    )
    payload["sleep_guard"] = inspect_caffeinate_guard(payload.get("pid"))
    if (
        supervisor_running
        and payload["sleep_guard"]["available"]
        and not payload["sleep_guard"]["running"]
        and str(payload["operational"].get("health") or "").startswith("RUNNING_")
    ):
        payload["operational"]["health"] = "RUNNING_SLEEP_GUARD_MISSING"
    payload["latest_completed_run_audit"] = latest_audit
    coverage_budget = campaign_coverage_budget(payload)
    payload["coverage_budget"] = coverage_budget
    if supervisor_running and not coverage_budget["target_still_reachable"]:
        payload["operational"]["health"] = "COLLECTION_TARGET_UNREACHABLE"
    return payload


def status(args) -> None:
    print(json.dumps(status_payload(args.output_root), indent=2))


def stop(args) -> None:
    payload = load_state(args.output_root)
    if not payload or not process_running(payload.get("pid")):
        print(json.dumps({"status": "NOT_RUNNING"}, indent=2))
        return
    os.kill(int(payload["pid"]), signal.SIGTERM)
    payload["stop_signal_sent_at"] = datetime.now(timezone.utc).isoformat()
    payload["status"] = "STOP_REQUESTED_WAITING_FOR_CHUNK"
    write_launch_state(args.output_root, payload)
    print(json.dumps({"status": payload["status"], "pid": payload["pid"], "note": "The current chunk is allowed to finish and seal its manifest."}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--hours", type=int, required=True)
    start_parser.add_argument("--chunk-seconds", type=int, default=3600)
    start_parser.add_argument("--symbol", default="BTCUSDT")
    start_parser.add_argument("--minimum-free-gb", type=float, default=25.0)
    start_parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    start_parser.set_defaults(handler=start)
    for name, handler in (("status", status), ("stop", stop)):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
        subparser.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
