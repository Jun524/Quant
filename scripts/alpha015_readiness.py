#!/usr/bin/env python3
"""Report whether the deferred minute cross-crypto candidate may be preregistered."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.candidate_activation import (
    evaluate_alpha015_activation,
    verify_alpha015_contract_semantics,
    verify_methodology_artifacts,
)
from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document, protocol_digests
from quant.research.alpha015_activation_lock import build_alpha015_activation_lock
from quant.research.alpha015_preregistration import (
    build_alpha015_preregistration,
    preregistration_artifact_paths,
    verify_alpha015_preregistration,
)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_report() -> tuple[dict, dict]:
    policy = json.loads((PROJECT_ROOT / "research" / "alpha015-activation-policy.json").read_text(encoding="utf-8"))
    amendment = json.loads(
        (PROJECT_ROOT / "research" / "alpha015-activation-amendment-001.json").read_text(encoding="utf-8")
    )
    # Never authorize return acquisition from the cached status JSON.  Rebuild
    # coverage from the sealed snapshot/manifest chain on every readiness run.
    universe_policy = policy_document()
    universe = assess_forward_universe_history(
        PROJECT_ROOT / "research" / "universe_snapshots",
        expected_protocol=protocol_digests(),
        policy=universe_policy,
    )
    registry = json.loads((PROJECT_ROOT / "research" / "registry.json").read_text(encoding="utf-8"))
    screening = json.loads((PROJECT_ROOT / "research" / "alpha-candidate-screening-20261010.json").read_text(encoding="utf-8"))
    methodology = json.loads((PROJECT_ROOT / "research" / "alpha015-methodology-audit.json").read_text(encoding="utf-8"))
    methodology = verify_methodology_artifacts(methodology, project_root=PROJECT_ROOT)
    contract_semantics = verify_alpha015_contract_semantics(
        methodology, project_root=PROJECT_ROOT
    )
    methodology["contract_semantics"] = contract_semantics
    methodology.setdefault("resolution_status", {})["contract_terms_consistent"] = (
        contract_semantics["status"] == "PASS"
    )
    activation_lock_verified = False
    activation_lock: dict | None = None
    required_days = int(
        (policy.get("activation_requirements") or {}).get(
            "point_in_time_universe_consecutive_days"
        )
        or 0
    )
    if (
        universe.get("status") == "READY"
        and int(universe.get("consecutive_days_ending_latest") or 0) >= required_days
    ):
        receipt_path = (
            PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
        )
        try:
            expected_lock = build_alpha015_activation_lock(
                snapshot_directory=PROJECT_ROOT / "research" / "universe_snapshots",
                universe_policy=universe_policy,
                expected_protocol=protocol_digests(),
                candidate_policy=policy,
                amendment=amendment,
            )
            stored_lock = json.loads(receipt_path.read_text(encoding="utf-8"))
            activation_lock_verified = stored_lock == expected_lock
            activation_lock = stored_lock if activation_lock_verified else None
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            activation_lock_verified = False
            activation_lock = None
    preregistration_verified = False
    if activation_lock_verified and activation_lock is not None:
        try:
            template = json.loads(
                (PROJECT_ROOT / "research" / "alpha015-preregistration-template.json").read_text(
                    encoding="utf-8"
                )
            )
            expected_preregistration = build_alpha015_preregistration(
                template=template,
                activation_lock=activation_lock,
                artifact_paths=preregistration_artifact_paths(),
                project_root=PROJECT_ROOT,
            )
            stored_preregistration = json.loads(
                (PROJECT_ROOT / "research" / "alpha015-preregistration.json").read_text(
                    encoding="utf-8"
                )
            )
            preregistration_verified = verify_alpha015_preregistration(
                stored=stored_preregistration,
                expected=expected_preregistration,
                registry=registry,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            preregistration_verified = False
    report = evaluate_alpha015_activation(
        policy=policy,
        universe_report=universe,
        registry=registry,
        screening=screening,
        methodology_audit=methodology,
        activation_lock_verified=activation_lock_verified,
        preregistration_verified=preregistration_verified,
    )
    return report, methodology


def main() -> None:
    report, methodology = build_report()
    json_path = PROJECT_ROOT / "reports" / "alpha015-readiness.json"
    markdown_path = PROJECT_ROOT / "reports" / "alpha015-readiness.md"
    atomic_write(json_path, json.dumps(report, indent=2) + "\n")
    checks = "\n".join(
        f"- [{'x' if passed else ' '}] `{name}`"
        for name, passed in report["checks"].items()
    )
    markdown = (
        "# Alpha 015 candidate readiness\n\n"
        f"Status: **{report['status']}**\n\n"
        f"Point-in-time universe: {report['current_consecutive_universe_days']} / "
        f"{report['required_consecutive_universe_days']} consecutive days "
        f"({report['days_remaining']} remaining).\n\n"
        f"{checks}\n\n"
        f"Unresolved methodology items: {len(report['unresolved_methodology'])}\n\n"
        f"Methodology artifact integrity: {methodology['artifact_integrity']['status']}\n\n"
        f"Next action: {report['next_action']}\n\n"
        f"{report['warning']}\n"
    )
    atomic_write(markdown_path, markdown)
    print(json.dumps(report, indent=2))
    if report["status"] in {
        "ACTIVATION_CONTRACT_BLOCK",
        "INCOMPATIBLE_WITH_CAPITAL_PRESERVATION_POLICY",
        "DATA_INTEGRITY_BLOCK",
        "PREMATURE_EXPERIMENT_REGISTERED",
    }:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
