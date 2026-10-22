"""Audit the pre-registered, research-only AOA transfer observation."""

from __future__ import annotations

from hashlib import sha256
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from quant.research.result_loader import load_freqtrade_result


STRATEGY = "AoaDistilledPolicyPrototype"
EXPECTED_NOTE_PREFIX = "AOA_FORWARD_LOCKED_V1 fee_per_fill=0.0005 prereg_sha256="


def _digest(path: Path) -> str:
    value = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _decode_object(payload: bytes, label: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def audit_aoa_forward(
    *,
    project_root: Path,
    preregistration_path: Path,
    result_path: Path,
    data_paths: list[Path],
) -> dict[str, Any]:
    preregistration_path = preregistration_path.resolve()
    result_path = result_path.resolve()
    preregistration_bytes = preregistration_path.read_bytes()
    preregistration_sha256 = sha256(preregistration_bytes).hexdigest()
    preregistration = _decode_object(preregistration_bytes, "AOA preregistration")
    if (
        preregistration.get("schema_version") != 1
        or preregistration.get("policy") != "AoaDistilledPolicyPrototype"
        or preregistration.get("status_before_open") != "LOCKED_UNSEEN"
        or preregistration.get("decision_rule", {}).get("pass_allowed") is not False
    ):
        raise ValueError("AOA forward preregistration identity is invalid")

    frozen = preregistration.get("frozen_artifacts")
    if not isinstance(frozen, dict) or not frozen:
        raise ValueError("AOA forward preregistration has no frozen artifacts")
    frozen_sources: list[dict[str, str]] = []
    for relative, expected in frozen.items():
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise ValueError("AOA frozen artifact inventory is invalid")
        path = (project_root / relative).resolve()
        if not path.is_relative_to(project_root.resolve()):
            raise ValueError("AOA frozen artifact escapes the project root")
        actual = _digest(path)
        if expected != f"sha256:{actual}":
            raise ValueError(f"AOA frozen artifact changed: {relative}")
        frozen_sources.append({"path": str(path), "sha256": actual})

    meta_path = result_path.with_suffix(".meta.json")
    meta_bytes = meta_path.read_bytes()
    metadata = _decode_object(meta_bytes, "Freqtrade metadata")
    strategy_metadata = metadata.get(STRATEGY)
    if not isinstance(strategy_metadata, dict):
        raise ValueError("AOA strategy metadata is missing")
    expected_note = EXPECTED_NOTE_PREFIX + preregistration_sha256
    if (
        strategy_metadata.get("timeframe") != "5m"
        or strategy_metadata.get("timeframe_detail") is not None
        or strategy_metadata.get("notes") != expected_note
    ):
        raise ValueError("AOA result metadata is not bound to the locked protocol")

    with ZipFile(result_path) as archive:
        names = archive.namelist()
        result_name = next(
            name
            for name in names
            if name.endswith(".json") and not name.endswith("_config.json")
        )
        config_name = next(name for name in names if name.endswith("_config.json"))
        source_name = next(name for name in names if name.endswith(f"_{STRATEGY}.py"))
        result_document = _decode_object(archive.read(result_name), "Freqtrade result")
        embedded_config = _decode_object(archive.read(config_name), "Freqtrade config")
        embedded_strategy_sha256 = sha256(archive.read(source_name)).hexdigest()

    expected_strategy_sha256 = str(
        frozen.get("user_data/strategies/AoaDistilledPolicyPrototype.py") or ""
    ).removeprefix("sha256:")
    if embedded_strategy_sha256 != expected_strategy_sha256:
        raise ValueError("AOA result embedded a strategy different from the frozen strategy")
    if (
        embedded_config.get("strategy") != STRATEGY
        or embedded_config.get("timeframe") != "5m"
        or embedded_config.get("dry_run") is not True
        or embedded_config.get("trading_mode") != "futures"
        or embedded_config.get("margin_mode") != "isolated"
        or embedded_config.get("max_open_trades") != 1
        or embedded_config.get("exchange", {}).get("pair_whitelist")
        != ["BTC/USDT:USDT"]
    ):
        raise ValueError("AOA embedded execution configuration is invalid")

    strategy_result = result_document.get("strategy", {}).get(STRATEGY)
    if not isinstance(strategy_result, dict):
        raise ValueError("AOA strategy result is missing")
    timerange = str(preregistration.get("locked_timerange") or "")
    expected_start = f"{timerange[:4]}-{timerange[4:6]}-{timerange[6:8]} 00:00:00"
    expected_end = f"{timerange[9:13]}-{timerange[13:15]}-{timerange[15:17]} 00:00:00"
    sample_start = datetime.strptime(timerange[:8], "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    sample_end = datetime.strptime(timerange[9:17], "%Y%m%d").replace(
        tzinfo=timezone.utc
    )
    registered_at = datetime.fromisoformat(
        str(preregistration.get("registered_at_utc") or "").replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    registration_precedes_sample = registered_at <= sample_start
    if (
        strategy_result.get("timerange") != timerange
        or strategy_result.get("backtest_start") != expected_start
        or strategy_result.get("backtest_end") != expected_end
        or strategy_result.get("enable_protections") is not True
    ):
        raise ValueError("AOA result does not cover the exact locked interval with protections")

    metrics = load_freqtrade_result(result_path, STRATEGY)
    data_sources = []
    for source in data_paths:
        source = source.resolve()
        if not source.is_file():
            raise ValueError(f"AOA input data is missing: {source}")
        data_sources.append(
            {"path": str(source), "bytes": source.stat().st_size, "sha256": _digest(source)}
        )
    if not data_sources:
        raise ValueError("AOA forward audit requires its input data inventory")

    trades = int(metrics["organic"]["trades"])
    if registration_precedes_sample:
        status = (
            "INTERIM_FORWARD_WINDOW_COMPLETE_NO_ACTIVITY"
            if trades == 0
            else "INTERIM_FORWARD_WINDOW_COMPLETE_INSUFFICIENT_SAMPLE"
        )
        sample_role = "pre_registered_interim_forward_observation_not_final_oos"
    else:
        status = (
            "HISTORICAL_HOLDOUT_COMPLETE_NO_ACTIVITY"
            if trades == 0
            else "HISTORICAL_HOLDOUT_COMPLETE_INSUFFICIENT_SAMPLE"
        )
        sample_role = "post_period_locked_unseen_holdout_not_prospective_forward"
    return {
        "schema_version": 1,
        "candidate": STRATEGY,
        "status": status,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "sample_role": sample_role,
        "integrity": {
            "status": "PASS",
            "frozen_artifacts_current": True,
            "exact_timerange": timerange,
            "registered_at_utc": registered_at.isoformat(),
            "sample_started_at_utc": sample_start.isoformat(),
            "sample_ended_at_utc": sample_end.isoformat(),
            "registration_precedes_sample": registration_precedes_sample,
            "fee_per_fill": 0.0005,
            "fee_binding": "Freqtrade --notes plus metadata hash binding; zero trades make fee arithmetic inapplicable",
            "embedded_strategy_matches_frozen_sha256": True,
            "embedded_config_research_only": True,
            "unexpected_live_order_submission": False,
        },
        "sources": {
            "preregistration": {
                "path": str(preregistration_path),
                "sha256": preregistration_sha256,
            },
            "frozen_artifacts": frozen_sources,
            "backtest": {"path": str(result_path), "sha256": _digest(result_path)},
            "backtest_metadata": {"path": str(meta_path), "sha256": sha256(meta_bytes).hexdigest()},
            "market_data": data_sources,
        },
        "metrics": {
            "organic_trades": trades,
            "forced_exits": int(metrics["forced_exit"]["trades"]),
            "net_pnl": float(metrics["organic"]["net_profit"]),
            "net_return": float(metrics["organic"]["net_return"]),
            "profit_factor": metrics["organic"]["profit_factor"],
            "maximum_drawdown": float(metrics["max_drawdown"]),
            "complete_months": int(metrics["monthly"]["complete_months"]),
        },
        "decision": {
            "pass_allowed": False,
            "fail_fast_triggered": False,
            "minimum_organic_trades_for_future_promotion": 100,
            "minimum_calendar_months_for_future_promotion": 12,
            "interpretation": (
                "The locked 15-day holdout produced no signals that became trades. "
                + (
                    "It was registered before the sample began, but remains too short to promote."
                    if registration_precedes_sample
                    else "It was registered after the interval ended, so it is not prospective forward evidence."
                )
                + " It neither supports nor refutes profitability and cannot be promoted."
            ),
        },
    }

