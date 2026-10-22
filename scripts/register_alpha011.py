#!/usr/bin/env python3
"""Register forward-only Alpha #11 before any eligible observation is evaluated."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


def main() -> None:
    registry_path = PROJECT_ROOT / "research" / "registry.json"
    prereg_path = PROJECT_ROOT / "research" / "alpha011-preregistration.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    candidate = prereg["candidate"]
    existing = [
        item for item in registry_data.get("experiments", []) if item.get("strategy") == candidate
    ]
    if existing:
        print(
            json.dumps(
                {
                    "status": "ALREADY_REGISTERED",
                    "experiment_id": existing[0]["experiment_id"],
                    "created_at": existing[0]["created_at"],
                }
            )
        )
        return
    record = ExperimentRegistry(registry_path).register(
        strategy=candidate,
        hypothesis=prereg["hypothesis"],
        reason="Forward-only clock-phase order-flow family motivated by a 2026 Binance perpetual-futures study; non-overlapping execution is fixed before eligible data.",
        result={
            "source_preregistration": "research/alpha011-preregistration.json",
            "validation_opened": False,
            "development": {},
            "gate": {
                "status": "ACCUMULATING",
                "stage": "forward_collection",
                "checks": [
                    {"name": "sixty_complete_development_days", "passed": False},
                    {"name": "one_hundred_natural_exits", "passed": False},
                    {"name": "locked_validation_remains_unopened", "passed": True}
                ]
            }
        },
        parent_experiment=None,
        reason_for_change=None,
        trial_accounting={
            "alpha_number": 11,
            "family": "clock_phase_order_flow",
            "family_trial": 1
        },
    )
    print(
        json.dumps(
            {
                "status": "REGISTERED",
                "experiment_id": record["experiment_id"],
                "created_at": record["created_at"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
