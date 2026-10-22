#!/usr/bin/env python3
"""Register Experiment 0015 only after replayable activation readiness."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_preregistration import (
    build_alpha015_preregistration,
    preregistration_artifact_paths,
)
from quant.research.registry import ExperimentRegistry
from scripts.alpha015_readiness import build_report


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise SystemExit("existing Alpha 015 preregistration differs from source replay")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    readiness, _ = build_report()
    if (
        readiness.get("status") != "READY_FOR_PREREGISTRATION_ONLY"
        or readiness.get("preregistration_authorized") is not True
        or readiness.get("return_data_authorized") is not False
    ):
        raise SystemExit(
            "Alpha 015 preregistration is not authorized; universe, method, and activation-lock gates remain fail-closed"
        )
    template = json.loads(
        (PROJECT_ROOT / "research" / "alpha015-preregistration-template.json").read_text(
            encoding="utf-8"
        )
    )
    activation_lock = json.loads(
        (PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json").read_text(
            encoding="utf-8"
        )
    )
    preregistration = build_alpha015_preregistration(
        template=template,
        activation_lock=activation_lock,
        artifact_paths=preregistration_artifact_paths(),
        project_root=PROJECT_ROOT,
    )
    preregistration_path = PROJECT_ROOT / "research" / "alpha015-preregistration.json"
    write_once(
        preregistration_path,
        (json.dumps(preregistration, indent=2) + "\n").encode("utf-8"),
    )
    expected_result = {
        "source_preregistration": "research/alpha015-preregistration.json",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "activation_lock_sha256": preregistration["activation_binding"]["lock_sha256"],
        "return_data_acquired": False,
        "validation_opened": False,
        "development": {},
        "gate": {
            "status": "ACCUMULATING",
            "stage": "source_bound_development_acquisition",
            "checks": [
                {"name": "one_hundred_twenty_development_days", "passed": False},
                {"name": "base_and_stress_net_positive", "passed": False},
                {"name": "selection_bias_gates", "passed": False},
                {"name": "locked_validation_remains_unopened", "passed": True},
            ],
        },
        "live_promotion_eligible": False,
    }
    record, created = ExperimentRegistry(
        PROJECT_ROOT / "research" / "registry.json"
    ).register_unique_strategy(
        strategy=preregistration["candidate"],
        hypothesis=preregistration["hypothesis"],
        reason=(
            "Source-aligned, forward-only test activated after 180 sealed prior-day universe "
            "snapshots; no return archive was acquired before this receipt."
        ),
        result=expected_result,
        parent_experiment=None,
        reason_for_change=None,
        trial_accounting={
            "alpha_number": 15,
            "family": "minute_cross_crypto_predictability",
            "family_trial": 1,
            "independent_alpha": True,
        },
    )
    if (
        record.get("hypothesis") != preregistration["hypothesis"]
        or record.get("result") != expected_result
        or record.get("trial_accounting")
        != {
            "alpha_number": 15,
            "family": "minute_cross_crypto_predictability",
            "family_trial": 1,
            "independent_alpha": True,
        }
    ):
        raise SystemExit("existing Experiment 0015 registry record differs from preregistration")
    replay, _ = build_report()
    if replay.get("status") != "READY_FOR_DATA_ACQUISITION":
        raise SystemExit("Experiment 0015 registration did not produce a valid acquisition receipt")
    print(
        json.dumps(
            {
                "status": "REGISTERED" if created else "ALREADY_REGISTERED",
                "experiment_id": record["experiment_id"],
                "preregistration_sha256": preregistration["preregistration_sha256"],
                "return_data_authorized": replay["return_data_authorized"],
                "live_order_authorization": "CLOSED",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
