"""Evaluate the pre-registered Alpha #7 shadow maker hypothesis."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
from pathlib import Path
import random
from statistics import mean, median

from quant.execution.shadow_maker import estimate_shadow_fill
from quant.research.protocol import LEGACY_PROTOCOL_FILE_SETS, PROTOCOL_FILES


CONFIG = {
    "depth_imbalance_min": 0.60,
    "flow_imbalance_min": 0.50,
    "flow_window_ms": 5_000,
    "signal_spacing_ms": 60_000,
    "queue_timeout_ms": 3_000,
    "shadow_notional_usdt": 50.0,
    "entry_fee": 0.0002,
    "exit_fee": 0.0005,
    "horizons_seconds": (5, 30, 60, 300),
}


def _read_gzip_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _timing_status(manifest: dict) -> dict:
    result = manifest.get("result") or {}
    duration = int(result.get("requested_duration_seconds") or 0)
    timing = result.get("timing") or {}
    if timing:
        wall_elapsed = float(timing.get("active_wall_seconds") or 0.0)
        return {
            "wall_elapsed_seconds": wall_elapsed,
            "healthy": bool(timing.get("healthy")),
            "source": "dual_clock",
        }
    try:
        started = datetime.fromisoformat(str(result["started_at"]).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(result["ended_at"]).replace("Z", "+00:00"))
        wall_elapsed = (ended - started).total_seconds()
    except (KeyError, TypeError, ValueError):
        return {"wall_elapsed_seconds": None, "healthy": False, "source": "missing"}
    return {
        "wall_elapsed_seconds": wall_elapsed,
        "healthy": bool(duration > 0 and duration * 0.90 <= wall_elapsed <= duration * 1.25),
        "source": "legacy_wall_clock",
    }


def _verify_run(run_dir: Path) -> dict:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise ValueError(f"unsupported manifest schema in {run_dir}")
    if manifest.get("symbol") != "BTCUSDT" or manifest.get("depth_levels") != 5:
        raise ValueError(f"run {run_dir} does not match preregistered BTCUSDT top-five data")
    if manifest.get("probe_kind") != "public_market_data_only_no_orders_or_api_keys":
        raise ValueError(f"run {run_dir} has an unexpected probe kind")
    required = {"depth.jsonl.gz", "book_features.jsonl.gz", "agg_trades.jsonl.gz"}
    observed: set[str] = set()
    for item in manifest.get("files") or []:
        name = str(item.get("path") or "")
        path = run_dir / name
        if name not in required or not path.is_file():
            raise ValueError(f"manifest references an unexpected or missing file: {path}")
        if path.stat().st_size != int(item.get("bytes") or -1) or _digest(path) != item.get("sha256"):
            raise ValueError(f"manifest integrity failure: {path}")
        observed.add(name)
    if observed != required:
        raise ValueError(f"manifest file set incomplete in {run_dir}: {sorted(required - observed)}")
    protocol = manifest.get("protocol_sha256")
    if protocol is not None:
        required_protocol = set(PROTOCOL_FILES)
        accepted_file_sets = {frozenset(required_protocol), *LEGACY_PROTOCOL_FILE_SETS}
        if frozenset(protocol) not in accepted_file_sets or any(
            not isinstance(value, str) or len(value) != 64 for value in protocol.values()
        ):
            raise ValueError(f"invalid protocol fingerprint in {run_dir}")
    return {
        "run_id": manifest.get("run_id"),
        "files_verified": len(observed),
        "protocol_sha256": protocol,
        "timing": _timing_status(manifest),
    }


def _received_time_ms(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _load_run(run_dir: Path) -> tuple[list[dict], list[dict]]:
    features = _load_features(run_dir)
    trades = _load_trades(run_dir)
    return features, trades


def _load_features(run_dir: Path) -> list[dict]:
    features = _read_gzip_jsonl(run_dir / "book_features.jsonl.gz")
    for row in features:
        row["_available_time_ms"] = _received_time_ms(row.get("received_at"), int(row["transaction_time_ms"]))
        row["_run_id"] = run_dir.name
    features.sort(key=lambda row: (int(row["_available_time_ms"]), int(row["last_update_id"])))
    return features


def _load_trades(run_dir: Path) -> list[dict]:
    trades: list[dict] = []
    for record in _read_gzip_jsonl(run_dir / "agg_trades.jsonl.gz"):
        trade = dict(record["payload"])
        trade["_exchange_time_ms"] = int(trade["T"])
        # Shadow decisions can only use trades after they reached this host.
        trade["T"] = _received_time_ms(record.get("received_at"), int(trade["T"]))
        trade["_run_id"] = run_dir.name
        trades.append(trade)
    trades.sort(key=lambda row: (int(row["T"]), int(row["a"])))
    return trades


def _load_runs(run_dirs: list[Path]) -> tuple[list[dict], list[dict]]:
    """Merge immutable chunks without resetting flow or signal spacing."""
    feature_by_update: dict[int, dict] = {}
    trade_by_id: dict[int, dict] = {}
    for run_dir in run_dirs:
        features, trades = _load_run(run_dir)
        for row in features:
            key = int(row["last_update_id"])
            if key not in feature_by_update or int(row["_available_time_ms"]) < int(feature_by_update[key]["_available_time_ms"]):
                feature_by_update[key] = row
        for trade in trades:
            key = int(trade["a"])
            if key not in trade_by_id or int(trade["T"]) < int(trade_by_id[key]["T"]):
                trade_by_id[key] = trade
    features = sorted(feature_by_update.values(), key=lambda row: (int(row["_available_time_ms"]), int(row["last_update_id"])))
    trades = sorted(trade_by_id.values(), key=lambda row: (int(row["T"]), int(row["a"])))
    return features, trades


def _day_from_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def _hour_from_ms(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H")


def _coverage(run_dirs: list[Path]) -> list[dict]:
    seconds_by_day: dict[str, set[int]] = defaultdict(set)
    for run_dir in run_dirs:
        features = _load_features(run_dir)
        for row in features:
            timestamp_ms = int(row["_available_time_ms"])
            seconds_by_day[_day_from_ms(timestamp_ms)].add(timestamp_ms // 1000)
    return [
        {
            "day": day,
            "observed_seconds": len(seconds),
            "coverage_ratio": len(seconds) / 86_400,
            "complete": len(seconds) / 86_400 >= 0.95,
        }
        for day, seconds in sorted(seconds_by_day.items())
    ]


def _phase_map(coverage: list[dict]) -> dict[str, str]:
    complete_days = [item["day"] for item in coverage if item["complete"]]
    result: dict[str, str] = {}
    for index, day in enumerate(complete_days):
        if index < 7:
            result[day] = "DEVELOPMENT"
        elif index < 10:
            result[day] = "CALIBRATION"
        elif index < 17:
            result[day] = "LOCKED_OOS"
        else:
            result[day] = "POST_LOCKED"
    return result


def _future_feature(features: list[dict], feature_times: list[int], target_ms: int) -> dict | None:
    index = bisect_left(feature_times, target_ms)
    if index >= len(features):
        return None
    # Decisions and fills use local availability time, so the missing-data
    # check must use the same clock.  Mixing exchange transaction time with a
    # local receive-time target can accept stale quotes or reject valid ones.
    if int(features[index]["_available_time_ms"]) - target_ms > 5_000:
        return None
    return features[index]


def _execution(
    *,
    side: str,
    feature: dict,
    decision_ms: int,
    trades: list[dict],
    trade_times: list[int],
    features: list[dict],
    feature_times: list[int],
) -> dict:
    limit_price = float(feature["best_bid"] if side == "buy" else feature["best_ask"])
    queue_ahead = float(feature["best_bid_qty"] if side == "buy" else feature["best_ask_qty"])
    quantity = CONFIG["shadow_notional_usdt"] / limit_price
    trade_start = bisect_right(trade_times, decision_ms)
    trade_end = bisect_right(trade_times, decision_ms + CONFIG["queue_timeout_ms"])
    estimate = estimate_shadow_fill(
        side=side,
        limit_price=limit_price,
        order_qty=quantity,
        queue_ahead_qty=queue_ahead,
        aggregate_trades=trades[trade_start:trade_end],
    )
    filled = float(estimate["unfilled_qty"] or 0.0) <= 1e-12
    result = {
        "decision_time_ms": decision_ms,
        "limit_price": limit_price,
        "quantity": quantity,
        "queue_ahead_qty": queue_ahead,
        "fully_filled": filled,
        "fill": estimate,
        "net_markout": {},
    }
    fill_time = estimate.get("full_fill_time_ms")
    if filled and fill_time:
        direction = 1.0 if side == "buy" else -1.0
        for horizon in CONFIG["horizons_seconds"]:
            future = _future_feature(features, feature_times, int(fill_time) + horizon * 1000)
            exit_price = (
                float(future["best_bid"] if side == "buy" else future["best_ask"])
                if future is not None
                else None
            )
            result["net_markout"][str(horizon)] = (
                direction * (exit_price / limit_price - 1.0) - CONFIG["entry_fee"] - CONFIG["exit_fee"]
                if exit_price is not None
                else None
            )
    return result


def _run_signals(run_dir: Path, phase_by_day: dict[str, str]) -> list[dict]:
    features, trades = _load_run(run_dir)
    return _run_signals_from_data(features, trades, phase_by_day)


def _run_signals_from_data(features: list[dict], trades: list[dict], phase_by_day: dict[str, str]) -> list[dict]:
    signals, _ = _run_signals_window(features, trades, phase_by_day)
    return signals


def _run_signals_window(
    features: list[dict],
    trades: list[dict],
    phase_by_day: dict[str, str],
    *,
    decision_start_ms: int | None = None,
    decision_end_ms: int | None = None,
    initial_last_signal_ms: int | None = None,
) -> tuple[list[dict], int]:
    if not features or not trades:
        return [], initial_last_signal_ms if initial_last_signal_ms is not None else -10**30
    feature_times = [int(row["_available_time_ms"]) for row in features]
    trade_times = [int(row["T"]) for row in trades]
    first_boundary = max(feature_times[0], decision_start_ms) if decision_start_ms is not None else feature_times[0]
    boundary = ((first_boundary + 999) // 1000) * 1000
    final_boundary = min(feature_times[-1], decision_end_ms) if decision_end_ms is not None else feature_times[-1]
    feature_index = -1
    trade_index = 0
    flow: deque[tuple[int, float, float]] = deque()
    buy_qty = sell_qty = 0.0
    last_signal_ms = initial_last_signal_ms if initial_last_signal_ms is not None else -10**30
    signals: list[dict] = []

    while boundary <= final_boundary:
        while feature_index + 1 < len(features) and feature_times[feature_index + 1] <= boundary:
            feature_index += 1
        while trade_index < len(trades) and trade_times[trade_index] <= boundary:
            trade = trades[trade_index]
            quantity = float(trade["q"])
            buy = quantity if not bool(trade["m"]) else 0.0
            sell = quantity if bool(trade["m"]) else 0.0
            flow.append((int(trade["T"]), buy, sell))
            buy_qty += buy
            sell_qty += sell
            trade_index += 1
        while flow and flow[0][0] <= boundary - CONFIG["flow_window_ms"]:
            _, buy, sell = flow.popleft()
            buy_qty -= buy
            sell_qty -= sell
        if feature_index < 0 or boundary - feature_times[feature_index] > 1_000:
            boundary += 1_000
            continue
        total_flow = buy_qty + sell_qty
        flow_imbalance = (buy_qty - sell_qty) / total_flow if total_flow > 0 else 0.0
        feature = features[feature_index]
        depth_imbalance = float(feature["depth_imbalance"])
        aligned = depth_imbalance * flow_imbalance > 0
        if (
            abs(depth_imbalance) >= CONFIG["depth_imbalance_min"]
            and abs(flow_imbalance) >= CONFIG["flow_imbalance_min"]
            and aligned
            and boundary - last_signal_ms >= CONFIG["signal_spacing_ms"]
        ):
            last_signal_ms = boundary
            side = "buy" if depth_imbalance > 0 else "sell"
            base_execution = _execution(
                side=side,
                feature=feature,
                decision_ms=boundary,
                trades=trades,
                trade_times=trade_times,
                features=features,
                feature_times=feature_times,
            )
            latency_index = bisect_left(feature_times, boundary + 100)
            latency_execution = None
            if latency_index < len(features) and feature_times[latency_index] - (boundary + 100) <= 500:
                latency_decision_ms = feature_times[latency_index]
                latency_execution = _execution(
                    side=side,
                    feature=features[latency_index],
                    decision_ms=latency_decision_ms,
                    trades=trades,
                    trade_times=trade_times,
                    features=features,
                    feature_times=feature_times,
                )
            signal = {
                "run_id": feature.get("_run_id", "unknown"),
                "signal_time_ms": boundary,
                "day": _day_from_ms(boundary),
                "hour": _hour_from_ms(boundary),
                "phase": phase_by_day.get(_day_from_ms(boundary), "UNASSIGNED_INCOMPLETE"),
                "side": side,
                "depth_imbalance": depth_imbalance,
                "flow_imbalance": flow_imbalance,
                **base_execution,
                "latency_100ms": latency_execution,
            }
            signals.append(signal)
        boundary += 1_000
    return signals, last_signal_ms


def _stream_signals(run_dirs: list[Path], phase_by_day: dict[str, str]) -> list[dict]:
    """Evaluate ordered chunks with five-minute look-forward using bounded RAM."""
    cache: dict[int, tuple[list[dict], list[dict]]] = {}

    def loaded(index: int) -> tuple[list[dict], list[dict]]:
        if index not in cache:
            cache[index] = _load_run(run_dirs[index])
        return cache[index]

    signals: list[dict] = []
    last_signal_ms = -10**30
    for index in range(len(run_dirs)):
        current_features, _ = loaded(index)
        if not current_features:
            continue
        window_paths = range(max(0, index - 1), min(len(run_dirs), index + 2))
        features: list[dict] = []
        trades: list[dict] = []
        for nearby in window_paths:
            nearby_features, nearby_trades = loaded(nearby)
            features.extend(nearby_features)
            trades.extend(nearby_trades)
        feature_by_update: dict[int, dict] = {}
        for row in features:
            key = int(row["last_update_id"])
            if key not in feature_by_update or int(row["_available_time_ms"]) < int(feature_by_update[key]["_available_time_ms"]):
                feature_by_update[key] = row
        trade_by_id: dict[int, dict] = {}
        for trade in trades:
            key = int(trade["a"])
            if key not in trade_by_id or int(trade["T"]) < int(trade_by_id[key]["T"]):
                trade_by_id[key] = trade
        merged_features = sorted(feature_by_update.values(), key=lambda row: (int(row["_available_time_ms"]), int(row["last_update_id"])))
        merged_trades = sorted(trade_by_id.values(), key=lambda row: (int(row["T"]), int(row["a"])))
        chunk_signals, last_signal_ms = _run_signals_window(
            merged_features,
            merged_trades,
            phase_by_day,
            decision_start_ms=int(current_features[0]["_available_time_ms"]),
            decision_end_ms=int(current_features[-1]["_available_time_ms"]),
            initial_last_signal_ms=last_signal_ms,
        )
        signals.extend(chunk_signals)
        for stale_index in [key for key in cache if key < index]:
            del cache[stale_index]
    return signals


def _phase_summary(signals: list[dict], phase: str) -> dict:
    selected = [signal for signal in signals if signal["phase"] == phase]
    filled = [signal for signal in selected if signal["fully_filled"]]
    result = {
        "eligible_shadow_orders": len(selected),
        "fully_filled_orders": len(filled),
        "fill_rate": len(filled) / len(selected) if selected else None,
    }
    for horizon in CONFIG["horizons_seconds"]:
        values = [
            float(signal["net_markout"][str(horizon)])
            for signal in filled
            if signal["net_markout"].get(str(horizon)) is not None
        ]
        result[f"net_markout_{horizon}s"] = {
            "observations": len(values),
            "mean": mean(values) if values else None,
            "median": median(values) if values else None,
            "positive_ratio": sum(value > 0 for value in values) / len(values) if values else None,
        }
    latency_values = [
        float(signal["latency_100ms"]["net_markout"]["60"])
        for signal in selected
        if signal.get("latency_100ms")
        and signal["latency_100ms"]["fully_filled"]
        and signal["latency_100ms"]["net_markout"].get("60") is not None
    ]
    result["latency_100ms_net_markout_60s"] = {
        "observations": len(latency_values),
        "mean": mean(latency_values) if latency_values else None,
    }
    sixty_second = [
        (signal["hour"], float(signal["net_markout"]["60"]))
        for signal in filled
        if signal["net_markout"].get("60") is not None
    ]
    hourly: dict[str, list[float]] = defaultdict(list)
    for hour, value in sixty_second:
        hourly[hour].append(value)
    positive_by_hour = {hour: sum(max(value, 0.0) for value in values) for hour, values in hourly.items()}
    gross_positive = sum(positive_by_hour.values())
    result["single_hour_profit_concentration"] = (
        max(positive_by_hour.values()) / gross_positive if gross_positive else None
    )
    result["hour_block_bootstrap_95pct_lower_mean_60s"] = _bootstrap_hourly_lower(hourly)
    return result


def _bootstrap_hourly_lower(hourly: dict[str, list[float]], simulations: int = 2000) -> float | None:
    """Deterministic hour-block bootstrap lower 95% bound of mean return."""
    blocks = list(hourly.values())
    if len(blocks) < 2 or sum(len(block) for block in blocks) < 20:
        return None
    generator = random.Random(7)
    estimates: list[float] = []
    for _ in range(simulations):
        sample = [value for _ in blocks for value in generator.choice(blocks)]
        estimates.append(mean(sample))
    estimates.sort()
    return estimates[int((len(estimates) - 1) * 0.025)]


def analyze_microstructure(root: Path) -> dict:
    run_dirs = sorted(path.parent for path in root.glob("*/manifest.json"))
    integrity = [_verify_run(run_dir) for run_dir in run_dirs]
    eligible_pairs = [
        (run_dir, item)
        for run_dir, item in zip(run_dirs, integrity)
        if bool(item["timing"]["healthy"])
    ]
    eligible_run_dirs = [run_dir for run_dir, _ in eligible_pairs]
    run_counters: dict[Path, dict[str, int]] = {}
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        result = manifest.get("result") or {}
        depth = result.get("depth") or {}
        trades = result.get("trades") or {}
        run_counters[run_dir] = {
            "depth_events": int(depth.get("depth_events") or 0),
            "aggregate_trades": int(trades.get("aggregate_trades") or 0),
            "feature_rows": int(depth.get("feature_rows") or 0),
            "compressed_bytes": sum(int(item.get("bytes") or 0) for item in manifest.get("files") or []),
        }

    def summed(paths: list[Path]) -> dict[str, int]:
        return {
            key: sum(run_counters[path][key] for path in paths)
            for key in ("depth_events", "aggregate_trades", "feature_rows", "compressed_bytes")
        }
    all_protocol_variants = {
        json.dumps(item["protocol_sha256"], sort_keys=True)
        for item in integrity
        if item.get("protocol_sha256") is not None
    }
    coverage = _coverage(eligible_run_dirs)
    complete_day_names = {item["day"] for item in coverage if item["complete"]}
    evaluated_protocol_variants: set[str] = set()
    legacy_complete_runs = 0
    for run_dir, item in eligible_pairs:
        run_days = {
            _day_from_ms(int(row["_available_time_ms"]))
            for row in _load_features(run_dir)
        }
        if not run_days.intersection(complete_day_names):
            continue
        if item.get("protocol_sha256") is None:
            legacy_complete_runs += 1
        else:
            evaluated_protocol_variants.add(json.dumps(item["protocol_sha256"], sort_keys=True))
    if legacy_complete_runs or len(evaluated_protocol_variants) > 1:
        raise ValueError("collection protocol is missing or changed within evaluated complete days")
    phases = _phase_map(coverage)
    signals = _stream_signals(eligible_run_dirs, phases)
    summaries = {phase: _phase_summary(signals, phase) for phase in ("DEVELOPMENT", "CALIBRATION", "LOCKED_OOS")}
    locked = summaries["LOCKED_OOS"]
    locked_60s = locked["net_markout_60s"]
    latency_60s = locked["latency_100ms_net_markout_60s"]
    complete_days = sum(item["complete"] for item in coverage)
    checks = [
        {"name": "complete_collection_days", "passed": complete_days >= 17, "actual": complete_days, "required": 17},
        {"name": "locked_eligible_orders", "passed": locked["eligible_shadow_orders"] >= 1000, "actual": locked["eligible_shadow_orders"], "required": 1000},
        {"name": "locked_queue_fills", "passed": locked["fully_filled_orders"] >= 200, "actual": locked["fully_filled_orders"], "required": 200},
        {"name": "locked_net_60s_markout", "passed": locked_60s["mean"] is not None and locked_60s["mean"] > 0, "actual": locked_60s["mean"], "required": "> 0 after 7 bps fees"},
        {
            "name": "hour_block_bootstrap_95pct_lower_bound",
            "passed": locked["hour_block_bootstrap_95pct_lower_mean_60s"] is not None and locked["hour_block_bootstrap_95pct_lower_mean_60s"] > 0,
            "actual": locked["hour_block_bootstrap_95pct_lower_mean_60s"],
            "required": "> 0",
        },
        {
            "name": "plus_100ms_latency_net_60s_markout",
            "passed": latency_60s["mean"] is not None and latency_60s["mean"] > 0,
            "actual": latency_60s["mean"],
            "required": "> 0 after 7 bps fees",
        },
        {
            "name": "single_hour_profit_concentration",
            "passed": locked["single_hour_profit_concentration"] is not None and locked["single_hour_profit_concentration"] <= 0.20,
            "actual": locked["single_hour_profit_concentration"],
            "required": "<= 0.20",
        },
    ]
    # The locked window is fixed. Once all 17 days exist, too few signals or
    # fills is itself a failed hypothesis rather than permission to extend the
    # sample until significance appears.
    collection_ready = checks[0]["passed"]
    status = "PASS" if all(check["passed"] for check in checks) else ("FAIL" if collection_ready else "ACCUMULATING")
    return {
        "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
        "status": status,
        "basis": "public market data only; decisions use local receive time, maker queue, and executable taker-exit quote; no API key or order",
        "configuration": CONFIG,
        "runs": len(run_dirs),
        "eligible_timing_runs": len(eligible_run_dirs),
        "excluded_timing_runs": len(run_dirs) - len(eligible_run_dirs),
        "collection_totals": {
            "all_verified_runs": summed(run_dirs),
            "timing_eligible_runs": summed(eligible_run_dirs),
        },
        "data_integrity": {
            "status": "PASS" if len(eligible_run_dirs) == len(run_dirs) else "PASS_WITH_TIMING_EXCLUSIONS",
            "verified_runs": integrity,
            "fingerprinted_protocol_variants": len(all_protocol_variants),
            "evaluated_protocol_variants": len(evaluated_protocol_variants),
            "legacy_prefingerprint_runs": sum(item.get("protocol_sha256") is None for item in integrity),
        },
        "coverage": coverage,
        "complete_days": complete_days,
        "all_detected_signals": len(signals),
        "incomplete_day_signals_excluded": sum(signal["phase"] == "UNASSIGNED_INCOMPLETE" for signal in signals),
        "phases": summaries,
        "checks": checks,
        "warning": "A PASS is shadow evidence only and still requires a separate dry-run gate before promotion.",
    }
