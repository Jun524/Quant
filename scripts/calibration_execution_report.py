#!/usr/bin/env python3
"""Seal a descriptive report for periodic, non-promotion execution probes."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.maker_quality import summarize_maker_execution
from scripts.maker_execution_report import load_rows
from scripts.manage_alpha001_forward_dryrun import protocol_digests


SCHEMA_VERSION = 1
REPORT_POLICY = "periodic-public-l2-entry-calibration-v1"
PROMOTION_POLICY_PATH = PROJECT_ROOT / "research" / "promotion-policy.json"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _utc(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_calibration_rows(rows: list[dict], manifest: dict) -> dict:
    """Validate the fixed periodic campaign and return its observed time span."""

    if not rows:
        raise ValueError("calibration evidence contains no rows")
    symbols = [str(symbol).upper() for symbol in manifest.get("symbols") or []]
    orders_per_symbol = int(manifest.get("orders_per_symbol") or 0)
    expected_orders = orders_per_symbol * len(symbols)
    if not symbols or len(symbols) != len(set(symbols)) or orders_per_symbol <= 0:
        raise ValueError("calibration manifest has an invalid symbol allocation")
    if int(manifest.get("orders") or 0) != expected_orders or len(rows) != expected_orders:
        raise ValueError("calibration row count does not match the fixed campaign allocation")

    identities: set[str] = set()
    observed_symbols: Counter[str] = Counter()
    starts: list[datetime] = []
    ends: list[datetime] = []
    timeout_seconds = int(manifest.get("timeout_seconds") or 0)
    markout_seconds = int(manifest.get("markout_seconds") or 0)
    notional = float(manifest.get("notional_usdt") or 0)
    for row in rows:
        signal_id = str(row.get("signal_id") or "")
        symbol = str(row.get("symbol") or "").upper()
        submitted = _utc(row.get("submitted_at"), field="submitted_at")
        order_ended = _utc(
            row.get("order_observation_ended_at"), field="order_observation_ended_at"
        )
        observed_until = _utc(row.get("observed_until"), field="observed_until")
        filled = float(row.get("filled_qty") or 0)
        submitted_qty = float(row.get("submitted_qty") or 0)
        if (
            not signal_id
            or signal_id in identities
            or symbol not in symbols
            or row.get("probe_kind") != "public_queue_estimate_no_order_submitted"
            or row.get("trigger_kind") != "periodic_calibration"
            or str(row.get("side") or "").lower() != "buy"
            or float(row.get("requested_notional_usdt") or 0) != notional
            or int(row.get("timeout_seconds") or 0) != timeout_seconds
            or int(row.get("markout_seconds") or 0) != markout_seconds
            or row.get("requested_limit_price_replayed") is not False
            or row.get("order_marketable_at_probe") is not False
            or row.get("aggregate_trade_ids_continuous") is not True
            or submitted_qty <= 0
            or filled < 0
            or filled > submitted_qty + 1e-12
            or order_ended < submitted
            or observed_until < order_ended
        ):
            raise ValueError("row does not match the fixed periodic calibration policy")
        order_duration = (order_ended - submitted).total_seconds()
        if order_duration > timeout_seconds + 60:
            raise ValueError("calibration order observation exceeded its allowed horizon")
        if filled < submitted_qty - 1e-12:
            if order_duration < timeout_seconds - 30 or not row.get("mid_after_timeout"):
                raise ValueError("unfilled calibration row lacks a full timeout observation")
        elif filled > 0:
            fill_time = _utc(row.get("fill_observed_at"), field="fill_observed_at")
            markout_time = _utc(row.get("markout_observed_at"), field="markout_observed_at")
            markout_delay = (markout_time - fill_time).total_seconds()
            if (
                not row.get("mid_after_5m")
                or markout_delay < markout_seconds - 30
                or markout_delay > markout_seconds + 60
            ):
                raise ValueError("filled calibration row lacks its fixed markout observation")
        identities.add(signal_id)
        observed_symbols[symbol] += 1
        starts.append(submitted)
        ends.append(observed_until)
    if observed_symbols != Counter({symbol: orders_per_symbol for symbol in symbols}):
        raise ValueError("calibration rows are not balanced across the declared symbols")
    return {
        "first_submitted_at": min(starts).isoformat(),
        "last_observed_at": max(ends).isoformat(),
        "temporal_span_hours": (max(ends) - min(starts)).total_seconds() / 3600,
        "distinct_utc_days": len({value.date() for value in starts}),
    }


def build_report(manifest_path: Path) -> dict:
    manifest_path = manifest_path.resolve()
    root = PROJECT_ROOT.resolve()
    try:
        relative_manifest = manifest_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("calibration manifest must be inside the project") from exc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_path = (root / str(manifest.get("source_path") or "")).resolve()
    try:
        relative_source = source_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("calibration source must be inside the project") from exc
    current_protocol = protocol_digests()
    if (
        manifest.get("schema_version") != 1
        or manifest.get("status") != "COMPLETE"
        or manifest.get("evidence_kind") != "public_l2_shadow"
        or manifest.get("order_scope") != "entry_long_only"
        or manifest.get("required_side") != "buy"
        or manifest.get("evidence_purpose") != "calibration_only_not_live_promotion"
        or manifest.get("trigger_kind") != "periodic_calibration"
        or not source_path.is_file()
        or manifest.get("source_sha256") != _digest(source_path)
        or manifest.get("protocol_sha256") != current_protocol
    ):
        raise ValueError("calibration manifest is incomplete, stale, or not source-bound")
    rows = load_rows(source_path)
    timing = validate_calibration_rows(rows, manifest)

    promotion_policy = json.loads(PROMOTION_POLICY_PATH.read_text(encoding="utf-8"))
    execution_policy = promotion_policy["execution_gate"]
    budget = float(execution_policy["maximum_entry_execution_drag_bps"])
    summary = summarize_maker_execution(
        rows,
        min_orders=int(manifest["orders"]),
        min_filled_orders=int(execution_policy["minimum_filled_orders"]),
        min_maker_share=float(execution_policy["minimum_maker_share"]),
        max_effective_fee_bps=float(execution_policy["maximum_effective_fee_bps"]),
        gross_edge_budget_bps=budget,
    )
    symbols = sorted(str(symbol).upper() for symbol in manifest["symbols"])
    per_symbol = {
        symbol: summarize_maker_execution(
            [row for row in rows if str(row.get("symbol") or "").upper() == symbol],
            min_orders=int(manifest["orders_per_symbol"]),
            min_filled_orders=int(execution_policy["minimum_filled_orders_per_symbol"]),
            min_maker_share=float(execution_policy["minimum_maker_share"]),
            max_effective_fee_bps=float(execution_policy["maximum_effective_fee_bps"]),
            gross_edge_budget_bps=budget,
        )
        for symbol in symbols
    }
    metric_pass = summary["status"] == "PASS" and all(
        item["status"] == "PASS" for item in per_symbol.values()
    )
    worst_symbol_drag = max(
        float(item["conservative_all_in_execution_drag_bps"])
        for item in per_symbol.values()
        if item["conservative_all_in_execution_drag_bps"] is not None
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": REPORT_POLICY,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "CALIBRATION_PASS" if metric_pass else "CALIBRATION_REVIEW",
        "promotion_eligible": False,
        "promotion_exclusion_reason": (
            "periodic probes are not causally triggered by strategy order attempts"
        ),
        "scope": "long entry at public best bid; no real order submitted",
        "measured_scopes": ["long_entry"],
        "unmeasured_scopes": ["long exit", "short entry", "short exit", "protective stop"],
        "source": {
            "manifest_path": str(relative_manifest),
            "manifest_sha256": _digest(manifest_path),
            "raw_path": str(relative_source),
            "raw_sha256": _digest(source_path),
        },
        "protocol_sha256": current_protocol,
        "timing": timing,
        "temporal_generalization": "INSUFFICIENT_SINGLE_WINDOW",
        "cost_reference": {
            "includes_fee": True,
            "aggregate_observed_all_in_drag_bps": summary[
                "conservative_all_in_execution_drag_bps"
            ],
            "worst_symbol_observed_all_in_drag_bps": worst_symbol_drag,
            "descriptive_research_stress_floor_bps": float(ceil(worst_symbol_drag)),
            "do_not_add_trading_fee_twice": True,
            "automatic_cost_model_mutation": False,
        },
        "per_symbol": per_symbol,
        "summary": summary,
        "limitations": [
            "one short collection window cannot represent other volatility or liquidity regimes",
            "public aggregate trades estimate queue fills but are not exchange acknowledgements",
            "the result cannot validate alpha profitability or any unmeasured order path",
        ],
    }


def markdown(report: dict) -> str:
    summary = report["summary"]
    timing = report["timing"]
    costs = report["cost_reference"]
    rows = "\n".join(
        f"| {symbol} | {item['orders']} | {item['orders_with_fills']} | "
        f"{item['fill_rate_by_qty']:.2%} | {item['conservative_all_in_execution_drag_bps']:.3f} |"
        for symbol, item in report["per_symbol"].items()
    )
    return f"""# Periodic execution calibration

