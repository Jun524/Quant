#!/usr/bin/env python3
"""Validate the sealed historical baseline and withheld-release order."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.historical_universe import verify_historical_daily_universe
from quant.research.historical_release_train import evaluate_historical_release_train


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    policy_path = PROJECT_ROOT / "research" / "historical-release-train-policy.json"
    baseline_path = PROJECT_ROOT / "research" / "historical_universe" / "daily-membership-2024H1.json"
    audit_path = PROJECT_ROOT / "reports" / "historical-universe-audit.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    baseline_receipt = verify_historical_daily_universe(baseline)
    report = evaluate_historical_release_train(
        policy,
        baseline_universe_sha256=baseline_receipt["universe_sha256"],
        baseline_audit_status=audit.get("status"),
    )
    report["policy_path"] = str(policy_path.relative_to(PROJECT_ROOT))
    report["baseline_artifact_path"] = str(baseline_path.relative_to(PROJECT_ROOT))
    _atomic_write(PROJECT_ROOT / "reports" / "historical-release-train-status.json", report)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
