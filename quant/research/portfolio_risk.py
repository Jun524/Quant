"""Source-bound multi-horizon drawdown and ruin audit for future portfolios."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import random
from statistics import NormalDist, mean, median
from typing import Any


PORTFOLIO_RISK_PROTOCOL_FILES = (
    "quant/research/portfolio_risk.py",
    "scripts/portfolio_risk_audit.py",
)


@dataclass(frozen=True)
class PortfolioRiskPolicy:
    minimum_daily_returns: int = 365
    horizon_days: int = 365
    short_block_days: int = 7
    long_block_days: int = 28
    simulations_per_block: int = 5_000
    ruin_equity_fraction: float = 0.50
    drawdown_threshold: float = 0.10
    annual_target_return: float = 1.10**12 - 1
    maximum_probability_of_ruin: float = 0.005
    maximum_probability_terminal_loss: float = 0.05
    # The 10% monthly return is an aspirational diagnostic, not a pass/fail
    # condition for a preservation-first portfolio.
    minimum_probability_reaching_annual_target: float = 0.0
    maximum_bootstrap_drawdown_p95: float = 0.10
    maximum_historical_drawdown: float = 0.10
    deterministic_daily_repeats: int = 3
    deterministic_weekly_repeats: int = 4
    deterministic_four_week_repeats: int = 2
    maximum_deterministic_stress_drawdown: float = 0.10
    binomial_confidence_level: float = 0.95
    seed: int = 1701


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def wilson_bounds(events: int, trials: int, confidence_level: float) -> tuple[float, float]:
    """Return one-sided Wilson lower/upper bounds for a binomial proportion."""
    if (
        isinstance(events, bool)
        or isinstance(trials, bool)
        or not isinstance(events, int)
        or not isinstance(trials, int)
        or trials <= 0
        or not 0 <= events <= trials
        or not 0.5 < confidence_level < 1.0
    ):
        raise ValueError("invalid Wilson interval inputs")
    probability = events / trials
    z_score = NormalDist().inv_cdf(confidence_level)
    z_squared = z_score * z_score
    denominator = 1.0 + z_squared / trials
    center = (probability + z_squared / (2.0 * trials)) / denominator
    margin = (
        z_score
        * (
            probability * (1.0 - probability) / trials
            + z_squared / (4.0 * trials * trials)
        )
        ** 0.5
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _parse_midnight_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("portfolio equity timestamps must include a UTC offset")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.time() != datetime.min.time():
        raise ValueError("portfolio equity timestamps must be 00:00:00 UTC")
    return parsed


def load_portfolio_equity(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["timestamp", "equity", "equity_type"]:
            raise ValueError("portfolio equity CSV header must be timestamp,equity,equity_type")
        points: list[tuple[datetime, float]] = []
        for line_number, row in enumerate(reader, start=2):
            timestamp = _parse_midnight_utc(str(row.get("timestamp") or ""))
            if str(row.get("equity_type") or "") != "mark_to_market":
                raise ValueError(f"row {line_number} is not mark_to_market equity")
            equity = float(row.get("equity") or 0.0)
            if not isfinite(equity) or equity <= 0:
                raise ValueError(f"row {line_number} equity must be finite and positive")
            points.append((timestamp, equity))
    if len(points) < 2:
        raise ValueError("portfolio equity needs at least two daily snapshots")
    for previous, current in zip(points, points[1:]):
        if current[0] - previous[0] != timedelta(days=1):
            raise ValueError("portfolio equity must contain every consecutive UTC day")
    returns = [current / previous - 1.0 for (_, previous), (_, current) in zip(points, points[1:])]
    if any(not isfinite(value) or value <= -1.0 for value in returns):
        raise ValueError("portfolio daily returns must be finite and greater than -1")
    peak = points[0][1]
    maximum_drawdown = 0.0
    for _, equity in points:
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
    return {
        "first_timestamp": points[0][0].isoformat(),
        "last_timestamp": points[-1][0].isoformat(),
        "snapshots": len(points),
        "daily_returns": returns,
        "daily_returns_sha256": sha256(
            json.dumps(returns, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest(),
        "historical_maximum_drawdown": maximum_drawdown,
    }


def _block_scenario(
    returns: list[float],
    *,
    block_days: int,
    policy: PortfolioRiskPolicy,
    seed: int,
) -> dict[str, Any]:
    starts = range(0, len(returns) - block_days + 1)
    generator = random.Random(seed)
    terminal_returns: list[float] = []
    maximum_drawdowns: list[float] = []
    ruin_count = drawdown_count = loss_count = target_count = 0
    for _ in range(policy.simulations_per_block):
        scenario: list[float] = []
        while len(scenario) < policy.horizon_days:
            start = generator.choice(starts)
            scenario.extend(returns[start : start + block_days])
        equity = peak = 1.0
        maximum_drawdown = 0.0
        ruined = False
        for value in scenario[: policy.horizon_days]:
            equity *= 1.0 + value
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
            ruined = ruined or equity <= policy.ruin_equity_fraction
        terminal = equity - 1.0
        terminal_returns.append(terminal)
        maximum_drawdowns.append(maximum_drawdown)
        ruin_count += int(ruined)
        drawdown_count += int(maximum_drawdown > policy.drawdown_threshold)
        loss_count += int(terminal < 0)
        target_count += int(terminal >= policy.annual_target_return)
    simulations = policy.simulations_per_block
    _, ruin_upper = wilson_bounds(
        ruin_count, simulations, policy.binomial_confidence_level
    )
    _, loss_upper = wilson_bounds(
        loss_count, simulations, policy.binomial_confidence_level
    )
    target_lower, _ = wilson_bounds(
        target_count, simulations, policy.binomial_confidence_level
    )
    return {
        "block_days": block_days,
        "simulations": simulations,
        "binomial_confidence_level": policy.binomial_confidence_level,
        "event_counts": {
            "ruin": ruin_count,
            "drawdown_exceeds_threshold": drawdown_count,
            "terminal_loss": loss_count,
            "reaches_annual_target": target_count,
        },
        "probability_of_ruin": ruin_count / simulations,
        "probability_of_ruin_upper_confidence": ruin_upper,
        "probability_drawdown_exceeds_threshold": drawdown_count / simulations,
        "probability_terminal_loss": loss_count / simulations,
        "probability_terminal_loss_upper_confidence": loss_upper,
        "probability_reaching_annual_target": target_count / simulations,
        "probability_reaching_annual_target_lower_confidence": target_lower,
        "terminal_return_mean": mean(terminal_returns),
        "terminal_return_median": median(terminal_returns),
        "terminal_return_p05": _quantile(terminal_returns, 0.05),
        "maximum_drawdown_median": median(maximum_drawdowns),
        "maximum_drawdown_p95": _quantile(maximum_drawdowns, 0.95),
    }


def _path_statistics(returns: list[float]) -> tuple[float, float]:
    equity = peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak)
    return equity - 1.0, maximum_drawdown


def _deterministic_tail_stress(
    returns: list[float], policy: PortfolioRiskPolicy
) -> dict[str, Any]:
    specifications = (
        ("worst_day", 1, policy.deterministic_daily_repeats),
        ("worst_7d", 7, policy.deterministic_weekly_repeats),
        ("worst_28d", 28, policy.deterministic_four_week_repeats),
    )
    scenarios: list[dict[str, Any]] = []
    for label, window_days, repeats in specifications:
        candidates: list[tuple[float, float, float, int, list[float]]] = []
        for start in range(len(returns) - window_days + 1):
            source_block = returns[start : start + window_days]
            source_return, _ = _path_statistics(source_block)
            terminal_return, maximum_drawdown = _path_statistics(
                source_block * repeats
            )
            candidates.append(
                (
                    source_return,
                    terminal_return,
                    maximum_drawdown,
                    start,
                    source_block,
                )
            )
        source_return, terminal_return, maximum_drawdown, source_start, source_block = max(
            candidates, key=lambda item: (item[2], -item[1], -item[3])
        )
        scenarios.append(
            {
                "name": f"{label}_x{repeats}",
                "source_window_days": window_days,
                "repeats": repeats,
                "source_start_return_index": source_start,
                "source_block_return": source_return,
                "stressed_days": window_days * repeats,
                "terminal_return": terminal_return,
                "maximum_drawdown": maximum_drawdown,
            }
        )
    return {
        "scenarios": scenarios,
        "maximum_drawdown": max(item["maximum_drawdown"] for item in scenarios),
        "worst_terminal_return": min(item["terminal_return"] for item in scenarios),
    }


def evaluate_portfolio_risk(
    equity: dict[str, Any], policy: PortfolioRiskPolicy | None = None
) -> dict[str, Any]:
    policy = policy or PortfolioRiskPolicy()
    returns = [float(value) for value in equity.get("daily_returns") or []]
    if any(not isfinite(value) or value <= -1.0 for value in returns):
        raise ValueError("portfolio daily returns must be finite and greater than -1")
    sufficient = len(returns) >= max(policy.minimum_daily_returns, policy.long_block_days * 4)
    scenarios: list[dict[str, Any]] = []
    deterministic_stress: dict[str, Any] = {
        "scenarios": [],
        "maximum_drawdown": None,
        "worst_terminal_return": None,
    }
    if sufficient:
        scenarios = [
            _block_scenario(
                returns,
                block_days=block_days,
                policy=policy,
                seed=policy.seed + offset,
            )
            for offset, block_days in enumerate(
                (policy.short_block_days, policy.long_block_days)
            )
        ]
        deterministic_stress = _deterministic_tail_stress(returns, policy)
    historical_drawdown = equity.get("historical_maximum_drawdown")
    aggregate = {
        "probability_of_ruin": max(
            (item["probability_of_ruin"] for item in scenarios), default=None
        ),
        "probability_of_ruin_upper_confidence": max(
            (item["probability_of_ruin_upper_confidence"] for item in scenarios),
            default=None,
        ),
        "probability_drawdown_exceeds_threshold": max(
            (item["probability_drawdown_exceeds_threshold"] for item in scenarios),
            default=None,
        ),
        "probability_terminal_loss": max(
            (item["probability_terminal_loss"] for item in scenarios), default=None
        ),
        "probability_terminal_loss_upper_confidence": max(
            (
                item["probability_terminal_loss_upper_confidence"]
                for item in scenarios
            ),
            default=None,
        ),
        "probability_reaching_annual_target": min(
            (item["probability_reaching_annual_target"] for item in scenarios),
            default=None,
        ),
        "probability_reaching_annual_target_lower_confidence": min(
            (
                item["probability_reaching_annual_target_lower_confidence"]
                for item in scenarios
            ),
            default=None,
        ),
        "terminal_return_p05": min(
            (item["terminal_return_p05"] for item in scenarios), default=None
        ),
        "maximum_drawdown_p95": max(
            (item["maximum_drawdown_p95"] for item in scenarios), default=None
        ),
    }
    checks = [
        {
            "name": "minimum_daily_returns",
            "passed": len(returns) >= policy.minimum_daily_returns,
            "actual": len(returns),
            "required": policy.minimum_daily_returns,
        },
        {
            "name": "historical_maximum_drawdown",
            "passed": isinstance(historical_drawdown, (int, float))
            and not isinstance(historical_drawdown, bool)
            and isfinite(float(historical_drawdown))
            and float(historical_drawdown) <= policy.maximum_historical_drawdown,
            "actual": historical_drawdown,
            "required": policy.maximum_historical_drawdown,
        },
        {
            "name": "probability_of_ruin",
            "passed": aggregate["probability_of_ruin_upper_confidence"] is not None
            and aggregate["probability_of_ruin_upper_confidence"]
            <= policy.maximum_probability_of_ruin,
            "actual": aggregate["probability_of_ruin_upper_confidence"],
            "required": policy.maximum_probability_of_ruin,
        },
        {
            "name": "probability_terminal_loss",
            "passed": aggregate["probability_terminal_loss_upper_confidence"] is not None
            and aggregate["probability_terminal_loss_upper_confidence"]
            <= policy.maximum_probability_terminal_loss,
            "actual": aggregate["probability_terminal_loss_upper_confidence"],
            "required": policy.maximum_probability_terminal_loss,
        },
        {
            "name": "probability_reaching_annual_target",
            "passed": aggregate["probability_reaching_annual_target_lower_confidence"]
            is not None
            and aggregate["probability_reaching_annual_target_lower_confidence"]
            >= policy.minimum_probability_reaching_annual_target,
            "actual": aggregate["probability_reaching_annual_target_lower_confidence"],
            "required": policy.minimum_probability_reaching_annual_target,
        },
        {
            "name": "bootstrap_maximum_drawdown_p95",
            "passed": aggregate["maximum_drawdown_p95"] is not None
            and aggregate["maximum_drawdown_p95"]
            <= policy.maximum_bootstrap_drawdown_p95,
            "actual": aggregate["maximum_drawdown_p95"],
            "required": policy.maximum_bootstrap_drawdown_p95,
        },
        {
            "name": "deterministic_tail_stress_drawdown",
            "passed": deterministic_stress["maximum_drawdown"] is not None
            and deterministic_stress["maximum_drawdown"]
            <= policy.maximum_deterministic_stress_drawdown,
            "actual": deterministic_stress["maximum_drawdown"],
            "required": policy.maximum_deterministic_stress_drawdown,
        },
    ]
    return {
        "schema_version": 1,
        "status": "PASS" if all(item["passed"] for item in checks) else "FAIL",
        "policy": asdict(policy),
        "equity": {
            key: equity.get(key)
            for key in (
                "first_timestamp",
                "last_timestamp",
                "snapshots",
                "daily_returns_sha256",
                "historical_maximum_drawdown",
            )
        },
        "observations": len(returns),
        "scenarios": scenarios,
        "aggregate": aggregate,
        "deterministic_stress": deterministic_stress,
        "checks": checks,
        "warning": "Block bootstrap preserves sampled weekly/four-week clustering, probability gates use one-sided Wilson confidence bounds, and deterministic tail repeats prevent rare observed loss clusters from being missed by Monte Carlo sampling. These remain scenario tests, not forecasts or guarantees.",
    }