- Status: **{report['status']}**
- Promotion eligible: **NO**
- Orders / fills: {summary['orders']} / {summary['orders_with_fills']}
- Quantity fill rate: {summary['fill_rate_by_qty']:.2%}
- Maker share: {summary['maker_share_by_filled_qty']:.2%}
- Aggregate conservative entry drag: {costs['aggregate_observed_all_in_drag_bps']:.3f} bps
- Worst-symbol conservative entry drag: {costs['worst_symbol_observed_all_in_drag_bps']:.3f} bps
- Descriptive stress floor: {costs['descriptive_research_stress_floor_bps']:.1f} bps (fee included)
- Observation span: {timing['temporal_span_hours']:.3f} hours across {timing['distinct_utc_days']} UTC day

| Symbol | Orders | Fills | Qty fill rate | All-in drag (bps) |
|---|---:|---:|---:|---:|
{rows}

This is a source- and protocol-bound calibration receipt, not a live-promotion
receipt. It covers only long entry at the public best bid. The short window
cannot establish performance across market regimes, and its fee-inclusive drag
must not be added on top of the same trading fee a second time.
"""


def default_output_prefix(manifest_path: Path) -> Path:
    """Keep each sealed campaign receipt separate from frozen research inputs.

    A generic ``reports/execution-calibration.json`` can be explicitly bound
    into a preregistration.  Re-running a later, unrelated calibration must
    not overwrite that frozen evidence.  Callers may still choose an explicit
    path when creating a new, deliberately versioned research contract.
    """
    name = manifest_path.name
    suffix = ".manifest.json"
    campaign = name[: -len(suffix)] if name.endswith(suffix) else manifest_path.stem
    return Path("reports") / f"execution-calibration-{campaign}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        help="Optional versioned output prefix (default derives one from the manifest).",
    )
    args = parser.parse_args()
    report = build_report(args.manifest)
    output_prefix = args.output_prefix or default_output_prefix(args.manifest)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    output_prefix.with_suffix(".json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    output_prefix.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
