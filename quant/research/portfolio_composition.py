"""Source-replayed portfolio sleeve contribution and diversification evidence."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite, sqrt
from pathlib import Path
from typing import Any

from quant.research.selection_bias import return_series_sha256


PORTFOLIO_COMPOSITION_PROTOCOL_FILES = (
    "quant/research/portfolio_composition.py",
    "quant/research/selection_bias.py",
    "scripts/portfolio_promotion_report.py",
)


def _parse_midnight_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("portfolio contribution timestamps must include a UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.time() != datetime.min.time():
        raise ValueError("portfolio contribution timestamps must be 00:00:00 UTC")
    return parsed


def load_portfolio_contributions(
    path: Path, candidate_order: list[str]
) -> dict[str, Any]:
    if not candidate_order or len(candidate_order) != len(set(candidate_order)):
        raise ValueError("portfolio candidate order must be non-empty and unique")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["timestamp", *candidate_order]:
            raise ValueError(
                "portfolio contribution CSV header must be timestamp followed by "
                "manifest sleeve candidates in exact order"
            )
        timestamps: list[datetime] = []
        contributions = {candidate: [] for candidate in candidate_order}
        for line_number, row in enumerate(reader, start=2):
            timestamp = _parse_midnight_utc(str(row.get("timestamp") or ""))
            values: dict[str, float] = {}
            for candidate in candidate_order:
                try:
                    value = float(row.get(candidate) or "")
                except ValueError as exc:
                    raise ValueError(
                        f"row {line_number} {candidate} contribution is invalid"
                    ) from exc
                if not isfinite(value) or value <= -1.0:
                    raise ValueError(
                        f"row {line_number} {candidate} contribution must be finite and greater than -1"
                    )
                values[candidate] = value
            portfolio_return = sum(values.values())
            if portfolio_return <= -1.0:
                raise ValueError(
                    f"row {line_number} summed portfolio return must be greater than -1"
                )
            timestamps.append(timestamp)
            for candidate, value in values.items():
                contributions[candidate].append(value)
    if not timestamps:
        raise ValueError("portfolio contribution matrix is empty")
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous != timedelta(days=1):
            raise ValueError("portfolio contributions must contain every consecutive UTC day")
    portfolio_returns = [
        sum(contributions[candidate][index] for candidate in candidate_order)
        for index in range(len(timestamps))
    ]
    return {
        "timestamps": timestamps,
        "candidate_order": candidate_order,
        "contributions": contributions,
        "portfolio_returns": portfolio_returns,
        "portfolio_returns_sha256": sha256(
            json.dumps(
                portfolio_returns, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest(),
    }


def load_sleeve_daily_pnl(
    path: Path, candidate_order: list[str]
) -> dict[str, Any]:
    """Load daily sleeve net PnL as fractions of initial portfolio equity."""
    if not candidate_order or len(candidate_order) != len(set(candidate_order)):
        raise ValueError("portfolio candidate order must be non-empty and unique")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["timestamp", *candidate_order]:
            raise ValueError(
                "sleeve daily PnL CSV header must be timestamp followed by "
                "manifest sleeve candidates in exact order"
            )
        timestamps: list[datetime] = []
        daily_net_pnl = {candidate: [] for candidate in candidate_order}
        for line_number, row in enumerate(reader, start=2):
            timestamp = _parse_midnight_utc(str(row.get("timestamp") or ""))
            timestamps.append(timestamp)
            for candidate in candidate_order:
                try:
                    value = float(row.get(candidate) or "")
                except ValueError as exc:
                    raise ValueError(
                        f"row {line_number} {candidate} sleeve PnL is invalid"
                    ) from exc
                if not isfinite(value):
                    raise ValueError(
                        f"row {line_number} {candidate} sleeve PnL must be finite"
                    )
                daily_net_pnl[candidate].append(value)
    if not timestamps:
        raise ValueError("sleeve daily PnL matrix is empty")
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous != timedelta(days=1):
            raise ValueError("sleeve daily PnL must contain every consecutive UTC day")
    return {
        "timestamps": timestamps,
        "candidate_order": candidate_order,
        "daily_net_pnl": daily_net_pnl,
    }


def _correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or not left:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    left_variance = sum(value * value for value in left_centered)
    right_variance = sum(value * value for value in right_centered)
    if left_variance <= 0.0 or right_variance <= 0.0:
        return None
    covariance = sum(
        left_value * right_value
        for left_value, right_value in zip(left_centered, right_centered)
    )
    return covariance / sqrt(left_variance * right_variance)


def evaluate_portfolio_composition(
    matrix: dict[str, Any],
    equity: dict[str, Any],
    sleeve_pnl: dict[str, Any],
    sleeve_initial_weights: dict[str, Any],
) -> dict[str, Any]:
    timestamps = list(matrix.get("timestamps") or [])
    candidate_order = [str(value) for value in matrix.get("candidate_order") or []]
    contributions = matrix.get("contributions") or {}
    portfolio_returns = [float(value) for value in matrix.get("portfolio_returns") or []]
    equity_returns = [float(value) for value in equity.get("daily_returns") or []]
    first_equity = datetime.fromisoformat(
        str(equity.get("first_timestamp") or "").replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    expected_timestamps = [
        first_equity + timedelta(days=index + 1) for index in range(len(equity_returns))
    ]
    aligned = timestamps == expected_timestamps and len(portfolio_returns) == len(
        equity_returns
    )
    reconciliation_errors = (
        [abs(left - right) for left, right in zip(portfolio_returns, equity_returns)]
        if aligned
        else []
    )
    maximum_reconciliation_error = max(reconciliation_errors, default=None)
    sleeve_pnl_timestamps = list(sleeve_pnl.get("timestamps") or [])
    sleeve_pnl_candidates = [
        str(value) for value in sleeve_pnl.get("candidate_order") or []
    ]
    daily_net_pnl = sleeve_pnl.get("daily_net_pnl") or {}
    sleeve_pnl_aligned = bool(
        sleeve_pnl_candidates == candidate_order
        and sleeve_pnl_timestamps == expected_timestamps
        and all(
            isinstance(daily_net_pnl.get(candidate), list)
            and len(daily_net_pnl[candidate]) == len(equity_returns)
            for candidate in candidate_order
        )
    )
    equity_fractions = [1.0]
    for value in equity_returns:
        equity_fractions.append(equity_fractions[-1] * (1.0 + value))
    sleeve_pnl_reconciliation_errors: list[float] = []
    contribution_binding_errors: list[float] = []
    if sleeve_pnl_aligned and aligned:
        for index in range(len(equity_returns)):
            sleeve_total = sum(
                float(daily_net_pnl[candidate][index])
                for candidate in candidate_order
            )
            sleeve_pnl_reconciliation_errors.append(
                abs(
                    sleeve_total
                    - (equity_fractions[index + 1] - equity_fractions[index])
                )
            )
            for candidate in candidate_order:
                expected_contribution = (
                    float(daily_net_pnl[candidate][index])
                    / equity_fractions[index]
                )
                contribution_binding_errors.append(
                    abs(
                        float(contributions[candidate][index])
                        - expected_contribution
                    )
                )
    maximum_sleeve_pnl_error = max(
        sleeve_pnl_reconciliation_errors, default=None
    )
    maximum_contribution_binding_error = max(
        contribution_binding_errors, default=None
    )
    sleeve_net_pnl_sha256 = {
        candidate: sha256(
            json.dumps(
                daily_net_pnl.get(candidate) or [],
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        for candidate in candidate_order
    }
    try:
        normalized_weights = {
            candidate: float(sleeve_initial_weights[candidate])
            for candidate in candidate_order
        }
    except (KeyError, TypeError, ValueError):
        normalized_weights = {}
    weights_valid = bool(
        set(sleeve_initial_weights) == set(candidate_order)
        and set(normalized_weights) == set(candidate_order)
        and all(isfinite(value) and value > 0.0 for value in normalized_weights.values())
        and abs(sum(normalized_weights.values()) - 1.0) <= 1e-12
    )
    sleeve_daily_returns: dict[str, list[float]] = {
        candidate: [] for candidate in candidate_order
    }
    minimum_sleeve_equity_fraction: float | None = None
    sleeves_self_financing = sleeve_pnl_aligned and weights_valid
    if sleeves_self_financing:
        for candidate in candidate_order:
            sleeve_equity = normalized_weights[candidate]
            minimum_sleeve_equity_fraction = (
                sleeve_equity
                if minimum_sleeve_equity_fraction is None
                else min(minimum_sleeve_equity_fraction, sleeve_equity)
            )
            for pnl_fraction in daily_net_pnl[candidate]:
                if sleeve_equity <= 0.0:
                    sleeves_self_financing = False
                    break
                sleeve_return = float(pnl_fraction) / sleeve_equity
                if not isfinite(sleeve_return) or sleeve_return <= -1.0:
                    sleeves_self_financing = False
                    break
                sleeve_daily_returns[candidate].append(sleeve_return)
                sleeve_equity += float(pnl_fraction)
                minimum_sleeve_equity_fraction = min(
                    float(minimum_sleeve_equity_fraction), sleeve_equity
                )
            if not sleeves_self_financing:
                break
    sleeve_daily_return_sha256 = (
        {
            candidate: return_series_sha256(sleeve_daily_returns[candidate])
            for candidate in candidate_order
        }
        if sleeves_self_financing
        else {}
    )
    pairwise: list[dict[str, Any]] = []
    correlations_defined = sleeves_self_financing
    for left_index, left in enumerate(candidate_order):
        for right in candidate_order[left_index + 1 :]:
            correlation = _correlation(
                list(sleeve_daily_returns.get(left) or []),
                list(sleeve_daily_returns.get(right) or []),
            )
            correlations_defined = correlations_defined and correlation is not None
            pairwise.append(
                {"left": left, "right": right, "correlation": correlation}
            )
    positive_totals = {
        candidate: sum(max(0.0, float(value)) for value in daily_net_pnl.get(candidate) or [])
        for candidate in candidate_order
    }
    total_positive = sum(positive_totals.values())
    positive_shares = (
        {
            candidate: positive_totals[candidate] / total_positive
            for candidate in candidate_order
        }
        if total_positive > 0.0
        else {}
    )
    checks = [
        {
            "name": "exact_mtm_date_alignment",
            "passed": aligned,
            "actual": {
                "contribution_first": timestamps[0].isoformat() if timestamps else None,
                "contribution_last": timestamps[-1].isoformat() if timestamps else None,
                "contribution_rows": len(timestamps),
                "equity_returns": len(equity_returns),
            },
            "required": "one contribution row for every MTM return date",
        },
        {
            "name": "daily_mtm_return_reconciliation",
            "passed": maximum_reconciliation_error is not None
            and maximum_reconciliation_error <= 1e-12,
            "actual": maximum_reconciliation_error,
            "required": "<= 1e-12",
        },
        {
            "name": "sleeve_pnl_exact_mtm_date_alignment",
            "passed": sleeve_pnl_aligned,
            "actual": {
                "sleeve_pnl_first": (
                    sleeve_pnl_timestamps[0].isoformat()
                    if sleeve_pnl_timestamps
                    else None
                ),
                "sleeve_pnl_last": (
                    sleeve_pnl_timestamps[-1].isoformat()
                    if sleeve_pnl_timestamps
                    else None
                ),
                "sleeve_pnl_rows": len(sleeve_pnl_timestamps),
            },
            "required": "one manifest-ordered sleeve PnL row for every MTM return date",
        },
        {
            "name": "sleeve_pnl_mtm_reconciliation",
            "passed": maximum_sleeve_pnl_error is not None
            and maximum_sleeve_pnl_error <= 1e-12,
            "actual": maximum_sleeve_pnl_error,
            "required": "daily sleeve net PnL fractions sum to the normalized MTM equity change within 1e-12",
        },
        {
            "name": "sleeve_contribution_binding",
            "passed": maximum_contribution_binding_error is not None
            and maximum_contribution_binding_error <= 1e-12,
            "actual": maximum_contribution_binding_error,
            "required": "every contribution equals its sleeve net PnL divided by prior-day portfolio MTM equity within 1e-12",
        },
        {
            "name": "self_financing_sleeve_return_reconstruction",
            "passed": sleeves_self_financing
            and minimum_sleeve_equity_fraction is not None
            and minimum_sleeve_equity_fraction > 0.0,
            "actual": {
                "minimum_sleeve_equity_fraction": minimum_sleeve_equity_fraction,
                "return_series_sha256": sleeve_daily_return_sha256,
            },
            "required": "positive self-financing sleeve equity paths derived from preregistered initial weights",
        },
        {
            "name": "pairwise_self_financing_return_correlations_defined",
            "passed": correlations_defined and bool(pairwise),
            "actual": pairwise,
            "required": "finite correlation of source-replayed self-financing daily returns for every sleeve pair",
        },
        {
            "name": "source_net_pnl_positive_share_available",
            "passed": bool(positive_shares)
            and set(positive_shares) == set(candidate_order),
            "actual": positive_shares,
            "required": "positive source sleeve-day net PnL exists and is allocated to every sleeve",
        },
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "candidate_order": candidate_order,
        "observations": len(timestamps),
        "first_timestamp": timestamps[0].isoformat() if timestamps else None,
        "last_timestamp": timestamps[-1].isoformat() if timestamps else None,
        "portfolio_returns_sha256": matrix.get("portfolio_returns_sha256"),
        "equity_daily_returns_sha256": equity.get("daily_returns_sha256"),
        "maximum_daily_reconciliation_error": maximum_reconciliation_error,
        "maximum_daily_sleeve_pnl_reconciliation_error": maximum_sleeve_pnl_error,
        "maximum_sleeve_contribution_binding_error": maximum_contribution_binding_error,
        "sleeve_net_pnl_fraction_sha256": sleeve_net_pnl_sha256,
        "sleeve_daily_return_sha256": sleeve_daily_return_sha256,
        "minimum_sleeve_equity_fraction": minimum_sleeve_equity_fraction,
        "pairwise_daily_correlation_basis": "self_financing_sleeve_daily_returns",
        "pairwise_daily_correlations": pairwise,
        "positive_pnl_share_basis": "source_sleeve_daily_net_pnl_fraction_of_initial_portfolio_equity",
        "positive_pnl_share_by_sleeve": positive_shares,
        "checks": checks,
        "definition": "Each contribution column must equal its source-bound sleeve daily net PnL fraction of initial portfolio equity divided by prior-day normalized portfolio MTM equity; both the sleeve PnL sum and contribution sum must reproduce the same portfolio MTM path. Diversification correlation is measured on reconstructed self-financing sleeve returns, while positive-PnL concentration is measured on source sleeve net PnL.",
    }
