#!/usr/bin/env python3
"""Build a source-bound Qlib-style diagnostic panel for Alpha #13 only."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.funding_dispersion_reversal import load_sealed_markets
from quant.research.funding_dispersion_signal_panel import (
    assert_alpha013_signal_contract,
    build_funding_dispersion_signal_panel,
)
from quant.research.signal_diagnostics import SIGNAL_DIAGNOSTIC_HEADER


PREREGISTRATION = PROJECT_ROOT / "research" / "alpha013-preregistration.json"
RAW_ROOT = PROJECT_ROOT / "research" / "alpha013" / "raw"
PANEL_PROTOCOL_FILES = (
    "research/alpha013-preregistration.json",
    "quant/research/funding_dispersion_reversal.py",
    "quant/research/funding_dispersion_signal_panel.py",
    "scripts/build_alpha013_signal_panel.py",
)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _panel_csv(observations: list[Any]) -> str:
    rows = [SIGNAL_DIAGNOSTIC_HEADER]
    rows.extend(
        [
            item.decision_at.isoformat(),
            item.forward_start_at.isoformat(),
            item.forward_end_at.isoformat(),
            item.symbol,
            repr(item.signal),
            repr(item.forward_return),
        ]
        for item in observations
    )
    from io import StringIO

    output = StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    return output.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel-output", type=Path, required=True)
    parser.add_argument("--receipt-output", type=Path, required=True)
    args = parser.parse_args()

    preregistration_bytes = PREREGISTRATION.read_bytes()
    preregistration = json.loads(preregistration_bytes)
    assert_alpha013_signal_contract(preregistration)
    markets, integrity = load_sealed_markets(RAW_ROOT)
    observations, coverage = build_funding_dispersion_signal_panel(
        markets,
        first_decision=preregistration["fresh_sample"]["first_eligible_decision"],
    )
    panel = _panel_csv(observations)
    _atomic_write(args.panel_output, panel)
    receipt = {
        "schema_version": 1,
        "kind": "ALPHA013_SOURCE_BOUND_SIGNAL_PANEL",
        "status": "READY_FOR_SIGNAL_DIAGNOSTIC" if observations else "ACCUMULATING",
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "panel": {
            "path": str(args.panel_output),
            "sha256": sha256(panel.encode("utf-8")).hexdigest(),
            "header": SIGNAL_DIAGNOSTIC_HEADER,
        },
        "coverage": coverage,
        "source_integrity": integrity,
        "protocol_sha256": {
            relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
            for relative in PANEL_PROTOCOL_FILES
        },
        "warning": "Gross unhedged labels are a source-bound predictive-signal diagnostic only. They exclude stop behavior, fees, slippage, funding, execution uncertainty, and strategy PnL.",
    }
    _atomic_write(args.receipt_output, json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"status": receipt["status"], "coverage": coverage}, indent=2))


if __name__ == "__main__":
    main()
