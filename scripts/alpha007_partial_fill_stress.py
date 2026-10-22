#!/usr/bin/env python3
"""Replay Alpha #7 with every positive partial fill charged to the strategy."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research import microstructure as primary_evaluator
from quant.research.partial_fill_stress import evaluate_partial_fill_stress
from scripts.sync_alpha007_registry import replay_report


POLICY_PATH = PROJECT_ROOT / "research" / "alpha007-partial-fill-stress-policy.json"
PRIMARY_PATH = PROJECT_ROOT / "reports" / "alpha007-accumulation.json"
RAW_ROOT = PROJECT_ROOT / "research" / "microstructure"
OUTPUT_PATH = PROJECT_ROOT / "reports" / "alpha007-partial-fill-stress.json"
REGISTRY_PATH = PROJECT_ROOT / "research" / "registry.json"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _source_inventory(run_dirs: list[Path]) -> tuple[list[dict], str]:
    inventory = [
        {
            "path": str((run_dir / "manifest.json").relative_to(PROJECT_ROOT)),
            "sha256": _digest(run_dir / "manifest.json"),
        }
        for run_dir in run_dirs
    ]
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    return inventory, sha256(encoded).hexdigest()


def _policy_registry_binding(policy: dict, policy_hash: str, primary: dict) -> dict:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    experiment = next(
        (
            item
            for item in registry.get("experiments") or []
            if item.get("strategy") == policy.get("candidate")
        ),
        None,
    )
    if experiment is None:
        raise ValueError("Alpha #7 registry experiment is missing")
    events = [
        item
        for item in registry.get("events") or []
        if item.get("experiment_id") == experiment.get("experiment_id")
        and item.get("event_type") == "auxiliary_gate_preregistered"
        and ((item.get("payload") or {}).get("gate")) == policy.get("policy")
    ]
    hashes = {(item.get("payload") or {}).get("policy_sha256") for item in events}
    if not events or hashes != {policy_hash}:
        raise ValueError("partial-fill policy is not uniquely hash-bound in the registry")
    first = min(events, key=lambda item: int(item["event_id"]))
    payload = first.get("payload") or {}
    if (
        payload.get("registered_at_utc") != policy.get("registered_at_utc")
        or payload.get("promotion_eligible") is not False
        or payload.get("can_only_veto") is not True
    ):
        raise ValueError("partial-fill registry event semantics are invalid")
    try:
        event_time = datetime.fromisoformat(str(first["created_at"]).replace("Z", "+00:00"))
        if event_time.tzinfo is None:
            raise ValueError("timezone missing")
        event_time = event_time.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("partial-fill registry event timestamp is invalid") from error
    complete_days = [
        str(item["day"])
        for item in primary.get("coverage") or []
        if isinstance(item, dict) and item.get("complete") is True
    ]
    locked_start = None
    if len(complete_days) >= 11:
        locked_start = datetime.fromisoformat(complete_days[10]).replace(tzinfo=timezone.utc)
        if event_time > locked_start:
            raise ValueError("partial-fill policy was registered after locked OOS began")
    return {
        "experiment_id": int(experiment["experiment_id"]),
        "event_id": int(first["event_id"]),
        "event_created_at": event_time.isoformat(),
        "before_locked_oos": locked_start is None or event_time <= locked_start,
        "locked_oos_start": locked_start.isoformat() if locked_start else None,
    }


def _eligible_runs(run_dirs: list[Path]) -> list[Path]:
    return [
        run_dir
        for run_dir in run_dirs
        if bool(primary_evaluator._verify_run(run_dir)["timing"]["healthy"])
    ]


class ExecutableQuoteLookup:
    """Resolve a future quote from the signal chunk and its nearest neighbors."""

    def __init__(self, run_dirs: list[Path]):
        self.run_dirs = run_dirs
        self.index = {path.name: position for position, path in enumerate(run_dirs)}
        self.cache: dict[int, list[dict]] = {}

    def _features(self, position: int) -> list[dict]:
        if position not in self.cache:
            self.cache[position] = primary_evaluator._load_features(
                self.run_dirs[position]
            )
        return self.cache[position]

    def price(self, signal: dict, execution: dict) -> float | None:
        position = self.index.get(str(signal.get("run_id") or ""))
        if position is None:
            return None
        rows: list[dict] = []
        for nearby in range(max(0, position - 1), min(len(self.run_dirs), position + 3)):
            rows.extend(self._features(nearby))
        by_update: dict[int, dict] = {}
        for row in rows:
            update_id = int(row["last_update_id"])
            if update_id not in by_update or int(row["_available_time_ms"]) < int(
                by_update[update_id]["_available_time_ms"]
            ):
                by_update[update_id] = row
        features = sorted(
            by_update.values(),
            key=lambda item: (int(item["_available_time_ms"]), int(item["last_update_id"])),
        )
        times = [int(item["_available_time_ms"]) for item in features]
        target_ms = int(execution["decision_time_ms"]) + 60_000
        future = primary_evaluator._future_feature(features, times, target_ms)
        if future is None:
            return None
        return float(future["best_bid"] if signal["side"] == "buy" else future["best_ask"])


def _record(
    signal: dict,
    execution: dict | None,
    *,
    path: str,
    quote_lookup: ExecutableQuoteLookup,
    round_trip_cost_bps: float,
) -> dict:
    base = {
        "path": path,
        "signal_time_ms": int(signal["signal_time_ms"]),
        "hour": signal["hour"],
        "side": signal["side"],
        "execution_available": execution is not None,
        "fill_fraction": 0.0,
        "net_return": None,
    }
    if execution is None:
        return base
    quantity = float(execution["quantity"])
    filled = float((execution.get("fill") or {}).get("filled_qty") or 0.0)
    fill_fraction = min(1.0, max(0.0, filled / quantity)) if quantity > 0 else 0.0
    base["fill_fraction"] = fill_fraction
    if fill_fraction <= 0:
        base["net_return"] = 0.0
        return base
    exit_price = quote_lookup.price(signal, execution)
    if exit_price is None:
        return base
    direction = 1.0 if signal["side"] == "buy" else -1.0
    base["net_return"] = (
        direction * (exit_price / float(execution["limit_price"]) - 1.0)
        - round_trip_cost_bps / 10_000
    )
    return base


def _report_run_dirs(primary: dict, raw_root: Path) -> list[Path]:
    verified = (primary.get("data_integrity") or {}).get("verified_runs") or []
    run_ids = [str(item.get("run_id") or "") for item in verified if isinstance(item, dict)]
    if not run_ids or "" in run_ids or len(run_ids) != len(set(run_ids)):
        raise ValueError("primary report has an invalid verified-run inventory")
    run_dirs = sorted(raw_root / run_id for run_id in run_ids)
    if any(not (run_dir / "manifest.json").is_file() for run_dir in run_dirs):
        raise ValueError("primary report references a missing run manifest")
    return run_dirs


def load_locked_records(run_dirs: list[Path], policy: dict) -> list[dict]:
    eligible = _eligible_runs(run_dirs)
    coverage = primary_evaluator._coverage(eligible)
    phase_by_day = primary_evaluator._phase_map(coverage)
    signals = [
        signal
        for signal in primary_evaluator._stream_signals(eligible, phase_by_day)
        if signal["phase"] == "LOCKED_OOS"
    ]
    lookup = ExecutableQuoteLookup(eligible)
    cost_bps = float(policy["position_accounting"]["round_trip_cost_bps"])
    records: list[dict] = []
    for signal in signals:
        records.append(
            _record(
                signal,
                signal,
                path="base",
                quote_lookup=lookup,
                round_trip_cost_bps=cost_bps,
            )
        )
        records.append(
            _record(
                signal,
                signal.get("latency_100ms"),
                path="latency_100ms",
                quote_lookup=lookup,
                round_trip_cost_bps=cost_bps,
            )
        )
    return records


def build_report() -> dict:
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    primary = json.loads(PRIMARY_PATH.read_text(encoding="utf-8"))
    policy_hash = _digest(POLICY_PATH)
    registry_binding = _policy_registry_binding(policy, policy_hash, primary)
    if primary.get("status") in {"PASS", "FAIL"}:
        replay_report(primary, raw_root=RAW_ROOT, project_root=PROJECT_ROOT)
    run_dirs = _report_run_dirs(primary, RAW_ROOT)
    records = load_locked_records(run_dirs, policy)
    inventory, inventory_hash = _source_inventory(run_dirs)
    result = evaluate_partial_fill_stress(
        records,
        primary_status=str(primary.get("status") or ""),
        complete_days=int(primary.get("complete_days") or 0),
        policy=policy,
    )
    return {
        "schema_version": 1,
        "policy": policy["policy"],
        "policy_sha256": policy_hash,
        "registry_binding": registry_binding,
        "primary_report_sha256": _digest(PRIMARY_PATH),
        "source_manifest_count": len(inventory),
        "source_manifest_inventory_sha256": inventory_hash,
        "evaluator_sha256": {
            "partial_fill_stress": _digest(
                PROJECT_ROOT / "quant" / "research" / "partial_fill_stress.py"
            ),
            "report_builder": _digest(Path(__file__)),
            "frozen_primary_evaluator": _digest(
                PROJECT_ROOT / "quant" / "research" / "microstructure.py"
            ),
        },
        **result,
    }


def main() -> None:
    report = build_report()
    OUTPUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] == "FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
