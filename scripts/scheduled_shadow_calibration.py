#!/usr/bin/env python3
"""Run one future public-L2 calibration window and seal only its reports.

This is a credential-free scheduler for the existing periodic calibration
campaign.  It never creates exchange orders: the campaign it launches uses
only public depth and aggregate-trade observations.  It is intentionally kept
outside every frozen strategy protocol, so scheduling a later diagnostic does
not mutate an active forward cohort.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manage_shadow_entry_campaign import status_payload
from quant.monitoring.execution_calibration_stability import (
    summarize_versioned_calibration_receipts,
)


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("not-before must include a timezone")
    return parsed.astimezone(timezone.utc)


def seconds_until(not_before: datetime, *, now: datetime) -> float:
    if now.tzinfo is None or not_before.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return max(0.0, (not_before - now).total_seconds())


def terminal_campaign_failure(status: str) -> bool:
    return status in {"FAILED", "STOPPED", "START_FAILED", "NOT_STARTED"}


def independent_window_not_before(
    not_before: datetime, temporal_independence: dict
) -> bool:
    """Reject a scheduled start that predates the latest required separation."""
    raw = temporal_independence.get("next_eligible_independent_window_start_at")
    if raw is None:
        return True
    if not isinstance(raw, str):
        return False
    return not_before >= parse_utc(raw)


def assert_independent_start_time(not_before: datetime) -> None:
    report = summarize_versioned_calibration_receipts(PROJECT_ROOT / "reports")
    temporal = report.get("temporal_independence")
    if not isinstance(temporal, dict) or not independent_window_not_before(
        not_before, temporal
    ):
        raise SystemExit(
            "scheduled window is earlier than the next eligible independent calibration start"
        )


def start_campaign() -> str:
    started = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "manage_shadow_entry_campaign.py"), "start"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        detail = (started.stderr or started.stdout).strip().splitlines()
        raise RuntimeError("campaign start failed" + (f": {detail[-1]}" if detail else ""))
    try:
        payload = json.loads(started.stdout)
        campaign_id = str(payload["campaign_id"])
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("campaign start returned an invalid status payload") from error
    if not campaign_id:
        raise RuntimeError("campaign start did not return an id")
    return campaign_id


def wait_for_completion(campaign_id: str, *, poll_seconds: float) -> Path:
    while True:
        payload = status_payload()
        if payload.get("campaign_id") != campaign_id:
            raise RuntimeError("campaign state no longer belongs to this scheduled window")
        status = str(payload.get("status") or "")
        if status == "COMPLETE":
            if payload.get("protocol_current") is not True:
                raise RuntimeError("completed campaign protocol is not current")
            manifest = payload.get("manifest")
            if not isinstance(manifest, str) or not manifest:
                raise RuntimeError("completed campaign has no sealed manifest")
            return PROJECT_ROOT / manifest
        if terminal_campaign_failure(status):
            raise RuntimeError(f"campaign reached terminal non-complete status: {status}")
        time.sleep(poll_seconds)


def seal_reports(manifest: Path) -> None:
    for script, arguments in (
        ("calibration_execution_report.py", [str(manifest)]),
        ("execution_calibration_stability.py", []),
    ):
        completed = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / script), *arguments],
            cwd=PROJECT_ROOT,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"{script} failed with exit {completed.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--not-before", required=True, help="ISO-8601 UTC timestamp")
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if args.poll_seconds < 5:
        raise SystemExit("poll-seconds must be at least five")
    not_before = parse_utc(args.not_before)
    assert_independent_start_time(not_before)
    delay = seconds_until(not_before, now=datetime.now(timezone.utc))
    if delay:
        time.sleep(delay)
    # Recheck after a long sleep: another valid campaign could have completed
    # while this scheduler was waiting and moved the separation boundary.
    assert_independent_start_time(not_before)
    # A current manager process is a separate campaign.  Never overwrite its
    # state simply because a scheduler woke up.
    existing = status_payload()
    if existing.get("running"):
        raise SystemExit("a shadow-entry campaign is already running")
    campaign_id = start_campaign()
    manifest = wait_for_completion(campaign_id, poll_seconds=args.poll_seconds)
    seal_reports(manifest)
    print(json.dumps({"status": "COMPLETE", "campaign_id": campaign_id, "manifest": str(manifest.relative_to(PROJECT_ROOT))}))


if __name__ == "__main__":
    main()
