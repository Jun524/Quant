"""Additive execution-cost stress that cannot rescue a primary experiment."""

from __future__ import annotations


def evaluate_alpha007_external_cost_stress(
    primary: dict, calibration: dict, policy: dict
) -> dict:
    if policy.get("candidate") != "ALPHA_007_IMBALANCE_GUIDED_MAKER":
        raise ValueError("external cost policy candidate mismatch")
    source = policy.get("source_calibration") or {}
    observed = float(
        ((calibration.get("per_symbol") or {}).get("BTCUSDT") or {}).get(
            "conservative_all_in_execution_drag_bps"
        )
        or 0
    )
    if (
        calibration.get("status") != "CALIBRATION_PASS"
        or calibration.get("promotion_eligible") is not False
        or abs(observed - float(source["btc_observed_all_in_entry_drag_bps"])) > 1e-12
        or calibration.get("cost_reference", {}).get("includes_fee") is not True
    ):
        raise ValueError("execution calibration does not match the external stress policy")
    config = primary.get("configuration") or {}
    base_bps = (float(config.get("entry_fee") or 0) + float(config.get("exit_fee") or 0)) * 10_000
    if abs(base_bps - float(policy["base_round_trip_cost_bps"])) > 1e-12:
        raise ValueError("Alpha #7 base costs do not match the external stress policy")
    additional = float(policy["additional_deduction_bps"]) / 10_000
    locked = (primary.get("phases") or {}).get("LOCKED_OOS") or {}
    mean_60 = (locked.get("net_markout_60s") or {}).get("mean")
    bootstrap = locked.get("hour_block_bootstrap_95pct_lower_mean_60s")
    latency = (locked.get("latency_100ms_net_markout_60s") or {}).get("mean")

    def stressed(value):
        return float(value) - additional if value is not None else None

    stressed_mean = stressed(mean_60)
    stressed_bootstrap = stressed(bootstrap)
    stressed_latency = stressed(latency)
    checks = [
        {
            "name": "primary_alpha007_pass",
            "passed": primary.get("status") == "PASS",
            "actual": primary.get("status"),
        },
        {
            "name": "ten_bps_net_60s_mean_positive",
            "passed": stressed_mean is not None and stressed_mean > 0,
            "actual": stressed_mean,
        },
        {
            "name": "ten_bps_hour_block_bootstrap_95pct_lower_positive",
            "passed": stressed_bootstrap is not None and stressed_bootstrap > 0,
            "actual": stressed_bootstrap,
        },
        {
            "name": "ten_bps_plus_100ms_latency_mean_positive",
            "passed": stressed_latency is not None and stressed_latency > 0,
            "actual": stressed_latency,
        },
    ]
    if int(primary.get("complete_days") or 0) < 17:
        status = "ACCUMULATING"
    else:
        status = "PASS" if all(item["passed"] for item in checks) else "FAIL"
    return {
        "candidate": policy["candidate"],
        "status": status,
        "promotion_eligible": False,
        "base_round_trip_cost_bps": base_bps,
        "stressed_round_trip_cost_bps": float(policy["stressed_round_trip_cost_bps"]),
        "additional_deduction_bps": float(policy["additional_deduction_bps"]),
        "stressed_locked_net_60s_mean": stressed_mean,
        "stressed_locked_hour_block_bootstrap_95pct_lower": stressed_bootstrap,
        "stressed_locked_plus_100ms_latency_mean": stressed_latency,
        "checks": checks,
    }
