#!/usr/bin/env python3
"""Report forward liquidation telemetry coverage without testing the hypothesis."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.liquidation_history import assess_liquidation_history
from quant.research.liquidation_telemetry import protocol_digests


def main() -> None:
    policy = json.loads(
        (PROJECT_ROOT / "research" / "liquidation-telemetry-policy.json").read_text(
            encoding="utf-8"
        )
    )
    report = assess_liquidation_history(
        PROJECT_ROOT / "research" / "liquidation_telemetry",
        policy=policy,
        expected_protocol=protocol_digests(PROJECT_ROOT),
    )
    output = PROJECT_ROOT / "reports" / "liquidation-telemetry-status.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] == "DATA_INTEGRITY_ATTENTION":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
