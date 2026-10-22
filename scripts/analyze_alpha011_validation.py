#!/usr/bin/env python3
"""Evaluate Alpha #11 only inside its immutable post-development lock window."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.quarter_hour_flow import (
    block_bootstrap_loss_probability,
    reprice_trades,
    simulate_trade_stream,
    summarize_trades,
)
from scripts.analyze_quarter_hour_flow import (
    iter_development_rows,
    load_funding,
    verify_monthly_funding_reconciliations,
)
from scripts.download_binance_aggtrades import (
    inspect_archive,
    inspect_funding_file,
    parse_checksum,
    protocol_digests as acquisition_protocol_digests,
)
from scripts.sync_alpha011_registry import receipt_health


LOCK_PATH = PROJECT_ROOT / "research" / "alpha011" / "validation-lock.json"
OUTPUT = PROJECT_ROOT / "reports" / "alpha011-validation.json"
REQUIRED_DAYS = 60
VALIDATION_PROTOCOL_PATHS = (
    Path("research/alpha011-preregistration.json"),
    Path("scripts/download_binance_aggtrades.py"),
    Path("quant/research/quarter_hour_flow.py"),
    Path("scripts/analyze_quarter_hour_flow.py"),
    Path("scripts/analyze_alpha011_validation.py"),
    Path("scripts/open_alpha011_validation.py"),
    Path("scripts/sync_alpha011_validation.py"),
)


def validation_protocol_digests(project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    return {
        str(path): sha256((project_root / path).read_bytes()).hexdigest()
        for path in VALIDATION_PROTOCOL_PATHS
    }


def validation_boundaries(start_day: date, *, days: int = REQUIRED_DAYS) -> list[int]:
    values: list[int] = []
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        for boundary in (time(0, 15), time(12, 15)):
            values.append(
                int(datetime.combine(day, boundary, timezone.utc).timestamp() * 1000)
            )
    return values


def locked_validation_gate(
    base: dict[str, Any],
    double_fee: dict[str, Any],
    slippage_stress: dict[str, Any],
    *,
    complete_days: int,
    bootstrap_loss_probability: float | None,
) -> dict[str, bool]:
    return {
        "complete_utc_days_minimum": complete_days >= 60,
        "natural_closed_trades_minimum": int(base["trades"]) >= 100,
        "net_return_positive": float(base["compounded_net_return"]) > 0,
        "profit_factor_minimum": base["profit_factor"] is not None
        and float(base["profit_factor"]) >= 1.20,
        "double_fee_net_positive": float(double_fee["compounded_net_return"]) > 0,
        "five_bps_per_side_slippage_net_positive": float(
            slippage_stress["compounded_net_return"]
        )
        > 0,
        "maximum_drawdown_maximum": float(base["maximum_drawdown"]) <= 0.20,
        "top_three_winner_profit_concentration_maximum": base[
            "top_three_winner_profit_concentration"
        ]
        is not None
        and float(base["top_three_winner_profit_concentration"]) <= 0.35,
        "long_expectancy_positive": base["long_expectancy"] is not None
        and float(base["long_expectancy"]) > 0,
        "short_expectancy_positive": base["short_expectancy"] is not None
        and float(base["short_expectancy"]) > 0,
        "eight_day_block_bootstrap_probability_of_loss_maximum": bootstrap_loss_probability
        is not None
        and bootstrap_loss_probability <= 0.05,
    }


def _resolve_source(path_value: object, *, project_root: Path) -> Path:
    path = Path(str(path_value))
    path = path if path.is_absolute() else project_root / path
    try:
        path.resolve().relative_to(
            (project_root / "research" / "alpha011" / "raw" / "BTCUSDT").resolve()
        )
    except ValueError as error:
        raise ValueError("validation source path is outside the sealed raw root") from error
    return path


def validate_lock_chain(
    lock: dict[str, Any],
    *,
    project_root: Path = PROJECT_ROOT,
    lock_bytes: bytes | None = None,
    expected_validation_protocol: dict[str, str] | None = None,
    expected_development_protocol: dict[str, str] | None = None,
    development_receipt_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Revalidate every development artifact that authorized the locked window."""
    current_validation_protocol = expected_validation_protocol or validation_protocol_digests(
        project_root
    )
    current_development_protocol = (
        expected_development_protocol or acquisition_protocol_digests()
    )
    structural = (
        lock.get("status") == "OPEN"
        and lock.get("immutable") is True
        and lock.get("candidate") == "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION"
        and lock.get("validation_protocol_sha256") == current_validation_protocol
        and lock.get("development_protocol_sha256") == current_development_protocol
        and int(lock.get("required_complete_utc_days") or 0) == REQUIRED_DAYS
        and int(lock.get("required_archives_for_final_exit") or 0) == REQUIRED_DAYS + 1
        and lock.get("promotion_eligible") is False
    )
    if not structural:
        raise ValueError("Alpha #11 validation lock is stale or structurally invalid")
    report_path = (project_root / str(lock.get("development_report") or "")).resolve()
    receipt_path = (project_root / str(lock.get("development_receipt") or "")).resolve()
    for path in (report_path, receipt_path):
        try:
            path.relative_to(project_root.resolve())
        except ValueError as error:
            raise ValueError("Alpha #11 lock evidence path escapes the project") from error
        if not path.is_file():
            raise ValueError("Alpha #11 lock evidence is missing")
    if sha256(report_path.read_bytes()).hexdigest() != lock.get("development_report_sha256"):
        raise ValueError("Alpha #11 locked development report has changed")
    if sha256(receipt_path.read_bytes()).hexdigest() != lock.get("development_receipt_sha256"):
        raise ValueError("Alpha #11 locked development receipt has changed")
    audit = development_receipt_audit or receipt_health(
        receipt_path=receipt_path,
        report_path=report_path,
        registry_path=project_root / "research" / "registry.json",
        project_root=project_root,
        expected_protocol=current_development_protocol,
        synchronizer_path=project_root / "scripts" / "sync_alpha011_registry.py",
    )
    if (
        audit.get("valid") is not True
        or audit.get("report_status") != "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED"
        or audit.get("registry_event_id") != lock.get("development_registry_event_id")
    ):
        raise ValueError("Alpha #11 locked development authorization is no longer valid")
    start_at = datetime.fromisoformat(
        str(lock["validation_start_utc"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if start_at.time() != time.min:
        raise ValueError("Alpha #11 validation must start at UTC midnight")
    return {
        "status": "PASS",
        "validation_start": start_at,
        "lock_sha256": sha256(lock_bytes or json.dumps(lock, sort_keys=True).encode()).hexdigest(),
        "development_receipt": audit,
        "validation_protocol_sha256": current_validation_protocol,
    }


def load_validation_files(
    manifest_path: Path,
    *,
    start_day: date,
    project_root: Path = PROJECT_ROOT,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "PASS"
        or manifest.get("symbol") != "BTCUSDT"
        or manifest.get("protocol_sha256") != acquisition_protocol_digests()
    ):
        raise ValueError("validation acquisition manifest is not current and passing")
    by_day = {
        date.fromisoformat(str(item["date"])): item
        for item in manifest.get("files") or []
    }
    required = [start_day + timedelta(days=offset) for offset in range(REQUIRED_DAYS + 1)]
    if any(day not in by_day for day in required):
        return manifest, []
    verified: list[dict[str, Any]] = []
    for day in required:
        item = by_day[day]
        archive_path = _resolve_source(item["archive"], project_root=project_root)
        checksum_path = _resolve_source(item["checksum"], project_root=project_root)
        funding_path = _resolve_source(item["funding_file"], project_root=project_root)
        expected = parse_checksum(
            checksum_path.read_bytes(), expected_name=archive_path.name
        )
        observed = inspect_archive(
            archive_path,
            expected_digest=expected,
            expected_csv_name=archive_path.name.removesuffix(".zip") + ".csv",
            day=day,
        )
        if observed["sha256"] != item.get("sha256"):
            raise ValueError("validation archive digest differs from manifest")
        funding_rows, funding_observed = inspect_funding_file(
            funding_path, symbol="BTCUSDT", day=day
        )
        for key, value in funding_observed.items():
            if item.get(key) != value:
                raise ValueError(f"validation funding {key} differs from manifest")
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
            raise ValueError("validation aggregate-trade IDs are discontinuous")
    return manifest, verified


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=LOCK_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    lock_bytes = args.lock.read_bytes()
    lock = json.loads(lock_bytes)
    try:
        lock_audit = validate_lock_chain(lock, lock_bytes=lock_bytes)
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error
    current_validation_protocol = lock_audit["validation_protocol_sha256"]
    start_at = lock_audit["validation_start"]
    manifest, files = load_validation_files(
        args.manifest,
        start_day=start_at.date(),
    )
    if not files:
        report = {
            "schema_version": 1,
            "candidate": lock["candidate"],
            "status": "ACCUMULATING_LOCKED_VALIDATION",
            "performance_calculated": False,
            "validation_start_utc": start_at.isoformat(),
            "required_complete_utc_days": REQUIRED_DAYS,
            "required_archives_for_final_exit": REQUIRED_DAYS + 1,
            "available_required_archives": sum(
                1
                for item in manifest.get("files") or []
                if start_at.date()
                <= date.fromisoformat(str(item["date"]))
                <= start_at.date() + timedelta(days=REQUIRED_DAYS)
            ),
            "lock_sha256": sha256(lock_bytes).hexdigest(),
            "validation_protocol_sha256": current_validation_protocol,
            "promotion_eligible": False,
        }
    else:
        boundaries = validation_boundaries(start_at.date())
        final_exit_ms = boundaries[-1] + 12 * 60 * 60 * 1000
        monthly = verify_monthly_funding_reconciliations(files)
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
        slippage_trades = reprice_trades(
            base_trades, fee_per_side_bps=5, slippage_per_side_bps=5
        )
        summaries = {
            "base": summarize_trades(base_trades),
            "double_fee": summarize_trades(double_fee_trades),
            "five_bps_slippage": summarize_trades(slippage_trades),
        }
        loss_probability = block_bootstrap_loss_probability(base_trades)
        checks = locked_validation_gate(
            summaries["base"],
            summaries["double_fee"],
            summaries["five_bps_slippage"],
            complete_days=REQUIRED_DAYS,
            bootstrap_loss_probability=loss_probability,
        )
        passed = all(checks.values())
        report = {
            "schema_version": 1,
            "candidate": lock["candidate"],
            "status": "PASS" if passed else "FAIL",
            "performance_calculated": True,
            "validation_start_utc": start_at.isoformat(),
            "validation_end_exclusive_utc": (
                start_at + timedelta(days=REQUIRED_DAYS)
            ).isoformat(),
            "required_complete_utc_days": REQUIRED_DAYS,
            "lock_sha256": sha256(lock_bytes).hexdigest(),
            "validation_protocol_sha256": current_validation_protocol,
            "acquisition_protocol_sha256": acquisition_protocol_digests(),
            "source_manifest": str(args.manifest),
            "source_manifest_sha256": sha256(args.manifest.read_bytes()).hexdigest(),
            "monthly_funding_reconciliations": monthly,
            "validation": {**summaries, "base_trades": base_trades},
            "eight_day_block_bootstrap_probability_of_loss": loss_probability,
            "gate": {
                "status": "PASS" if passed else "FAIL",
                "checks": [
                    {"name": name, "passed": value}
                    for name, value in checks.items()
                ],
            },
            "promotion_eligible": False,
            "warning": (
                "A sleeve PASS still requires portfolio selection-bias, diversification, "
                "cost, drawdown, ruin, and live-preflight gates."
            ),
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "validation"}, indent=2))


if __name__ == "__main__":
    main()
