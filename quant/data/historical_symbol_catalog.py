"""Read and seal the official Binance archive symbol catalogue.

The public S3 index includes paths for delisted contracts, unlike a current
``exchangeInfo`` response.  A catalogue is only a *source inventory*: it must
not be mistaken for proof that a symbol was tradable or liquid on any given
past day.  The latter requires a separate day-level membership replay.
"""

from __future__ import annotations

from hashlib import sha256
import json
from typing import Iterable
from xml.etree import ElementTree


S3_LIST_ENDPOINT = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
KLINE_PREFIX = "data/futures/um/daily/klines/"


class HistoricalSymbolCatalogError(ValueError):
    """Official archive listing data is malformed or outside the fixed scope."""


def _tag_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(root: ElementTree.Element, name: str) -> str | None:
    for child in root:
        if _tag_name(child) == name:
            value = (child.text or "").strip()
            return value or None
    return None


def _archive_symbol(prefix: str, *, parent_prefix: str) -> str | None:
    if not prefix.startswith(parent_prefix) or not prefix.endswith("/"):
        raise HistoricalSymbolCatalogError("archive prefix is outside the requested tree")
    remainder = prefix[len(parent_prefix) : -1]
    if "/" in remainder or not remainder:
        raise HistoricalSymbolCatalogError("archive symbol prefix is not a direct child")
    # The USD-M archive tree also preserves dated delivery and BUSD/USDC
    # contracts (for example ``BTCBUSD_210129``).  They are legitimate archive
    # paths but outside this Binance USDT-M perpetual-only project.
    if not remainder.endswith("USDT") or "_" in remainder:
        return None
    if not remainder.isalnum() or remainder != remainder.upper():
        raise HistoricalSymbolCatalogError(
            f"archive symbol is not uppercase alphanumeric: {remainder!r}"
        )
    return remainder


def parse_s3_symbol_page(payload: bytes, *, parent_prefix: str = KLINE_PREFIX) -> dict:
    """Parse one checksum-independent S3 listing page without downloading data."""
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise HistoricalSymbolCatalogError("S3 listing is not valid XML") from error
    if _tag_name(root) != "ListBucketResult":
        raise HistoricalSymbolCatalogError("S3 listing root is invalid")
    if _child_text(root, "Prefix") != parent_prefix:
        raise HistoricalSymbolCatalogError("S3 listing prefix does not match request")
    raw_prefixes: list[str] = []
    for child in root:
        if _tag_name(child) != "CommonPrefixes":
            continue
        value = _child_text(child, "Prefix")
        if value is None:
            raise HistoricalSymbolCatalogError("S3 common prefix is empty")
        raw_prefixes.append(value)
    parsed = [_archive_symbol(item, parent_prefix=parent_prefix) for item in raw_prefixes]
    symbols = [item for item in parsed if item is not None]
    if len(symbols) != len(set(symbols)):
        raise HistoricalSymbolCatalogError("S3 listing contains duplicate symbol prefixes")
    truncated = _child_text(root, "IsTruncated")
    if truncated not in {"true", "false"}:
        raise HistoricalSymbolCatalogError("S3 listing truncation state is invalid")
    token = _child_text(root, "NextContinuationToken")
    if truncated == "true" and token is None:
        raise HistoricalSymbolCatalogError("truncated S3 listing lacks continuation token")
    if truncated == "false" and token is not None:
        raise HistoricalSymbolCatalogError("complete S3 listing unexpectedly has continuation token")
    return {
        "symbols": sorted(symbols),
        "is_truncated": truncated == "true",
        "next_continuation_token": token,
        "payload_sha256": sha256(payload).hexdigest(),
        "excluded_non_usdt_or_delivery_prefixes": len(raw_prefixes) - len(symbols),
    }


def build_historical_symbol_catalog(pages: Iterable[dict]) -> dict:
    """Seal a complete ordered sequence of parsed S3 listing pages."""
    rows = list(pages)
    if not rows:
        raise HistoricalSymbolCatalogError("historical catalogue has no listing pages")
    symbols: list[str] = []
    page_digests: list[str] = []
    for index, page in enumerate(rows):
        if not isinstance(page, dict):
            raise HistoricalSymbolCatalogError("historical catalogue page is invalid")
        values = page.get("symbols")
        digest = page.get("payload_sha256")
        if not isinstance(values, list) or not isinstance(digest, str) or len(digest) != 64:
            raise HistoricalSymbolCatalogError("historical catalogue page fields are invalid")
        if index + 1 < len(rows) and page.get("is_truncated") is not True:
            raise HistoricalSymbolCatalogError("non-final S3 page is unexpectedly complete")
        if index + 1 == len(rows) and page.get("is_truncated") is not False:
            raise HistoricalSymbolCatalogError("final S3 page is unexpectedly truncated")
        symbols.extend(values)
        page_digests.append(digest)
    if len(symbols) != len(set(symbols)):
        raise HistoricalSymbolCatalogError("symbol appears in more than one S3 listing page")
    ordered = sorted(symbols)
    body = {
        "schema_version": 1,
        "status": "SOURCE_CATALOG_SEALED_NOT_DAY_MEMBERSHIP",
        "source": {
            "endpoint": S3_LIST_ENDPOINT,
            "prefix": KLINE_PREFIX,
            "dataset": "USD-M daily kline archive directory inventory",
            "pages": len(rows),
            "page_payload_sha256": page_digests,
        },
        "symbols": ordered,
        "symbol_count": len(ordered),
        "excluded_non_usdt_or_delivery_prefixes": sum(
            int(item.get("excluded_non_usdt_or_delivery_prefixes") or 0)
            for item in rows
        ),
        "does_not_prove": [
            "past daily contract status",
            "past daily liquidity",
            "past daily spread or orderbook depth",
            "strategy return or live eligibility",
        ],
        "live_order_authorization": "CLOSED",
    }
    digest = sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**body, "catalog_sha256": digest}
