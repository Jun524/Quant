#!/usr/bin/env python3
"""Evaluate Alpha 016 development only from its sealed official archives."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Any, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.research.spot_perp_flow_data import (  # noqa: E402
    SpotPerpFlowDataError,
    file_sha256,
    iter_verified_rows,
)
from quant.research.spot_perp_flow_divergence import (  # noqa: E402
    MINUTE_MS,
    SpotPerpFlowConfig,
    daily_block_bootstrap_excess_return,
    reprice_trades,
    summarize_trades,
)
from quant.research.spot_perp_flow_evaluation import (  # noqa: E402
    SpotPerpFlowEvaluationError,
    extract_market_observations,
    matched_controls_from_references,
    simulate_extracted_flow,
)
from quant.research.spot_perp_flow_protocol import (  # noqa: E402
    build_phase_plan,
    canonical_sha256,
    evaluate_phase_gate,
)


def _parse_boundary(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _rows_from_manifest(manifest: dict[str, Any], *, root: Path, market: str) -> Iterator[dict[str, Any]]:
    if manifest.get("status") != "PUBLISHER_VERIFIED_SOURCE_BYTES_SEALED":
        raise SpotPerpFlowDataError("source manifest is not sealed")
    for item in sorted((entry for entry in manifest.get("files") or [] if entry.get("market") == market), key=lambda entry: entry["date"]):
        path = Path(str(item["archive"])).resolve()
        if not path.is_relative_to(root.resolve()):
            raise SpotPerpFlowDataError("source archive escapes sealed phase directory")
        if file_sha256(path) != item.get("sha256"):
            raise SpotPerpFlowDataError("source archive changed after sealing")
        yield from iter_verified_rows(
            path,
            market=market,
            symbol=str(item["symbol"]),
            day=datetime.fromisoformat(f"{item['date']}T00:00:00+00:00").date(),
        )


def evaluate() -> dict[str, Any]:
    preregistration_path = PROJECT_ROOT / "research/alpha016-preregistration.json"
    source_root = PROJECT_ROOT / "research/alpha016/raw/development"
    manifest_path = source_root / "manifest.json"
    report_path = PROJECT_ROOT / "reports/alpha016-development.json"
    if report_path.exists():
        raise RuntimeError("development report already sealed; refusing overwrite")
    preregistration = json.loads(preregistration_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = build_phase_plan(preregistration, phase="development")
    if manifest.get("plan_sha256") != plan["plan_sha256"]:
        raise RuntimeError("sealed source manifest does not bind the current development plan")
    if manifest.get("chain", {}).get("status") != "PASS" or len(manifest.get("files") or []) != 182:
        raise RuntimeError("source manifest is incomplete or its ID chain did not pass")

    boundaries = [_parse_boundary(value) for value in plan["calendar"]["decision_boundaries_utc"]]
    config = SpotPerpFlowConfig()
    coverage_start = min(boundaries) - config.observation_window_ms
    coverage_end = max(boundaries) + config.entry_delay_ms + config.maximum_reference_delay_ms + config.holding_ms + config.maximum_reference_delay_ms
    spot = extract_market_observations(
        _rows_from_manifest(manifest, root=source_root, market="spot"),
        market="spot",
        boundaries_ms=boundaries,
        coverage_start_ms=coverage_start,
        coverage_end_ms=coverage_end,
        config=config,
        spot_references=True,
    )
    perpetual = extract_market_observations(
        _rows_from_manifest(manifest, root=source_root, market="usd_m_perpetual"),
        market="perpetual",
        boundaries_ms=boundaries,
        coverage_start_ms=coverage_start,
        coverage_end_ms=coverage_end,
        config=config,
    )
    trades = simulate_extracted_flow(spot, perpetual, boundaries_ms=boundaries, config=config)
    controls = matched_controls_from_references(
        trades,
        references=spot["spot_references"],
        boundaries_ms=boundaries,
        config=config,
    )
    base = summarize_trades(trades)
    double_fee = summarize_trades(reprice_trades(trades, fee_per_side_bps=20.0, slippage_per_side_bps=3.0))
    slippage_stress = summarize_trades(reprice_trades(trades, fee_per_side_bps=10.0, slippage_per_side_bps=5.0))
    calendar_start = datetime.fromtimestamp(min(boundaries) / 1000, tz=timezone.utc).date()
    calendar_end_exclusive = datetime.fromtimestamp(max(boundaries) / 1000, tz=timezone.utc).date().replace(day=1)
    if calendar_end_exclusive <= calendar_start:
        raise RuntimeError("development calendar cannot be derived")
    calendar_end_exclusive = datetime(calendar_end_exclusive.year, calendar_end_exclusive.month, 1, tzinfo=timezone.utc)
    if calendar_end_exclusive.month == 12:
        calendar_end_exclusive = calendar_end_exclusive.replace(year=calendar_end_exclusive.year + 1, month=1)
    else:
        calendar_end_exclusive = calendar_end_exclusive.replace(month=calendar_end_exclusive.month + 1)
    bootstrap = daily_block_bootstrap_excess_return(
        trades,
        controls,
        calendar_start_utc=calendar_start,
        calendar_end_exclusive_utc=calendar_end_exclusive.date(),
    )
    causal_features = all(
        spot["feature_last_time_ms"][boundary] is not None
        and perpetual["feature_last_time_ms"][boundary] is not None
        and spot["feature_last_time_ms"][boundary] < boundary
        and perpetual["feature_last_time_ms"][boundary] < boundary
        for boundary in boundaries
    )
    bounded_references = all(
        reference.entry_time_ms >= boundary + config.entry_delay_ms
        and reference.entry_time_ms <= boundary + config.entry_delay_ms + config.maximum_reference_delay_ms
        and reference.exit_time_ms >= reference.entry_time_ms + config.holding_ms
        and reference.exit_time_ms <= reference.entry_time_ms + config.holding_ms + config.maximum_reference_delay_ms
        for boundary, reference in spot["spot_references"].items()
    )
    evidence = {
        "complete_utc_days": len({datetime.fromtimestamp(boundary / 1000, tz=timezone.utc).date() for boundary in boundaries}),
        "natural_virtual_trades": base["trades"],
        "net_return": base["compounded_net_return"],
        "profit_factor": base["profit_factor"],
        "double_fee_net_return": double_fee["compounded_net_return"],
        "slippage_stress_net_return": slippage_stress["compounded_net_return"],
        "random_control_excess_return": bootstrap["observed_excess_return"],
        "bootstrap_ci95_lower": bootstrap["ci95_lower"],
        "data_integrity_pass": spot["coverage"]["status"] == "PASS" and perpetual["coverage"]["status"] == "PASS",
        "lookahead_pass": causal_features and bounded_references,
    }
    report = evaluate_phase_gate(preregistration, phase="development", evidence=evidence)
    report.update(
        {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_manifest": str(manifest_path.relative_to(PROJECT_ROOT)),
            "source_manifest_sha256": _sha256(manifest_path),
            "source_manifest_plan_sha256": manifest["plan_sha256"],
            "preregistration_file_sha256": _sha256(preregistration_path),
            "evaluator_file_sha256": _sha256(Path(__file__)),
            "config": asdict(config),
            "coverage": {"spot": spot["coverage"], "perpetual": perpetual["coverage"]},
            "causal_feature_last_time_ms": {"spot": spot["feature_last_time_ms"], "perpetual": perpetual["feature_last_time_ms"]},
            "reference_bounds_pass": bounded_references,
            "base": base,
            "double_fee": double_fee,
            "slippage_stress": slippage_stress,
            "controls": summarize_trades(controls),
            "bootstrap": bootstrap,
            "orders_submitted": False,
            "live_promotion_eligible": False,
        }
    )
    report["report_sha256"] = canonical_sha256({key: value for key, value in report.items() if key != "report_sha256"})
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    try:
        result = evaluate()
    except (OSError, RuntimeError, SpotPerpFlowDataError, SpotPerpFlowEvaluationError, ValueError) as error:
        raise SystemExit(f"Alpha016 development evaluation failed closed: {error}") from error
    print(json.dumps({"status": result["status"], "report": "reports/alpha016-development.json", "orders_submitted": False}, indent=2))
