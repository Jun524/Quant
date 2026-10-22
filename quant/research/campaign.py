"""Resource and connection preflight for bounded microstructure campaigns."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

from quant.research.microstructure import _timing_status, _verify_run
from quant.research.protocol import protocol_digests


def microstructure_run_audit(run_dir: Path, *, project_root: Path | None = None) -> dict:
    """Audit one sealed run without loading its event stream into memory."""
    try:
        integrity = _verify_run(run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "run_id": run_dir.name,
            "status": "FAIL",
            "checks": {"manifest_integrity": False},
            "error": f"{type(exc).__name__}: {exc}",
        }

    result = manifest.get("result") or {}
    depth = result.get("depth") or {}
    trades = result.get("trades") or {}
    timing = _timing_status(manifest)
    current_protocol = protocol_digests(project_root or Path(__file__).resolve().parents[2])
    critical_checks = {
        "manifest_integrity": True,
        "current_protocol": integrity.get("protocol_sha256") == current_protocol,
    }
    quality_checks = {
        "timing_healthy": bool(timing["healthy"]),
        "depth_stream_present": int(depth.get("connections") or 0) >= 1 and int(depth.get("feature_rows") or 0) > 0,
        "trade_stream_present": int(trades.get("connections") or 0) >= 1 and int(trades.get("aggregate_trades") or 0) > 0,
        "zero_depth_reconnects": int(depth.get("reconnects") or 0) == 0,
        "zero_trade_reconnects": int(trades.get("reconnects") or 0) == 0,
        "no_stream_errors": depth.get("last_error") is None and trades.get("last_error") is None,
        "sequence_recovery_clean": depth.get("last_sequence_error") is None and int(depth.get("sequence_resyncs") or 0) <= 1,
        "zero_stale_depth_events": int(depth.get("stale_depth_events") or 0) == 0,
    }
    if not all(critical_checks.values()):
        status = "FAIL"
    elif not all(quality_checks.values()):
        status = "QUALITY_WARNING"
    else:
        status = "PASS"
    return {
        "run_id": integrity["run_id"],
        "status": status,
        "checks": {**critical_checks, **quality_checks},
        "requested_duration_seconds": int(result.get("requested_duration_seconds") or 0),
        "active_wall_seconds": timing["wall_elapsed_seconds"],
        "timing_source": timing["source"],
        "depth_reconnects": int(depth.get("reconnects") or 0),
        "trade_reconnects": int(trades.get("reconnects") or 0),
        "sequence_resyncs": int(depth.get("sequence_resyncs") or 0),
        "files_verified": integrity["files_verified"],
    }


def latest_microstructure_run_audit(root: Path, *, project_root: Path | None = None) -> dict | None:
    manifests = sorted(root.glob("*/manifest.json"))
    if not manifests:
        return None
    return microstructure_run_audit(manifests[-1].parent, project_root=project_root)


def microstructure_campaign_preflight(
    root: Path,
    *,
    campaign_hours: int = 456,
    minimum_free_after_gib: float = 25.0,
    safety_multiplier: float = 1.5,
    disk_path: Path | None = None,
) -> dict:
    if campaign_hours < 1 or minimum_free_after_gib < 0 or safety_multiplier < 1:
        raise ValueError("campaign hours/reserve must be valid and safety_multiplier >= 1")
    run_dirs = sorted(path.parent for path in root.glob("*/manifest.json"))
    current_protocol = protocol_digests(Path(__file__).resolve().parents[2])
    samples: list[dict] = []
    for run_dir in run_dirs:
        integrity = _verify_run(run_dir)
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        duration = int((manifest.get("result") or {}).get("requested_duration_seconds") or 0)
        total_bytes = sum(int(item.get("bytes") or 0) for item in manifest.get("files") or [])
        result = manifest.get("result") or {}
        depth = result.get("depth") or {}
        trades = result.get("trades") or {}
        sequence_resyncs = int(depth.get("sequence_resyncs") or 0)
        timing_status = _timing_status(manifest)
        wall_elapsed = timing_status["wall_elapsed_seconds"]
        timing_healthy = bool(timing_status["healthy"])
        samples.append(
            {
                "run_id": integrity["run_id"],
                "duration_seconds": duration,
                "compressed_bytes": total_bytes,
                "bytes_per_second": total_bytes / duration if duration else None,
                "depth_reconnects": int(depth.get("reconnects") or 0),
                "trade_reconnects": int(trades.get("reconnects") or 0),
                "sequence_resyncs": sequence_resyncs,
                "wall_elapsed_seconds": wall_elapsed,
                "timing_healthy": timing_healthy,
                "timing_source": timing_status["source"],
                "current_protocol": integrity.get("protocol_sha256") == current_protocol,
                "healthy": bool(
                    duration >= 300
                    and timing_healthy
                    and int(depth.get("connections") or 0) >= 1
                    and int(trades.get("connections") or 0) >= 1
                    and int(depth.get("reconnects") or 0) == 0
                    and int(trades.get("reconnects") or 0) == 0
                    and depth.get("last_error") is None
                    and trades.get("last_error") is None
                    and depth.get("last_sequence_error") is None
                    and sequence_resyncs <= 1
                    and int(depth.get("feature_rows") or 0) > 0
                    and int(trades.get("aggregate_trades") or 0) > 0
                ),
            }
        )
    rates = [float(item["bytes_per_second"]) for item in samples if item["bytes_per_second"] is not None]
    rate = max(rates, default=0.0)
    estimated_bytes = rate * campaign_hours * 3600 * safety_multiplier
    free_gib = shutil.disk_usage(disk_path or root).free / 1024**3
    estimated_gib = estimated_bytes / 1024**3
    checks = {
        "manifest_integrity": bool(run_dirs),
        "healthy_current_protocol_five_minute_sample": any(
            item["healthy"] and item["current_protocol"] for item in samples
        ),
        "storage_rate_available": rate > 0,
        "disk_reserve_after_estimate": free_gib - estimated_gib >= minimum_free_after_gib,
    }
    return {
        "status": "READY" if all(checks.values()) else "NOT_READY",
        "campaign_hours": campaign_hours,
        "campaign_days": campaign_hours / 24,
        "sample_runs": samples,
        "observed_max_bytes_per_second": rate,
        "safety_multiplier": safety_multiplier,
        "estimated_storage_gib": estimated_gib,
        "current_free_gib": free_gib,
        "minimum_free_after_gib": minimum_free_after_gib,
        "projected_free_after_gib": free_gib - estimated_gib,
        "checks": checks,
        "warning": "Estimate uses the maximum observed compressed rate times a safety multiplier; volatile markets can still exceed it.",
    }
