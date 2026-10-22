#!/usr/bin/env python3
"""Append compact Alpha #7 accumulation changes as immutable registry events."""

from __future__ import annotations

import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


EVIDENCE_ENCODING = "gzip"
REPORT_PATH = PROJECT_ROOT / "reports" / "alpha007-accumulation.json"
RECEIPT_PATH = PROJECT_ROOT / "reports" / "alpha007-terminal-receipt.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
RAW_ROOT = PROJECT_ROOT / "research" / "microstructure"
PARTIAL_FILL_REPORT_PATH = PROJECT_ROOT / "reports" / "alpha007-partial-fill-stress.json"
TERMINAL_STATUSES = {"PASS", "FAIL"}


def archive_report(report_path: Path, archive_root: Path) -> tuple[str, Path]:
    """Seal the exact report bytes outside the registry and return its digest/path."""
    payload = report_path.read_bytes()
    source_hash = sha256(payload).hexdigest()
    archive_path = archive_root / f"{source_hash}.json.gz"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if gzip.decompress(archive_path.read_bytes()) != payload:
            raise ValueError(f"Alpha #7 evidence archive hash collision: {archive_path}")
        return source_hash, archive_path

    with tempfile.NamedTemporaryFile(
        mode="w+b",
        dir=archive_path.parent,
        prefix=".alpha007-evidence-",
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


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_once_json(path: Path, payload: dict) -> bool:
    """Create an immutable receipt, accepting only an exact idempotent replay."""
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("existing Alpha #7 terminal receipt is unreadable") from error
        if existing != payload:
            raise ValueError("Alpha #7 terminal receipt is already sealed with different evidence")
        return False
    _atomic_json(path, payload)
    return True


def registry_status(report_status: str) -> str:
    mapping = {"ACCUMULATING": "ACCUMULATING", "PASS": "REVIEW", "FAIL": "FAIL"}
    if report_status not in mapping:
        raise ValueError(f"unsupported Alpha #7 status: {report_status}")
    return mapping[report_status]


def replay_report(
    report: dict,
    *,
    raw_root: Path = RAW_ROOT,
    project_root: Path = PROJECT_ROOT,
) -> None:
    """Require terminal evidence to be byte-equivalent to a fresh source replay."""
    from quant.research.microstructure import analyze_microstructure
    from scripts.analyze_microstructure import audit_complete_day_protocols

    replayed = analyze_microstructure(raw_root)
    replayed["complete_day_protocol_audit"] = audit_complete_day_protocols(
        raw_root, replayed, project_root=project_root
    )
    if replayed != report:
        raise ValueError("Alpha #7 report differs from exact source replay")


def compact_accumulation(report: dict) -> dict:
    """Keep decision evidence in the registry without duplicating every run row."""
    integrity = report.get("data_integrity") or {}
    verified_runs = integrity.get("verified_runs") or []
    compact_integrity = {
        key: value for key, value in integrity.items() if key != "verified_runs"
    }
    compact_integrity["verified_run_count"] = len(verified_runs)
    keys = (
        "candidate",
        "status",
        "basis",
        "configuration",
        "runs",
        "eligible_timing_runs",
        "excluded_timing_runs",
        "collection_totals",
        "coverage",
        "complete_days",
        "all_detected_signals",
        "incomplete_day_signals_excluded",
        "phases",
        "checks",
        "warning",
    )
    result = {key: report.get(key) for key in keys}
    result["data_integrity"] = compact_integrity
    return result


def event_has_compact_archive(event: dict, source_hash: str) -> bool:
    """Return true only for a same-report event written by the compact schema."""
    payload = event.get("payload") or {}
    result = payload.get("result") or {}
    accumulation = result.get("accumulation") or {}
    integrity = accumulation.get("data_integrity") or {}
    return bool(
        payload.get("source_sha256") == source_hash
        and result.get("source_sha256") == source_hash
        and result.get("evidence_archive")
        and result.get("evidence_encoding") == EVIDENCE_ENCODING
        and "verified_runs" not in integrity
    )


def build_registry_result(
    report: dict,
    *,
    source_hash: str,
    evidence_archive: str,
) -> dict:
    status = registry_status(str(report.get("status") or ""))
    return {
        "status": status,
        "shadow_status": report["status"],
        "promotion_eligible": False,
        "validation_opened": report["status"] in TERMINAL_STATUSES,
        "locked_validation_opened": report["status"] in TERMINAL_STATUSES,
        "source_report": "reports/alpha007-accumulation.json",
        "source_sha256": source_hash,
        "evidence_archive": evidence_archive,
        "evidence_encoding": EVIDENCE_ENCODING,
        "accumulation": compact_accumulation(report),
        "development": {},
        "gate": {
            "status": status,
            "stage": "forward_collection"
            if report["status"] == "ACCUMULATING"
            else "locked_oos",
            "checks": report["checks"],
        },
        "warning": (
            "A shadow-maker PASS is REVIEW only. It cannot become a portfolio sleeve "
            "without the separate 10bp cost stress, partial-fill veto, and an "
            "order-path dry-run gate."
            if report["status"] == "PASS"
            else "Alpha #7 has not produced live-promotion evidence."
        ),
    }


def receipt_health(
    *,
    receipt_path: Path = RECEIPT_PATH,
    report_path: Path = REPORT_PATH,
    registry_path: Path = REGISTRY_PATH,
    raw_root: Path = RAW_ROOT,
    project_root: Path = PROJECT_ROOT,
    analyzer_path: Path | None = None,
    synchronizer_path: Path | None = None,
) -> dict:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {"valid": False, "failures": [f"receipt_inputs_invalid:{type(error).__name__}"]}
    analyzer_path = analyzer_path or project_root / "scripts" / "analyze_microstructure.py"
    synchronizer_path = synchronizer_path or Path(__file__)
    failures: list[str] = []
    if receipt.get("schema_version") != 1 or receipt.get("status") != "PASS":
        failures.append("receipt_schema_or_status_invalid")
    for label, path in (
        ("report", report_path),
        ("analyzer", analyzer_path),
        ("synchronizer", synchronizer_path),
    ):
        try:
            current_digest = digest(path)
        except OSError:
            failures.append(f"{label}_artifact_missing")
            continue
        if receipt.get(f"{label}_sha256") != current_digest:
            failures.append(f"{label}_sha256_mismatch")
    if receipt.get("source_replay") != "PASS":
        failures.append("source_replay_not_pass")
    if receipt.get("promotion_eligible") is not False:
        failures.append("promotion_eligibility_must_be_false")
    report_status = report.get("status")
    try:
        complete_days = int(report.get("complete_days") or 0)
    except (TypeError, ValueError):
        complete_days = -1
    if report_status not in TERMINAL_STATUSES or complete_days < 17:
        failures.append("report_not_terminal")
    if receipt.get("report_status") != report_status:
        failures.append("receipt_report_status_mismatch")
    if receipt.get("candidate") != report.get("candidate"):
        failures.append("receipt_candidate_mismatch")
    protocol_audit = report.get("complete_day_protocol_audit") or {}
    if protocol_audit.get("status") != "PASS":
        failures.append("complete_day_protocol_audit_not_pass")
    partial_fill_status = None
    if report_status == "PASS":
        partial_path = project_root / "reports" / "alpha007-partial-fill-stress.json"
        partial_policy_path = (
            project_root / "research" / "alpha007-partial-fill-stress-policy.json"
        )
        partial_evaluator_path = (
            project_root / "quant" / "research" / "partial_fill_stress.py"
        )
        partial_builder_path = project_root / "scripts" / "alpha007_partial_fill_stress.py"
        frozen_evaluator_path = project_root / "quant" / "research" / "microstructure.py"
        try:
            partial = json.loads(partial_path.read_text(encoding="utf-8"))
            partial_digest = digest(partial_path)
            partial_fill_status = partial.get("status")
            evaluator_hashes = partial.get("evaluator_sha256") or {}
            if (
                receipt.get("partial_fill_report_sha256") != partial_digest
                or receipt.get("partial_fill_veto_status") != partial_fill_status
                or receipt.get("partial_fill_policy_sha256")
                != partial.get("policy_sha256")
                or partial_fill_status not in {"PASS", "FAIL"}
                or partial.get("promotion_eligible") is not False
                or partial.get("primary_report_sha256") != digest(report_path)
                or partial.get("policy_sha256") != digest(partial_policy_path)
                or evaluator_hashes.get("partial_fill_stress")
                != digest(partial_evaluator_path)
                or evaluator_hashes.get("report_builder") != digest(partial_builder_path)
                or evaluator_hashes.get("frozen_primary_evaluator")
                != digest(frozen_evaluator_path)
                or (partial.get("registry_binding") or {}).get("before_locked_oos")
                is not True
            ):
                failures.append("partial_fill_veto_binding_invalid")
        except (OSError, json.JSONDecodeError):
            failures.append("partial_fill_veto_missing_or_invalid")
    raw_event_id = receipt.get("registry_event_id")
    event_id = raw_event_id if isinstance(raw_event_id, int) and not isinstance(raw_event_id, bool) else None
    if event_id is None:
        failures.append("registry_event_id_invalid")
    events = registry.get("events") or []
    if not isinstance(events, list):
        events = []
        failures.append("registry_events_invalid")
    event = next(
        (
            item
            for item in events
            if isinstance(item, dict) and item.get("event_id") == event_id
        ),
        None,
    )
    result = ((event or {}).get("payload") or {}).get("result") or {}
    expected_registry_status = (
        registry_status(str(report_status)) if report_status in TERMINAL_STATUSES else None
    )
    if (
        event is None
        or event.get("event_type") != "result_update"
        or result.get("source_sha256") != receipt.get("report_sha256")
        or result.get("shadow_status") != receipt.get("report_status")
        or result.get("status") != expected_registry_status
        or result.get("promotion_eligible") is not False
        or ((result.get("accumulation") or {}).get("candidate"))
        != report.get("candidate")
    ):
        failures.append("registry_event_binding_invalid")
    archive_root = project_root / "research" / "evidence" / "alpha007"
    archive = project_root / str(receipt.get("evidence_archive") or "")
    try:
        archive.resolve().relative_to(archive_root.resolve())
        if gzip.decompress(archive.read_bytes()) != report_path.read_bytes():
            failures.append("evidence_archive_mismatch")
    except (OSError, ValueError, gzip.BadGzipFile):
        failures.append("evidence_archive_invalid")
    run_inventory = receipt.get("verified_run_manifests")
    if not isinstance(run_inventory, list) or not run_inventory:
        failures.append("run_manifest_inventory_missing")
    else:
        inventory_items_valid = all(isinstance(item, dict) for item in run_inventory)
        recorded_paths = [
            str(item.get("path") or "")
            for item in run_inventory
            if isinstance(item, dict)
        ]
        current_paths = {
            str(path.relative_to(project_root))
            for path in sorted(raw_root.glob("*/manifest.json"))
        }
        if (
            not inventory_items_valid
            or "" in recorded_paths
            or len(recorded_paths) != len(set(recorded_paths))
            or set(recorded_paths) != current_paths
        ):
            failures.append("run_manifest_inventory_not_exact")
        for item in run_inventory:
            if not isinstance(item, dict):
                break
            path = project_root / str(item.get("path") or "")
            try:
                path.resolve().relative_to(raw_root.resolve())
                current = digest(path)
            except (OSError, ValueError):
                failures.append("run_manifest_inventory_invalid")
                break
            if current != item.get("sha256"):
                failures.append("run_manifest_digest_mismatch")
                break
    return {
        "valid": not failures,
        "failures": failures,
        "report_status": receipt.get("report_status"),
        "partial_fill_veto_status": partial_fill_status,
        "registry_event_id": receipt.get("registry_event_id"),
        "report_sha256": receipt.get("report_sha256"),
    }


def main() -> None:
    registry_path = REGISTRY_PATH
    report_path = REPORT_PATH
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    experiment = next(
        (item for item in data.get("experiments", []) if item.get("strategy") == "ALPHA_007_IMBALANCE_GUIDED_MAKER"),
        None,
    )
    if experiment is None:
        raise SystemExit("Alpha #7 is not registered; run make register-phase3 first")
    source_hash = sha256(report_path.read_bytes()).hexdigest()
    prior = [
        event
        for event in data.get("events", [])
        if event.get("experiment_id") == experiment["experiment_id"] and event.get("event_type") == "result_update"
    ]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    terminal = report.get("status") in TERMINAL_STATUSES
    if prior and event_has_compact_archive(prior[-1], source_hash) and not terminal:
        print(json.dumps({"status": "UNCHANGED", "source_sha256": source_hash}, indent=2))
        return
    if terminal:
        if int(report.get("complete_days") or 0) < 17:
            raise SystemExit("FAIL: Alpha #7 terminal report has fewer than 17 complete days")
        try:
            replay_report(report)
        except ValueError as error:
            raise SystemExit(f"FAIL: {error}") from error
    partial_fill_report = None
    if terminal and report.get("status") == "PASS":
        from scripts.alpha007_partial_fill_stress import build_report as build_partial_fill_report

        try:
            partial_fill_report = build_partial_fill_report()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise SystemExit(f"FAIL: Alpha #7 partial-fill veto replay failed: {error}") from error
        _atomic_json(PARTIAL_FILL_REPORT_PATH, partial_fill_report)
    archived_hash, archive_path = archive_report(
        report_path,
        PROJECT_ROOT / "research" / "evidence" / "alpha007",
    )
    if archived_hash != source_hash:
        raise ValueError("Alpha #7 report changed while it was being archived")
    status = registry_status(str(report.get("status") or ""))
    result = build_registry_result(
        report,
        source_hash=source_hash,
        evidence_archive=str(archive_path.relative_to(PROJECT_ROOT)),
    )
    existing = next(
        (
            event for event in prior
            if (((event.get("payload") or {}).get("result") or {}).get("source_sha256"))
            == source_hash
            and (((event.get("payload") or {}).get("result") or {}).get("status"))
            == status
        ),
        None,
    )
    if existing is None:
        event = ExperimentRegistry(registry_path).append_event(
            experiment_id=int(experiment["experiment_id"]),
            event_type="result_update",
            payload={"source_sha256": source_hash, "result": result},
        )
    else:
        event = existing
    if terminal:
        run_inventory = [
            {
                "path": str(path.relative_to(PROJECT_ROOT)),
                "sha256": digest(path),
            }
            for path in sorted(RAW_ROOT.glob("*/manifest.json"))
        ]
        receipt = {
            "schema_version": 1,
            "status": "PASS",
            "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
            "report_status": report["status"],
            "report_sha256": source_hash,
            "analyzer_sha256": digest(PROJECT_ROOT / "scripts" / "analyze_microstructure.py"),
            "synchronizer_sha256": digest(Path(__file__)),
            "source_replay": "PASS",
            "verified_run_manifests": run_inventory,
            "evidence_archive": str(archive_path.relative_to(PROJECT_ROOT)),
            "registry_event_id": int(event["event_id"]),
            "promotion_eligible": False,
        }
        if partial_fill_report is not None:
            receipt.update(
                {
                    "partial_fill_veto_status": partial_fill_report["status"],
                    "partial_fill_report_sha256": digest(PARTIAL_FILL_REPORT_PATH),
                    "partial_fill_policy_sha256": partial_fill_report["policy_sha256"],
                }
            )
        try:
            write_once_json(RECEIPT_PATH, receipt)
        except ValueError as error:
            raise SystemExit(f"FAIL: {error}") from error
    print(
        json.dumps(
            {
                "status": "APPENDED" if existing is None else "ALREADY_SYNCED",
                "event_id": event["event_id"],
                "source_sha256": source_hash,
                "registry_status": status,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
