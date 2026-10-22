"""Fail-closed availability receipts for funding-based forward decisions.

A funding event timestamp alone says when funding settled, not when a local
research process had the value.  This module audits separately captured,
time-bounded decision receipts and deliberately refuses retrospective REST
reconstruction as decision-time evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any


def _utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def expected_decisions(
    preregistration: dict[str, Any], *, now: datetime
) -> list[datetime]:
    """Return decisions whose one-hour entry deadline has already elapsed."""

    fresh = preregistration.get("fresh_sample") or {}
    fixed = preregistration.get("fixed_signal_and_execution") or {}
    first = _utc(str(fresh["first_eligible_decision"]))
    if fixed.get("decision_time") != "Monday 04:00 UTC":
        raise ValueError("unexpected decision contract")
    if fixed.get("entry") != "next 1h candle open at Monday 05:00 UTC":
        raise ValueError("unexpected entry contract")
    if first.weekday() != 0 or (first.hour, first.minute, first.second) != (4, 0, 0):
        raise ValueError("first eligible decision violates the fixed schedule")
    values: list[datetime] = []
    cursor = first
    now = _utc(now)
    while cursor + timedelta(hours=1) <= now:
        values.append(cursor)
        cursor += timedelta(days=7)
    return values


def receipt_filename(decision_at: datetime) -> str:
    return f"{decision_at.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"


def audit_decision_availability(
    preregistration: dict[str, Any],
    *,
    preregistration_sha256: str,
    receipt_directory: Path,
    now: datetime,
) -> dict[str, Any]:
    """Audit that every elapsed decision had a receipt before entry.

    ``PASS_AVAILABILITY_ONLY`` only proves collection timing.  It is not a
    performance result and never authorizes promotion or orders.
    """

    decisions = expected_decisions(preregistration, now=now)
    fixed = preregistration["fixed_signal_and_execution"]
    symbols = fixed["universe"]
    required_events = fixed["minimum_funding_events_per_symbol"]
    failures: list[dict[str, str]] = []
    verified: list[str] = []
    for decision in decisions:
        path = receipt_directory / receipt_filename(decision)
        if not path.is_file():
            failures.append({"decision_at": decision.isoformat(), "reason": "missing_receipt"})
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            captured_at = _utc(str(receipt["captured_at_utc"]))
            received = receipt["funding_events"]
            if not isinstance(received, dict):
                raise ValueError("funding_events is not an object")
            valid_events = (
                sorted(received) == sorted(symbols)
                and all(
                    isinstance(received[symbol], list)
                    and len(received[symbol]) >= required_events
                    and all(
                        _utc(str(event["funding_at_utc"])) <= decision
                        for event in received[symbol]
                    )
                    for symbol in symbols
                )
            )
            valid = (
                receipt.get("schema_version") == 1
                and receipt.get("kind") == "FUNDING_DECISION_AVAILABILITY_RECEIPT"
                and receipt.get("decision_at_utc") == decision.isoformat()
                and receipt.get("preregistration_sha256") == preregistration_sha256
                and decision <= captured_at <= decision + timedelta(hours=1)
                and valid_events
            )
            if not valid:
                raise ValueError("receipt contract mismatch")
            verified.append(decision.isoformat())
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            failures.append({"decision_at": decision.isoformat(), "reason": f"invalid_receipt:{type(error).__name__}"})
    if not decisions:
        status = "WAITING_FIRST_DECISION_DEADLINE"
    elif failures:
        status = "CAUSAL_AVAILABILITY_VETO"
    else:
        status = "PASS_AVAILABILITY_ONLY"
    return {
        "schema_version": 1,
        "kind": "FUNDING_DECISION_AVAILABILITY_AUDIT",
        "preregistration_sha256": preregistration_sha256,
        "status": status,
        "promotion_eligible": False,
        "live_order_authorization": "CLOSED",
        "elapsed_decisions": len(decisions),
        "verified_decisions": verified,
        "failures": failures,
        "limitation": "Funding history fetched after a decision is not accepted as evidence that the value was available before entry.",
    }


def preregistration_digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()
