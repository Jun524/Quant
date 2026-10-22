"""Operational-only review checkpoints for Alpha 015 universe collection."""

from __future__ import annotations

from typing import Any


_MILESTONES = (
    (
        30,
        "COLLECTION_INTEGRITY_REVIEW",
        "Review sealed-chain continuity, fixed-window collection, and endpoint coverage.",
    ),
    (
        60,
        "UNIVERSE_STABILITY_REVIEW",
        "Review membership continuity and collection quality before committing further research time.",
    ),
    (
        90,
        "PRE_ACTIVATION_RESEARCH_REVIEW",
        "Decide whether the deferred candidate remains worth completing without inspecting returns.",
    ),
)

_INTEGRITY_FAILURES = frozenset(
    {"DATA_INTEGRITY_FAIL", "ACCUMULATING_WITH_GAPS", "MISSING"}
)


def build_alpha015_collection_milestones(
    *, current_days: int, universe_status: str
) -> list[dict[str, Any]]:
    """Describe early review points without weakening Alpha 015 activation.

    The frozen Alpha 015 contract still requires 180 current-protocol days.
    This observer deliberately has no authority to authorize archive access,
    experiment registration, or live orders.
    """
    if current_days < 0:
        raise ValueError("current_days must be non-negative")
    integrity_ok = universe_status not in _INTEGRITY_FAILURES
    milestones: list[dict[str, Any]] = []
    for threshold, name, scope in _MILESTONES:
        status = (
            "BLOCKED_DATA_INTEGRITY"
            if not integrity_ok
            else "REVIEW_DUE"
            if current_days >= threshold
            else "PENDING"
        )
        milestones.append(
            {
                "days": threshold,
                "name": name,
                "status": status,
                "days_until_due": max(0, threshold - current_days),
                "review_scope": scope,
                "promotion_capability": "DIAGNOSTIC_ONLY",
                "return_data_authorized": False,
                "experiment_registration_authorized": False,
                "live_order_authorization": "CLOSED",
            }
        )
    return milestones
