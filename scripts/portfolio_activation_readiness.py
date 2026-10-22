#!/usr/bin/env python3
"""Report whether a new locked-forward portfolio sample may be preregistered."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.portfolio_activation import evaluate_portfolio_activation


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
    policy = json.loads((PROJECT_ROOT / "research" / "portfolio-activation-policy.json").read_text(encoding="utf-8"))
    registry = json.loads((PROJECT_ROOT / "research" / "registry.json").read_text(encoding="utf-8"))
    report = evaluate_portfolio_activation(policy, registry)
    _atomic_write(PROJECT_ROOT / "reports" / "portfolio-activation-readiness.json", report)
    print(json.dumps(report, indent=2))
    if report["status"] == "ACTIVATION_CONTRACT_BLOCK":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
