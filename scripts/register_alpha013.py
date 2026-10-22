#!/usr/bin/env python3
"""Register forward-only Alpha #13 exactly once."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


CANDIDATE = "ALPHA_013_FUNDING_DISPERSION_REVERSAL"


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    if prereg.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #13 preregistration candidate mismatch")
    if prereg.get("status") != "PREREGISTERED_FORWARD_ONLY":
        raise SystemExit("FAIL: Alpha #13 must be forward-only")
    provenance = prereg.get("provenance") or {}
    if provenance.get("post_hoc_hypothesis") is not True:
        raise SystemExit("FAIL: Alpha #13 post-hoc provenance must be explicit")
    if int(provenance.get("parent_experiment") or -1) != 9:
        raise SystemExit("FAIL: Alpha #13 must remain a child of Experiment 0009")

    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    existing = [
        item for item in registry_data.get("experiments", [])
        if item.get("strategy") == CANDIDATE
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_REGISTERED", "experiment_id": existing[0]["experiment_id"]}))
        return
    if not any(
        int(item.get("experiment_id") or -1) == 9
        for item in registry_data.get("experiments", [])
    ):
        raise SystemExit("FAIL: parent Experiment 0009 is absent")

    record = ExperimentRegistry(registry_path).register(
        strategy=CANDIDATE,
        hypothesis=prereg["hypothesis"],
        reason=(
            "Prospective-only test of the relative price component discovered after "
            "Alpha #9 failed its carry mechanism; no previously seen return may count."
        ),
        result={
            "source_preregistration": "research/alpha013-preregistration.json",
            "post_hoc_hypothesis": True,
            "historical_confirmation_allowed": False,
            "development": {},
            "validation_opened": False,
            "gate": {
                "status": "ACCUMULATING",
                "stage": "forward_collection",
                "checks": [
                    {"name": "thirty_fresh_development_weeks", "passed": False},
                    {"name": "sixty_fresh_closed_legs", "passed": False},
                    {"name": "raw_relative_price_pnl_positive", "passed": False},
                    {"name": "locked_validation_remains_unopened", "passed": True}
                ]
            }
        },
        parent_experiment=9,
        reason_for_change=(
            "Experiment 0009 observed positive price PnL but failed because funding carry "
            "did not cover friction; this mechanism change therefore requires entirely new data."
        ),
        trial_accounting={
            "alpha_number": 13,
            "family": "cross_sectional_funding_dispersion",
            "family_trial": 2,
            "post_hoc_from_experiment": 9
        },
    )
    print(json.dumps({"status": "REGISTERED", "experiment_id": record["experiment_id"]}, indent=2))


if __name__ == "__main__":
    main()
