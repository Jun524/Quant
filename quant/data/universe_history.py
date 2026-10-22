"""Coverage gate for append-only full-market point-in-time snapshots."""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from quant.data.snapshots import snapshot_manifest_path, validate_sealed_snapshot


def assess_universe_history(
    directory: Path,
    *,
    as_of: datetime | None = None,
    required_consecutive_days: int = 180,
    maximum_latest_age_days: int = 1,
) -> dict:
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if required_consecutive_days < 1 or maximum_latest_age_days < 0:
        raise ValueError("coverage requirements must be non-negative")
    batches: list[dict] = []
    for path in sorted(directory.glob("universe-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError(f"unsupported universe snapshot schema: {path}")
        observed_at = datetime.fromisoformat(str(payload["observed_at"])).astimezone(timezone.utc)
        if observed_at > as_of:
            continue
        metadata = payload.get("metadata") or {}
        instruments = payload.get("instruments") or []
        batches.append(
            {
                "path": str(path),
                "observed_at": observed_at,
                "date": observed_at.date(),
                "full_market": bool(metadata.get("all_usdt_perpetual")),
                "instruments": len(instruments),
                "eligible": sum(bool(item.get("eligible")) for item in instruments),
                "depth_collected": sum(bool(item.get("depth_collected")) for item in instruments),
            }
        )
    full = [item for item in batches if item["full_market"]]
    latest_by_date: dict = {}
    for item in full:
        current = latest_by_date.get(item["date"])
        if current is None or item["observed_at"] > current["observed_at"]:
            latest_by_date[item["date"]] = item
    dates = sorted(latest_by_date)
    consecutive = 0
    if dates:
        consecutive = 1
        for previous, current in zip(reversed(dates[:-1]), reversed(dates[1:])):
            if (current - previous).days != 1:
                break
            consecutive += 1
    gaps = [(current - previous).days for previous, current in zip(dates, dates[1:])]
    latest = latest_by_date[dates[-1]] if dates else None
    latest_age_days = (as_of.date() - dates[-1]).days if dates else None
    checks = {
        "consecutive_daily_full_market_snapshots": consecutive >= required_consecutive_days,
        "latest_snapshot_fresh": latest_age_days is not None and latest_age_days <= maximum_latest_age_days,
        "latest_snapshot_has_instruments": latest is not None and int(latest["instruments"]) > 0,
        "latest_snapshot_has_depth_sample": latest is not None and int(latest["depth_collected"]) > 0,
    }
    return {
        "status": "READY" if all(checks.values()) else "ACCUMULATING",
        "as_of": as_of.isoformat(),
        "all_batches": len(batches),
        "full_market_batches": len(full),
        "unique_full_market_days": len(dates),
        "coverage_start": dates[0].isoformat() if dates else None,
        "coverage_end": dates[-1].isoformat() if dates else None,
        "consecutive_days_ending_latest": consecutive,
        "required_consecutive_days": required_consecutive_days,
        "maximum_gap_days": max(gaps, default=None),
        "latest_age_days": latest_age_days,
        "latest": (
            {
                key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in latest.items()
                if key != "date"
            }
            if latest
            else None
        ),
        "checks": checks,
        "warning": "READY only proves point-in-time universe coverage; it does not prove any strategy has edge.",
    }


def _date_range(start: date, end_inclusive: date) -> list[date]:
    if end_inclusive < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end_inclusive - start).days + 1)]


