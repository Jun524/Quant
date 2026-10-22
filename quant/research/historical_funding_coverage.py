"""Audit dynamic-universe funding availability before historical net trials."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from quant.data.historical_universe import verify_historical_daily_universe
from quant.research.historical_execution_coverage import _periods
from quant.research.historical_funding_ledger import (
    HistoricalFundingLedgerError,
    verify_historical_funding_manifest,
)


class HistoricalFundingCoverageError(ValueError):
    """The selected trading days cannot be reconciled to funding archives."""


def _month_keys(entry_day: date, holding_days: int) -> list[str]:
    if holding_days < 1:
        raise HistoricalFundingCoverageError("holding_days must be positive")
    entry = datetime.combine(entry_day, datetime.min.time(), tzinfo=timezone.utc)
    exit_time = entry + timedelta(days=holding_days)
    cursor = date(entry.year, entry.month, 1)
    final = date((exit_time - timedelta(milliseconds=1)).year, (exit_time - timedelta(milliseconds=1)).month, 1)
    months: list[str] = []
    while cursor <= final:
        months.append(cursor.isoformat())
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    return months


def audit_historical_funding_coverage(
    artifact: Mapping[str, Any], policy: Mapping[str, Any], manifest: Mapping[str, Any], *, holding_days: int = 1
) -> dict[str, Any]:
    """Check funding archive presence for every selected symbol-day and fixed hold.

    This does not compute PnL.  It asserts only that every month touched by a
    prospective next-open position can later be revalidated against the
    publisher archive.
    """
    universe = verify_historical_daily_universe(artifact)
    funding = verify_historical_funding_manifest(manifest)
    if funding["universe_sha256"] != universe["universe_sha256"]:
        raise HistoricalFundingCoverageError("funding manifest belongs to another universe")
    records = {(str(item["symbol"]), str(item["month"])) for item in manifest["records"]}
    missing_sources = {(str(item["symbol"]), str(item["month"])) for item in manifest["not_found"]}
    membership = artifact["membership"]
    assert isinstance(membership, Mapping)
    reports = []
    total_required, total_covered = 0, 0
    for label, start, end in _periods(policy):
        required, covered, unavailable = 0, 0, []
        for raw_day, entry in membership.items():
            decision_day = date.fromisoformat(str(raw_day))
            if not start <= decision_day <= end:
                continue
            assert isinstance(entry, Mapping)
            for symbol in entry["selected"]:
                for month in _month_keys(decision_day + timedelta(days=1), holding_days):
                    required += 1
                    key = (str(symbol), month)
                    if key in records:
                        covered += 1
                    elif len(unavailable) < 20:
                        unavailable.append({"decision_day": decision_day.isoformat(), "symbol": str(symbol), "month": month, "source_state": "NOT_FOUND" if key in missing_sources else "NOT_IN_MANIFEST"})
        total_required += required
        total_covered += covered
        reports.append({"period": label, "funding_symbol_month_requirements": required, "covered_requirements": covered, "uncovered_requirements": required - covered, "coverage_ratio": covered / required if required else None, "sample_uncovered": unavailable, "status": "PASS" if required > 0 and required == covered else "FAIL"})
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["status"] == "PASS" for item in reports) else "FAIL",
        "kind": "HISTORICAL_FUNDING_AVAILABILITY_ONLY",
        "universe_sha256": universe["universe_sha256"],
        "holding_days": holding_days,
        "periods": reports,
        "total_requirements": total_required,
        "total_covered_requirements": total_covered,
        "coverage_ratio": total_covered / total_required if total_required else None,
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "live_order_authorization": "CLOSED",
        "warning": "Funding availability does not calculate funding PnL or validate a strategy; every trial must still replay events for its exact holding interval.",
    }
