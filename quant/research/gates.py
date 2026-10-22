"""Deterministic promotion gates; they never change a trading configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GatePolicy:
    min_oos_trades: int = 30
    min_profit_factor: float = 1.20
    max_drawdown: float = 0.15
    min_oos_net_return: float = 0.0
    max_short_single_trade_profit_concentration: float = 0.50


def evaluate_candidate(
    *,
    development: dict,
    validation: dict,
    cost_stress: dict,
    lookahead_passed: bool,
    recursive_passed: bool,
    role: str = "general",
    slippage_stress: dict | None = None,
    policy: GatePolicy = GatePolicy(),
) -> dict:
    """Return PASS/REVIEW/FAIL from pre-committed, explainable checks."""
    checks: list[dict] = []

    def check(name: str, passed: bool, actual, required, severity: str = "fail") -> None:
        checks.append({"name": name, "passed": passed, "actual": actual, "required": required, "severity": severity})

    validation_organic = validation["organic"]
    stress_organic = cost_stress["organic"]
    check("development_net_positive", development["organic"]["net_return"] > 0, development["organic"]["net_return"], "> 0")
    check("oos_net_positive", validation_organic["net_return"] > policy.min_oos_net_return, validation_organic["net_return"], f"> {policy.min_oos_net_return}")
    check("oos_profit_factor", (validation_organic["profit_factor"] or 0.0) >= policy.min_profit_factor, validation_organic["profit_factor"], f">= {policy.min_profit_factor}")
    check("oos_max_drawdown", validation["max_drawdown"] <= policy.max_drawdown, validation["max_drawdown"], f"<= {policy.max_drawdown}")
    check("cost_stress_net_positive", stress_organic["net_return"] > 0, stress_organic["net_return"], "> 0")
    if slippage_stress is not None:
        check("slippage_stress_net_positive", slippage_stress["organic"]["net_return"] > 0, slippage_stress["organic"]["net_return"], "> 0")
    check("lookahead", lookahead_passed, lookahead_passed, True)
    check("recursive", recursive_passed, recursive_passed, True)
    check("oos_sample_size", validation_organic["trades"] >= policy.min_oos_trades, validation_organic["trades"], f">= {policy.min_oos_trades}", severity="review")

    if role == "short":
        short_metrics = validation.get("short_metrics") or {}
        check(
            "bear_expectancy_positive",
            (short_metrics.get("expectancy") or 0.0) > 0,
            short_metrics.get("expectancy"),
            "> 0",
        )
        concentration = short_metrics.get("largest_winner_profit_concentration")
        check(
            "single_trade_profit_concentration",
            concentration is not None and concentration <= policy.max_short_single_trade_profit_concentration,
            concentration,
            f"<= {policy.max_short_single_trade_profit_concentration}",
        )
    if role == "breakout":
        breakout_metrics = validation.get("breakout_metrics") or {}
        check("breakout_expectancy_positive", (breakout_metrics.get("expectancy") or 0.0) > 0, breakout_metrics.get("expectancy"), "> 0")
        concentration = breakout_metrics.get("largest_winner_profit_concentration")
        check(
            "single_trade_profit_concentration",
            concentration is not None and concentration <= policy.max_short_single_trade_profit_concentration,
            concentration,
            f"<= {policy.max_short_single_trade_profit_concentration}",
        )

    failed = [item for item in checks if not item["passed"] and item["severity"] == "fail"]
    review = [item for item in checks if not item["passed"] and item["severity"] == "review"]
    status = "FAIL" if failed else "REVIEW" if review else "PASS"
    return {"status": status, "role": role, "policy": asdict(policy), "checks": checks}
