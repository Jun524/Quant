#!/usr/bin/env python3
"""Audit a Binance API key's permissions without placing or changing orders.

The signed endpoint used here is read-only.  The receipt deliberately excludes
the API key, secret, request signature, and Binance's account creation time.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.binance.com"
TIME_ENDPOINT = "/api/v3/time"
PERMISSION_ENDPOINT = "/sapi/v1/account/apiRestrictions"
SOURCE_ENDPOINT = f"{BASE_URL}{PERMISSION_ENDPOINT}"
RECEIPT_SCHEMA_VERSION = 1
POLICY_NAME = "binance-usdtm-futures-least-privilege-v1"
DEFAULT_RECEIPT = Path(__file__).resolve().parents[1] / "reports" / "binance-api-permission-audit.json"

REQUIRED_TRUE_PERMISSIONS = (
    "ipRestrict",
    "enableReading",
    "enableFutures",
)
REQUIRED_FALSE_PERMISSIONS = (
    "enableWithdrawals",
    "enableInternalTransfer",
    "enableMargin",
    "permitsUniversalTransfer",
    "enableVanillaOptions",
    "enableFixApiTrade",
    "enableFixReadOnly",
    "enableSpotAndMarginTrading",
    "enablePortfolioMarginTrading",
)
AUDITED_PERMISSION_FIELDS = REQUIRED_TRUE_PERMISSIONS + REQUIRED_FALSE_PERMISSIONS


class PermissionAuditError(RuntimeError):
    """A sanitized audit failure safe to display without leaking credentials."""


def api_key_fingerprint(api_key: str) -> str:
    """Return a non-reversible identifier suitable for binding a receipt."""
    return f"sha256:{sha256(api_key.encode('utf-8')).hexdigest()}"


def permission_failures(payload: dict[str, Any]) -> list[str]:
    """Return fail-closed least-privilege policy violations."""
    failures: list[str] = []
    for name in REQUIRED_TRUE_PERMISSIONS:
        if payload.get(name) is not True:
            failures.append(f"{name} must be true")
    for name in REQUIRED_FALSE_PERMISSIONS:
        if payload.get(name) is not False:
            failures.append(f"{name} must be false")
    unknown_permission_fields = sorted(
        name
        for name in payload
        if (name.startswith("enable") or name.startswith("permits") or name == "ipRestrict")
        and name not in AUDITED_PERMISSION_FIELDS
    )
    if unknown_permission_fields:
        failures.append(
            "unreviewed permission fields returned by Binance: " + ", ".join(unknown_permission_fields)
        )
    return failures


def sanitized_permissions(payload: dict[str, Any]) -> dict[str, bool | None]:
    """Keep only documented permission booleans; never persist account metadata."""
    return {
        name: payload.get(name) if isinstance(payload.get(name), bool) else None
        for name in AUDITED_PERMISSION_FIELDS
    }


def build_receipt(
    payload: dict[str, Any],
    *,
    api_key: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = observed_at or datetime.now(timezone.utc)
    permissions = sanitized_permissions(payload)
    failures = permission_failures(payload)
    unknown_permission_fields = sorted(
        name
        for name in payload
        if (name.startswith("enable") or name.startswith("permits") or name == "ipRestrict")
        and name not in AUDITED_PERMISSION_FIELDS
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "PASS" if not failures else "FAIL",
        "source_endpoint": SOURCE_ENDPOINT,
        "api_key_fingerprint": api_key_fingerprint(api_key),
        "policy": POLICY_NAME,
        "permissions": permissions,
        "unknown_permission_fields": unknown_permission_fields,
        "failures": failures,
    }


def permission_audit_receipt_failures(
    receipt: dict[str, Any],
    *,
    expected_api_key: str,
    now: datetime | None = None,
    maximum_age_seconds: int = 900,
) -> list[str]:
    """Validate freshness, key binding, origin, and policy from raw receipt fields."""
    now = now or datetime.now(timezone.utc)
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append("permission audit schema is unsupported")
    if receipt.get("source_endpoint") != SOURCE_ENDPOINT:
        failures.append("permission audit source endpoint is unexpected")
    if receipt.get("policy") != POLICY_NAME:
        failures.append("permission audit policy is unexpected")
    if len(expected_api_key) < 16:
        failures.append("current API key is unavailable for permission receipt binding")
    if receipt.get("api_key_fingerprint") != api_key_fingerprint(expected_api_key):
        failures.append("permission audit is bound to a different API key")

    observed_raw = receipt.get("observed_at")
    try:
        observed_at = datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00"))
        if observed_at.tzinfo is None:
            raise ValueError("timezone missing")
        age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
        if age < 0 or age > maximum_age_seconds:
            failures.append("permission audit is stale or future-dated")
    except (TypeError, ValueError):
        failures.append("permission audit timestamp is invalid")

    permissions = receipt.get("permissions")
    unknown_permission_fields = receipt.get("unknown_permission_fields")
    if not isinstance(permissions, dict):
        failures.append("permission audit has no permission object")
    else:
        failures.extend(f"permission audit: {item}" for item in permission_failures(permissions))
    if unknown_permission_fields != []:
        failures.append("permission audit contains unreviewed permission fields")
    if receipt.get("status") != "PASS":
        failures.append("permission audit status is not PASS")
    if receipt.get("failures") != []:
        failures.append("permission audit records policy failures")
    if (
        isinstance(permissions, dict)
        and not permission_failures(permissions)
        and unknown_permission_fields == []
    ):
        expected_status = "PASS"
    else:
        expected_status = "FAIL"
    if receipt.get("status") != expected_status:
        failures.append("permission audit status does not match its permission fields")
    return failures


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PermissionAuditError(f"cannot read environment file ({type(error).__name__})") from error
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise PermissionAuditError("environment file contains an invalid line")
        name, value = stripped.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def fetch_json_value(url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> Any:
    request = Request(url, headers=headers or {}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(65_537)
    except HTTPError as error:
        raise PermissionAuditError(f"Binance rejected the audit request (HTTP {error.code})") from error
    except URLError as error:
        raise PermissionAuditError(f"Binance audit network request failed ({type(error.reason).__name__})") from error
    except TimeoutError as error:
        raise PermissionAuditError("Binance audit network request timed out") from error
    if len(raw) > 65_536:
        raise PermissionAuditError("Binance audit response exceeded the size limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermissionAuditError("Binance audit returned invalid JSON") from error
    return payload


def fetch_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    payload = fetch_json_value(url, headers=headers, timeout=timeout)
    if not isinstance(payload, dict):
        raise PermissionAuditError("Binance audit returned a non-object response")
    return payload


def fetch_permissions(api_key: str, api_secret: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Call only Binance public time and read-only API-permission endpoints."""
    time_payload = fetch_json(f"{BASE_URL}{TIME_ENDPOINT}", timeout=timeout)
    server_time = time_payload.get("serverTime")
    if not isinstance(server_time, int) or server_time <= 0:
        raise PermissionAuditError("Binance server time response is invalid")
    query = urlencode({"timestamp": server_time, "recvWindow": 5000})
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), "sha256").hexdigest()
    return fetch_json(
        f"{SOURCE_ENDPOINT}?{query}&signature={signature}",
        headers={"X-MBX-APIKEY": api_key},
        timeout=timeout,
    )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    env = dict(os.environ)
    try:
        if args.env_file:
            env.update(load_dotenv(args.env_file))
        api_key = env.get("BINANCE_FUTURES_API_KEY", "")
        api_secret = env.get("BINANCE_FUTURES_API_SECRET", "")
        if len(api_key) < 16 or len(api_secret) < 16:
            raise PermissionAuditError("Binance futures API credentials are missing or too short")
        payload = fetch_permissions(api_key, api_secret, timeout=args.timeout)
        receipt = build_receipt(payload, api_key=api_key)
        atomic_write_json(args.output, receipt)
    except PermissionAuditError as error:
        print(f"FAIL: {error}")
        sys.exit(2)

    print(f"Permission audit receipt: {args.output}")
    if receipt["status"] != "PASS":
        for failure in receipt["failures"]:
            print(f"FAIL: {failure}")
        sys.exit(2)
    print("PASS: API key is IP-restricted and limited to reading plus futures trading.")


if __name__ == "__main__":
    main()
