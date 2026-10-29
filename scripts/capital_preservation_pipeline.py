#!/usr/bin/env python3
"""Reproduce a preregistered capital-preservation historical diagnostic.

This is intentionally a diagnostic reporter, not an order adapter or a
promotion mechanism.  It binds four independent result exports, the
preregistration, execution contract, integrity evidence, and current policy
artifacts before evaluating the shared capital-preservation gate.
"""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from re import fullmatch
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.capital_preservation import evaluate_capital_preservation
from quant.research.result_loader import load_freqtrade_result


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be one JSON object: {path}")
    return value


def validate_current_preregistration(preregistration: dict[str, Any]) -> list[str]:
    """Return explicit failures before any candidate result is interpreted."""
    failures: list[str] = []
    candidate = preregistration.get("candidate")
    if not isinstance(candidate, str) or not candidate.strip():
        failures.append("candidate is missing")
    status = preregistration.get("status")
    if not isinstance(status, str) or not status.startswith("PREREGISTERED"):
        failures.append("preregistration status is not locked")
    if preregistration.get("live_order_authorization") != "CLOSED":
        failures.append("preregistration must keep live order authorization CLOSED")
    return failures


def _reference(path: Path) -> dict[str, str]:
    try:
        relative = str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        relative = str(path)
    return {"path": relative, "sha256": _sha256(path)}


def verify_funding_reconciliation_sources(integrity: dict[str, Any]) -> dict[str, Any]:
    """Rehash every source named by the candidate's funding receipt.

    A generic capital-preservation diagnostic cannot prescribe a single
    funding-file format, but it can reject a receipt that is merely a claim.
    Receipt records are project-relative so an absolute path cannot silently
    bind unrelated local data.
    """
    receipt = integrity.get("funding_reconciliation")
    failures: list[str] = []
    verified: list[dict[str, str]] = []
    if not isinstance(receipt, dict):
        failures.append("funding reconciliation receipt is missing")
    elif receipt.get("status") != "PASS":
        failures.append("funding reconciliation receipt is not PASS")
    elif receipt.get("source_bound") is not True:
        failures.append("funding reconciliation receipt is not source-bound")
    else:
        records = receipt.get("source_records")
        if not isinstance(records, list) or not records:
            failures.append("funding reconciliation receipt has no source records")
        else:
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    failures.append(f"funding source record {index} is invalid")
                    continue
                relative = record.get("path")
                expected = record.get("sha256")
                if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                    failures.append(f"funding source record {index} has an unsafe path")
                    continue
                if not isinstance(expected, str) or fullmatch(r"[0-9a-f]{64}", expected) is None:
                    failures.append(f"funding source record {index} has an invalid SHA-256")
                    continue
                source = (PROJECT_ROOT / relative).resolve()
                if not source.is_relative_to(PROJECT_ROOT.resolve()):
                    failures.append(f"funding source record {index} escapes project root")
                    continue
                if not source.is_file():
                    failures.append(f"funding source record {index} is missing")
                    continue
                actual = _sha256(source)
                if actual != expected:
                    failures.append(f"funding source record {index} SHA-256 mismatch")
                    continue
                verified.append({"path": relative, "sha256": actual})
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "verified_source_records": verified,
    }


def build_diagnostic_report(
    *,
    preregistration: dict[str, Any],
    preregistration_path: Path,
    development: dict[str, Any],
    development_stress: dict[str, Any],
    post_development: dict[str, Any],
    post_development_stress: dict[str, Any],
    execution_contract: dict[str, Any],
    integrity: dict[str, Any],
    risk_policy: dict[str, Any],
    objective: dict[str, Any],
    sources: dict[str, Path],
) -> dict[str, Any]:
    preregistration_failures = validate_current_preregistration(preregistration)
    if preregistration_failures:
        return {
            "schema_version": 1,
            "candidate": preregistration.get("candidate"),
            "status": "PREREGISTRATION_INVALID",
            "promotion_eligible": False,
            "live_order_authorization": "CLOSED",
            "failures": preregistration_failures,
            "inputs": {name: _reference(path) for name, path in sources.items()},
        }
    funding_reconciliation_audit = verify_funding_reconciliation_sources(integrity)
    gate_integrity = dict(integrity)
    if funding_reconciliation_audit["status"] != "PASS":
        gate_integrity["funding_reconciliation"] = {
            "status": "FAIL",
            "source_bound": False,
        }
    gate = evaluate_capital_preservation(
        development,
        development_stress,
        post_development,
        post_development_stress,
        objective=objective,
        risk_policy=risk_policy,
        execution_contract=execution_contract,
        integrity=gate_integrity,
    )
    return {
        "schema_version": 1,
        "candidate": preregistration["candidate"],
        "status": gate["status"],
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "preregistration_status": preregistration["status"],
        "capital_preservation_gate": gate,
        "funding_reconciliation_audit": funding_reconciliation_audit,
        "inputs": {name: _reference(path) for name, path in sources.items()},
        "warning": (
            "Historical diagnostics can reject a candidate but cannot authorize "
            "a live order or replace locked-forward evidence."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--development", required=True, type=Path)
    parser.add_argument("--development-stress", required=True, type=Path)
    parser.add_argument("--post-development", required=True, type=Path)
    parser.add_argument("--post-development-stress", required=True, type=Path)
    parser.add_argument("--execution-contract", required=True, type=Path)
    parser.add_argument("--integrity", required=True, type=Path)
    parser.add_argument(
        "--risk-policy", type=Path, default=PROJECT_ROOT / "research/risk-policy.json"
    )
    parser.add_argument(
        "--objective",
        type=Path,
        default=PROJECT_ROOT / "research/capital-preservation-objective.json",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    paths = {
        "preregistration": args.preregistration,
        "development": args.development,
        "development_stress": args.development_stress,
        "post_development": args.post_development,
        "post_development_stress": args.post_development_stress,
        "execution_contract": args.execution_contract,
        "integrity": args.integrity,
        "risk_policy": args.risk_policy,
        "objective": args.objective,
    }
    preregistration = _load_object(args.preregistration, label="preregistration")
    report = build_diagnostic_report(
        preregistration=preregistration,
        preregistration_path=args.preregistration,
        development=load_freqtrade_result(args.development, args.strategy),
        development_stress=load_freqtrade_result(args.development_stress, args.strategy),
        post_development=load_freqtrade_result(args.post_development, args.strategy),
        post_development_stress=load_freqtrade_result(
            args.post_development_stress, args.strategy
        ),
        execution_contract=_load_object(args.execution_contract, label="execution contract"),
        integrity=_load_object(args.integrity, label="integrity report"),
        risk_policy=_load_object(args.risk_policy, label="risk policy"),
        objective=_load_object(args.objective, label="capital-preservation objective"),
        sources=paths,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "candidate": report.get("candidate"),
                "status": report["status"],
                "promotion_eligible": False,
                "live_order_authorization": "CLOSED",
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    if report["status"] in {"FAIL", "PREREGISTRATION_INVALID"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
