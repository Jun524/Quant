#!/usr/bin/env python3
"""Write a single fail-closed readiness report for capital preservation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.goal_readiness import (
    audit_portfolio_promotion_receipt,
    build_goal_readiness,
)
from quant.research.historical_admission import evaluate_historical_admission
from quant.research.historical_cost_guard import evaluate_historical_cost_policy
from scripts.research_health import build_health_report


def _load_optional(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return payload


def _source(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "present": False, "sha256": None}
    return {
        "path": str(path),
        "present": True,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }


def compare_portfolio_promotion_replay(receipt: dict, recomputed: dict) -> dict:
    excluded = {"generated_at_utc", "inputs"}
    reported_core = {key: value for key, value in receipt.items() if key not in excluded}
    recomputed_core = {
        key: value for key, value in recomputed.items() if key not in excluded
    }
    failures = (
        []
        if reported_core == recomputed_core
        else ["portfolio promotion receipt verdict does not match full source replay"]
    )
    return {"status": "PASS" if not failures else "FAIL", "failures": failures}


def replay_portfolio_promotion_receipt(
    receipt: dict, *, project_root: Path
) -> dict:
    inputs = receipt.get("inputs")
    if not isinstance(inputs, dict):
        return {"status": "FAIL", "failures": ["receipt inputs are missing"]}
    required_scalar_paths = (
        "manifest",
        "policy",
        "portfolio_report",
        "execution_audit",
    )
    if any(not isinstance(inputs.get(name), str) for name in required_scalar_paths):
        return {
            "status": "FAIL",
            "failures": ["receipt replay paths are incomplete"],
        }
    portfolio_selection = inputs.get("portfolio_selection_bias_audit")
    portfolio_risk = inputs.get("portfolio_risk_audit")
    portfolio_contributions = inputs.get("portfolio_contributions")
    portfolio_sleeve_pnl = inputs.get("portfolio_sleeve_pnl")
    portfolio_performance = inputs.get("portfolio_performance")
    execution_observations = inputs.get("execution_observations")
    sleeve_reports = inputs.get("sleeve_reports")
    sleeve_selection = inputs.get("sleeve_selection_bias_audits")
    if (
        not isinstance(portfolio_selection, dict)
        or not isinstance(portfolio_selection.get("path"), str)
        or not isinstance(portfolio_risk, dict)
        or not isinstance(portfolio_risk.get("path"), str)
        or not isinstance(portfolio_contributions, dict)
        or not isinstance(portfolio_contributions.get("path"), str)
        or not isinstance(portfolio_sleeve_pnl, dict)
        or not isinstance(portfolio_sleeve_pnl.get("path"), str)
        or not isinstance(portfolio_performance, dict)
        or set(portfolio_performance) != {"trades", "exposure", "accounting"}
        or any(
            not isinstance(source, dict)
            or not isinstance(source.get("path"), str)
            for source in portfolio_performance.values()
        )
        or not isinstance(execution_observations, dict)
        or not isinstance(execution_observations.get("path"), str)
        or not isinstance(execution_observations.get("segments"), list)
        or not execution_observations.get("segments")
        or not isinstance(sleeve_reports, dict)
        or not sleeve_reports
        or not isinstance(sleeve_selection, dict)
        or set(sleeve_selection) != set(sleeve_reports)
    ):
        return {
            "status": "FAIL",
            "failures": ["receipt replay source inventory is incomplete"],
        }

    def source_path(source: object) -> str | None:
        return source.get("path") if isinstance(source, dict) else None

    with tempfile.TemporaryDirectory(prefix="portfolio-promotion-replay-") as raw:
        output = Path(raw) / "recomputed.json"
        command = [
            sys.executable,
            str(project_root / "scripts" / "portfolio_promotion_report.py"),
            "--manifest",
            inputs["manifest"],
            "--policy",
            inputs["policy"],
            "--portfolio-report",
            inputs["portfolio_report"],
            "--portfolio-selection-bias-audit",
            portfolio_selection["path"],
            "--portfolio-risk-audit",
            portfolio_risk["path"],
            "--portfolio-contributions",
            portfolio_contributions["path"],
            "--portfolio-sleeve-pnl",
            portfolio_sleeve_pnl["path"],
            "--portfolio-trades",
            portfolio_performance["trades"]["path"],
            "--portfolio-exposure",
            portfolio_performance["exposure"]["path"],
            "--portfolio-accounting",
            portfolio_performance["accounting"]["path"],
            "--execution-audit",
            inputs["execution_audit"],
            "--output",
            str(output),
        ]
        for candidate in sorted(sleeve_reports):
            path = source_path(sleeve_reports[candidate])
            audit_path = source_path(sleeve_selection[candidate])
            if not path or not audit_path:
                return {
                    "status": "FAIL",
                    "failures": [f"{candidate} replay source path is missing"],
                }
            command.extend(["--sleeve-report", f"{candidate}={path}"])
            command.extend(
                ["--sleeve-selection-bias-audit", f"{candidate}={audit_path}"]
            )
        run = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if run.returncode not in (0, 2) or not output.exists():
            detail = (run.stderr or run.stdout).strip().splitlines()
            return {
                "status": "FAIL",
                "failures": [
                    "portfolio promotion source replay could not complete"
                    + (f": {detail[-1]}" if detail else "")
                ],
            }
        try:
            recomputed = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {
                "status": "FAIL",
                "failures": [f"portfolio promotion replay output is invalid: {exc}"],
            }
    if not isinstance(recomputed, dict):
        return {
            "status": "FAIL",
            "failures": ["portfolio promotion replay output is not an object"],
        }
    return compare_portfolio_promotion_replay(receipt, recomputed)


def _markdown(report: dict) -> str:
    lines = [
        "# Capital-preservation research readiness",
        "",
        f"- Status: `{report['status']}`",
        f"- Objective proven: `{str(report['objective_proven']).lower()}`",
        f"- Live order authorization: `{report['live_order_authorization']}`",
        f"- Research health: `{report['research_health']}`",
        "- Return objective: "
        f"`{report['return_objective']['promotion_requirement']}` "
        f"(monthly return: `{report['return_objective']['monthly_return_target']}`)",
        "- Research cadence: historical diagnostics immediately; forward reviews at "
        + ", ".join(str(day) for day in report["research_cadence"]["forward_review_days"])
        + " days.",
        "",
        "## Proof pipeline",
        "",
        "| Gate | Status | Actual | Required |",
        "|---|---:|---:|---:|",
    ]
    for item in report["phases"]:
        lines.append(
            f"| {item['name']} | {item['status']} | {item.get('actual', '-')} | {item.get('required', '-')} |"
        )
    lines.extend(["", "## Active forward candidates", ""])
    if report["active_forward_candidates"]:
        for item in report["active_forward_candidates"]:
            lines.append(
                f"- Experiment {item['experiment_id']}: `{item['strategy']}` — `{item['status']}`"
            )
    else:
        lines.append("- None")
    lines.extend(["", "## Ordered blockers", ""])
    for item in report["ordered_blockers"]:
        lines.append(f"- {item}")
    lines.extend(["", report["interpretation"], ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoreboard", type=Path, default=Path("reports/platform-scoreboard.json"))
    parser.add_argument("--portfolio-promotion", type=Path, default=Path("reports/portfolio-promotion.json"))
    parser.add_argument("--alpha001-forward", type=Path, default=Path("reports/alpha001-forward.json"))
    parser.add_argument("--alpha001-bias-audit", type=Path, default=Path("reports/alpha001-bias-audit.json"))
    parser.add_argument("--aoa-forward", type=Path, default=Path("reports/aoa-forward-locked.json"))
    parser.add_argument(
        "--historical-evidence-policy",
        type=Path,
        default=Path("research/historical-research-policy.json"),
    )
    parser.add_argument(
        "--historical-cost-policy",
        type=Path,
        default=Path("research/historical-cost-accounting-policy.json"),
    )
    parser.add_argument("--historical-execution-coverage", type=Path, default=Path("reports/historical-execution-coverage.json"))
    parser.add_argument("--historical-funding-coverage", type=Path, default=Path("reports/historical-funding-coverage.json"))
    parser.add_argument("--historical-funding-ledger-audit", type=Path, default=Path("reports/historical-funding-ledger-audit.json"))
    parser.add_argument(
        "--research-objective",
        type=Path,
        default=Path("research/capital-preservation-objective.json"),
    )
    parser.add_argument("--output", type=Path, default=Path("reports/goal-readiness.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("reports/goal-readiness.md"))
    args = parser.parse_args()

    sources = [
        args.scoreboard,
        args.portfolio_promotion,
        args.alpha001_forward,
        args.alpha001_bias_audit,
        args.aoa_forward,
        args.historical_evidence_policy,
        args.historical_cost_policy,
        args.historical_execution_coverage,
        args.historical_funding_coverage,
        args.historical_funding_ledger_audit,
        args.research_objective,
        Path("research/promotion-policy.json"),
        Path("research/portfolio-promotion-policy.json"),
    ]
    portfolio_promotion = _load_optional(args.portfolio_promotion)
    receipt_integrity = (
        audit_portfolio_promotion_receipt(
            portfolio_promotion,
            project_root=PROJECT_ROOT,
            current_policy=PROJECT_ROOT / "research" / "portfolio-promotion-policy.json",
        )
        if portfolio_promotion is not None
        else {"status": "NOT_OPENED", "failures": []}
    )
    if portfolio_promotion is not None and receipt_integrity.get("status") == "PASS":
        receipt_integrity = replay_portfolio_promotion_receipt(
            portfolio_promotion, project_root=PROJECT_ROOT
        )
    report = build_goal_readiness(
        _load_optional(args.scoreboard) or {},
        build_health_report(),
        portfolio_promotion=portfolio_promotion,
        alpha001_forward=_load_optional(args.alpha001_forward),
        alpha001_bias_audit=_load_optional(args.alpha001_bias_audit),
        aoa_forward=_load_optional(args.aoa_forward),
        historical_evidence_policy=(
            evaluate_historical_admission(_load_optional(args.historical_evidence_policy) or {})
            if args.historical_evidence_policy.exists()
            else None
        ),
        historical_cost_policy=(
            evaluate_historical_cost_policy(_load_optional(args.historical_cost_policy) or {})
            if args.historical_cost_policy.exists()
            else None
        ),
        historical_execution_coverage=_load_optional(args.historical_execution_coverage),
        historical_funding_coverage=_load_optional(args.historical_funding_coverage),
        historical_funding_ledger_audit=_load_optional(args.historical_funding_ledger_audit),
        portfolio_receipt_integrity=receipt_integrity,
        research_objective=_load_optional(args.research_objective),
    )
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["sources"] = [_source(path) for path in sources]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_output.write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "objective_proven": report["objective_proven"],
        "live_order_authorization": report["live_order_authorization"],
        "output": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
