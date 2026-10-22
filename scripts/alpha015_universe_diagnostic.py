#!/usr/bin/env python3
"""Write an operational, non-promotional Alpha 015 universe diagnostic."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.data.universe_history import assess_forward_universe_history
from quant.data.universe_protocol import policy_document, protocol_digests
from quant.monitoring.alpha015_milestones import build_alpha015_collection_milestones
from quant.monitoring.alpha015_universe_diagnostic import summarize_eligible_membership


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def build_report(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    snapshot_directory = project_root / "research" / "universe_snapshots"
    policy = policy_document()
    universe = assess_forward_universe_history(
        snapshot_directory,
        expected_protocol=protocol_digests(),
        policy=policy,
    )
    valid_dates = set(universe.get("valid_days") or [])
    memberships: list[tuple[str, set[str]]] = []
    parse_failures: list[dict[str, str]] = []
    seen_dates: set[str] = set()
    for path in sorted(snapshot_directory.glob("universe-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            scheduled = (payload.get("metadata") or {}).get("scheduled_date")
            # Older diagnostic snapshots intentionally lack the forward
            # scheduler date.  They are outside `valid_days` and must not turn
            # a clean current-protocol collection into a false integrity alarm.
            if scheduled is None:
                continue
            scheduled_date = str(scheduled)
            if scheduled_date not in valid_dates:
                continue
            if scheduled_date in seen_dates:
                raise ValueError("duplicate snapshot for valid scheduled date")
            seen_dates.add(scheduled_date)
            symbols = {
                str(row["symbol"])
                for row in payload.get("instruments") or []
                if row.get("eligible") is True and str(row.get("symbol") or "")
            }
            memberships.append((scheduled_date, symbols))
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as error:
            parse_failures.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    summary = summarize_eligible_membership(memberships)
    milestones = build_alpha015_collection_milestones(
        current_days=int(universe.get("consecutive_days_ending_latest") or 0),
        universe_status=str(universe.get("status") or "MISSING"),
    )
    due = [item["name"] for item in milestones if item["status"] == "REVIEW_DUE"]
    integrity_failures = {
        "DATA_INTEGRITY_FAIL",
        "ACCUMULATING_WITH_GAPS",
        "MISSING",
    }
    if str(universe.get("status")) in integrity_failures or parse_failures:
        status = "BLOCKED_DATA_INTEGRITY"
    elif due:
        status = "REVIEW_DUE"
    else:
        status = "ACCUMULATING"
    return {
        "schema_version": 1,
        "kind": "ALPHA015_RETURN_FREE_UNIVERSE_DIAGNOSTIC",
        "status": status,
        "universe_status": universe.get("status"),
        "current_consecutive_universe_days": universe.get("consecutive_days_ending_latest"),
        "required_consecutive_universe_days": universe.get("required_consecutive_days"),
        "milestones": milestones,
        "reviews_due": due,
        "membership_summary": summary,
        "snapshot_parse_failures": parse_failures,
        "promotion_capability": "DIAGNOSTIC_ONLY",
        "return_data_authorized": False,
        "experiment_registration_authorized": False,
        "live_order_authorization": "CLOSED",
        "warning": "This report evaluates sealed universe membership only; it cannot establish alpha or authorize return-data access.",
    }


def main() -> None:
    report = build_report()
    json_path = PROJECT_ROOT / "reports" / "alpha015-universe-diagnostic.json"
    markdown_path = PROJECT_ROOT / "reports" / "alpha015-universe-diagnostic.md"
    _atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    summary = report["membership_summary"]
    markdown = (
        "# Alpha 015 return-free universe diagnostic\n\n"
        f"Status: **{report['status']}**\n\n"
        f"Universe coverage: {report['current_consecutive_universe_days']} / "
        f"{report['required_consecutive_universe_days']} consecutive days.\n\n"
        f"Eligible symbols: minimum {summary['minimum_eligible_symbols']}, "
        f"median {summary['median_eligible_symbols']}, "
        f"maximum {summary['maximum_eligible_symbols']}.\n\n"
        f"Return or price data accessed: `{summary['return_or_price_data_accessed']}`.\n\n"
        f"{report['warning']}\n"
    )
    _atomic_write(markdown_path, markdown)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
