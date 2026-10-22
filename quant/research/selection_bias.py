"""Selection-bias diagnostics for strategy and portfolio promotion.

The Deflated Sharpe Ratio follows Bailey and Lopez de Prado (2014): the
selected periodic Sharpe is tested against the expected maximum produced by
the full trial inventory, with skewness and kurtosis in the sampling error.
CSCV/PBO follows Bailey et al. (2017): select the best trial in each in-sample
combination and measure how often it ranks below the median out of sample.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
import json
from math import e, log, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np


EULER_MASCHERONI = 0.5772156649015329
SELECTION_BIAS_PROTOCOL_FILES = (
    "quant/research/selection_bias.py",
    "scripts/selection_bias_audit.py",
)


@dataclass(frozen=True)
class SelectionBiasPolicy:
    minimum_observations: int = 252
    minimum_deflated_sharpe_probability: float = 0.95
    maximum_probability_backtest_overfitting: float = 0.10
    cscv_slices: int = 8


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sha256_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def load_daily_trial_matrix(
    path: Path, timestamp_column: str = "timestamp"
) -> tuple[list[str], list[str], np.ndarray]:
    """Load a complete ordered daily net-return matrix with a strict time axis."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        if timestamp_column not in headers:
            raise ValueError(f"missing timestamp column: {timestamp_column}")
        trial_ids = [item for item in headers if item != timestamp_column]
        if (
            not trial_ids
            or len(set(trial_ids)) != len(trial_ids)
            or not all(isinstance(item, str) and item for item in trial_ids)
        ):
            raise ValueError("trial columns must be unique and non-empty")
        timestamps: list[str] = []
        parsed_timestamps: list[datetime] = []
        rows: list[list[float]] = []
        for line_number, row in enumerate(reader, start=2):
            raw_timestamp = str(row.get(timestamp_column) or "")
            parsed_timestamp = _parse_utc(raw_timestamp)
            try:
                values = [float(row[item]) for item in trial_ids]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid return in row {line_number}: {exc}") from exc
            if parsed_timestamp is None:
                raise ValueError(f"invalid timezone-aware timestamp in row {line_number}")
            if not np.isfinite(values).all():
                raise ValueError(f"row {line_number} contains a non-finite return")
            timestamps.append(raw_timestamp)
            parsed_timestamps.append(parsed_timestamp)
            rows.append(values)
    if len(rows) < 3:
        raise ValueError("at least three return observations are required")
    if any(right <= left for left, right in zip(parsed_timestamps, parsed_timestamps[1:])):
        raise ValueError("timestamps must be unique and strictly increasing")
    return timestamps, trial_ids, np.asarray(rows, dtype=float)


