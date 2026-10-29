#!/usr/bin/env python3
"""Start, inspect, and stop the detached liquidation telemetry campaign."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
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

from quant.monitoring.sleep_guard import inspect_caffeinate_guard
from quant.research.liquidation_history import assess_liquidation_history
from quant.research.liquidation_telemetry import protocol_digests


DEFAULT_ROOT = PROJECT_ROOT / "research" / "liquidation_telemetry"
POLICY_PATH = PROJECT_ROOT / "research" / "liquidation-telemetry-policy.json"


def _state(root: Path) -> dict | None:
    try:
        return json.loads((root / "campaign-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(root: Path, payload: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "campaign-state.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _running(pid: int | None) -> bool:
    if not pid or pid < 1:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and "run_liquidation_telemetry_campaign.py" in result.stdout


def _latest_audit(root: Path) -> dict | None:
    manifests = sorted(path for path in root.glob("*/manifest.json") if path.is_file())
    if not manifests:
        return None
    path = manifests[-1]
    failures: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "FAIL", "path": str(path), "failures": ["manifest_unreadable"]}
    if payload.get("status") != "PASS":
        failures.append("collector_status_not_pass")
    if payload.get("protocol_sha256") != protocol_digests(PROJECT_ROOT):
        failures.append("protocol_stale")
    if payload.get("feed_semantics") != "censored_latest_snapshot_not_total_liquidation_volume":
        failures.append("feed_semantics_missing")
    for item in payload.get("files") or []:
        source = path.parent / str(item.get("path") or "")
        if (
            not source.is_file()
            or source.stat().st_size != int(item.get("bytes") or -1)
            or sha256(source.read_bytes()).hexdigest() != item.get("sha256")
        ):
            failures.append(f"file_mismatch:{source.name}")
    return {"status": "FAIL" if failures else "PASS", "path": str(path), "failures": failures}


def _historical_integrity(root: Path) -> dict:
    """Replay all sealed telemetry coverage for status, not just the newest run."""
    try:
        policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        return assess_liquidation_history(
            root,
            policy=policy,
            expected_protocol=protocol_digests(PROJECT_ROOT),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return {
            "status": "DATA_INTEGRITY_ATTENTION",
            "failure": f"historical_integrity_replay_failed:{type(error).__name__}",
        }


def status_payload(root: Path = DEFAULT_ROOT) -> dict:
    payload = _state(root) or {"status": "NEVER_STARTED", "pid": None}
    running = _running(payload.get("pid"))
    state_status = str(payload.get("status") or "UNKNOWN")
    latest = _latest_audit(root)
    historical_integrity = _historical_integrity(root)
    if not running:
        health = "TERMINAL" if state_status in {"FINISHED", "STOPPED"} else "PROCESS_DOWN"
    elif state_status == "WAITING_ELIGIBLE_START":
        health = "WAITING_ELIGIBLE_START"
    elif state_status == "RUNNING":
        started = payload.get("current_chunk_started_at")
        if started:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(started))).total_seconds()
            stale = age > int(payload.get("chunk_seconds") or 0) + 900
        else:
            stale = False
        if stale:
            health = "CHUNK_STALE"
        elif int(payload.get("failed_chunks") or 0) > 0:
            health = "RUNNING_WITH_DATA_GAPS"
        else:
            health = "RUNNING_HEALTHY"
    else:
        health = "STATE_INVALID"
    if running and latest and latest["status"] != "PASS":
        health = "LATEST_CHUNK_AUDIT_FAILED"
    elif running and (
        historical_integrity.get("status") == "DATA_INTEGRITY_ATTENTION"
        or bool(
            (historical_integrity.get("mark_price_continuity") or {}).get(
                "gap_failures"
            )
        )
    ):
        health = "RUNNING_WITH_DATA_GAPS"
    sleep_guard = inspect_caffeinate_guard(payload.get("pid"))
    if running and sleep_guard.get("available") and not sleep_guard.get("running"):
        health = "SLEEP_GUARD_MISSING"
    return {
        **payload,
        "running": running,
        "health": health,
        "latest_manifest_audit": latest,
        "historical_integrity": historical_integrity,
        "sleep_guard": sleep_guard,
    }


def start(args) -> None:
    existing = _state(args.output_root)
    if existing and _running(existing.get("pid")):
        print(json.dumps(status_payload(args.output_root), indent=2))
        return
    args.output_root.mkdir(parents=True, exist_ok=True)
    log_path = args.output_root / f"supervisor-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log"
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_liquidation_telemetry_campaign.py"),
        "--hours",
        str(args.hours),
        "--chunk-seconds",
        str(args.chunk_seconds),
        "--minimum-free-gb",
        str(args.minimum_free_gb),
        "--output-root",
        str(args.output_root),
    ]
    with log_path.open("ab", buffering=0) as handle:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    caffeinate_pid = None
    if shutil.which("caffeinate"):
        keeper = subprocess.Popen(
            ["caffeinate", "-ims", "-w", str(process.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        caffeinate_pid = keeper.pid
    time.sleep(1)
    payload = _state(args.output_root) or {}
    payload.update(
        {"pid": process.pid, "caffeinate_pid": caffeinate_pid, "supervisor_log": str(log_path)}
    )
    _write_state(args.output_root, payload)
    report = status_payload(args.output_root)
    print(json.dumps(report, indent=2))
    if not report["running"]:
        raise SystemExit(3)


def stop(args) -> None:
    payload = _state(args.output_root)
    if not payload or not _running(payload.get("pid")):
        print(json.dumps({"status": "NOT_RUNNING"}))
        return
    os.kill(int(payload["pid"]), signal.SIGTERM)
    print(json.dumps({"status": "STOP_REQUESTED", "pid": payload["pid"]}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--hours", type=int, default=2208)
    start_parser.add_argument("--chunk-seconds", type=int, default=3600)
    start_parser.add_argument("--minimum-free-gb", type=float, default=10.0)
    start_parser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
    start_parser.set_defaults(handler=start)
    for name, handler in (("status", lambda args: print(json.dumps(status_payload(args.output_root), indent=2))), ("stop", stop)):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--output-root", type=Path, default=DEFAULT_ROOT)
        subparser.set_defaults(handler=handler)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