def assess_forward_universe_history(
    directory: Path,
    *,
    expected_protocol: dict[str, str],
    policy: dict,
    as_of: datetime | None = None,
) -> dict:
    """Validate only fixed-window, sealed, current-protocol daily observations."""
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    eligible_start = date.fromisoformat(str(policy["eligible_start_date"]))
    sampling = policy["sampling"]
    readiness = policy["readiness"]
    start_time = datetime_time.fromisoformat(str(sampling["start_utc"]))
    window_seconds = int(sampling["window_seconds"])
    today_window_end = datetime.combine(as_of.date(), start_time, tzinfo=timezone.utc) + timedelta(
        seconds=window_seconds
    )
    last_due = as_of.date() if as_of >= today_window_end else as_of.date() - timedelta(days=1)
    due_dates = _date_range(eligible_start, last_due)

    current: list[dict] = []
    legacy_days: list[str] = []
    diagnostic_days: list[str] = []
    invalid: list[dict[str, str]] = []
    for path in sorted(directory.glob("universe-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            observed = datetime.fromisoformat(str(payload["observed_at"])).astimezone(timezone.utc)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError) as error:
            invalid.append({"path": str(path), "error": f"raw snapshot cannot be parsed: {error}"})
            continue
        if observed.date() < eligible_start:
            continue
        manifest_path = snapshot_manifest_path(path)
        try:
            manifest_header = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            invalid.append({"path": str(path), "error": f"current-era manifest missing/invalid: {error}"})
            continue
        if manifest_header.get("evidence_role") == "diagnostic":
            diagnostic_days.append(observed.date().isoformat())
            continue
        if manifest_header.get("evidence_role") != "daily_forward_universe":
            invalid.append({"path": str(path), "error": "unknown universe evidence role"})
            continue
        if manifest_header.get("protocol_sha256") != expected_protocol:
            legacy_days.append(observed.date().isoformat())
            continue
        try:
            sealed = validate_sealed_snapshot(path, expected_protocol=expected_protocol)
            manifest = sealed["manifest"]
            scheduled = date.fromisoformat(str(manifest["scheduled_date"]))
            window_start = datetime.combine(scheduled, start_time, tzinfo=timezone.utc)
            collection_started = datetime.fromisoformat(
                str(manifest["collection_started_at"])
            ).astimezone(timezone.utc)
            duration = float(manifest["collection_duration_seconds"])
            if not (window_start <= collection_started < window_start + timedelta(seconds=window_seconds)):
                raise ValueError("collection start is outside the fixed UTC window")
            if duration < 0 or duration > float(sampling["maximum_collection_duration_seconds"]):
                raise ValueError("collection duration exceeds policy")
            if sealed["observed_at"].date() != scheduled:
                raise ValueError("observation and scheduled dates differ")
            metadata = sealed["payload"].get("metadata") or {}
            if (
                metadata.get("all_usdt_perpetual") is not True
                or int(metadata.get("depth_pairs") or -1) != int(sampling["depth_pairs"])
                or metadata.get("evidence_role") != "daily_forward_universe"
            ):
                raise ValueError("snapshot collection scope differs from policy")
            coverage = metadata.get("detailed_coverage") or {}
            ratios = coverage.get("ratios") or {}
            if (
                int(coverage.get("requested") or 0) != int(sampling["depth_pairs"])
                or set(ratios) != {"depth", "candles", "funding", "open_interest"}
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or float(value) < float(sampling["minimum_detailed_coverage_ratio"])
                    or float(value) > 1.0
                    for value in ratios.values()
                )
            ):
                raise ValueError("snapshot detailed endpoint coverage differs from policy")
        except (KeyError, TypeError, ValueError) as error:
            invalid.append({"path": str(path), "error": str(error)})
            continue
        current.append(
            {
                "date": scheduled,
                "observed_at": sealed["observed_at"],
                "path": str(path),
                "manifest_path": str(manifest_path),
                "manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
                "manifest": manifest,
                "instruments": int(manifest["instrument_count"]),
                "eligible": int(manifest["eligible_count"]),
                "depth_collected": int(manifest["depth_collected_count"]),
            }
        )

    by_date: dict[date, list[dict]] = {}
    for item in current:
        by_date.setdefault(item["date"], []).append(item)
    duplicates = sorted(day.isoformat() for day, items in by_date.items() if len(items) != 1)
    valid = [items[0] for day, items in sorted(by_date.items()) if len(items) == 1]
    chain_failures: list[str] = []
    previous: dict | None = None
    for item in valid:
        manifest = item["manifest"]
        if previous is None:
            if manifest.get("previous_manifest_path") is not None or manifest.get(
                "previous_manifest_sha256"
            ) is not None:
                chain_failures.append(f"{item['date']}: first current-protocol manifest has a predecessor")
        else:
            expected_path = str(Path(previous["manifest_path"]).relative_to(directory))
            if (
                manifest.get("previous_manifest_path") != expected_path
                or manifest.get("previous_manifest_sha256") != previous["manifest_sha256"]
            ):
                chain_failures.append(f"{item['date']}: predecessor manifest binding mismatch")
        previous = item

    valid_dates = {item["date"] for item in valid}
    missing = [day.isoformat() for day in due_dates if day not in valid_dates]
    consecutive = 0
    if valid:
        consecutive = 1
        ordered_dates = [item["date"] for item in valid]
        for previous_day, current_day in zip(reversed(ordered_dates[:-1]), reversed(ordered_dates[1:])):
            if current_day - previous_day != timedelta(days=1):
                break
            consecutive += 1
    latest = valid[-1] if valid else None
    latest_age = (as_of.date() - latest["date"]).days if latest else None
    required_days = int(readiness["required_consecutive_days"])
    fresh = latest_age is not None and latest_age <= int(readiness["maximum_latest_age_days"])
    if invalid or duplicates or chain_failures:
        status = "DATA_INTEGRITY_FAIL"
    elif not due_dates:
        status = "WAITING_FIRST_WINDOW"
    elif consecutive >= required_days and fresh:
        status = "READY"
    elif missing:
        status = "ACCUMULATING_WITH_GAPS"
    else:
        status = "ACCUMULATING"
    return {
        "schema_version": 1,
        "status": status,
        "as_of": as_of.isoformat(),
        "eligible_start": eligible_start.isoformat(),
        "current_protocol_days": len(valid),
        "valid_days": [item["date"].isoformat() for item in valid],
        "legacy_protocol_days": sorted(set(legacy_days)),
        "diagnostic_days": sorted(set(diagnostic_days)),
        "missing_due_days": missing,
        "duplicate_days": duplicates,
        "invalid_snapshots": invalid,
        "chain_failures": chain_failures,
        "consecutive_days_ending_latest": consecutive,
        "required_consecutive_days": required_days,
        "latest_age_days": latest_age,
        "latest": (
            {
                "date": latest["date"].isoformat(),
                "observed_at": latest["observed_at"].isoformat(),
                "path": latest["path"],
                "instruments": latest["instruments"],
                "eligible": latest["eligible"],
                "depth_collected": latest["depth_collected"],
            }
            if latest
            else None
        ),
        "protocol_sha256": expected_protocol,
        "warning": "READY proves point-in-time universe coverage only; it does not prove strategy edge.",
    }
