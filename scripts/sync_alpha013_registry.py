#!/usr/bin/env python3
"""Append source-bound Alpha #13 weekly development snapshots to the registry."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from datetime import datetime, timedelta


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry
from scripts.alpha013_status import status_payload
from quant.research.funding_dispersion_reversal import evaluate_development, load_sealed_markets
from scripts.reconcile_alpha013_funding import reconciled_coverage


CANDIDATE = "ALPHA_013_FUNDING_DISPERSION_REVERSAL"
REPORT_PATH = PROJECT_ROOT / "reports" / "alpha013-forward.json"
PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "evidence" / "alpha013"
EVIDENCE_ENCODING = "gzip"
RECEIPT_PATH = PROJECT_ROOT / "reports" / "alpha013-development-receipt.json"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def archive_report(report_path: Path, archive_root: Path) -> tuple[str, Path]:
    payload = report_path.read_bytes()
    source_hash = sha256(payload).hexdigest()
    archive_path = archive_root / f"{source_hash}.json.gz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if gzip.decompress(archive_path.read_bytes()) != payload:
            raise ValueError(f"Alpha #13 evidence archive hash collision: {archive_path}")
        return source_hash, archive_path
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        dir=archive_path.parent,
        prefix=".alpha013-evidence-",
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


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def replay_report(
    report: dict[str, Any],
    *,
    raw_root: Path = RAW_ROOT,
    preregistration_path: Path = PREREGISTRATION_PATH,
) -> None:
    """Rebuild the development report without mutating its sealed acquisition code."""
    prereg = json.loads(preregistration_path.read_text(encoding="utf-8"))
    markets, integrity = load_sealed_markets(raw_root)
    preliminary = evaluate_development(
        markets,
        first_decision=prereg["fresh_sample"]["first_eligible_decision"],
        funding_reconciled=False,
    )
    weeks = preliminary["development"]["base"]["weeks"]
    if weeks:
        first_decision = datetime.fromisoformat(
            prereg["fresh_sample"]["first_eligible_decision"].replace("Z", "+00:00")
        )
        reconciliation = reconciled_coverage(
            raw_root,
            start_day=(first_decision - timedelta(days=7)).date(),
            end_day=datetime.fromisoformat(weeks[-1]["close_at"]).date(),
        )
    else:
        reconciliation = {
            "status": "NOT_APPLICABLE_YET",
            "required_days": 0,
            "covered_days": [],
            "failures": [],
        }
    replayed = evaluate_development(
        markets,
        first_decision=prereg["fresh_sample"]["first_eligible_decision"],
        funding_reconciled=reconciliation["status"] == "PASS",
    )
    replayed["data_integrity"] = integrity
    replayed["funding_reconciliation"] = reconciliation
    replayed["preregistration_sha256"] = digest(preregistration_path)
    replayed["preregistration_path"] = str(
        preregistration_path.relative_to(PROJECT_ROOT)
    )
    if replayed != report:
        raise ValueError("Alpha #13 report differs from exact sealed-source replay")


def registry_status(report_status: str) -> str:
    mapping = {
        "ACCUMULATING": "ACCUMULATING",
        "FAIL_DEVELOPMENT": "FAIL",
        "PASS_DEVELOPMENT": "REVIEW",
    }
    if report_status not in mapping:
        raise ValueError(f"unsupported Alpha #13 report status: {report_status}")
    return mapping[report_status]


def funding_reconciliation_only_pending(report: dict[str, Any]) -> bool:
    gate = report.get("gate") or {}
    return bool(
        report.get("status") == "FAIL_DEVELOPMENT"
        and gate.get("all_funding_rows_reconciled") is False
        and all(
            value is True
            for name, value in gate.items()
            if name != "all_funding_rows_reconciled"
        )
    )


def build_registry_result(
    report: dict[str, Any],
    *,
    report_sha256: str,
    evidence_archive: str,
    preregistration_sha256: str,
) -> dict[str, Any]:
    base = (report.get("development") or {}).get("base") or {}
    components = base.get("pnl_components") or {}
    checks = [
        {
            "name": str(name),
            "passed": bool(passed),
            "actual": bool(passed),
            "required": True,
            "severity": "fail",
        }
        for name, passed in (report.get("gate") or {}).items()
    ]
    status = registry_status(str(report.get("status") or ""))
    return {
        "status": status,
        "development_status": report["status"],
        "promotion_eligible": False,
        "validation_opened": False,
        "locked_validation_opened": False,
        "source_report": "reports/alpha013-forward.json",
        "source_sha256": report_sha256,
        "evidence_archive": evidence_archive,
        "evidence_encoding": EVIDENCE_ENCODING,
        "preregistration_path": "research/alpha013-preregistration.json",
        "preregistration_sha256": preregistration_sha256,
        "source_inventory_sha256": (report.get("data_integrity") or {}).get(
            "source_inventory_sha256"
        ),
        "protocol_sha256": (report.get("data_integrity") or {}).get(
            "protocol_sha256"
        ),
        "development": {
            "organic": {
                "trades": int(base.get("eligible_weeks") or 0),
                "closed_legs": int(base.get("closed_legs") or 0),
                "net_profit": float(components.get("net_pnl") or 0.0),
                "net_return": float(base.get("net_return") or 0.0),
                "profit_factor": base.get("profit_factor_by_week"),
            },
            "pnl_components": {
                "price_pnl_estimate": float(
                    components.get("raw_relative_price_pnl") or 0.0
                ),
                "trading_fees": float(components.get("trading_fees") or 0.0),
                "funding_pnl": float(components.get("funding_pnl") or 0.0),
                "modeled_slippage": float(
                    components.get("modeled_slippage") or 0.0
                ),
                "net_pnl": float(components.get("net_pnl") or 0.0),
            },
            "carry_metrics": {
                "maximum_mark_to_market_drawdown": base.get(
                    "maximum_mark_to_market_drawdown"
                ),
                "largest_winning_week_concentration": base.get(
                    "largest_winning_week_concentration"
                ),
                "median_score_spread": base.get("median_score_spread"),
                "double_fee_net_return": (
                    (report.get("development") or {}).get("double_fee") or {}
                ).get("net_return"),
                "five_bps_slippage_net_return": (
                    (report.get("development") or {}).get("five_bps_slippage")
                    or {}
                ).get("net_return"),
            },
        },
        "gate": {
            "status": status,
            "stage": "forward_development",
            "policy": {"min_oos_trades": 30},
            "checks": checks,
        },
        "warning": (
            "Development PASS is REVIEW only. Locked validation remains unopened and "
            "this snapshot cannot authorize portfolio or live promotion."
        ),
    }


def latest_snapshot_key(events: list[dict[str, Any]], experiment_id: int) -> tuple | None:
    for event in reversed(events):
        if (
            int(event.get("experiment_id") or -1) != experiment_id
            or event.get("event_type") != "result_update"
        ):
            continue
        result = (event.get("payload") or {}).get("result") or {}
        organic = (result.get("development") or {}).get("organic") or {}
        return (
            result.get("development_status"),
            int(organic.get("trades") or 0),
        )
    return None


def receipt_health(
    *,
    receipt_path: Path = RECEIPT_PATH,
    report_path: Path = REPORT_PATH,
    registry_path: Path = REGISTRY_PATH,
    preregistration_path: Path = PREREGISTRATION_PATH,
    synchronizer_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"valid": False, "failures": [f"receipt_inputs_invalid:{type(error).__name__}"]}
    failures: list[str] = []
    synchronizer_path = synchronizer_path or Path(__file__)
    if receipt.get("schema_version") != 1 or receipt.get("status") != "PASS":
        failures.append("receipt_schema_or_status_invalid")
    if receipt.get("report_sha256") != digest(report_path):
        failures.append("report_sha256_mismatch")
    if receipt.get("preregistration_sha256") != digest(preregistration_path):
        failures.append("preregistration_sha256_mismatch")
    integrity = report.get("data_integrity") or {}
    if receipt.get("source_inventory_sha256") != integrity.get("source_inventory_sha256"):
        failures.append("source_inventory_sha256_mismatch")
    if receipt.get("protocol_sha256") != integrity.get("protocol_sha256"):
        failures.append("protocol_sha256_mismatch")
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
        or result.get("source_inventory_sha256") != receipt.get("source_inventory_sha256")
        or result.get("development_status") != receipt.get("report_status")
    ):
        failures.append("registry_event_binding_invalid")
    archive = project_root / str(receipt.get("evidence_archive") or "")
    try:
        if gzip.decompress(archive.read_bytes()) != report_path.read_bytes():
            failures.append("evidence_archive_mismatch")
    except (OSError, gzip.BadGzipFile):
        failures.append("evidence_archive_invalid")
    return {
        "valid": not failures,
        "failures": failures,
        "report_status": receipt.get("report_status"),
        "registry_event_id": receipt.get("registry_event_id"),
        "report_sha256": receipt.get("report_sha256"),
        "source_inventory_sha256": receipt.get("source_inventory_sha256"),
    }


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    state = status_payload()
    if state.get("forward_report_current") is not True:
        raise SystemExit("FAIL: Alpha #13 forward report is stale or source-unbound")
    if report.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #13 report candidate mismatch")
    preregistration_sha256 = digest(PREREGISTRATION_PATH)
    if report.get("preregistration_sha256") != preregistration_sha256:
        raise SystemExit("FAIL: Alpha #13 report preregistration mismatch")
    if report.get("locked_validation_opened") is not False:
        raise SystemExit("FAIL: Alpha #13 development report opened validation")
    funding_only_pending = funding_reconciliation_only_pending(report)
    if funding_only_pending:
        print(json.dumps({"status": "WAITING_FUNDING_RECONCILIATION"}))
        return
    terminal = report.get("status") in {"PASS_DEVELOPMENT", "FAIL_DEVELOPMENT"}
    if terminal:
        try:
            replay_report(report)
        except ValueError as error:
            raise SystemExit(f"FAIL: {error}") from error

    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments", []) if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #13 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    base = (report.get("development") or {}).get("base") or {}
    snapshot_key = (report.get("status"), int(base.get("eligible_weeks") or 0))
    if snapshot_key == ("ACCUMULATING", 0):
        print(json.dumps({"status": "NO_NEW_ELIGIBLE_WEEK", "snapshot": snapshot_key}))
        return
    if (
        not terminal
        and latest_snapshot_key(registry.get("events") or [], experiment_id) == snapshot_key
    ):
        print(json.dumps({"status": "UNCHANGED", "snapshot": snapshot_key}))
        return

    source_hash, archive_path = archive_report(REPORT_PATH, ARCHIVE_ROOT)
    existing = next(
        (
            item for item in registry.get("events") or []
            if int(item.get("experiment_id") or -1) == experiment_id
            and item.get("event_type") == "result_update"
            and (((item.get("payload") or {}).get("result") or {}).get("source_sha256"))
            == source_hash
        ),
        None,
    )
    if existing is None:
        result = build_registry_result(
            report,
            report_sha256=source_hash,
            evidence_archive=str(archive_path.relative_to(PROJECT_ROOT)),
            preregistration_sha256=preregistration_sha256,
        )
        event = ExperimentRegistry(REGISTRY_PATH).append_event(
            experiment_id=experiment_id,
            event_type="result_update",
            payload={"source_sha256": source_hash, "result": result},
        )
    else:
        event = existing
    if terminal:
        receipt = {
            "schema_version": 1,
            "status": "PASS",
            "candidate": CANDIDATE,
            "report_status": report["status"],
            "report_sha256": source_hash,
            "preregistration_sha256": preregistration_sha256,
            "source_inventory_sha256": (report.get("data_integrity") or {}).get(
                "source_inventory_sha256"
            ),
            "protocol_sha256": (report.get("data_integrity") or {}).get(
                "protocol_sha256"
            ),
            "synchronizer_sha256": digest(Path(__file__)),
            "source_replay": "PASS",
            "evidence_archive": str(archive_path.relative_to(PROJECT_ROOT)),
            "registry_event_id": int(event["event_id"]),
            "promotion_eligible": False,
            "locked_validation_opened": False,
        }
        _atomic_json(RECEIPT_PATH, receipt)
    print(
        json.dumps(
            {
                "status": "APPENDED" if existing is None else "ALREADY_SYNCED",
                "event_id": event["event_id"],
                "snapshot": snapshot_key,
                "source_sha256": source_hash,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
