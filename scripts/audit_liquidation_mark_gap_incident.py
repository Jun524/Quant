#!/usr/bin/env python3
"""Re-audit the immutable excluded-day receipt for liquidation telemetry."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.liquidation_history import assess_liquidation_history
from quant.research.liquidation_incident import verify_mark_gap_incident
from quant.research.liquidation_telemetry import protocol_digests


INCIDENT_PATH = PROJECT_ROOT / "research/incidents/liquidation-telemetry-mark-gap-20261028.json"
POLICY_PATH = PROJECT_ROOT / "research/liquidation-telemetry-policy.json"
ROOT = PROJECT_ROOT / "research/liquidation_telemetry"
OUTPUT = PROJECT_ROOT / "reports/liquidation-telemetry-mark-gap-incident-audit.json"


def main() -> None:
    incident = json.loads(INCIDENT_PATH.read_text(encoding="utf-8"))
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    coverage = assess_liquidation_history(
        ROOT, policy=policy, expected_protocol=protocol_digests(PROJECT_ROOT)
    )
    receipt = {
        **verify_mark_gap_incident(
            incident, project_root=PROJECT_ROOT, coverage=coverage
        ),
        "coverage_status": coverage["status"],
        "complete_utc_days_count": coverage["complete_utc_days_count"],
    }
    OUTPUT.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2))
    if receipt["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
