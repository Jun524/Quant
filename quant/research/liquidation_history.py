"""Fail-closed coverage accounting for forward liquidation telemetry chunks."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path


def merged_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total = 0.0
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += (end - start).total_seconds()
            start, end = next_start, next_end
    return total + (end - start).total_seconds()


def assess_liquidation_history(
    root: Path,
    *,
    policy: dict,
    expected_protocol: dict[str, str],
    now: datetime | None = None,
) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    eligible_start = datetime.fromisoformat(
        str(policy["eligible_start_utc"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    intervals_by_day: dict[date, list[tuple[datetime, datetime]]] = defaultdict(list)
    invalid: list[dict] = []
    valid_chunks = liquidation_rows = mark_rows = open_interest_rows = 0
    for path in sorted(root.glob("*/manifest.json")) if root.is_dir() else ():
        failures: list[str] = []
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            result = manifest["result"]
            started = datetime.fromisoformat(str(result["started_at"])).astimezone(timezone.utc)
            ended = datetime.fromisoformat(str(result["ended_at"])).astimezone(timezone.utc)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            invalid.append({"path": str(path), "failures": ["manifest_unreadable"]})
            continue
        if manifest.get("status") != "PASS":
            failures.append("status_not_pass")
        if manifest.get("purpose") != "forward_risk_overlay_telemetry_not_promotion_evidence":
            failures.append("purpose_mismatch")
        if manifest.get("feed_semantics") != "censored_latest_snapshot_not_total_liquidation_volume":
            failures.append("feed_semantics_mismatch")
        if manifest.get("protocol_sha256") != expected_protocol:
            failures.append("protocol_stale")
        if started < eligible_start:
            failures.append("starts_before_eligible_time")
        if ended <= started or ended > now + timedelta(minutes=5):
            failures.append("time_interval_invalid")
        for item in manifest.get("files") or []:
            source = path.parent / str(item.get("path") or "")
            try:
                size = int(item["bytes"])
                digest = str(item["sha256"])
            except (KeyError, TypeError, ValueError):
                failures.append("file_metadata_invalid")
                continue
            if (
                not source.is_file()
                or source.stat().st_size != size
                or sha256(source.read_bytes()).hexdigest() != digest
            ):
                failures.append(f"file_mismatch:{source.name}")
        if failures:
            invalid.append({"path": str(path), "failures": sorted(set(failures))})
            continue
        valid_chunks += 1
        rows = result.get("rows") or {}
        liquidation_rows += int(rows.get("liquidation_rows") or 0)
        mark_rows += int(rows.get("mark_rows") or 0)
        open_interest_rows += int((result.get("open_interest") or {}).get("rows") or 0)
        cursor = started.date()
        while cursor <= ended.date():
            day_start = datetime.combine(cursor, time.min, timezone.utc)
            day_end = day_start + timedelta(days=1)
            clipped_start = max(started, day_start)
            clipped_end = min(ended, day_end)
            if clipped_end > clipped_start:
                intervals_by_day[cursor].append((clipped_start, clipped_end))
            cursor += timedelta(days=1)

    coverage_threshold = float(policy["development_gate"]["minimum_stream_coverage_per_day"])
    last_complete_day = now.date() - timedelta(days=1)
    due_days: list[date] = []
    cursor = eligible_start.date()
    while cursor <= last_complete_day:
        due_days.append(cursor)
        cursor += timedelta(days=1)
    daily = []
    complete_days: list[str] = []
    for day in due_days:
        seconds = merged_seconds(intervals_by_day.get(day, []))
        ratio = min(1.0, seconds / 86_400)
        complete = ratio >= coverage_threshold
        daily.append({"date": day.isoformat(), "covered_seconds": seconds, "coverage_ratio": ratio, "complete": complete})
        if complete:
            complete_days.append(day.isoformat())
    required = int(policy["development_gate"]["complete_utc_days"])
    if invalid:
        status = "DATA_INTEGRITY_ATTENTION"
    elif valid_chunks == 0:
        status = "WAITING_FIRST_ELIGIBLE_CHUNK"
    elif len(complete_days) < required:
        status = "ACCUMULATING_DEVELOPMENT"
    else:
        status = "DEVELOPMENT_TELEMETRY_READY_ANALYSIS_NOT_RUN"
    return {
        "schema_version": 1,
        "observed_at": now.isoformat(),
        "candidate": policy["candidate"],
        "status": status,
        "role": policy["role"],
        "feed_complete": False,
        "eligible_start": eligible_start.isoformat(),
        "valid_current_protocol_chunks": valid_chunks,
        "invalid_chunks": invalid,
        "complete_utc_days": complete_days,
        "complete_utc_days_count": len(complete_days),
        "development_days_required": required,
        "development_days_remaining": max(0, required - len(complete_days)),
        "locked_validation_opened": False,
        "observed_liquidation_snapshot_rows": liquidation_rows,
        "mark_price_rows": mark_rows,
        "open_interest_rows": open_interest_rows,
        "daily_coverage": daily,
        "performance_calculated": False,
    }
