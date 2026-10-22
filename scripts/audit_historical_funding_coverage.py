#!/usr/bin/env python3
"""Write the fixed one-day funding-availability receipt for archival research."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.historical_funding_coverage import audit_historical_funding_coverage


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
    artifact_path = PROJECT_ROOT / "research/historical_universe/daily-membership-2024H1.json"
    policy_path = PROJECT_ROOT / "research/historical-universe-reconstruction-policy.json"
    manifest_path = PROJECT_ROOT / "research/historical_funding/manifest-2024H1.json"
    report = audit_historical_funding_coverage(
        json.loads(artifact_path.read_text(encoding="utf-8")),
        json.loads(policy_path.read_text(encoding="utf-8")),
        json.loads(manifest_path.read_text(encoding="utf-8")),
        holding_days=1,
    )
    report.update({"artifact_path": str(artifact_path.relative_to(PROJECT_ROOT)), "manifest_path": str(manifest_path.relative_to(PROJECT_ROOT))})
    _atomic_write(PROJECT_ROOT / "reports/historical-funding-coverage.json", report)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
