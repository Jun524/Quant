#!/usr/bin/env python3
"""Build terminal, JSON, and static HTML research scoreboards."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.evidence import build_scoreboard, write_scoreboard


def frozen_alpha015_calibration_reference(project_root: Path) -> dict:
    """Report whether Alpha 015's frozen calibration file remains exact.

    A later descriptive calibration may pass while Alpha 015's immutable input
    remains a separate versioned receipt.  Read the path from the frozen
    contract rather than assuming the mutable generic report filename.
    """
    audit = project_root / "research" / "alpha015-methodology-audit.json"
    relative = "reports/execution-calibration.json"
    try:
        frozen = json.loads(audit.read_text(encoding="utf-8"))
        transfer = frozen["frozen_transfer_choices"]
        # Preserve an explicit legacy fallback only for older sealed audits.
        # Current contracts must name a versioned receipt.
        relative = str(
            transfer.get("execution_calibration_path")
            or "reports/execution-calibration.json"
        )
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ValueError("frozen calibration path must be relative")
        calibration = (project_root / candidate).resolve()
        calibration.relative_to(project_root.resolve())
        expected = transfer["execution_calibration_sha256"]
        actual = sha256(calibration.read_bytes()).hexdigest()
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("frozen calibration digest is invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "status": "MISSING_OR_INVALID",
            "path": relative,
            "promotion_eligible": False,
        }
    return {
        "status": "PASS" if actual == expected else "FROZEN_HASH_MISMATCH",
        "path": relative,
        "expected_sha256": expected,
        "actual_sha256": actual,
        "promotion_eligible": False,
        "warning": "A mismatch blocks Alpha 015 activation; do not rewrite a frozen digest to match a later calibration.",
    }


def auxiliary_gate_snapshot(project_root: Path = PROJECT_ROOT) -> dict:
    specifications = {
        "execution_calibration": ("reports/execution-calibration.json", "status"),
        "execution_calibration_stability": ("reports/execution-calibration-stability.json", "status"),
        "alpha007_ten_bps_cost_stress": ("reports/alpha007-external-cost-stress.json", "status"),
        "alpha007_partial_fill_veto": ("reports/alpha007-partial-fill-stress.json", "status"),
        "alpha007_exchange_filter_feasibility": ("reports/alpha007-exchange-filter-audit.json", "status"),
        "alpha007_order_path_conformance": ("reports/alpha007-order-path-status.json", "status"),
        "liquidation_risk_overlay": ("reports/liquidation-overlay-development.json", "status"),
        "point_in_time_universe": ("reports/universe-accumulation.json", "status"),
        "alpha015_candidate_readiness": ("reports/alpha015-readiness.json", "status"),
        "alpha015_prior_day_activation_lock": ("reports/alpha015-activation-lock-status.json", "status"),
        "portfolio_activation_readiness": ("reports/portfolio-activation-readiness.json", "status"),
    }
    result = {}
    for name, (relative, status_key) in specifications.items():
        path = project_root / relative
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            status = str(payload[status_key])
        except (OSError, KeyError, json.JSONDecodeError):
            result[name] = {"status": "MISSING_OR_INVALID", "path": relative}
            continue
        result[name] = {
            "status": status,
            "path": relative,
            "promotion_eligible": bool(payload.get("promotion_eligible", False)),
        }
    result["alpha015_frozen_calibration_reference"] = (
        frozen_alpha015_calibration_reference(project_root)
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=Path("research/registry.json"))
    parser.add_argument("--output", type=Path, default=Path("reports/platform-scoreboard.json"))
    parser.add_argument("--html-output", type=Path, default=Path("reports/platform-scoreboard.html"))
    args = parser.parse_args()
    scoreboard = build_scoreboard(json.loads(args.registry.read_text(encoding="utf-8")))
    scoreboard["auxiliary_gates"] = auxiliary_gate_snapshot()
    write_scoreboard(scoreboard, json_path=args.output, html_path=args.html_output)
    print(f"Experiments: {scoreboard['experiments']}")
    for status, count in scoreboard["status_counts"].items():
        print(f"{status}: {count}")
    print("Failure reasons (all mentions):")
    for reason, count in scoreboard["failure_reasons"].items():
        print(f"  {reason}: {count}")
    print("Primary failure reasons:")
    for reason, count in scoreboard["primary_failure_reasons"].items():
        print(f"  {reason}: {count}")
    for label in ("best_oos", "best_development_diagnostic", "most_robust", "highest_sample", "lowest_turnover"):
        row = scoreboard[label]
        print(f"{label}: {row['strategy'] if row else 'n/a'}")
    print("Auxiliary gates:")
    for name, item in scoreboard["auxiliary_gates"].items():
        print(f"  {name}: {item['status']}")


if __name__ == "__main__":
    main()
