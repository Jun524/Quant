#!/usr/bin/env python3
"""Fail-closed configuration and credential checks before a live review.

This tool deliberately never connects to Binance and never prints secrets.
It validates only structural safeguards; it cannot establish profitability or
override the repository-wide live-order authorization boundary.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any


PLACEHOLDER_FRAGMENTS = ("change-me", "replace-with", "overridden", "example", "your-")
REQUIRED_ENV = {
    "BINANCE_FUTURES_API_KEY": 16,
    "BINANCE_FUTURES_API_SECRET": 16,
    "FREQTRADE_API_USERNAME": 3,
    "FREQTRADE_API_PASSWORD": 24,
    "FREQTRADE_JWT_SECRET": 32,
    "FREQTRADE_WS_TOKEN": 32,
}
BIAS_AUDIT_SCHEMA_VERSION = 1
BIAS_AUDIT_POLICY = "freqtrade-current-protocol-bias-audit-v1"
LIVE_STRATEGY = "FuturesBtcRegimeRiskBudgetLong"
BIAS_AUDIT_TIMERANGE = "20220101-20260901"
BIAS_AUDIT_FREQTRADE_VERSION = "2026.8"
EXECUTION_AUDIT_SCHEMA_VERSION = 6
EXECUTION_AUDIT_POLICY = "alpha001-signal-triggered-entry-execution-audit-v5"
MINIMUM_LOOKAHEAD_SIGNALS = 20
LIVE_STARTUP_CANDLES = 1500
MAXIMUM_RECURSIVE_VARIANCE_PCT = 0.10
MAXIMUM_EXECUTION_AUDIT_AGE_SECONDS = 30 * 86_400
PROJECT_ROOT = Path(__file__).resolve().parents[1]
RISK_POLICY_PATH = PROJECT_ROOT / "research" / "risk-policy.json"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sealed_artifact_failures(
    result: dict[str, Any],
    *,
    label: str,
    project_root: Path,
) -> list[str]:
    relative = result.get("artifact_path")
    expected = result.get("artifact_sha256")
    if not isinstance(relative, str) or not relative:
        return [f"{label} evidence path is missing"]
    if not _is_sha256(expected):
        return [f"{label} evidence SHA-256 is missing or invalid"]
    root = project_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return [f"{label} evidence path escapes the project root"]
    if not path.is_file():
        return [f"{label} evidence artifact is missing"]
    digest = sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        return [f"{label} evidence artifact SHA-256 does not match"]
    return []


def _lookahead_artifact_fields(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        matching = [row for row in rows if row.get("strategy") == LIVE_STRATEGY]
        if len(matching) != 1:
            return None
        row = matching[0]
        raw_bias = str(row.get("has_bias", "")).strip().lower()
        if raw_bias not in {"true", "false"}:
            return None
        return {
            "has_bias": raw_bias == "true",
            "total_signals": int(row["total_signals"]),
            "biased_entry_signals": int(row["biased_entry_signals"]),
            "biased_exit_signals": int(row["biased_exit_signals"]),
        }
    except (OSError, UnicodeError, csv.Error, KeyError, TypeError, ValueError):
        return None


def _recursive_artifact_fields(path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    values: dict[str, float] = {}
    for line in content.splitlines():
        if not line.startswith("│"):
            continue
        cells = [cell.strip() for cell in line.strip("│").split("│")]
        if len(cells) < 2 or cells[0].lower() == "indicators":
            continue
        raw = cells[1]
        if raw == "-":
            value = 0.0
        elif raw.endswith("%"):
            try:
                value = abs(float(raw[:-1]))
            except ValueError:
                return None
        else:
            continue
        if not math.isfinite(value):
            return None
        values[cells[0]] = value
    if not values:
        return None
    return {
        "indicator_lookahead_bias": "No lookahead bias on indicators found." not in content,
        "indicator_abs_variance_pct": values,
        "maximum_abs_variance_pct": max(values.values()),
    }


def bias_audit_receipt_failures(
    receipt: dict[str, Any] | None,
    *,
    expected_protocol: dict[str, str],
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """Validate a fail-closed Freqtrade lookahead/recursive audit receipt.

    The receipt is intentionally bound to the complete Alpha #1 forward
    protocol, not merely the strategy filename.  A strategy/config/report
    change therefore invalidates old bias evidence automatically.
    """
    if not isinstance(receipt, dict):
        return ["current-protocol bias audit receipt is missing"]

    failures: list[str] = []
    if receipt.get("schema_version") != BIAS_AUDIT_SCHEMA_VERSION:
        failures.append("bias audit schema is unsupported")
    if receipt.get("policy") != BIAS_AUDIT_POLICY:
        failures.append("bias audit policy is unexpected")
    if receipt.get("candidate") != LIVE_STRATEGY:
        failures.append("bias audit candidate does not match the live strategy")
    if receipt.get("protocol_sha256") != expected_protocol:
        failures.append("bias audit is not bound to the current research protocol")
    if receipt.get("status") != "PASS":
        failures.append("bias audit status is not PASS")
    if receipt.get("failures") != []:
        failures.append("bias audit records failures")
    if receipt.get("freqtrade_version") != BIAS_AUDIT_FREQTRADE_VERSION:
        failures.append("bias audit Freqtrade version does not match the live runtime")
    if receipt.get("timerange") != BIAS_AUDIT_TIMERANGE:
        failures.append("bias audit timerange does not match the locked audit range")

    lookahead = receipt.get("lookahead")
    if not isinstance(lookahead, dict):
        failures.append("bias audit has no lookahead result")
    else:
        if lookahead.get("passed") is not True or lookahead.get("has_bias") is not False:
            failures.append("lookahead analysis did not pass without bias")
        total_signals = lookahead.get("total_signals")
        if (
            not isinstance(total_signals, int)
            or isinstance(total_signals, bool)
            or total_signals < MINIMUM_LOOKAHEAD_SIGNALS
        ):
            failures.append(
                f"lookahead analysis verified fewer than {MINIMUM_LOOKAHEAD_SIGNALS} signals"
            )
        if (
            not isinstance(lookahead.get("biased_entry_signals"), int)
            or isinstance(lookahead.get("biased_entry_signals"), bool)
            or lookahead.get("biased_entry_signals") != 0
        ):
            failures.append("lookahead analysis found biased entry signals")
        if (
            not isinstance(lookahead.get("biased_exit_signals"), int)
            or isinstance(lookahead.get("biased_exit_signals"), bool)
            or lookahead.get("biased_exit_signals") != 0
        ):
            failures.append("lookahead analysis found biased exit signals")
        artifact_failures = _sealed_artifact_failures(
            lookahead, label="lookahead", project_root=project_root
        )
        failures.extend(artifact_failures)
        if not artifact_failures:
            fields = _lookahead_artifact_fields(project_root / lookahead["artifact_path"])
            if fields is None or any(lookahead.get(name) != value for name, value in fields.items()):
                failures.append("lookahead receipt fields do not match the sealed CSV")

    recursive = receipt.get("recursive")
    if not isinstance(recursive, dict):
        failures.append("bias audit has no recursive result")
    else:
        if recursive.get("passed") is not True:
            failures.append("recursive analysis did not pass")
        startup_candles = recursive.get("startup_candle_count")
        if (
            not isinstance(startup_candles, int)
            or isinstance(startup_candles, bool)
            or startup_candles != LIVE_STARTUP_CANDLES
        ):
            failures.append("recursive analysis does not test the live startup candle count")
        maximum_variance = recursive.get("maximum_abs_variance_pct")
        if (
            not isinstance(maximum_variance, (int, float))
            or isinstance(maximum_variance, bool)
            or not math.isfinite(maximum_variance)
            or maximum_variance < 0
            or maximum_variance > MAXIMUM_RECURSIVE_VARIANCE_PCT
        ):
            failures.append(
                "recursive analysis exceeds the maximum 0.10% indicator variance"
            )
        artifact_failures = _sealed_artifact_failures(
            recursive, label="recursive", project_root=project_root
        )
        failures.extend(artifact_failures)
        if not artifact_failures:
            fields = _recursive_artifact_fields(project_root / recursive["artifact_path"])
            if fields is None or any(recursive.get(name) != value for name, value in fields.items()):
                failures.append("recursive receipt fields do not match the sealed output")
    return failures


def research_promotion_failures(
    report: dict,
    state: dict,
    *,
    current_protocol: dict[str, str],
    bias_audit: dict[str, Any] | None,
    project_root: Path = PROJECT_ROOT,
    database: dict,
    forward_running: bool,
    now: datetime | None = None,
    maximum_evidence_age_seconds: int = 86_400,
) -> list[str]:
    """Return every reason the research evidence cannot authorize live mode."""
    now = now or datetime.now(timezone.utc)
    failures: list[str] = []
    failures.extend(
        bias_audit_receipt_failures(
            bias_audit,
            expected_protocol=current_protocol,
            project_root=project_root,
        )
    )
    if report.get("candidate") != LIVE_STRATEGY:
        failures.append("research candidate does not match the live strategy")
    if report.get("status") != "PASS":
        failures.append("research report status is not PASS")
    if report.get("protocol_current") is not True:
        failures.append("research report protocol is not current")
    if (report.get("data_integrity") or {}).get("status") != "PASS":
        failures.append("research data integrity is not PASS")
    gate = report.get("gate") or {}
    checks = gate.get("checks") or []
    if gate.get("status") != "PASS" or not checks or any(item.get("passed") is not True for item in checks):
        failures.append("research promotion gate is not fully PASS")
    if int(((report.get("mtm") or {}).get("monthly") or {}).get("complete_months") or 0) < 12:
        failures.append("research evidence has fewer than 12 complete months")
    if int((report.get("trades") or {}).get("natural_closed_trades") or 0) < 100:
        failures.append("research evidence has fewer than 100 natural closed trades")
    if int((report.get("trades") or {}).get("open_trades") or 0) != 0:
        failures.append("research report contains open trades")
    if state.get("protocol_sha256") != current_protocol:
        failures.append("launch protocol differs from the current research protocol")
    if report.get("database") != state.get("database"):
        failures.append("research report is not bound to the current protocol database")
    if int(database.get("open_trades") or 0) != 0:
        failures.append("current protocol database contains open trades")
    if forward_running:
        failures.append("forward dry-run must be stopped before live review")
    last_observed = (report.get("mtm") or {}).get("last_observed_at")
    try:
        observed_at = datetime.fromisoformat(str(last_observed).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > maximum_evidence_age_seconds:
            failures.append("research MTM evidence is stale or future-dated")
    except (TypeError, ValueError):
        failures.append("research MTM evidence has no valid latest timestamp")
    return failures


def execution_audit_receipt_failures(
    receipt: dict[str, Any] | None,
    *,
    expected_protocol: dict[str, str],
    expected_symbols: list[str] | None = None,
    project_root: Path = PROJECT_ROOT,
    now: datetime | None = None,
    expected_execution_budget_bps: float = 10.0,
    maximum_notional_usdt: float = 50.0,
    expected_timeout_seconds: int = 300,
    expected_markout_seconds: int = 300,
    minimum_orders_per_symbol: int = 20,
    minimum_filled_orders_per_symbol: int = 6,
    minimum_maker_share: float = 0.70,
    maximum_effective_fee_bps: float = 2.8,
    minimum_orders: int = 100,
    minimum_filled_orders: int = 30,
    required_trigger_kind: str = "alpha001_live_order_attempt",
    maximum_trigger_delay_seconds: float = 10.0,
    require_exact_limit_price_replay: bool = True,
) -> list[str]:
    """Verify current-protocol L2 execution evidence and its sealed raw rows."""
    now = now or datetime.now(timezone.utc)
    failures: list[str] = []
    if not isinstance(receipt, dict):
        return ["execution audit receipt is missing or invalid"]
    if receipt.get("schema_version") != EXECUTION_AUDIT_SCHEMA_VERSION:
        failures.append("execution audit schema version is not current")
    if receipt.get("policy") != EXECUTION_AUDIT_POLICY:
        failures.append("execution audit policy is not current")
    if receipt.get("candidate") != LIVE_STRATEGY:
        failures.append("execution audit candidate does not match the live strategy")
    if receipt.get("evidence_kind") != "public_l2_shadow":
        failures.append("execution audit is not conservative public L2 shadow evidence")
    if receipt.get("order_scope") != "entry_long_only" or receipt.get("required_side") != "buy":
        failures.append("execution audit is not scoped to Alpha #1 long entries")
    if receipt.get("protocol_sha256") != expected_protocol:
        failures.append("execution audit is not bound to the current research protocol")
    if receipt.get("status") != "PASS":
        failures.append("execution audit status is not PASS")
    if (
        receipt.get("maximum_shadow_notional_usdt") != maximum_notional_usdt
        or receipt.get("required_trigger_kind") != required_trigger_kind
        or receipt.get("maximum_trigger_to_probe_delay_seconds") != maximum_trigger_delay_seconds
        or receipt.get("entry_timeout_seconds") != expected_timeout_seconds
        or receipt.get("post_fill_markout_seconds") != expected_markout_seconds
        or receipt.get("exact_freqtrade_limit_price_replay_required")
        is not require_exact_limit_price_replay
        or receipt.get("all_current_protocol_execution_segments_required") is not True
    ):
        failures.append("execution audit size or observation horizons do not match policy")

    checks = receipt.get("checks") or []
    required_checks = {
        "minimum_order_sample",
        "minimum_filled_order_sample",
        "maker_share",
        "effective_fee_bps",
        "filled_markout_coverage",
        "unfilled_opportunity_coverage",
        "all_in_execution_drag_within_gross_edge",
    }
    checks_by_name = {item.get("name"): item for item in checks if isinstance(item, dict)}
    if not required_checks.issubset(checks_by_name):
        failures.append("execution audit is missing required checks")
    elif any(checks_by_name[name].get("passed") is not True for name in required_checks):
        failures.append("execution audit required checks are not fully PASS")
    if int(receipt.get("orders") or 0) < 100:
        failures.append("execution audit has fewer than 100 submitted orders")
    if int(receipt.get("orders_with_fills") or 0) < 30:
        failures.append("execution audit has fewer than 30 filled orders")
    observed_symbols = receipt.get("symbols")
    expected_symbol_set = set(expected_symbols or [])
    if not isinstance(observed_symbols, list) or set(observed_symbols) != expected_symbol_set:
        failures.append("execution audit symbols do not exactly match the live whitelist")
    per_symbol = receipt.get("per_symbol") or {}
    per_symbol_invalid = set(per_symbol) != expected_symbol_set
    for symbol in expected_symbol_set:
        item = per_symbol.get(symbol) or {}
        fee_bps = item.get("effective_fee_bps")
        drag_bps = item.get("conservative_all_in_execution_drag_bps")
        if (
            item.get("status") != "PASS"
            or int(item.get("orders") or 0) < minimum_orders_per_symbol
            or int(item.get("orders_with_fills") or 0) < minimum_filled_orders_per_symbol
            or float(item.get("maker_share_by_filled_qty") or 0.0) < minimum_maker_share
            or not isinstance(fee_bps, (int, float))
            or isinstance(fee_bps, bool)
            or fee_bps > maximum_effective_fee_bps
            or float(item.get("filled_markout_coverage_ratio") or 0.0) != 1.0
            or float(item.get("unfilled_opportunity_coverage_ratio") or 0.0) != 1.0
            or not isinstance(drag_bps, (int, float))
            or isinstance(drag_bps, bool)
            or drag_bps >= expected_execution_budget_bps
        ):
            per_symbol_invalid = True
    if expected_symbol_set and per_symbol_invalid:
        failures.append("execution audit per-symbol gates are not fully PASS")
    if float(receipt.get("filled_markout_coverage_ratio") or 0.0) != 1.0:
        failures.append("execution audit lacks complete five-minute markout coverage")
    if float(receipt.get("unfilled_opportunity_coverage_ratio") or 0.0) != 1.0:
        failures.append("execution audit lacks complete unfilled-opportunity coverage")
    gross_edge = receipt.get("gross_edge_budget_bps")
    all_in_drag = receipt.get("conservative_all_in_execution_drag_bps")
    if (
        not isinstance(gross_edge, (int, float))
        or isinstance(gross_edge, bool)
        or not math.isfinite(gross_edge)
        or gross_edge != expected_execution_budget_bps
        or not isinstance(all_in_drag, (int, float))
        or isinstance(all_in_drag, bool)
        or not math.isfinite(all_in_drag)
        or all_in_drag >= gross_edge
    ):
        failures.append("execution audit all-in drag does not fit inside gross edge")

    generated_at = receipt.get("generated_at_utc")
    try:
        generated = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - generated.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > MAXIMUM_EXECUTION_AUDIT_AGE_SECONDS:
            failures.append("execution audit receipt is stale or future-dated")
    except (TypeError, ValueError):
        failures.append("execution audit receipt has no valid timestamp")

    root = project_root.resolve()
    segments = receipt.get("source_segments")
    verified_segments: list[tuple[Path, str]] = []
    if not isinstance(segments, list) or not segments:
        failures.append("execution audit source segment inventory is missing")
        segments = []
    for segment in segments:
        if not isinstance(segment, dict):
            failures.append("execution audit source segment is invalid")
            continue
        relative = segment.get("source_path")
        expected_digest = segment.get("source_sha256")
        manifest_relative = segment.get("collection_manifest_path")
        manifest_digest = segment.get("collection_manifest_sha256")
        expected_orders = segment.get("orders")
        if (
            not isinstance(relative, str)
            or not relative
            or not _is_sha256(expected_digest)
            or not isinstance(manifest_relative, str)
            or not manifest_relative
            or not _is_sha256(manifest_digest)
            or not isinstance(expected_orders, int)
            or isinstance(expected_orders, bool)
            or expected_orders < 0
        ):
            failures.append("execution audit source segment paths, hashes, or count are invalid")
            continue
        source = (root / relative).resolve()
        manifest_path = (root / manifest_relative).resolve()
        try:
            source.relative_to(root)
            manifest_path.relative_to(root)
        except ValueError:
            failures.append("execution audit source segment escapes the project root")
            continue
        if not source.is_file():
            failures.append("execution audit raw source is missing")
            continue
        if sha256(source.read_bytes()).hexdigest() != expected_digest:
            failures.append("execution audit raw source SHA-256 does not match")
            continue
        if not manifest_path.is_file():
            failures.append("execution audit collection manifest is missing")
            continue
        if sha256(manifest_path.read_bytes()).hexdigest() != manifest_digest:
            failures.append("execution audit collection manifest SHA-256 does not match")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("schema_version") != 1
                or manifest.get("status") != "SEALED_ON_STOP"
                or manifest.get("protocol_sha256") != expected_protocol
                or manifest.get("source_path") != relative
                or manifest.get("source_sha256") != expected_digest
                or int(manifest.get("orders") or 0) != expected_orders
                or manifest.get("evidence_purpose") != "live_promotion_candidate"
                or manifest.get("order_scope") != "entry_long_only"
                or manifest.get("required_side") != "buy"
                or manifest.get("trigger_kind") != required_trigger_kind
                or manifest.get("maximum_notional_usdt") != maximum_notional_usdt
                or manifest.get("timeout_seconds") != expected_timeout_seconds
                or manifest.get("markout_seconds") != expected_markout_seconds
            ):
                failures.append("execution audit collection manifest is not current and complete")
                continue
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            failures.append("execution audit collection manifest cannot be parsed")
            continue
        verified_segments.append((source, manifest_relative))
    segment_paths = [str(item.get("source_path") or "") for item in segments if isinstance(item, dict)]
    manifest_paths = [
        str(item.get("collection_manifest_path") or "")
        for item in segments
        if isinstance(item, dict)
    ]
    if len(segment_paths) != len(set(segment_paths)) or len(manifest_paths) != len(set(manifest_paths)):
        failures.append("execution audit contains duplicate source segments")
    if len(verified_segments) == len(segments) and verified_segments:
        try:
            from quant.execution.maker_quality import summarize_maker_execution
            from scripts.maker_execution_report import (
                current_protocol_entry_manifest_inventory,
                load_rows,
                validate_signal_triggered_rows,
            )

            rows: list[dict] = []
            for (source, _manifest_relative), segment in zip(verified_segments, segments):
                segment_rows = load_rows(source)
                if len(segment_rows) != int(segment["orders"]):
                    raise ValueError("source segment row count does not match receipt")
                rows.extend(segment_rows)
            signal_ids = [str(row.get("signal_id") or "") for row in rows]
            if len(signal_ids) != len(set(signal_ids)):
                raise ValueError("duplicate signal ids across execution segments")
            inventory = current_protocol_entry_manifest_inventory(project_root, expected_protocol)
            if (
                {item.get("path") for item in inventory} != set(manifest_paths)
                or any(item.get("status") != "SEALED_ON_STOP" for item in inventory)
                or receipt.get("current_protocol_manifest_inventory") != inventory
            ):
                failures.append(
                    "execution audit does not cover every sealed current-protocol entry manifest"
                )
            validate_signal_triggered_rows(
                rows,
                {
                    "required_trigger_kind": required_trigger_kind,
                    "maximum_shadow_notional_usdt": maximum_notional_usdt,
                    "entry_timeout_seconds": expected_timeout_seconds,
                    "post_fill_markout_seconds": expected_markout_seconds,
                    "maximum_trigger_to_probe_delay_seconds": maximum_trigger_delay_seconds,
                },
            )
            for row in rows:
                symbol = str(row.get("symbol") or "").upper()
                expected_pair = f"{symbol[:-4]}/USDT:USDT" if symbol.endswith("USDT") else ""
                line_digest = str(row.get("trigger_line_sha256") or "")
                offset = row.get("trigger_log_offset")
                signal_id = str(row.get("signal_id") or "")
                if (
                    row.get("probe_kind") != "public_queue_estimate_no_order_submitted"
                    or row.get("trigger_kind") != required_trigger_kind
                    or row.get("trigger_source") != "local_freqtrade_initial_entry_log"
                    or str(row.get("side") or "").lower() != "buy"
                    or float(row.get("requested_notional_usdt") or 0) <= 0
                    or float(row.get("requested_notional_usdt") or 0) > maximum_notional_usdt
                    or float(row.get("requested_notional_usdt") or 0)
                    != float(row.get("requested_stake_usdt") or 0)
                    or int(row.get("timeout_seconds") or 0) != expected_timeout_seconds
                    or int(row.get("markout_seconds") or 0) != expected_markout_seconds
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
                ):
                    raise ValueError("row policy mismatch")
                detected = datetime.fromisoformat(
                    str(row.get("trigger_detected_at")).replace("Z", "+00:00")
                )
                submitted = datetime.fromisoformat(str(row.get("submitted_at")).replace("Z", "+00:00"))
                if detected.tzinfo is None or submitted.tzinfo is None:
                    raise ValueError("row trigger timestamp timezone missing")
                delay = (submitted - detected).total_seconds()
                if delay < 0 or delay > maximum_trigger_delay_seconds:
                    raise ValueError("row trigger delay mismatch")
            recomputed = summarize_maker_execution(
                rows,
                min_orders=minimum_orders,
                min_filled_orders=minimum_filled_orders,
                min_maker_share=minimum_maker_share,
                max_effective_fee_bps=maximum_effective_fee_bps,
                gross_edge_budget_bps=expected_execution_budget_bps,
            )
            recomputed_per_symbol = {
                symbol: summarize_maker_execution(
                    [row for row in rows if str(row.get("symbol") or "").upper() == symbol],
                    min_orders=minimum_orders_per_symbol,
                    min_filled_orders=minimum_filled_orders_per_symbol,
                    min_maker_share=minimum_maker_share,
                    max_effective_fee_bps=maximum_effective_fee_bps,
                    gross_edge_budget_bps=expected_execution_budget_bps,
                )
                for symbol in expected_symbol_set
            }
            if any(receipt.get(key) != value for key, value in recomputed.items()):
                failures.append("execution audit summary does not match recomputed raw rows")
            if receipt.get("per_symbol") != recomputed_per_symbol:
                failures.append("execution audit per-symbol summaries do not match recomputed raw rows")
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            failures.append(f"execution audit raw rows cannot be recomputed: {type(error).__name__}: {error}")
    return failures


def compose_api_binding_failures(documents: list[str], *, service: str = "freqtrade-futures") -> list[str]:
    """Reject host-exposed API mappings for the live futures service."""
    failures: list[str] = []
    for document_index, document in enumerate(documents, start=1):
        in_service = False
        in_ports = False
        for raw_line in document.splitlines():
            stripped = raw_line.strip()
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent == 2 and stripped.endswith(":"):
                in_service = stripped == f"{service}:"
                in_ports = False
                continue
            if not in_service:
                continue
            if indent == 4 and stripped == "ports:":
                in_ports = True
                continue
            if indent <= 4 and stripped and not stripped.startswith("#"):
                in_ports = False
            if not in_ports or indent < 6 or not stripped.startswith("-"):
                continue
            mapping = stripped[1:].strip().strip('"').strip("'")
            if not mapping.endswith(":8080"):
                continue
            if not (mapping.startswith("127.0.0.1:") or mapping.startswith("[::1]:")):
                failures.append(
                    f"compose document {document_index} exposes {service} API outside loopback: {mapping}"
                )
    return failures


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"FAIL: cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise SystemExit(f"FAIL: {path} must contain a JSON object")
    return value


def is_placeholder(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or any(fragment in normalized for fragment in PLACEHOLDER_FRAGMENTS)


def load_dotenv(path: Path) -> dict[str, str]:
    """Read the simple KEY=VALUE format used by the provided example file."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as error:
        raise SystemExit(f"FAIL: cannot read {path}: {error}") from error
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"FAIL: invalid dotenv line in {path}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def live_order_authorization_failure(
    policy_path: Path = RISK_POLICY_PATH,
) -> str | None:
    """Return a fail-closed reason when current policy forbids live review.

    A structural preflight must never become an implicit authorization path.
    The active capital-preservation policy deliberately remains CLOSED until a
    separately versioned authorization mechanism exists.
    """
    try:
        policy = load_config(policy_path)
    except SystemExit:
        return "Risk Policy cannot be read; live review is forbidden"
    authorization = policy.get("live_order_authorization")
    if authorization == "CLOSED":
        return "Risk Policy live order authorization is CLOSED"
    return "Risk Policy has no supported explicit live-order authorization"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--research-report", type=Path)
    parser.add_argument("--bias-audit", type=Path)
    parser.add_argument("--execution-audit", type=Path)
    parser.add_argument("--permission-audit", type=Path)
    parser.add_argument("--account-audit", type=Path)
    parser.add_argument("--exchange-filter-audit", type=Path)
    parser.add_argument("--compose-file", type=Path, action="append", default=[])
    parser.add_argument(
        "--forward-state",
        type=Path,
        default=PROJECT_ROOT / "research" / "alpha001-forward-dryrun-state.json",
    )
    parser.add_argument("--require-live", action="store_true")
    parser.add_argument("--require-env", action="store_true")
    parser.add_argument(
        "--allow-running-dry-run",
        action="store_true",
        help="Allow initial_state=running only when the merged configuration is provably dry-run.",
    )
    args = parser.parse_args()

    config = load_config(args.base)
    if args.overlay:
        config = deep_merge(config, load_config(args.overlay))

    failures: list[str] = []
    warnings: list[str] = []
    checks = {
        "strategy": config.get("strategy") == "FuturesBtcRegimeRiskBudgetLong",
        "trading_mode": config.get("trading_mode") == "futures",
        "margin_mode": config.get("margin_mode") == "isolated",
        "max_open_trades": isinstance(config.get("max_open_trades"), int)
        and config["max_open_trades"] <= 2,
        "liquidation_buffer": float(config.get("liquidation_buffer", 0)) >= 0.10,
        "force_entry_disabled": config.get("force_entry_enable") is False,
        "maker_seeking_long_entry": (config.get("entry_pricing") or {}).get("price_side") == "same",
        "immediate_long_exit_pricing": (config.get("exit_pricing") or {}).get("price_side") == "other",
        "safe_boot_state": config.get("initial_state") == "stopped"
        or (
            args.allow_running_dry_run
            and not args.require_live
            and config.get("dry_run") is True
            and config.get("initial_state") == "running"
        ),
    }
    for name, passed in checks.items():
        if not passed:
            failures.append(name)

    if args.require_live and config.get("dry_run") is not False:
        failures.append("dry_run must be false in the live overlay")
    if not args.require_live and config.get("dry_run") is not True:
        warnings.append("configuration is not dry-run; use --require-live only for an intentional live review")

    api = config.get("api_server", {})
    if api.get("enable_openapi") is not False:
        failures.append("api_server.enable_openapi must be false")
    if api.get("CORS_origins") not in ([], None):
        failures.append("api_server.CORS_origins must be empty")
    if args.allow_running_dry_run and api.get("enabled") is True and api.get("listen_ip_address") not in {
        "127.0.0.1",
        "::1",
        "localhost",
    }:
        failures.append("automated dry-run API must bind only to loopback")

    env_values = dict(os.environ)
    if args.env_file:
        env_values.update(load_dotenv(args.env_file))
    if args.require_env:
        for name, minimum_length in REQUIRED_ENV.items():
            value = env_values.get(name, "")
            if len(value) < minimum_length or is_placeholder(value):
                failures.append(f"environment variable {name} is missing, placeholder-like, or too short")

    if args.require_live:
        authorization_failure = live_order_authorization_failure()
        if authorization_failure:
            failures.append(authorization_failure)
        if args.permission_audit is None:
            failures.append("--permission-audit is required for live review")
        else:
            try:
                from scripts.audit_binance_api_permissions import permission_audit_receipt_failures

                permission_receipt = load_config(args.permission_audit)
                failures.extend(
                    permission_audit_receipt_failures(
                        permission_receipt,
                        expected_api_key=env_values.get("BINANCE_FUTURES_API_KEY", ""),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"API permission audit cannot be verified: {type(error).__name__}: {error}")
        if args.account_audit is None:
            failures.append("--account-audit is required for live review")
        else:
            try:
                from scripts.audit_binance_futures_account import (
                    account_audit_receipt_failures,
                    config_symbols,
                )

                account_receipt = load_config(args.account_audit)
                failures.extend(
                    account_audit_receipt_failures(
                        account_receipt,
                        expected_api_key=env_values.get("BINANCE_FUTURES_API_KEY", ""),
                        expected_symbols=config_symbols(config),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"futures account audit cannot be verified: {type(error).__name__}: {error}")
        if args.exchange_filter_audit is None:
            failures.append("--exchange-filter-audit is required for live review")
        else:
            try:
                from scripts.audit_binance_futures_account import config_symbols
                from scripts.audit_futures_order_feasibility import receipt_failures

                policy = load_config(PROJECT_ROOT / "research" / "promotion-policy.json")
                failures.extend(
                    receipt_failures(
                        load_config(args.exchange_filter_audit),
                        expected_symbols=config_symbols(config),
                        expected_desired_notional_usdt=config.get("stake_amount"),
                        expected_maximum_notional_usdt=policy["execution_gate"]["maximum_shadow_notional_usdt"],
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"exchange filter feasibility cannot be verified: {type(error).__name__}: {error}")
        if not args.compose_file:
            failures.append("at least one --compose-file is required for live API exposure review")
        else:
            try:
                compose_documents = [path.read_text(encoding="utf-8") for path in args.compose_file]
                failures.extend(compose_api_binding_failures(compose_documents))
            except OSError as error:
                failures.append(f"compose exposure cannot be verified: {type(error).__name__}: {error}")
        if args.execution_audit is None:
            failures.append("--execution-audit is required for live review")
        else:
            try:
                from scripts.manage_alpha001_forward_dryrun import protocol_digests
                from scripts.audit_binance_futures_account import config_symbols

                execution_policy = load_config(
                    PROJECT_ROOT / "research" / "promotion-policy.json"
                )["execution_gate"]
                failures.extend(
                    execution_audit_receipt_failures(
                        load_config(args.execution_audit),
                        expected_protocol=protocol_digests(),
                        expected_symbols=config_symbols(config),
                        expected_execution_budget_bps=float(execution_policy["maximum_entry_execution_drag_bps"]),
                        maximum_notional_usdt=float(execution_policy["maximum_shadow_notional_usdt"]),
                        expected_timeout_seconds=int(execution_policy["entry_timeout_seconds"]),
                        expected_markout_seconds=int(execution_policy["post_fill_markout_seconds"]),
                        minimum_orders_per_symbol=int(execution_policy["minimum_orders_per_symbol"]),
                        minimum_filled_orders_per_symbol=int(execution_policy["minimum_filled_orders_per_symbol"]),
                        minimum_maker_share=float(execution_policy["minimum_maker_share"]),
                        maximum_effective_fee_bps=float(execution_policy["maximum_effective_fee_bps"]),
                        minimum_orders=int(execution_policy["minimum_orders"]),
                        minimum_filled_orders=int(execution_policy["minimum_filled_orders"]),
                        required_trigger_kind=str(execution_policy["required_trigger_kind"]),
                        maximum_trigger_delay_seconds=float(
                            execution_policy["maximum_trigger_to_probe_delay_seconds"]
                        ),
                        require_exact_limit_price_replay=bool(
                            execution_policy["require_exact_freqtrade_limit_price_replay"]
                        ),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"execution audit cannot be verified: {type(error).__name__}: {error}")
        if args.research_report is None:
            failures.append("--research-report is required for live review")
        elif args.bias_audit is None:
            failures.append("--bias-audit is required for live review")
        else:
            try:
                report = load_config(args.research_report)
                bias_audit = load_config(args.bias_audit)
                state = load_config(args.forward_state)
                from scripts.manage_alpha001_forward_dryrun import (
                    database_summary,
                    process_running,
                    protocol_digests,
                )

                database_value = state.get("database")
                if not isinstance(database_value, str):
                    raise ValueError("forward state has no database path")
                database_path = PROJECT_ROOT / database_value
                failures.extend(
                    research_promotion_failures(
                        report,
                        state,
                        current_protocol=protocol_digests(),
                        bias_audit=bias_audit,
                        database=database_summary(database_path),
                        forward_running=process_running(state.get("pid")),
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                failures.append(f"research promotion evidence cannot be verified: {type(error).__name__}: {error}")

    mode = "LIVE" if args.require_live else "DRY-RUN"
    print(f"Preflight mode: {mode}")
    print("Validated: strategy, futures, isolated margin, <=2 positions, liquidation buffer, no force-entry, safe boot state")
    if warnings:
        for warning in warnings:
            print(f"WARN: {warning}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        sys.exit(2)
    print("PASS: structural safeguards are present. This is not a profitability or outage guarantee.")


if __name__ == "__main__":
    main()
