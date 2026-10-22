#!/usr/bin/env python3
"""Register Alpha #14's development-only transfer diagnostic exactly once."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


CANDIDATE = "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP"


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha014-preregistration.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    if prereg.get("candidate") != CANDIDATE:
        raise SystemExit("FAIL: Alpha #14 preregistration candidate mismatch")
    if prereg.get("status") != "PREREGISTERED_DEVELOPMENT_ONLY_TRANSFER_TEST":
        raise SystemExit("FAIL: Alpha #14 must remain a development-only transfer test")
    provenance = prereg.get("provenance") or {}
    if provenance.get("independent_alpha") is not False:
        raise SystemExit("FAIL: Alpha #14 must not be represented as independent")
    if int(provenance.get("family_trial") or -1) != 3:
        raise SystemExit("FAIL: Alpha #14 must remain family trial 3")

    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    existing = [
        item for item in registry_data.get("experiments", [])
        if item.get("strategy") == CANDIDATE
    ]
    if existing:
        print(json.dumps({"status": "ALREADY_REGISTERED", "experiment_id": existing[0]["experiment_id"]}))
        return
    experiment_ids = {
        int(item.get("experiment_id") or -1)
        for item in registry_data.get("experiments", [])
    }
    if not {9, 13}.issubset(experiment_ids):
        raise SystemExit("FAIL: parent experiments 0009 and 0013 must exist")

    record = ExperimentRegistry(registry_path).register(
        strategy=CANDIDATE,
        hypothesis=prereg["hypothesis"],
        reason=(
            "Development-only transfer test of the dated-futures basis result on "
            "Binance perpetuals; explicitly not an independent alpha or promotion sample."
        ),
        result={
            "source_preregistration": "research/alpha014-preregistration.json",
            "source_screening": "research/alpha-candidate-screening-20261010.json",
            "validation_opened": False,
            "live_promotion_eligible": False,
            "development": {},
            "gate": {
                "status": "ACCUMULATING",
                "stage": "preregistered_development_transfer_test",
                "checks": [
                    {"name": "development_evaluated", "passed": False},
                    {"name": "family_trial_three_disclosed", "passed": True},
                    {"name": "historical_validation_absent", "passed": True},
                    {"name": "live_promotion_ineligible", "passed": True}
                ]
            }
        },
        parent_experiment=13,
        reason_for_change=(
            "Alpha 009/013 use funding dispersion; Alpha 014 tests the related "
            "spot-minus-perpetual price basis daily, so fresh trial accounting is mandatory."
        ),
        trial_accounting={
            "alpha_number": 14,
            "family": "cross_sectional_funding_dispersion",
            "family_trial": 3,
            "post_hoc_from_experiments": [9, 13],
            "independent_alpha": False
        },
    )
    print(json.dumps({"status": "REGISTERED", "experiment_id": record["experiment_id"]}, indent=2))


if __name__ == "__main__":
    main()
