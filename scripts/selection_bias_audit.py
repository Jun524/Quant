#!/usr/bin/env python3
"""Create a sealed DSR/CSCV-PBO audit from a complete daily trial matrix."""

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

from quant.research.selection_bias import (
    SELECTION_BIAS_PROTOCOL_FILES,
    evaluate_selection_bias,
    load_daily_trial_matrix,
    validate_trial_inventory,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-returns", type=Path, required=True)
    parser.add_argument("--trial-inventory", type=Path, required=True)
    parser.add_argument("--timestamp-column", default="timestamp")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source_bytes = args.trial_returns.read_bytes()
    timestamps, trial_ids, matrix = load_daily_trial_matrix(
        args.trial_returns, args.timestamp_column
    )
    inventory_bytes = args.trial_inventory.read_bytes()
    inventory = json.loads(inventory_bytes.decode("utf-8"))
    inventory_validation = validate_trial_inventory(
        inventory,
        trial_ids=trial_ids,
        first_observation_at=timestamps[0],
    )
    if inventory_validation["status"] != "PASS":
        raise SystemExit(
            "invalid trial inventory: " + "; ".join(inventory_validation["failures"])
        )
    selected_trial = str(inventory_validation["selected_trial_id"])
    selected_column = trial_ids.index(selected_trial)
    result = evaluate_selection_bias(
        matrix[:, selected_column],
        matrix,
        attempted_trials=len(trial_ids),
        trial_ids=trial_ids,
        selected_trial_id=selected_trial,
        trial_inventory_complete=True,
        pre_registered_single_trial=bool(
            inventory_validation["pre_registered_single_trial"]
        ),
    )
    result.update(
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "protocol_current": True,
            "protocol_sha256": {
                relative: sha256((PROJECT_ROOT / relative).read_bytes()).hexdigest()
                for relative in SELECTION_BIAS_PROTOCOL_FILES
            },
            "source": {
                "path": str(args.trial_returns),
                "sha256": sha256(source_bytes).hexdigest(),
                "timestamp_column": args.timestamp_column,
                "first_timestamp": timestamps[0],
                "last_timestamp": timestamps[-1],
                "rows": len(timestamps),
            },
            "trial_inventory": {
                "path": str(args.trial_inventory),
                "sha256": sha256(inventory_bytes).hexdigest(),
                "validation": inventory_validation,
            },
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "output": str(args.output)}, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 2)


if __name__ == "__main__":
    main()
