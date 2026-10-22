#!/usr/bin/env python3
"""Audit Binance USD-M account state through read-only signed endpoints.

No endpoint in this script places, changes, or cancels an order.  The receipt
contains only safety-relevant booleans, counts, symbols, and configuration.
Balances, position quantities, API credentials, and signatures are excluded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlencode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.audit_binance_api_permissions import (
    PermissionAuditError,
    api_key_fingerprint,
    atomic_write_json,
    fetch_json,
    fetch_json_value,
    load_dotenv,
)


BASE_URL = "https://fapi.binance.com"
TIME_ENDPOINT = "/fapi/v1/time"
ACCOUNT_ENDPOINT = "/fapi/v2/account"
POSITION_MODE_ENDPOINT = "/fapi/v1/positionSide/dual"
MULTI_ASSET_MODE_ENDPOINT = "/fapi/v1/multiAssetsMargin"
OPEN_ORDERS_ENDPOINT = "/fapi/v1/openOrders"
OPEN_ALGO_ORDERS_ENDPOINT = "/fapi/v1/openAlgoOrders"
SYMBOL_CONFIG_ENDPOINT = "/fapi/v1/symbolConfig"
SOURCE_ENDPOINTS = [
    f"{BASE_URL}{ACCOUNT_ENDPOINT}",
    f"{BASE_URL}{POSITION_MODE_ENDPOINT}",
    f"{BASE_URL}{MULTI_ASSET_MODE_ENDPOINT}",
    f"{BASE_URL}{OPEN_ORDERS_ENDPOINT}",
    f"{BASE_URL}{OPEN_ALGO_ORDERS_ENDPOINT}",
    f"{BASE_URL}{SYMBOL_CONFIG_ENDPOINT}",
]
RECEIPT_SCHEMA_VERSION = 1
POLICY_NAME = "binance-usdtm-flat-isolated-one-way-v1"
DEFAULT_RECEIPT = Path(__file__).resolve().parents[1] / "reports" / "binance-futures-account-audit.json"


def config_symbols(config: dict[str, Any]) -> list[str]:
    pairs = ((config.get("exchange") or {}).get("pair_whitelist") or [])
    symbols: list[str] = []
    for pair in pairs:
        if not isinstance(pair, str) or "/" not in pair:
            raise ValueError("futures pair whitelist contains an invalid pair")
        base, quote_contract = pair.split("/", 1)
        quote = quote_contract.split(":", 1)[0]
        symbol = f"{base}{quote}"
        if not symbol.isalnum() or not symbol.isupper():
            raise ValueError("futures pair whitelist contains a non-canonical pair")
        symbols.append(symbol)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("futures pair whitelist is empty or duplicated")
    return sorted(symbols)


def _is_nonzero(value: Any) -> bool:
    try:
        return Decimal(str(value)) != 0
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid decimal") from None


def normalize_account_state(
    *,
    account: Any,
    position_mode: Any,
    multi_asset_mode: Any,
    open_orders: Any,
    open_algo_orders: Any,
    symbol_configurations: Any,
    required_symbols: list[str],
) -> dict[str, Any]:
    account_dict = account if isinstance(account, dict) else {}
    position_dict = position_mode if isinstance(position_mode, dict) else {}
    multi_dict = multi_asset_mode if isinstance(multi_asset_mode, dict) else {}

    nonzero_position_symbols: list[str] = []
    invalid_position_rows = 0
    positions = account_dict.get("positions")
    if not isinstance(positions, list):
        invalid_position_rows += 1
    else:
        for row in positions:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                invalid_position_rows += 1
                continue
            try:
                if _is_nonzero(row.get("positionAmt")):
                    nonzero_position_symbols.append(row["symbol"])
            except ValueError:
                invalid_position_rows += 1

    normalized_configs: list[dict[str, Any]] = []
    invalid_symbol_configuration_rows = 0
    required = set(required_symbols)
    if not isinstance(symbol_configurations, list):
        invalid_symbol_configuration_rows += 1
    else:
        for row in symbol_configurations:
            if not isinstance(row, dict):
                invalid_symbol_configuration_rows += 1
                continue
            symbol = row.get("symbol")
            if symbol not in required:
                continue
            normalized_configs.append(
                {
                    "symbol": symbol,
                    "margin_type": row.get("marginType"),
                    "auto_add_margin": row.get("isAutoAddMargin"),
                    "leverage": row.get("leverage"),
                }
            )

    return {
        "can_trade": account_dict.get("canTrade"),
        "account_multi_assets_margin": account_dict.get("multiAssetsMargin"),
        "queried_multi_assets_margin": multi_dict.get("multiAssetsMargin"),
        "dual_side_position": position_dict.get("dualSidePosition"),
        "total_position_initial_margin": account_dict.get("totalPositionInitialMargin"),
        "total_open_order_initial_margin": account_dict.get("totalOpenOrderInitialMargin"),
        "nonzero_position_symbols": sorted(set(nonzero_position_symbols)),
        "invalid_position_rows": invalid_position_rows,
        "standard_open_order_count": len(open_orders) if isinstance(open_orders, list) else None,
        "algo_open_order_count": len(open_algo_orders) if isinstance(open_algo_orders, list) else None,
        "symbol_configurations": sorted(normalized_configs, key=lambda item: str(item["symbol"])),
        "invalid_symbol_configuration_rows": invalid_symbol_configuration_rows,
    }


def account_state_failures(state: dict[str, Any], *, required_symbols: list[str]) -> list[str]:
    failures: list[str] = []
    if state.get("can_trade") is not True:
        failures.append("USD-M futures account must be able to trade")
    if state.get("account_multi_assets_margin") is not False:
        failures.append("account must use single-asset margin mode")
    if state.get("queried_multi_assets_margin") is not False:
        failures.append("queried multi-assets mode must be disabled")
    if state.get("dual_side_position") is not False:
        failures.append("account must use one-way position mode")

    for field, label in (
        ("total_position_initial_margin", "position initial margin"),
        ("total_open_order_initial_margin", "open-order initial margin"),
    ):
        try:
            if _is_nonzero(state.get(field)):
                failures.append(f"{label} must be zero")
        except ValueError:
            failures.append(f"{label} is missing or invalid")

    if state.get("invalid_position_rows") != 0:
        failures.append("position response contains invalid rows")
    if state.get("nonzero_position_symbols") != []:
        failures.append("USD-M account has open positions")
    if state.get("standard_open_order_count") != 0:
        failures.append("USD-M account has standard open orders or an invalid response")
    if state.get("algo_open_order_count") != 0:
        failures.append("USD-M account has algo open orders or an invalid response")
    if state.get("invalid_symbol_configuration_rows") != 0:
        failures.append("symbol-configuration response contains invalid rows")

    configs = state.get("symbol_configurations")
    if not isinstance(configs, list):
        failures.append("symbol configurations are missing")
        return failures
    seen = [row.get("symbol") for row in configs if isinstance(row, dict)]
    if sorted(seen) != sorted(required_symbols):
        failures.append("symbol configurations do not exactly cover the live whitelist")
    for row in configs:
        if not isinstance(row, dict):
            failures.append("symbol configuration row is invalid")
            continue
        symbol = row.get("symbol", "unknown")
        if row.get("margin_type") != "ISOLATED":
            failures.append(f"{symbol} margin type must already be ISOLATED")
        if row.get("auto_add_margin") is not False:
            failures.append(f"{symbol} auto-add margin must be disabled")
        leverage = row.get("leverage")
        if not isinstance(leverage, int) or isinstance(leverage, bool) or leverage != 1:
            failures.append(f"{symbol} leverage must already be 1")
    return failures


def build_receipt(
    state: dict[str, Any],
    *,
    api_key: str,
    required_symbols: list[str],
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or datetime.now(timezone.utc)
    failures = account_state_failures(state, required_symbols=required_symbols)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS" if not failures else "FAIL",
        "source_endpoints": SOURCE_ENDPOINTS,
        "api_key_fingerprint": api_key_fingerprint(api_key),
        "policy": POLICY_NAME,
        "required_symbols": sorted(required_symbols),
        "state": state,
        "failures": failures,
    }


def account_audit_receipt_failures(
    receipt: dict[str, Any],
    *,
    expected_api_key: str,
    expected_symbols: list[str],
    now: datetime | None = None,
    maximum_age_seconds: int = 900,
) -> list[str]:
    now = now or datetime.now(timezone.utc)
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append("account audit schema is unsupported")
    if receipt.get("source_endpoints") != SOURCE_ENDPOINTS:
        failures.append("account audit source endpoints are unexpected")
    if receipt.get("policy") != POLICY_NAME:
        failures.append("account audit policy is unexpected")
    if len(expected_api_key) < 16:
        failures.append("current API key is unavailable for account receipt binding")
    if receipt.get("api_key_fingerprint") != api_key_fingerprint(expected_api_key):
        failures.append("account audit is bound to a different API key")
    if receipt.get("required_symbols") != sorted(expected_symbols):
        failures.append("account audit is bound to a different symbol whitelist")
    try:
        observed_at = datetime.fromisoformat(str(receipt.get("observed_at")).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > maximum_age_seconds:
            failures.append("account audit is stale or future-dated")
    except (TypeError, ValueError):
        failures.append("account audit timestamp is invalid")

    state = receipt.get("state")
    state_failures = (
        account_state_failures(state, required_symbols=expected_symbols)
        if isinstance(state, dict)
        else ["account audit has no state object"]
    )
    failures.extend(f"account audit: {item}" for item in state_failures)
    expected_status = "PASS" if not state_failures else "FAIL"
    if receipt.get("status") != "PASS":
        failures.append("account audit status is not PASS")
    if receipt.get("status") != expected_status:
        failures.append("account audit status does not match its state fields")
    if receipt.get("failures") != []:
        failures.append("account audit records policy failures")
    return failures


def fetch_account_state(
    api_key: str,
    api_secret: str,
    *,
    required_symbols: list[str],
    timeout: float = 10.0,
) -> dict[str, Any]:
    time_payload = fetch_json(f"{BASE_URL}{TIME_ENDPOINT}", timeout=timeout)
    server_time = time_payload.get("serverTime")
    if not isinstance(server_time, int) or server_time <= 0:
        raise PermissionAuditError("Binance futures server time response is invalid")
    monotonic_start = time.monotonic()

    def signed_get(endpoint: str) -> Any:
        timestamp = server_time + int((time.monotonic() - monotonic_start) * 1000)
        query = urlencode({"timestamp": timestamp, "recvWindow": 5000})
        signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), "sha256").hexdigest()
        return fetch_json_value(
            f"{BASE_URL}{endpoint}?{query}&signature={signature}",
            headers={"X-MBX-APIKEY": api_key},
            timeout=timeout,
        )

    return normalize_account_state(
        account=signed_get(ACCOUNT_ENDPOINT),
        position_mode=signed_get(POSITION_MODE_ENDPOINT),
        multi_asset_mode=signed_get(MULTI_ASSET_MODE_ENDPOINT),
        open_orders=signed_get(OPEN_ORDERS_ENDPOINT),
        open_algo_orders=signed_get(OPEN_ALGO_ORDERS_ENDPOINT),
        symbol_configurations=signed_get(SYMBOL_CONFIG_ENDPOINT),
        required_symbols=required_symbols,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("configuration must be a JSON object")
        required_symbols = config_symbols(config)
        env = dict(os.environ)
        if args.env_file:
            env.update(load_dotenv(args.env_file))
        api_key = env.get("BINANCE_FUTURES_API_KEY", "")
        api_secret = env.get("BINANCE_FUTURES_API_SECRET", "")
        if len(api_key) < 16 or len(api_secret) < 16:
            raise PermissionAuditError("Binance futures API credentials are missing or too short")
        state = fetch_account_state(
            api_key,
            api_secret,
            required_symbols=required_symbols,
            timeout=args.timeout,
        )
        receipt = build_receipt(state, api_key=api_key, required_symbols=required_symbols)
        atomic_write_json(args.output, receipt)
    except (OSError, json.JSONDecodeError, ValueError, PermissionAuditError) as error:
        print(f"FAIL: Binance futures account audit could not complete ({type(error).__name__})")
        sys.exit(2)

    print(f"Futures account audit receipt: {args.output}")
    if receipt["status"] != "PASS":
        for failure in receipt["failures"]:
            print(f"FAIL: {failure}")
        sys.exit(2)
    print("PASS: account is flat, order-free, one-way, single-asset, isolated, and 1x.")


if __name__ == "__main__":
    main()
