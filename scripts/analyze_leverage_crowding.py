#!/usr/bin/env python3
"""Evaluate preregistered Alpha #10 development data without opening locked OOS."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any
from zipfile import ZipFile

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.leverage_crowding import (
    build_decision_frame,
    development_gate,
    simulate_trades,
    summarize_trades,
)
from scripts.download_binance_futures_metrics import parse_checksum, verify_archive


PROTOCOL_PATHS = (
    Path("research/alpha010-preregistration.json"),
    Path("scripts/download_binance_futures_metrics.py"),
    Path("quant/research/leverage_crowding.py"),
    Path("scripts/analyze_leverage_crowding.py"),
)


def protocol_digests() -> dict[str, str]:
    return {
        str(path): sha256((PROJECT_ROOT / path).read_bytes()).hexdigest()
        for path in PROTOCOL_PATHS
    }


def normalize_metrics_day(frame: pd.DataFrame, *, day: str) -> tuple[pd.DataFrame, dict[str, int]]:
    """Normalize publisher defects without manufacturing a metrics observation."""
    required = {"create_time", "symbol", "sum_open_interest"}
    missing_columns = sorted(required - set(frame.columns))
    if missing_columns:
        raise ValueError(f"metrics day {day} is missing columns: {', '.join(missing_columns)}")
    result = frame.copy()
    result["create_time"] = pd.to_datetime(result["create_time"], utc=True, errors="raise")
    if set(result["symbol"].astype(str)) != {"BTCUSDT"}:
        raise ValueError(f"metrics day {day} contains another symbol")

    repeated = result[result["create_time"].duplicated(keep=False)]
    if not repeated.empty:
        for timestamp, group in repeated.groupby("create_time", sort=False):
            if len(group.drop_duplicates()) != 1:
                raise ValueError(f"metrics day {day} has conflicting rows at {timestamp.isoformat()}")
    raw_rows = len(result)
    result = result.drop_duplicates().sort_values("create_time")
    if result["create_time"].duplicated().any():
        raise ValueError(f"metrics day {day} retains duplicate timestamps after exact deduplication")

    start = pd.Timestamp(f"{day}T00:00:00Z")
    expected_index = pd.date_range(start, periods=288, freq="5min")
    actual_index = pd.DatetimeIndex(result["create_time"])
    if not actual_index.difference(expected_index).empty:
        raise ValueError(f"metrics day {day} contains an off-grid or out-of-day timestamp")
    result = result.set_index("create_time").reindex(expected_index)
    result.index.name = "create_time"
    return result.reset_index(), {
        "raw_rows": raw_rows,
        "exact_duplicate_rows_removed": raw_rows - int(actual_index.size),
        "missing_grid_rows": int(result["sum_open_interest"].isna().sum()),
    }


def load_metrics(
    manifest_path: Path, *, expected_protocol: dict[str, str]
) -> tuple[pd.DataFrame, dict[str, Any], dict[str, int]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "PASS":
        raise ValueError("metrics manifest status is not PASS")
    if manifest.get("protocol_sha256") != expected_protocol:
        raise ValueError("metrics acquisition protocol differs from the current Alpha #10 protocol")
    if manifest.get("symbol") != "BTCUSDT":
        raise ValueError("metrics manifest is not BTCUSDT")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != int(manifest.get("expected_days") or 0):
        raise ValueError("metrics manifest file coverage is incomplete")
    frames: list[pd.DataFrame] = []
    integrity = {
        "raw_rows": 0,
        "exact_duplicate_rows_removed": 0,
        "missing_grid_rows": 0,
        "days_with_exact_duplicates": 0,
        "days_with_missing_grid_rows": 0,
    }
    seen_dates: set[str] = set()
    for item in files:
        day = str(item.get("date"))
        if day in seen_dates:
            raise ValueError("metrics manifest contains duplicate days")
        seen_dates.add(day)
        archive_path = Path(str(item.get("archive")))
        checksum_path = Path(str(item.get("checksum")))
        if not archive_path.is_absolute():
            archive_path = PROJECT_ROOT / archive_path
        if not checksum_path.is_absolute():
            checksum_path = PROJECT_ROOT / checksum_path
        archive_payload = archive_path.read_bytes()
        archive_name = archive_path.name
        expected_digest = parse_checksum(checksum_path.read_bytes(), expected_name=archive_name)
        csv_name = archive_name.removesuffix(".zip") + ".csv"
        verified = verify_archive(
            archive_payload,
            expected_digest=expected_digest,
            expected_csv_name=csv_name,
        )
        if verified["sha256"] != item.get("sha256"):
            raise ValueError("metrics manifest digest does not match reverified archive")
        with ZipFile(archive_path) as archive:
            with archive.open(csv_name) as handle:
                frame = pd.read_csv(handle)
        normalized, day_integrity = normalize_metrics_day(frame, day=day)
        frames.append(normalized)
        for key in ("raw_rows", "exact_duplicate_rows_removed", "missing_grid_rows"):
            integrity[key] += day_integrity[key]
        integrity["days_with_exact_duplicates"] += int(
            day_integrity["exact_duplicate_rows_removed"] > 0
        )
        integrity["days_with_missing_grid_rows"] += int(day_integrity["missing_grid_rows"] > 0)
    metrics = pd.concat(frames, ignore_index=True).sort_values("create_time")
    if metrics["create_time"].duplicated().any():
        raise ValueError("metrics contain duplicate timestamps")
    return metrics, manifest, integrity


def markdown(report: dict[str, Any]) -> str:
    base = report["development"]["base"]
    checks = report["development_gate"]["checks"]
    lines = [
        "# Alpha #10 development report",
        "",
        f"- Verdict: **{report['status']}**",
        f"- Natural exits: {base['trades']}",
        f"- Compounded net return on 1x trade notional: {base['compounded_net_return']:.4%}",
        f"- Profit factor: {base['profit_factor']}",
        f"- Maximum drawdown: {base['maximum_drawdown']}",
        f"- 3% crash precision lift: {base['three_percent_crash_precision_lift']}",
        "- Locked OOS opened: **no**",
        "",
        "## Development gate",
        "",
    ]
    lines.extend(f"- {'PASS' if item['passed'] else 'FAIL'} — {item['name']}" for item in checks)
    lines.extend(
        [
            "",
            "A development pass would only authorize a one-time locked-OOS evaluation. It is not a live-trading approval.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--metrics-manifest",
        type=Path,
        default=Path("research/alpha010/raw/BTCUSDT/manifest-2021-01-01-2025-01-01.json"),
    )
    parser.add_argument(
        "--candles",
        type=Path,
        default=Path("user_data/data/binance/futures/BTC_USDT_USDT-5m-futures.feather"),
    )
    parser.add_argument(
        "--funding",
        type=Path,
        default=Path("user_data/data/binance/futures/BTC_USDT_USDT-1h-funding_rate.feather"),
    )
    parser.add_argument("--output", type=Path, default=Path("reports/alpha010-development.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/alpha010-development.md"))
    args = parser.parse_args()

    current_protocol = protocol_digests()
    metrics, manifest, metrics_integrity = load_metrics(
        args.metrics_manifest, expected_protocol=current_protocol
    )
    candles = pd.read_feather(args.candles)
    funding = pd.read_feather(args.funding)
    decisions = build_decision_frame(metrics, candles, funding)
    start = pd.Timestamp("2021-07-01T00:00:00Z")
    end = pd.Timestamp("2025-01-01T00:00:00Z")
    development_decisions = decisions[(decisions.index >= start) & (decisions.index < end)]

    scenarios = {
        "base": (5.0, 2.0),
        "double_fee": (10.0, 2.0),
        "five_bps_slippage": (5.0, 5.0),
    }
    trade_sets = {
        name: simulate_trades(
            decisions,
            candles,
            funding,
            start=start,
            end_exclusive=end,
            fee_per_side_bps=fee_bps,
            slippage_per_side_bps=slippage_bps,
        )
        for name, (fee_bps, slippage_bps) in scenarios.items()
    }
    summaries = {
        name: summarize_trades(trades, decisions, start=start, end_exclusive=end)
        for name, trades in trade_sets.items()
    }
    checks = development_gate(
        summaries["base"], summaries["double_fee"], summaries["five_bps_slippage"]
    )
    passed = all(checks.values())
    report = {
        "schema_version": 1,
        "candidate": "ALPHA_010_LEVERAGE_CROWDING_CRASH_SHORT",
        "status": "DEVELOPMENT_PASS_LOCKED_OOS_UNOPENED" if passed else "FAIL_DEVELOPMENT",
        "validation_opened": False,
        "protocol_sha256": current_protocol,
        "data_integrity": {
            "status": "PASS",
            "metrics_manifest": str(args.metrics_manifest),
            "metrics_manifest_status": manifest["status"],
            "verified_metric_days": manifest["verified_days"],
            "metrics_rows": len(metrics),
            "decision_rows": len(decisions),
            "development_decision_rows": len(development_decisions),
            "development_complete_feature_rows": int(
                development_decisions["features_complete"].sum()
            ),
            "development_incomplete_feature_rows": int(
                (~development_decisions["features_complete"]).sum()
            ),
            "normalization": metrics_integrity,
            "missing_data_policy": "NaN preserved; no fill or interpolation; incomplete features cannot signal",
        },
        "development": {
            **summaries,
            "trades": trade_sets["base"],
        },
        "development_gate": {
            "status": "PASS" if passed else "FAIL",
            "checks": [{"name": name, "passed": value} for name, value in checks.items()],
        },
        "locked_oos": {
            "status": "UNOPENED",
            "period": "2025-01-01 through 2026-09-01 UTC, end-exclusive",
        },
        "warning": "A development pass only authorizes one locked-OOS evaluation and never authorizes live trading.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "development"}, indent=2))
    print(json.dumps({"development": summaries}, indent=2))


if __name__ == "__main__":
    main()
