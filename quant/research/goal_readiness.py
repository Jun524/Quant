"""Fail-closed progress view for the capital-preservation futures objective.

This is an observability layer, not another promotion policy.  It reports
which existing promotion proofs are present without converting calibration,
diagnostic backtests, or accumulating samples into evidence.
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Any

from quant.research.portfolio_promotion import (
    PORTFOLIO_MONTHLY_POLICY,
    PROMOTION_PROTOCOL_FILES,
    PortfolioPromotionPolicy,
)


def audit_portfolio_promotion_receipt(
    receipt: dict[str, Any],
    *,
    project_root: Path,
    current_policy: Path,
) -> dict[str, Any]:
    """Rehash every promotion input and reject receipts from an older policy."""
    failures: list[str] = []
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict):
        return {"status": "FAIL", "failures": ["receipt inputs are missing"]}

    def resolve(raw: Any) -> Path | None:
        if not isinstance(raw, str) or not raw:
            return None
        path = Path(raw)
        return path if path.is_absolute() else project_root / path

    def verify(label: str, raw_path: Any, expected_digest: Any) -> None:
        path = resolve(raw_path)
        if path is None or not isinstance(expected_digest, str):
            failures.append(f"{label} path or digest is missing")
            return
        try:
            actual = sha256(path.read_bytes()).hexdigest()
        except OSError:
            failures.append(f"{label} source is unreadable")
            return
        if actual != expected_digest:
            failures.append(f"{label} source digest mismatch")

    verify("manifest", inputs.get("manifest"), inputs.get("manifest_sha256"))
    verify("policy", inputs.get("policy"), inputs.get("policy_sha256"))
    verify(
        "portfolio report",
        inputs.get("portfolio_report"),
        inputs.get("portfolio_report_sha256"),
    )
    verify(
        "execution audit",
        inputs.get("execution_audit"),
        inputs.get("execution_audit_sha256"),
    )
    execution_observations = inputs.get("execution_observations")
    if not isinstance(execution_observations, dict):
        failures.append("portfolio execution observation source is missing")
    else:
        verify(
            "portfolio execution observation manifest",
            execution_observations.get("path"),
            execution_observations.get("sha256"),
        )
        execution_segments = execution_observations.get("segments")
        if not isinstance(execution_segments, list) or not execution_segments:
            failures.append("portfolio execution sealed segment inventory is missing")
        else:
            for index, segment in enumerate(execution_segments):
                if not isinstance(segment, dict):
                    failures.append(f"portfolio execution segment {index} is invalid")
                    continue
                verify(
                    f"portfolio execution segment {index}",
                    segment.get("path"),
                    segment.get("sha256"),
                )
                market_manifest = segment.get("market_data_manifest")
                if not isinstance(market_manifest, dict):
                    failures.append(
                        f"portfolio execution segment {index} market-data manifest is missing"
                    )
                    continue
                verify(
                    f"portfolio execution segment {index} market-data manifest",
                    market_manifest.get("path"),
                    market_manifest.get("sha256"),
                )
                market_files = market_manifest.get("files")
                if not isinstance(market_files, list) or not market_files:
                    failures.append(
                        f"portfolio execution segment {index} market-data files are missing"
                    )
                    continue
                for file_index, source_file in enumerate(market_files):
                    if not isinstance(source_file, dict):
                        failures.append(
                            f"portfolio execution segment {index} market-data file {file_index} is invalid"
                        )
                        continue
                    verify(
                        f"portfolio execution segment {index} market-data file {file_index}",
                        source_file.get("path"),
                        source_file.get("sha256"),
                    )
    sleeve_sources = inputs.get("sleeve_reports")
    if not isinstance(sleeve_sources, dict) or not sleeve_sources:
        failures.append("sleeve report sources are missing")
    else:
        for candidate, source in sleeve_sources.items():
            if not isinstance(source, dict):
                failures.append(f"{candidate} sleeve source is invalid")
                continue
            verify(
                f"{candidate} sleeve report",
                source.get("path"),
                source.get("sha256"),
            )
    portfolio_selection_source = inputs.get("portfolio_selection_bias_audit")
    if not isinstance(portfolio_selection_source, dict):
        failures.append("portfolio selection-bias audit source is missing")
    else:
        verify(
            "portfolio selection-bias audit",
            portfolio_selection_source.get("path"),
            portfolio_selection_source.get("sha256"),
        )
        matrix_source = portfolio_selection_source.get("trial_return_matrix")
        if not isinstance(matrix_source, dict):
            failures.append("portfolio trial return matrix source is missing")
        else:
            verify(
                "portfolio trial return matrix",
                matrix_source.get("path"),
                matrix_source.get("sha256"),
            )
        inventory_source = portfolio_selection_source.get("trial_inventory")
        if not isinstance(inventory_source, dict):
            failures.append("portfolio sealed trial inventory source is missing")
        else:
            verify(
                "portfolio sealed trial inventory",
                inventory_source.get("path"),
                inventory_source.get("sha256"),
            )
    portfolio_risk_source = inputs.get("portfolio_risk_audit")
    if not isinstance(portfolio_risk_source, dict):
        failures.append("portfolio risk audit source is missing")
    else:
        verify(
            "portfolio risk audit",
            portfolio_risk_source.get("path"),
            portfolio_risk_source.get("sha256"),
        )
        equity_source = portfolio_risk_source.get("mark_to_market_equity")
        if not isinstance(equity_source, dict):
            failures.append("portfolio risk mark-to-market equity source is missing")
        else:
            verify(
                "portfolio risk mark-to-market equity",
                equity_source.get("path"),
                equity_source.get("sha256"),
            )
    portfolio_contribution_source = inputs.get("portfolio_contributions")
    if not isinstance(portfolio_contribution_source, dict):
        failures.append("portfolio contribution matrix source is missing")
    else:
        verify(
            "portfolio contribution matrix",
            portfolio_contribution_source.get("path"),
            portfolio_contribution_source.get("sha256"),
        )
    portfolio_sleeve_pnl_source = inputs.get("portfolio_sleeve_pnl")
    if not isinstance(portfolio_sleeve_pnl_source, dict):
        failures.append("portfolio sleeve PnL matrix source is missing")
    else:
        verify(
            "portfolio sleeve PnL matrix",
            portfolio_sleeve_pnl_source.get("path"),
            portfolio_sleeve_pnl_source.get("sha256"),
        )
    portfolio_performance_sources = inputs.get("portfolio_performance")
    if (
        not isinstance(portfolio_performance_sources, dict)
        or set(portfolio_performance_sources) != {
            "trades",
            "exposure",
            "accounting",
        }
    ):
        failures.append("portfolio performance source inventory is missing")
    else:
        for name, source in portfolio_performance_sources.items():
            if not isinstance(source, dict):
                failures.append(f"portfolio performance {name} source is invalid")
                continue
            verify(
                f"portfolio performance {name}",
                source.get("path"),
                source.get("sha256"),
            )
    sleeve_selection_sources = inputs.get("sleeve_selection_bias_audits")
    if (
        not isinstance(sleeve_selection_sources, dict)
        or not isinstance(sleeve_sources, dict)
        or set(sleeve_selection_sources) != set(sleeve_sources)
    ):
        failures.append("sleeve selection-bias audit source inventory is not exact")
    else:
        for candidate, source in sleeve_selection_sources.items():
            if not isinstance(source, dict):
                failures.append(f"{candidate} selection-bias source is invalid")
                continue
            verify(
                f"{candidate} selection-bias audit",
                source.get("path"),
                source.get("sha256"),
            )
            matrix_source = source.get("trial_return_matrix")
            if not isinstance(matrix_source, dict):
                failures.append(f"{candidate} trial return matrix source is missing")
            else:
                verify(
                    f"{candidate} trial return matrix",
                    matrix_source.get("path"),
                    matrix_source.get("sha256"),
                )
            inventory_source = source.get("trial_inventory")
            if not isinstance(inventory_source, dict):
                failures.append(f"{candidate} sealed trial inventory source is missing")
            else:
                verify(
                    f"{candidate} sealed trial inventory",
                    inventory_source.get("path"),
                    inventory_source.get("sha256"),
                )
    evaluator_protocol = inputs.get("evaluator_protocol_sha256")
    if not isinstance(evaluator_protocol, dict) or set(evaluator_protocol) != set(
        PROMOTION_PROTOCOL_FILES
    ):
        failures.append("evaluator protocol source inventory is not exact")
    else:
        for relative in PROMOTION_PROTOCOL_FILES:
            verify(
                f"evaluator protocol {relative}",
                relative,
                evaluator_protocol.get(relative),
            )
    try:
        current_digest = sha256(current_policy.read_bytes()).hexdigest()
    except OSError:
        failures.append("current portfolio policy is unreadable")
    else:
        if inputs.get("policy_sha256") != current_digest:
            failures.append("receipt is bound to an obsolete portfolio policy")
    return {"status": "PASS" if not failures else "FAIL", "failures": failures}


def _named_check(report: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not isinstance(report, dict):
        return None
    for item in report.get("checks") or []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _group_status(
    report: dict[str, Any] | None,
    names: tuple[str, ...],
) -> tuple[str, list[dict[str, Any]]]:
    checks = [_named_check(report, name) for name in names]
    available = [item for item in checks if item is not None]
    if not available:
        return "NOT_OPENED", []
    if len(available) != len(names):
        return "INCOMPLETE_EVIDENCE", available
    return (
        "PASS" if all(item.get("passed") is True for item in available) else "NOT_PASSING",
        available,
    )


def build_goal_readiness(
    scoreboard: dict[str, Any],
    research_health: dict[str, Any],
    *,
    portfolio_promotion: dict[str, Any] | None = None,
    alpha001_forward: dict[str, Any] | None = None,
    alpha001_bias_audit: dict[str, Any] | None = None,
    aoa_forward: dict[str, Any] | None = None,
    historical_evidence_policy: dict[str, Any] | None = None,
    historical_cost_policy: dict[str, Any] | None = None,
    historical_execution_coverage: dict[str, Any] | None = None,
    historical_funding_coverage: dict[str, Any] | None = None,
    historical_funding_ledger_audit: dict[str, Any] | None = None,
    portfolio_receipt_integrity: dict[str, Any] | None = None,
    research_objective: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an honest readiness matrix from current machine reports."""
    status_counts = scoreboard.get("status_counts") or {}
    details = scoreboard.get("experiments_detail") or []
    active = [
        {
            "experiment_id": item.get("experiment_id"),
            "strategy": item.get("strategy"),
            "status": item.get("status"),
        }
        for item in details
        if isinstance(item, dict) and item.get("status") == "ACCUMULATING"
    ]
    research_pass_candidates = int(status_counts.get("PASS") or 0)
    raw_portfolio_status = (
        str(portfolio_promotion.get("status"))
        if isinstance(portfolio_promotion, dict)
        else "NOT_OPENED"
    )
    receipt_integrity = portfolio_receipt_integrity or {
        "status": "NOT_OPENED" if portfolio_promotion is None else "NOT_AUDITED",
        "failures": [],
    }
    portfolio_status = raw_portfolio_status
    if raw_portfolio_status != "NOT_OPENED" and receipt_integrity.get("status") == "FAIL":
        portfolio_status = "STALE_OR_INVALID_EVIDENCE"

    monthly_status, monthly_checks = _group_status(
        portfolio_promotion,
        (
            "portfolio_performance_source_replay",
            "reported_performance_matches_source",
            "locked_sample_source_window_binding",
            "combined_monthly_target_gate",
            "daily_mtm_profit_factor",
            "boundary_force_exit_integrity",
            "common_daily_observations",
        ),
    )
    risk_status, risk_checks = _group_status(
        portfolio_promotion,
        (
            "portfolio_composition_source_replay",
            "reported_composition_matches_source",
            "maximum_gross_leverage",
            "maximum_absolute_net_leverage",
            "intraday_exposure_reconciliation",
            "maximum_intratrade_leverage_adjusted_mae",
            "zero_liquidations",
            "maximum_absolute_pairwise_correlation",
            "single_sleeve_positive_pnl_concentration",
            "portfolio_multi_block_risk",
        ),
    )
    cost_status, cost_checks = _group_status(
        portfolio_promotion,
        (
            "simultaneous_cost_funding_stress",
            "preregistered_execution_cost_floor",
            "complete_execution_path_evidence",
        ),
    )
    sleeve_status, sleeve_checks = _group_status(
        portfolio_promotion,
        ("standalone_sleeve_promotion", "current_sleeve_bias_audits"),
    )
    selection_bias_status, selection_bias_checks = _group_status(
        portfolio_promotion,
        ("portfolio_selection_bias", "current_sleeve_selection_bias_audits"),
    )

    alpha001_gate = (alpha001_forward or {}).get("gate") or {}
    alpha001_monthly = (alpha001_forward or {}).get("mtm", {}).get("monthly") or {}
    alpha001_trades = (alpha001_forward or {}).get("trades") or {}
    alpha001_bias_pass = bool(
        (alpha001_bias_audit or {}).get("status") == "PASS"
        and (alpha001_bias_audit or {}).get("lookahead", {}).get("passed") is True
        and (alpha001_bias_audit or {}).get("recursive", {}).get("passed") is True
    )
    auxiliary = scoreboard.get("auxiliary_gates") or {}
    supporting_non_promotion = {
        name: {
            "status": item.get("status"),
            "promotion_eligible": bool(item.get("promotion_eligible", False)),
        }
        for name, item in auxiliary.items()
        if isinstance(item, dict)
    }
    if isinstance(aoa_forward, dict):
        supporting_non_promotion["aoa_locked_holdout"] = {
            "status": aoa_forward.get("status"),
            "promotion_eligible": bool(aoa_forward.get("promotion_eligible", False)),
            "organic_trades": (aoa_forward.get("metrics") or {}).get(
                "organic_trades"
            ),
            "sample_role": aoa_forward.get("sample_role"),
        }
    if isinstance(historical_evidence_policy, dict):
        historical_oi_check = _named_check(
            historical_evidence_policy,
            "historical_open_interest_blocked_without_publication_time",
        )
        supporting_non_promotion["historical_evidence_policy"] = {
            "status": historical_evidence_policy.get("status"),
            "promotion_eligible": False,
            "kind": historical_evidence_policy.get("kind"),
            "historical_point_in_time_available_now": historical_evidence_policy.get(
                "historical_point_in_time_available_now"
            ),
            "historical_open_interest_decision_feature_blocked": (
                historical_oi_check.get("passed")
                if isinstance(historical_oi_check, dict)
                else None
            ),
        }
    if isinstance(historical_cost_policy, dict):
        supporting_non_promotion["historical_cost_policy"] = {
            "status": historical_cost_policy.get("status"),
            "promotion_eligible": False,
            "kind": historical_cost_policy.get("kind"),
        }
    for name, report in (
        ("historical_next_open_coverage", historical_execution_coverage),
        ("historical_one_day_funding_coverage", historical_funding_coverage),
        ("historical_funding_ledger_replay", historical_funding_ledger_audit),
    ):
        if isinstance(report, dict):
            supporting_non_promotion[name] = {
                "status": report.get("status"),
                "promotion_eligible": False,
                "kind": report.get("kind"),
                "universe_sha256": report.get("universe_sha256"),
            }

    phases = [
        {
            "name": "forward_research_operations",
            "status": "PASS" if research_health.get("status") == "HEALTHY" else "ATTENTION",
            "actual": research_health.get("status"),
            "required": "HEALTHY",
        },
        {
            "name": "independent_locked_forward_sleeves",
            "status": sleeve_status,
            "actual": research_pass_candidates,
            "required": asdict(PortfolioPromotionPolicy())["minimum_sleeves"],
            "note": "Registry PASS count is informational; only the portfolio gate verifies locked-forward sleeve evidence.",
            "checks": sleeve_checks,
        },
        {
            "name": "combined_monthly_objective",
            "status": monthly_status,
            "actual": portfolio_status,
            "required": "> 0 cost-after geometric monthly return; trade and daily MTM PF >= 1.20; >= 12 complete locked-forward months; >= 100 organic trades; force_exit only at the exact sample end. Monthly 10% remains aspirational and is reported separately.",
            "checks": monthly_checks,
        },
        {
            "name": "combined_downside_and_diversification",
            "status": risk_status,
            "actual": portfolio_status,
            "required": "MDD <= 10%, intratrade leverage-adjusted MAE <= 20%, trade-overlap exposure reconciliation, bootstrap ruin <= 0.5%, deterministic worst-block recurrence MDD <= 10%, zero liquidations, 1x leverage/correlation/concentration limits",
            "checks": risk_checks,
        },
        {
            "name": "combined_cost_and_execution_paths",
            "status": cost_status,
            "actual": portfolio_status,
            "required": "positive simultaneous fee+slippage+funding stress, per-trade accounting costs covering preregistered scope budgets, and every entry/exit/stop scope PASS",
            "checks": cost_checks,
        },
        {
            "name": "selection_bias_control",
            "status": selection_bias_status,
            "actual": portfolio_status,
            "required": "complete trial inventories; DSR probability >= 95%; CSCV PBO <= 10%",
            "checks": selection_bias_checks,
        },
        {
            "name": "multi_strategy_live_adapter",
            "status": "DEFERRED_NOT_BUILT",
            "required": "build only after a preregistered portfolio passes all research gates",
            "note": "The current live preflight is intentionally Alpha #1-specific.",
        },
        {
            "name": "live_order_authorization",
            "status": "CLOSED",
            "required": "separate live preflight after portfolio promotion PASS",
        },
    ]

    blockers: list[str] = []
    entry_evidence = (
        (research_health.get("alpha001_forward") or {}).get(
            "entry_execution_evidence_continuity"
        )
        or {}
    )
    if entry_evidence.get("status") == "EVIDENCE_DISCONTINUITY":
        blockers.append(
            "Alpha #1 entry-execution evidence contains an unreplayed observation interval; "
            "it is ineligible for any future promotion evidence even while the replacement recorder runs."
        )
    if research_pass_candidates < PortfolioPromotionPolicy().minimum_sleeves:
        blockers.append(
            f"No eligible portfolio yet: {research_pass_candidates} research PASS candidates; "
            f"at least {PortfolioPromotionPolicy().minimum_sleeves} independently locked-forward PASS sleeves are required."
        )
    alpha007_filter = supporting_non_promotion.get(
        "alpha007_exchange_filter_feasibility"
    )
    if isinstance(alpha007_filter, dict) and alpha007_filter.get("status") == "FAIL":
        blockers.append(
            "Alpha 007's frozen fixed-notional order path is not exchange-filter "
            "feasible; it remains research-only and cannot be repaired by silently "
            "changing its order size."
        )
    if portfolio_status == "NOT_OPENED":
        blockers.append("No preregistered portfolio candidate or combined locked-forward promotion report exists.")
    elif portfolio_status != "PASS":
        failed = [
            str(item.get("name"))
            for item in (portfolio_promotion or {}).get("checks") or []
            if isinstance(item, dict) and item.get("passed") is not True
        ]
        blockers.append("Portfolio promotion is not PASS: " + ", ".join(failed))
    blockers.append(
        "The multi-strategy live adapter and portfolio-specific live preflight are intentionally not built before a portfolio PASS."
    )
    blockers.append("Live order authorization remains closed; calibration and dry-run evidence cannot authorize orders.")

    objective_proven = (
        portfolio_status == "PASS" and receipt_integrity.get("status") == "PASS"
    )
    if objective_proven:
        overall = "PROMOTION_PREREQUISITES_PASS_LIVE_CLOSED"
    elif research_health.get("status") in {"HEALTHY", "DEGRADED"}:
        overall = "RESEARCH_ACTIVE_NOT_LIVE_READY"
    else:
        overall = "RESEARCH_ATTENTION_REQUIRED"

    return_objective = (
        (research_objective or {}).get("return_objective")
        if isinstance(research_objective, dict)
        else None
    )
    if not isinstance(return_objective, dict):
        # Keep the headline target visible even when the optional policy
        # artifact is absent.  It is deliberately not added to promotion
        # thresholds: capital preservation and positive cost-after return
        # remain the fail-closed requirements.
        return_objective = {
            "aspirational_net_geometric_monthly_return": 0.10,
            "promotion_requirement": False,
            "required_direction": "positive_cost_after_return",
            "warning": (
                "Ten percent per month is an ambition, not a promise, a sizing "
                "instruction, or a reason to increase leverage or turnover."
            ),
        }

    research_cadence = (
        (research_objective or {}).get("research_cadence")
        if isinstance(research_objective, dict)
        else None
    )
    if not isinstance(research_cadence, dict):
        research_cadence = {
            "historical_research": "Start immediately from sealed historical data.",
            "forward_review_days": [30, 60, 90],
            "promotion_rule": "No universal 180-day wait; promotion remains evidence-gated.",
        }

    return {
        "schema_version": 1,
        "objective": "Binance USDT-M futures portfolio: capital preservation and positive cost-after return are required; 10% net geometric monthly return is aspirational",
        "status": overall,
        "objective_proven": objective_proven,
        "live_order_authorization": "CLOSED",
        "research_health": research_health.get("status"),
        "thresholds": {
            **asdict(PORTFOLIO_MONTHLY_POLICY),
            "portfolio": asdict(PortfolioPromotionPolicy()),
        },
        "return_target": return_objective,
        "research_cadence": research_cadence,
        "experiment_counts": {
            "total": int(scoreboard.get("experiments") or 0),
            **{str(key): int(value) for key, value in status_counts.items()},
        },
        "active_forward_candidates": active,
        "portfolio_promotion_status": portfolio_status,
        "portfolio_promotion_reported_status": raw_portfolio_status,
        "portfolio_receipt_integrity": receipt_integrity,
        "phases": phases,
        "current_single_candidate_evidence": {
            "candidate": (alpha001_forward or {}).get("candidate"),
            "forward_status": (alpha001_forward or {}).get("status"),
            "monthly_gate_status": alpha001_gate.get("status"),
            "complete_months": alpha001_monthly.get("complete_months"),
            "natural_closed_trades": alpha001_trades.get("natural_closed_trades"),
            "current_bias_audit_pass": alpha001_bias_pass,
            "entry_execution_evidence_continuity": entry_evidence.get("status"),
            "entry_execution_promotion_evidence_eligible": entry_evidence.get(
                "promotion_evidence_eligible"
            ),
            "not_portfolio_proof": True,
        },
        "supporting_non_promotion_gates": supporting_non_promotion,
        "ordered_blockers": blockers,
        "interpretation": (
            "The 10% monthly target is aspirational rather than a promotion requirement. "
            "Positive cost-after returns and the stricter capital-preservation gates remain mandatory. "
            "This readiness report never promotes calibration, backtests, or partial forward samples."
        ),
    }
