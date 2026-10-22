#!/usr/bin/env python3
"""Collect an append-only Binance USDT-M universe snapshot (no credentials or orders)."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.snapshots import seal_snapshot_batch, write_snapshot_batch
from quant.data.universe import InstrumentSnapshot, UniversePolicy, select_universe
from quant.data.universe_protocol import policy_document, protocol_digests

DEFAULT_PAIRS = ("BTC/USDT:USDT", "ETH/USDT:USDT", "BNB/USDT:USDT", "SOL/USDT:USDT", "XRP/USDT:USDT")


def _listed_at(market: dict, observed_at: datetime) -> datetime:
    raw = (market.get("info") or {}).get("onboardDate")
    try:
        return datetime.fromtimestamp(float(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        # Unknown listing date must not accidentally pass the age gate.
        return observed_at


def _local_candle_status(
    data_dir: Path,
    pair: str,
    *,
    observed_at: datetime,
) -> tuple[int, bool, bool, str | None]:
    """Use only locally stored 1h data; unavailable history is explicitly false."""
    filename = pair.replace("/", "_").replace(":", "_") + "-1h-futures.feather"
    path = data_dir / "futures" / filename
    if not path.exists():
        return 0, False, False, None
    try:
        import pandas as pd

        candles = pd.read_feather(path, columns=["date"])
    except (ImportError, OSError):
        return 0, False, False, None
    if candles.empty:
        return 0, False, False, None
    dates = candles["date"]
    if dates.dt.tz is None:
        dates = dates.dt.tz_localize("UTC")
    else:
        dates = dates.dt.tz_convert("UTC")
    latest = dates.max().to_pydatetime().astimezone(timezone.utc)
    gaps = candles["date"].diff().dropna()
    continuous = bool((gaps.dt.total_seconds() <= 3600).all())
    fresh = timedelta(0) <= observed_at - latest <= timedelta(hours=2)
    return len(candles), continuous, fresh, latest.isoformat()


def _remote_candle_status(
    exchange,
    pair: str,
    *,
    observed_at: datetime,
    limit: int,
) -> tuple[int, bool, bool, str | None]:
    """Validate completed public 1h candles without trusting stale local files."""
    rows = exchange.fetch_ohlcv(pair, timeframe="1h", limit=limit)
    observed_ms = int(observed_at.timestamp() * 1000)
    completed = sorted(
        {
            int(row[0]): row
            for row in rows
            if isinstance(row, (list, tuple))
            and len(row) >= 6
            and int(row[0]) + 3_600_000 <= observed_ms
        }.values(),
        key=lambda row: int(row[0]),
    )
    latest_rows = completed[-500:]
    if not latest_rows:
        return 0, False, False, None
    timestamps = [int(row[0]) for row in latest_rows]
    continuous = len(latest_rows) >= 500 and all(
        current - previous == 3_600_000
        for previous, current in zip(timestamps, timestamps[1:])
    )
    latest = datetime.fromtimestamp(timestamps[-1] / 1000, tz=timezone.utc)
    fresh = timedelta(0) <= observed_at - latest <= timedelta(hours=2)
    return len(latest_rows), continuous, fresh, latest.isoformat()


def _policy_window(policy: dict, scheduled_date: date) -> tuple[datetime, datetime]:
    raw = str(policy["sampling"]["start_utc"])
    start_time = datetime_time.fromisoformat(raw)
    start = datetime.combine(scheduled_date, start_time, tzinfo=timezone.utc)
    return start, start + timedelta(seconds=int(policy["sampling"]["window_seconds"]))


def validate_forward_collection_start(
    observed_at: datetime,
    *,
    scheduled_date: date,
    policy: dict,
) -> None:
    eligible_start = date.fromisoformat(str(policy["eligible_start_date"]))
    start, end = _policy_window(policy, scheduled_date)
    if scheduled_date < eligible_start:
        raise ValueError("scheduled universe date predates forward eligibility")
    if observed_at.date() != scheduled_date or not (start <= observed_at < end):
        raise ValueError(
            f"forward universe collection must start in [{start.isoformat()}, {end.isoformat()})"
        )


def _depth(orderbook: dict, mid: float, bps: float) -> float:
    if mid <= 0:
        return 0.0
    lower, upper = mid * (1 - bps / 10_000), mid * (1 + bps / 10_000)
    bids = sum(float(price) * float(amount) for price, amount in orderbook.get("bids", []) if float(price) >= lower)
    asks = sum(float(price) * float(amount) for price, amount in orderbook.get("asks", []) if float(price) <= upper)
    return bids + asks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("research/universe_snapshots"))
    parser.add_argument("--data-dir", type=Path, default=Path("user_data/data/binance"))
    parser.add_argument("--pairs", nargs="*")
    parser.add_argument("--all-usdt-perpetual", action="store_true")
    parser.add_argument("--depth-pairs", type=int, default=50)
    parser.add_argument(
        "--evidence-role",
        choices=("diagnostic", "daily_forward_universe"),
        default="diagnostic",
    )
    parser.add_argument("--scheduled-date", type=date.fromisoformat)
    args = parser.parse_args()
    if args.depth_pairs < 0:
        raise ValueError("--depth-pairs must be non-negative")
    forward_policy = policy_document()
    collection_started_at = datetime.now(timezone.utc)
    if args.evidence_role == "daily_forward_universe":
        if args.scheduled_date is None:
            raise ValueError("--scheduled-date is required for forward universe evidence")
        if not args.all_usdt_perpetual:
            raise ValueError("forward universe evidence requires --all-usdt-perpetual")
        if args.depth_pairs != int(forward_policy["sampling"]["depth_pairs"]):
            raise ValueError("forward universe depth-pairs differs from preregistered policy")
        validate_forward_collection_start(
            collection_started_at,
            scheduled_date=args.scheduled_date,
            policy=forward_policy,
        )

    try:
        import ccxt
    except ImportError as exc:
        raise RuntimeError("Run this collector with the Freqtrade runtime, which includes ccxt.") from exc
    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    markets = exchange.load_markets()
    if args.pairs:
        pairs = tuple(args.pairs)
    elif args.all_usdt_perpetual:
        pairs = tuple(sorted(symbol for symbol, market in markets.items() if market.get("swap") and market.get("quote") == "USDT"))
    else:
        pairs = DEFAULT_PAIRS
    tickers = exchange.fetch_tickers(pairs)
    observed_at = datetime.now(timezone.utc)
    if args.evidence_role == "daily_forward_universe":
        validate_forward_collection_start(
            observed_at,
            scheduled_date=args.scheduled_date,
            policy=forward_policy,
        )
    ranked = sorted(
        pairs,
        key=lambda pair: float((tickers.get(pair) or {}).get("quoteVolume") or 0.0),
        reverse=True,
    )
    detailed_pairs = set(ranked[: args.depth_pairs])
    policy = UniversePolicy()
    instruments: list[dict] = []
    for pair in pairs:
        market, ticker = markets.get(pair) or {}, tickers.get(pair) or {}
        bid, ask = ticker.get("bid"), ticker.get("ask")
        depth_10bps = depth_25bps = open_interest = funding_rate = mark_price = None
        orderbook = None
        funding_collected = False
        open_interest_collected = False
        candle_source = "local"
        if pair in detailed_pairs:
            try:
                orderbook = exchange.fetch_order_book(pair, limit=100)
                if not bid and orderbook.get("bids"):
                    bid = orderbook["bids"][0][0]
                if not ask and orderbook.get("asks"):
                    ask = orderbook["asks"][0][0]
            except ccxt.BaseError:
                pass
            try:
                funding = exchange.fetch_funding_rate(pair)
                funding_rate, mark_price = funding.get("fundingRate"), funding.get("markPrice")
                funding_collected = funding_rate is not None and mark_price is not None
            except ccxt.BaseError:
                pass
            try:
                interest = exchange.fetch_open_interest(pair)
                open_interest = interest.get("openInterestValue") or interest.get("openInterestAmount")
                open_interest_collected = open_interest is not None
            except ccxt.BaseError:
                pass
        mid = (float(bid) + float(ask)) / 2 if bid and ask else 0.0
        spread_bps = (float(ask) - float(bid)) / mid * 10_000 if mid else None
        if orderbook is not None:
            depth_10bps, depth_25bps = _depth(orderbook, mid, 10), _depth(orderbook, mid, 25)
        if pair in detailed_pairs:
            candle_source = "binance_public_1h"
            try:
                candle_count, data_continuous, data_fresh, latest_candle_at = (
                    _remote_candle_status(
                        exchange,
                        pair,
                        observed_at=observed_at,
                        limit=int(forward_policy["sampling"]["detailed_candle_limit"]),
                    )
                )
            except ccxt.BaseError:
                candle_count, data_continuous, data_fresh, latest_candle_at = 0, False, False, None
        else:
            candle_count, data_continuous, data_fresh, latest_candle_at = _local_candle_status(
                args.data_dir,
                pair,
                observed_at=observed_at,
            )
        listed_at = _listed_at(market, observed_at)
        item = {
            "timestamp": observed_at.isoformat(),
            "symbol": pair,
            "listing_status": "active" if market.get("active", True) else "inactive",
            "listed_at": listed_at.isoformat(),
            "quote_volume": ticker.get("quoteVolume") or 0.0,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "depth_10bps": depth_10bps,
            "depth_25bps": depth_25bps,
            "open_interest": open_interest,
            "funding_rate": funding_rate,
            "mark_price": mark_price,
            "candle_count": candle_count,
            "data_available": data_continuous,
            "data_continuous": data_continuous,
            "data_fresh": data_fresh,
            "latest_candle_at": latest_candle_at,
            "candle_source": candle_source,
            "depth_requested": pair in detailed_pairs,
            "depth_collected": orderbook is not None,
            "funding_collected": funding_collected,
            "open_interest_collected": open_interest_collected,
        }
        decision = select_universe(
            [
                InstrumentSnapshot(
                    pair=pair,
                    observed_at=observed_at,
                    listed_at=listed_at,
                    quote_volume_24h=float(item["quote_volume"]),
                    candle_count=candle_count,
                    data_continuous=data_continuous,
                    spread_bps=spread_bps,
                    orderbook_depth=depth_10bps,
                    active=item["listing_status"] == "active",
                    data_fresh=data_fresh,
                )
            ],
            as_of=observed_at,
            policy=policy,
        )
        item["eligible"] = pair in decision.selected
        item["eligibility_reasons"] = list(decision.rejected.get(pair, ()))
        instruments.append(item)
    collection_completed_at = datetime.now(timezone.utc)
    duration = (collection_completed_at - collection_started_at).total_seconds()
    detailed_count = len(detailed_pairs)
    coverage = {
        "requested": detailed_count,
        "depth": sum(bool(item["depth_collected"]) for item in instruments),
        "candles": sum(
            item["depth_requested"]
            and item["candle_count"] >= 500
            and item["data_continuous"]
            and item["data_fresh"]
            for item in instruments
        ),
        "funding": sum(bool(item["funding_collected"]) for item in instruments),
        "open_interest": sum(bool(item["open_interest_collected"]) for item in instruments),
    }
    coverage["ratios"] = {
        name: count / detailed_count if detailed_count else 0.0
        for name, count in coverage.items()
        if name not in {"requested", "ratios"}
    }
    if (
        args.evidence_role == "daily_forward_universe"
        and duration > float(forward_policy["sampling"]["maximum_collection_duration_seconds"])
    ):
        raise RuntimeError("forward universe collection exceeded its maximum duration")
    if args.evidence_role == "daily_forward_universe" and any(
        ratio < float(forward_policy["sampling"]["minimum_detailed_coverage_ratio"])
        for ratio in coverage["ratios"].values()
    ):
        raise RuntimeError(f"forward universe detailed coverage is below policy: {coverage}")
    path = write_snapshot_batch(
        directory=args.output_dir,
        observed_at=observed_at,
        instruments=instruments,
        metadata={
            "exchange": "binanceusdm",
            "all_usdt_perpetual": args.all_usdt_perpetual,
            "depth_pairs": args.depth_pairs,
            "evidence_role": args.evidence_role,
            "scheduled_date": args.scheduled_date.isoformat() if args.scheduled_date else None,
            "policy": forward_policy["policy"],
            "detailed_coverage": coverage,
        },
    )
    try:
        manifest = seal_snapshot_batch(
            path,
            protocol_sha256=protocol_digests(),
            evidence_role=args.evidence_role,
            scheduled_date=args.scheduled_date.isoformat() if args.scheduled_date else None,
            collection_started_at=collection_started_at,
            collection_completed_at=collection_completed_at,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    print(
        json.dumps(
            {
                "snapshot": str(path),
                "manifest": str(manifest),
                "instruments": len(instruments),
                "eligible": sum(item["eligible"] for item in instruments),
                "duration_seconds": duration,
                "detailed_coverage": coverage,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
