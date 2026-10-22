#!/usr/bin/env python3
"""Evaluate the sealed Alpha #11 development sample without opening validation."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import sys
from typing import Any, Iterator
from zipfile import ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.quarter_hour_flow import (
    block_bootstrap_loss_probability,
    development_gate,
    reprice_trades,
    simulate_trade_stream,
    summarize_trades,
)
from scripts.download_binance_aggtrades import (
    PROTOCOL_PATHS,
    inspect_archive,
    inspect_funding_file,
    parse_checksum,
    protocol_digests,
)
from scripts.reconcile_alpha011_funding import compare_funding_rows, inspect_monthly_archive


ELIGIBLE_START = date(2026, 10, 9)
DEVELOPMENT_DAYS = 60
MONTHLY_FUNDING_ROOT = Path("research/alpha011/funding-monthly")


def _utc_ms(day: date, value: time) -> int:
    return int(datetime.combine(day, value, tzinfo=timezone.utc).timestamp() * 1000)


def development_boundaries() -> list[int]:
    output = []
    for offset in range(DEVELOPMENT_DAYS):
        day = ELIGIBLE_START + timedelta(days=offset)
        output.extend((_utc_ms(day, time(0, 15)), _utc_ms(day, time(12, 15))))
    return output


def load_and_verify_manifest(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS" or manifest.get("symbol") != "BTCUSDT":
        raise ValueError("Alpha #11 manifest is not a passing BTCUSDT acquisition")
    current_protocol = protocol_digests()
    if manifest.get("protocol_sha256") != current_protocol:
        raise ValueError("Alpha #11 acquisition protocol differs from the current sealed protocol")
    if manifest.get("start") != ELIGIBLE_START.isoformat():
        raise ValueError("Alpha #11 manifest does not start on the fixed eligible date")
    required_last_day = ELIGIBLE_START + timedelta(days=DEVELOPMENT_DAYS)
    required_end = required_last_day + timedelta(days=1)
    if date.fromisoformat(str(manifest.get("end_exclusive"))) < required_end:
        return manifest, []
    by_day = {date.fromisoformat(str(item["date"])): item for item in manifest.get("files") or []}
    required_days = [
        ELIGIBLE_START + timedelta(days=offset) for offset in range(DEVELOPMENT_DAYS + 1)
    ]
    missing = [day.isoformat() for day in required_days if day not in by_day]
    if missing:
        raise ValueError("Alpha #11 manifest is missing required days: " + ", ".join(missing))
    verified: list[dict[str, Any]] = []
    for day in required_days:
        item = by_day[day]
        archive_path = Path(str(item["archive"]))
        checksum_path = Path(str(item["checksum"]))
        if not archive_path.is_absolute():
            archive_path = PROJECT_ROOT / archive_path
        if not checksum_path.is_absolute():
            checksum_path = PROJECT_ROOT / checksum_path
        funding_path = Path(str(item["funding_file"]))
        if not funding_path.is_absolute():
            funding_path = PROJECT_ROOT / funding_path
        expected = parse_checksum(checksum_path.read_bytes(), expected_name=archive_path.name)
        observed = inspect_archive(
            archive_path,
            expected_digest=expected,
            expected_csv_name=archive_path.name.removesuffix(".zip") + ".csv",
            day=day,
        )
        if observed["sha256"] != item.get("sha256"):
            raise ValueError("reverified archive digest differs from manifest")
        funding_rows, funding_observed = inspect_funding_file(
            funding_path, symbol="BTCUSDT", day=day
        )
        for key, value in funding_observed.items():
            if item.get(key) != value:
                raise ValueError(f"reverified funding {key} differs from manifest")
        verified.append(
            {
                **item,
                "archive_path": archive_path,
                "funding_path": funding_path,
                "funding_rows_data": funding_rows,
                "day_value": day,
                **observed,
            }
        )
    for previous, current in zip(verified, verified[1:]):
        if int(current["first_agg_trade_id"]) != int(previous["last_agg_trade_id"]) + 1:
            raise ValueError("aggregate-trade IDs are discontinuous across development days")
    return manifest, verified


def iter_development_rows(files: list[dict[str, Any]], *, final_exit_ms: int) -> Iterator[dict[str, Any]]:
    yielded_after_exit = False
    for item in files:
        path = Path(item["archive_path"])
        csv_name = path.name.removesuffix(".zip") + ".csv"
        with ZipFile(path) as archive, archive.open(csv_name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
            for row in reader:
                timestamp = int(row["transact_time"])
                yield row
                if timestamp >= final_exit_ms:
                    yielded_after_exit = True
                    return
    if not yielded_after_exit:
        raise ValueError("development archives do not contain an executable final exit")


def load_funding(files: list[dict[str, Any]], *, end_ms: int) -> list[tuple[int, float, float]]:
    """Return every distinct Regular/Special charge with its mark-notional price."""
    events: list[tuple[int, float, float]] = []
    for item in files:
        for row in item["funding_rows_data"]:
            timestamp = int(row["fundingTime"])
            if timestamp <= end_ms:
                events.append(
                    (
                        timestamp,
                        float(Decimal(str(row["fundingRate"]))),
                        float(Decimal(str(row["markPrice"]))),
                    )
                )
    events.sort(key=lambda item: item[0])
    if not events:
        raise ValueError("sealed funding files contain no event before the final development exit")
    return events


def completed_months_in_files(files: list[dict[str, Any]]) -> list[str]:
    days = sorted(date.fromisoformat(str(item["date"])) for item in files)
    if not days:
        return []
    cursor = days[0].replace(day=1)
    output: list[str] = []
    while True:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        if next_month > days[-1]:
            break
        output.append(cursor.strftime("%Y-%m"))
        cursor = next_month
    return output


def verify_monthly_funding_reconciliations(
    files: list[dict[str, Any]], *, root: Path = MONTHLY_FUNDING_ROOT
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for month in completed_months_in_files(files):
        name = f"BTCUSDT-fundingRate-{month}.zip"
        archive_path = root / name
        checksum_path = root / f"{name}.CHECKSUM"
        expected_digest = parse_checksum(
            checksum_path.read_bytes(), expected_name=name
        )
        monthly_rows = inspect_monthly_archive(
            archive_path,
            expected_digest=expected_digest,
            expected_csv_name=name.removesuffix(".zip") + ".csv",
            month=month,
        )
        rest_rows = [
            row
            for item in files
            if str(item["date"]).startswith(month)
            for row in item["funding_rows_data"]
        ]
        comparison = compare_funding_rows(rest_rows, monthly_rows)
        if comparison["status"] != "PASS":
            raise ValueError(f"monthly funding reconciliation failed for {month}")
        reports.append(
            {
                "month": month,
                "status": "PASS",
                "publisher_sha256": expected_digest,
                "rest_regular_events": comparison["rest_regular_events"],
                "rest_special_events": comparison["rest_special_events"],
            }
        )
    return reports


def write_accumulating(output: Path, markdown_output: Path, manifest: dict[str, Any]) -> None:
    available = int(manifest.get("verified_days") or 0)
    report = {
        "schema_version": 1,
        "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
        "status": "ACCUMULATING",
        "performance_calculated": False,
        "verified_days": available,
        "required_archives_for_development_close": DEVELOPMENT_DAYS + 1,
        "locked_validation": "UNOPENED",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_output.write_text(
        "# Alpha #11 accumulation\n\nPerformance remains sealed until all development archives exist.\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/alpha011-development.json"))
    parser.add_argument(
        "--markdown-output", type=Path, default=Path("reports/alpha011-development.md")
    )
    args = parser.parse_args()
    manifest, files = load_and_verify_manifest(args.manifest)
    if not files:
        write_accumulating(args.output, args.markdown_output, manifest)
        return

    boundaries = development_boundaries()
    final_exit_ms = boundaries[-1] + 12 * 60 * 60 * 1000
    monthly_reconciliations = verify_monthly_funding_reconciliations(files)
    funding = load_funding(files, end_ms=final_exit_ms)
    base_trades = simulate_trade_stream(
        iter_development_rows(files, final_exit_ms=final_exit_ms),
        boundaries_ms=boundaries,
        funding_events=funding,
        fee_per_side_bps=5,
        slippage_per_side_bps=2,
    )
    double_fee_trades = reprice_trades(
        base_trades, fee_per_side_bps=10, slippage_per_side_bps=2
    )
    slippage_stress_trades = reprice_trades(
        base_trades, fee_per_side_bps=5, slippage_per_side_bps=5
    )
    summaries = {
        "base": summarize_trades(base_trades),
        "double_fee": summarize_trades(double_fee_trades),
        "five_bps_slippage": summarize_trades(slippage_stress_trades),
    }
    loss_probability = block_bootstrap_loss_probability(base_trades)
    checks = development_gate(
        summaries["base"],
        summaries["double_fee"],
        summaries["five_bps_slippage"],
        complete_days=DEVELOPMENT_DAYS,
        bootstrap_loss_probability=loss_probability,
    )
    passed = all(checks.values())
    report = {
        "schema_version": 1,
        "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
        "status": "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED" if passed else "FAIL_DEVELOPMENT",
        "performance_calculated": True,
        "validation_opened": False,
        "protocol_sha256": protocol_digests(),
        "data_integrity": {
            "status": "PASS",
            "manifest": str(args.manifest),
            "verified_days_used": len(files),
            "complete_development_days": DEVELOPMENT_DAYS,
            "first_day": files[0]["date"],
            "last_archive_used_only_to_close_the_final_development_trade": files[-1]["date"],
            "aggregate_trade_rows_in_archives": sum(int(item["rows"]) for item in files),
            "funding_events_in_archives": sum(int(item["funding_events"]) for item in files),
            "funding_source": "sealed Binance USD-M /fapi/v1/fundingRate daily responses",
            "monthly_funding_reconciliations": monthly_reconciliations,
        },
        "development": {**summaries, "base_trades": base_trades},
        "eight_day_block_bootstrap_probability_of_loss": loss_probability,
        "development_gate": {
            "status": "PASS" if passed else "FAIL",
            "checks": [{"name": name, "passed": value} for name, value in checks.items()],
        },
        "locked_validation": {
            "status": "UNOPENED",
            "rule": "may be evaluated once only if every development check passes",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(
        "\n".join(
            [
                "# Alpha #11 development report",
                "",
                f"- Verdict: **{report['status']}**",
                f"- Natural exits: {summaries['base']['trades']}",
                f"- Compounded net return: {summaries['base']['compounded_net_return']:.4%}",
                f"- Profit factor: {summaries['base']['profit_factor']}",
                f"- Maximum drawdown: {summaries['base']['maximum_drawdown']:.4%}",
                "- Locked validation opened: **no**",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "development"}, indent=2))


if __name__ == "__main__":
    main()
