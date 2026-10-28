#!/usr/bin/env python3
"""Build a fail-closed multi-strategy portfolio promotion report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from dataclasses import asdict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.portfolio_promotion import (
    PROMOTION_PROTOCOL_FILES,
    PortfolioPromotionPolicy,
    evaluate_portfolio_promotion,
    expected_execution_scopes,
)
from quant.research.portfolio_execution import (
    PORTFOLIO_EXECUTION_PROTOCOL_FILES,
    evaluate_portfolio_execution,
    load_execution_observation_manifest,
)
from quant.research.portfolio_composition import (
    PORTFOLIO_COMPOSITION_PROTOCOL_FILES,
    evaluate_portfolio_composition,
    load_portfolio_contributions,
    load_sleeve_daily_pnl,
)
from quant.research.equity import load_mark_to_market_equity
from quant.research.portfolio_performance import (
    PORTFOLIO_PERFORMANCE_PROTOCOL_FILES,
    evaluate_portfolio_performance,
    load_portfolio_accounting,
    load_portfolio_exposure,
    load_portfolio_trades,
)
from quant.research.selection_bias import (
    SELECTION_BIAS_PROTOCOL_FILES,
    evaluate_selection_bias,
    load_daily_trial_matrix,
    validate_trial_inventory,
)
from quant.research.portfolio_risk import (
    PORTFOLIO_RISK_PROTOCOL_FILES,
    evaluate_portfolio_risk,
    load_portfolio_equity,
)


def _decode(raw: bytes, path: Path) -> dict:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def _parse_sleeve_report(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("use CANDIDATE=path.json")
    candidate, raw_path = value.split("=", 1)
    if not candidate or not raw_path:
        raise argparse.ArgumentTypeError("candidate and path must be non-empty")
    return candidate, Path(raw_path)


def _load_selection_bias_audit(path: Path) -> tuple[dict, dict[str, object]]:
    raw = path.read_bytes()
    audit = _decode(raw, path)
    protocol = audit.get("protocol_sha256")
    if not isinstance(protocol, dict) or set(protocol) != set(SELECTION_BIAS_PROTOCOL_FILES):
        raise SystemExit(f"{path} has an invalid selection-bias protocol inventory")
    for relative in SELECTION_BIAS_PROTOCOL_FILES:
        current = sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
        if protocol.get(relative) != current:
            raise SystemExit(f"{path} is bound to an obsolete selection-bias protocol")
    source = audit.get("source")
    if not isinstance(source, dict):
        raise SystemExit(f"{path} selection-bias matrix source is missing")
    raw_source_path = source.get("path")
    expected_source_digest = source.get("sha256")
    if not isinstance(raw_source_path, str) or not isinstance(expected_source_digest, str):
        raise SystemExit(f"{path} selection-bias matrix path or digest is missing")
    source_path = Path(raw_source_path)
    if not source_path.is_absolute():
        source_path = PROJECT_ROOT / source_path
    try:
        actual_source_digest = sha256(source_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise SystemExit(f"{path} selection-bias matrix is unreadable: {exc}") from exc
    if actual_source_digest != expected_source_digest:
        raise SystemExit(f"{path} selection-bias matrix digest mismatch")
    inventory_source = audit.get("trial_inventory")
    if not isinstance(inventory_source, dict):
        raise SystemExit(f"{path} sealed trial inventory source is missing")
    raw_inventory_path = inventory_source.get("path")
    expected_inventory_digest = inventory_source.get("sha256")
    if not isinstance(raw_inventory_path, str) or not isinstance(
        expected_inventory_digest, str
    ):
        raise SystemExit(f"{path} sealed trial inventory path or digest is missing")
    inventory_path = Path(raw_inventory_path)
    if not inventory_path.is_absolute():
        inventory_path = PROJECT_ROOT / inventory_path
    try:
        inventory_bytes = inventory_path.read_bytes()
        actual_inventory_digest = sha256(inventory_bytes).hexdigest()
    except OSError as exc:
        raise SystemExit(f"{path} sealed trial inventory is unreadable: {exc}") from exc
    if actual_inventory_digest != expected_inventory_digest:
        raise SystemExit(f"{path} sealed trial inventory digest mismatch")
    timestamp_column = source.get("timestamp_column")
    if not isinstance(timestamp_column, str) or not timestamp_column:
        raise SystemExit(f"{path} timestamp column is missing")
    try:
        timestamps, trial_ids, matrix = load_daily_trial_matrix(
            source_path, timestamp_column
        )
        inventory = json.loads(inventory_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{path} source replay failed: {exc}") from exc
    validation = validate_trial_inventory(
        inventory,
        trial_ids=trial_ids,
        first_observation_at=timestamps[0],
    )
    if validation["status"] != "PASS":
        raise SystemExit(
            f"{path} sealed trial inventory replay failed: "
            + "; ".join(validation["failures"])
        )
    selected_trial = str(validation["selected_trial_id"])
    selected_column = trial_ids.index(selected_trial)
    recomputed = evaluate_selection_bias(
        matrix[:, selected_column],
        matrix,
        attempted_trials=len(trial_ids),
        trial_ids=trial_ids,
        selected_trial_id=selected_trial,
        trial_inventory_complete=True,
        pre_registered_single_trial=bool(validation["pre_registered_single_trial"]),
    )
    if any(audit.get(key) != value for key, value in recomputed.items()):
        raise SystemExit(f"{path} statistical audit does not match source replay")
    if (
        source.get("first_timestamp") != timestamps[0]
        or source.get("last_timestamp") != timestamps[-1]
        or source.get("rows") != len(timestamps)
        or inventory_source.get("validation") != validation
    ):
        raise SystemExit(f"{path} source metadata does not match source replay")
    return audit, {
        "path": str(path),
        "sha256": sha256(raw).hexdigest(),
        "trial_return_matrix": {
            "path": str(source_path),
            "sha256": actual_source_digest,
        },
        "trial_inventory": {
            "path": str(inventory_path),
            "sha256": actual_inventory_digest,
        },
    }


def _load_portfolio_risk_audit(path: Path) -> tuple[dict, dict[str, object]]:
    raw = path.read_bytes()
    audit = _decode(raw, path)
    protocol = audit.get("protocol_sha256")
    if not isinstance(protocol, dict) or set(protocol) != set(PORTFOLIO_RISK_PROTOCOL_FILES):
        raise SystemExit(f"{path} has an invalid portfolio-risk protocol inventory")
    for relative in PORTFOLIO_RISK_PROTOCOL_FILES:
        current = sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
        if protocol.get(relative) != current:
            raise SystemExit(f"{path} is bound to an obsolete portfolio-risk protocol")
    source = audit.get("source")
    if not isinstance(source, dict):
        raise SystemExit(f"{path} portfolio-risk source is missing")
    raw_source_path = source.get("path")
    expected_digest = source.get("sha256")
    if not isinstance(raw_source_path, str) or not isinstance(expected_digest, str):
        raise SystemExit(f"{path} portfolio-risk source path or digest is missing")
    source_path = Path(raw_source_path)
    if not source_path.is_absolute():
        source_path = PROJECT_ROOT / source_path
    try:
        source_bytes = source_path.read_bytes()
        actual_digest = sha256(source_bytes).hexdigest()
        recomputed = evaluate_portfolio_risk(load_portfolio_equity(source_path))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"{path} portfolio-risk source replay failed: {exc}") from exc
    if actual_digest != expected_digest:
        raise SystemExit(f"{path} portfolio-risk source digest mismatch")
    if any(audit.get(key) != value for key, value in recomputed.items()):
        raise SystemExit(f"{path} portfolio-risk audit does not match source replay")
    return audit, {
        "path": str(path),
        "sha256": sha256(raw).hexdigest(),
        "mark_to_market_equity": {
            "path": str(source_path),
            "sha256": actual_digest,
        },
    }


def _load_portfolio_execution_audit(
    path: Path,
    *,
    manifest: dict,
    manifest_sha256: str,
    policy_sha256: str,
    policy: PortfolioPromotionPolicy,
    equity_path: Path,
) -> tuple[dict, dict[str, str]]:
    raw = path.read_bytes()
    audit = _decode(raw, path)
    protocol = audit.get("protocol_sha256")
    if not isinstance(protocol, dict) or set(protocol) != set(
        PORTFOLIO_EXECUTION_PROTOCOL_FILES
    ):
        raise SystemExit(f"{path} has an invalid portfolio-execution protocol inventory")
    current_protocol = {
        relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
        for relative in PORTFOLIO_EXECUTION_PROTOCOL_FILES
    }
    if protocol != current_protocol:
        raise SystemExit(f"{path} is bound to an obsolete portfolio-execution protocol")
    sources = audit.get("sources")
    if not isinstance(sources, dict) or set(sources) != {
        "observation_manifest",
        "mark_to_market_equity",
    }:
        raise SystemExit(f"{path} portfolio-execution source inventory is incomplete")

    def resolve_source(name: str) -> tuple[Path, bytes]:
        source = sources.get(name)
        if not isinstance(source, dict):
            raise SystemExit(f"{path} portfolio-execution {name} source is invalid")
        raw_path = source.get("path")
        expected_digest = source.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected_digest, str):
            raise SystemExit(f"{path} portfolio-execution {name} path or digest is missing")
        source_path = Path(raw_path)
        if not source_path.is_absolute():
            source_path = PROJECT_ROOT / source_path
        try:
            payload = source_path.read_bytes()
        except OSError as exc:
            raise SystemExit(f"{path} portfolio-execution {name} is unreadable: {exc}") from exc
        if sha256(payload).hexdigest() != expected_digest:
            raise SystemExit(f"{path} portfolio-execution {name} digest mismatch")
        return source_path, payload

    observation_manifest_path, observation_manifest_bytes = resolve_source(
        "observation_manifest"
    )
    execution_equity_path, execution_equity_bytes = resolve_source(
        "mark_to_market_equity"
    )
    if sha256(execution_equity_bytes).hexdigest() != sha256(equity_path.read_bytes()).hexdigest():
        raise SystemExit(
            f"{path} portfolio-execution equity differs from portfolio-risk equity"
        )
    expected_scopes, failures = expected_execution_scopes(manifest)
    if failures:
        raise SystemExit("portfolio manifest execution scopes are invalid: " + "; ".join(failures))
    try:
        equity = load_portfolio_equity(execution_equity_path)
        observation_bundle = load_execution_observation_manifest(
            observation_manifest_path,
            portfolio_manifest_sha256=manifest_sha256,
            portfolio_policy_sha256=policy_sha256,
        )
        reported_manifest_source = sources["observation_manifest"]
        if (
            not isinstance(reported_manifest_source, dict)
            or reported_manifest_source.get("segments")
            != observation_bundle["segments"]
        ):
            raise ValueError("execution observation segment inventory does not match replay")
        recomputed = evaluate_portfolio_execution(
            observation_bundle["observations"],
            expected_scopes,
            minimum_observations_per_scope=policy.minimum_execution_observations_per_scope,
            minimum_passive_fills_per_scope=policy.minimum_passive_fills_per_scope,
            coverage_start=datetime.fromisoformat(equity["first_timestamp"]),
            coverage_end=datetime.fromisoformat(equity["last_timestamp"]),
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"{path} portfolio-execution source replay failed: {exc}") from exc
    recomputed.update(
        {
            "portfolio_manifest_sha256": manifest_sha256,
            "portfolio_promotion_policy_sha256": policy_sha256,
            "protocol_current": True,
            "protocol_sha256": current_protocol,
            "sources": sources,
        }
    )
    if audit != recomputed:
        raise SystemExit(f"{path} portfolio-execution audit does not match source replay")
    return audit, {
        "path": str(observation_manifest_path),
        "sha256": sha256(observation_manifest_bytes).hexdigest(),
        "segments": observation_bundle["segments"],
    }


def _build_portfolio_composition_audit(
    path: Path,
    sleeve_pnl_path: Path,
    *,
    candidate_order: list[str],
    sleeve_initial_weights: dict[str, float],
    equity_path: Path,
) -> tuple[dict, dict[str, str]]:
    try:
        source_bytes = path.read_bytes()
        sleeve_pnl_bytes = sleeve_pnl_path.read_bytes()
        matrix = load_portfolio_contributions(path, candidate_order)
        sleeve_pnl = load_sleeve_daily_pnl(sleeve_pnl_path, candidate_order)
        equity = load_portfolio_equity(equity_path)
        audit = evaluate_portfolio_composition(
            matrix, equity, sleeve_pnl, sleeve_initial_weights
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"{path} portfolio composition replay failed: {exc}") from exc
    audit.update(
        {
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in PORTFOLIO_COMPOSITION_PROTOCOL_FILES
            },
            "source": {
                "path": str(path),
                "sha256": sha256(source_bytes).hexdigest(),
            },
            "sleeve_pnl_source": {
                "path": str(sleeve_pnl_path),
                "sha256": sha256(sleeve_pnl_bytes).hexdigest(),
            },
        }
    )
    return audit, {
        "path": str(path),
        "sha256": sha256(source_bytes).hexdigest(),
    }


def _build_portfolio_performance_audit(
    *,
    trades_path: Path,
    exposure_path: Path,
    accounting_path: Path,
    equity_path: Path,
    candidate_order: list[str],
    risk_audit: dict,
    evidence: dict,
    execution_scopes: list[dict],
    maximum_simultaneous_stress_drawdown: float,
    minimum_daily_mtm_profit_factor: float,
    promotion_policy: PortfolioPromotionPolicy,
) -> tuple[dict, dict[str, dict[str, str]]]:
    try:
        trade_bytes = trades_path.read_bytes()
        exposure_bytes = exposure_path.read_bytes()
        accounting_bytes = accounting_path.read_bytes()
        trades = load_portfolio_trades(trades_path, candidate_order)
        exposure = load_portfolio_exposure(exposure_path)
        accounting = load_portfolio_accounting(accounting_path)
        equity = load_mark_to_market_equity(equity_path)
        audit = evaluate_portfolio_performance(
            trades=trades,
            exposure=exposure,
            accounting=accounting,
            equity=equity,
            portfolio_risk_audit=risk_audit,
            evidence=evidence,
            candidates=candidate_order,
            execution_scopes=execution_scopes,
            maximum_simultaneous_stress_drawdown=maximum_simultaneous_stress_drawdown,
            minimum_daily_mtm_profit_factor=minimum_daily_mtm_profit_factor,
            maximum_gross_leverage=promotion_policy.maximum_gross_leverage,
            maximum_absolute_net_leverage=promotion_policy.maximum_absolute_net_leverage,
            maximum_trade_leverage_adjusted_mae=(
                promotion_policy.maximum_portfolio_trade_leverage_adjusted_mae
            ),
            maximum_rule_violations=promotion_policy.maximum_portfolio_rule_violations,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(f"portfolio performance replay failed: {exc}") from exc
    audit.update(
        {
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in PORTFOLIO_PERFORMANCE_PROTOCOL_FILES
            },
            "sources": {
                "trades": {
                    "path": str(trades_path),
                    "sha256": sha256(trade_bytes).hexdigest(),
                },
                "exposure": {
                    "path": str(exposure_path),
                    "sha256": sha256(exposure_bytes).hexdigest(),
                },
                "accounting": {
                    "path": str(accounting_path),
                    "sha256": sha256(accounting_bytes).hexdigest(),
                },
            },
        }
    )
    return audit, audit["sources"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--policy",
        type=Path,
        default=PROJECT_ROOT / "research" / "portfolio-promotion-policy.json",
    )
    parser.add_argument("--portfolio-report", type=Path, required=True)
    parser.add_argument("--sleeve-report", action="append", type=_parse_sleeve_report, default=[])
    parser.add_argument("--portfolio-selection-bias-audit", type=Path, required=True)
    parser.add_argument("--portfolio-risk-audit", type=Path, required=True)
    parser.add_argument("--portfolio-contributions", type=Path, required=True)
    parser.add_argument("--portfolio-sleeve-pnl", type=Path, required=True)
    parser.add_argument("--portfolio-trades", type=Path, required=True)
    parser.add_argument("--portfolio-exposure", type=Path, required=True)
    parser.add_argument("--portfolio-accounting", type=Path, required=True)
    parser.add_argument(
        "--sleeve-selection-bias-audit",
        action="append",
        type=_parse_sleeve_report,
        default=[],
    )
    parser.add_argument("--execution-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("reports/portfolio-promotion.json"))
    args = parser.parse_args()

    manifest_bytes = args.manifest.read_bytes()
    policy_bytes = args.policy.read_bytes()
    manifest_document = _decode(manifest_bytes, args.manifest)
    policy_document = _decode(policy_bytes, args.policy)
    machine_policy = policy_document.get("machine_policy")
    promotion_policy = PortfolioPromotionPolicy()
    if machine_policy != asdict(promotion_policy):
        raise SystemExit("portfolio policy document does not match the implemented policy")
    sleeve_reports: dict[str, dict] = {}
    sleeve_report_sources: dict[str, dict[str, str]] = {}
    for candidate, path in args.sleeve_report:
        if candidate in sleeve_reports:
            raise SystemExit(f"duplicate sleeve report candidate: {candidate}")
        sleeve_bytes = path.read_bytes()
        sleeve_reports[candidate] = _decode(sleeve_bytes, path)
        sleeve_report_sources[candidate] = {
            "path": str(path),
            "sha256": sha256(sleeve_bytes).hexdigest(),
        }
    portfolio_selection_audit, portfolio_selection_source = _load_selection_bias_audit(
        args.portfolio_selection_bias_audit
    )
    portfolio_risk_audit, portfolio_risk_source = _load_portfolio_risk_audit(
        args.portfolio_risk_audit
    )
    sleeves = manifest_document.get("sleeves")
    if not isinstance(sleeves, list) or not all(isinstance(item, dict) for item in sleeves):
        raise SystemExit("portfolio manifest sleeves are invalid")
    candidate_order = [str(item.get("candidate") or "") for item in sleeves]
    sleeve_initial_weights = {
        str(item.get("candidate") or ""): item.get("allocation_weight")
        for item in sleeves
    }
    performance_execution_scopes, performance_scope_failures = expected_execution_scopes(
        manifest_document
    )
    if performance_scope_failures:
        raise SystemExit(
            "portfolio execution scopes are invalid: "
            + "; ".join(performance_scope_failures)
        )
    equity_source = portfolio_risk_source.get("mark_to_market_equity")
    raw_equity_path = (
        equity_source.get("path") if isinstance(equity_source, dict) else None
    )
    if not isinstance(raw_equity_path, str) or not raw_equity_path:
        raise SystemExit("portfolio risk MTM equity source is missing")
    equity_path = Path(raw_equity_path)
    if not equity_path.is_absolute():
        equity_path = PROJECT_ROOT / equity_path
    portfolio_execution_audit, portfolio_execution_source = (
        _load_portfolio_execution_audit(
            args.execution_audit,
            manifest=manifest_document,
            manifest_sha256=sha256(manifest_bytes).hexdigest(),
            policy_sha256=sha256(policy_bytes).hexdigest(),
            policy=promotion_policy,
            equity_path=equity_path,
        )
    )
    portfolio_composition_audit, portfolio_composition_source = (
        _build_portfolio_composition_audit(
            args.portfolio_contributions,
            args.portfolio_sleeve_pnl,
            candidate_order=candidate_order,
            sleeve_initial_weights=sleeve_initial_weights,
            equity_path=equity_path,
        )
    )
    sleeve_selection_audits: dict[str, dict] = {}
    sleeve_selection_sources: dict[str, dict[str, object]] = {}
    for candidate, path in args.sleeve_selection_bias_audit:
        if candidate in sleeve_selection_audits:
            raise SystemExit(f"duplicate sleeve selection-bias candidate: {candidate}")
        audit, source = _load_selection_bias_audit(path)
        sleeve_selection_audits[candidate] = audit
        sleeve_selection_sources[candidate] = source
    if set(sleeve_selection_audits) != set(sleeve_reports):
        raise SystemExit("sleeve selection-bias audit inventory must exactly match sleeve reports")
    for candidate, report in sleeve_reports.items():
        embedded = report.get("selection_bias_audit")
        if embedded is not None and embedded != sleeve_selection_audits[candidate]:
            raise SystemExit(f"{candidate} embedded selection-bias audit does not match its source")
        report["selection_bias_audit"] = sleeve_selection_audits[candidate]
    portfolio_report_bytes = args.portfolio_report.read_bytes()
    execution_audit_bytes = args.execution_audit.read_bytes()
    portfolio_report = _decode(portfolio_report_bytes, args.portfolio_report)
    portfolio_performance_audit, portfolio_performance_sources = (
        _build_portfolio_performance_audit(
            trades_path=args.portfolio_trades,
            exposure_path=args.portfolio_exposure,
            accounting_path=args.portfolio_accounting,
            equity_path=equity_path,
            candidate_order=candidate_order,
            risk_audit=portfolio_risk_audit,
            evidence=portfolio_report.get("evidence") or {},
            execution_scopes=performance_execution_scopes,
            maximum_simultaneous_stress_drawdown=(
                promotion_policy.maximum_simultaneous_cost_stress_drawdown
            ),
            minimum_daily_mtm_profit_factor=(
                promotion_policy.minimum_portfolio_daily_mtm_profit_factor
            ),
            promotion_policy=promotion_policy,
        )
    )
    embedded_portfolio_selection = portfolio_report.get("selection_bias_audit")
    if embedded_portfolio_selection is not None and embedded_portfolio_selection != portfolio_selection_audit:
        raise SystemExit("embedded portfolio selection-bias audit does not match its source")
    portfolio_report["selection_bias_audit"] = portfolio_selection_audit
    embedded_portfolio_risk = portfolio_report.get("portfolio_risk_audit")
    if embedded_portfolio_risk is not None and embedded_portfolio_risk != portfolio_risk_audit:
        raise SystemExit("embedded portfolio risk audit does not match its source")
    portfolio_report["portfolio_risk_audit"] = portfolio_risk_audit
    embedded_portfolio_composition = portfolio_report.get("portfolio_composition_audit")
    if (
        embedded_portfolio_composition is not None
        and embedded_portfolio_composition != portfolio_composition_audit
    ):
        raise SystemExit("embedded portfolio composition audit does not match source replay")
    portfolio_report["portfolio_composition_audit"] = portfolio_composition_audit
    for field, audit_field in (
        ("common_daily_observations", "observations"),
        ("pairwise_daily_correlations", "pairwise_daily_correlations"),
        ("positive_pnl_share_by_sleeve", "positive_pnl_share_by_sleeve"),
    ):
        embedded_value = portfolio_report.get(field)
        replayed_value = portfolio_composition_audit.get(audit_field)
        if embedded_value is not None and embedded_value != replayed_value:
            raise SystemExit(f"embedded portfolio {field} does not match source replay")
        portfolio_report[field] = replayed_value
    embedded_performance = portfolio_report.get("portfolio_performance_audit")
    if embedded_performance is not None and embedded_performance != portfolio_performance_audit:
        raise SystemExit("embedded portfolio performance audit does not match source replay")
    portfolio_report["portfolio_performance_audit"] = portfolio_performance_audit
    performance_fields = {
        "monthly_target_gate": portfolio_performance_audit.get("monthly_target_gate"),
        "maximum_gross_leverage": portfolio_performance_audit.get(
            "maximum_gross_leverage"
        ),
        "maximum_absolute_net_leverage": portfolio_performance_audit.get(
            "maximum_absolute_net_leverage"
        ),
        "maximum_trade_leverage_adjusted_mae": portfolio_performance_audit.get(
            "maximum_trade_leverage_adjusted_mae"
        ),
        "liquidations": portfolio_performance_audit.get("liquidations"),
        "rule_violations": portfolio_performance_audit.get("rule_violations"),
        "simultaneous_cost_funding_stress_net_return": (
            portfolio_performance_audit.get("stress_returns") or {}
        ).get("simultaneous"),
        "simultaneous_cost_funding_stress_max_drawdown": (
            (portfolio_performance_audit.get("stress_paths") or {}).get("simultaneous")
            or {}
        ).get("maximum_drawdown"),
    }
    for field, replayed_value in performance_fields.items():
        embedded_value = portfolio_report.get(field)
        if embedded_value is not None and embedded_value != replayed_value:
            raise SystemExit(f"embedded portfolio {field} does not match source replay")
        portfolio_report[field] = replayed_value
    result = evaluate_portfolio_promotion(
        manifest_document,
        portfolio_report,
        sleeve_reports,
        portfolio_execution_audit,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        policy_sha256=sha256(policy_bytes).hexdigest(),
    )
    result["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["inputs"] = {
        "manifest": str(args.manifest),
        "manifest_sha256": sha256(manifest_bytes).hexdigest(),
        "policy": str(args.policy),
        "policy_sha256": sha256(policy_bytes).hexdigest(),
        "portfolio_report": str(args.portfolio_report),
        "portfolio_report_sha256": sha256(portfolio_report_bytes).hexdigest(),
        "execution_audit": str(args.execution_audit),
        "execution_audit_sha256": sha256(execution_audit_bytes).hexdigest(),
        "execution_observations": portfolio_execution_source,
        "sleeve_reports": sleeve_report_sources,
        "portfolio_selection_bias_audit": portfolio_selection_source,
        "portfolio_risk_audit": portfolio_risk_source,
        "portfolio_contributions": portfolio_composition_source,
        "portfolio_sleeve_pnl": {
            "path": str(args.portfolio_sleeve_pnl),
            "sha256": sha256(args.portfolio_sleeve_pnl.read_bytes()).hexdigest(),
        },
        "portfolio_performance": portfolio_performance_sources,
        "sleeve_selection_bias_audits": sleeve_selection_sources,
        "evaluator_protocol_sha256": {
            relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
            for relative in PROMOTION_PROTOCOL_FILES
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
