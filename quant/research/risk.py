"""Cluster-aware preliminary risk-of-ruin simulation for research gates."""

from __future__ import annotations

import random
from statistics import mean, median


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    location = (len(ordered) - 1) * probability
    lower = int(location)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def block_bootstrap_risk(
    daily_returns: list[float],
    *,
    horizon_days: int = 365,
    block_days: int = 7,
    simulations: int = 5_000,
    ruin_equity_fraction: float = 0.50,
    drawdown_threshold: float = 0.20,
    annual_target_return: float = 1.10**12 - 1,
    seed: int = 17,
) -> dict:
    """Resample contiguous daily blocks to retain short loss clustering.

    This is a non-parametric scenario generator, not a forecast. Results from
    closed-trade daily PnL remain preliminary because intraday unrealized loss
    and liquidation paths are absent.
    """
    returns = [float(value) for value in daily_returns]
    if len(returns) < max(block_days * 4, 28):
        return {
            "status": "INSUFFICIENT_SAMPLE",
            "observations": len(returns),
            "required_minimum": max(block_days * 4, 28),
        }
    if horizon_days < 1 or block_days < 1 or simulations < 100:
        raise ValueError("horizon_days/block_days must be positive and simulations >= 100")
    if not 0 < ruin_equity_fraction < 1 or not 0 < drawdown_threshold < 1:
        raise ValueError("ruin and drawdown thresholds must be fractions between zero and one")

    block_starts = range(0, len(returns) - block_days + 1)
    generator = random.Random(seed)
    terminal_returns: list[float] = []
    maximum_drawdowns: list[float] = []
    ruin_count = drawdown_breach_count = target_count = loss_count = 0
    for _ in range(simulations):
        scenario: list[float] = []
        while len(scenario) < horizon_days:
            start = generator.choice(block_starts)
            scenario.extend(returns[start : start + block_days])
        equity = peak = 1.0
        maximum_drawdown = 0.0
        ruined = False
        for daily_return in scenario[:horizon_days]:
            equity *= max(0.0, 1.0 + daily_return)
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, 1.0 - equity / peak if peak else 1.0)
            ruined = ruined or equity <= ruin_equity_fraction
        terminal_return = equity - 1.0
        terminal_returns.append(terminal_return)
        maximum_drawdowns.append(maximum_drawdown)
        ruin_count += ruined
        drawdown_breach_count += maximum_drawdown > drawdown_threshold
        target_count += terminal_return >= annual_target_return
        loss_count += terminal_return < 0
    return {
        "status": "AVAILABLE_PRELIMINARY",
        "basis": "7-day contiguous block bootstrap of supplied daily returns; deterministic seed",
        "observations": len(returns),
        "horizon_days": horizon_days,
        "block_days": block_days,
        "simulations": simulations,
        "ruin_equity_fraction": ruin_equity_fraction,
        "drawdown_threshold": drawdown_threshold,
        "annual_target_return": annual_target_return,
        "probability_of_ruin": ruin_count / simulations,
        "probability_drawdown_exceeds_threshold": drawdown_breach_count / simulations,
        "probability_terminal_loss": loss_count / simulations,
        "probability_reaching_annual_target": target_count / simulations,
        "terminal_return_median": median(terminal_returns),
        "terminal_return_mean": mean(terminal_returns),
        "terminal_return_p05": _quantile(terminal_returns, 0.05),
        "terminal_return_p95": _quantile(terminal_returns, 0.95),
        "maximum_drawdown_median": median(maximum_drawdowns),
        "maximum_drawdown_p95": _quantile(maximum_drawdowns, 0.95),
    }
