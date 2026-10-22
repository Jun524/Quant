#!/usr/bin/env python3
"""Seal Binance USD-M archive symbol prefixes, including delisted contracts."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.historical_symbol_catalog import (
    KLINE_PREFIX,
    S3_LIST_ENDPOINT,
    build_historical_symbol_catalog,
    parse_s3_symbol_page,
)


MAX_PAGE_BYTES = 5_000_000


def _request_url(token: str | None) -> str:
    query = {"list-type": "2", "prefix": KLINE_PREFIX, "delimiter": "/"}
    if token is not None:
        query["continuation-token"] = token
    return f"{S3_LIST_ENDPOINT}?{urlencode(query)}"


def _fetch(url: str, *, timeout: float) -> bytes:
    request = Request(url, headers={"User-Agent": "quant-historical-universe-catalog/1"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_PAGE_BYTES + 1)
    if len(payload) > MAX_PAGE_BYTES:
        raise RuntimeError("S3 listing page exceeded fixed size limit")
    return payload


def _write_once(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != encoded:
            raise RuntimeError("existing historical symbol catalogue differs from replay")
        return
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("research/historical_universe/source-symbol-catalog.json"),
    )
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100 or args.timeout <= 0:
        raise SystemExit("--max-pages must be 1..100 and --timeout must be positive")

    pages: list[dict] = []
    token: str | None = None
    for _ in range(args.max_pages):
        page = parse_s3_symbol_page(_fetch(_request_url(token), timeout=args.timeout))
        pages.append(page)
        if not page["is_truncated"]:
            break
        token = page["next_continuation_token"]
    if pages[-1]["is_truncated"]:
        raise SystemExit("S3 symbol catalogue exceeded --max-pages")
    catalogue = build_historical_symbol_catalog(pages)
    output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
    _write_once(output, catalogue)
    print(json.dumps({
        "status": catalogue["status"],
        "symbol_count": catalogue["symbol_count"],
        "pages": catalogue["source"]["pages"],
        "catalog_sha256": catalogue["catalog_sha256"],
        "output": str(output.relative_to(PROJECT_ROOT)),
        "live_order_authorization": "CLOSED",
    }, indent=2))


if __name__ == "__main__":
    main()
