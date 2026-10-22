#!/usr/bin/env python3
"""Replay, archive, and register Alpha #13's locked-validation verdict."""

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
from scripts.analyze_alpha013_validation import (
    LOCK_PATH,
    OUTPUT as REPORT_PATH,
    RAW_ROOT,
    validate_lock_chain,
    validation_protocol_digests,
)


CANDIDATE = "ALPHA_013_FUNDING_DISPERSION_REVERSAL"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
RECEIPT_PATH = PROJECT_ROOT / "reports" / "alpha013-validation-receipt.json"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "evidence" / "alpha013-validation"


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


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
            raise ValueError("Alpha #13 validation evidence hash collision")
        return source_hash, archive_path
    with tempfile.NamedTemporaryFile(
        mode="w+b", dir=archive_path.parent, prefix=".alpha013-validation-", delete=False
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


def replay_report(report: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="alpha013-validation-replay-") as directory:
        replay_path = Path(directory) / "report.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "analyze_alpha013_validation.py"),
                "--lock",
                str(LOCK_PATH),
                "--raw-root",
                str(RAW_ROOT),
                "--output",
                str(replay_path),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                "Alpha #13 validation replay failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        replayed = json.loads(replay_path.read_text(encoding="utf-8"))
    if replayed != report:
        raise ValueError("Alpha #13 validation report differs from exact source replay")


def build_registry_result(
    report: dict[str, Any],
    *,
    report_sha256: str,
    lock_sha256: str,
    evidence_archive: str,
) -> dict[str, Any]:
    status = str(report.get("status") or "")
    if status not in {"PASS", "FAIL"}:
        raise ValueError("Alpha #13 validation report is not terminal")
    base = ((report.get("validation") or {}).get("base") or {})
    components = base.get("pnl_components") or {}
    return {
        "status": status,
        "validation_status": status,
        "promotion_eligible": False,
        "validation_opened": True,
        "locked_validation_opened": True,
        "source_report": "reports/alpha013-validation.json",
        "source_sha256": report_sha256,
        "validation_lock_sha256": lock_sha256,
        "source_inventory_sha256": (report.get("data_integrity") or {}).get(
            "source_inventory_sha256"
        ),
        "protocol_sha256": report.get("validation_protocol_sha256"),
        "evidence_archive": evidence_archive,
        "evidence_encoding": "gzip",
        "validation": {
            "organic": {
                "trades": int(base.get("eligible_weeks") or 0),
                "closed_legs": int(base.get("closed_legs") or 0),
                "net_profit": float(components.get("net_pnl") or 0.0),
                "net_return": float(base.get("net_return") or 0.0),
                "profit_factor": base.get("profit_factor_by_week"),
            },
            "pnl_components": {
                "price_pnl_estimate": float(components.get("raw_relative_price_pnl") or 0.0),
                "trading_fees": float(components.get("trading_fees") or 0.0),
                "funding_pnl": float(components.get("funding_pnl") or 0.0),
                "modeled_slippage": float(components.get("modeled_slippage") or 0.0),
                "net_pnl": float(components.get("net_pnl") or 0.0),
            },
        },
        "gate": {
            "status": status,
            "stage": "locked_forward_validation",
            "policy": {"min_oos_trades": 26, "min_profit_factor": 1.25},
            "checks": [
                {
                    "name": str(name),
                    "passed": bool(passed),
                    "actual": bool(passed),
                    "required": True,
                    "severity": "fail",
                }
                for name, passed in (report.get("gate") or {}).items()
            ],
        },
        "warning": "Validation PASS is a sleeve result; portfolio and live gates remain mandatory.",
    }


def receipt_health(
    *,
    receipt_path: Path = RECEIPT_PATH,
    report_path: Path = REPORT_PATH,
    lock_path: Path = LOCK_PATH,
    registry_path: Path = REGISTRY_PATH,
    project_root: Path = PROJECT_ROOT,
    synchronizer_path: Path | None = None,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        validate_lock_chain(lock, lock_bytes=lock_bytes)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {"valid": False, "failures": [f"receipt_inputs_invalid:{type(error).__name__}"]}
    failures: list[str] = []
    synchronizer_path = synchronizer_path or Path(__file__)
    expected = {
        "receipt_schema_or_status_invalid": receipt.get("schema_version") == 1
        and receipt.get("status") == "PASS",
        "report_sha256_mismatch": receipt.get("report_sha256") == digest(report_path),
        "lock_sha256_mismatch": receipt.get("lock_sha256") == sha256(lock_bytes).hexdigest(),
        "source_inventory_sha256_mismatch": receipt.get("source_inventory_sha256")
        == (report.get("data_integrity") or {}).get("source_inventory_sha256"),
        "protocol_sha256_mismatch": receipt.get("protocol_sha256")
        == validation_protocol_digests(),
        "synchronizer_sha256_mismatch": receipt.get("synchronizer_sha256")
        == digest(synchronizer_path),
        "source_replay_not_pass": receipt.get("source_replay") == "PASS",
        "promotion_eligibility_must_be_false": receipt.get("promotion_eligible") is False,
    }
    failures.extend(name for name, passed in expected.items() if not passed)
    event_id = int(receipt.get("registry_event_id") or -1)
    event = next(
        (item for item in registry.get("events") or [] if int(item.get("event_id") or -2) == event_id),
        None,
    )
    result = ((event or {}).get("payload") or {}).get("result") or {}
    if (
        event is None
        or result.get("source_sha256") != receipt.get("report_sha256")
        or result.get("validation_lock_sha256") != receipt.get("lock_sha256")
        or result.get("validation_status") != receipt.get("report_status")
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
    }


def main() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    if report.get("status") in {
        "ACCUMULATING_LOCKED_VALIDATION",
        "WAITING_LOCKED_VALIDATION_FUNDING_RECONCILIATION",
    }:
        print(json.dumps({"status": "VALIDATION_NOT_READY"}))
        return
    if report.get("candidate") != CANDIDATE or report.get("status") not in {"PASS", "FAIL"}:
        raise SystemExit("FAIL: Alpha #13 validation report is not terminal")
    lock_bytes = LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    validate_lock_chain(lock, lock_bytes=lock_bytes)
    if report.get("lock_sha256") != sha256(lock_bytes).hexdigest():
        raise SystemExit("FAIL: Alpha #13 validation report lock mismatch")
    if report.get("validation_protocol_sha256") != validation_protocol_digests():
        raise SystemExit("FAIL: Alpha #13 validation protocol is stale")
    try:
        replay_report(report)
    except ValueError as error:
        raise SystemExit(f"FAIL: {error}") from error
    source_hash, archive_path = archive_report(REPORT_PATH, ARCHIVE_ROOT)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments") or []
        if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #13 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
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
            lock_sha256=sha256(lock_bytes).hexdigest(),
            evidence_archive=str(archive_path.relative_to(PROJECT_ROOT)),
        )
        event = ExperimentRegistry(REGISTRY_PATH).append_event(
            experiment_id=experiment_id,
            event_type="result_update",
            payload={"source_sha256": source_hash, "result": result},
        )
    else:
        event = existing
    receipt = {
        "schema_version": 1,
        "status": "PASS",
        "candidate": CANDIDATE,
        "report_status": report["status"],
        "report_sha256": source_hash,
        "lock_sha256": sha256(lock_bytes).hexdigest(),
        "source_inventory_sha256": (report.get("data_integrity") or {}).get(
            "source_inventory_sha256"
        ),
        "protocol_sha256": validation_protocol_digests(),
        "synchronizer_sha256": digest(Path(__file__)),
        "source_replay": "PASS",
        "evidence_archive": str(archive_path.relative_to(PROJECT_ROOT)),
        "registry_event_id": int(event["event_id"]),
        "promotion_eligible": False,
    }
    _atomic_json(RECEIPT_PATH, receipt)
    print(json.dumps({"status": "SYNCED" if existing is None else "ALREADY_SYNCED", "registry_event_id": event["event_id"]}, indent=2))


if __name__ == "__main__":
    main()