def validate_trial_inventory(
    inventory: Any,
    *,
    trial_ids: list[str],
    first_observation_at: str,
) -> dict[str, Any]:
    """Validate that every trial was sealed before its common locked sample."""
    failures: list[str] = []
    if not isinstance(inventory, dict):
        return {"status": "FAIL", "failures": ["trial inventory must be one JSON object"]}
    if inventory.get("template_only_not_evidence") is True:
        failures.append("the template file cannot be used as evidence")
    if inventory.get("schema_version") != 1:
        failures.append("trial inventory schema_version must equal 1")
    if not isinstance(inventory.get("inventory_id"), str) or not inventory.get("inventory_id"):
        failures.append("inventory_id is missing")
    if not isinstance(inventory.get("family_id"), str) or not inventory.get("family_id"):
        failures.append("family_id is missing")
    if inventory.get("sample_role") != "locked_forward":
        failures.append("sample_role must be locked_forward")
    if inventory.get("return_periodicity") != "daily":
        failures.append("return_periodicity must be daily")
    if inventory.get("return_definition") != "net_of_fees_slippage_funding":
        failures.append("return_definition must be net_of_fees_slippage_funding")
    if inventory.get("complete_attempted_trial_inventory") is not True:
        failures.append("complete_attempted_trial_inventory must be true")
    if inventory.get("selection_rule_preregistered") is not True:
        failures.append("selection_rule_preregistered must be true")
    if not _sha256_text(inventory.get("selection_rule_sha256")):
        failures.append("selection_rule_sha256 is missing or invalid")

    sample_started = _parse_utc(inventory.get("sample_started_at_utc"))
    first_observation = _parse_utc(first_observation_at)
    inventory_closed = _parse_utc(inventory.get("inventory_closed_at_utc"))
    selected_at = _parse_utc(inventory.get("selected_at_utc"))
    if sample_started is None or first_observation is None or sample_started != first_observation:
        failures.append("sample_started_at_utc must equal the first return observation")
    if inventory_closed is None or selected_at is None:
        failures.append("inventory_closed_at_utc and selected_at_utc must be timezone-aware")
    elif sample_started is None or not (selected_at <= inventory_closed < sample_started):
        failures.append("selection must precede inventory close and both must precede sample")

    selected_trial_id = inventory.get("selected_trial_id")
    if (
        not trial_ids
        or len(set(trial_ids)) != len(trial_ids)
        or not all(isinstance(item, str) and item for item in trial_ids)
    ):
        failures.append("return trial IDs must be unique and non-empty")
    if selected_trial_id not in trial_ids:
        failures.append("selected_trial_id is absent from the return columns")
    trials = inventory.get("trials")
    if not isinstance(trials, list):
        failures.append("trials must be a list")
        trials = []
    inventory_ids = [item.get("trial_id") for item in trials if isinstance(item, dict)]
    if len(inventory_ids) != len(trials) or inventory_ids != trial_ids:
        failures.append("ordered trial inventory must exactly match the return columns")
    for attempt_number, item in enumerate(trials, start=1):
        if not isinstance(item, dict):
            failures.append(f"trial {attempt_number} is invalid")
            continue
        trial_id = item.get("trial_id")
        if item.get("attempt_number") != attempt_number:
            failures.append(f"{trial_id} attempt_number is not consecutive")
        if not _sha256_text(item.get("configuration_sha256")):
            failures.append(f"{trial_id} configuration_sha256 is missing or invalid")
        registered_at = _parse_utc(item.get("registered_at_utc"))
        if (
            registered_at is None
            or inventory_closed is None
            or sample_started is None
            or registered_at > inventory_closed
            or registered_at >= sample_started
        ):
            failures.append(f"{trial_id} registration does not precede the sealed sample")
    return {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "inventory_id": inventory.get("inventory_id"),
        "family_id": inventory.get("family_id"),
        "selected_trial_id": selected_trial_id,
        "attempted_trials": len(trials),
        "pre_registered_single_trial": len(trials) == 1,
    }


