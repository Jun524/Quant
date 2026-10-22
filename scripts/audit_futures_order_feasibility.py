#!/usr/bin/env python3
"""Seal public Binance USD-M filter feasibility for a bounded live order path.

The audit is read-only.  It proves neither alpha nor trading permission; it
only prevents a live review from assuming a configured stake can be submitted
when quantity increments force a larger order than the risk policy permits.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.bounded_notional import evaluate_bounded_executable_notional
from scripts.audit_binance_futures_account import config_symbols


EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={}"
SCHEMA_VERSION = 1
KIND = "BINANCE_USDTM_BOUNDED_ORDER_FEASIBILITY"


def _fetch(url: str) -> tuple[dict[str, Any], str]:
    request = Request(url, headers={"User-Agent": "quant-order-feasibility/1"})
    with urlopen(request, timeout=15) as response:
        payload = response.read()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Binance public endpoint did not return a JSON object")
    return value, sha256(payload).hexdigest()


def build_report(
    config: dict[str, Any], *, maximum_notional_usdt: object
) -> dict[str, Any]:
    symbols = config_symbols(config)
    desired = config.get("stake_amount")
    if not isinstance(desired, (int, float, str)) or isinstance(desired, bool):
        raise ValueError("config stake_amount must be a scalar fixed USDT amount")
    exchange_info, exchange_hash = _fetch(EXCHANGE_INFO_URL)
    exchange_symbols = {
        str(item.get("symbol")): item
        for item in exchange_info.get("symbols") or []
        if isinstance(item, dict)
    }
    results: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, str]] = {
        "exchange_info": {"url": EXCHANGE_INFO_URL, "response_sha256": exchange_hash}
    }
    for symbol in symbols:
        symbol_info = exchange_symbols.get(symbol)
        if symbol_info is None:
            raise ValueError(f"{symbol} is absent from Binance USD-M exchangeInfo")
        ticker_url = BOOK_TICKER_URL.format(symbol)
        ticker, ticker_hash = _fetch(ticker_url)
        results[symbol] = {
            "source_snapshot": {"symbol": symbol_info, "book_ticker": ticker},
            "sizing": evaluate_bounded_executable_notional(
                symbol_info,
                ticker,
                desired_notional_usdt=desired,
                maximum_notional_usdt=maximum_notional_usdt,
            ),
        }
        sources[f"book_ticker:{symbol}"] = {
            "url": ticker_url,
            "response_sha256": ticker_hash,
        }
    status = "PASS" if all(
        item["sizing"]["status"] == "WITHIN_RISK_CAP" for item in results.values()
    ) else "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "maximum_age_seconds_for_live_review": 900,
        "status": status,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "symbols": symbols,
        "desired_notional_usdt": str(desired),
        "maximum_notional_usdt": str(maximum_notional_usdt),
        "sources": sources,
        "results": results,
        "interpretation": (
            "A PASS only establishes current order-size feasibility. It does not "
            "authorize live trading or replace strategy, risk, or execution gates."
        ),
    }


def receipt_failures(
    receipt: dict[str, Any] | None,
    *,
    expected_symbols: list[str],
    expected_desired_notional_usdt: object,
    expected_maximum_notional_usdt: object,
    now: datetime | None = None,
) -> list[str]:
    """Validate a fresh, source-bearing feasibility receipt for live review."""

    now = now or datetime.now(timezone.utc)
    if not isinstance(receipt, dict):
        return ["exchange filter feasibility receipt is missing or invalid"]
    failures: list[str] = []
    if receipt.get("schema_version") != SCHEMA_VERSION or receipt.get("kind") != KIND:
        failures.append("exchange filter feasibility receipt schema is invalid")
    if receipt.get("status") != "PASS":
        failures.append("exchange filter feasibility is not PASS")
    if receipt.get("promotion_eligible") is not False or receipt.get("live_order_authorization") != "CLOSED":
        failures.append("exchange filter feasibility receipt has unsafe authorization fields")
    if receipt.get("symbols") != sorted(expected_symbols):
        failures.append("exchange filter feasibility symbols do not match the live whitelist")
    try:
        size_matches = (
            Decimal(str(receipt.get("desired_notional_usdt")))
            == Decimal(str(expected_desired_notional_usdt))
            and Decimal(str(receipt.get("maximum_notional_usdt")))
            == Decimal(str(expected_maximum_notional_usdt))
        )
    except (InvalidOperation, TypeError, ValueError):
        size_matches = False
    if not size_matches:
        failures.append("exchange filter feasibility size limits do not match policy")
    try:
        created = datetime.fromisoformat(str(receipt["generated_at_utc"]).replace("Z", "+00:00"))
        age = (now - created.astimezone(timezone.utc)).total_seconds()
        maximum_age = int(receipt["maximum_age_seconds_for_live_review"])
        if age < 0 or age > maximum_age:
            failures.append("exchange filter feasibility receipt is stale or future-dated")
    except (KeyError, TypeError, ValueError):
        failures.append("exchange filter feasibility receipt timestamp is invalid")
    results = receipt.get("results")
    sources = receipt.get("sources")
    if not isinstance(results, dict) or set(results) != set(expected_symbols):
        failures.append("exchange filter feasibility results are incomplete")
    else:
        invalid_sizing = False
        for symbol, item in results.items():
            try:
                snapshot = item["source_snapshot"]
                recomputed = evaluate_bounded_executable_notional(
                    snapshot["symbol"],
                    snapshot["book_ticker"],
                    desired_notional_usdt=expected_desired_notional_usdt,
                    maximum_notional_usdt=expected_maximum_notional_usdt,
                )
                if (
                    recomputed != item.get("sizing")
                    or recomputed["symbol"] != symbol
                    or recomputed["status"] != "WITHIN_RISK_CAP"
                ):
                    invalid_sizing = True
            except (KeyError, TypeError, ValueError):
                invalid_sizing = True
        if invalid_sizing:
            failures.append("exchange filter feasibility contains a risk-cap veto or invalid sizing replay")
    if not isinstance(sources, dict) or set(sources) != {
        "exchange_info", *(f"book_ticker:{symbol}" for symbol in expected_symbols)
    } or any(
        not isinstance(item, dict)
        or not isinstance(item.get("url"), str)
        or not isinstance(item.get("response_sha256"), str)
        or len(item["response_sha256"]) != 64
        for item in (sources or {}).values()
    ):
        failures.append("exchange filter feasibility source receipts are incomplete")
    return failures


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--maximum-notional-usdt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = build_report(config, maximum_notional_usdt=args.maximum_notional_usdt)
    _atomic_json(args.output, report)
    print(json.dumps(report, indent=2))
    if report["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
