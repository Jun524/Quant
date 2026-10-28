"""Causal evaluation helpers for Alpha #1's append-only forward dry-run."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from math import prod
from pathlib import Path
import sqlite3

from quant.monitoring.equity import select_causal_daily_boundaries
from quant.research.monthly_target import MonthlyTargetPolicy, evaluate_monthly_target
from quant.research.risk import block_bootstrap_risk


EXPECTED_SOURCE = "freqtrade_rest_net_liquidation_equity"
EXPECTED_NOTE = "Simulated balances"
CURRENT_MANIFEST_SOURCE = "authenticated_loopback_freqtrade_net_liquidation"
KNOWN_MANIFEST_SOURCES = {
    CURRENT_MANIFEST_SOURCE,
    "authenticated_loopback_freqtrade_balance_total_bot",
}
NON_NATURAL_EXITS = {"force_exit", "force_sell", "emergency_exit"}
SLIPPAGE_BPS_PER_SIDE = 5.0
PROMOTION_POLICY_PATH = Path(__file__).resolve().parents[2] / "research" / "promotion-policy.json"


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def audit_mtm_segments(paths: list[Path], *, project_root: Path, current_protocol: dict[str, str]) -> dict:
    eligible: list[Path] = []
    segments = []
    for path in paths:
        manifest_path = path.with_suffix(".manifest.json")
        audit = {"path": str(path.relative_to(project_root)), "manifest": str(manifest_path.relative_to(project_root))}
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or manifest.get("evidence_kind") != "freqtrade_dryrun_mark_to_market":
                raise ValueError("invalid MTM manifest schema or evidence kind")
            if manifest.get("equity_path") != audit["path"]:
                raise ValueError("MTM manifest path mismatch")
            if manifest.get("source") not in KNOWN_MANIFEST_SOURCES:
                raise ValueError("unexpected MTM manifest source")
            status = manifest.get("status")
            if status != "ACTIVE":
                seal = manifest.get("seal") or {}
                content = path.read_text(encoding="utf-8")
                observations = len([line for line in content.splitlines() if line.strip()]) if content.endswith("\n") else max(
                    0, len([line for line in content.splitlines() if line.strip()]) - 1
                )
                if (
                    int(seal.get("bytes") or -1) != path.stat().st_size
                    or seal.get("sha256") != _digest(path)
                    or int(seal.get("observations") or -1) != observations
                ):
                    raise ValueError("sealed MTM segment integrity failure")
            protocol_current = manifest.get("protocol_sha256") == current_protocol
            if protocol_current and manifest.get("source") != CURRENT_MANIFEST_SOURCE:
                raise ValueError("current protocol uses a legacy MTM valuation source")
            audit.update({"status": status, "protocol_current": protocol_current, "integrity": "PASS"})
            if protocol_current:
                eligible.append(path)
            else:
                audit["exclusion_reason"] = "LEGACY_PROTOCOL"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            audit.update({"integrity": "FAIL", "protocol_current": False, "exclusion_reason": str(exc)})
        segments.append(audit)
    return {
        "segments": segments,
        "eligible_paths": eligible,
        "eligible_segments": len(eligible),
        "excluded_segments": len(paths) - len(eligible),
        "status": "PASS" if eligible and all(item["integrity"] == "PASS" for item in segments) else "FAIL_CLOSED",
    }


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("MTM timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def load_mtm_observations(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if content and not content.endswith("\n"):
            lines = lines[:-1]
        for line in lines:
            if line.strip():
                row = json.loads(line)
                if row.get("equity_type") != "mark_to_market":
                    raise ValueError(f"non-MTM equity row in {path}")
                if row.get("source") != EXPECTED_SOURCE or row.get("dry_run_note") != EXPECTED_NOTE:
                    raise ValueError(f"untrusted dry-run equity source in {path}")
                if float(row.get("equity") or 0) <= 0 or float(row.get("starting_capital") or 0) <= 0:
                    raise ValueError(f"non-positive equity in {path}")
                rows.append(row)
    rows.sort(key=lambda item: _utc(str(item["observed_at"])))
    timestamps = [_utc(str(item["observed_at"])) for item in rows]
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise ValueError("MTM timestamps must be globally unique and increasing")
    starting_values = {round(float(item["starting_capital"]), 12) for item in rows}
    if len(starting_values) > 1:
        raise ValueError("starting capital changed within the forward evidence stream")
    return rows


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def summarize_mtm(rows: list[dict], *, maximum_gap_seconds: int = 180) -> dict:
    if not rows:
        return {
            "observations": 0,
            "continuous": False,
            "starting_capital": None,
            "latest_equity": None,
            "total_return": None,
            "maximum_drawdown": None,
            "maximum_gap_seconds": None,
            "daily_boundaries": [],
            "daily_returns": [],
            "monthly": {
                "complete_months": 0,
                "series": [],
                "geometric_monthly_return": None,
                "positive_month_ratio": None,
                "worst_month": None,
            },
        }
    times = [_utc(str(item["observed_at"])) for item in rows]
    equities = [float(item["equity"]) for item in rows]
    gaps = [(current - previous).total_seconds() for previous, current in zip(times, times[1:])]
    largest_gap = max(gaps, default=0.0)
    continuous = largest_gap <= maximum_gap_seconds
    peak = equities[0]
    maximum_drawdown = 0.0
    for equity in equities:
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak if peak else 1.0)
    boundary_error = None
    try:
        boundaries = select_causal_daily_boundaries(rows, maximum_age_seconds=120)
    except ValueError as exc:
        boundaries = []
        boundary_error = str(exc)
    daily_returns = [
        current["equity"] / previous["equity"] - 1.0
        for previous, current in zip(boundaries, boundaries[1:])
    ]
    by_boundary = {_utc(str(item["timestamp"])): float(item["equity"]) for item in boundaries}
    monthly_series = []
    for start, starting_equity in sorted(by_boundary.items()):
        if start.day != 1:
            continue
        end = _next_month(start)
        if end not in by_boundary:
            continue
        monthly_series.append(
            {"month": start.strftime("%Y-%m"), "return": by_boundary[end] / starting_equity - 1.0}
        )
    monthly_returns = [item["return"] for item in monthly_series]
    geometric = prod(1.0 + value for value in monthly_returns) ** (1.0 / len(monthly_returns)) - 1.0 if monthly_returns else None
    return {
        "observations": len(rows),
        "first_observed_at": rows[0]["observed_at"],
        "last_observed_at": rows[-1]["observed_at"],
        "continuous": continuous,
        "allowed_gap_seconds": maximum_gap_seconds,
        "maximum_gap_seconds": largest_gap,
        "starting_capital": float(rows[0]["starting_capital"]),
        "latest_equity": equities[-1],
        "total_return": equities[-1] / float(rows[0]["starting_capital"]) - 1.0,
        "maximum_drawdown": maximum_drawdown,
        "daily_boundaries": boundaries,
        "daily_boundary_error": boundary_error,
        "daily_returns": daily_returns,
        "monthly": {
            "complete_months": len(monthly_series),
            "series": monthly_series,
            "geometric_monthly_return": geometric,
            "positive_month_ratio": sum(value > 0 for value in monthly_returns) / len(monthly_returns)
            if monthly_returns
            else None,
            "worst_month": min(monthly_returns) if monthly_returns else None,
        },
    }


def load_forward_trades(database: Path) -> dict:
    if not database.is_file():
        raise ValueError(f"missing forward database: {database}")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM trades ORDER BY id")]
    finally:
        connection.close()
    closed = [row for row in rows if not bool(row["is_open"])]
    natural = [row for row in closed if str(row.get("exit_reason") or "") not in NON_NATURAL_EXITS]
    profits = [float(row.get("close_profit_abs") or 0.0) for row in natural]
    gross_wins = sum(value for value in profits if value > 0)
    gross_losses = -sum(value for value in profits if value < 0)
    fees = sum(float(row.get("fee_open_cost") or 0.0) + float(row.get("fee_close_cost") or 0.0) for row in natural)
    funding_values = [float(row.get("funding_fees") or 0.0) for row in natural]
    funding = sum(funding_values)
    favorable_funding = sum(max(value, 0.0) for value in funding_values)
    adverse_funding_paid = -sum(min(value, 0.0) for value in funding_values)
    funding_evidence = sum("funding_fees" in row for row in natural)
    net = sum(profits)
    excursions = []
    modeled_slippage = 0.0
    slippage_evidence = 0
    for row in natural:
        open_rate = float(row.get("open_rate") or 0.0)
        close_rate = float(row.get("close_rate") or 0.0)
        amount = float(row.get("amount") or 0.0)
        contract_size = float(row.get("contract_size") or 1.0)
        leverage = float(row.get("leverage") or 1.0)
        adverse_rate = row.get("max_rate") if bool(row.get("is_short")) else row.get("min_rate")
        if open_rate <= 0 or adverse_rate is None:
            pass
        else:
            raw = (float(adverse_rate) - open_rate) / open_rate if bool(row.get("is_short")) else (open_rate - float(adverse_rate)) / open_rate
            excursions.append(max(0.0, raw * leverage))
        if open_rate > 0 and close_rate > 0 and amount > 0 and contract_size > 0:
            modeled_slippage += (
                amount * contract_size * (open_rate + close_rate) * SLIPPAGE_BPS_PER_SIDE / 10_000
            )
            slippage_evidence += 1
    liquidation_exits = sum("liquid" in str(row.get("exit_reason") or "").lower() for row in closed)
    maximum_leverage = max((float(row.get("leverage") or 1.0) for row in rows), default=None)
    return {
        "all_trades": len(rows),
        "open_trades": len(rows) - len(closed),
        "closed_trades": len(closed),
        "natural_closed_trades": len(natural),
        "forced_closed_trades": len(closed) - len(natural),
        "wins": sum(value > 0 for value in profits),
        "losses": sum(value < 0 for value in profits),
        "net_profit": net,
        "profit_factor": gross_wins / gross_losses if gross_losses else None,
        "trading_fees": fees,
        "funding_pnl": funding,
        "favorable_funding_removed": favorable_funding,
        "adverse_funding_paid": adverse_funding_paid,
        "funding_stress_net_profit": net - favorable_funding - adverse_funding_paid,
        "price_pnl_estimate": net - funding + fees,
        "double_fee_net_profit": net - fees,
        "slippage_bps_per_side": SLIPPAGE_BPS_PER_SIDE,
        "modeled_slippage": modeled_slippage,
        "slippage_stress_net_profit": net - modeled_slippage,
        "trade_risk": {
            "excursion_coverage_ratio": len(excursions) / len(natural) if natural else 0.0,
            "slippage_coverage_ratio": slippage_evidence / len(natural) if natural else 0.0,
            "funding_coverage_ratio": funding_evidence / len(natural) if natural else 0.0,
            "maximum_leverage": maximum_leverage,
            "maximum_leverage_adjusted_mae": max(excursions, default=None),
            "liquidation_exits": liquidation_exits,
        },
    }


def build_forward_report(
    *, observations: list[dict], trades: dict, protocol_current: bool, data_integrity: dict | None = None
) -> dict:
    mtm = summarize_mtm(observations)
    promotion_policy = json.loads(PROMOTION_POLICY_PATH.read_text(encoding="utf-8"))
    starting_capital = float(mtm.get("starting_capital") or 0.0)
    natural_trades = int(trades["natural_closed_trades"])
    organic = {
        "trades": natural_trades,
        "net_profit": trades["net_profit"],
        "net_return": trades["net_profit"] / starting_capital if starting_capital else 0.0,
        "profit_factor": trades["profit_factor"],
    }
    base = {
        "monthly": mtm["monthly"],
        "organic": organic,
        "max_drawdown": mtm["maximum_drawdown"],
        "equity_basis": "mark_to_market" if mtm["continuous"] and protocol_current else "invalid_mark_to_market",
        "trade_risk": trades["trade_risk"],
    }
    stress = {
        "organic": {
            "net_return": trades["double_fee_net_profit"] / starting_capital if starting_capital else 0.0
        }
    }
    slippage_stress = {
        "organic": {
            "net_return": trades["slippage_stress_net_profit"] / starting_capital if starting_capital else 0.0
        }
    }
    funding_stress = {
        "organic": {
            "net_return": trades["funding_stress_net_profit"] / starting_capital if starting_capital else 0.0
        }
    }
    risk = block_bootstrap_risk(mtm["daily_returns"])
    gate = evaluate_monthly_target(
        base,
        double_cost=stress,
        slippage_stress=slippage_stress,
        funding_stress=funding_stress,
        risk=risk,
        evidence={
            "sample_role": "locked_forward",
            "pre_registered": True,
            "parameter_changes_after_start": 0,
            "policy": promotion_policy.get("policy"),
            "registered_at_utc": promotion_policy.get("registered_at_utc"),
            "sample_started_at_utc": mtm.get("first_observed_at"),
        },
        policy=MonthlyTargetPolicy(
            require_slippage_stress_positive=True,
            require_funding_stress_positive=True,
        ),
    )
    sample_ready = mtm["monthly"]["complete_months"] >= 12 and natural_trades >= 100
    return {
        "schema_version": 1,
        "candidate": "FuturesBtcRegimeRiskBudgetLong",
        "status": gate["status"] if sample_ready else "ACCUMULATING",
        "policy_scope": {
            "cohort": "FROZEN_LEGACY_FORWARD_COHORT",
            "current_capital_preservation_policy_eligibility": False,
            "reason": (
                "This sealed cohort retains its launch protocol. It cannot be reinterpreted as a pass under "
                "the current Risk Policy and cannot authorize live trading."
            ),
        },
        "basis": "credential-free Binance futures dry-run with append-only Freqtrade total_bot MTM observations",
        "protocol_current": protocol_current,
        "data_integrity": data_integrity,
        "mtm": mtm,
        "trades": trades,
        "risk_of_ruin": risk,
        "gate": gate,
        "warning": "Dry-run execution is forward evidence, not proof of live fills or future returns.",
    }
