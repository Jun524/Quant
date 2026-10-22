#!/usr/bin/env python3
"""Audit Alpha #13 funding availability receipts without evaluating PnL."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.funding_decision_availability import (
    audit_decision_availability,
    preregistration_digest,
)


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", type=Path, default=PROJECT_ROOT / "research/alpha013-preregistration.json")
    parser.add_argument("--receipts", type=Path, default=PROJECT_ROOT / "research/alpha013/decision-receipts")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "reports/alpha013-decision-availability.json")
    args = parser.parse_args()
    preregistration = json.loads(args.preregistration.read_text(encoding="utf-8"))
    report = audit_decision_availability(
        preregistration,
        preregistration_sha256=preregistration_digest(args.preregistration),
        receipt_directory=args.receipts,
        now=datetime.now(timezone.utc),
    )
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["preregistration_path"] = str(args.preregistration)
    _atomic_write(args.output, report)
    print(json.dumps(report, indent=2))
    if report["status"] == "CAUSAL_AVAILABILITY_VETO":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
