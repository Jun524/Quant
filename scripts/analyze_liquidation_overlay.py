#!/usr/bin/env python3
"""Evaluate only the locked development slice of liquidation-risk telemetry."""

from __future__ import annotations

import gzip
from bisect import bisect_right
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.liquidation_history import assess_liquidation_history
from quant.research.liquidation_overlay import evaluate_overlay
from quant.research.liquidation_telemetry import protocol_digests


ROOT = PROJECT_ROOT / "research" / "liquidation_telemetry"
POLICY_PATH = PROJECT_ROOT / "research" / "liquidation-telemetry-policy.json"


def _valid_sources(
    expected_protocol: dict[str, str], selected_days: set[date]
) -> list[Path]:
    sources: list[Path] = []
    for manifest_path in sorted(ROOT.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "PASS" or manifest.get("protocol_sha256") != expected_protocol:
            continue
        result = manifest.get("result") or {}
        started = datetime.fromisoformat(str(result["started_at"])).astimezone(timezone.utc)
        ended = datetime.fromisoformat(str(result["ended_at"])).astimezone(timezone.utc)
        cursor = started.date()
        intersects_selected_day = False
        while cursor <= ended.date():
            intersects_selected_day = intersects_selected_day or cursor in selected_days
            cursor += timedelta(days=1)
        if not intersects_selected_day:
            continue
        for item in manifest.get("files") or []:
            path = manifest_path.parent / str(item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != int(item["bytes"])
                or sha256(path.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise RuntimeError(f"source binding failed for {path}")
            sources.append(path)
    return sources


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    output: list[tuple[int, int]] = []
    start, end = sorted(intervals)[0]
    for next_start, next_end in sorted(intervals)[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            output.append((start, end))
            start, end = next_start, next_end
    output.append((start, end))
    return output


def _inside(timestamp: int, intervals: list[tuple[int, int]], starts: list[int]) -> bool:
    index = bisect_right(starts, timestamp) - 1
    return index >= 0 and timestamp <= intervals[index][1]


def _stream_selected_records(
    sources: list[Path], selected_days: set[date]
) -> tuple[list[dict], list[dict], list[dict]]:
    liquidations: list[dict] = []
    open_interest: list[dict] = []
    for source in sources:
        with gzip.open(source, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                try:
                    day = datetime.fromtimestamp(
                        int(row["event_time_ms"]) / 1_000, timezone.utc
                    ).date()
                except (KeyError, TypeError, ValueError, OSError):
                    continue
                if day not in selected_days:
                    continue
                if row.get("kind") == "observed_liquidation_snapshot":
                    liquidations.append(row)
                elif row.get("kind") == "open_interest":
                    open_interest.append(row)

    intervals_by_symbol: dict[str, list[tuple[int, int]]] = {}
    for row in liquidations:
        symbol = str(row["symbol"])
        timestamp = int(row["event_time_ms"])
        intervals_by_symbol.setdefault(symbol, []).append((timestamp - 62_000, timestamp + 1_800_000))
        for weeks in range(1, 5):
            baseline = timestamp - weeks * 7 * 86_400_000
            day = datetime.fromtimestamp(baseline / 1_000, timezone.utc).date()
            if day in selected_days:
                intervals_by_symbol[symbol].append((baseline - 2_000, baseline + 1_800_000))
    intervals_by_symbol = {
        symbol: _merge(intervals) for symbol, intervals in intervals_by_symbol.items()
    }
    starts_by_symbol = {
        symbol: [start for start, _ in intervals]
        for symbol, intervals in intervals_by_symbol.items()
    }
    marks: list[dict] = []
    for source in sources:
        with gzip.open(source, "rt", encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("kind") != "mark_price":
                    continue
                symbol = str(row.get("symbol") or "")
                intervals = intervals_by_symbol.get(symbol)
                if not intervals:
                    continue
                timestamp = int(row["event_time_ms"])
                mark_day = datetime.fromtimestamp(timestamp / 1_000, timezone.utc).date()
                if mark_day in selected_days and _inside(
                    timestamp, intervals, starts_by_symbol[symbol]
                ):
                    marks.append(row)
    return liquidations, open_interest, marks


def markdown(report: dict) -> str:
    if report["status"] == "ACCUMULATING":
        return f"""# Liquidation risk overlay

- Status: **ACCUMULATING**
- Complete eligible UTC days: {report['complete_utc_days']} / {report['required_complete_utc_days']}
- Performance calculated: **NO**
- Locked validation opened: **NO**

Thresholds remain sealed. No partial-sample performance is shown.
"""
    metrics = report["development"]
    checks = "\n".join(
        f"- {'PASS' if item['passed'] else 'FAIL'} — `{item['name']}`: {item['actual']}"
        for item in metrics["checks"]
    )
    return f"""# Liquidation risk overlay

- Status: **{report['status']}**
- Complete development UTC days: {report['complete_utc_days']}
- Matched non-overlapping episodes: {metrics['matched_episodes']}
- Triggered median 30m max excursion: {metrics['triggered_median_max_abs_excursion_30m']}
- Matched baseline median: {metrics['baseline_median_max_abs_excursion_30m']}
- Median tail multiple: {metrics['median_tail_multiple']}
- Locked validation opened: **NO**

## Development checks

{checks}

The Binance feed is censored and these values describe observable snapshots,
not total market liquidations. A development PASS requires a separate explicit
lock-opening action and does not authorize trading.
"""


def main() -> None:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    protocol = protocol_digests(PROJECT_ROOT)
    coverage = assess_liquidation_history(
        ROOT, policy=policy, expected_protocol=protocol
    )
    required = int(policy["development_gate"]["complete_utc_days"])
    complete_days = [date.fromisoformat(value) for value in coverage["complete_utc_days"]]
    base = {
        "schema_version": 1,
        "candidate": policy["candidate"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "protocol_sha256": protocol,
        "complete_utc_days": len(complete_days),
        "required_complete_utc_days": required,
        "locked_validation_opened": False,
    }
    if coverage["invalid_chunks"]:
        report = {**base, "status": "DATA_INTEGRITY_FAILURE", "performance_calculated": False, "coverage": coverage}
    elif len(complete_days) < required:
        report = {**base, "status": "ACCUMULATING", "performance_calculated": False}
    else:
        selected = set(sorted(complete_days)[:required])
        sources = _valid_sources(protocol, selected)
        liquidations, open_interest, marks = _stream_selected_records(sources, selected)
        development = evaluate_overlay(
            liquidations,
            open_interest,
            marks,
            policy=policy,
            complete_days=selected,
        )
        report = {
            **base,
            "status": "DEVELOPMENT_PASS_REQUIRES_EXPLICIT_LOCK_OPEN" if development["status"] == "PASS" else "FAIL",
            "performance_calculated": True,
            "selected_development_days": [day.isoformat() for day in sorted(selected)],
            "latest_opened_utc_day": max(selected).isoformat(),
            "source_files": len(sources),
            "development": development,
        }
    output = PROJECT_ROOT / "reports" / "liquidation-overlay-development"
    output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] in {"DATA_INTEGRITY_FAILURE", "FAIL"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
