#!/usr/bin/env python3
"""Print the fail-closed capital-preservation policy contract status."""

from __future__ import annotations

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.risk_policy_contract import validate_risk_policy_contract


def main() -> None:
    risk = json.loads((PROJECT_ROOT / "research/risk-policy.json").read_text(encoding="utf-8"))
    objective = json.loads(
        (PROJECT_ROOT / "research/capital-preservation-objective.json").read_text(encoding="utf-8")
    )
    result = validate_risk_policy_contract(risk, objective)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
