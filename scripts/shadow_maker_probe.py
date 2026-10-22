#!/usr/bin/env python3
"""Observe a hypothetical Binance USD-M post-only order without placing it.

This command uses public market-data endpoints only.  It accepts no API key,
never calls a trade endpoint, and never submits or cancels an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.shadow_maker import estimate_shadow_fill


BASE_URL = "https://fapi.binance.com"


def get_json(path: str, params: dict, *, attempts: int = 3) -> object:
    url = f"{BASE_URL}{path}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "quant-shadow-maker/1.0"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        except HTTPError as error:
            # Binance 418 is an active IP ban and 429 is a rate-limit warning.
            # Retrying either can extend/escalate the ban, so fail immediately.
            if error.code in {418, 429}:
                raise
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def best_book(symbol: str) -> dict[str, object]:
    payload = get_json("/fapi/v1/depth", {"symbol": symbol, "limit": 100})
    if not isinstance(payload, dict) or not payload.get("bids") or not payload.get("asks"):
        raise RuntimeError("Binance depth response did not contain bids and asks")
    bids = [(float(price), float(quantity)) for price, quantity in payload["bids"]]
    asks = [(float(price), float(quantity)) for price, quantity in payload["asks"]]
    bid_price, bid_qty = bids[0]
    ask_price, ask_qty = asks[0]
    return {
        "bid_price": bid_price,
        "bid_qty": bid_qty,
        "ask_price": ask_price,
        "ask_qty": ask_qty,
        "bids": bids,
        "asks": asks,
        "mid": (bid_price + ask_price) / 2,
        "last_update_id": int(payload["lastUpdateId"]),
    }


def queue_ahead_at_limit(*, side: str, limit_price: float, book: dict[str, object]) -> float:
    """Return displayed quantity already resting at the exact replay price.

    A price beyond the 100-level snapshot is rejected instead of assuming an
    empty queue.  A valid price gap inside the observed range has zero queue.
    """
    levels = book["bids"] if side == "buy" else book["asks"]
    assert isinstance(levels, list) and levels
    for price, quantity in levels:
        if abs(float(price) - limit_price) <= max(1e-12, limit_price * 1e-12):
            return float(quantity)
    covered = (
        limit_price >= float(levels[-1][0])
        if side == "buy"
        else limit_price <= float(levels[-1][0])
    )
    if not covered:
        raise RuntimeError("requested limit price is outside the 100-level depth snapshot")
    return 0.0


def immediate_limit_fill(
    *, side: str, limit_price: float, quantity: float, book: dict[str, object]
) -> tuple[float, float]:
    """Conservatively replay a marketable limit order against visible depth."""
    levels = book["asks"] if side == "buy" else book["bids"]
    assert isinstance(levels, list)
    remaining = quantity
    notional = 0.0
    for price, available in levels:
        price = float(price)
        eligible = price <= limit_price if side == "buy" else price >= limit_price
        if not eligible:
            break
        filled = min(remaining, float(available))
        notional += filled * price
        remaining -= filled
        if remaining <= 1e-12:
            return quantity, notional / quantity
    raise RuntimeError("marketable replay order exceeds visible eligible depth")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", required=True, choices=("buy", "sell"))
    size = parser.add_mutually_exclusive_group(required=True)
    size.add_argument("--qty", type=float, help="Hypothetical base-asset quantity; no order is sent")
    size.add_argument(
        "--notional-usdt",
        type=float,
        help="Hypothetical quote notional converted to quantity at the submission mid; no order is sent",
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--maker-fee", type=float, default=0.0002)
    parser.add_argument("--taker-fee", type=float, default=0.0005)
    parser.add_argument(
        "--limit-price",
        type=float,
        help="Exact intended limit price to replay; omitted calibration probes use the current touch",
    )
    parser.add_argument("--markout-seconds", type=int, default=300)
    parser.add_argument("--signal-id")
    parser.add_argument(
        "--trigger-kind",
        choices=("manual_calibration", "periodic_calibration", "alpha001_live_order_attempt"),
        default="manual_calibration",
    )
    parser.add_argument("--output", type=Path, default=Path("research/shadow_maker_orders.jsonl"))
    args = parser.parse_args()
    if (
        (args.qty is not None and args.qty <= 0)
        or (args.notional_usdt is not None and args.notional_usdt <= 0)
        or args.timeout_seconds < 1
        or args.timeout_seconds > 3600
        or args.poll_seconds < 1
        or args.maker_fee < 0
        or args.taker_fee < 0
        or (args.limit_price is not None and args.limit_price <= 0)
        or args.markout_seconds < 1
        or args.markout_seconds > 3600
    ):
        raise ValueError("qty > 0, timeout/markout 1..3600 seconds, and poll >= 1 second are required")
    symbol = args.symbol.upper()
    if not re.fullmatch(r"[A-Z0-9]{5,20}", symbol):
        raise ValueError("invalid symbol")

    submitted_at = datetime.now(timezone.utc)
    start_book = best_book(symbol)
    limit_price = float(
        args.limit_price
        if args.limit_price is not None
        else (start_book["bid_price"] if args.side == "buy" else start_book["ask_price"])
    )
    quantity = (
        float(args.qty)
        if args.qty is not None
        else float(args.notional_usdt) / limit_price
    )
    marketable = (
        limit_price >= float(start_book["ask_price"])
        if args.side == "buy"
        else limit_price <= float(start_book["bid_price"])
    )
    queue_ahead = 0.0 if marketable else queue_ahead_at_limit(
        side=args.side, limit_price=limit_price, book=start_book
    )
    latest = get_json("/fapi/v1/aggTrades", {"symbol": symbol, "limit": 1})
    if not isinstance(latest, list) or not latest:
        raise RuntimeError("Binance aggregate-trade response was empty")
    next_id = int(latest[-1]["a"]) + 1
    first_requested_trade_id = next_id
    trades: list[dict] = []
    fill_observed_monotonic: float | None = None
    fill_observed_at: datetime | None = None
    immediate_average_price = 0.0
    if marketable:
        immediate_filled, immediate_average_price = immediate_limit_fill(
            side=args.side,
            limit_price=limit_price,
            quantity=quantity,
            book=start_book,
        )
        fill_observed_monotonic = time.monotonic()
        fill_observed_at = datetime.now(timezone.utc)
        estimate = {
            "filled_qty": immediate_filled,
            "unfilled_qty": 0.0,
            "queue_ahead_initial_qty": 0.0,
            "queue_ahead_remaining_qty": 0.0,
            "eligible_trade_qty": 0.0,
            "eligible_trade_count": 0,
            "first_fill_time_ms": None,
            "full_fill_time_ms": None,
        }
    else:
        deadline = time.monotonic() + args.timeout_seconds
        while time.monotonic() < deadline:
            page = get_json("/fapi/v1/aggTrades", {"symbol": symbol, "fromId": next_id, "limit": 1000})
            if isinstance(page, list) and page:
                observed_ids = [int(item["a"]) for item in page]
                if observed_ids[0] != next_id or any(
                    current != previous + 1
                    for previous, current in zip(observed_ids, observed_ids[1:])
                ):
                    raise RuntimeError("Binance aggregate-trade IDs were not continuous")
                trades.extend(page)
                next_id = int(page[-1]["a"]) + 1
                estimate = estimate_shadow_fill(
                    side=args.side,
                    limit_price=limit_price,
                    order_qty=quantity,
                    queue_ahead_qty=queue_ahead,
                    aggregate_trades=trades,
                )
                if float(estimate["filled_qty"]) > 0 and fill_observed_monotonic is None:
                    fill_observed_monotonic = time.monotonic()
                    fill_observed_at = datetime.now(timezone.utc)
                if float(estimate["unfilled_qty"]) <= 1e-12:
                    break
            time.sleep(args.poll_seconds)

        estimate = estimate_shadow_fill(
            side=args.side,
            limit_price=limit_price,
            order_qty=quantity,
            queue_ahead_qty=queue_ahead,
            aggregate_trades=trades,
        )
    timeout_book = best_book(symbol)
    order_observation_ended_at = datetime.now(timezone.utc)
    filled = float(estimate["filled_qty"])
    markout_book = None
    markout_observed_at = None
    if filled > 0:
        fill_observed_monotonic = fill_observed_monotonic or time.monotonic()
        fill_observed_at = fill_observed_at or datetime.now(timezone.utc)
        remaining = args.markout_seconds - (time.monotonic() - fill_observed_monotonic)
        if remaining > 0:
            time.sleep(remaining)
        markout_book = best_book(symbol)
        markout_observed_at = datetime.now(timezone.utc)
    record = {
        "signal_id": args.signal_id or f"shadow-{uuid4()}",
        "probe_kind": "public_queue_estimate_no_order_submitted",
        "trigger_kind": args.trigger_kind,
        "symbol": symbol,
        "submitted_at": submitted_at.isoformat(),
        "observed_until": datetime.now(timezone.utc).isoformat(),
        "order_observation_ended_at": order_observation_ended_at.isoformat(),
        "side": args.side,
        "submitted_qty": quantity,
        "requested_notional_usdt": args.notional_usdt,
        "timeout_seconds": args.timeout_seconds,
        "reference_mid": start_book["mid"],
        "shadow_limit_price": limit_price,
        "requested_limit_price_replayed": args.limit_price is not None,
        "order_marketable_at_probe": marketable,
        "requested_price_drift_bps": (limit_price / float(start_book["mid"]) - 1.0) * 10_000,
        "filled_qty": filled,
        "average_fill_price": immediate_average_price if marketable else (limit_price if filled else 0.0),
        "maker_filled_qty": 0.0 if marketable else filled,
        "taker_filled_qty": filled if marketable else 0.0,
        "fee": filled * (immediate_average_price if marketable else limit_price) * (
            args.taker_fee if marketable else args.maker_fee
        ),
        "mid_after_timeout": timeout_book["mid"] if filled < quantity else None,
        "mid_after_5m": markout_book["mid"] if markout_book else None,
        "fill_observed_at": fill_observed_at.isoformat() if fill_observed_at else None,
        "markout_observed_at": markout_observed_at.isoformat() if markout_observed_at else None,
        "markout_seconds": args.markout_seconds,
        **estimate,
        "aggregate_trades_observed": len(trades),
        "first_requested_aggregate_trade_id": first_requested_trade_id,
        "last_observed_aggregate_trade_id": int(trades[-1]["a"]) if trades else None,
        "aggregate_trade_ids_continuous": True,
        "model_limitations": [
            "public aggregate trades are not an exchange order acknowledgement",
            "queue cancellations are not credited",
            "hidden and RPI liquidity can change true queue position",
            "a marketable replay consumes only the visible 100-level snapshot",
            "no API key or order endpoint was used"
        ]
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
