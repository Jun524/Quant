#!/usr/bin/env python3
"""Report whether historical diagnostic costs are fixed before evaluation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.historical_cost_guard import evaluate_historical_cost_policy


def main() -> None:
    policy_path = PROJECT_ROOT / "research" / "historical-cost-accounting-policy.json"
    report = evaluate_historical_cost_policy(json.loads(policy_path.read_text(encoding="utf-8")))
    report["policy_path"] = str(policy_path.relative_to(PROJECT_ROOT))
    output = PROJECT_ROOT / "reports" / "historical-cost-policy-status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
