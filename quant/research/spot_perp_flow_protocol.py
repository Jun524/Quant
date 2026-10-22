"""Locked phase calendar and fail-closed gate transitions for Alpha 016."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any


CANDIDATE = "ALPHA_016_SPOT_PERP_FLOW_DIVERGENCE"
_DEVELOPMENT_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
_OOS_START = datetime(2025, 4, 1, tzinfo=timezone.utc)
_OOS_END = datetime(2025, 6, 30, tzinfo=timezone.utc)
_STEP = timedelta(hours=4)


class SpotPerpFlowProtocolError(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _decisions(start: datetime, end: datetime) -> list[datetime]:
    values: list[datetime] = []
    cursor = start
    while cursor < end:
        values.append(cursor)
        cursor += _STEP
    return values


def phase_calendar(phase: str) -> dict[str, Any]:
    """Return the immutable source and decision calendar without reading data."""
    if phase == "development":
        decisions = _decisions(_DEVELOPMENT_START, _OOS_START)
        # The 20:00 decision would exit after the locked OOS boundary.
        decisions = [item for item in decisions if item != _OOS_START - _STEP]
        source_start = date(2024, 12, 31)  # ten-minute feature warmup for Jan 1 00:00
        source_end = date(2025, 3, 31)
    elif phase == "locked_oos":
        decisions = _decisions(_OOS_START, _OOS_END)
        # Fixed eight-hour embargo after the final development execution.
        decisions = [item for item in decisions if item != _OOS_START]
        source_start = date(2025, 4, 1)
        source_end = date(2025, 6, 30)  # final 20:00 decision exits just after midnight
    else:
        raise SpotPerpFlowProtocolError("phase must be development or locked_oos")
    source_days: list[str] = []
    cursor = source_start
    while cursor <= source_end:
        source_days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return {
        "phase": phase,
        "decision_boundaries_utc": [item.isoformat().replace("+00:00", "Z") for item in decisions],
        "source_days": source_days,
        "source_markets": ["spot", "usd_m_perpetual"],
        "decision_count": len(decisions),
        "source_day_count": len(source_days),
        "calendar_sha256": canonical_sha256(
            {
                "phase": phase,
                "decisions": [item.isoformat() for item in decisions],
                "source_days": source_days,
            }
        ),
    }


def _assert_preregistration(preregistration: dict[str, Any]) -> None:
    if preregistration.get("candidate") != CANDIDATE:
        raise SpotPerpFlowProtocolError("candidate mismatch")
    if preregistration.get("status") != "PREREGISTERED_HISTORICAL_DEVELOPMENT_LOCKED":
        raise SpotPerpFlowProtocolError("preregistration is not development locked")
    scope = preregistration.get("research_scope") or {}
    if any(scope.get(key) is not False for key in ("orders_submitted", "real_or_test_positions", "live_promotion_eligible")):
        raise SpotPerpFlowProtocolError("simulation-only scope is invalid")


def build_phase_plan(
    preregistration: dict[str, Any], *, phase: str, development_report: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Authorize exactly one phase, withholding OOS inventory until development passes."""
    _assert_preregistration(preregistration)
    preregistration_sha256 = canonical_sha256(preregistration)
    if phase == "locked_oos":
        if not development_report:
            raise SpotPerpFlowProtocolError("locked OOS remains unopened without development report")
        if not (
            development_report.get("candidate") == CANDIDATE
            and development_report.get("phase") == "development"
            and development_report.get("status") == "PASS"
            and development_report.get("preregistration_sha256") == preregistration_sha256
            and all(bool(item.get("passed")) for item in development_report.get("checks") or [])
        ):
            raise SpotPerpFlowProtocolError("locked OOS remains unopened because development did not pass")
    calendar = phase_calendar(phase)
    body = {
        "schema_version": 1,
        "candidate": CANDIDATE,
        "phase": phase,
        "status": "AUTHORIZED_PLAN_NO_SOURCE_BYTES_ACQUIRED",
        "preregistration_sha256": preregistration_sha256,
        "calendar": calendar,
        "orders_submitted": False,
        "live_promotion_eligible": False,
    }
    return {**body, "plan_sha256": canonical_sha256(body)}


def evaluate_phase_gate(
    preregistration: dict[str, Any], *, phase: str, evidence: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate fixed gate values; no missing evidence is interpreted as a pass."""
    _assert_preregistration(preregistration)
    if phase not in {"development", "locked_oos"}:
        raise SpotPerpFlowProtocolError("phase must be development or locked_oos")
    policy = preregistration[f"{phase}_gate"]
    checks = [
        ("complete_utc_days_minimum", int(evidence.get("complete_utc_days") or 0) >= int(policy["complete_utc_days_minimum"])),
        ("natural_virtual_trades_minimum", int(evidence.get("natural_virtual_trades") or 0) >= int(policy["natural_virtual_trades_minimum"])),
        ("net_return_positive", float(evidence.get("net_return") or 0.0) > 0.0),
        ("profit_factor_minimum", evidence.get("profit_factor") is not None and float(evidence["profit_factor"]) >= float(policy["profit_factor_minimum"])),
        ("double_fee_net_positive", float(evidence.get("double_fee_net_return") or 0.0) > 0.0),
        ("slippage_stress_net_positive", float(evidence.get("slippage_stress_net_return") or 0.0) > 0.0),
        ("random_control_excess_return_positive", float(evidence.get("random_control_excess_return") or 0.0) > 0.0),
        ("daily_block_bootstrap_ci95_lower_positive", evidence.get("bootstrap_ci95_lower") is not None and float(evidence["bootstrap_ci95_lower"]) > 0.0),
        ("data_integrity_pass", evidence.get("data_integrity_pass") is True),
        ("lookahead_pass", evidence.get("lookahead_pass") is True),
    ]
    status = "PASS" if all(passed for _, passed in checks) else "FAIL"
    body = {
        "schema_version": 1,
        "candidate": CANDIDATE,
        "phase": phase,
        "status": status,
        "preregistration_sha256": canonical_sha256(preregistration),
        "evidence": evidence,
        "checks": [{"name": name, "passed": passed} for name, passed in checks],
        "orders_submitted": False,
        "live_promotion_eligible": False,
    }
    return {**body, "report_sha256": canonical_sha256(body)}
