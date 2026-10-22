#!/usr/bin/env python3
"""Report point-in-time full-market universe accumulation status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document, protocol_digests
from scripts.run_universe_scheduler import scheduler_status_payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("research/universe_snapshots"))
    parser.add_argument("--required-days", type=int, default=180)
    parser.add_argument("--output", type=Path, default=Path("reports/universe-accumulation.json"))
    args = parser.parse_args()
    policy = policy_document()
    if args.required_days != int(policy["readiness"]["required_consecutive_days"]):
        raise SystemExit("--required-days cannot override the preregistered forward policy")
    report = assess_forward_universe_history(
        args.input,
        expected_protocol=protocol_digests(),
        policy=policy,
    )
    report["scheduler"] = scheduler_status_payload()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] == "DATA_INTEGRITY_FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
