#!/usr/bin/env python3
"""Replay and gate the sealed Alpha 015 development sample.

This command is deliberately unavailable until Alpha 015 is preregistered and
the acquisition manifest is complete. It never opens locked-validation data
and only writes a development report; a passing report is not live authority.
"""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.alpha015_evaluation import evaluate_alpha015_development
from quant.research.alpha015_replay import (
    Alpha015ReplayError,
    prepare_alpha015_phase_bundle,
    replay_alpha015_day,
)
from scripts.alpha015_readiness import build_report


DATA_ROOT = PROJECT_ROOT / "research" / "alpha015_data" / "development"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
REPORT_PATH = PROJECT_ROOT / "research" / "alpha015_development" / "report.json"


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Alpha015ReplayError(f"invalid Alpha 015 artifact: {path.name}") from error
    if not isinstance(value, dict):
        raise Alpha015ReplayError(f"Alpha 015 artifact is not an object: {path.name}")
    return value


def _write_once(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise Alpha015ReplayError("existing development report differs from replay")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    readiness, _ = build_report()
    if not (
        readiness.get("status") == "READY_FOR_DATA_ACQUISITION"
        and readiness.get("return_data_authorized") is True
    ):
        raise Alpha015ReplayError("Alpha 015 development replay is not authorized")
    preregistration = _load(PROJECT_ROOT / "research" / "alpha015-preregistration.json")
    activation_lock = _load(
        PROJECT_ROOT / "research" / "alpha015_activation" / "activation-lock.json"
    )
    manifest = _load(MANIFEST_PATH)
    bundle = prepare_alpha015_phase_bundle(
        manifest=manifest,
        activation_lock=activation_lock,
        archive_root=DATA_ROOT,
        phase="development",
        expected_preregistration_sha256=str(preregistration["preregistration_sha256"]),
    )
    replays = [
        replay_alpha015_day(
            bundle,
            activation_lock=activation_lock,
            evaluation_day=day,
            verify_future_row_invariance=True,
        )
        for day in activation_lock["development"]
    ]
    report = evaluate_alpha015_development(
        replays,
        candidate=str(preregistration["candidate"]),
        lookahead_analysis_passed=True,
        future_row_invariance_passed=True,
    )
    report["source"] = {
        "preregistration_path": "research/alpha015-preregistration.json",
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "activation_lock_sha256": activation_lock["lock_sha256"],
        "development_manifest_path": str(MANIFEST_PATH.relative_to(PROJECT_ROOT)),
        "development_manifest_sha256": sha256(MANIFEST_PATH.read_bytes()).hexdigest(),
        "replay_engine_path": "quant/research/alpha015_replay.py",
        "replay_engine_sha256": sha256(
            (PROJECT_ROOT / "quant" / "research" / "alpha015_replay.py").read_bytes()
        ).hexdigest(),
    }
    _write_once(REPORT_PATH, report)
    print(json.dumps({"status": report["status"], "report": str(REPORT_PATH)}, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    try:
        main()
    except Alpha015ReplayError as error:
        raise SystemExit(f"Alpha 015 development replay blocked: {error}") from error
