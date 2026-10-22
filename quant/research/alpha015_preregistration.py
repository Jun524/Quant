"""Build and replay Alpha 015's post-activation preregistration receipt."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any


class Alpha015PreregistrationError(RuntimeError):
    pass


def preregistration_artifact_paths() -> dict[str, Path]:
    """Return the exact project-relative source inventory for Experiment 0015."""
    return {
        "template": Path("research/alpha015-preregistration-template.json"),
        "methodology_audit": Path("research/alpha015-methodology-audit.json"),
        "methodology_amendment_002": Path("research/alpha015-methodology-amendment-002.json"),
        "methodology_amendment_003": Path("research/alpha015-methodology-amendment-003.json"),
        "data_amendment_004": Path("research/alpha015-data-amendment-004.json"),
        "funding_amendment_005": Path("research/alpha015-funding-amendment-005.json"),
        "rebalance_amendment_006": Path("research/alpha015-rebalance-amendment-006.json"),
        "timestamp_amendment_007": Path("research/alpha015-timestamp-amendment-007.json"),
        "acquisition_amendment_008": Path("research/alpha015-acquisition-amendment-008.json"),
        "selection_amendment_009": Path("research/alpha015-selection-amendment-009.json"),
        "risk_amendment_010": Path("research/alpha015-risk-amendment-010.json"),
        "contract_amendment_011": Path("research/alpha015-contract-amendment-011.json"),
        "activation_lock": Path("research/alpha015_activation/activation-lock.json"),
        "primary_source_manifest": Path("research/papers/alpha015-primary-source-manifest.json"),
        "primary_source_pdf": Path("research/papers/guo-sang-tu-wang-2024-cross-cryptocurrency-return-predictability-aam.pdf"),
        "causal_kernel": Path("quant/research/cross_crypto_predictor.py"),
        "simulation_engine": Path("quant/research/alpha015_simulation.py"),
        "development_statistics": Path("quant/research/alpha015_evaluation.py"),
        "daily_replay_engine": Path("quant/research/alpha015_replay.py"),
        "development_runner": Path("scripts/evaluate_alpha015_development.py"),
        "validation_unlock_runner": Path("scripts/open_alpha015_validation.py"),
        "validation_runner": Path("scripts/evaluate_alpha015_validation.py"),
        "funding_reconciliation_runner": Path("scripts/reconcile_alpha015_funding.py"),
        "acquisition_protocol": Path("quant/research/alpha015_acquisition.py"),
        "acquisition_runner": Path("scripts/acquire_alpha015.py"),
        "data_guard": Path("quant/research/alpha015_data_guard.py"),
        "execution_calibration": Path(
            "reports/execution-calibration-alpha001-entry-20261013T065131Z.json"
        ),
    }


def file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_alpha015_preregistration(
    *,
    template: dict[str, Any],
    activation_lock: dict[str, Any],
    artifact_paths: dict[str, Path],
    project_root: Path,
) -> dict[str, Any]:
    """Bind the locked template to the exact activation and method sources."""
    if template.get("status") != "LOCKED_TEMPLATE_NOT_AN_EXPERIMENT":
        raise Alpha015PreregistrationError("preregistration template status is invalid")
    if activation_lock.get("status") != "LOCKED_FOR_PREREGISTRATION_ONLY":
        raise Alpha015PreregistrationError("activation lock is not preregistration-ready")
    if (
        int(activation_lock.get("formation_snapshot_count") or 0) != 180
        or len(activation_lock.get("development") or []) != 120
        or len(activation_lock.get("locked_validation") or []) != 60
        or activation_lock.get("validation_open") is not False
    ):
        raise Alpha015PreregistrationError("activation lock calendar is incomplete")
    required_names = {
        "template",
        "methodology_audit",
        "methodology_amendment_002",
        "methodology_amendment_003",
        "data_amendment_004",
        "funding_amendment_005",
        "rebalance_amendment_006",
        "timestamp_amendment_007",
        "acquisition_amendment_008",
        "selection_amendment_009",
        "risk_amendment_010",
        "contract_amendment_011",
        "activation_lock",
        "primary_source_manifest",
        "primary_source_pdf",
        "causal_kernel",
        "simulation_engine",
        "development_statistics",
        "daily_replay_engine",
        "development_runner",
        "validation_unlock_runner",
        "validation_runner",
        "funding_reconciliation_runner",
        "acquisition_protocol",
        "acquisition_runner",
        "data_guard",
        "execution_calibration",
    }
    if set(artifact_paths) != required_names:
        raise Alpha015PreregistrationError("preregistration artifact inventory is not exact")
    artifacts: dict[str, dict[str, str]] = {}
    root = project_root.resolve()
    resolved_paths: dict[str, Path] = {}
    for name, path in sorted(artifact_paths.items()):
        actual = path if path.is_absolute() else root / path
        actual = actual.resolve()
        try:
            relative = actual.relative_to(root)
        except ValueError as error:
            raise Alpha015PreregistrationError(
                f"preregistration artifact escapes project root: {name}"
            ) from error
        if not actual.is_file():
            raise Alpha015PreregistrationError(f"preregistration artifact missing: {name}")
        resolved_paths[name] = actual
        artifacts[name] = {"path": str(relative), "sha256": file_sha256(actual)}
    lock_body = {
        key: value for key, value in activation_lock.items() if key != "lock_sha256"
    }
    if activation_lock.get("lock_sha256") != canonical_sha256(lock_body):
        raise Alpha015PreregistrationError("activation lock self-digest mismatch")

    body = {
        "schema_version": 1,
        "candidate": template["candidate"],
        "status": "PREREGISTERED_SOURCE_ACQUISITION_LOCKED",
        "registered_at_source_cutoff": activation_lock["source_cutoff_at_utc"],
        "hypothesis": template["hypothesis"],
        "protocol": {
            key: value
            for key, value in template.items()
            if key
            in {
                "activation",
                "model",
                "portfolio",
                "costs",
                "development_gate",
                "locked_validation_gate",
                "post_validation_requirements",
                "prohibited",
            }
        },
        "activation_binding": {
            "lock_sha256": activation_lock["lock_sha256"],
            "split_sha256": activation_lock["split_sha256"],
            "formation_start": activation_lock["formation_start"],
            "formation_end": activation_lock["formation_end"],
            "evaluation_start": activation_lock["evaluation_start"],
            "evaluation_end": activation_lock["evaluation_end"],
        },
        "artifacts": artifacts,
        "return_data_acquired": False,
        "locked_validation_open": False,
        "live_order_authorization": "CLOSED",
    }
    return {**body, "preregistration_sha256": canonical_sha256(body)}


def verify_alpha015_preregistration(
    *,
    stored: dict[str, Any],
    expected: dict[str, Any],
    registry: dict[str, Any],
) -> bool:
    records = [
        item
        for item in registry.get("experiments", [])
        if int(((item.get("trial_accounting") or {}).get("alpha_number") or -1)) == 15
        or str(item.get("strategy") or "").startswith("ALPHA_015")
    ]
    if len(records) != 1 or stored != expected:
        return False
    record = records[0]
    result = record.get("result") or {}
    return (
        record.get("strategy") == expected.get("candidate")
        and record.get("hypothesis") == expected.get("hypothesis")
        and result.get("source_preregistration") == "research/alpha015-preregistration.json"
        and result.get("preregistration_sha256") == expected.get("preregistration_sha256")
        and result.get("activation_lock_sha256")
        == (expected.get("activation_binding") or {}).get("lock_sha256")
        and result.get("return_data_acquired") is False
        and result.get("validation_opened") is False
        and (record.get("trial_accounting") or {}).get("family")
        == "minute_cross_crypto_predictability"
        and (record.get("trial_accounting") or {}).get("family_trial") == 1
    )
