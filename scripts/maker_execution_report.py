#!/usr/bin/env python3
"""Evaluate actual maker-seeking order records from CSV or JSONL."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.maker_quality import summarize_maker_execution
from scripts.manage_alpha001_forward_dryrun import protocol_digests


RECEIPT_SCHEMA_VERSION = 6
RECEIPT_POLICY = "alpha001-signal-triggered-entry-execution-audit-v5"
PROMOTION_POLICY_PATH = PROJECT_ROOT / "research" / "promotion-policy.json"


def load_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    raise ValueError("input must be .csv, .jsonl, or .ndjson")


def validate_signal_triggered_rows(rows: list[dict], execution_policy: dict) -> None:
    """Reject rows that are not causal, continuous Alpha #1 entry probes."""
    if not rows:
        raise ValueError("execution evidence contains no rows")
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        expected_pair = f"{symbol[:-4]}/USDT:USDT" if symbol.endswith("USDT") else ""
        line_digest = str(row.get("trigger_line_sha256") or "")
        offset = row.get("trigger_log_offset")
        signal_id = str(row.get("signal_id") or "")
        observed_trades = int(row.get("aggregate_trades_observed") or 0)
        first_trade_id = row.get("first_requested_aggregate_trade_id")
        last_trade_id = row.get("last_observed_aggregate_trade_id")
        if (
            row.get("probe_kind") != "public_queue_estimate_no_order_submitted"
            or row.get("trigger_kind") != execution_policy["required_trigger_kind"]
            or row.get("trigger_source") != "local_freqtrade_initial_entry_log"
            or str(row.get("side") or "").lower() != "buy"
            or float(row.get("requested_notional_usdt") or 0) <= 0
            or float(row.get("requested_notional_usdt") or 0)
            > float(execution_policy["maximum_shadow_notional_usdt"])
            or float(row.get("requested_notional_usdt") or 0)
            != float(row.get("requested_stake_usdt") or 0)
            or int(row.get("timeout_seconds") or 0) != int(execution_policy["entry_timeout_seconds"])
            or int(row.get("markout_seconds") or 0)
            != int(execution_policy["post_fill_markout_seconds"])
            or row.get("trigger_pair") != expected_pair
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not re.fullmatch(r"[0-9a-f]{64}", line_digest)
            or signal_id != f"alpha001-{symbol}-{offset}-{line_digest[:12]}"
            or float(row.get("freqtrade_requested_limit_price") or 0) <= 0
            or row.get("requested_limit_price_replayed") is not True
            or float(row.get("shadow_limit_price") or 0)
            != float(row.get("freqtrade_requested_limit_price") or 0)
            or row.get("aggregate_trade_ids_continuous") is not True
            or observed_trades < 0
            or not isinstance(first_trade_id, int)
            or isinstance(first_trade_id, bool)
            or first_trade_id < 0
            or (
                observed_trades > 0
                and (
                    not isinstance(last_trade_id, int)
                    or isinstance(last_trade_id, bool)
                    or last_trade_id - first_trade_id + 1 != observed_trades
                )
            )
            or (observed_trades == 0 and last_trade_id is not None)
        ):
            raise ValueError("execution row does not match the causal signal-triggered probe policy")
        try:
            detected = datetime.fromisoformat(str(row["trigger_detected_at"]).replace("Z", "+00:00"))
            submitted = datetime.fromisoformat(str(row["submitted_at"]).replace("Z", "+00:00"))
            order_ended = datetime.fromisoformat(
                str(row["order_observation_ended_at"]).replace("Z", "+00:00")
            )
            if detected.tzinfo is None or submitted.tzinfo is None or order_ended.tzinfo is None:
                raise ValueError("timezone missing")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("execution row has invalid causal timestamps") from exc
        trigger_delay = (submitted - detected).total_seconds()
        order_duration = (order_ended - submitted).total_seconds()
        if (
            trigger_delay < 0
            or trigger_delay > float(execution_policy["maximum_trigger_to_probe_delay_seconds"])
            or order_duration < 0
            or order_duration > 360
        ):
            raise ValueError("execution row timing falls outside the preregistered bounds")
        filled = float(row.get("filled_qty") or 0)
        submitted_qty = float(row.get("submitted_qty") or 0)
        if filled < submitted_qty - 1e-12 and order_duration < 270:
            raise ValueError("unfilled shadow entry was observed for less than its five-minute timeout")
        if filled > 0:
            try:
                fill_observed = datetime.fromisoformat(
                    str(row["fill_observed_at"]).replace("Z", "+00:00")
                )
                markout_observed = datetime.fromisoformat(
                    str(row["markout_observed_at"]).replace("Z", "+00:00")
                )
                if fill_observed.tzinfo is None or markout_observed.tzinfo is None:
                    raise ValueError("timezone missing")
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("filled execution row has invalid markout timestamps") from exc
            markout_delay = (markout_observed - fill_observed).total_seconds()
            if markout_delay < 270 or markout_delay > 360:
                raise ValueError("filled execution row does not contain a five-minute markout")


