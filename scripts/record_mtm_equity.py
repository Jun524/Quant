#!/usr/bin/env python3
"""Poll Freqtrade's read-only balance endpoint into append-only JSONL."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
from math import isfinite
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch_json(url: str, endpoint: str, username: str, password: str, timeout: float):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = Request(
        f"{url.rstrip('/')}/api/v1/{endpoint.lstrip('/')}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - operator supplies local API URL
        return json.load(response)


def fetch_balance(url: str, username: str, password: str, timeout: float) -> dict:
    return fetch_json(url, "balance", username, password, timeout)


def fetch_open_trades(url: str, username: str, password: str, timeout: float) -> list[dict]:
    payload = fetch_json(url, "status", username, password, timeout)
    if not isinstance(payload, list):
        raise ValueError("status endpoint did not return an open-trade list")
    return payload


def calculate_net_liquidation_equity(balance: dict, open_trades: list[dict]) -> dict:
    """Convert Freqtrade's price-only futures balance into cost-aware liquidation equity."""
    balance_total = float(balance.get("total_bot") or 0.0)
    if not isfinite(balance_total) or balance_total <= 0:
        raise ValueError("balance endpoint returned invalid total_bot")
    positions = {
        str(item.get("currency")): item
        for item in balance.get("currencies", [])
        if bool(item.get("is_position"))
    }
    if len(positions) != len(open_trades):
        raise ValueError("balance positions and open trades are not an atomic match")
    adjustment = 0.0
    funding_pnl = 0.0
    for trade in open_trades:
        pair = str(trade.get("pair") or "")
        position = positions.pop(pair, None)
        if position is None:
            raise ValueError(f"missing balance position for open trade {pair!r}")
        collateral = float(trade.get("stake_amount") or 0.0)
        position_equity = float(position.get("est_stake") or 0.0)
        net_profit = float(trade.get("profit_abs"))
        funding = float(trade.get("funding_fees") or 0.0)
        values = (collateral, position_equity, net_profit, funding)
        if not all(isfinite(value) for value in values) or collateral <= 0:
            raise ValueError("open trade contains invalid mark-to-market fields")
        raw_price_profit = position_equity - collateral
        adjustment += net_profit - raw_price_profit
        funding_pnl += funding
    equity = balance_total + adjustment
    if not isfinite(equity) or equity <= 0:
        raise ValueError("calculated net-liquidation equity is invalid")
    return {
        "equity": equity,
        "balance_total_bot": balance_total,
        "open_trade_net_liquidation_adjustment": adjustment,
        "open_funding_pnl": funding_pnl,
        "open_trades": len(open_trades),
    }


def collect_equity(
    *,
    url: str,
    username: str,
    password: str,
    timeout_seconds: float,
    duration_seconds: int,
    interval_seconds: int,
    error_backoff_seconds: int,
    maximum_consecutive_errors: int,
    output: Path,
    fetcher=fetch_balance,
    trade_fetcher=fetch_open_trades,
    sleeper=time.sleep,
    maximum_observations: int | None = None,
) -> dict:
    if maximum_consecutive_errors < 1 or error_backoff_seconds < 0:
        raise ValueError("retry limits must be positive")
    deadline = time.monotonic() + duration_seconds
    captured = consecutive_errors = total_errors = 0
    while True:
        observed_at = datetime.now(timezone.utc)
        try:
            payload = fetcher(url, username, password, timeout_seconds)
            if payload.get("note") != "Simulated balances":
                raise SystemExit("refusing non-dry-run balance source; expected note='Simulated balances'")
            open_trades = trade_fetcher(url, username, password, timeout_seconds)
            valuation = calculate_net_liquidation_equity(payload, open_trades)
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, TypeError, ValueError) as exc:
            consecutive_errors += 1
            total_errors += 1
            print(
                f"balance collection transient error {consecutive_errors}/{maximum_consecutive_errors}: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_errors >= maximum_consecutive_errors:
                raise SystemExit(f"balance collection failed closed after consecutive errors: {exc}") from exc
            sleeper(min(error_backoff_seconds, max(0.0, deadline - time.monotonic())))
            continue
        row = {
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            **valuation,
            "equity_type": "mark_to_market",
            "equity_method": "balance_total_bot_plus_open_trade_net_liquidation_adjustment",
            "source": "freqtrade_rest_net_liquidation_equity",
            "starting_capital": payload.get("starting_capital"),
            "trade_count": payload.get("trade_count"),
            "dry_run_note": payload.get("note"),
        }
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        captured += 1
        consecutive_errors = 0
        if maximum_observations is not None and captured >= maximum_observations:
            break
        if time.monotonic() >= deadline:
            break
        sleeper(min(interval_seconds, max(0.0, deadline - time.monotonic())))
    return {"status": "COMPLETE", "output": str(output), "observations": captured, "transient_errors": total_errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("FREQTRADE_API_URL", "http://127.0.0.1:8081"))
    parser.add_argument("--duration-seconds", type=int, default=3600)
    parser.add_argument("--interval-seconds", type=int, default=60)
    parser.add_argument("--timeout-seconds", type=float, default=10)
    parser.add_argument("--error-backoff-seconds", type=int, default=5)
    parser.add_argument("--maximum-consecutive-errors", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    username = os.environ.get("FREQTRADE_API_USERNAME")
    password = os.environ.get("FREQTRADE_API_PASSWORD")
    if not username or not password:
        parser.error("set FREQTRADE_API_USERNAME and FREQTRADE_API_PASSWORD")
    if args.duration_seconds < 1 or args.interval_seconds < 1:
        parser.error("duration and interval must be positive")
    output = args.output or Path("research/equity/raw") / f"equity-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    output.parent.mkdir(parents=True, exist_ok=True)

    result = collect_equity(
        url=args.url,
        username=username,
        password=password,
        timeout_seconds=args.timeout_seconds,
        duration_seconds=args.duration_seconds,
        interval_seconds=args.interval_seconds,
        error_backoff_seconds=args.error_backoff_seconds,
        maximum_consecutive_errors=args.maximum_consecutive_errors,
        output=output,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
