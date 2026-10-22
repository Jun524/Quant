#!/usr/bin/env python3
"""Register the simulation-only spot/perpetual flow-divergence hypothesis once."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


CANDIDATE = "ALPHA_016_SPOT_PERP_FLOW_DIVERGENCE"


def main() -> None:
    preregistration_path = PROJECT_ROOT / "research" / "alpha016-preregistration.json"
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    if preregistration.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha 016 preregistration candidate mismatch")
    if preregistration.get("status") != "PREREGISTERED_HISTORICAL_DEVELOPMENT_LOCKED":
        raise SystemExit("FAIL: Alpha 016 must remain development locked")
    if (preregistration.get("research_scope") or {}).get("orders_submitted") is not False:
        raise SystemExit("FAIL: Alpha 016 must remain simulation-only")
    if (preregistration.get("research_scope") or {}).get("funding_included") is not False:
        raise SystemExit("FAIL: Alpha 016 must not credit funding")
    if (preregistration.get("research_scope") or {}).get("live_promotion_eligible") is not False:
        raise SystemExit("FAIL: Alpha 016 must not be live-promotion eligible")

    expected_result = {
        "source_preregistration": "research/alpha016-preregistration.json",
        "development_data_acquired": False,
        "validation_opened": False,
        "live_promotion_eligible": False,
        "development": {},
        "gate": {
            "status": "ACCUMULATING",
            "stage": "preregistered_historical_development_acquisition",
            "checks": [
                {"name": "spot_and_perpetual_data_admitted", "passed": False},
                {"name": "development_evaluated", "passed": False},
                {"name": "locked_oos_unopened", "passed": True},
                {"name": "simulation_only", "passed": True},
            ],
        },
    }
    record, created = ExperimentRegistry(PROJECT_ROOT / "research" / "registry.json").register_unique_strategy(
        strategy=CANDIDATE,
        hypothesis=preregistration["hypothesis"],
        reason=(
            "First fixed test of delayed BTC spot performance following a completed "
            "spot/perpetual aggressive-flow disagreement; simulation only."
        ),
        result=expected_result,
        parent_experiment=None,
        reason_for_change=None,
        trial_accounting={
            "alpha_number": 16,
            "family": "spot_perpetual_flow_divergence",
            "family_trial": 1,
            "independent_alpha": False,
            "independence_status": "UNMEASURED_SHARED_ORDER_FLOW_DATA",
        },
    )
    if record.get("result") != expected_result:
        raise SystemExit("FAIL: existing Alpha 016 registry record differs from preregistration")
    print(json.dumps({"status": "REGISTERED" if created else "ALREADY_REGISTERED", "experiment_id": record["experiment_id"], "live_order_authorization": "CLOSED"}, indent=2))


if __name__ == "__main__":
    main()
