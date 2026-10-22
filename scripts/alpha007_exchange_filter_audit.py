#!/usr/bin/env python3
"""Fetch and seal the current public BTCUSDT order-filter feasibility gate."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.execution.exchange_filter_gate import evaluate_fixed_notional_filter_gate


EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BOOK_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol=BTCUSDT"
OUTPUT_PATH = PROJECT_ROOT / "reports" / "alpha007-exchange-filter-audit.json"


def _get(url: str) -> tuple[dict, str]:
    request = Request(url, headers={"User-Agent": "quant-research-alpha007/1"})
    with urlopen(request, timeout=15) as response:
        payload = response.read()
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Binance public endpoint did not return an object")
    return value, sha256(payload).hexdigest()


def build_report() -> dict:
    exchange_info, exchange_hash = _get(EXCHANGE_INFO_URL)
    ticker, ticker_hash = _get(BOOK_TICKER_URL)
    symbol = next(
        (item for item in exchange_info.get("symbols") or [] if item.get("symbol") == "BTCUSDT"),
        None,
    )
    if symbol is None:
        raise ValueError("BTCUSDT is absent from Binance USD-M exchangeInfo")
    result = evaluate_fixed_notional_filter_gate(
        symbol, ticker, fixed_notional_usdt=50
    )
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "maximum_age_seconds_for_promotion": 3600,
        "sources": {
            "exchange_info": {"url": EXCHANGE_INFO_URL, "response_sha256": exchange_hash},
            "book_ticker": {"url": BOOK_TICKER_URL, "response_sha256": ticker_hash},
        },
        "source_snapshot": {"symbol": symbol, "book_ticker": ticker},
        **result,
    }


def main() -> None:
    report = build_report()
    OUTPUT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    if report["status"] == "FAIL":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
