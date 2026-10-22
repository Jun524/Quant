#!/usr/bin/env python3
"""Register Alpha #10 once, before downloading or evaluating its sample."""

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
    prereg_path = PROJECT_ROOT / "research" / "alpha010-preregistration.json"
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    candidate = prereg["candidate"]
    existing = [item for item in registry_data.get("experiments", []) if item.get("strategy") == candidate]
    if existing:
        print(json.dumps({"status": "ALREADY_REGISTERED", "experiment_id": existing[0]["experiment_id"]}))
        return
    record = ExperimentRegistry(registry_path).register(
        strategy=candidate,
        hypothesis=prereg["hypothesis"],
        reason="Independent derivatives-leverage crash-risk family backed by checksum-verifiable Binance open-interest metrics.",
        result={
            "source_preregistration": "research/alpha010-preregistration.json",
            "validation_opened": False,
            "development": {},
            "gate": {
                "status": "ACCUMULATING",
                "stage": "public_data_acquisition",
                "checks": [
                    {"name": "publisher_checksums_verified", "passed": False},
                    {"name": "development_evaluated", "passed": False},
                    {"name": "locked_oos_remains_unopened", "passed": True}
                ]
            }
        },
        parent_experiment=None,
        reason_for_change=None,
        trial_accounting={
            "alpha_number": 10,
            "family": "derivatives_leverage_crowding",
            "family_trial": 1
        },
    )
    print(json.dumps({"status": "REGISTERED", "experiment_id": record["experiment_id"]}, indent=2))


if __name__ == "__main__":
    main()
