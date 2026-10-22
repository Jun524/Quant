"""Point-in-time universe selection without survivorship leakage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass(frozen=True)
class UniversePolicy:
    min_quote_volume_24h: float = 10_000_000.0
    min_listing_age_days: int = 30
    min_candles: int = 500
    max_spread_bps: float = 15.0
    min_orderbook_depth: float = 50_000.0


@dataclass(frozen=True)
class InstrumentSnapshot:
    """What was known about one tradable contract at ``observed_at`` only."""

    pair: str
    observed_at: datetime
    listed_at: datetime
    quote_volume_24h: float
    candle_count: int
    data_continuous: bool
    spread_bps: float | None
    orderbook_depth: float | None
    active: bool = True
    data_fresh: bool = True


@dataclass(frozen=True)
class UniverseDecision:
    as_of: datetime
    selected: tuple[str, ...]
    rejected: dict[str, tuple[str, ...]]


def _rejection_reasons(snapshot: InstrumentSnapshot, as_of: datetime, policy: UniversePolicy) -> list[str]:
    reasons: list[str] = []
    if not snapshot.active:
        reasons.append("inactive")
    if snapshot.listed_at > as_of - timedelta(days=policy.min_listing_age_days):
        reasons.append("listing_age")
    if snapshot.quote_volume_24h < policy.min_quote_volume_24h:
        reasons.append("quote_volume")
    if snapshot.candle_count < policy.min_candles:
        reasons.append("candle_history")
    if not snapshot.data_continuous:
        reasons.append("data_continuity")
    if not snapshot.data_fresh:
        reasons.append("data_freshness")
    if snapshot.spread_bps is None or snapshot.spread_bps > policy.max_spread_bps:
        reasons.append("spread")
    if snapshot.orderbook_depth is None or snapshot.orderbook_depth < policy.min_orderbook_depth:
        reasons.append("orderbook_depth")
    return reasons


def select_universe(
    snapshots: Iterable[InstrumentSnapshot], *, as_of: datetime, policy: UniversePolicy = UniversePolicy()
) -> UniverseDecision:
    """Select using the latest snapshot on or before ``as_of`` for each pair.

    A snapshot dated after ``as_of`` is never considered. This point-in-time
    rule prevents a currently liquid, surviving contract from appearing in an
    older backtest merely because it exists today.
    """
    latest: dict[str, InstrumentSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.observed_at > as_of:
            continue
        current = latest.get(snapshot.pair)
        if current is None or snapshot.observed_at > current.observed_at:
            latest[snapshot.pair] = snapshot

    selected: list[str] = []
    rejected: dict[str, tuple[str, ...]] = {}
    for pair, snapshot in sorted(latest.items()):
        reasons = _rejection_reasons(snapshot, as_of, policy)
        if reasons:
            rejected[pair] = tuple(reasons)
        else:
            selected.append(pair)
    return UniverseDecision(as_of=as_of, selected=tuple(selected), rejected=rejected)


def build_universe_history(
    snapshots: Iterable[InstrumentSnapshot], *, as_of_dates: Iterable[datetime], policy: UniversePolicy = UniversePolicy()
) -> dict[str, list[str]]:
    """Return reproducible point-in-time pair lists keyed by ISO date."""
    snapshot_list = list(snapshots)
    return {
        as_of.date().isoformat(): list(select_universe(snapshot_list, as_of=as_of, policy=policy).selected)
        for as_of in sorted(as_of_dates)
    }