def current_protocol_entry_manifest_inventory(
    project_root: Path,
    current_protocol: dict[str, str],
) -> list[dict]:
    """List every promotion-purpose entry manifest for the current protocol."""
    inventory: list[dict] = []
    root = project_root.resolve()
    manifest_root = root / "research" / "execution" / "raw"
    for path in (
        sorted(manifest_root.glob("alpha001-signal-entry-*.manifest.json"))
        if manifest_root.is_dir()
        else ()
    ):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            try:
                invalid_digest = sha256(path.read_bytes()).hexdigest()
            except OSError:
                invalid_digest = None
            inventory.append(
                {
                    "path": str(path.resolve().relative_to(root)),
                    "sha256": invalid_digest,
                    "status": "INVALID_UNREADABLE",
                }
            )
            continue
        if (
            payload.get("evidence_purpose") != "live_promotion_candidate"
            or payload.get("protocol_sha256") != current_protocol
        ):
            continue
        inventory.append(
            {
                "path": str(path.resolve().relative_to(root)),
                "sha256": sha256(path.read_bytes()).hexdigest(),
                "status": payload.get("status"),
            }
        )
    return inventory


def markdown(report: dict, sources: list[Path]) -> str:
    checks = "\n".join(
        f"- {'PASS' if item['passed'] else 'FAIL'} — `{item['name']}`: actual={item['actual']}"
        for item in report["checks"]
    )
    source_lines = "\n".join(f"- Source: `{source}`" for source in sources)
    return f"""# Maker execution quality

{source_lines}
- Status: **{report['status']}**
- Orders: {report['orders']}
- Quantity fill rate: {report['fill_rate_by_qty']:.2%}
- Maker share of fills: {report['maker_share_by_filled_qty']:.2%}
- Effective fee: {report['effective_fee_bps'] if report['effective_fee_bps'] is not None else 'N/A'} bps
- Signed slippage: {report['signed_slippage_bps'] if report['signed_slippage_bps'] is not None else 'N/A'} bps
- Fee + signed slippage: {report['fee_plus_signed_slippage_bps'] if report['fee_plus_signed_slippage_bps'] is not None else 'N/A'} bps
- Unfilled opportunity cost: {report['unfilled_opportunity_cost_bps'] if report['unfilled_opportunity_cost_bps'] is not None else 'N/A'} bps
- Five-minute adverse markout: {report['post_fill_adverse_markout_5m_bps'] if report['post_fill_adverse_markout_5m_bps'] is not None else 'N/A'} bps

## Checks

{checks}

Positive signed cost means execution hurt the strategy. A PASS here validates
execution only; it does not validate the trading alpha.
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, nargs="+")
    parser.add_argument("--output-prefix", type=Path, default=Path("reports/maker-execution"))
    parser.add_argument("--min-orders", type=int, default=100)
    parser.add_argument("--min-filled-orders", type=int, default=30)
    parser.add_argument("--min-orders-per-symbol", type=int, default=20)
    parser.add_argument("--min-filled-orders-per-symbol", type=int, default=6)
    parser.add_argument("--min-maker-share", type=float, default=0.70)
    parser.add_argument("--max-effective-fee-bps", type=float, default=2.8)
    parser.add_argument("--gross-edge-budget-bps", type=float)
    parser.add_argument("--evidence-kind", choices=("public_l2_shadow",), required=True)
    args = parser.parse_args()
    promotion_policy = json.loads(PROMOTION_POLICY_PATH.read_text(encoding="utf-8"))
    execution_policy = promotion_policy["execution_gate"]
    locked_budget = float(execution_policy["maximum_entry_execution_drag_bps"])
    gross_edge_budget_bps = (
        locked_budget if args.gross_edge_budget_bps is None else args.gross_edge_budget_bps
    )
    if gross_edge_budget_bps != locked_budget:
        raise ValueError("gross-edge-budget-bps must equal the preregistered execution budget")
    locked_cli_values = {
        "min_orders": int(execution_policy["minimum_orders"]),
        "min_filled_orders": int(execution_policy["minimum_filled_orders"]),
        "min_orders_per_symbol": int(execution_policy["minimum_orders_per_symbol"]),
        "min_filled_orders_per_symbol": int(execution_policy["minimum_filled_orders_per_symbol"]),
        "min_maker_share": float(execution_policy["minimum_maker_share"]),
        "max_effective_fee_bps": float(execution_policy["maximum_effective_fee_bps"]),
    }
    if any(getattr(args, name) != value for name, value in locked_cli_values.items()):
        raise ValueError("execution thresholds must equal the preregistered policy")
    input_paths = sorted({path.resolve() for path in args.input})
    relative_inputs: list[Path] = []
    for input_path in input_paths:
        try:
            relative_inputs.append(input_path.relative_to(PROJECT_ROOT.resolve()))
        except ValueError as exc:
            raise ValueError("execution evidence must be stored inside the project root") from exc
    rows = [row for input_path in input_paths for row in load_rows(input_path)]
    validate_signal_triggered_rows(rows, execution_policy)
    signal_ids = [str(row.get("signal_id") or "") for row in rows]
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("execution evidence contains duplicate signal ids across segments")
    manifest_paths = [input_path.with_suffix(".manifest.json") for input_path in input_paths]
    if any(not manifest_path.is_file() for manifest_path in manifest_paths):
        raise ValueError("execution evidence is missing a campaign manifest")
    current_protocol = protocol_digests()
    manifest_inventory = current_protocol_entry_manifest_inventory(PROJECT_ROOT, current_protocol)
    expected_manifest_paths = {
        str(manifest_path.relative_to(PROJECT_ROOT)) for manifest_path in manifest_paths
    }
    if (
        {item.get("path") for item in manifest_inventory} != expected_manifest_paths
        or any(item.get("status") != "SEALED_ON_STOP" for item in manifest_inventory)
    ):
        raise ValueError(
            "execution evidence must include every sealed current-protocol promotion segment"
        )
    source_segments: list[dict] = []
    for input_path, relative_input, manifest_path in zip(
        input_paths, relative_inputs, manifest_paths
    ):
        segment_rows = load_rows(input_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_digest = sha256(input_path.read_bytes()).hexdigest()
        if (
            manifest.get("schema_version") != 1
            or manifest.get("status") != "SEALED_ON_STOP"
            or manifest.get("evidence_kind") != args.evidence_kind
            or manifest.get("evidence_purpose") != "live_promotion_candidate"
            or manifest.get("order_scope") != "entry_long_only"
            or manifest.get("required_side") != "buy"
            or manifest.get("trigger_kind") != execution_policy["required_trigger_kind"]
            or manifest.get("source_path") != str(relative_input)
            or manifest.get("source_sha256") != source_digest
            or manifest.get("protocol_sha256") != current_protocol
            or int(manifest.get("orders") or 0) != len(segment_rows)
            or float(manifest.get("maximum_notional_usdt") or 0)
            != float(execution_policy["maximum_shadow_notional_usdt"])
            or int(manifest.get("timeout_seconds") or 0)
            != int(execution_policy["entry_timeout_seconds"])
            or int(manifest.get("markout_seconds") or 0)
            != int(execution_policy["post_fill_markout_seconds"])
        ):
            raise ValueError(
                "execution campaign manifest is incomplete, stale, or not bound to the raw rows"
            )
        source_segments.append(
            {
                "source_path": str(relative_input),
                "source_sha256": source_digest,
                "collection_manifest_path": str(manifest_path.relative_to(PROJECT_ROOT)),
                "collection_manifest_sha256": sha256(manifest_path.read_bytes()).hexdigest(),
                "orders": len(segment_rows),
            }
        )
    if args.evidence_kind == "public_l2_shadow" and any(
        row.get("probe_kind") != "public_queue_estimate_no_order_submitted" for row in rows
    ):
        raise ValueError("public_l2_shadow evidence contains a row without the public queue probe marker")
    if any(str(row.get("side") or "").lower() != "buy" for row in rows):
        raise ValueError("Alpha #1 execution evidence must contain long-entry buy probes only")
    if any(
        row.get("trigger_kind") != execution_policy["required_trigger_kind"]
        or row.get("trigger_source") != "local_freqtrade_initial_entry_log"
        or float(row.get("requested_notional_usdt") or 0) <= 0
        or float(row.get("requested_notional_usdt") or 0)
        > float(execution_policy["maximum_shadow_notional_usdt"])
        or float(row.get("requested_notional_usdt") or 0)
        != float(row.get("requested_stake_usdt") or 0)
        or int(row.get("timeout_seconds") or 0) != int(execution_policy["entry_timeout_seconds"])
        or int(row.get("markout_seconds") or 0) != int(execution_policy["post_fill_markout_seconds"])
        for row in rows
    ):
        raise ValueError("execution rows do not match the preregistered trigger, size, or observation horizons")
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        expected_pair = f"{symbol[:-4]}/USDT:USDT" if symbol.endswith("USDT") else ""
        line_digest = str(row.get("trigger_line_sha256") or "")
        offset = row.get("trigger_log_offset")
        signal_id = str(row.get("signal_id") or "")
        if (
            row.get("trigger_pair") != expected_pair
            or not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not re.fullmatch(r"[0-9a-f]{64}", line_digest)
            or signal_id != f"alpha001-{symbol}-{offset}-{line_digest[:12]}"
            or float(row.get("freqtrade_requested_limit_price") or 0) <= 0
        ):
            raise ValueError("execution row has invalid Freqtrade trigger identity")
        try:
            detected = datetime.fromisoformat(str(row["trigger_detected_at"]).replace("Z", "+00:00"))
            submitted = datetime.fromisoformat(str(row["submitted_at"]).replace("Z", "+00:00"))
            if detected.tzinfo is None or submitted.tzinfo is None:
                raise ValueError("timezone missing")
            delay = (submitted - detected).total_seconds()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("execution row has invalid trigger timing") from exc
        if delay < 0 or delay > float(execution_policy["maximum_trigger_to_probe_delay_seconds"]):
            raise ValueError("execution row was not probed promptly after the live order-attempt trigger")
    symbols = sorted({str(row.get("symbol") or "").upper() for row in rows})
    if not symbols or "" in symbols:
        raise ValueError("every execution row must contain a symbol")
    summary = summarize_maker_execution(
        rows,
        min_orders=args.min_orders,
        min_filled_orders=args.min_filled_orders,
        min_maker_share=args.min_maker_share,
        max_effective_fee_bps=args.max_effective_fee_bps,
        gross_edge_budget_bps=gross_edge_budget_bps,
    )
    per_symbol = {
        symbol: summarize_maker_execution(
            [row for row in rows if str(row.get("symbol") or "").upper() == symbol],
            min_orders=args.min_orders_per_symbol,
            min_filled_orders=args.min_filled_orders_per_symbol,
            min_maker_share=args.min_maker_share,
            max_effective_fee_bps=args.max_effective_fee_bps,
            gross_edge_budget_bps=gross_edge_budget_bps,
        )
        for symbol in symbols
    }
    if any(item["status"] != "PASS" for item in per_symbol.values()):
        summary["status"] = "REVIEW"
    report = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "policy": RECEIPT_POLICY,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate": "FuturesBtcRegimeRiskBudgetLong",
        "evidence_kind": args.evidence_kind,
        "order_scope": "entry_long_only",
        "required_side": "buy",
        "source_segments": source_segments,
        "current_protocol_manifest_inventory": manifest_inventory,
        "protocol_sha256": current_protocol,
        "gross_edge_budget_bps": gross_edge_budget_bps,
        "maximum_shadow_notional_usdt": execution_policy["maximum_shadow_notional_usdt"],
        "required_trigger_kind": execution_policy["required_trigger_kind"],
        "exact_freqtrade_limit_price_replay_required": execution_policy[
            "require_exact_freqtrade_limit_price_replay"
        ],
        "all_current_protocol_execution_segments_required": execution_policy[
            "all_current_protocol_execution_segments_required"
        ],
        "maximum_trigger_to_probe_delay_seconds": execution_policy[
            "maximum_trigger_to_probe_delay_seconds"
        ],
        "entry_timeout_seconds": execution_policy["entry_timeout_seconds"],
        "post_fill_markout_seconds": execution_policy["post_fill_markout_seconds"],
        "symbols": symbols,
        "per_symbol": per_symbol,
        **summary,
    }
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output_prefix.with_suffix(".md").write_text(markdown(report, input_paths), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
