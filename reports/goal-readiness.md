# Capital-preservation research readiness

- Status: `RESEARCH_ATTENTION_REQUIRED`
- Objective proven: `false`
- Live order authorization: `CLOSED`
- Research health: `ATTENTION_REQUIRED`
- Return objective: `positive_long_horizon_cost_after_expectancy` (monthly return: `NOT_A_GATE`)
- Research cadence: historical diagnostics immediately; forward reviews at 30, 60, 90 days.

## Proof pipeline

| Gate | Status | Actual | Required |
|---|---:|---:|---:|
| forward_research_operations | ATTENTION | ATTENTION_REQUIRED | HEALTHY |
| independent_locked_forward_sleeves | NOT_OPENED | 0 | 2 |
| combined_cost_after_expectancy | NOT_OPENED | NOT_OPENED | positive cost-after return; trade and daily MTM PF >= 1.20; >= 12 complete locked-forward months; >= 100 organic trades; force_exit only at the exact sample end. |
| combined_downside_and_diversification | NOT_OPENED | NOT_OPENED | MDD <= 10%, intratrade leverage-adjusted MAE <= 20%, trade-overlap exposure reconciliation, bootstrap ruin <= 0.5%, deterministic worst-block recurrence MDD <= 10%, zero liquidations, 1x leverage/correlation/concentration limits |
| combined_cost_and_execution_paths | NOT_OPENED | NOT_OPENED | positive simultaneous fee+slippage+funding stress, per-trade accounting costs covering preregistered scope budgets, and every entry/exit/stop scope PASS |
| selection_bias_control | NOT_OPENED | NOT_OPENED | complete trial inventories; DSR probability >= 95%; CSCV PBO <= 10% |
| multi_strategy_live_adapter | DEFERRED_NOT_BUILT | - | build only after a preregistered portfolio passes all research gates |
| live_order_authorization | CLOSED | - | separate live preflight after portfolio promotion PASS |

## Active forward candidates

- Experiment 7: `ALPHA_007_IMBALANCE_GUIDED_MAKER` — `ACCUMULATING`
- Experiment 11: `ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION` — `ACCUMULATING`

## Ordered blockers

- No eligible portfolio yet: 0 research PASS candidates; at least 2 independently locked-forward PASS sleeves are required.
- Alpha 007's frozen fixed-notional order path is not exchange-filter feasible; it remains research-only and cannot be repaired by silently changing its order size.
- No preregistered portfolio candidate or combined locked-forward promotion report exists.
- The multi-strategy live adapter and portfolio-specific live preflight are intentionally not built before a portfolio PASS.
- Live order authorization remains closed; calibration and dry-run evidence cannot authorize orders.

Monthly return is not a promotion requirement. Positive long-horizon cost-after expectancy and the capital-preservation gates remain mandatory. This readiness report never promotes calibration, backtests, or partial forward samples.