def _one_dimensional_finite(values: Any, *, name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or len(array) == 0 or not np.isfinite(array).all():
        raise ValueError(f"{name} must be a non-empty finite vector")
    return array


def periodic_sharpe(returns: Any) -> float:
    values = _one_dimensional_finite(returns, name="returns")
    if len(values) < 2:
        raise ValueError("at least two returns are required")
    standard_deviation = float(values.std(ddof=1))
    if standard_deviation <= 0:
        raise ValueError("returns must have positive sample variance")
    return float(values.mean() / standard_deviation)


def expected_maximum_sharpe(trial_sharpes: Any) -> float:
    """Expected maximum under a zero-Sharpe null for the disclosed trials."""
    sharpes = _one_dimensional_finite(trial_sharpes, name="trial_sharpes")
    count = len(sharpes)
    if count == 1:
        return 0.0
    dispersion = float(sharpes.std(ddof=1))
    if dispersion == 0:
        return 0.0
    normal = NormalDist()
    first = normal.inv_cdf(1.0 - 1.0 / count)
    second = normal.inv_cdf(1.0 - 1.0 / (count * e))
    return float(dispersion * ((1.0 - EULER_MASCHERONI) * first + EULER_MASCHERONI * second))


def deflated_sharpe_ratio(returns: Any, trial_sharpes: Any) -> dict[str, float | int]:
    values = _one_dimensional_finite(returns, name="returns")
    if len(values) < 3:
        raise ValueError("at least three selected-strategy returns are required")
    selected_sharpe = periodic_sharpe(values)
    centered = values - values.mean()
    population_standard_deviation = float(values.std(ddof=0))
    if population_standard_deviation <= 0:
        raise ValueError("returns must have positive variance")
    standardized = centered / population_standard_deviation
    skewness = float(np.mean(standardized**3))
    kurtosis = float(np.mean(standardized**4))
    benchmark = expected_maximum_sharpe(trial_sharpes)
    variance_term = (
        1.0
        - skewness * selected_sharpe
        + ((kurtosis - 1.0) / 4.0) * selected_sharpe**2
    )
    if not np.isfinite(variance_term) or variance_term <= 0:
        raise ValueError("Deflated Sharpe sampling variance is not positive")
    statistic = (
        (selected_sharpe - benchmark)
        * sqrt(len(values) - 1)
        / sqrt(variance_term)
    )
    return {
        "probability": float(NormalDist().cdf(statistic)),
        "selected_periodic_sharpe": selected_sharpe,
        "expected_maximum_periodic_sharpe": benchmark,
        "observations": len(values),
        "trials": len(_one_dimensional_finite(trial_sharpes, name="trial_sharpes")),
        "skewness": skewness,
        "kurtosis": kurtosis,
        "test_statistic": float(statistic),
    }


def trial_matrix_sha256(trial_return_matrix: Any, trial_ids: list[str]) -> str:
    """Hash an ordered trial inventory and its exact float64 return matrix."""
    matrix = np.asarray(trial_return_matrix, dtype="<f8")
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("trial_return_matrix must be a finite 2D matrix")
    if (
        len(trial_ids) != matrix.shape[1]
        or not all(isinstance(item, str) and item for item in trial_ids)
        or len(set(trial_ids)) != len(trial_ids)
    ):
        raise ValueError("trial_ids must be unique, non-empty, and match matrix columns")
    header = json.dumps(
        {"shape": list(matrix.shape), "trial_ids": trial_ids},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256()
    digest.update(header)
    digest.update(b"\0")
    digest.update(matrix.tobytes(order="C"))
    return digest.hexdigest()


def return_series_sha256(returns: Any) -> str:
    """Hash one exact finite return series using the portfolio MTM convention."""
    values = _one_dimensional_finite(returns, name="returns")
    return sha256(
        json.dumps(
            [float(value) for value in values],
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _column_sharpes(matrix: np.ndarray, rows: np.ndarray) -> np.ndarray:
    subset = matrix[rows]
    means = subset.mean(axis=0)
    deviations = subset.std(axis=0, ddof=1)
    return np.divide(
        means,
        deviations,
        out=np.full(matrix.shape[1], -np.inf, dtype=float),
        where=deviations > 0,
    )


def probability_backtest_overfitting(
    trial_return_matrix: Any, *, slices: int = 8
) -> dict[str, Any]:
    matrix = np.asarray(trial_return_matrix, dtype=float)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("trial_return_matrix must be a finite 2D matrix")
    observations, trials = matrix.shape
    if trials < 2:
        raise ValueError("PBO requires at least two candidate trials")
    if slices < 4 or slices % 2 or observations < slices * 2:
        raise ValueError("CSCV requires an even slice count >= 4 and at least two rows per slice")
    partitions = [np.asarray(item, dtype=int) for item in np.array_split(np.arange(observations), slices)]
    logits: list[float] = []
    below_median = 0
    for chosen in combinations(range(slices), slices // 2):
        selected_slices = set(chosen)
        in_sample = np.concatenate([partitions[index] for index in chosen])
        out_sample = np.concatenate(
            [partitions[index] for index in range(slices) if index not in selected_slices]
        )
        in_scores = _column_sharpes(matrix, in_sample)
        selected_trial = int(np.argmax(in_scores))
        out_scores = _column_sharpes(matrix, out_sample)
        selected_score = out_scores[selected_trial]
        # Average ranks make ties deterministic. rank/(N+1) keeps logits finite.
        rank = 1.0 + float(np.sum(out_scores < selected_score)) + 0.5 * float(
            np.sum(out_scores == selected_score) - 1
        )
        relative_rank = rank / (trials + 1.0)
        logit = log(relative_rank / (1.0 - relative_rank))
        logits.append(logit)
        below_median += int(logit <= 0.0)
    return {
        "probability": below_median / len(logits),
        "combinations": len(logits),
        "slices": slices,
        "trials": trials,
        "observations": observations,
        "median_logit": float(np.median(logits)),
        "logits": logits,
    }


def evaluate_selection_bias(
    selected_returns: Any,
    trial_return_matrix: Any,
    *,
    attempted_trials: int,
    trial_ids: list[str],
    selected_trial_id: str,
    trial_inventory_complete: bool,
    pre_registered_single_trial: bool = False,
    policy: SelectionBiasPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or SelectionBiasPolicy()
    matrix = np.asarray(trial_return_matrix, dtype=float)
    if matrix.ndim != 2 or not np.isfinite(matrix).all():
        raise ValueError("trial_return_matrix must be a finite 2D matrix")
    if attempted_trials != matrix.shape[1] or attempted_trials < 1:
        raise ValueError("attempted trial inventory does not match the return matrix")
    matrix_digest = trial_matrix_sha256(matrix, trial_ids)
    if selected_trial_id not in trial_ids:
        raise ValueError("selected_trial_id is not present in trial_ids")
    selected = _one_dimensional_finite(selected_returns, name="selected_returns")
    if len(selected) != matrix.shape[0]:
        raise ValueError("selected returns and trial matrix must share observations")
    selected_column = trial_ids.index(selected_trial_id)
    if not np.array_equal(selected, matrix[:, selected_column], equal_nan=False):
        raise ValueError("selected returns do not equal the disclosed selected trial column")
    trial_sharpes = np.array(
        [periodic_sharpe(matrix[:, column]) for column in range(matrix.shape[1])]
    )
    dsr = deflated_sharpe_ratio(selected, trial_sharpes)
    if attempted_trials == 1:
        if not pre_registered_single_trial:
            raise ValueError("one-trial PBO exemption requires prior registration")
        pbo = {
            "status": "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL",
            "probability": 0.0,
            "slices": None,
            "trials": 1,
            "observations": len(selected),
        }
    else:
        pbo = {
            "status": "CALCULATED",
            **probability_backtest_overfitting(matrix, slices=policy.cscv_slices),
        }
    checks = [
        {
            "name": "trial_inventory_complete",
            "passed": trial_inventory_complete is True,
            "actual": trial_inventory_complete,
            "required": True,
        },
        {
            "name": "minimum_observations",
            "passed": len(selected) >= policy.minimum_observations,
            "actual": len(selected),
            "required": policy.minimum_observations,
        },
        {
            "name": "deflated_sharpe_probability",
            "passed": dsr["probability"] >= policy.minimum_deflated_sharpe_probability,
            "actual": dsr["probability"],
            "required": policy.minimum_deflated_sharpe_probability,
        },
        {
            "name": "probability_backtest_overfitting",
            "passed": pbo["probability"] <= policy.maximum_probability_backtest_overfitting,
            "actual": pbo["probability"],
            "required": policy.maximum_probability_backtest_overfitting,
        },
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "policy": asdict(policy),
        "attempted_trials": attempted_trials,
        "trial_ids": trial_ids,
        "selected_trial_id": selected_trial_id,
        "selected_return_sha256": return_series_sha256(selected),
        "trial_return_matrix_sha256": matrix_digest,
        "trial_inventory_complete": trial_inventory_complete,
        "pre_registered_single_trial": pre_registered_single_trial,
        "deflated_sharpe": dsr,
        "pbo": pbo,
        "checks": checks,
        "warning": "This gate reduces selection bias; it does not prove a persistent trading edge.",
    }
