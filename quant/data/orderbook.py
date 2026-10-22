"""Sequence-safe local order-book primitives for Binance USD-M research."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping, Sequence


class OrderBookSequenceError(RuntimeError):
    """Raised when a diff stream can no longer be joined to the local book."""


def _levels(values: Sequence[Sequence[str | float]]) -> dict[float, float]:
    result: dict[float, float] = {}
    for raw_price, raw_quantity, *_ in values:
        price, quantity = float(raw_price), float(raw_quantity)
        if not isfinite(price) or not isfinite(quantity) or price <= 0 or quantity < 0:
            raise ValueError("book price must be positive and quantity non-negative")
        if quantity:
            result[price] = quantity
    return result


@dataclass
class LocalOrderBook:
    """Apply absolute-quantity depth diffs with Binance sequence checks."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    last_update_id: int | None = None
    bridged: bool = False
    updates_applied: int = 0

    def load_snapshot(self, snapshot: Mapping) -> None:
        self.bids = _levels(snapshot.get("bids") or [])
        self.asks = _levels(snapshot.get("asks") or [])
        self.last_update_id = int(snapshot["lastUpdateId"])
        self.bridged = False
        self.updates_applied = 0
        if not self.bids or not self.asks:
            raise ValueError("snapshot must contain non-empty bids and asks")

    @staticmethod
    def _update_side(side: dict[float, float], changes: Sequence[Sequence[str | float]]) -> None:
        for raw_price, raw_quantity, *_ in changes:
            price, quantity = float(raw_price), float(raw_quantity)
            if price <= 0 or quantity < 0 or not isfinite(price) or not isfinite(quantity):
                raise ValueError("book update price must be positive and quantity non-negative")
            if quantity == 0:
                side.pop(price, None)
            else:
                side[price] = quantity

    def apply(self, event: Mapping) -> bool:
        """Apply one depth event; return False only for a stale event."""
        if self.last_update_id is None:
            raise RuntimeError("load_snapshot must be called before apply")
        first_update = int(event["U"])
        final_update = int(event["u"])
        if final_update < self.last_update_id:
            return False
        if not self.bridged:
            if not first_update <= self.last_update_id <= final_update:
                raise OrderBookSequenceError(
                    f"event {first_update}..{final_update} does not bridge snapshot {self.last_update_id}"
                )
            self.bridged = True
        else:
            previous_update = int(event["pu"])
            if previous_update != self.last_update_id:
                raise OrderBookSequenceError(
                    f"event pu={previous_update} does not match previous u={self.last_update_id}"
                )
        self._update_side(self.bids, event.get("b") or [])
        self._update_side(self.asks, event.get("a") or [])
        self.last_update_id = final_update
        self.updates_applied += 1
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            raise OrderBookSequenceError("local order book is empty or crossed")
        return True

    def features(self, levels: int = 5) -> dict[str, float | int]:
        if not self.bridged or self.last_update_id is None:
            raise RuntimeError("book must be bridged before features are available")
        if levels < 1:
            raise ValueError("levels must be positive")
        bids = sorted(self.bids.items(), reverse=True)[:levels]
        asks = sorted(self.asks.items())[:levels]
        if not bids or not asks:
            raise RuntimeError("book has no top levels")
        bid_price, bid_qty = bids[0]
        ask_price, ask_qty = asks[0]
        mid = (bid_price + ask_price) / 2
        bid_depth = sum(quantity for _, quantity in bids)
        ask_depth = sum(quantity for _, quantity in asks)
        depth = bid_depth + ask_depth
        touch_depth = bid_qty + ask_qty
        microprice = (ask_price * bid_qty + bid_price * ask_qty) / touch_depth
        return {
            "last_update_id": self.last_update_id,
            "updates_applied": self.updates_applied,
            "best_bid": bid_price,
            "best_bid_qty": bid_qty,
            "best_ask": ask_price,
            "best_ask_qty": ask_qty,
            "mid": mid,
            "spread_bps": (ask_price - bid_price) / mid * 10_000,
            "touch_imbalance": (bid_qty - ask_qty) / touch_depth,
            "depth_levels": levels,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "depth_imbalance": (bid_depth - ask_depth) / depth,
            "microprice": microprice,
            "microprice_edge_bps": (microprice - mid) / mid * 10_000,
        }
