"""Return-free diagnostics for Alpha 015's point-in-time universe."""

from __future__ import annotations

from statistics import median
from typing import Any, Iterable


def summarize_eligible_membership(
    daily_memberships: Iterable[tuple[str, set[str]]],
    *,
    minimum_cross_section: int = 30,
) -> dict[str, Any]:
    """Summarize membership stability without reading any price or return data."""
    if minimum_cross_section < 1:
        raise ValueError("minimum_cross_section must be positive")
    days = sorted((str(day), set(symbols)) for day, symbols in daily_memberships)
    dates = [day for day, _ in days]
    if len(set(dates)) != len(dates):
        raise ValueError("daily memberships must contain one record per UTC date")
    daily_counts = [{"date": day, "eligible_symbols": len(symbols)} for day, symbols in days]
    transitions: list[dict[str, Any]] = []
    for (previous_day, previous), (current_day, current) in zip(days, days[1:]):
        union = previous | current
        shared = previous & current
        entrants = current - previous
        exits = previous - current
        transitions.append(
            {
                "from_date": previous_day,
                "to_date": current_day,
                "shared_symbols": len(shared),
                "entrants": len(entrants),
                "exits": len(exits),
                "jaccard_similarity": len(shared) / len(union) if union else 1.0,
            }
        )
    counts = [row["eligible_symbols"] for row in daily_counts]
    similarities = [row["jaccard_similarity"] for row in transitions]
    return {
        "source_scope": "SEALED_UNIVERSE_MEMBERSHIP_ONLY",
        "return_or_price_data_accessed": False,
        "days_analyzed": len(days),
        "daily_eligible_counts": daily_counts,
        "minimum_eligible_symbols": min(counts) if counts else None,
        "maximum_eligible_symbols": max(counts) if counts else None,
        "median_eligible_symbols": median(counts) if counts else None,
        "minimum_cross_section": minimum_cross_section,
        "minimum_cross_section_met_every_day": bool(counts)
        and min(counts) >= minimum_cross_section,
        "membership_transitions": transitions,
        "minimum_jaccard_similarity": min(similarities) if similarities else None,
        "median_jaccard_similarity": median(similarities) if similarities else None,
    }
