#!/usr/bin/env python3
"""Apply the immutable 10bp external execution stress to Alpha #7."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.external_cost_stress import evaluate_alpha007_external_cost_stress


def main() -> None:
    policy_path = PROJECT_ROOT / "research" / "alpha007-external-cost-stress-policy.json"
    primary_path = PROJECT_ROOT / "reports" / "alpha007-accumulation.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    calibration_path = PROJECT_ROOT / policy["source_calibration"]["report"]
    if sha256(calibration_path.read_bytes()).hexdigest() != policy["source_calibration"]["report_sha256"]:
        raise SystemExit("calibration report hash does not match the external stress policy")
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    if calibration.get("source", {}).get("raw_sha256") != policy["source_calibration"]["raw_sha256"]:
        raise SystemExit("calibration raw source does not match the external stress policy")
    if calibration.get("source", {}).get("manifest_sha256") != policy["source_calibration"]["manifest_sha256"]:
        raise SystemExit("calibration manifest does not match the external stress policy")
    primary = json.loads(primary_path.read_text(encoding="utf-8"))
    result = evaluate_alpha007_external_cost_stress(primary, calibration, policy)
    report = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy": policy["policy"],
        "policy_sha256": sha256(policy_path.read_bytes()).hexdigest(),
        "primary_report_sha256": sha256(primary_path.read_bytes()).hexdigest(),
        "calibration_report_sha256": sha256(calibration_path.read_bytes()).hexdigest(),
        **result,
    }
    output = PROJECT_ROOT / "reports" / "alpha007-external-cost-stress.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] == "FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
