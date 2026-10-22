#!/usr/bin/env python3
"""Build a sealed multi-block risk audit from daily portfolio MTM equity."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.portfolio_risk import (
    PORTFOLIO_RISK_PROTOCOL_FILES,
    evaluate_portfolio_risk,
    load_portfolio_equity,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--equity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_bytes = args.equity.read_bytes()
    result = evaluate_portfolio_risk(load_portfolio_equity(args.equity))
    result.update(
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in PORTFOLIO_RISK_PROTOCOL_FILES
            },
            "source": {
                "path": str(args.equity),
                "sha256": sha256(source_bytes).hexdigest(),
            },
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
