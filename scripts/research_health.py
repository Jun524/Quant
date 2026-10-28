#!/usr/bin/env python3
"""Emit one machine-readable health verdict for all active forward research."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.manage_alpha001_forward_dryrun import status_payload as alpha001_status_payload
from scripts.manage_microstructure_campaign import status_payload as alpha007_status_payload
from scripts.analyze_microstructure import audit_complete_day_protocols
from scripts.manage_shadow_entry_campaign import status_payload as execution_status_payload
from scripts.alpha011_status import status_payload as alpha011_status_payload
from scripts.alpha013_status import status_payload as alpha013_status_payload
from scripts.alpha015_readiness import build_report as alpha015_readiness_report
from scripts.alpha015_universe_diagnostic import build_report as alpha015_universe_diagnostic_report
from scripts.sync_alpha007_registry import receipt_health as alpha007_receipt_health
from scripts.alpha007_order_path_status import build_report as alpha007_order_path_status
from scripts.close_alpha007_collection_shortfall import (
    CAMPAIGN_STATE_PATH as ALPHA007_CAMPAIGN_STATE_PATH,
    CLOSURE_PATH as ALPHA007_COLLECTION_CLOSURE_PATH,
    PRIMARY_PATH as ALPHA007_PRIMARY_REPORT_PATH,
    digest as alpha007_closure_digest,
    validate_collection_closure,
)
from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document as universe_policy_document
from quant.data.universe_protocol import protocol_digests as universe_protocol_digests
from quant.monitoring.alpha015_milestones import build_alpha015_collection_milestones
from scripts.run_universe_scheduler import scheduler_status_payload as universe_scheduler_status
from scripts.manage_liquidation_telemetry_campaign import (
    status_payload as liquidation_telemetry_status,
)
from scripts.run_liquidation_overlay_scheduler import (
    status_payload as liquidation_overlay_scheduler_status,
)
from quant.research.risk_policy_contract import validate_risk_policy_contract


HEALTHY_ALPHA001 = {"RUNNING_HEALTHY", "RUNNING_STARTING"}
HEALTHY_ALPHA001_ENTRY_EVIDENCE = {"CONTINUOUS", "NOT_YET_STARTED"}
HEALTHY_ALPHA007 = {
    "RUNNING_HEALTHY",
    "RUNNING_ACTIVE_EXTENDED",
    "RUNNING_BETWEEN_CHUNKS",
    "RUNNING_COLLECTOR_STARTING",
    "SHADOW_REVIEW_SEALED",
    "SHADOW_REVIEW_AUXILIARY_VETOED",
    "SHADOW_FAIL_SEALED",
    "COLLECTION_SHORTFALL_CLOSED",
}
WARNING_ALPHA007 = {
    "RUNNING_WITH_DATA_WARNING",
    "RUNNING_WITH_NEGATIVE_LOCKED_TELEMETRY",
}
HEALTHY_ALPHA011 = {
    "WAITING_FIRST_COMPLETE_UTC_DAY",
    "WAITING_PUBLISHER_ARCHIVE",
    "ACCUMULATING_DEVELOPMENT",
    "DEVELOPMENT_FAIL",
    "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
    "ACCUMULATING_LOCKED_VALIDATION",
    "VALIDATION_PASS_REVIEW",
    "VALIDATION_FAIL",
}
HEALTHY_ALPHA013 = {
    "WAITING_FIRST_COMPLETE_UTC_DAY",
    "WAITING_PUBLISHER_ARCHIVE",
    "ACCUMULATING_SIGNAL_LOOKBACK",
    "ACCUMULATING_FORWARD_WEEKS",
    "WAITING_FUNDING_RECONCILIATION",
    "DEVELOPMENT_FAIL",
    "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
    "ACCUMULATING_LOCKED_VALIDATION",
    "VALIDATION_PASS_REVIEW",
    "VALIDATION_FAIL",
}
HEALTHY_EXECUTION = {"RUNNING_HEALTHY", "COMPLETE"}
HEALTHY_UNIVERSE = {"WAITING_FIRST_WINDOW", "ACCUMULATING", "READY"}
WARNING_UNIVERSE = {"ACCUMULATING_WITH_GAPS", "COLLECTION_RETRY_PENDING"}
HEALTHY_LIQUIDATION_TELEMETRY = {"WAITING_ELIGIBLE_START", "RUNNING_HEALTHY"}
WARNING_LIQUIDATION_TELEMETRY = {"RUNNING_WITH_DATA_GAPS"}
HEALTHY_ALPHA015 = {
    "WAITING_POINT_IN_TIME_UNIVERSE",
    "WAITING_METHODOLOGY_LOCK",
    "READY_FOR_PREREGISTRATION_ONLY",
    "READY_FOR_DATA_ACQUISITION",
}
ALPHA001_MAXIMUM_MTM_GAP_SECONDS = 120


def assess_mtm_continuity(
    path: Path,
    *,
    maximum_gap_seconds: int = ALPHA001_MAXIMUM_MTM_GAP_SECONDS,
) -> dict:
    """Check active append-only MTM observations, ignoring an incomplete tail line."""
    try:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if content and not content.endswith("\n"):
            lines = lines[:-1]
        timestamps = [
            datetime.fromisoformat(
                str(json.loads(line)["observed_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            for line in lines
            if line.strip()
        ]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return {
            "status": "FAIL_CLOSED",
            "path": str(path),
            "maximum_allowed_gap_seconds": maximum_gap_seconds,
            "reason": f"{type(error).__name__}: {error}",
        }
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(timestamps, timestamps[1:])
    ]
    strictly_increasing = all(
        current > previous for previous, current in zip(timestamps, timestamps[1:])
    )
    maximum_gap = max(gaps, default=0.0)
    passed = bool(timestamps) and strictly_increasing and maximum_gap <= maximum_gap_seconds
    return {
        "status": "PASS" if passed else "FAIL_CLOSED",
        "path": str(path),
        "observations": len(timestamps),
        "strictly_increasing": strictly_increasing,
        "maximum_allowed_gap_seconds": maximum_gap_seconds,
        "maximum_gap_seconds": maximum_gap,
    }


def assess_alpha001_entry_execution_continuity(
    alpha001: dict,
    *,
    raw_root: Path = PROJECT_ROOT / "research" / "execution" / "raw",
) -> dict:
    """Fail closed when a current-protocol entry-evidence interval was not observed.

    This is deliberately independent of process health.  A replacement recorder can
    be running correctly while an earlier unobserved interval remains ineligible to
    support a future promotion decision.
    """
    expected_protocol = alpha001.get("protocol_sha256")
    if not isinstance(expected_protocol, dict) or not expected_protocol:
        return {
            "status": "FAIL_CLOSED",
            "promotion_evidence_eligible": False,
            "failures": ["current_alpha001_protocol_is_missing"],
            "segments": [],
            "unreplayed_recoveries": [],
        }

    failures: list[str] = []
    segments: list[dict] = []
    try:
        manifest_paths = sorted(raw_root.glob("alpha001-signal-entry-*.manifest.json"))
    except OSError as error:
        return {
            "status": "FAIL_CLOSED",
            "promotion_evidence_eligible": False,
            "failures": [f"entry_execution_manifest_inventory_unreadable:{type(error).__name__}"],
            "segments": [],
            "unreplayed_recoveries": [],
        }
    for path in manifest_paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            failures.append(f"entry_execution_manifest_invalid:{path.name}")
            continue
        if manifest.get("protocol_sha256") != expected_protocol:
            continue
        status = str(manifest.get("status") or "UNKNOWN")
        try:
            display_path = str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            display_path = str(path)
        segments.append(
            {
                "path": display_path,
                "status": status,
                "trigger_log_start_offset": manifest.get("trigger_log_start_offset"),
                "trigger_log_last_offset": manifest.get("trigger_log_last_offset"),
            }
        )
        if status == "FAILED":
            failures.append(f"failed_current_protocol_entry_segment:{path.name}")

    recovery_history = alpha001.get("entry_execution_recovery_history") or []
    if not isinstance(recovery_history, list):
        failures.append("entry_execution_recovery_history_invalid")
        recovery_history = []
    unreplayed_recoveries = [
        item
        for item in recovery_history
        if isinstance(item, dict) and item.get("uncovered_interval_replayed") is not True
    ]
    if unreplayed_recoveries:
        failures.append("entry_execution_recovery_contains_unreplayed_interval")

    if failures:
        status = "EVIDENCE_DISCONTINUITY"
    elif not segments:
        status = "NOT_YET_STARTED"
    else:
        status = "CONTINUOUS"
    sealed_for_promotion = bool(segments) and all(
        item.get("status") == "SEALED_ON_STOP" for item in segments
    )
    return {
        "status": status,
        "promotion_evidence_eligible": status == "CONTINUOUS" and sealed_for_promotion,
        "promotion_evidence_readiness": (
            "SEALED_CONTINUOUS"
            if status == "CONTINUOUS" and sealed_for_promotion
            else "ACTIVE_OR_INCOMPLETE"
            if status == "CONTINUOUS"
            else "INELIGIBLE"
        ),
        "failures": failures,
        "segments": segments,
        "unreplayed_recoveries": unreplayed_recoveries,
    }


def alpha007_complete_day_protocol_health(
    *,
    report_path: Path = PROJECT_ROOT / "reports" / "alpha007-accumulation.json",
    raw_root: Path = PROJECT_ROOT / "research" / "microstructure",
    project_root: Path = PROJECT_ROOT,
) -> dict:
    """Replay the cheap complete-day protocol audit used by the Alpha #7 report."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"health": "FAIL", "failures": ["accumulation_report_missing_or_invalid"]}
    coverage = report.get("coverage") or []
    complete_days = [
        str(item.get("day"))
        for item in coverage
        if isinstance(item, dict) and item.get("complete") is True
    ]
    failures: list[str] = []
    if int(report.get("complete_days") or 0) != len(complete_days):
        failures.append("complete_day_count_mismatch")
    verified_runs = (report.get("data_integrity") or {}).get("verified_runs") or []
    reported_ids = [
        str(item.get("run_id") or "") for item in verified_runs if isinstance(item, dict)
    ]
    if not reported_ids or len(reported_ids) != len(set(reported_ids)) or "" in reported_ids:
        failures.append("reported_run_inventory_invalid")
    source_ended_at: dict[str, datetime] = {}
    for manifest_path in sorted(raw_root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            run_id = str(manifest["run_id"])
            ended_at = datetime.fromisoformat(
                str((manifest.get("result") or {})["ended_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            failures.append(f"source_manifest_invalid:{manifest_path.name}")
            continue
        if run_id in source_ended_at:
            failures.append(f"source_manifest_run_id_duplicate:{run_id}")
        source_ended_at[run_id] = ended_at
    missing_reported_sources = sorted(set(reported_ids) - set(source_ended_at))
    if missing_reported_sources:
        failures.append("reported_run_source_missing")
    included_end_times = [
        source_ended_at[run_id] for run_id in reported_ids if run_id in source_ended_at
    ]
    if included_end_times:
        report_cutoff = max(included_end_times)
        source_ids_through_cutoff = {
            run_id for run_id, ended_at in source_ended_at.items() if ended_at <= report_cutoff
        }
        if source_ids_through_cutoff != set(reported_ids):
            failures.append("source_manifest_inventory_mismatch_through_report_cutoff")
    if not complete_days:
        return {
            "health": "NOT_YET_REQUIRED" if not failures else "FAIL",
            "complete_days": 0,
            "failures": failures,
        }
    try:
        recomputed = audit_complete_day_protocols(
            raw_root,
            report,
            project_root=project_root,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        failures.append(f"replay_failed:{type(error).__name__}:{error}")
        recomputed = None
    if recomputed is not None and report.get("complete_day_protocol_audit") != recomputed:
        failures.append("stored_audit_does_not_match_replay")
    return {
        "health": "PASS" if not failures else "FAIL",
        "complete_days": len(complete_days),
        "failures": failures,
        "recomputed": recomputed,
    }


def alpha007_collection_shortfall_closure_health(
    *,
    closure_path: Path = ALPHA007_COLLECTION_CLOSURE_PATH,
    primary_path: Path = ALPHA007_PRIMARY_REPORT_PATH,
    campaign_path: Path = ALPHA007_CAMPAIGN_STATE_PATH,
) -> dict:
    """Validate the independent terminal receipt for an ended short campaign.

    The receipt is intentionally separate from the frozen Alpha #7 evaluator.
    It may only convert an already-ended, under-covered collection into a
    terminal FAIL; it cannot turn it into evidence for OOS or promotion.
    """
    if not closure_path.exists():
        return {"present": False, "valid": False, "failures": []}
    try:
        closure = json.loads(closure_path.read_text(encoding="utf-8"))
        primary = json.loads(primary_path.read_text(encoding="utf-8"))
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "present": True,
            "valid": False,
            "failures": [f"collection_shortfall_receipt_unreadable:{type(error).__name__}"],
        }
    valid = validate_collection_closure(
        closure,
        primary,
        campaign,
        primary_sha256=alpha007_closure_digest(primary_path),
        campaign_sha256=alpha007_closure_digest(campaign_path),
    )
    return {
        "present": True,
        "valid": valid,
        "status": closure.get("status"),
        "failure_class": closure.get("failure_class"),
        "actual_complete_days": closure.get("actual_complete_days"),
        "required_complete_days": closure.get("required_complete_days"),
        "promotion_eligible": closure.get("promotion_eligible"),
        "locked_oos_opened": closure.get("locked_oos_opened"),
        "orders_submitted": closure.get("orders_submitted"),
        "failures": [] if valid else ["collection_shortfall_receipt_evidence_mismatch"],
    }


def classify_research_health(
    alpha001_health: str,
    alpha007_health: str,
    alpha011_health: str = "WAITING_FIRST_COMPLETE_UTC_DAY",
    execution_health: str = "RUNNING_HEALTHY",
    alpha013_health: str = "WAITING_FIRST_COMPLETE_UTC_DAY",
    universe_health: str = "WAITING_FIRST_WINDOW",
    liquidation_telemetry_health: str = "WAITING_ELIGIBLE_START",
    alpha015_health: str = "WAITING_POINT_IN_TIME_UNIVERSE",
    alpha001_entry_evidence_health: str = "CONTINUOUS",
    risk_policy_health: str = "PASS",
) -> str:
    if (
        alpha001_health in HEALTHY_ALPHA001
        and alpha001_entry_evidence_health in HEALTHY_ALPHA001_ENTRY_EVIDENCE
        and alpha007_health in HEALTHY_ALPHA007
        and alpha011_health in HEALTHY_ALPHA011
        and execution_health in HEALTHY_EXECUTION
        and alpha013_health in HEALTHY_ALPHA013
        and universe_health in HEALTHY_UNIVERSE
        and liquidation_telemetry_health in HEALTHY_LIQUIDATION_TELEMETRY
        and alpha015_health in HEALTHY_ALPHA015
        and risk_policy_health == "PASS"
    ):
        return "HEALTHY"
    if (
        alpha001_health in HEALTHY_ALPHA001
        and alpha001_entry_evidence_health in HEALTHY_ALPHA001_ENTRY_EVIDENCE
        and alpha007_health in HEALTHY_ALPHA007 | WARNING_ALPHA007
        and alpha011_health in HEALTHY_ALPHA011
        and execution_health in HEALTHY_EXECUTION
        and alpha013_health in HEALTHY_ALPHA013
        and universe_health in HEALTHY_UNIVERSE | WARNING_UNIVERSE
        and liquidation_telemetry_health
        in HEALTHY_LIQUIDATION_TELEMETRY | WARNING_LIQUIDATION_TELEMETRY
        and alpha015_health in HEALTHY_ALPHA015
        and risk_policy_health == "PASS"
        and (
            alpha007_health in WARNING_ALPHA007
            or universe_health in WARNING_UNIVERSE
            or liquidation_telemetry_health in WARNING_LIQUIDATION_TELEMETRY
        )
    ):
        return "DEGRADED"
    return "ATTENTION_REQUIRED"


def capital_preservation_policy_health(
    *, project_root: Path = PROJECT_ROOT
) -> dict:
    """Load both policy artifacts and fail closed when their contract drifts."""
    try:
        risk = json.loads((project_root / "research/risk-policy.json").read_text(encoding="utf-8"))
        objective = json.loads(
            (project_root / "research/capital-preservation-objective.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "status": "FAIL",
            "live_order_authorization": "CLOSED",
            "failures": [f"policy_artifact_unreadable:{type(error).__name__}"],
        }
    return validate_risk_policy_contract(risk, objective)


def resolve_alpha013_health(payload: dict, scheduler_health: str) -> str:
    # A sealed causal veto intentionally stops the collector.  Report that
    # terminal evidence failure rather than misclassifying the expected stop
    # as an operational outage.
    status = str(payload.get("status") or "UNKNOWN")
    if status == "CAUSAL_AVAILABILITY_VETO":
        return status
    if scheduler_health not in {"RUNNING_HEALTHY", "RUNNING_RETRY_PENDING"}:
        return "SCHEDULER_DOWN"
    if payload.get("forward_report_current") is not True:
        return "FORWARD_REPORT_STALE"
    return status


def resolve_alpha007_terminal_health(
    operational_health: str,
    report: dict,
    receipt: dict,
) -> str:
    status = str(report.get("status") or "")
    if status not in {"PASS", "FAIL"}:
        return operational_health
    if receipt.get("valid") is not True:
        return "TERMINAL_RECEIPT_INVALID"
    if status == "PASS" and receipt.get("partial_fill_veto_status") == "FAIL":
        return "SHADOW_REVIEW_AUXILIARY_VETOED"
    return "SHADOW_REVIEW_SEALED" if status == "PASS" else "SHADOW_FAIL_SEALED"


def alpha007_nonterminal_performance_warning(report: dict) -> dict:
    """Surface negative locked telemetry without ending or retuning the cohort.

    The fixed Alpha #7 protocol must collect all 17 UTC days even when early
    observations are bad.  This is therefore a monitoring warning, never an
    early verdict or an instruction to alter its thresholds, cost model, or
    collection duration.
    """
    if report.get("status") != "ACCUMULATING":
        return {"status": "NOT_APPLICABLE", "reasons": []}
    try:
        complete_days = int(report.get("complete_days") or 0)
    except (TypeError, ValueError):
        complete_days = 0
    checks = {
        str(item.get("name")): item
        for item in (report.get("checks") or [])
        if isinstance(item, dict)
    }
    critical = (
        "locked_net_60s_markout",
        "hour_block_bootstrap_95pct_lower_bound",
        "plus_100ms_latency_net_60s_markout",
    )
    failed_critical = [
        name
        for name in critical
        if checks.get(name, {}).get("passed") is False
        and isinstance(checks.get(name, {}).get("actual"), (int, float))
        and not isinstance(checks.get(name, {}).get("actual"), bool)
        and float(checks[name]["actual"]) <= 0.0
    ]
    concentration = checks.get("single_hour_profit_concentration") or {}
    concentration_failed = (
        concentration.get("passed") is False
        and isinstance(concentration.get("actual"), (int, float))
        and not isinstance(concentration.get("actual"), bool)
        and float(concentration["actual"]) > 0.20
    )
    warning = complete_days >= 7 and len(failed_critical) == len(critical)
    reasons = failed_critical + (
        ["single_hour_profit_concentration"] if concentration_failed else []
    )
    return {
        "status": "WARNING" if warning else "NO_EARLY_WARNING",
        "complete_days": complete_days,
        "minimum_complete_days": 7,
        "reasons": reasons,
        "nonterminal": True,
        "action": (
            "Continue the fixed collection unchanged; do not promote, retune, or extend the cohort."
            if warning
            else "Continue the fixed collection unchanged."
        ),
    }


def build_health_report() -> dict:
    policy_contract = capital_preservation_policy_health()
    alpha001 = alpha001_status_payload()
    alpha007 = alpha007_status_payload(PROJECT_ROOT / "research" / "microstructure")
    alpha007_protocol_audit = alpha007_complete_day_protocol_health()
    try:
        alpha007_report = json.loads(
            (PROJECT_ROOT / "reports" / "alpha007-accumulation.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, json.JSONDecodeError):
        alpha007_report = {}
    alpha007_receipt = alpha007_receipt_health()
    alpha007_collection_closure = alpha007_collection_shortfall_closure_health()
    try:
        alpha007_order_path = alpha007_order_path_status()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        alpha007_order_path = {
            "status": "MISSING_OR_INVALID",
            "blockers": [f"status_replay_failed:{type(error).__name__}"],
        }
    alpha011 = alpha011_status_payload()
    alpha013 = alpha013_status_payload()
    execution = execution_status_payload()
    liquidation_telemetry = liquidation_telemetry_status()
    liquidation_overlay_scheduler = liquidation_overlay_scheduler_status()
    try:
        alpha015, alpha015_methodology = alpha015_readiness_report()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        alpha015 = {
            "status": "MISSING_OR_INVALID",
            "checks": {},
            "unresolved_methodology": [f"readiness_replay_failed:{type(error).__name__}"],
        }
        alpha015_methodology = {"artifact_integrity": {"status": "FAIL"}}
    try:
        alpha015_universe_diagnostic = alpha015_universe_diagnostic_report()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        alpha015_universe_diagnostic = {
            "status": "DIAGNOSTIC_REPLAY_FAILED",
            "error": f"{type(error).__name__}: {error}",
        }
    universe_policy = universe_policy_document()
    universe = assess_forward_universe_history(
        PROJECT_ROOT / "research" / "universe_snapshots",
        expected_protocol=universe_protocol_digests(),
        policy=universe_policy,
    )
    universe_scheduler = universe_scheduler_status()
    alpha001_health = str(alpha001.get("operational_health") or "UNKNOWN")
    alpha001_entry_evidence = assess_alpha001_entry_execution_continuity(alpha001)
    alpha001_equity = (alpha001.get("equity_recorder") or {}).get("output") or {}
    alpha001_raw_path = (
        alpha001_equity.get("path") if isinstance(alpha001_equity, dict) else None
    )
    if isinstance(alpha001_raw_path, str) and alpha001_raw_path:
        continuity_path = Path(alpha001_raw_path)
        if not continuity_path.is_absolute():
            continuity_path = PROJECT_ROOT / continuity_path
        alpha001_mtm_continuity = assess_mtm_continuity(continuity_path)
    else:
        alpha001_mtm_continuity = {
            "status": "FAIL_CLOSED",
            "path": None,
            "maximum_allowed_gap_seconds": ALPHA001_MAXIMUM_MTM_GAP_SECONDS,
            "reason": "active Alpha001 MTM output path is missing",
        }
    if alpha001_mtm_continuity["status"] != "PASS":
        alpha001_health = "RUNNING_MTM_CONTINUITY_FAILURE"
    alpha007_health = str((alpha007.get("operational") or {}).get("health") or "UNKNOWN")
    alpha007_telemetry_warning = alpha007_nonterminal_performance_warning(alpha007_report)
    if alpha007_protocol_audit["health"] == "FAIL":
        alpha007_health = "COMPLETE_DAY_PROTOCOL_AUDIT_FAIL"
    elif (
        alpha007_collection_closure["present"]
        and alpha007_collection_closure["valid"] is not True
    ):
        alpha007_health = "COLLECTION_SHORTFALL_CLOSURE_INVALID"
    elif alpha007_collection_closure["valid"] is True:
        alpha007_health = "COLLECTION_SHORTFALL_CLOSED"
    elif alpha007_order_path.get("status") in {"INTEGRITY_FAIL", "MISSING_OR_INVALID"}:
        alpha007_health = "ORDER_PATH_POLICY_INTEGRITY_FAIL"
    elif alpha007_telemetry_warning["status"] == "WARNING":
        alpha007_health = "RUNNING_WITH_NEGATIVE_LOCKED_TELEMETRY"
    else:
        alpha007_health = resolve_alpha007_terminal_health(
            alpha007_health, alpha007_report, alpha007_receipt
        )
    alpha011_health = str(alpha011.get("status") or "UNKNOWN")
    alpha011_scheduler_health = str((alpha011.get("scheduler") or {}).get("health") or "UNKNOWN")
    if alpha011_scheduler_health not in {"RUNNING_HEALTHY", "RUNNING_RETRY_PENDING"}:
        alpha011_health = "SCHEDULER_DOWN"
    execution_health = str(execution.get("health") or "UNKNOWN")
    alpha013_scheduler_health = str((alpha013.get("scheduler") or {}).get("health") or "UNKNOWN")
    alpha013_health = resolve_alpha013_health(alpha013, alpha013_scheduler_health)
    universe_health = str(universe.get("status") or "UNKNOWN")
    liquidation_health = str(liquidation_telemetry.get("health") or "UNKNOWN")
    liquidation_scheduler_health = str(
        liquidation_overlay_scheduler.get("health") or "UNKNOWN"
    )
    if liquidation_scheduler_health not in {
        "RUNNING_HEALTHY",
        "RUNNING_RETRY_PENDING",
    }:
        liquidation_health = "OVERLAY_SCHEDULER_DOWN"
    elif str(liquidation_overlay_scheduler.get("research_status") or "") in {
        "DATA_INTEGRITY_FAILURE",
        "MISSING_OR_INVALID",
    }:
        liquidation_health = "OVERLAY_DATA_INTEGRITY_FAILURE"
    universe_scheduler_health = str(universe_scheduler.get("health") or "UNKNOWN")
    alpha015_health = str(alpha015.get("status") or "UNKNOWN")
    if universe_scheduler_health == "RUNNING_RETRY_PENDING":
        universe_health = "COLLECTION_RETRY_PENDING"
    elif universe_scheduler_health != "RUNNING_HEALTHY":
        universe_health = "SCHEDULER_DOWN"
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "status": classify_research_health(
            alpha001_health,
            alpha007_health,
            alpha011_health,
            execution_health,
            alpha013_health,
            universe_health,
            liquidation_health,
            alpha015_health,
            alpha001_entry_evidence_health=alpha001_entry_evidence["status"],
            risk_policy_health=str(policy_contract.get("status") or "FAIL"),
        ),
        "capital_preservation_policy": policy_contract,
        "alpha001_forward": {
            "health": alpha001_health,
            "protocol_current": alpha001.get("protocol_current"),
            "manifest_valid": (alpha001.get("equity_manifest_health") or {}).get("valid"),
            "bot_running": alpha001.get("running"),
            "recorder_running": (alpha001.get("equity_recorder") or {}).get("running"),
            "observations": ((alpha001.get("equity_recorder") or {}).get("output") or {}).get("observations"),
            "trades": (alpha001.get("database") or {}).get("trades"),
            "sleep_guard": (alpha001.get("sleep_guard") or {}).get("running"),
            "entry_execution_recorder_running": (
                alpha001.get("entry_execution_recorder") or {}
            ).get("running"),
            "entry_execution_orders": (
                alpha001.get("entry_execution_manifest_health") or {}
            ).get("orders"),
            "entry_execution_manifest_valid": (
                alpha001.get("entry_execution_manifest_health") or {}
            ).get("valid"),
            "entry_execution_evidence_continuity": alpha001_entry_evidence,
            "mtm_continuity": alpha001_mtm_continuity,
        },
        "alpha007_microstructure": {
            "health": alpha007_health,
            "supervisor_running": (alpha007.get("operational") or {}).get("supervisor_running"),
            "collector_running": (alpha007.get("operational") or {}).get("collector_running"),
            "completed_chunks": alpha007.get("completed_chunks"),
            "failed_chunks": alpha007.get("failed_chunks"),
            "latest_audit": (alpha007.get("latest_completed_run_audit") or {}).get("status"),
            "target_still_reachable": (alpha007.get("coverage_budget") or {}).get("target_still_reachable"),
            "sealed_success_hours": round(
                float((alpha007.get("coverage_budget") or {}).get("sealed_success_seconds") or 0) / 3600,
                3,
            ),
            "sleep_guard": (alpha007.get("sleep_guard") or {}).get("running"),
            "complete_day_protocol_audit": alpha007_protocol_audit,
            "shadow_report_status": alpha007_report.get("status"),
            "collection_shortfall_closure": alpha007_collection_closure,
            "nonterminal_performance_warning": alpha007_telemetry_warning,
            "terminal_receipt_valid": alpha007_receipt.get("valid"),
            "partial_fill_veto_status": alpha007_receipt.get(
                "partial_fill_veto_status"
            ),
            "order_path_status": alpha007_order_path.get("status"),
            "order_path_blockers": alpha007_order_path.get("blockers"),
            "order_path_live_order_authorization": alpha007_order_path.get(
                "live_order_authorization"
            ),
            "terminal_registry_status": (
                "FAIL"
                if alpha007_collection_closure.get("valid") is True
                else "REVIEW"
                if alpha007_report.get("status") == "PASS"
                else alpha007_report.get("status")
            ),
        },
        "alpha011_forward": {
            "health": alpha011_health,
            "eligible_start": alpha011.get("eligible_start"),
            "closed_development_days": alpha011.get("closed_development_days"),
            "development_days_remaining": alpha011.get("development_days_remaining"),
            "missing_archive_days": alpha011.get("missing_archive_days"),
            "pending_publication_days": alpha011.get("pending_publication_days"),
            "overdue_missing_archive_days": alpha011.get(
                "overdue_missing_archive_days"
            ),
            "performance_calculated": alpha011.get("performance_calculated"),
            "development_report_status": alpha011.get("development_report_status"),
            "development_receipt_valid": (
                alpha011.get("development_receipt") or {}
            ).get("valid"),
            "locked_validation_opened": alpha011.get("locked_validation_opened"),
            "validation_lock_valid": alpha011.get("validation_lock_valid"),
            "closed_validation_days": alpha011.get("closed_validation_days"),
            "validation_days_remaining": alpha011.get("validation_days_remaining"),
            "validation_report_status": alpha011.get("validation_report_status"),
            "scheduler_health": alpha011_scheduler_health,
            "scheduler_running": (alpha011.get("scheduler") or {}).get("running"),
            "scheduler_last_exit_code": (alpha011.get("scheduler") or {}).get("last_exit_code"),
            "scheduler_next_run_at": (alpha011.get("scheduler") or {}).get("next_run_at"),
        },
        "alpha013_forward": {
            "health": alpha013_health,
            "experiment_id": alpha013.get("experiment_id"),
            "registered_at_utc": alpha013.get("registered_at_utc"),
            "first_eligible_decision": alpha013.get("first_eligible_decision"),
            "valid_complete_days": alpha013.get("valid_complete_days"),
            "missing_complete_days": alpha013.get("missing_complete_days"),
            "pending_publication_days": alpha013.get("pending_publication_days"),
            "overdue_missing_days": alpha013.get("overdue_missing_days"),
            "invalid_days": alpha013.get("invalid_days"),
            "forward_report_current": alpha013.get("forward_report_current"),
            "source_inventory_sha256": alpha013.get("source_inventory_sha256"),
            "development_weeks_closed": alpha013.get("development_weeks_closed"),
            "development_weeks_required": alpha013.get("development_weeks_required"),
            "locked_validation_opened": alpha013.get("locked_validation_opened"),
            "validation_lock_valid": alpha013.get("validation_lock_valid"),
            "validation_weeks_closed": alpha013.get("validation_weeks_closed"),
            "validation_weeks_required": alpha013.get("validation_weeks_required"),
            "validation_report_status": alpha013.get("validation_report_status"),
            "scheduler_health": alpha013_scheduler_health,
            "scheduler_running": (alpha013.get("scheduler") or {}).get("running"),
            "scheduler_last_exit_code": (alpha013.get("scheduler") or {}).get("last_exit_code"),
            "scheduler_next_run_at": (alpha013.get("scheduler") or {}).get("next_run_at"),
        },
        "periodic_execution_calibration": {
            "health": execution_health,
            "running": execution.get("running"),
            "protocol_current": execution.get("protocol_current"),
            "orders": (execution.get("progress") or {}).get("orders"),
            "target": (execution.get("progress") or {}).get("target"),
            "fills": (execution.get("progress") or {}).get("fills"),
            "coverage_failures": (execution.get("progress") or {}).get("coverage_failures"),
            "sleep_guard": (execution.get("sleep_guard") or {}).get("running"),
        },
        "point_in_time_universe": {
            "health": universe_health,
            "eligible_start": universe.get("eligible_start"),
            "current_protocol_days": universe.get("current_protocol_days"),
            "consecutive_days_ending_latest": universe.get(
                "consecutive_days_ending_latest"
            ),
            "required_consecutive_days": universe.get("required_consecutive_days"),
            "missing_due_days": universe.get("missing_due_days"),
            "invalid_snapshots": universe.get("invalid_snapshots"),
            "chain_failures": universe.get("chain_failures"),
            "scheduler_health": universe_scheduler_health,
            "scheduler_running": universe_scheduler.get("running"),
            "scheduler_next_run_at": universe_scheduler.get("next_run_at"),
            "sleep_guard": (universe_scheduler.get("sleep_guard") or {}).get("running"),
        },
        "alpha015_candidate": {
            "health": alpha015_health,
            "experiment_registered": alpha015.get("experiment_registered"),
            "preregistration_authorized": alpha015.get(
                "preregistration_authorized"
            ),
            "return_data_authorized": alpha015.get("return_data_authorized"),
            "current_consecutive_universe_days": alpha015.get(
                "current_consecutive_universe_days"
            ),
            "required_consecutive_universe_days": alpha015.get(
                "required_consecutive_universe_days"
            ),
            "days_remaining": alpha015.get("days_remaining"),
            "diagnostic_milestones": build_alpha015_collection_milestones(
                current_days=int(alpha015.get("current_consecutive_universe_days") or 0),
                universe_status=str(alpha015.get("status") or "MISSING"),
            ),
            "return_free_universe_diagnostic": alpha015_universe_diagnostic,
            "unresolved_methodology": alpha015.get("unresolved_methodology"),
            "checks": alpha015.get("checks"),
            "artifact_integrity": (
                alpha015_methodology.get("artifact_integrity") or {}
            ).get("status"),
            "live_order_authorization": "CLOSED",
        },
        "liquidation_risk_telemetry": {
            "health": liquidation_health,
            "running": liquidation_telemetry.get("running"),
            "status": liquidation_telemetry.get("status"),
            "eligible_start": liquidation_telemetry.get("eligible_start"),
            "completed_chunks": liquidation_telemetry.get("completed_chunks"),
            "failed_chunks": liquidation_telemetry.get("failed_chunks"),
            "latest_manifest_audit": (
                liquidation_telemetry.get("latest_manifest_audit") or {}
            ).get("status"),
            "sleep_guard": (
                liquidation_telemetry.get("sleep_guard") or {}
            ).get("running"),
            "overlay_scheduler_health": liquidation_scheduler_health,
            "overlay_scheduler_running": liquidation_overlay_scheduler.get("running"),
            "overlay_scheduler_next_run_at": liquidation_overlay_scheduler.get(
                "next_run_at"
            ),
            "research_status": liquidation_overlay_scheduler.get("research_status"),
            "validation_lock_present": liquidation_overlay_scheduler.get(
                "validation_lock_present"
            ),
        },
    }


def main() -> None:
    report = build_health_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "ATTENTION_REQUIRED":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
