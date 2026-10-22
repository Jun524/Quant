#!/usr/bin/env python3
"""Replay and seal Alpha #11's one-shot locked-validation verdict."""

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
from scripts.analyze_alpha011_validation import (
    LOCK_PATH,
    OUTPUT as REPORT_PATH,
    validate_lock_chain,
    validation_protocol_digests,
)


CANDIDATE = "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION"
RECEIPT_PATH = PROJECT_ROOT / "reports" / "alpha011-validation-receipt.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "evidence" / "alpha011-validation"


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
            raise ValueError("Alpha #11 validation evidence hash collision")
        return source_hash, archive_path
    with tempfile.NamedTemporaryFile(
        mode="w+b", dir=archive_path.parent, prefix=".alpha011-validation-", delete=False
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


def _source_manifest(report: dict[str, Any], project_root: Path = PROJECT_ROOT) -> Path:
    path = Path(str(report.get("source_manifest") or ""))
    path = path if path.is_absolute() else project_root / path
    try:
        path.resolve().relative_to(
            (project_root / "research" / "alpha011" / "raw" / "BTCUSDT").resolve()
        )
    except ValueError as error:
        raise ValueError("Alpha #11 validation manifest escapes the sealed raw root") from error
    if not path.is_file():
        raise ValueError("Alpha #11 validation manifest is missing")
    return path


def replay_report(report: dict[str, Any], *, project_root: Path = PROJECT_ROOT) -> None:
    manifest_value = str(report.get("source_manifest") or "")
    with tempfile.TemporaryDirectory(prefix="alpha011-validation-replay-") as directory:
        replay_path = Path(directory) / "report.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "analyze_alpha011_validation.py"),
                "--manifest",
                manifest_value,
                "--lock",
                str(LOCK_PATH),
                "--output",
                str(replay_path),
            ],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ValueError(
                "Alpha #11 validation source replay failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        replayed = json.loads(replay_path.read_text(encoding="utf-8"))
    if replayed != report:
        raise ValueError("Alpha #11 validation report differs from exact source replay")


def build_registry_result(
    report: dict[str, Any],
    *,
    report_sha256: str,
    manifest_sha256: str,
    lock_sha256: str,
    evidence_archive: str,
) -> dict[str, Any]:
    status = str(report.get("status") or "")
    if status not in {"PASS", "FAIL"}:
        raise ValueError("Alpha #11 validation verdict is not terminal")
    base = ((report.get("validation") or {}).get("base") or {})
    checks = [
        {
            "name": str(item.get("name") or ""),
            "passed": bool(item.get("passed")),
            "actual": bool(item.get("passed")),
            "required": True,
            "severity": "fail",
        }
        for item in (report.get("gate") or {}).get("checks") or []
    ]
    return {
        "status": status,
        "validation_status": status,
        "promotion_eligible": False,
        "validation_opened": True,
        "locked_validation_opened": True,
        "source_report": "reports/alpha011-validation.json",
        "source_sha256": report_sha256,
        "source_manifest": report.get("source_manifest"),
        "source_manifest_sha256": manifest_sha256,
        "validation_lock_sha256": lock_sha256,
        "protocol_sha256": report.get("validation_protocol_sha256"),
        "evidence_archive": evidence_archive,
        "evidence_encoding": "gzip",
        "validation": {
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
        },
        "gate": {
            "status": status,
            "stage": "locked_forward_validation",
            "policy": {"min_oos_trades": 100, "min_profit_factor": 1.20},
            "checks": checks,
        },
        "warning": (
            "A locked-validation PASS is a research sleeve only; promotion_eligible remains "
            "false until portfolio and live-preflight gates independently pass."
        ),
    }


def receipt_health(
    *,
    receipt_path: Path = RECEIPT_PATH,
    report_path: Path = REPORT_PATH,
    lock_path: Path = LOCK_PATH,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        manifest_path = _source_manifest(report)
        lock_bytes = lock_path.read_bytes()
        lock = json.loads(lock_bytes)
        validate_lock_chain(lock, lock_bytes=lock_bytes)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return {"valid": False, "failures": [f"receipt_inputs_invalid:{type(error).__name__}"]}
    failures: list[str] = []
    protocol = validation_protocol_digests()
    checks = {
        "receipt_schema_or_status_invalid": receipt.get("schema_version") == 1
        and receipt.get("status") == "PASS",
        "report_sha256_mismatch": receipt.get("report_sha256") == digest(report_path),
        "manifest_sha256_mismatch": receipt.get("manifest_sha256") == digest(manifest_path),
        "lock_sha256_mismatch": receipt.get("lock_sha256") == sha256(lock_bytes).hexdigest(),
        "receipt_protocol_stale": receipt.get("protocol_sha256") == protocol,
        "report_protocol_stale": report.get("validation_protocol_sha256") == protocol,
        "synchronizer_sha256_mismatch": receipt.get("synchronizer_sha256") == digest(Path(__file__)),
        "source_replay_not_pass": receipt.get("source_replay") == "PASS",
        "promotion_eligibility_must_be_false": receipt.get("promotion_eligible") is False,
    }
    failures.extend(name for name, passed in checks.items() if not passed)
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
        or result.get("validation_lock_sha256") != receipt.get("lock_sha256")
        or result.get("validation_status") != receipt.get("report_status")
    ):
        failures.append("registry_event_binding_invalid")
    archive = PROJECT_ROOT / str(receipt.get("evidence_archive") or "")
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
    if report.get("status") == "ACCUMULATING_LOCKED_VALIDATION":
        print(json.dumps({"status": "VALIDATION_NOT_READY"}))
        return
    if report.get("candidate") != CANDIDATE or report.get("status") not in {"PASS", "FAIL"}:
        raise SystemExit("FAIL: Alpha #11 validation report is not terminal")
    lock_bytes = LOCK_PATH.read_bytes()
    lock = json.loads(lock_bytes)
    validate_lock_chain(lock, lock_bytes=lock_bytes)
    if report.get("lock_sha256") != sha256(lock_bytes).hexdigest():
        raise SystemExit("FAIL: Alpha #11 validation report is not bound to its lock")
    if report.get("validation_protocol_sha256") != validation_protocol_digests():
        raise SystemExit("FAIL: Alpha #11 validation protocol is stale")
    manifest_path = _source_manifest(report)
    if report.get("source_manifest_sha256") != digest(manifest_path):
        raise SystemExit("FAIL: Alpha #11 validation manifest digest mismatch")
    replay_report(report)
    report_sha256, archive_path = archive_report(REPORT_PATH, ARCHIVE_ROOT)
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiments = [item for item in registry.get("experiments") or [] if item.get("strategy") == CANDIDATE]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #11 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    existing = next(
        (
            item for item in registry.get("events") or []
            if int(item.get("experiment_id") or -1) == experiment_id
            and item.get("event_type") == "result_update"
            and (((item.get("payload") or {}).get("result") or {}).get("source_sha256")) == report_sha256
        ),
        None,
    )
    if existing is None:
        result = build_registry_result(
            report,
            report_sha256=report_sha256,
            manifest_sha256=digest(manifest_path),
            lock_sha256=sha256(lock_bytes).hexdigest(),
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
        "report_sha256": report_sha256,
        "manifest_sha256": digest(manifest_path),
        "lock_sha256": sha256(lock_bytes).hexdigest(),
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
