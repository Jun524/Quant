"""Canonical parsing and protocol binding for censored liquidation telemetry."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Iterable


DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
PROTOCOL_FILES = (
    "research/liquidation-telemetry-policy.json",
    "quant/research/liquidation_telemetry.py",
    "scripts/collect_liquidation_telemetry.py",
    "quant/research/liquidation_overlay.py",
    "scripts/analyze_liquidation_overlay.py",
    "scripts/open_liquidation_overlay_validation.py",
    "scripts/analyze_liquidation_overlay_validation.py",
)


def protocol_digests(project_root: Path) -> dict[str, str]:
    return {
        relative: sha256((project_root / relative).read_bytes()).hexdigest()
        for relative in PROTOCOL_FILES
    }


def _objects(payload: object) -> Iterable[dict]:
    if isinstance(payload, list):
        for item in payload:
            yield from _objects(item)
    elif isinstance(payload, dict):
        if "data" in payload and payload.get("stream"):
            yield from _objects(payload["data"])
        else:
            yield payload


def liquidation_events(payload: object, *, symbols: set[str]) -> list[dict]:
    """Return valid force-order snapshots without claiming feed completeness."""

    events: list[dict] = []
    for item in _objects(payload):
        order = item.get("o")
        if item.get("e") != "forceOrder" or not isinstance(order, dict):
            continue
        symbol = str(order.get("s") or "").upper()
        side = str(order.get("S") or "").upper()
        if symbol not in symbols or side not in {"BUY", "SELL"}:
            continue
        try:
            event_time_ms = int(item["E"])
            trade_time_ms = int(order["T"])
            original_qty = float(order["q"])
            accumulated_qty = float(order["z"])
            last_qty = float(order["l"])
            average_price = float(order["ap"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            event_time_ms <= 0
            or trade_time_ms <= 0
            or original_qty < 0
            or accumulated_qty < 0
            or last_qty < 0
            or average_price <= 0
        ):
            continue
        fingerprint = (
            f"{symbol}|{side}|{trade_time_ms}|{original_qty:.16g}|"
            f"{accumulated_qty:.16g}|{average_price:.16g}"
        )
        events.append(
            {
                "kind": "observed_liquidation_snapshot",
                "symbol": symbol,
                "side": side,
                "liquidated_position_side": "SHORT" if side == "BUY" else "LONG",
                "event_time_ms": event_time_ms,
                "trade_time_ms": trade_time_ms,
                "original_qty": original_qty,
                "accumulated_filled_qty": accumulated_qty,
                "last_filled_qty": last_qty,
                "average_price": average_price,
                "observed_snapshot_notional": average_price * accumulated_qty,
                "fingerprint": sha256(fingerprint.encode()).hexdigest(),
                "feed_completeness": "CENSORED_LATEST_SNAPSHOT_PER_SYMBOL_PER_1000MS",
            }
        )
    return events


def mark_prices(payload: object, *, symbols: set[str]) -> list[dict]:
    rows: list[dict] = []
    for item in _objects(payload):
        symbol = str(item.get("s") or "").upper()
        if symbol not in symbols:
            continue
        try:
            event_time_ms = int(item["E"])
            mark_price = float(item["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if event_time_ms <= 0 or mark_price <= 0:
            continue
        rows.append(
            {
                "kind": "mark_price",
                "symbol": symbol,
                "event_time_ms": event_time_ms,
                "mark_price": mark_price,
            }
        )
    return rows
