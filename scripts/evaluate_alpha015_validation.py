#!/usr/bin/env python3
"""Replay Alpha 015's sealed 60-day validation sample without promotion.

The command deliberately requires a separate, publisher-bound all-180-day
funding-reconciliation receipt.  A favourable validation return alone is not
enough to issue even a research REVIEW.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_evaluation import evaluate_alpha015_locked_validation
from quant.research.alpha015_replay import (
    Alpha015ReplayError,
    prepare_alpha015_phase_bundle,
    replay_alpha015_day,
)
from scripts.open_alpha015_validation import validate_validation_unlock
from scripts.reconcile_alpha015_funding import verify_reconciliation_receipt


DEVELOPMENT_REPORT_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "report.json"
UNLOCK_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "validation-unlock.json"
PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha015-preregistration.json"
ACTIVATION_LOCK_PATH = PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
VALIDATION_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / "locked_validation"
MANIFEST_PATH = VALIDATION_ROOT / "manifest.json"
FUNDING_RECONCILIATION_PATH = PROJECT_ROOT / "research" / "alpha015_funding_reconciliation" / "report.json"
REPORT_PATH = PROJECT_ROOT / "research" / "alpha015_validation" / "report.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise Alpha015ReplayError(f"required Alpha 015 artifact is unavailable: {path.name}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha015ReplayError(f"invalid Alpha 015 artifact: {path.name}") from error
    if not isinstance(value, dict):
        raise Alpha015ReplayError(f"Alpha 015 artifact is not an object: {path.name}")
    return value


def _source_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_once(path: Path, payload: dict[str, Any]) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise Alpha015ReplayError("existing locked-validation report differs from replay")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _no_retuning(
    *, preregistration: dict[str, Any], development_report: dict[str, Any], activation_lock: dict[str, Any]
) -> bool:
    """Ensure the held-out replay uses precisely preregistered method bytes."""
    artifacts = preregistration.get("artifacts") or {}
    expected_replay = ((artifacts.get("daily_replay_engine") or {}).get("sha256"))
    expected_evaluation = ((artifacts.get("development_statistics") or {}).get("sha256"))
    return (
        expected_replay == _source_digest(PROJECT_ROOT / "quant" / "research" / "alpha015_replay.py")
        and expected_evaluation
        == _source_digest(PROJECT_ROOT / "quant" / "research" / "alpha015_evaluation.py")
        and (development_report.get("source") or {}).get("replay_engine_sha256") == expected_replay
        and (development_report.get("source") or {}).get("activation_lock_sha256")
        == activation_lock.get("lock_sha256")
    )


def _funding_receipt_passes(
    receipt: dict[str, Any],
    *, preregistration_sha256: str, development_report_sha256: str, activation_lock_sha256: str,
) -> bool:
    return (
        verify_reconciliation_receipt(receipt)
        and
        receipt.get("status") == "PASS"
        and receipt.get("preregistration_sha256") == preregistration_sha256
        and receipt.get("development_report_sha256") == development_report_sha256
        and receipt.get("activation_lock_sha256") == activation_lock_sha256
        and receipt.get("authorized_evaluation_days") == 180
        and receipt.get("all_daily_funding_events_reconciled") is True
        and isinstance(receipt.get("source_sha256"), str)
        and len(receipt["source_sha256"]) == 64
    )


def main() -> None:
    preregistration = _load(PREREGISTRATION_PATH)
    activation_lock = _load(ACTIVATION_LOCK_PATH)
    development_bytes = DEVELOPMENT_REPORT_PATH.read_bytes()
    development = _load(DEVELOPMENT_REPORT_PATH)
    development_sha256 = sha256(development_bytes).hexdigest()
    unlock = _load(UNLOCK_PATH)
    if not validate_validation_unlock(
        unlock,
        report=development,
        preregistration=preregistration,
        activation_lock=activation_lock,
        report_sha256=development_sha256,
    ):
        raise Alpha015ReplayError("locked-validation unlock receipt is invalid")
    funding = _load(FUNDING_RECONCILIATION_PATH)
    funding_passed = _funding_receipt_passes(
        funding,
        preregistration_sha256=str(preregistration.get("preregistration_sha256") or ""),
        development_report_sha256=development_sha256,
        activation_lock_sha256=str(activation_lock.get("lock_sha256") or ""),
    )
    if not funding_passed:
        raise Alpha015ReplayError("all-180-day publisher funding reconciliation is not PASS")
    manifest = _load(MANIFEST_PATH)
    bundle = prepare_alpha015_phase_bundle(
        manifest=manifest,
        activation_lock=activation_lock,
        archive_root=VALIDATION_ROOT,
        phase="locked_validation",
        expected_preregistration_sha256=str(preregistration["preregistration_sha256"]),
        validation_unlock=unlock,
        expected_development_report_sha256=development_sha256,
    )
    replays = [
        replay_alpha015_day(
            bundle,
            activation_lock=activation_lock,
            evaluation_day=day,
            verify_future_row_invariance=True,
        )
        for day in activation_lock["locked_validation"]
    ]
    report = evaluate_alpha015_locked_validation(
        replays,
        candidate=str(preregistration["candidate"]),
        all_funding_reconciled=True,
        no_parameter_or_universe_retuning=_no_retuning(
            preregistration=preregistration,
            development_report=development,
            activation_lock=activation_lock,
        ),
        lookahead_analysis_passed=True,
        future_row_invariance_passed=True,
    )
    report["source"] = {
        "preregistration_path": str(PREREGISTRATION_PATH.relative_to(PROJECT_ROOT)),
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "activation_lock_sha256": activation_lock["lock_sha256"],
        "development_report_sha256": development_sha256,
        "validation_manifest_sha256": _source_digest(MANIFEST_PATH),
        "funding_reconciliation_path": str(FUNDING_RECONCILIATION_PATH.relative_to(PROJECT_ROOT)),
        "funding_reconciliation_sha256": _source_digest(FUNDING_RECONCILIATION_PATH),
    }
    _write_once(REPORT_PATH, report)
    print(json.dumps({"status": report["status"], "report": str(REPORT_PATH)}, indent=2))
    if report["status"] != "REVIEW":
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except (Alpha015ReplayError, OSError) as error:
        raise SystemExit(f"Alpha 015 locked validation replay blocked: {error}") from error
