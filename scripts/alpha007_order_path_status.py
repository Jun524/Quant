#!/usr/bin/env python3
"""Fail-closed activation status for the Alpha #7 order-path campaign."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.sync_alpha007_registry import receipt_health


POLICY_PATH = PROJECT_ROOT / "research" / "alpha007-order-path-policy.json"
IMPLEMENTATION_PATH = PROJECT_ROOT / "quant" / "execution" / "alpha007_order_path.py"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"
PRIMARY_PATH = PROJECT_ROOT / "reports" / "alpha007-accumulation.json"
EXTERNAL_COST_PATH = PROJECT_ROOT / "reports" / "alpha007-external-cost-stress.json"
PARTIAL_FILL_PATH = PROJECT_ROOT / "reports" / "alpha007-partial-fill-stress.json"
FILTER_PATH = PROJECT_ROOT / "reports" / "alpha007-exchange-filter-audit.json"
OUTPUT_PATH = PROJECT_ROOT / "reports" / "alpha007-order-path-status.json"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one object")
    return value


def _registry_binding(policy: dict) -> dict:
    registry = _load(REGISTRY_PATH)
    experiment = next(
        (
            item
            for item in registry.get("experiments") or []
            if item.get("strategy") == policy.get("candidate")
        ),
        None,
    )
    events = [
        item
        for item in registry.get("events") or []
        if experiment is not None
        and item.get("experiment_id") == experiment.get("experiment_id")
        and item.get("event_type") == "auxiliary_gate_preregistered"
        and ((item.get("payload") or {}).get("gate")) == policy.get("policy")
    ]
    payloads = [item.get("payload") or {} for item in events]
    valid = bool(
        len(events) == 1
        and payloads[0].get("policy_sha256") == _digest(POLICY_PATH)
        and payloads[0].get("implementation_sha256") == _digest(IMPLEMENTATION_PATH)
        and payloads[0].get("promotion_eligible") is False
        and payloads[0].get("can_only_veto") is True
    )
    return {
        "valid": valid,
        "experiment_id": experiment.get("experiment_id") if experiment else None,
        "event_id": events[0].get("event_id") if len(events) == 1 else None,
    }


def _filter_fresh(report: dict, now: datetime) -> tuple[bool, float | None]:
    try:
        generated = datetime.fromisoformat(
            str(report["generated_at_utc"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        age = (now - generated).total_seconds()
        maximum = float(report["maximum_age_seconds_for_promotion"])
    except (KeyError, TypeError, ValueError):
        return False, None
    return 0 <= age <= maximum, age


def build_report(*, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    policy = _load(POLICY_PATH)
    primary = _load(PRIMARY_PATH)
    external = _load(EXTERNAL_COST_PATH)
    partial = _load(PARTIAL_FILL_PATH)
    exchange_filter = _load(FILTER_PATH)
    registry_binding = _registry_binding(policy)
    terminal_receipt = receipt_health()
    filter_fresh, filter_age = _filter_fresh(exchange_filter, now)
    primary_hash = _digest(PRIMARY_PATH)
    checks = [
        {
            "name": "policy_and_implementation_registry_binding",
            "passed": registry_binding["valid"],
            "actual": registry_binding,
        },
        {
            "name": "primary_shadow_pass",
            "passed": primary.get("status") == "PASS",
            "actual": primary.get("status"),
            "required": "PASS",
        },
        {
            "name": "primary_terminal_receipt",
            "passed": terminal_receipt.get("valid") is True,
            "actual": terminal_receipt.get("valid"),
            "required": True,
        },
        {
            "name": "ten_bps_external_cost_stress",
            "passed": external.get("status") == "PASS"
            and external.get("primary_report_sha256") == primary_hash,
            "actual": external.get("status"),
            "required": "PASS bound to current primary",
        },
        {
            "name": "partial_fill_veto",
            "passed": partial.get("status") == "PASS"
            and partial.get("primary_report_sha256") == primary_hash,
            "actual": partial.get("status"),
            "required": "PASS bound to current primary",
        },
        {
            "name": "fresh_exchange_filter_audit",
            "passed": filter_fresh,
            "actual_age_seconds": filter_age,
            "required_max": exchange_filter.get("maximum_age_seconds_for_promotion"),
        },
        {
            "name": "fixed_notional_exchange_filter_feasibility",
            "passed": exchange_filter.get("status") == "PASS",
            "actual": exchange_filter.get("status"),
            "required": "PASS",
        },
        {
            "name": "credential_free_only",
            "passed": (policy.get("fixed_order_path") or {}).get("network_submission")
            is False,
            "actual": (policy.get("fixed_order_path") or {}).get("network_submission"),
            "required": False,
        },
    ]
    integrity_ok = registry_binding["valid"]
    primary_status = primary.get("status")
    if not integrity_ok:
        status = "INTEGRITY_FAIL"
    elif primary_status == "ACCUMULATING":
        status = (
            "WAITING_PRIMARY_WITH_KNOWN_FILTER_BLOCK"
            if exchange_filter.get("status") == "FAIL"
            else "WAITING_PRIMARY"
        )
    elif primary_status == "FAIL":
        status = "NOT_ACTIVATED_PRIMARY_FAIL"
    elif all(item["passed"] for item in checks):
        status = "READY_FOR_CREDENTIAL_FREE_CAMPAIGN"
    else:
        status = "NOT_ACTIVATED_AUXILIARY_FAIL"
    return {
        "schema_version": 1,
        "generated_at_utc": now.isoformat(),
        "candidate": policy["candidate"],
        "status": status,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "network_submission": False,
        "policy_sha256": _digest(POLICY_PATH),
        "implementation_sha256": _digest(IMPLEMENTATION_PATH),
        "registry_binding": registry_binding,
        "checks": checks,
        "blockers": [item["name"] for item in checks if not item["passed"]],
    }


def main() -> None:
    report = build_report()
    OUTPUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] in {"INTEGRITY_FAIL", "NOT_ACTIVATED_AUXILIARY_FAIL"}:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
