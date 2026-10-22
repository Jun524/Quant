#!/usr/bin/env python3
"""Generate a deterministic, non-tuning postmortem for failed Alpha #10."""

from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.registry import ExperimentRegistry


SOURCE = Path("reports/alpha010-development.json")
OUTPUT = Path("reports/alpha010-postmortem.json")
MARKDOWN = Path("reports/alpha010-postmortem.md")


def main() -> None:
    source_payload = SOURCE.read_bytes()
    report = json.loads(source_payload)
    if report.get("status") != "FAIL_DEVELOPMENT" or report.get("validation_opened") is not False:
        raise SystemExit("Alpha #10 source is not the sealed development failure")
    base = report["development"]["base"]
    trades = report["development"]["trades"]
    by_year: dict[str, list[float]] = defaultdict(list)
    by_exit: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_year[str(trade["decision_time"])[:4]].append(float(trade["net_return"]))
        by_exit[str(trade["exit_reason"])].append(float(trade["net_return"]))

    yearly = {
        year: {
            "trades": len(values),
            "sum_net_return": sum(values),
            "mean_net_return": mean(values),
            "wins": sum(value > 0 for value in values),
            "losses": sum(value < 0 for value in values),
        }
        for year, values in sorted(by_year.items())
    }
    exit_groups = {
        reason: {
            "trades": len(values),
            "sum_net_return": sum(values),
            "mean_net_return": mean(values),
        }
        for reason, values in sorted(by_exit.items())
    }
    every_year_negative = all(item["sum_net_return"] < 0 for item in yearly.values())
    every_exit_family_negative = all(item["sum_net_return"] < 0 for item in exit_groups.values())
    price_edge_negative = float(base["price_return_sum"]) < 0
    conclusion = (
        "NO_SIGNAL_EDGE_BROAD_BASED"
        if every_year_negative and every_exit_family_negative and price_edge_negative
        else "MIXED_FAILURE_REQUIRES_DIAGNOSTIC_REVIEW"
    )
    result = {
        "schema_version": 1,
        "candidate": report["candidate"],
        "source_report": str(SOURCE),
        "source_sha256": sha256(source_payload).hexdigest(),
        "verdict": "FAIL_REMAINS_SEALED",
        "locked_oos_opened": False,
        "post_result_parameter_changes": False,
        "failure_attribution": conclusion,
        "evidence": {
            "trades": base["trades"],
            "compounded_net_return": base["compounded_net_return"],
            "profit_factor": base["profit_factor"],
            "maximum_drawdown": base["maximum_drawdown"],
            "price_return_sum": base["price_return_sum"],
            "trading_fee_drag_sum": base["trading_fee_drag_sum"],
            "slippage_drag_sum": base["slippage_drag_sum"],
            "funding_return_sum": base["funding_return_sum"],
            "three_percent_crash_precision_lift": base[
                "three_percent_crash_precision_lift"
            ],
            "yearly": yearly,
            "exit_reason_groups": exit_groups,
            "every_year_negative": every_year_negative,
            "every_exit_family_negative": every_exit_family_negative,
            "price_edge_negative_before_costs": price_edge_negative,
        },
        "research_action": "Do not tune Alpha #10 thresholds, stop, holding horizon, or costs. Reopen this family only with a new externally motivated mechanism and a new trial number.",
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Alpha #10 postmortem",
        "",
        f"- Verdict: **{result['verdict']}**",
        f"- Attribution: **{conclusion}**",
        f"- Compounded net return: {base['compounded_net_return']:.2%}",
        f"- Price return sum before fees/slippage/funding: {base['price_return_sum']:.2%}",
        f"- Profit factor: {base['profit_factor']:.3f}",
        f"- Maximum drawdown: {base['maximum_drawdown']:.2%}",
        f"- 3% crash precision lift: {base['three_percent_crash_precision_lift']:.3f}",
        "- Locked OOS opened: **no**",
        "- Post-result parameter changes: **none**",
        "",
        "## Calendar stability",
        "",
    ]
    lines.extend(
        f"- {year}: {item['trades']} trades, sum net {item['sum_net_return']:.2%}"
        for year, item in yearly.items()
    )
    lines.extend(["", "## Exit families", ""])
    lines.extend(
        f"- {reason}: {item['trades']} trades, sum net {item['sum_net_return']:.2%}"
        for reason, item in exit_groups.items()
    )
    lines.extend(["", result["research_action"], ""])
    MARKDOWN.write_text("\n".join(lines), encoding="utf-8")
    registry_path = Path("research/registry.json")
    registry_data = json.loads(registry_path.read_text(encoding="utf-8"))
    experiment = next(
        item
        for item in registry_data["experiments"]
        if item.get("strategy") == "ALPHA_010_LEVERAGE_CROWDING_CRASH_SHORT"
    )
    prior = [
        event
        for event in registry_data.get("events", [])
        if event.get("experiment_id") == experiment["experiment_id"]
        and event.get("event_type") == "postmortem"
        and (event.get("payload") or {}).get("source_sha256") == result["source_sha256"]
    ]
    if not prior:
        ExperimentRegistry(registry_path).append_event(
            experiment_id=int(experiment["experiment_id"]),
            event_type="postmortem",
            payload={
                "source_report": str(OUTPUT),
                "source_sha256": result["source_sha256"],
                "failure_attribution": conclusion,
                "locked_oos_opened": False,
                "post_result_parameter_changes": False,
            },
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
