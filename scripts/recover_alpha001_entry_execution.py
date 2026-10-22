#!/usr/bin/env python3
"""Start a new Alpha #1 entry-shadow evidence segment after recorder failure.

This tool never replays the uncovered log interval, changes strategy settings, or
starts an order-capable process.  It records only future initial-entry attempts
from a new immutable log offset and preserves the failed segment in state history.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manage_alpha001_forward_dryrun import (
    BOT_LOG,
    STATE_PATH,
    entry_recorder_process_running,
    process_running,
    protocol_digests,
)


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        raise RuntimeError("Alpha #1 forward state is missing")
    value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Alpha #1 forward state is invalid")
    return value


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT))


def _manifest_is_active(path: Path, *, expected_output: Path, expected_protocol: dict[str, str]) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        value.get("status") == "ACTIVE"
        and value.get("source_path") == _relative(expected_output)
        and value.get("protocol_sha256") == expected_protocol
    )


def _plan(state: dict[str, Any], *, stamp: str, duration_seconds: int) -> dict[str, Any]:
    if not process_running(state.get("pid")):
        raise RuntimeError("Alpha #1 forward dry-run is not running")
    if entry_recorder_process_running(state.get("entry_execution_recorder_pid")):
        raise RuntimeError("an Alpha #1 entry recorder is already running")
    if state.get("protocol_sha256") != protocol_digests():
        raise RuntimeError("active Alpha #1 protocol differs from its launch manifest")
    if not BOT_LOG.is_file():
        raise RuntimeError("Alpha #1 Freqtrade log is missing")
    root = PROJECT_ROOT / "research" / "execution" / "raw"
    output = root / f"alpha001-signal-entry-recovery-{stamp}.jsonl"
    manifest = output.with_suffix(".manifest.json")
    recorder_log = PROJECT_ROOT / "user_data" / "logs" / f"alpha001-forward-entry-execution-recovery-{stamp}.log"
    if output.exists() or manifest.exists() or recorder_log.exists():
        raise RuntimeError("recovery output collision")
    return {
        "output": output,
        "manifest": manifest,
        "recorder_log": recorder_log,
        "start_offset": BOT_LOG.stat().st_size,
        "duration_seconds": duration_seconds,
        "previous": {
            "pid": state.get("entry_execution_recorder_pid"),
            "output": state.get("entry_execution_output"),
            "manifest": state.get("entry_execution_manifest"),
            "log": state.get("entry_execution_recorder_log"),
        },
    }


def recover(args: argparse.Namespace) -> int:
    state = _load_state()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = _plan(state, stamp=stamp, duration_seconds=args.duration_seconds)
    printable = {
        "status": "RECOVERY_PLAN" if args.dry_run else "RECOVERY_STARTING",
        "recovers_from_log_offset": plan["start_offset"],
        "output": _relative(plan["output"]),
        "manifest": _relative(plan["manifest"]),
        "recorder_log": _relative(plan["recorder_log"]),
        "previous_segment": plan["previous"],
        "strategy_or_order_settings_changed": False,
        "uncovered_interval_replayed": False,
    }
    if args.dry_run:
        print(json.dumps(printable, indent=2))
        return 0

    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "record_alpha001_entry_execution.py"),
        "--log-path", str(BOT_LOG),
        "--output", str(plan["output"]),
        "--manifest", str(plan["manifest"]),
        "--start-offset", str(plan["start_offset"]),
        "--duration-seconds", str(plan["duration_seconds"]),
    ]
    plan["recorder_log"].parent.mkdir(parents=True, exist_ok=True)
    with plan["recorder_log"].open("ab", buffering=0) as handle:
        recorder = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(args.startup_seconds)
    protocol = protocol_digests()
    active = entry_recorder_process_running(recorder.pid) and _manifest_is_active(
        plan["manifest"], expected_output=plan["output"], expected_protocol=protocol
    )
    if not active:
        printable.update({"status": "RECOVERY_FAILED", "pid": recorder.pid})
        print(json.dumps(printable, indent=2))
        return 3

    history = state.get("entry_execution_recovery_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "previous_segment": plan["previous"],
            "new_segment": {
                "pid": recorder.pid,
                "output": _relative(plan["output"]),
                "manifest": _relative(plan["manifest"]),
                "log": _relative(plan["recorder_log"]),
                "start_offset": plan["start_offset"],
            },
            "uncovered_interval_replayed": False,
        }
    )
    state.update(
        {
            "entry_execution_recorder_pid": recorder.pid,
            "entry_execution_output": _relative(plan["output"]),
            "entry_execution_manifest": _relative(plan["manifest"]),
            "entry_execution_recorder_log": _relative(plan["recorder_log"]),
            "entry_execution_recovery_history": history,
        }
    )
    _atomic_write(STATE_PATH, state)
    printable.update({"status": "RECOVERY_RUNNING", "pid": recorder.pid})
    print(json.dumps(printable, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=366 * 24 * 60 * 60)
    parser.add_argument("--startup-seconds", type=float, default=3.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.duration_seconds < 1 or args.startup_seconds <= 0:
        parser.error("duration and startup values must be positive")
    try:
        raise SystemExit(recover(args))
    except RuntimeError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
