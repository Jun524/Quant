#!/usr/bin/env python3
"""Replay and append Alpha #11's terminal development verdict exactly once."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry
from scripts.download_binance_aggtrades import protocol_digests


CANDIDATE = "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION"
REPORT_PATH = PROJECT_ROOT / "reports" / "alpha011-development.json"
RECEIPT_PATH = PROJECT_ROOT / "reports" / "alpha011-development-receipt.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "evidence" / "alpha011"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha011" / "raw" / "BTCUSDT"
EVIDENCE_ENCODING = "gzip"
TERMINAL_STATUSES = {
    "FAIL_DEVELOPMENT": "FAIL",
    "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED": "REVIEW",
}


def digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def archive_report(report_path: Path, archive_root: Path) -> tuple[str, Path]:
    payload = report_path.read_bytes()
    source_hash = sha256(payload).hexdigest()
    archive_path = archive_root / f"{source_hash}.json.gz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if gzip.decompress(archive_path.read_bytes()) != payload:
            raise ValueError(f"Alpha #11 evidence archive hash collision: {archive_path}")
        return source_hash, archive_path
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        dir=archive_path.parent,
        prefix=".alpha011-evidence-",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        with gzip.GzipFile(fileobj=temporary, mode="wb", mtime=0) as compressed:
            compressed.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
    try:
        os.replace(temporary_path, archive_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return source_hash, archive_path


def registry_status(report_status: str) -> str:
    if report_status not in TERMINAL_STATUSES:
        raise ValueError(f"unsupported Alpha #11 terminal status: {report_status}")
    return TERMINAL_STATUSES[report_status]


def _manifest_from_report(report: dict[str, Any], project_root: Path) -> Path:
    value = (report.get("data_integrity") or {}).get("manifest")
    path = Path(str(value or ""))
    path = path if path.is_absolute() else project_root / path
    try:
        path.resolve().relative_to(
            (project_root / "research" / "alpha011" / "raw" / "BTCUSDT").resolve()
        )
    except ValueError as error:
        raise ValueError("Alpha #11 report manifest is outside the sealed raw root") from error
    if not path.is_file():
        raise ValueError("Alpha #11 report manifest is missing")
    return path


def replay_report(
    report: dict[str, Any],
    *,
    manifest_path: Path,
    project_root: Path = PROJECT_ROOT,
) -> None:
    with tempfile.TemporaryDirectory(prefix="alpha011-replay-") as directory:
        replay_path = Path(directory) / "report.json"
        markdown_path = Path(directory) / "report.md"
        completed = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "analyze_quarter_hour_flow.py"),
                "--manifest",
                str(manifest_path),
                "--output",
                str(replay_path),
                "--markdown-output",
                str(markdown_path),
            ],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                "Alpha #11 source replay failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        replayed = json.loads(replay_path.read_text(encoding="utf-8"))
    if replayed != report:
        raise ValueError("Alpha #11 stored report differs from exact source replay")


def build_registry_result(
    report: dict[str, Any],
    *,
    report_sha256: str,
    manifest_sha256: str,
    evidence_archive: str,
) -> dict[str, Any]:
    status = registry_status(str(report.get("status") or ""))
    base = (report.get("development") or {}).get("base") or {}
    checks = [
        {
            "name": str(item.get("name") or ""),
            "passed": bool(item.get("passed")),
            "actual": bool(item.get("passed")),
            "required": True,
            "severity": "fail",
        }
        for item in (report.get("development_gate") or {}).get("checks") or []
    ]
    return {
        "status": status,
        "development_status": report["status"],
        "promotion_eligible": False,
        "validation_opened": False,
        "locked_validation_opened": False,
        "source_report": "reports/alpha011-development.json",
        "source_sha256": report_sha256,
        "evidence_archive": evidence_archive,
        "evidence_encoding": EVIDENCE_ENCODING,
        "source_manifest": (report.get("data_integrity") or {}).get("manifest"),
        "source_manifest_sha256": manifest_sha256,
        "protocol_sha256": report.get("protocol_sha256"),
        "development": {
            "organic": {
                "trades": int(base.get("trades") or 0),
                "net_profit": float(base.get("sum_net_return") or 0.0),
                "net_return": float(base.get("compounded_net_return") or 0.0),
                "profit_factor": base.get("profit_factor"),
            },
            "pnl_components": {
                "price_pnl_estimate": float(base.get("price_return_sum") or 0.0),
                "trading_fees": -float(base.get("trading_fee_drag_sum") or 0.0),
                "funding_pnl": float(base.get("funding_return_sum") or 0.0),
                "modeled_slippage": -float(base.get("slippage_drag_sum") or 0.0),
                "net_pnl": float(base.get("sum_net_return") or 0.0),
            },
            "breakout_metrics": {
                "maximum_drawdown": base.get("maximum_drawdown"),
                "largest_winner_profit_concentration": base.get(
                    "top_three_winner_profit_concentration"
                ),
                "long_expectancy": base.get("long_expectancy"),
                "short_expectancy": base.get("short_expectancy"),
                "mae_mean": base.get("mae_mean"),
                "mfe_mean": base.get("mfe_mean"),
                "median_holding_minutes": (
                    float(base["median_holding_hours"]) * 60
                    if base.get("median_holding_hours") is not None
                    else None
                ),
            },
        },
        "gate": {
            "status": status,
            "stage": "forward_development",
            "policy": {"min_oos_trades": 100},
            "checks": checks,
        },
        "warning": (
            "Development PASS is REVIEW only. Locked validation remains unopened and "
            "this receipt cannot authorize portfolio or live promotion."
        ),
    }


def receipt_health(
    *,
    receipt_path: Path = RECEIPT_PATH,
    report_path: Path = REPORT_PATH,
    registry_path: Path = REGISTRY_PATH,
    project_root: Path = PROJECT_ROOT,
    expected_protocol: dict[str, str] | None = None,
    synchronizer_path: Path | None = None,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        manifest_path = _manifest_from_report(report, project_root)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {"valid": False, "failures": [f"receipt_inputs_invalid:{type(error).__name__}"]}
    failures: list[str] = []
    current_protocol = expected_protocol or protocol_digests()
    synchronizer_path = synchronizer_path or Path(__file__)
    if receipt.get("schema_version") != 1 or receipt.get("status") != "PASS":
        failures.append("receipt_schema_or_status_invalid")
    if receipt.get("report_sha256") != digest(report_path):
        failures.append("report_sha256_mismatch")
    if receipt.get("manifest_sha256") != digest(manifest_path):
        failures.append("manifest_sha256_mismatch")
    if receipt.get("protocol_sha256") != current_protocol:
        failures.append("receipt_protocol_stale")
    if report.get("protocol_sha256") != current_protocol:
        failures.append("report_protocol_stale")
    if receipt.get("synchronizer_sha256") != digest(synchronizer_path):
        failures.append("synchronizer_sha256_mismatch")
    if receipt.get("source_replay") != "PASS":
        failures.append("source_replay_not_pass")
    if receipt.get("promotion_eligible") is not False:
        failures.append("promotion_eligibility_must_be_false")
    if receipt.get("locked_validation_opened") is not False:
        failures.append("locked_validation_must_be_unopened")
    event_id = int(receipt.get("registry_event_id") or -1)
    event = next(
        (item for item in registry.get("events") or [] if int(item.get("event_id") or -2) == event_id),
        None,
    )
    result = ((event or {}).get("payload") or {}).get("result") or {}
    if (
        event is None
        or result.get("source_sha256") != receipt.get("report_sha256")
        or result.get("source_manifest_sha256") != receipt.get("manifest_sha256")
        or result.get("development_status") != receipt.get("report_status")
    ):
        failures.append("registry_event_binding_invalid")
    return {
        "valid": not failures,
        "failures": failures,
        "report_status": receipt.get("report_status"),
        "registry_event_id": receipt.get("registry_event_id"),
        "report_sha256": receipt.get("report_sha256"),
        "manifest_sha256": receipt.get("manifest_sha256"),
    }


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if report.get("status") == "ACCUMULATING":
        print(json.dumps({"status": "DEVELOPMENT_NOT_READY"}))
        return
    if report.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #11 report candidate mismatch")
    registry_status(str(report.get("status") or ""))
    if report.get("protocol_sha256") != protocol_digests():
        raise SystemExit("FAIL: Alpha #11 report protocol is stale")
    if report.get("validation_opened") is not False:
        raise SystemExit("FAIL: Alpha #11 report opened locked validation")
    integrity = report.get("data_integrity") or {}
    if (
        integrity.get("status") != "PASS"
        or int(integrity.get("verified_days_used") or 0) != 61
        or int(integrity.get("complete_development_days") or 0) != 60
    ):
        raise SystemExit("FAIL: Alpha #11 report data integrity is incomplete")
    manifest_path = _manifest_from_report(report, PROJECT_ROOT)
    replay_report(report, manifest_path=manifest_path)
    report_sha256, archive_path = archive_report(REPORT_PATH, ARCHIVE_ROOT)
    manifest_sha256 = digest(manifest_path)

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments", []) if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #11 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    existing = next(
        (
            item
            for item in registry.get("events") or []
            if int(item.get("experiment_id") or -1) == experiment_id
            and item.get("event_type") == "result_update"
            and (((item.get("payload") or {}).get("result") or {}).get("source_sha256"))
            == report_sha256
        ),
        None,
    )
    if existing is None:
        result = build_registry_result(
            report,
            report_sha256=report_sha256,
            manifest_sha256=manifest_sha256,
            evidence_archive=str(archive_path.relative_to(PROJECT_ROOT)),
        )
        event = ExperimentRegistry(REGISTRY_PATH).append_event(
            experiment_id=experiment_id,
            event_type="result_update",
            payload={"source_sha256": report_sha256, "result": result},
        )
    else:
        event = existing
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "candidate": CANDIDATE,
        "report_status": report["status"],
        "report_path": "reports/alpha011-development.json",
        "report_sha256": report_sha256,
        "manifest_path": str(manifest_path.relative_to(PROJECT_ROOT)),
        "manifest_sha256": manifest_sha256,
        "protocol_sha256": protocol_digests(),
        "synchronizer_sha256": digest(Path(__file__)),
        "source_replay": "PASS",
        "evidence_archive": str(archive_path.relative_to(PROJECT_ROOT)),
        "evidence_encoding": EVIDENCE_ENCODING,
        "registry_event_id": int(event["event_id"]),
        "promotion_eligible": False,
        "locked_validation_opened": False,
    }
    _atomic_json(RECEIPT_PATH, receipt)
    print(
        json.dumps(
            {
                "status": "SYNCED" if existing is None else "ALREADY_SYNCED",
                "registry_event_id": event["event_id"],
                "report_sha256": report_sha256,
                "receipt": str(RECEIPT_PATH.relative_to(PROJECT_ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
