"""Immutable prior-day universe bindings for deferred Alpha 015.

The lock is built only after the existing forward-universe protocol is READY.
It maps 180 formation snapshots D0..D179 to 180 evaluation days D1..D180,
so every return day has an exact, sealed prior-day membership set.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from quant.data.snapshots import file_sha256, snapshot_manifest_path, validate_sealed_snapshot
from quant.data.universe_history import assess_forward_universe_history


class Alpha015ActivationLockError(RuntimeError):
    pass


def _canonical_sha256(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_alpha015_activation_lock(
    *,
    snapshot_directory: Path,
    universe_policy: dict[str, Any],
    expected_protocol: dict[str, str],
    candidate_policy: dict[str, Any],
    amendment: dict[str, Any],
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Revalidate source bytes and build the exact 120/60 calendar binding."""
    as_of = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    requirements = candidate_policy.get("activation_requirements") or {}
    required = int(requirements.get("point_in_time_universe_consecutive_days") or 0)
    clarification = amendment.get("clarification") or {}
    if (
        required != 180
        or int(clarification.get("universe_snapshot_days") or 0) != required
        or int(clarification.get("evaluation_days") or 0) != required
        or int(clarification.get("development_days") or 0) != 120
        or int(clarification.get("locked_validation_days") or 0) != 60
    ):
        raise Alpha015ActivationLockError("activation calendar contract mismatch")
    if (
        amendment.get("candidate") != candidate_policy.get("candidate")
        or amendment.get("status") != "PRE_DATA_FAIL_CLOSED_AMENDMENT"
        or (amendment.get("evidence_state") or {}).get("real_return_archives_downloaded") is not False
    ):
        raise Alpha015ActivationLockError("activation amendment is not applicable")

    coverage = assess_forward_universe_history(
        snapshot_directory,
        expected_protocol=expected_protocol,
        policy=universe_policy,
        as_of=as_of,
    )
    if coverage["status"] != "READY" or int(coverage["consecutive_days_ending_latest"]) < required:
        raise Alpha015ActivationLockError("point-in-time universe is not ready")
    if coverage["invalid_snapshots"] or coverage["duplicate_days"] or coverage["chain_failures"]:
        raise Alpha015ActivationLockError("universe source integrity failed")

    formation_days = [date.fromisoformat(value) for value in coverage["valid_days"][:required]]
    if len(formation_days) != required or any(
        right - left != timedelta(days=1)
        for left, right in zip(formation_days, formation_days[1:])
    ):
        raise Alpha015ActivationLockError("the first 180 formation days are not consecutive")

    sealed_by_day: dict[date, dict[str, Any]] = {}
    for snapshot in sorted(snapshot_directory.glob("universe-*.json")):
        try:
            sealed = validate_sealed_snapshot(snapshot, expected_protocol=expected_protocol)
        except ValueError:
            continue
        day = sealed["observed_at"].date()
        if day not in set(formation_days):
            continue
        if day in sealed_by_day:
            raise Alpha015ActivationLockError("multiple sealed snapshots exist for a formation day")
        sealed_by_day[day] = {"snapshot": snapshot, **sealed}
    if set(sealed_by_day) != set(formation_days):
        raise Alpha015ActivationLockError("a formation day is not bound to a sealed snapshot")

    bindings: list[dict[str, Any]] = []
    for membership_day in formation_days:
        sealed = sealed_by_day[membership_day]
        payload = sealed["payload"]
        eligible = sorted(
            str(item["symbol"])
            for item in payload.get("instruments") or []
            if item.get("eligible") is True
        )
        if len(eligible) != int(sealed["manifest"].get("eligible_count") or -1):
            raise Alpha015ActivationLockError("eligible-symbol count differs from manifest")
        manifest_path = snapshot_manifest_path(sealed["snapshot"])
        evaluation_day = membership_day + timedelta(days=1)
        bindings.append(
            {
                "evaluation_date": evaluation_day.isoformat(),
                "membership_snapshot_date": membership_day.isoformat(),
                "snapshot_path": str(sealed["snapshot"].relative_to(snapshot_directory.parent.parent)),
                "snapshot_sha256": file_sha256(sealed["snapshot"]),
                "manifest_path": str(manifest_path.relative_to(snapshot_directory.parent.parent)),
                "manifest_sha256": file_sha256(manifest_path),
                "eligible_symbols": eligible,
                "eligible_symbols_sha256": _canonical_sha256(eligible),
                "eligible_count": len(eligible),
            }
        )

    development = [item["evaluation_date"] for item in bindings[:120]]
    validation = [item["evaluation_date"] for item in bindings[120:]]
    split_sha256 = sha256(
        ("development\n" + "\n".join(development) + "\nlocked_validation\n" + "\n".join(validation)).encode()
    ).hexdigest()
    body = {
        "schema_version": 1,
        "candidate": candidate_policy["candidate"],
        "status": "LOCKED_FOR_PREREGISTRATION_ONLY",
        "source_cutoff_at_utc": sealed_by_day[formation_days[-1]]["observed_at"].isoformat(),
        "formation_snapshot_count": len(formation_days),
        "evaluation_day_count": len(bindings),
        "formation_start": formation_days[0].isoformat(),
        "formation_end": formation_days[-1].isoformat(),
        "evaluation_start": development[0],
        "evaluation_end": validation[-1],
        "development": development,
        "locked_validation": validation,
        "split_sha256": split_sha256,
        "validation_open": False,
        "universe_protocol_sha256": expected_protocol,
        "bindings": bindings,
        "warning": "This lock permits preregistration only; it is not evidence of edge or permission to trade.",
    }
    return {**body, "lock_sha256": _canonical_sha256(body)}
