"""Failure attribution, cost budgets, and platform-level research summaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from pathlib import Path
import html
import json


@dataclass(frozen=True)
class EdgeBudget:
    """Pre-backtest cost budget in basis points per completed round trip."""

    expected_gross_edge_bps: float
    expected_fee_bps: float
    expected_spread_bps: float
    expected_slippage_bps: float
    expected_funding_bps: float

    @property
    def expected_cost_bps(self) -> float:
        return self.expected_fee_bps + self.expected_spread_bps + self.expected_slippage_bps + self.expected_funding_bps

    @property
    def edge_buffer_bps(self) -> float:
        return self.expected_gross_edge_bps - self.expected_cost_bps

    @property
    def viable(self) -> bool:
        return self.edge_buffer_bps > 0

    def to_dict(self) -> dict:
        result = asdict(self)
        result.update(
            {
                "expected_cost_bps": self.expected_cost_bps,
                "edge_buffer_bps": self.edge_buffer_bps,
                "viable": self.viable,
            }
        )
        return result


def _number(mapping: dict, key: str, default: float = 0.0) -> float:
    value = mapping.get(key, default)
    return float(value) if value is not None else default


def _pnl_components(result: dict) -> dict[str, float]:
    """Normalize old reports that predate the explicit PnL-component schema."""
    organic = result.get("organic") or {}
    components = result.get("pnl_components") or {}
    net = _number(organic, "net_profit", _number(components, "net_pnl"))
    fees = _number(
        result,
        "estimated_fee_cost",
        max(0.0, -_number(components, "trading_fees")),
    )
    funding = _number(result, "funding_net", _number(components, "funding_pnl"))
    slippage = -_number(components, "modeled_slippage")
    price = _number(components, "price_pnl_estimate", net + fees - funding + slippage)
    return {
        "price_pnl": price,
        "trading_fees": -fees,
        "funding_pnl": funding,
        "slippage_pnl": -slippage,
        "net_pnl": net,
    }


def _validation_result(record: dict) -> dict:
    return ((record.get("result") or {}).get("validation") or {})


def _development_result(record: dict) -> dict:
    return ((record.get("result") or {}).get("development") or {})


def classify_experiment(record: dict) -> dict:
    """Classify a result without changing its original PASS/REVIEW/FAIL verdict.

    Attribution is deliberately mechanical and uses the locked validation
    result, except that a development funding-dominated loss is retained when
    it directly explains a hypothesis' structural failure (Alpha #3).
    """
    result = record.get("result") or {}
    non_performance_veto = bool(result.get("non_performance_veto"))
    gate = result.get("gate") or {}
    validation = _validation_result(record)
    development = _development_result(record)
    has_validation = bool(validation)
    evaluation = validation if has_validation else development
    validation_organic = validation.get("organic") or {}
    evaluation_organic = evaluation.get("organic") or {}
    development_organic = development.get("organic") or {}
    components = _pnl_components(evaluation)
    slippage_stress = result.get("slippage_stress") or {}
    if slippage_stress:
        # The stress export replays the identical OOS trades with only modeled
        # slippage added.  Carry that drag into the standard attribution row.
        stress_components = _pnl_components(slippage_stress)
        modeled_slippage = max(0.0, -stress_components["slippage_pnl"])
        components["slippage_pnl"] = -modeled_slippage
        components["net_pnl"] = components["price_pnl"] + components["trading_fees"] + components["funding_pnl"] - modeled_slippage
    dev_components = _pnl_components(development)
    specialized_metrics = (
        evaluation.get("breakout_metrics")
        or evaluation.get("short_metrics")
        or evaluation.get("carry_metrics")
        or {}
    )
    trades = int(evaluation_organic.get("trades") or 0)
    policy = gate.get("policy") or {}
    minimum = int(policy.get("min_oos_trades") or 30)
    status = gate.get("status") or "UNKNOWN"
    price_positive_net_negative = components["price_pnl"] > 0 and components["net_pnl"] <= 0
    execution_drag = -components["trading_fees"] - components["funding_pnl"] - components["slippage_pnl"]
    reasons: list[str] = [str(reason) for reason in result.get("failure_reasons") or []]
    if status == "REVIEW" and trades < minimum:
        reasons.append("INSUFFICIENT_SAMPLE")
    if has_validation and development_organic.get("net_return", 0.0) > 0 and validation_organic.get("net_return", 0.0) <= 0:
        reasons.append("OOS_DECAY")
    if not non_performance_veto and status != "ACCUMULATING" and components["price_pnl"] <= 0:
        reasons.append("NO_SIGNAL_EDGE")
    # Development is considered here because the sign of funding can change
    # with market conditions; a price-positive development loss dominated by
    # funding is still valuable evidence about a holding-style short Alpha.
    dev_funding_drag = max(0.0, -dev_components["funding_pnl"])
    if dev_components["price_pnl"] > 0 and dev_components["net_pnl"] <= 0 and dev_funding_drag > -dev_components["trading_fees"]:
        reasons.append("FUNDING_DOMINATED")
    if price_positive_net_negative and execution_drag > 0:
        reasons.append("COST_DOMINATED")
    concentration_check = next(
        (check for check in gate.get("checks") or [] if check.get("name") == "single_trade_profit_concentration"),
        None,
    )
    if concentration_check and not concentration_check.get("passed", False):
        reasons.append("CONCENTRATED_PNL")
    failed_checks = {
        str(check.get("name") or "")
        for check in gate.get("checks") or []
        if not check.get("passed", False)
    }
    if any("carry" in name or "funding_exceeds" in name for name in failed_checks):
        reasons.append("CARRY_NOT_SELF_FINANCING")
    if any("profit_factor" in name for name in failed_checks) and components["net_pnl"] > 0:
        reasons.append("WEAK_EDGE")
    if status == "ACCUMULATING":
        reasons.append("FORWARD_SAMPLE_ACCUMULATING")
    if not non_performance_veto and not reasons and status == "FAIL":
        reasons.append("NO_SIGNAL_EDGE")

    reasons = list(dict.fromkeys(reasons))
    priority = (
        "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION",
        "RETURN_TARGET_MISALIGNED",
        "COST_STRESS_FAILURE",
        "CARRY_NOT_SELF_FINANCING",
        "FUNDING_DOMINATED",
        "COST_DOMINATED",
        "CONCENTRATED_PNL",
        "INSUFFICIENT_SAMPLE",
        "WEAK_EDGE",
        "NO_SIGNAL_EDGE",
        "OOS_DECAY",
        "FORWARD_SAMPLE_ACCUMULATING",
    )
    explicit_primary = result.get("primary_failure")
    primary = (
        str(explicit_primary)
        if explicit_primary in reasons
        else next((reason for reason in priority if reason in reasons), None)
    )
    gross_per_trade = components["price_pnl"] / trades if trades else None
    cost_per_trade = execution_drag / trades if trades else None
    return {
        "experiment_id": record.get("experiment_id"),
        "strategy": record.get("strategy"),
        "status": status,
        "primary_failure": primary,
        "failure_reasons": reasons,
        "trade_count": trades,
        "evaluation_stage": "validation" if has_validation else "development",
        "turnover": float(evaluation["turnover"]) if evaluation.get("turnover") is not None else None,
        "median_holding_minutes": ((evaluation.get("breakout_metrics") or {}).get("median_holding_minutes") or (evaluation.get("short_metrics") or {}).get("median_holding_minutes")),
        "mfe_mae_ratio": specialized_metrics.get("mfe_mae_ratio"),
        "mfe_mean": specialized_metrics.get("mfe_mean"),
        "mae_mean": specialized_metrics.get("mae_mean"),
        "winner_concentration": specialized_metrics.get("largest_winner_profit_concentration"),
        "pnl_by_regime": specialized_metrics.get("pnl_by_regime"),
        "profit_factor": evaluation_organic.get("profit_factor"),
        "net_return": _number(evaluation_organic, "net_return"),
        "gross_price_edge": components["price_pnl"],
        "trading_fee_drag": -components["trading_fees"],
        "funding_drag": -components["funding_pnl"],
        "slippage_drag": -components["slippage_pnl"],
        "net_edge": components["net_pnl"],
        "gross_edge_per_trade": gross_per_trade,
        "cost_per_trade": cost_per_trade,
        "edge_buffer_per_trade": gross_per_trade - cost_per_trade if gross_per_trade is not None and cost_per_trade is not None else None,
    }


def build_scoreboard(registry: dict) -> dict:
    """Build a deterministic, read-only summary from the append-only registry."""
    effective = {int(record["experiment_id"]): deepcopy(record) for record in registry.get("experiments", [])}
    for event in sorted(registry.get("events") or [], key=lambda item: int(item["event_id"])):
        record = effective.get(int(event.get("experiment_id") or 0))
        if record is None:
            raise ValueError(f"registry event references unknown experiment {event.get('experiment_id')}")
        if event.get("event_type") == "result_update":
            record["result"] = deepcopy((event.get("payload") or {}).get("result") or {})
    rows = [classify_experiment(effective[key]) for key in sorted(effective)]
    known_statuses = ("PASS", "REVIEW", "FAIL", "ACCUMULATING")
    statuses = {status: sum(row["status"] == status for row in rows) for status in known_statuses}
    for status in sorted({str(row["status"]) for row in rows} - set(known_statuses)):
        statuses[status] = sum(row["status"] == status for row in rows)
    primary_failures: dict[str, int] = {}
    failure_mentions: dict[str, int] = {}
    for row in rows:
        if row["primary_failure"]:
            primary_failures[row["primary_failure"]] = primary_failures.get(row["primary_failure"], 0) + 1
        for reason in row["failure_reasons"]:
            failure_mentions[reason] = failure_mentions.get(reason, 0) + 1

    def best(key, default=None, *, candidates=None):
        candidates = [row for row in (rows if candidates is None else candidates) if row[key] is not None]
        return max(candidates, key=lambda row: row[key]) if candidates else default

    eligible = [row for row in rows if row["status"] in {"PASS", "REVIEW"}]
    most_robust = max(eligible, key=lambda row: (row["net_return"], row["profit_factor"] or 0.0), default=None)
    oos_rows = [row for row in rows if row["evaluation_stage"] == "validation"]
    development_rows = [row for row in rows if row["evaluation_stage"] == "development"]
    turnover_rows = [row for row in rows if row["turnover"] is not None]
    return {
        "schema_version": 1,
        "experiments": len(rows),
        "events": len(registry.get("events") or []),
        "status_counts": statuses,
        "failure_reasons": dict(sorted(failure_mentions.items())),
        "primary_failure_reasons": dict(sorted(primary_failures.items())),
        "best_oos": best("net_return", candidates=oos_rows),
        "best_development_diagnostic": best("net_return", candidates=development_rows),
        "most_robust": most_robust,
        "highest_sample": best("trade_count"),
        "lowest_turnover": min(turnover_rows, key=lambda row: row["turnover"], default=None),
        "experiments_detail": rows,
    }


def write_scoreboard(scoreboard: dict, *, json_path: Path, html_path: Path | None = None) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(scoreboard, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if html_path is None:
        return
    rows = scoreboard["experiments_detail"]
    headers = ("experiment_id", "strategy", "status", "primary_failure", "trade_count", "net_return", "profit_factor", "edge_buffer_per_trade", "mfe_mae_ratio", "winner_concentration")
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(header, '')))}</td>" for header in headers) + "</tr>"
        for row in rows
    )
    page = (
        "<!doctype html><meta charset='utf-8'><title>Quant Platform Scoreboard</title>"
        "<style>body{font-family:system-ui;margin:2rem}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.4rem;text-align:left}</style>"
        f"<h1>Platform scoreboard</h1><p>Experiments: {scoreboard['experiments']} | Status: {html.escape(str(scoreboard['status_counts']))}</p>"
        f"<p>Failure reasons (all mentions): {html.escape(str(scoreboard['failure_reasons']))}</p>"
        f"<p>Primary failure reasons: {html.escape(str(scoreboard['primary_failure_reasons']))}</p>"
        f"<p>Auxiliary gates: {html.escape(str(scoreboard.get('auxiliary_gates', {})))}</p>"
        "<table><thead><tr>" + "".join(f"<th>{header}</th>" for header in headers) + f"</tr></thead><tbody>{body}</tbody></table>"
    )
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(page + "\n", encoding="utf-8")
