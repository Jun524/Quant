#!/usr/bin/env python3
"""Print a read-only integrity diagnostic for the active shadow-entry campaign."""

from __future__ import annotations

import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.monitoring.shadow_campaign_progress import summarize_shadow_campaign_progress


def main() -> None:
    state_path = PROJECT_ROOT / "research" / "execution" / "shadow-entry-campaign-state.json"
    if not state_path.is_file():
        raise SystemExit("shadow entry campaign state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    campaign_dir = PROJECT_ROOT / str(state["campaign_dir"])
    rows: list[dict] = []
    for symbol in state["symbols"]:
        path = campaign_dir / f"{symbol}.jsonl"
        if not path.is_file():
            continue
        rows.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    report = summarize_shadow_campaign_progress(
        rows,
        symbols=state["symbols"],
        orders_per_symbol=int(state["orders_per_symbol"]),
        campaign_status=str(state.get("status") or "UNKNOWN"),
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
