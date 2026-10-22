"""Source-replayed execution-path evidence for portfolio promotion."""

from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from typing import Any

from quant.execution.shadow_maker import estimate_shadow_fill


PORTFOLIO_EXECUTION_PROTOCOL_FILES = (
    "quant/research/portfolio_execution.py",
    "scripts/portfolio_execution_audit.py",
    "quant/execution/shadow_maker.py",
    "scripts/collect_microstructure.py",
    "quant/data/orderbook.py",
)

MARKET_DATA_PROTOCOL_FILES = (
    "scripts/collect_microstructure.py",
    "quant/data/orderbook.py",
)

OBSERVATION_HEADER = [
    "observation_id",
    "signal_id",
    "market_data_segment_id",
    "observed_at",
    "evidence_window_end_at",
    "candidate",
    "symbol",
    "position_side",
    "order_role",
    "action_side",
    "order_type",
    "trigger_kind",
    "probe_attempted",
    "filled",
    "cost_observed",
    "all_in_drag_bps",
    "limit_price",
    "order_qty",
    "queue_ahead_qty",
    "fee_bps",
    "first_depth_update_id",
    "last_depth_update_id",
    "first_agg_trade_id",
    "last_agg_trade_id",
]
EXECUTION_SOURCE_KIND = "binance_usdtm_public_depth_and_aggtrade"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("execution observation timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_flag(value: str, *, line_number: int, name: str) -> bool:
    if value not in {"0", "1"}:
        raise ValueError(f"row {line_number} {name} must be 0 or 1")
    return value == "1"


def execution_scope_key(scope: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(scope.get("candidate") or ""),
        str(scope.get("symbol") or "").upper(),
        str(scope.get("position_side") or "").lower(),
        str(scope.get("order_role") or "").lower(),
        str(scope.get("action_side") or "").lower(),
        str(scope.get("order_type") or "").lower(),
    )


def load_execution_observations(
    path: Path, *, expected_segment_id: str | None = None
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_signal_ids: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != OBSERVATION_HEADER:
            raise ValueError("portfolio execution CSV header does not match the locked schema")
        for line_number, row in enumerate(reader, start=2):
            observation_id = str(row.get("observation_id") or "")
            signal_id = str(row.get("signal_id") or "")
            segment_id = str(row.get("market_data_segment_id") or "")
            if not observation_id or observation_id in seen_ids:
                raise ValueError(f"row {line_number} observation_id is missing or duplicated")
            if not signal_id or signal_id in seen_signal_ids:
                raise ValueError(f"row {line_number} signal_id is missing or duplicated")
            if not segment_id or (
                expected_segment_id is not None and segment_id != expected_segment_id
            ):
                raise ValueError(f"row {line_number} market-data segment binding is invalid")
            observed_at = _parse_utc(str(row.get("observed_at") or ""))
            evidence_window_end_at = _parse_utc(
                str(row.get("evidence_window_end_at") or "")
            )
            if evidence_window_end_at < observed_at:
                raise ValueError(
                    f"row {line_number} evidence window must not end before observation"
                )
            identity = {
                "candidate": str(row.get("candidate") or ""),
                "symbol": str(row.get("symbol") or "").upper(),
                "position_side": str(row.get("position_side") or "").lower(),
                "order_role": str(row.get("order_role") or "").lower(),
                "action_side": str(row.get("action_side") or "").lower(),
                "order_type": str(row.get("order_type") or "").lower(),
                "trigger_kind": str(row.get("trigger_kind") or ""),
            }
            if (
                not identity["candidate"]
                or not identity["symbol"]
                or identity["position_side"] not in {"long", "short"}
                or identity["order_role"] not in {"entry", "exit", "protective_stop"}
                or identity["action_side"] not in {"buy", "sell"}
                or identity["order_type"]
                not in {"maker_limit", "aggressive_limit", "market"}
                or not identity["trigger_kind"]
            ):
                raise ValueError(f"row {line_number} execution scope identity is invalid")
            probe_attempted = _parse_flag(
                str(row.get("probe_attempted") or ""),
                line_number=line_number,
                name="probe_attempted",
            )
            filled = _parse_flag(
                str(row.get("filled") or ""),
                line_number=line_number,
                name="filled",
            )
            cost_observed = _parse_flag(
                str(row.get("cost_observed") or ""),
                line_number=line_number,
                name="cost_observed",
            )
            if (filled or cost_observed) and not probe_attempted:
                raise ValueError(
                    f"row {line_number} fill or cost evidence cannot exist without a probe"
                )
            raw_drag = str(row.get("all_in_drag_bps") or "")
            drag: float | None = None
            if cost_observed:
                try:
                    drag = float(raw_drag)
                except ValueError as exc:
                    raise ValueError(f"row {line_number} all_in_drag_bps is invalid") from exc
                if not isfinite(drag):
                    raise ValueError(f"row {line_number} all_in_drag_bps must be finite")
            elif raw_drag:
                raise ValueError(
                    f"row {line_number} unobserved cost must not contain all_in_drag_bps"
                )
            numeric_inputs: dict[str, float] = {}
            for name in ("limit_price", "order_qty", "queue_ahead_qty", "fee_bps"):
                try:
                    number = float(str(row.get(name) or ""))
                except ValueError as exc:
                    raise ValueError(f"row {line_number} {name} is invalid") from exc
                if not isfinite(number):
                    raise ValueError(f"row {line_number} {name} must be finite")
                numeric_inputs[name] = number
            if (
                numeric_inputs["limit_price"] <= 0
                or numeric_inputs["order_qty"] <= 0
                or numeric_inputs["queue_ahead_qty"] < 0
                or numeric_inputs["fee_bps"] < 0
            ):
                raise ValueError(
                    f"row {line_number} execution replay inputs are outside their valid range"
                )
            event_ids: dict[str, int] = {}
            for name in (
                "first_depth_update_id",
                "last_depth_update_id",
                "first_agg_trade_id",
                "last_agg_trade_id",
            ):
                try:
                    event_id = int(str(row.get(name) or ""))
                except ValueError as exc:
                    raise ValueError(f"row {line_number} {name} is invalid") from exc
                if event_id < 0:
                    raise ValueError(f"row {line_number} {name} must be non-negative")
                event_ids[name] = event_id
            if (
                event_ids["last_depth_update_id"]
                < event_ids["first_depth_update_id"]
                or event_ids["last_agg_trade_id"]
                < event_ids["first_agg_trade_id"]
            ):
                raise ValueError(f"row {line_number} Binance event ID range is reversed")
            seen_ids.add(observation_id)
            seen_signal_ids.add(signal_id)
            rows.append(
                {
                    "observation_id": observation_id,
                    "signal_id": signal_id,
                    "market_data_segment_id": segment_id,
                    "observed_at": observed_at,
                    "evidence_window_end_at": evidence_window_end_at,
                    **identity,
                    "probe_attempted": probe_attempted,
                    "filled": filled,
                    "cost_observed": cost_observed,
                    "all_in_drag_bps": drag,
                    **numeric_inputs,
                    **event_ids,
                }
            )
    if not rows:
        raise ValueError("portfolio execution observation CSV is empty")
    return rows


def _open_market_text(path: Path):
    return gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(
        "r", encoding="utf-8"
    )


def _interval_contains(
    index: dict[str, Any],
    first: int,
    last: int,
    window_start: datetime,
    window_end: datetime,
) -> bool:
    events: list[tuple[int, int, datetime]] = index["events"]
    starts: list[int] = index["starts"]
    position = max(0, bisect_right(starts, first) - 1)
    while position > 0 and events[position - 1][1] >= first:
        position -= 1
    cursor = first
    while cursor <= last:
        best_end = cursor - 1
        while position < len(events) and events[position][0] <= cursor:
            event_first, event_last, received_at = events[position]
            if (
                event_last >= cursor
                and window_start <= received_at <= window_end
                and event_last > best_end
            ):
                best_end = event_last
            position += 1
        if best_end < cursor:
            return False
        cursor = best_end + 1
    return True


def _build_event_index(
    values: list[tuple[int, int, datetime]],
) -> dict[str, Any]:
    events = sorted(values, key=lambda item: (item[0], item[1], item[2]))
    return {"events": events, "starts": [item[0] for item in events]}


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= max(1e-9, abs(left) * 1e-9, abs(right) * 1e-9)


def _replay_execution_observation(row: dict[str, Any], market_data: dict[str, Any]) -> None:
    """Recompute a conservative fill and per-observation drag from sealed raw data."""
    if not row["probe_attempted"]:
        if row["filled"] or row["cost_observed"] or row["all_in_drag_bps"] is not None:
            raise ValueError("unattempted execution observation contains fill or cost claims")
        return

    features = market_data["features_by_update_id"]
    try:
        start = features[row["first_depth_update_id"]]
        terminal = features[row["last_depth_update_id"]]
    except KeyError as exc:
        raise ValueError("execution observation lacks exact start/end book features") from exc
    if not (
        row["observed_at"] <= start["received_at"] <= row["evidence_window_end_at"]
        and row["observed_at"] <= terminal["received_at"] <= row["evidence_window_end_at"]
        and start["received_at"] <= terminal["received_at"]
    ):
        raise ValueError("execution observation book features fall outside its evidence window")

    side = row["action_side"]
    sign = 1.0 if side == "buy" else -1.0
    reference_mid = float(start["mid"])
    terminal_mid = float(terminal["mid"])
    limit_price = float(row["limit_price"])
    quantity = float(row["order_qty"])
    queue_ahead = float(row["queue_ahead_qty"])
    order_type = row["order_type"]
    fill_price: float | None = None
    replayed_filled = False

    if order_type == "maker_limit":
        expected_touch = float(start["best_bid"] if side == "buy" else start["best_ask"])
        expected_queue = float(
            start["best_bid_qty"] if side == "buy" else start["best_ask_qty"]
        )
        opposing_touch = float(start["best_ask"] if side == "buy" else start["best_bid"])
        marketable = limit_price >= opposing_touch if side == "buy" else limit_price <= opposing_touch
        if marketable or not _close(limit_price, expected_touch) or not _close(
            queue_ahead, expected_queue
        ):
            raise ValueError("maker replay must rest at the sealed start-book touch and queue")
        trades = [
            item
            for event_id, item in market_data["trades_by_id"].items()
            if row["first_agg_trade_id"] <= event_id <= row["last_agg_trade_id"]
            and row["observed_at"] <= item["received_at"] <= row["evidence_window_end_at"]
        ]
        estimate = estimate_shadow_fill(
            side=side,
            limit_price=limit_price,
            order_qty=quantity,
            queue_ahead_qty=queue_ahead,
            aggregate_trades=[item["payload"] for item in trades],
        )
        replayed_filled = float(estimate["unfilled_qty"] or 0.0) <= 1e-12
        if replayed_filled:
            fill_price = limit_price
    else:
        opposing_price = float(start["best_ask"] if side == "buy" else start["best_bid"])
        opposing_qty = float(start["best_ask_qty"] if side == "buy" else start["best_bid_qty"])
        crosses = limit_price >= opposing_price if side == "buy" else limit_price <= opposing_price
        if queue_ahead != 0 or quantity > opposing_qty + 1e-12:
            raise ValueError("aggressive replay exceeds the sealed start-book touch")
        if order_type == "aggressive_limit" and not crosses:
            raise ValueError("aggressive limit does not cross the sealed start-book touch")
        if order_type == "market" and not _close(limit_price, opposing_price):
            raise ValueError("market replay price must equal the sealed opposing touch")
        replayed_filled = True
        fill_price = opposing_price

    if replayed_filled:
        assert fill_price is not None
        signed_slippage = sign * (fill_price - reference_mid) / reference_mid * 10_000
        adverse_markout = sign * (fill_price - terminal_mid) / fill_price * 10_000
        replayed_drag = max(0.0, signed_slippage + float(row["fee_bps"])) + max(
            0.0, adverse_markout
        )
    else:
        missed_opportunity = sign * (terminal_mid - reference_mid) / reference_mid * 10_000
        replayed_drag = max(0.0, missed_opportunity)

    if (
        row["filled"] != replayed_filled
        or not row["cost_observed"]
        or row["all_in_drag_bps"] is None
        or not _close(float(row["all_in_drag_bps"]), replayed_drag)
    ):
        raise ValueError("execution fill or all-in drag claim does not replay from raw market data")
    row["replayed_filled"] = replayed_filled
    row["replayed_all_in_drag_bps"] = replayed_drag


def _load_market_data_manifest(path: Path) -> dict[str, Any]:
    payload = path.read_bytes()
    document = json.loads(payload.decode("utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("market-data manifest is invalid")
    symbol = str(document.get("symbol") or "").upper()
    result = document.get("result")
    files = document.get("files")
    if (
        not symbol
        or document.get("probe_kind")
        != "public_market_data_only_no_orders_or_api_keys"
        or not isinstance(result, dict)
        or not isinstance(files, list)
    ):
        raise ValueError("market-data manifest identity is invalid")
    protocol = document.get("protocol_sha256")
    project_root = Path(__file__).resolve().parents[2]
    if not isinstance(protocol, dict) or any(
        protocol.get(relative) != sha256((project_root / relative).read_bytes()).hexdigest()
        for relative in MARKET_DATA_PROTOCOL_FILES
    ):
        raise ValueError("market-data collector protocol is missing or obsolete")
    started_at = _parse_utc(str(result.get("started_at") or ""))
    ended_at = _parse_utc(str(result.get("ended_at") or ""))
    if started_at >= ended_at:
        raise ValueError("market-data manifest time interval is invalid")
    base = path.resolve().parent
    verified_files: list[dict[str, str]] = []
    file_by_name: dict[str, Path] = {}
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"market-data file {index} is invalid")
        relative_path = item.get("path")
        expected_digest = item.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
            raise ValueError(f"market-data file {index} path or digest is missing")
        source_path = (base / relative_path).resolve()
        if not source_path.is_relative_to(base):
            raise ValueError(f"market-data file {index} escapes its manifest directory")
        source_bytes = source_path.read_bytes()
        actual_digest = sha256(source_bytes).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"market-data file {index} digest mismatch")
        file_by_name[source_path.name] = source_path
        verified_files.append(
            {"path": str(source_path), "sha256": actual_digest}
        )
    depth_path = next(
        (value for name, value in file_by_name.items() if name.startswith("depth.jsonl")),
        None,
    )
    trades_path = next(
        (
            value
            for name, value in file_by_name.items()
            if name.startswith("agg_trades.jsonl")
        ),
        None,
    )
    if depth_path is None or trades_path is None:
        raise ValueError("market-data manifest lacks depth or aggregate-trade source")
    features_path = next(
        (value for name, value in file_by_name.items() if name.startswith("book_features.jsonl")),
        None,
    )
    if features_path is None:
        raise ValueError("market-data manifest lacks sealed book-feature source")
    depth_intervals: list[tuple[int, int, datetime]] = []
    with _open_market_text(depth_path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("kind") not in {"snapshot", "depth"}:
                raise ValueError(f"depth source row {line_number} is invalid")
            if row.get("kind") == "snapshot":
                continue
            event = row.get("payload")
            if not isinstance(event, dict) or str(event.get("s") or "").upper() != symbol:
                raise ValueError(f"depth source row {line_number} symbol is invalid")
            first = int(event.get("U"))
            last = int(event.get("u"))
            received_at = _parse_utc(str(row.get("received_at") or ""))
            if first < 0 or last < first:
                raise ValueError(f"depth source row {line_number} update IDs are invalid")
            depth_intervals.append((first, last, received_at))
    features_by_update_id: dict[int, dict[str, Any]] = {}
    with _open_market_text(features_path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if (
                not isinstance(row, dict)
                or row.get("kind") != "book_feature"
                or str(row.get("symbol") or "").upper() != symbol
            ):
                raise ValueError(f"book-feature source row {line_number} is invalid")
            update_id = int(row.get("last_update_id"))
            received_at = _parse_utc(str(row.get("received_at") or ""))
            parsed = {
                "received_at": received_at,
                "best_bid": float(row["best_bid"]),
                "best_ask": float(row["best_ask"]),
                "best_bid_qty": float(row["best_bid_qty"]),
                "best_ask_qty": float(row["best_ask_qty"]),
                "mid": float(row["mid"]),
            }
            if (
                update_id < 0
                or min(parsed["best_bid"], parsed["best_ask"], parsed["mid"]) <= 0
                or min(parsed["best_bid_qty"], parsed["best_ask_qty"]) < 0
                or parsed["best_bid"] >= parsed["best_ask"]
                or not _close(parsed["mid"], (parsed["best_bid"] + parsed["best_ask"]) / 2)
                or update_id in features_by_update_id
            ):
                raise ValueError(f"book-feature source row {line_number} values are invalid")
            features_by_update_id[update_id] = parsed
    agg_intervals: list[tuple[int, int, datetime]] = []
    trades_by_id: dict[int, dict[str, Any]] = {}
    with _open_market_text(trades_path) as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            event = row.get("payload") if isinstance(row, dict) else None
            if (
                not isinstance(event, dict)
                or row.get("kind") != "agg_trade"
                or str(event.get("s") or "").upper() != symbol
            ):
                raise ValueError(f"aggregate-trade source row {line_number} is invalid")
            event_id = int(event.get("a"))
            received_at = _parse_utc(str(row.get("received_at") or ""))
            try:
                price = float(event["p"])
                quantity = float(event["q"])
                buyer_was_maker = event["m"]
                trade_time = int(event["T"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"aggregate-trade source row {line_number} payload is invalid"
                ) from exc
            if (
                event_id < 0
                or event_id in trades_by_id
                or price <= 0
                or quantity <= 0
                or not isinstance(buyer_was_maker, bool)
                or trade_time < 0
            ):
                raise ValueError(f"aggregate-trade source row {line_number} ID is invalid")
            agg_intervals.append((event_id, event_id, received_at))
            trades_by_id[event_id] = {"received_at": received_at, "payload": event}
    if not depth_intervals or not agg_intervals or not features_by_update_id:
        raise ValueError(
            "market-data source has no usable depth, book-feature, or aggregate-trade events"
        )
    return {
        "path": str(path.resolve()),
        "sha256": sha256(payload).hexdigest(),
        "symbol": symbol,
        "started_at": started_at,
        "ended_at": ended_at,
        "depth_intervals": _build_event_index(depth_intervals),
        "agg_trade_intervals": _build_event_index(agg_intervals),
        "features_by_update_id": features_by_update_id,
        "trades_by_id": trades_by_id,
        "files": verified_files,
    }


def load_execution_observation_manifest(
    path: Path,
    *,
    portfolio_manifest_sha256: str,
    portfolio_policy_sha256: str,
) -> dict[str, Any]:
    """Verify a finalized append-only segment chain and load its observations."""
    raw_manifest = path.read_bytes()
    document = json.loads(raw_manifest.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("portfolio execution observation manifest must be one object")
    if (
        document.get("schema_version") != 1
        or document.get("status") != "SEALED"
        or document.get("source_kind") != EXECUTION_SOURCE_KIND
        or document.get("portfolio_manifest_sha256") != portfolio_manifest_sha256
        or document.get("portfolio_promotion_policy_sha256") != portfolio_policy_sha256
    ):
        raise ValueError("portfolio execution observation manifest binding is invalid")
    protocol = document.get("protocol_sha256")
    project_root = Path(__file__).resolve().parents[2]
    current_protocol = {
        relative: sha256((project_root / relative).read_bytes()).hexdigest()
        for relative in PORTFOLIO_EXECUTION_PROTOCOL_FILES
    }
    if protocol != current_protocol:
        raise ValueError("portfolio execution observation manifest protocol is obsolete")
    segments = document.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("portfolio execution observation manifest has no segments")
    base = path.resolve().parent
    observations: list[dict[str, Any]] = []
    source_segments: list[dict[str, Any]] = []
    prior_digest: str | None = None
    prior_last_observed: datetime | None = None
    seen_segment_ids: set[str] = set()
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise ValueError("portfolio execution observation segment is invalid")
        segment_id = str(segment.get("segment_id") or "")
        relative_path = segment.get("path")
        expected_digest = segment.get("sha256")
        market_manifest_raw_path = segment.get("market_data_manifest_path")
        market_manifest_expected_digest = segment.get("market_data_manifest_sha256")
        if (
            not segment_id
            or segment_id in seen_segment_ids
            or not isinstance(relative_path, str)
            or not relative_path
            or not isinstance(expected_digest, str)
            or len(expected_digest) != 64
            or not isinstance(market_manifest_raw_path, str)
            or not market_manifest_raw_path
            or not isinstance(market_manifest_expected_digest, str)
            or len(market_manifest_expected_digest) != 64
            or segment.get("previous_segment_sha256") != prior_digest
        ):
            raise ValueError(f"portfolio execution segment {index} identity or chain is invalid")
        segment_path = (base / relative_path).resolve()
        if not segment_path.is_relative_to(base):
            raise ValueError(f"portfolio execution segment {index} escapes its manifest directory")
        payload = segment_path.read_bytes()
        actual_digest = sha256(payload).hexdigest()
        if actual_digest != expected_digest:
            raise ValueError(f"portfolio execution segment {index} digest mismatch")
        rows = load_execution_observations(
            segment_path, expected_segment_id=segment_id
        )
        market_manifest_path = (base / market_manifest_raw_path).resolve()
        if not market_manifest_path.is_relative_to(base):
            raise ValueError(
                f"portfolio execution segment {index} market-data manifest escapes its observation-manifest directory"
            )
        market_data = _load_market_data_manifest(market_manifest_path)
        if market_data["sha256"] != market_manifest_expected_digest:
            raise ValueError(
                f"portfolio execution segment {index} market-data manifest digest mismatch"
            )
        for row in rows:
            if (
                row["symbol"] != market_data["symbol"]
                or not (
                    market_data["started_at"]
                    <= row["observed_at"]
                    <= market_data["ended_at"]
                )
                or not _interval_contains(
                    market_data["depth_intervals"],
                    row["first_depth_update_id"],
                    row["last_depth_update_id"],
                    row["observed_at"],
                    row["evidence_window_end_at"],
                )
                or not _interval_contains(
                    market_data["agg_trade_intervals"],
                    row["first_agg_trade_id"],
                    row["last_agg_trade_id"],
                    row["observed_at"],
                    row["evidence_window_end_at"],
                )
            ):
                raise ValueError(
                    f"portfolio execution segment {index} observation is not covered by its raw Binance events"
                )
            _replay_execution_observation(row, market_data)
        first_observed = min(item["observed_at"] for item in rows)
        last_observed = max(item["observed_at"] for item in rows)
        sealed_at = _parse_utc(str(segment.get("sealed_at_utc") or ""))
        if (
            segment.get("rows") != len(rows)
            or segment.get("first_observed_at") != first_observed.isoformat()
            or segment.get("last_observed_at") != last_observed.isoformat()
            or sealed_at < last_observed
            or (prior_last_observed is not None and first_observed <= prior_last_observed)
        ):
            raise ValueError(f"portfolio execution segment {index} metadata is inconsistent")
        observations.extend(rows)
        source_segments.append(
            {
                "segment_id": segment_id,
                "path": str(segment_path),
                "sha256": actual_digest,
                "market_data_manifest": {
                    "path": market_data["path"],
                    "sha256": market_data["sha256"],
                    "files": market_data["files"],
                },
            }
        )
        seen_segment_ids.add(segment_id)
        prior_digest = actual_digest
        prior_last_observed = last_observed
    observation_ids = [item["observation_id"] for item in observations]
    signal_ids = [item["signal_id"] for item in observations]
    if len(observation_ids) != len(set(observation_ids)) or len(signal_ids) != len(
        set(signal_ids)
    ):
        raise ValueError("execution observation or signal IDs repeat across sealed segments")
    return {
        "manifest": document,
        "manifest_sha256": sha256(raw_manifest).hexdigest(),
        "manifest_path": str(path.resolve()),
        "segments": source_segments,
        "observations": observations,
    }


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def evaluate_portfolio_execution(
    observations: list[dict[str, Any]],
    expected_scopes: list[dict[str, Any]],
    *,
    minimum_observations_per_scope: int,
    minimum_passive_fills_per_scope: int,
    coverage_start: datetime,
    coverage_end: datetime,
) -> dict[str, Any]:
    expected_by_key = {execution_scope_key(item): item for item in expected_scopes}
    observed_keys = {execution_scope_key(item) for item in observations}
    unexpected = sorted(observed_keys - set(expected_by_key))
    outside_sample = sum(
        not (coverage_start <= item["observed_at"] <= coverage_end)
        for item in observations
    )
    scopes: list[dict[str, Any]] = []
    for key, expected in expected_by_key.items():
        relevant = [item for item in observations if execution_scope_key(item) == key]
        attempts = sum(item["probe_attempted"] for item in relevant)
        replayed = [
            item
            for item in relevant
            if "replayed_filled" in item and "replayed_all_in_drag_bps" in item
        ]
        fills = sum(bool(item["replayed_filled"]) for item in replayed)
        costs = [
            float(item["replayed_all_in_drag_bps"])
            for item in replayed
            if item["cost_observed"]
        ]
        observations_count = len(relevant)
        trigger_coverage = attempts / observations_count if observations_count else 0.0
        cost_coverage = len(costs) / attempts if attempts else 0.0
        mean_drag = sum(costs) / len(costs) if costs else None
        p95_drag = _quantile(costs, 0.95) if costs else None
        maker = expected["order_type"] == "maker_limit"
        minimum_fills = (
            minimum_passive_fills_per_scope
            if maker
            else minimum_observations_per_scope
        )
        budget = float(expected["maximum_all_in_drag_bps"])
        expected_fee_bps = float(expected["fee_bps"])
        passed = (
            observations_count >= minimum_observations_per_scope
            and len(replayed) == observations_count
            and attempts == observations_count
            and fills >= minimum_fills
            and (maker or fills == observations_count)
            and len(costs) == attempts
            and mean_drag is not None
            and p95_drag is not None
            and mean_drag < budget
            and p95_drag < budget
            and all(_close(float(item["fee_bps"]), expected_fee_bps) for item in relevant)
            and all(item["trigger_kind"] == expected["trigger_kind"] for item in relevant)
            and all(coverage_start <= item["observed_at"] <= coverage_end for item in relevant)
        )
        scopes.append(
            {
                **expected,
                "status": "PASS" if passed else "FAIL",
                "signal_triggered_observations": observations_count,
                "fills": fills,
                "trigger_coverage_ratio": trigger_coverage,
                "cost_coverage_ratio": cost_coverage,
                "raw_replay_coverage_ratio": (
                    len(replayed) / observations_count if observations_count else 0.0
                ),
                "mean_all_in_drag_bps": mean_drag,
                "p95_all_in_drag_bps": p95_drag,
            }
        )
    checks = [
        {
            "name": "exact_execution_scope_inventory",
            "passed": not unexpected and bool(expected_by_key),
            "actual": {"unexpected": unexpected, "expected_scopes": len(expected_by_key)},
            "required": "no observations outside the manifest scope inventory",
        },
        {
            "name": "execution_observations_within_mtm_sample",
            "passed": outside_sample == 0,
            "actual": outside_sample,
            "required": 0,
        },
        {
            "name": "all_execution_scopes_pass",
            "passed": bool(scopes) and all(item["status"] == "PASS" for item in scopes),
            "actual": sum(item["status"] == "PASS" for item in scopes),
            "required": len(scopes),
        },
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "evidence_purpose": "live_promotion_candidate",
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "observation_rows": len(observations),
        "scopes": scopes,
        "checks": checks,
    }
