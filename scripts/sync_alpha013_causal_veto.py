#!/usr/bin/env python3
"""Seal an Alpha #13 causal-availability veto in the append-only registry.

This is deliberately separate from forward PnL synchronisation.  A missing
decision-time receipt invalidates the prospective evidence before any return
can be evaluated; it is not evidence that the trading hypothesis lost money.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry
from scripts.sync_alpha013_registry import archive_report, digest


CANDIDATE = "ALPHA_013_FUNDING_DISPERSION_REVERSAL"
AUDIT_PATH = PROJECT_ROOT / "reports" / "alpha013-decision-availability.json"
PREREGISTRATION_PATH = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
ARCHIVE_ROOT = PROJECT_ROOT / "research" / "evidence" / "alpha013-causal-availability"


def validate_audit(audit: dict[str, Any], *, preregistration_sha256: str) -> None:
    """Accept only a bound, elapsed-decision causal availability veto."""

    required = {
        "schema_version": 1,
        "kind": "FUNDING_DECISION_AVAILABILITY_AUDIT",
        "status": "CAUSAL_AVAILABILITY_VETO",
        "preregistration_sha256": preregistration_sha256,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
    }
    if any(audit.get(key) != value for key, value in required.items()):
        raise ValueError("Alpha #13 causal-availability audit contract mismatch")
    if int(audit.get("elapsed_decisions") or 0) <= 0:
        raise ValueError("Alpha #13 causal-availability audit has no elapsed decision")
    failures = audit.get("failures")
    if not isinstance(failures, list) or not failures:
        raise ValueError("Alpha #13 causal-availability audit has no veto reason")


def build_registry_result(
    audit: dict[str, Any],
    *,
    audit_sha256: str,
    evidence_archive: str,
    preregistration_sha256: str,
) -> dict[str, Any]:
    """Produce a non-performance terminal result for the public scoreboard."""

    return {
        "status": "FAIL",
        "development_status": "CAUSAL_AVAILABILITY_VETO",
        "promotion_eligible": False,
        "validation_opened": False,
        "locked_validation_opened": False,
        "non_performance_veto": True,
        "source_report": "reports/alpha013-decision-availability.json",
        "source_sha256": audit_sha256,
        "evidence_archive": evidence_archive,
        "evidence_encoding": "gzip",
        "preregistration_path": "research/alpha013-preregistration.json",
        "preregistration_sha256": preregistration_sha256,
        "causal_availability": {
            "elapsed_decisions": int(audit["elapsed_decisions"]),
            "verified_decisions": list(audit.get("verified_decisions") or []),
            "failures": list(audit["failures"]),
        },
        "failure_reasons": ["CAUSAL_AVAILABILITY_VETO"],
        "primary_failure": "CAUSAL_AVAILABILITY_VETO",
        "development": {},
        "gate": {
            "status": "FAIL",
            "stage": "forward_decision_availability",
            "checks": [
                {
                    "name": "every_elapsed_decision_has_pre_entry_funding_receipt",
                    "passed": False,
                    "actual": False,
                    "required": True,
                    "severity": "fail",
                }
            ],
        },
        "warning": (
            "This FAIL records a missing causal-availability receipt, not a price, "
            "funding, cost, or PnL failure. Retrospective REST data must not repair it."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=AUDIT_PATH)
    parser.add_argument("--preregistration", type=Path, default=PREREGISTRATION_PATH)
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT)
    args = parser.parse_args()

    audit_bytes = args.audit.read_bytes()
    audit = json.loads(audit_bytes)
    preregistration_sha256 = digest(args.preregistration)
    validate_audit(audit, preregistration_sha256=preregistration_sha256)

    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    experiments = [
        item for item in registry.get("experiments", []) if item.get("strategy") == CANDIDATE
    ]
    if len(experiments) != 1:
        raise SystemExit("FAIL: Alpha #13 must be registered exactly once")
    experiment_id = int(experiments[0]["experiment_id"])
    source_sha256 = sha256(audit_bytes).hexdigest()
    existing = next(
        (
            event
            for event in registry.get("events") or []
            if int(event.get("experiment_id") or -1) == experiment_id
            and event.get("event_type") == "result_update"
            and (((event.get("payload") or {}).get("result") or {}).get("source_sha256"))
            == source_sha256
        ),
        None,
    )
    if existing is None:
        archived_hash, archive_path = archive_report(args.audit, args.archive_root)
        if archived_hash != source_sha256:
            raise RuntimeError("causal-availability audit hash changed while sealing")
        result = build_registry_result(
            audit,
            audit_sha256=source_sha256,
            evidence_archive=str(archive_path.relative_to(PROJECT_ROOT)),
            preregistration_sha256=preregistration_sha256,
        )
        event = ExperimentRegistry(args.registry).append_event(
            experiment_id=experiment_id,
            event_type="result_update",
            payload={"source_sha256": source_sha256, "result": result},
        )
    else:
        event = existing
    print(
        json.dumps(
            {
                "status": "APPENDED" if existing is None else "ALREADY_SYNCED",
                "event_id": event["event_id"],
                "source_sha256": source_sha256,
                "promotion_eligible": False,
                "classification": "NON_PERFORMANCE_CAUSAL_VETO",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
