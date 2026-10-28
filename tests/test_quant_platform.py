from __future__ import annotations

import fcntl
import csv
import json
import gzip
import os
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from zipfile import ZipFile

import numpy as np
import pandas as pd

from quant.data.validator import validate_ohlcv_rows
from quant.data.universe import InstrumentSnapshot, UniversePolicy, build_universe_history, select_universe
from quant.data.universe_history import assess_forward_universe_history, assess_universe_history
from quant.data.historical_symbol_catalog import (
    HistoricalSymbolCatalogError,
    build_historical_symbol_catalog,
    parse_s3_symbol_page,
)
from quant.data.historical_universe import (
    HistoricalDailyBar,
    HistoricalUniverseError,
    build_historical_daily_universe,
    load_historical_daily_price_panel,
    monthly_1d_archive_spec,
    parse_publisher_checksum as parse_historical_universe_checksum,
    validate_monthly_1d_archive,
    verify_historical_daily_universe,
)
from scripts.run_historical_universe_scheduler import scheduler_health as historical_scheduler_health
from scripts.audit_historical_universe import audit as audit_historical_universe
from scripts.collect_historical_monthly_funding import _month_starts as historical_funding_month_starts
from scripts.run_historical_funding_scheduler import health as historical_funding_scheduler_health
from scripts.alpha013_status import signal_panel_health
from scripts.build_alpha013_signal_panel import PANEL_PROTOCOL_FILES
from scripts.historical_research_status import report_is_bound_to_universe
from quant.data.snapshots import (
    load_instrument_snapshots,
    seal_snapshot_batch,
    write_snapshot_batch,
)
from quant.data.orderbook import LocalOrderBook, OrderBookSequenceError
from quant.execution.cost_model import CostInputs, estimate_costs
from quant.execution.maker_quality import summarize_maker_execution
from quant.execution.shadow_maker import estimate_shadow_fill
from quant.execution.alpha007_order_path import (
    Alpha007OrderPath,
    OrderPathError,
    SymbolFilters,
)
from quant.execution.exchange_filter_gate import evaluate_fixed_notional_filter_gate
from quant.regime.classifier import Regime, RegimeClassifier, RegimeConfig
from quant.research.evidence import EdgeBudget, build_scoreboard
from quant.research.gates import GatePolicy, evaluate_candidate
from quant.research.partial_fill_stress import evaluate_partial_fill_stress
from quant.research.registry import ExperimentRegistry
from quant.research.result_loader import _monthly_distribution, load_freqtrade_result
from quant.research.artifacts import artifact_reference
from quant.research.monthly_target import MonthlyTargetPolicy, evaluate_monthly_target
from quant.research.capital_preservation import evaluate_capital_preservation
from quant.research.selection_bias import (
    SelectionBiasPolicy,
    deflated_sharpe_ratio,
    evaluate_selection_bias,
    periodic_sharpe,
    probability_backtest_overfitting,
    validate_trial_inventory,
)
from quant.research.signal_diagnostics import (
    SIGNAL_DIAGNOSTIC_HEADER,
    SignalObservation,
    load_signal_observations,
    summarize_signal_diagnostics,
)
from quant.research.funding_dispersion_signal_panel import (
    assert_alpha013_signal_contract,
    build_funding_dispersion_signal_panel,
)
from quant.research.funding_decision_availability import (
    audit_decision_availability,
    receipt_filename,
)
from quant.research.portfolio_promotion import (
    PORTFOLIO_MONTHLY_POLICY,
    PROMOTION_PROTOCOL_FILES,
    REQUIRED_MONTHLY_CHECK_NAMES,
    evaluate_portfolio_promotion,
    expected_execution_scopes,
)
from quant.research.portfolio_activation import evaluate_portfolio_activation
from quant.research.portfolio_composition import (
    evaluate_portfolio_composition,
    load_portfolio_contributions,
    load_sleeve_daily_pnl,
)
from quant.research.portfolio_performance import (
    EXPOSURE_HEADER,
    TRADE_HEADER,
    _stressed_equity_path,
    evaluate_portfolio_performance,
    load_portfolio_accounting,
    load_portfolio_exposure,
    load_portfolio_trades,
)
from quant.research.portfolio_execution import (
    MARKET_DATA_PROTOCOL_FILES,
    PORTFOLIO_EXECUTION_PROTOCOL_FILES,
    evaluate_portfolio_execution,
    load_execution_observation_manifest,
)
from quant.research.portfolio_risk import (
    PortfolioRiskPolicy,
    evaluate_portfolio_risk,
    load_portfolio_equity,
    wilson_bounds,
)
from quant.research.goal_readiness import (
    audit_portfolio_promotion_receipt,
    build_goal_readiness,
)
from quant.research.risk_policy_contract import validate_risk_policy_contract
from quant.research.historical_admission import (
    HistoricalAdmissionError,
    evaluate_historical_admission,
)
from quant.research.historical_release_train import (
    HistoricalReleaseTrainError,
    evaluate_historical_release_train,
)
from quant.research.historical_cost_guard import (
    HistoricalCostPolicyError,
    evaluate_historical_cost_policy,
)
from quant.research.historical_daily_execution import (
    HistoricalDailyExecutionError,
    attach_verified_funding,
    simulate_daily_leg,
)
from quant.research.historical_execution_coverage import (
    audit_historical_next_open_coverage,
)
from quant.research.historical_funding_ledger import (
    HistoricalFundingLedgerError,
    funding_return_for_interval,
    verify_historical_funding_manifest,
)
from quant.research.historical_funding_coverage import audit_historical_funding_coverage
from quant.research.candidate_activation import (
    evaluate_alpha015_activation,
    verify_alpha015_contract_semantics,
    verify_methodology_artifacts,
)
from quant.monitoring.alpha015_milestones import build_alpha015_collection_milestones
from quant.monitoring.alpha015_universe_diagnostic import summarize_eligible_membership
from quant.monitoring.shadow_campaign_progress import summarize_shadow_campaign_progress
from quant.monitoring.execution_calibration_stability import (
    summarize_versioned_calibration_receipts,
)
from quant.research.alpha015_data_guard import (
    Alpha015DataError,
    archive_spec as alpha015_archive_spec,
    binance_archive_symbol,
    assert_manifest_phase_access,
    authorized_days as alpha015_authorized_days,
    authorized_archive_days as alpha015_authorized_archive_days,
    build_locked_calendar,
    download_and_seal_archive,
    download_and_seal_funding,
    iter_reverified_archive_rows,
    load_reverified_funding_events,
    monthly_funding_archive_spec,
    reconcile_funding_events,
    required_phase_archive_specs,
    seal_downloaded_archive_file,
    validate_aggtrade_day_chain,
    validate_funding_payload as alpha015_validate_funding_payload,
    validate_monthly_funding_archive,
    validate_phase_archive_inventory,
)
from quant.research.alpha015_activation_lock import (
    Alpha015ActivationLockError,
    build_alpha015_activation_lock,
)
from quant.research.alpha015_acquisition import (
    Alpha015AcquisitionError,
    assess_storage_budget as alpha015_storage_budget,
    build_phase_acquisition_plan,
    build_development_acquisition_plan,
    build_progress_receipt as alpha015_build_progress_receipt,
    build_size_inventory_receipt as alpha015_build_size_inventory_receipt,
    verify_progress_receipt as alpha015_verify_progress_receipt,
    verify_size_inventory_receipt as alpha015_verify_size_inventory_receipt,
)
from quant.research.alpha015_preregistration import (
    build_alpha015_preregistration,
    canonical_sha256 as alpha015_canonical_sha256,
    verify_alpha015_preregistration,
)
from quant.research.alpha015_simulation import (
    first_trade_price_at_or_after,
    returns_from_closed_klines,
    simulate_daily_path,
)
from quant.research.alpha015_evaluation import (
    ALPHA015_DEVELOPMENT_SELECTION_POLICY,
    alpha015_newey_west_mean_t_statistic,
    evaluate_alpha015_development,
    evaluate_alpha015_development_selection_bias,
    evaluate_alpha015_locked_validation,
)
from quant.research.alpha015_replay import (
    Alpha015ReplayError,
    aggregate_trade_boundary_prices,
    forecast_future_row_invariance,
)
from scripts.open_alpha015_validation import (
    build_validation_unlock as build_alpha015_validation_unlock,
    validate_validation_unlock as validate_alpha015_validation_unlock,
)
from scripts.calibration_execution_report import default_output_prefix
from scripts.reconcile_alpha015_funding import (
    build_reconciliation_receipt as build_alpha015_funding_reconciliation_receipt,
    verify_reconciliation_receipt as verify_alpha015_funding_reconciliation_receipt,
)
from quant.research.cross_crypto_predictor import (
    AdaptiveLassoKernelConfig,
    causal_training_matrices,
    target_schedule_turnover_floor,
    decile_target_weights,
    quintile_target_weights,
    execution_cost_fraction as cross_crypto_execution_cost,
    execution_window as cross_crypto_execution_window,
    fit_adaptive_lasso,
    forecast_cross_section,
    funding_pnl_fraction,
    select_penalty_by_forward_mse,
)
from quant.research.equity import load_mark_to_market_equity
from quant.research.risk import block_bootstrap_risk
from quant.research.aoa_history import reconstruct_xbtusd_episodes, summarize_episodes
from quant.research.aoa_behavior import attach_episode_labels, build_causal_features
from quant.research.aoa_forward import audit_aoa_forward
from quant.research.microstructure import (
    _future_feature,
    _load_run,
    _run_signals,
    _stream_signals,
    _verify_run,
    analyze_microstructure,
)
from scripts.analyze_microstructure import audit_complete_day_protocols
from quant.monitoring.equity import select_causal_daily_boundaries
from quant.monitoring.sleep_guard import inspect_caffeinate_guard
from quant.research.funding_carry import CarryConfig, _simulate_leg
from quant.research.funding_dispersion_reversal import (
    FundingDispersionConfig,
    simulate_forward as simulate_funding_dispersion_forward,
)
from quant.research.perp_basis_catchup import (
    PerpBasisCatchupConfig,
    basis_score as perpetual_basis_score,
    simulate_development as simulate_perp_basis_development,
)
from quant.research.spot_perp_carry import (
    _monthly_metrics as spot_perp_monthly_metrics,
    same_asset_basis_pnl,
    short_funding_pnl,
)
from quant.research.leverage_crowding import build_decision_frame, simulate_trades, summarize_trades
from quant.research.liquidation_telemetry import liquidation_events, mark_prices
from quant.research.liquidation_history import assess_liquidation_history, merged_seconds
from quant.research.liquidation_overlay import (
    collapse_episodes as collapse_liquidation_episodes,
    evaluate_overlay as evaluate_liquidation_overlay,
    qualifying_triggers as liquidation_qualifying_triggers,
)
from quant.research.external_cost_stress import evaluate_alpha007_external_cost_stress
from quant.research.quarter_hour_flow import (
    block_bootstrap_loss_probability as quarter_hour_bootstrap_loss_probability,
    simulate_trade_stream as simulate_quarter_hour_trade_stream,
    summarize_trades as summarize_quarter_hour_trades,
    reprice_trades as reprice_quarter_hour_trades,
)
from quant.research.spot_perp_flow_divergence import (
    SpotPerpFlowConfig,
    daily_block_bootstrap_excess_return,
    matched_random_baseline,
    reprice_trades as reprice_spot_perp_flow_trades,
    simulate_flow_divergence,
    summarize_trades as summarize_spot_perp_flow_trades,
    validate_trade_stream_coverage,
)
from quant.research.spot_perp_flow_protocol import (
    SpotPerpFlowProtocolError,
    build_phase_plan as build_spot_perp_flow_phase_plan,
    evaluate_phase_gate as evaluate_spot_perp_flow_gate,
    phase_calendar as spot_perp_flow_phase_calendar,
)
from quant.research.spot_perp_flow_data import (
    SpotPerpFlowDataError,
    inspect_archive as inspect_spot_perp_flow_archive,
    verify_adjacent_day_chains as verify_spot_perp_flow_day_chains,
)
from quant.research.spot_perp_flow_evaluation import (
    SpotReference,
    SpotPerpFlowEvaluationError,
    extract_market_observations as extract_spot_perp_flow_market_observations,
    matched_controls_from_references as matched_spot_perp_flow_controls,
    simulate_extracted_flow as simulate_extracted_spot_perp_flow,
)
from quant.research.campaign import microstructure_campaign_preflight, microstructure_run_audit
from quant.research.protocol import protocol_digests
from quant.research.forward_dryrun import audit_mtm_segments, build_forward_report, load_forward_trades, summarize_mtm
from scripts.alpha001_forward_report import (
    audit_database_binding,
    audit_net_liquidation_rows,
    enforce_data_integrity,
)
from scripts.portfolio_promotion_report import (
    _load_portfolio_risk_audit,
    _load_selection_bias_audit,
)
from scripts.goal_readiness_report import compare_portfolio_promotion_replay
from scripts.manage_microstructure_campaign import campaign_coverage_budget, derive_status_health
from scripts.manage_liquidation_telemetry_campaign import (
    status_payload as liquidation_campaign_status_payload,
)
from scripts.run_microstructure_campaign import chunk_wall_timeout_seconds, run_chunk_with_watchdog
from scripts.sync_alpha007_registry import (
    archive_report,
    build_registry_result as build_alpha007_registry_result,
    compact_accumulation,
    event_has_compact_archive,
    receipt_health as alpha007_receipt_health,
    registry_status as alpha007_registry_status,
    write_once_json as write_alpha007_receipt_once,
)
from scripts.manage_alpha001_forward_dryrun import (
    forward_operational_health,
    inspect_bot_log,
    inspect_equity_manifest,
    load_forward_cohort,
    protocol_database_path,
    select_database_path,
)
from scripts.record_mtm_equity import calculate_net_liquidation_equity, collect_equity
from scripts.record_alpha001_entry_execution import parse_order_attempt
from scripts.shadow_maker_probe import get_json as shadow_get_json
from scripts.shadow_maker_probe import immediate_limit_fill, queue_ahead_at_limit
from scripts.research_health import (
    assess_alpha001_entry_execution_continuity,
    assess_mtm_continuity,
    alpha007_complete_day_protocol_health,
    alpha007_nonterminal_performance_warning,
    classify_research_health,
    resolve_alpha007_terminal_health,
    resolve_alpha013_health,
    capital_preservation_policy_health,
)
from scripts.alpha011_status import scheduler_health as alpha011_scheduler_health
from scripts.alpha011_status import status_payload as alpha011_status_payload
from scripts.calibration_execution_report import validate_calibration_rows
from scripts.build_platform_scoreboard import auxiliary_gate_snapshot
from scripts.scheduled_shadow_calibration import (
    independent_window_not_before,
    seconds_until,
    terminal_campaign_failure,
)
from scripts.open_liquidation_overlay_validation import build_validation_lock
from scripts.update_alpha011_data import publisher_has_day as alpha011_publisher_has_day
from scripts.update_alpha011_data import publisher_ready_prefix as alpha011_ready_prefix
from scripts.update_alpha011_data import finish_ready_development as finish_alpha011_development
from scripts.update_alpha011_data import completed_eligible_months
from scripts.analyze_alpha011_validation import (
    locked_validation_gate as alpha011_locked_validation_gate,
    validate_lock_chain as validate_alpha011_lock_chain,
    validation_boundaries as alpha011_validation_boundaries,
)
from scripts.open_alpha011_validation import (
    build_validation_lock as build_alpha011_validation_lock,
)
from scripts.sync_alpha011_validation import (
    archive_report as archive_alpha011_validation_report,
    build_registry_result as build_alpha011_validation_registry_result,
)
from scripts.alpha013_status import scheduler_health as alpha013_scheduler_health
from scripts.alpha013_status import status_payload as alpha013_status_payload
from scripts.update_alpha013_data import publisher_has_day as alpha013_publisher_has_day
from scripts.update_alpha013_data import publisher_ready_prefix as alpha013_ready_prefix
from scripts.update_alpha013_data import is_terminal_status as alpha013_is_terminal_status
from scripts.run_alpha013_scheduler import terminal_state_for_experiment as alpha013_terminal_scheduler_state
from scripts.download_alpha013_day import (
    Alpha013DownloadError,
    EXPECTED_COLUMNS as ALPHA013_COLUMNS,
    manifest_inventory_digest as alpha013_manifest_inventory_digest,
    parse_checksum as parse_alpha013_checksum,
    protocol_digests as alpha013_protocol_digests,
    validate_hourly_archive as validate_alpha013_hourly_archive,
)
from scripts.reconcile_alpha013_funding import compare_rows as compare_alpha013_funding_rows
from scripts.sync_alpha013_registry import (
    archive_report as archive_alpha013_report,
    build_registry_result as build_alpha013_registry_result,
    latest_snapshot_key as alpha013_latest_snapshot_key,
    registry_status as alpha013_registry_status,
    receipt_health as alpha013_development_receipt_health,
    funding_reconciliation_only_pending as alpha013_development_funding_pending,
)
from scripts.sync_alpha013_causal_veto import (
    build_registry_result as build_alpha013_causal_veto_registry_result,
    validate_audit as validate_alpha013_causal_veto_audit,
)
from quant.execution.bounded_notional import evaluate_bounded_executable_notional
from scripts.analyze_alpha013_validation import (
    funding_reconciliation_only_pending as alpha013_validation_funding_pending,
    locked_validation_gate as alpha013_locked_validation_gate,
)
from scripts.open_alpha013_validation import (
    build_validation_lock as build_alpha013_validation_lock,
    next_monday_0400,
)
from scripts.sync_alpha013_validation import (
    build_registry_result as build_alpha013_validation_registry_result,
)
from scripts.sync_alpha011_registry import (
    archive_report as archive_alpha011_report,
    build_registry_result as build_alpha011_registry_result,
    receipt_health as alpha011_receipt_health,
    registry_status as alpha011_registry_status,
)
from scripts.collect_universe_snapshot import (
    _remote_candle_status,
    validate_forward_collection_start,
)
from scripts.live_preflight import (
    BIAS_AUDIT_POLICY,
    EXECUTION_AUDIT_POLICY,
    EXECUTION_AUDIT_SCHEMA_VERSION,
    bias_audit_receipt_failures,
    compose_api_binding_failures,
    execution_audit_receipt_failures,
    research_promotion_failures,
)
from scripts.audit_futures_order_feasibility import receipt_failures as order_feasibility_receipt_failures
from scripts.audit_binance_api_permissions import (
    api_key_fingerprint,
    build_receipt,
    permission_audit_receipt_failures,
    permission_failures,
)
from scripts.audit_binance_futures_account import (
    account_audit_receipt_failures,
    account_state_failures,
    build_receipt as build_account_receipt,
    config_symbols,
    normalize_account_state,
)
from scripts.download_binance_futures_metrics import parse_checksum, verify_archive
from scripts.analyze_leverage_crowding import normalize_metrics_day
from scripts.download_binance_aggtrades import funding_query_url
from scripts.download_binance_aggtrades import inspect_archive as inspect_aggtrades_archive
from scripts.download_binance_aggtrades import validate_funding_payload
from scripts.analyze_quarter_hour_flow import load_funding as load_alpha011_funding


def passing_forward_evidence() -> dict:
    return {
        "sample_role": "locked_forward",
        "pre_registered": True,
        "parameter_changes_after_start": 0,
        "registered_at_utc": "2025-01-01T00:00:00Z",
        "sample_started_at_utc": "2025-01-02T00:00:00Z",
    }


def passing_bootstrap_risk(*, maximum_drawdown_p95: float = 0.15) -> dict:
    return {
        "probability_of_ruin": 0.0,
        "probability_terminal_loss": 0.05,
        "probability_reaching_annual_target": 0.60,
        "maximum_drawdown_p95": maximum_drawdown_p95,
    }
from scripts.analyze_quarter_hour_flow import completed_months_in_files
from scripts.reconcile_alpha011_funding import compare_funding_rows, inspect_monthly_archive


def sample_metrics(*, net_return: float = 0.02, trades: int = 25, pf: float = 1.5) -> dict:
    return {
        "organic": {"net_return": net_return, "trades": trades, "profit_factor": pf},
        "max_drawdown": 0.05,
    }


class CostModelTests(unittest.TestCase):
    def test_component_costs_are_explicit(self) -> None:
        estimate = estimate_costs(
            CostInputs(100, 110, 0.0005, 0.0005, entry_slippage_bps=2, exit_slippage_bps=3, spread_bps=4, funding_paid=1)
        )
        self.assertAlmostEqual(estimate.entry_fee, 0.05)
        self.assertAlmostEqual(estimate.exit_fee, 0.055)
        self.assertAlmostEqual(estimate.slippage, 0.053)
        self.assertAlmostEqual(estimate.spread, 0.042)
        self.assertAlmostEqual(estimate.total_cost, 1.2)

    def test_maker_quality_exposes_nonfills_and_adverse_selection(self) -> None:
        rows = [
            {
                "signal_id": "buy-fill",
                "side": "buy",
                "submitted_qty": 10,
                "reference_mid": 100,
                "filled_qty": 10,
                "average_fill_price": 99.9,
                "maker_filled_qty": 10,
                "taker_filled_qty": 0,
                "fee": 0.1998,
                "mid_after_5m": 99.7,
                "mid_after_timeout": "",
            },
            {
                "signal_id": "sell-missed",
                "side": "sell",
                "submitted_qty": 10,
                "reference_mid": 100,
                "filled_qty": 0,
                "average_fill_price": 0,
                "maker_filled_qty": 0,
                "taker_filled_qty": 0,
                "fee": 0,
                "mid_after_5m": "",
                "mid_after_timeout": 99,
            },
        ]
        report = summarize_maker_execution(
            rows, min_orders=2, min_filled_orders=1, gross_edge_budget_bps=5
        )
        self.assertAlmostEqual(report["fill_rate_by_qty"], 0.5)
        self.assertAlmostEqual(report["maker_share_by_filled_qty"], 1.0)
        self.assertAlmostEqual(report["effective_fee_bps"], 2.0)
        self.assertAlmostEqual(report["signed_slippage_bps"], -10.0)
        self.assertAlmostEqual(report["unfilled_opportunity_cost_bps"], 100.0)
        self.assertGreater(report["post_fill_adverse_markout_5m_bps"], 0)
        self.assertGreater(report["conservative_all_in_execution_drag_bps"], 5)
        self.assertEqual(report["status"], "REVIEW")

        feasible = summarize_maker_execution(
            rows, min_orders=2, min_filled_orders=1, gross_edge_budget_bps=100
        )
        self.assertEqual(feasible["filled_markout_coverage_ratio"], 1.0)
        self.assertEqual(feasible["unfilled_opportunity_coverage_ratio"], 1.0)
        self.assertEqual(feasible["status"], "PASS")

    def test_alpha007_order_path_rounds_filters_and_never_crosses(self) -> None:
        filters = SymbolFilters.from_values(
            tick_size="0.1",
            step_size="0.0001",
            min_qty="0.0001",
            max_qty="100",
            min_notional="5",
        )
        engine = Alpha007OrderPath()
        command = engine.on_signal(
            signal_id="locked-buy-1",
            side="buy",
            decision_ms=1_000_000,
            observed_ms=1_000_100,
            best_bid="99999.99",
            best_ask="100000.05",
            filters=filters,
        )
        self.assertEqual(command.action, "PLACE")
        self.assertEqual(command.role, "ENTRY")
        self.assertEqual(command.order_type, "LIMIT")
        self.assertEqual(command.time_in_force, "GTX")
        self.assertFalse(command.reduce_only)
        self.assertEqual(command.position_side, "BOTH")
        self.assertLessEqual(Decimal(command.price), Decimal("99999.99"))
        self.assertEqual(Decimal(command.price) % filters.tick_size, 0)
        self.assertEqual(Decimal(command.quantity) % filters.step_size, 0)
        self.assertLessEqual(
            Decimal(command.quantity) * Decimal(command.price), Decimal("50")
        )
        self.assertLessEqual(len(command.client_order_id), 36)
        with self.assertRaisesRegex(OrderPathError, "cycle is active"):
            engine.on_signal(
                signal_id="overlap",
                side="sell",
                decision_ms=1_000_200,
                observed_ms=1_000_200,
                best_bid="99999.9",
                best_ask="100000.0",
                filters=filters,
            )
        with self.assertRaisesRegex(OrderPathError, "stale"):
            Alpha007OrderPath().on_signal(
                signal_id="stale",
                side="buy",
                decision_ms=1_000_000,
                observed_ms=1_000_251,
                best_bid="99999.9",
                best_ask="100000.0",
                filters=filters,
            )

    def test_alpha007_order_path_reconciles_partial_before_reduce_only_exit(self) -> None:
        filters = SymbolFilters.from_values(
            tick_size="0.1",
            step_size="0.0001",
            min_qty="0.0001",
            max_qty="100",
            min_notional="5",
        )
        engine = Alpha007OrderPath()
        entry = engine.on_signal(
            signal_id="partial-cycle",
            side="BUY",
            decision_ms=2_000_000,
            observed_ms=2_000_050,
            best_bid="100000",
            best_ask="100000.1",
            filters=filters,
        )
        engine.on_entry_update(
            client_order_id=entry.client_order_id,
            event_ms=2_001_000,
            cumulative_filled_qty="0.0002",
            terminal=False,
        )
        cancel = engine.on_timer(2_003_000)
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0].action, "CANCEL")
        self.assertEqual(engine.on_timer(2_060_000), [])
        engine.on_cancel_ack(
            client_order_id=entry.client_order_id,
            event_ms=2_060_001,
            cumulative_filled_qty="0.0002",
        )
        exits = engine.on_timer(2_060_001)
        self.assertEqual(len(exits), 1)
        exit_command = exits[0]
        self.assertEqual(exit_command.order_type, "MARKET")
        self.assertEqual(exit_command.side, "SELL")
        self.assertTrue(exit_command.reduce_only)
        self.assertEqual(exit_command.quantity, "0.0002")
        engine.on_exit_update(
            client_order_id=exit_command.client_order_id,
            event_ms=2_060_050,
            cumulative_filled_qty="0.0002",
            terminal=True,
        )
        self.assertEqual(engine.state, "FLAT")

    def test_alpha007_order_path_emergency_retries_residual_reduce_only_exit(self) -> None:
        filters = SymbolFilters.from_values(
            tick_size="0.1",
            step_size="0.0001",
            min_qty="0.0001",
            max_qty="100",
            min_notional="5",
        )
        engine = Alpha007OrderPath()
        entry = engine.on_signal(
            signal_id="emergency-cycle",
            side="SELL",
            decision_ms=3_000_000,
            observed_ms=3_000_010,
            best_bid="100000",
            best_ask="100000.1",
            filters=filters,
        )
        engine.on_entry_update(
            client_order_id=entry.client_order_id,
            event_ms=3_000_500,
            cumulative_filled_qty="0.0003",
            terminal=False,
        )
        emergency = engine.emergency(3_000_600)
        self.assertEqual([item.action for item in emergency], ["CANCEL"])
        engine.on_cancel_ack(
            client_order_id=entry.client_order_id,
            event_ms=3_000_700,
            cumulative_filled_qty="0.0003",
        )
        exit_command = engine.on_timer(3_000_700)[0]
        self.assertEqual(exit_command.side, "BUY")
        self.assertTrue(exit_command.reduce_only)
        retry = engine.on_exit_update(
            client_order_id=exit_command.client_order_id,
            event_ms=3_000_800,
            cumulative_filled_qty="0.0002",
            terminal=True,
        )
        self.assertEqual(len(retry), 1)
        self.assertNotEqual(retry[0].client_order_id, exit_command.client_order_id)
        self.assertEqual(retry[0].quantity, "0.0001")
        self.assertTrue(retry[0].reduce_only)
        self.assertEqual(retry[0].reason, "residual_reduce_only_retry")
        engine.on_exit_update(
            client_order_id=retry[0].client_order_id,
            event_ms=3_000_900,
            cumulative_filled_qty="0.0001",
            terminal=True,
        )
        self.assertEqual(engine.state, "FLAT")

    def test_alpha007_current_exchange_filters_can_block_fixed_fifty_usdt(self) -> None:
        symbol = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "orderTypes": ["LIMIT", "MARKET"],
            "timeInForce": ["GTC", "GTX"],
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                    "maxQty": "1000",
                },
                {
                    "filterType": "MARKET_LOT_SIZE",
                    "stepSize": "0.001",
                    "minQty": "0.001",
                    "maxQty": "120",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "50"},
            ],
        }
        blocked = evaluate_fixed_notional_filter_gate(
            symbol,
            {"symbol": "BTCUSDT", "bidPrice": "100000", "askPrice": "100000.1"},
            fixed_notional_usdt=50,
        )
        self.assertEqual(blocked["status"], "FAIL")
        self.assertEqual(blocked["sides"]["BUY"]["fixed_notional_quantity"], "0.000")
        self.assertGreater(
            Decimal(blocked["sides"]["BUY"]["minimum_executable_notional"]),
            Decimal("50"),
        )
        relaxed_symbol = json.loads(json.dumps(symbol))
        next(
            item
            for item in relaxed_symbol["filters"]
            if item["filterType"] == "MIN_NOTIONAL"
        )["notional"] = "5"
        executable = evaluate_fixed_notional_filter_gate(
            relaxed_symbol,
            {"symbol": "BTCUSDT", "bidPrice": "49999.9", "askPrice": "50000"},
            fixed_notional_usdt=50,
        )
        self.assertEqual(executable["status"], "PASS")

    def test_bounded_notional_rejects_filter_forced_risk_increase(self) -> None:
        symbol = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                {"filterType": "MIN_NOTIONAL", "notional": "50"},
            ],
        }
        ticker = {"symbol": "BTCUSDT", "bidPrice": "83000", "askPrice": "83000.1"}
        vetoed = evaluate_bounded_executable_notional(
            symbol, ticker, desired_notional_usdt="50", maximum_notional_usdt="50"
        )
        self.assertEqual(vetoed["status"], "RISK_CAP_VETO")
        self.assertFalse(vetoed["sides"]["BUY"]["admitted"])
        self.assertGreater(
            Decimal(vetoed["sides"]["BUY"]["executable_notional_usdt"]), Decimal("50")
        )
        accepted = evaluate_bounded_executable_notional(
            symbol, ticker, desired_notional_usdt="50", maximum_notional_usdt="85"
        )
        self.assertEqual(accepted["status"], "WITHIN_RISK_CAP")
        self.assertTrue(accepted["sides"]["SELL"]["admitted"])
        self.assertFalse(accepted["promotion_eligible"])

    def test_order_feasibility_receipt_requires_fresh_bounded_results(self) -> None:
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        receipt = {
            "schema_version": 1,
            "kind": "BINANCE_USDTM_BOUNDED_ORDER_FEASIBILITY",
            "generated_at_utc": now.isoformat(),
            "maximum_age_seconds_for_live_review": 900,
            "status": "PASS",
            "promotion_eligible": False,
            "live_order_authorization": "CLOSED",
            "symbols": ["BTCUSDT"],
            "desired_notional_usdt": "50",
            "maximum_notional_usdt": "50",
            "sources": {
                "exchange_info": {"url": "https://example.test/exchange", "response_sha256": "a" * 64},
                "book_ticker:BTCUSDT": {"url": "https://example.test/book", "response_sha256": "b" * 64},
            },
            "results": {"BTCUSDT": {"sizing": {"status": "WITHIN_RISK_CAP", "live_order_authorization": "CLOSED"}}},
        }
        # The verifier recomputes from the sealed snapshots; a bare PASS flag
        # is intentionally insufficient.
        receipt["results"]["BTCUSDT"]["source_snapshot"] = {
            "symbol": {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            },
            "book_ticker": {"symbol": "BTCUSDT", "bidPrice": "100", "askPrice": "125"},
        }
        receipt["results"]["BTCUSDT"]["sizing"] = evaluate_bounded_executable_notional(
            receipt["results"]["BTCUSDT"]["source_snapshot"]["symbol"],
            receipt["results"]["BTCUSDT"]["source_snapshot"]["book_ticker"],
            desired_notional_usdt=50,
            maximum_notional_usdt=50,
        )
        self.assertEqual(
            order_feasibility_receipt_failures(
                receipt,
                expected_symbols=["BTCUSDT"],
                expected_desired_notional_usdt=50,
                expected_maximum_notional_usdt=50,
                now=now,
            ),
            [],
        )
        rejected = json.loads(json.dumps(receipt))
        rejected["results"]["BTCUSDT"]["sizing"]["status"] = "RISK_CAP_VETO"
        self.assertIn(
            "exchange filter feasibility contains a risk-cap veto or invalid sizing replay",
            order_feasibility_receipt_failures(
                rejected,
                expected_symbols=["BTCUSDT"],
                expected_desired_notional_usdt=50,
                expected_maximum_notional_usdt=50,
                now=now,
            ),
        )

    def test_alpha007_order_path_faults_after_bounded_exit_retries(self) -> None:
        filters = SymbolFilters.from_values(
            tick_size="0.1",
            step_size="0.0001",
            min_qty="0.0001",
            max_qty="100",
            min_notional="5",
        )
        engine = Alpha007OrderPath()
        entry = engine.on_signal(
            signal_id="bounded-retries",
            side="BUY",
            decision_ms=4_000_000,
            observed_ms=4_000_001,
            best_bid="100000",
            best_ask="100000.1",
            filters=filters,
        )
        engine.on_entry_update(
            client_order_id=entry.client_order_id,
            event_ms=4_001_000,
            cumulative_filled_qty=entry.quantity,
            terminal=True,
        )
        current = engine.on_timer(4_060_000)[0]
        for event_ms in (4_060_100, 4_060_200):
            retry = engine.on_exit_update(
                client_order_id=current.client_order_id,
                event_ms=event_ms,
                cumulative_filled_qty="0",
                terminal=True,
            )
            self.assertEqual(len(retry), 1)
            current = retry[0]
        with self.assertRaisesRegex(OrderPathError, "maximum_retries"):
            engine.on_exit_update(
                client_order_id=current.client_order_id,
                event_ms=4_060_300,
                cumulative_filled_qty="0",
                terminal=True,
            )
        self.assertEqual(engine.state, "FAULT")


class MonitoringTests(unittest.TestCase):
    def test_shadow_campaign_progress_is_integrity_only_and_rejects_bad_rows(self) -> None:
        row = {
            "signal_id": "calibration-BTCUSDT-001",
            "symbol": "BTCUSDT",
            "probe_kind": "public_queue_estimate_no_order_submitted",
            "trigger_kind": "periodic_calibration",
            "side": "buy",
            "aggregate_trade_ids_continuous": True,
            "submitted_qty": 1.0,
            "filled_qty": 1.0,
            "mid_after_5m": 100.0,
        }
        report = summarize_shadow_campaign_progress(
            [row], symbols=["BTCUSDT"], orders_per_symbol=2, campaign_status="RUNNING"
        )
        self.assertEqual(report["integrity_status"], "PASS")
        self.assertFalse(report["campaign_complete"])
        self.assertFalse(report["cost_suitability_determined"])
        self.assertFalse(report["performance_determined"])
        self.assertEqual(report["promotion_capability"], "DIAGNOSTIC_ONLY")
        self.assertFalse(
            summarize_shadow_campaign_progress(
                [dict(row, signal_id="calibration-BTCUSDT-002")],
                symbols=["BTCUSDT"],
                orders_per_symbol=1,
                campaign_status="RUNNING",
            )["campaign_complete"]
        )
        self.assertTrue(
            summarize_shadow_campaign_progress(
                [row],
                symbols=["BTCUSDT"],
                orders_per_symbol=1,
                campaign_status="COMPLETE",
            )["campaign_complete"]
        )
        bad = summarize_shadow_campaign_progress(
            [dict(row, aggregate_trade_ids_continuous=False)],
            symbols=["BTCUSDT"],
            orders_per_symbol=1,
            campaign_status="RUNNING",
        )
        self.assertEqual(bad["integrity_status"], "FAIL")
        self.assertIn("row[0]:aggregate_trade_ids_not_continuous", bad["integrity_failures"])

    def test_alpha015_return_free_membership_diagnostic(self) -> None:
        summary = summarize_eligible_membership(
            [
                ("2026-10-10", {"BTCUSDT", "ETHUSDT", "SOLUSDT"}),
                ("2026-10-11", {"BTCUSDT", "ETHUSDT", "BNBUSDT"}),
            ],
            minimum_cross_section=3,
        )
        self.assertEqual(summary["source_scope"], "SEALED_UNIVERSE_MEMBERSHIP_ONLY")
        self.assertFalse(summary["return_or_price_data_accessed"])
        self.assertTrue(summary["minimum_cross_section_met_every_day"])
        self.assertEqual(summary["membership_transitions"][0]["entrants"], 1)
        self.assertEqual(summary["membership_transitions"][0]["exits"], 1)
        self.assertAlmostEqual(
            summary["membership_transitions"][0]["jaccard_similarity"], 0.5
        )
        with self.assertRaisesRegex(ValueError, "one record per UTC date"):
            summarize_eligible_membership(
                [("2026-10-10", {"BTCUSDT"}), ("2026-10-10", {"ETHUSDT"})]
            )

    def test_alpha015_operational_milestones_are_non_promotional(self) -> None:
        pending = build_alpha015_collection_milestones(
            current_days=4, universe_status="ACCUMULATING"
        )
        self.assertEqual(
            [(item["days"], item["status"]) for item in pending],
            [(30, "PENDING"), (60, "PENDING"), (90, "PENDING")],
        )
        due = build_alpha015_collection_milestones(
            current_days=90, universe_status="ACCUMULATING"
        )
        self.assertTrue(all(item["status"] == "REVIEW_DUE" for item in due))
        failed = build_alpha015_collection_milestones(
            current_days=90, universe_status="DATA_INTEGRITY_FAIL"
        )
        self.assertTrue(
            all(item["status"] == "BLOCKED_DATA_INTEGRITY" for item in failed)
        )
        self.assertTrue(
            all(
                item["promotion_capability"] == "DIAGNOSTIC_ONLY"
                and not item["return_data_authorized"]
                and not item["experiment_registration_authorized"]
                and item["live_order_authorization"] == "CLOSED"
                for item in due
            )
        )

    def test_periodic_execution_calibration_is_strictly_non_promotion(self) -> None:
        manifest = {
            "symbols": ["BTCUSDT"],
            "orders_per_symbol": 1,
            "orders": 1,
            "notional_usdt": 50.0,
            "timeout_seconds": 300,
            "markout_seconds": 300,
        }
        row = {
            "signal_id": "calibration-BTCUSDT-001",
            "symbol": "BTCUSDT",
            "probe_kind": "public_queue_estimate_no_order_submitted",
            "trigger_kind": "periodic_calibration",
            "side": "buy",
            "requested_notional_usdt": 50.0,
            "timeout_seconds": 300,
            "markout_seconds": 300,
            "requested_limit_price_replayed": False,
            "order_marketable_at_probe": False,
            "aggregate_trade_ids_continuous": True,
            "submitted_qty": 1.0,
            "filled_qty": 1.0,
            "submitted_at": "2026-10-09T00:00:00+00:00",
            "order_observation_ended_at": "2026-10-09T00:00:15+00:00",
            "observed_until": "2026-10-09T00:05:15+00:00",
            "fill_observed_at": "2026-10-09T00:00:15+00:00",
            "markout_observed_at": "2026-10-09T00:05:15+00:00",
            "mid_after_5m": 100.0,
        }
        timing = validate_calibration_rows([row], manifest)
        self.assertEqual(timing["distinct_utc_days"], 1)
        promotion_like = dict(row, trigger_kind="alpha001_live_order_attempt")
        with self.assertRaisesRegex(ValueError, "periodic calibration policy"):
            validate_calibration_rows([promotion_like], manifest)

    def test_calibration_receipt_default_path_is_campaign_versioned(self) -> None:
        prefix = default_output_prefix(
            Path("research/execution/raw/alpha001-entry-20261013T044659Z.manifest.json")
        )
        self.assertEqual(
            prefix,
            Path("reports/execution-calibration-alpha001-entry-20261013T044659Z"),
        )
        self.assertNotEqual(prefix, Path("reports/execution-calibration"))

    def test_scheduled_calibration_waits_until_aware_deadline_and_detects_failure(self) -> None:
        deadline = datetime(2026, 10, 14, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            seconds_until(deadline, now=deadline - timedelta(seconds=15)), 15.0
        )
        self.assertEqual(seconds_until(deadline, now=deadline), 0.0)
        self.assertTrue(terminal_campaign_failure("FAILED"))
        self.assertTrue(terminal_campaign_failure("STOPPED"))
        self.assertFalse(terminal_campaign_failure("RUNNING"))
        self.assertFalse(
            independent_window_not_before(
                deadline,
                {"next_eligible_independent_window_start_at": "2026-10-14T10:00:00+00:00"},
            )
        )
        self.assertTrue(
            independent_window_not_before(
                deadline,
                {"next_eligible_independent_window_start_at": "2026-10-14T08:00:00+00:00"},
            )
        )

    def test_versioned_calibration_inventory_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            reports = root / "reports"
            reports.mkdir()
            evidence = root / "raw"
            evidence.mkdir()
            manifest_a = evidence / "a.manifest.json"
            rows_a = evidence / "a.jsonl"
            manifest_a.write_text('{"campaign":"a"}\n', encoding="utf-8")
            rows_a.write_text('{"row":"a"}\n', encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "policy": "periodic-public-l2-entry-calibration-v1",
                "status": "CALIBRATION_PASS",
                "promotion_eligible": False,
                "source": {
                    "manifest_path": "raw/a.manifest.json",
                    "manifest_sha256": sha256(manifest_a.read_bytes()).hexdigest(),
                    "raw_path": "raw/a.jsonl",
                    "raw_sha256": sha256(rows_a.read_bytes()).hexdigest(),
                },
                "timing": {"first_submitted_at": "2026-10-01T00:00:00+00:00", "last_observed_at": "2026-10-01T01:00:00+00:00"},
                "summary": {
                    "orders": 100,
                    "fill_rate_by_qty": 0.90,
                    "conservative_all_in_execution_drag_bps": 3.0,
                },
                "cost_reference": {
                    "worst_symbol_observed_all_in_drag_bps": 4.0,
                    "descriptive_research_stress_floor_bps": 4.0,
                    "includes_fee": True,
                    "do_not_add_trading_fee_twice": True,
                },
            }
            (reports / "execution-calibration-a.json").write_text(json.dumps(receipt), encoding="utf-8")
            # The generated inventory shares the report prefix but is not an
            # input receipt; it must be ignored rather than treated as a
            # malformed calibration.
            (reports / "execution-calibration-stability.json").write_text(
                json.dumps({"status": "WAITING_INDEPENDENT_CALIBRATION_RECEIPTS"}),
                encoding="utf-8",
            )
            pending = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(pending["status"], "WAITING_INDEPENDENT_CALIBRATION_RECEIPTS")
            self.assertEqual(
                pending["execution_path_coverage"]["observed_scopes"], []
            )
            self.assertFalse(pending["execution_path_coverage"]["complete"])
            second = dict(receipt)
            manifest_b = evidence / "b.manifest.json"
            rows_b = evidence / "b.jsonl"
            manifest_b.write_text('{"campaign":"b"}\n', encoding="utf-8")
            rows_b.write_text('{"row":"b"}\n', encoding="utf-8")
            second["source"] = {
                "manifest_path": "raw/b.manifest.json",
                "manifest_sha256": sha256(manifest_b.read_bytes()).hexdigest(),
                "raw_path": "raw/b.jsonl",
                "raw_sha256": sha256(rows_b.read_bytes()).hexdigest(),
            }
            second["measured_scopes"] = ["long_entry", "short_entry"]
            (reports / "execution-calibration-b.json").write_text(json.dumps(second), encoding="utf-8")
            nearby = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(nearby["status"], "WAITING_TEMPORALLY_SEPARATED_RECEIPTS")
            second["timing"] = {
                "first_submitted_at": "2026-10-02T02:00:00+00:00",
                "last_observed_at": "2026-10-02T03:00:00+00:00",
            }
            (reports / "execution-calibration-b.json").write_text(json.dumps(second), encoding="utf-8")
            ready = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(ready["status"], "READY_FOR_DESCRIPTIVE_COMPARISON")
            self.assertTrue(
                ready["temporal_independence"]["temporally_separated_pair_available"]
            )
            self.assertEqual(
                ready["temporal_independence"]["next_eligible_independent_window_start_at"],
                "2026-10-03T03:00:00+00:00",
            )
            self.assertEqual(
                ready["descriptive_cost_dispersion"]["range_bps"], 0.0
            )
            self.assertFalse(
                ready["descriptive_cost_dispersion"]["automatic_cost_model_mutation"]
            )
            self.assertEqual(
                ready["descriptive_cost_dispersion"]["maximum_observed_descriptive_stress_floor_bps"],
                4.0,
            )
            self.assertEqual(
                ready["execution_path_coverage"]["observed_scopes"],
                ["long_entry", "short_entry"],
            )
            self.assertFalse(ready["execution_path_coverage"]["complete"])
            self.assertFalse(ready["automatic_cost_model_mutation"])
            self.assertEqual(ready["live_order_authorization"], "CLOSED")

            missing_fee_declaration = dict(second)
            manifest_fee = evidence / "fee.manifest.json"
            rows_fee = evidence / "fee.jsonl"
            manifest_fee.write_text('{"campaign":"fee"}\n', encoding="utf-8")
            rows_fee.write_text('{"row":"fee"}\n', encoding="utf-8")
            missing_fee_declaration["source"] = {
                "manifest_path": "raw/fee.manifest.json",
                "manifest_sha256": sha256(manifest_fee.read_bytes()).hexdigest(),
                "raw_path": "raw/fee.jsonl",
                "raw_sha256": sha256(rows_fee.read_bytes()).hexdigest(),
            }
            missing_fee_declaration["cost_reference"] = {
                "worst_symbol_observed_all_in_drag_bps": 4.0,
                "descriptive_research_stress_floor_bps": 4.0,
            }
            (reports / "execution-calibration-fee.json").write_text(
                json.dumps(missing_fee_declaration), encoding="utf-8"
            )
            fee_invalid = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(fee_invalid["status"], "DATA_INTEGRITY_FAIL")
            (reports / "execution-calibration-fee.json").unlink()

            rows_b.write_text('{"row":"tampered"}\n', encoding="utf-8")
            tampered = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(tampered["status"], "DATA_INTEGRITY_FAIL")
            rows_b.write_text('{"row":"b"}\n', encoding="utf-8")

            malformed = dict(receipt)
            malformed["source"] = {
                "manifest_path": "raw/malformed.manifest.json",
                "manifest_sha256": "a" * 64,
                "raw_path": "raw/malformed.jsonl",
                "raw_sha256": "a" * 64,
            }
            malformed["timing"] = {
                "first_submitted_at": "not-a-timestamp",
                "last_observed_at": "2026-10-03T00:00:00+00:00",
            }
            (reports / "execution-calibration-malformed.json").write_text(
                json.dumps(malformed), encoding="utf-8"
            )
            invalid = summarize_versioned_calibration_receipts(reports)
            self.assertEqual(invalid["status"], "DATA_INTEGRITY_FAIL")

    def test_entry_execution_trigger_requires_actual_alpha001_order_attempt(self) -> None:
        detected = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
        line = (
            "2026-10-09 21:00:00,000 - freqtrade.freqtradebot - INFO - "
            "Long signal found: about create a new trade for ETH/USDT:USDT "
            "with stake_amount: 45.0 and price: 2500.25 ..."
        )
        trigger = parse_order_attempt(line, offset=1234, detected_at=detected)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["symbol"], "ETHUSDT")
        self.assertEqual(trigger["requested_stake_usdt"], 45.0)
        self.assertEqual(trigger["freqtrade_requested_limit_price"], 2500.25)
        self.assertEqual(trigger["trigger_log_offset"], 1234)
        self.assertIsNone(
            parse_order_attempt(
                line.replace("Long signal found", "Replacing buy order"),
                offset=1234,
                detected_at=detected,
            )
        )
        self.assertIsNone(
            parse_order_attempt(
                line.replace("stake_amount: 45.0", "stake_amount: 51.0"),
                offset=1234,
                detected_at=detected,
            )
        )

    def test_signal_entry_probe_replays_exact_limit_and_detects_taker_cross(self) -> None:
        book = {
            "bids": [(99.0, 3.0), (98.0, 4.0)],
            "asks": [(101.0, 1.0), (102.0, 2.0)],
        }
        self.assertEqual(queue_ahead_at_limit(side="buy", limit_price=99.0, book=book), 3.0)
        self.assertEqual(queue_ahead_at_limit(side="buy", limit_price=100.0, book=book), 0.0)
        filled, average = immediate_limit_fill(
            side="buy", limit_price=102.0, quantity=2.0, book=book
        )
        self.assertEqual(filled, 2.0)
        self.assertEqual(average, 101.5)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            queue_ahead_at_limit(side="buy", limit_price=97.0, book=book)

    def test_shadow_probe_does_not_retry_binance_ban_or_rate_limit(self) -> None:
        for status in (418, 429):
            with self.subTest(status=status), patch(
                "scripts.shadow_maker_probe.urlopen",
                side_effect=HTTPError("https://fapi.binance.com", status, "limited", {}, None),
            ) as mocked:
                with self.assertRaises(HTTPError):
                    shadow_get_json("/fapi/v1/time", {}, attempts=3)
                self.assertEqual(mocked.call_count, 1)

    def test_execution_audit_is_protocol_and_raw_source_bound(self) -> None:
        rows = []
        for index in range(100):
            line_digest = sha256(f"trigger-{index}".encode()).hexdigest()
            rows.append(
                {
                    "signal_id": f"alpha001-BTCUSDT-{index}-{line_digest[:12]}",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "submitted_qty": 1,
                    "reference_mid": 100,
                    "filled_qty": 1 if index < 30 else 0,
                    "average_fill_price": 100 if index < 30 else 0,
                    "maker_filled_qty": 1 if index < 30 else 0,
                    "taker_filled_qty": 0,
                    "fee": 0.02 if index < 30 else 0,
                    "mid_after_5m": 100 if index < 30 else None,
                    "mid_after_timeout": None if index < 30 else 100,
                    "probe_kind": "public_queue_estimate_no_order_submitted",
                    "trigger_kind": "alpha001_live_order_attempt",
                    "trigger_source": "local_freqtrade_initial_entry_log",
                    "requested_notional_usdt": 50.0,
                    "requested_stake_usdt": 50.0,
                    "timeout_seconds": 300,
                    "markout_seconds": 300,
                    "trigger_detected_at": "2026-10-09T00:00:00+00:00",
                    "submitted_at": "2026-10-09T00:00:01+00:00",
                    "trigger_pair": "BTC/USDT:USDT",
                    "trigger_log_offset": index,
                    "trigger_line_sha256": line_digest,
                    "freqtrade_requested_limit_price": 100.0,
                    "shadow_limit_price": 100.0,
                    "requested_limit_price_replayed": True,
                    "order_observation_ended_at": "2026-10-09T00:05:01+00:00",
                    "fill_observed_at": "2026-10-09T00:00:01+00:00" if index < 30 else None,
                    "markout_observed_at": "2026-10-09T00:05:01+00:00" if index < 30 else None,
                    "aggregate_trades_observed": 1 if index < 30 else 0,
                    "first_requested_aggregate_trade_id": index * 10,
                    "last_observed_aggregate_trade_id": index * 10 if index < 30 else None,
                    "aggregate_trade_ids_continuous": True,
                }
            )
        summary = summarize_maker_execution(
            rows,
            min_orders=100,
            min_filled_orders=30,
            gross_edge_budget_bps=10,
        )
        self.assertEqual(summary["status"], "PASS")
        per_symbol_summary = summarize_maker_execution(
            rows,
            min_orders=20,
            min_filled_orders=6,
            gross_edge_budget_bps=10,
        )
        protocol = {"strategy.py": "a" * 64}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "research" / "execution" / "raw"
            raw_root.mkdir(parents=True)
            source = raw_root / "alpha001-signal-entry-test.jsonl"
            source.write_text(
                "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
                encoding="utf-8",
            )
            manifest = raw_root / "alpha001-signal-entry-test.manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "SEALED_ON_STOP",
                        "protocol_sha256": protocol,
                        "source_path": str(source.relative_to(root)),
                        "source_sha256": sha256(source.read_bytes()).hexdigest(),
                        "order_scope": "entry_long_only",
                        "required_side": "buy",
                        "evidence_purpose": "live_promotion_candidate",
                        "trigger_kind": "alpha001_live_order_attempt",
                        "maximum_notional_usdt": 50.0,
                        "timeout_seconds": 300,
                        "markout_seconds": 300,
                        "orders": len(rows),
                    }
                ),
                encoding="utf-8",
            )
            receipt = {
                "schema_version": EXECUTION_AUDIT_SCHEMA_VERSION,
                "policy": EXECUTION_AUDIT_POLICY,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "candidate": "FuturesBtcRegimeRiskBudgetLong",
                "evidence_kind": "public_l2_shadow",
                "order_scope": "entry_long_only",
                "required_side": "buy",
                "source_segments": [
                    {
                        "source_path": str(source.relative_to(root)),
                        "source_sha256": sha256(source.read_bytes()).hexdigest(),
                        "collection_manifest_path": str(manifest.relative_to(root)),
                        "collection_manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
                        "orders": len(rows),
                    }
                ],
                "protocol_sha256": protocol,
                "current_protocol_manifest_inventory": [
                    {
                        "path": str(manifest.relative_to(root)),
                        "sha256": sha256(manifest.read_bytes()).hexdigest(),
                        "status": "SEALED_ON_STOP",
                    }
                ],
                "gross_edge_budget_bps": 10,
                "maximum_shadow_notional_usdt": 50.0,
                "required_trigger_kind": "alpha001_live_order_attempt",
                "maximum_trigger_to_probe_delay_seconds": 10,
                "entry_timeout_seconds": 300,
                "post_fill_markout_seconds": 300,
                "exact_freqtrade_limit_price_replay_required": True,
                "all_current_protocol_execution_segments_required": True,
                "symbols": ["BTCUSDT"],
                "per_symbol": {"BTCUSDT": per_symbol_summary},
                **summary,
            }
            self.assertEqual(
                execution_audit_receipt_failures(
                    receipt,
                    expected_protocol=protocol,
                    expected_symbols=["BTCUSDT"],
                    project_root=root,
                ),
                [],
            )
            second_source = raw_root / "alpha001-signal-entry-test2.jsonl"
            second_source.write_text("", encoding="utf-8")
            second_manifest = raw_root / "alpha001-signal-entry-test2.manifest.json"
            second_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "SEALED_ON_STOP",
                        "protocol_sha256": protocol,
                        "source_path": str(second_source.relative_to(root)),
                        "source_sha256": sha256(second_source.read_bytes()).hexdigest(),
                        "orders": 0,
                        "order_scope": "entry_long_only",
                        "required_side": "buy",
                        "evidence_purpose": "live_promotion_candidate",
                        "trigger_kind": "alpha001_live_order_attempt",
                        "maximum_notional_usdt": 50.0,
                        "timeout_seconds": 300,
                        "markout_seconds": 300,
                    }
                ),
                encoding="utf-8",
            )
            combined_receipt = json.loads(json.dumps(receipt))
            combined_receipt["source_segments"].append(
                {
                    "source_path": str(second_source.relative_to(root)),
                    "source_sha256": sha256(second_source.read_bytes()).hexdigest(),
                    "collection_manifest_path": str(second_manifest.relative_to(root)),
                    "collection_manifest_sha256": sha256(second_manifest.read_bytes()).hexdigest(),
                    "orders": 0,
                }
            )
            combined_receipt["current_protocol_manifest_inventory"].append(
                {
                    "path": str(second_manifest.relative_to(root)),
                    "sha256": sha256(second_manifest.read_bytes()).hexdigest(),
                    "status": "SEALED_ON_STOP",
                }
            )
            self.assertEqual(
                execution_audit_receipt_failures(
                    combined_receipt,
                    expected_protocol=protocol,
                    expected_symbols=["BTCUSDT"],
                    project_root=root,
                ),
                [],
            )
            second_manifest.unlink()
            second_source.unlink()
            symbol_failures = execution_audit_receipt_failures(
                receipt,
                expected_protocol=protocol,
                expected_symbols=["BTCUSDT", "ETHUSDT"],
                project_root=root,
            )
            self.assertIn(
                "execution audit symbols do not exactly match the live whitelist",
                symbol_failures,
            )
            self.assertIn(
                "execution audit per-symbol gates are not fully PASS",
                symbol_failures,
            )
            weak_symbol_receipt = json.loads(json.dumps(receipt))
            weak_symbol_receipt["per_symbol"]["BTCUSDT"]["orders"] = 1
            weak_symbol_receipt["per_symbol"]["BTCUSDT"]["status"] = "PASS"
            self.assertIn(
                "execution audit per-symbol gates are not fully PASS",
                execution_audit_receipt_failures(
                    weak_symbol_receipt,
                    expected_protocol=protocol,
                    expected_symbols=["BTCUSDT"],
                    project_root=root,
                ),
            )
            inflated_budget_receipt = dict(receipt)
            inflated_budget_receipt["gross_edge_budget_bps"] = 1000
            self.assertIn(
                "execution audit all-in drag does not fit inside gross edge",
                execution_audit_receipt_failures(
                    inflated_budget_receipt,
                    expected_protocol=protocol,
                    expected_symbols=["BTCUSDT"],
                    project_root=root,
                ),
            )
            source.write_text("tampered\n", encoding="utf-8")
            failures = execution_audit_receipt_failures(
                receipt,
                expected_protocol=protocol,
                expected_symbols=["BTCUSDT"],
                project_root=root,
            )
        self.assertIn("execution audit raw source SHA-256 does not match", failures)

    def test_sleep_guard_requires_exact_target_and_strong_assertions(self) -> None:
        process_table = """
101 /usr/bin/caffeinate -i -w 700
102 /usr/bin/caffeinate -ims -w 701
103 python worker.py --text 'caffeinate -ims -w 700'
104 /usr/bin/caffeinate -dims -w 700
"""
        guard = inspect_caffeinate_guard(700, ps_output=process_table, executable="/usr/bin/caffeinate")
        self.assertTrue(guard["available"])
        self.assertTrue(guard["running"])
        self.assertEqual([item["pid"] for item in guard["guards"]], [101, 104])
        self.assertFalse(guard["guards"][0]["valid"])
        self.assertTrue(guard["guards"][1]["valid"])

        weak_only = inspect_caffeinate_guard(
            700,
            ps_output="101 /usr/bin/caffeinate -i -w 700\n",
            executable="/usr/bin/caffeinate",
        )
        self.assertFalse(weak_only["running"])

    def test_alpha011_completed_months_exclude_current_partial_month(self) -> None:
        self.assertEqual(
            completed_eligible_months(datetime(2026, 12, 15, tzinfo=timezone.utc)),
            ["2026-10", "2026-11"],
        )

    def test_alpha011_scheduler_health_requires_live_pid_and_fresh_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "status.json"
            now = datetime.now(timezone.utc)
            status_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "heartbeat_at": now.isoformat(),
                        "interval_seconds": 21_600,
                        "state": "SLEEPING",
                        "last_exit_code": 0,
                    }
                ),
                encoding="utf-8",
            )
            with patch("scripts.alpha011_status._scheduler_process_running", return_value=True):
                self.assertEqual(
                    alpha011_scheduler_health(now=now, path=status_path)["health"],
                    "RUNNING_HEALTHY",
                )
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            payload["heartbeat_at"] = (now - timedelta(hours=7)).isoformat()
            status_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("scripts.alpha011_status._scheduler_process_running", return_value=True):
                self.assertEqual(
                    alpha011_scheduler_health(now=now, path=status_path)["health"],
                    "HEARTBEAT_STALE",
                )

    def test_alpha011_publisher_probe_validates_checksum_binding(self) -> None:
        day = datetime(2026, 10, 9).date()
        name = "BTCUSDT-aggTrades-2026-10-09.zip"
        payload = f"{'a' * 64}  {name}\n".encode()
        with patch("scripts.update_alpha011_data.fetch_bytes", return_value=payload):
            self.assertTrue(alpha011_publisher_has_day(day))
        with patch("scripts.update_alpha011_data.fetch_bytes", return_value=None):
            self.assertFalse(alpha011_publisher_has_day(day))
        with patch(
            "scripts.update_alpha011_data.fetch_bytes",
            return_value=f"{'a' * 64}  wrong.zip\n".encode(),
        ):
            with self.assertRaisesRegex(Exception, "expected archive name"):
                alpha011_publisher_has_day(day)

    def test_alpha013_hourly_archive_requires_exact_complete_utc_day(self) -> None:
        day = datetime(2026, 10, 10).date()
        start_ms = int(datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp() * 1000)

        def archive_payload(row_count: int = 24, *, gap_at: int | None = None) -> bytes:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                lines = [",".join(ALPHA013_COLUMNS)]
                for hour in range(row_count):
                    effective_hour = hour + (1 if gap_at is not None and hour >= gap_at else 0)
                    opened = start_ms + effective_hour * 3_600_000
                    lines.append(
                        f"{opened},100,102,99,101,1,{opened + 3_599_999},100,10,0.5,50,0"
                    )
                archive.writestr(
                    "BTCUSDT-1h-2026-10-10.csv", "\n".join(lines) + "\n"
                )
            return buffer.getvalue()

        valid = archive_payload()
        result = validate_alpha013_hourly_archive(
            valid,
            expected_digest=sha256(valid).hexdigest(),
            symbol="BTCUSDT",
            day=day,
        )
        self.assertEqual(result["rows"], 24)
        with self.assertRaisesRegex(Alpha013DownloadError, "24 hourly rows"):
            short = archive_payload(23)
            validate_alpha013_hourly_archive(
                short,
                expected_digest=sha256(short).hexdigest(),
                symbol="BTCUSDT",
                day=day,
            )
        with self.assertRaisesRegex(Alpha013DownloadError, "continuous exact UTC day"):
            gapped = archive_payload(gap_at=10)
            validate_alpha013_hourly_archive(
                gapped,
                expected_digest=sha256(gapped).hexdigest(),
                symbol="BTCUSDT",
                day=day,
            )

    def test_alpha013_checksum_and_scheduler_fail_closed(self) -> None:
        name = "BTCUSDT-1h-2026-10-10.zip"
        digest = "b" * 64
        self.assertEqual(
            parse_alpha013_checksum(f"{digest}  {name}\n".encode(), expected_name=name),
            digest,
        )
        with self.assertRaisesRegex(Alpha013DownloadError, "expected archive name"):
            parse_alpha013_checksum(
                f"{digest}  other.zip\n".encode(), expected_name=name
            )
        with tempfile.TemporaryDirectory() as directory:
            status_path = Path(directory) / "status.json"
            now = datetime.now(timezone.utc)
            status_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "heartbeat_at": now.isoformat(),
                        "interval_seconds": 21_600,
                        "state": "SLEEPING",
                        "last_exit_code": 0,
                    }
                ),
                encoding="utf-8",
            )
            with patch("scripts.alpha013_status._process_running", return_value=True):
                self.assertEqual(
                    alpha013_scheduler_health(now=now, path=status_path)["health"],
                    "RUNNING_HEALTHY",
                )
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            payload["heartbeat_at"] = (now - timedelta(hours=7)).isoformat()
            status_path.write_text(json.dumps(payload), encoding="utf-8")
            with patch("scripts.alpha013_status._process_running", return_value=True):
                self.assertEqual(
                    alpha013_scheduler_health(now=now, path=status_path)["health"],
                    "RUNNING_STALE",
                )

    def test_alpha013_publisher_probe_only_treats_http_404_as_not_ready(self) -> None:
        day = datetime(2026, 10, 10).date()
        name = "BTCUSDT-1h-2026-10-10.zip"
        payload = f"{'c' * 64}  {name}\n".encode()
        with patch("scripts.update_alpha013_data._fetch", return_value=payload):
            self.assertTrue(alpha013_publisher_has_day(day))

        missing = HTTPError("https://example.test", 404, "Not Found", {}, None)
        wrapped_missing = Alpha013DownloadError("download failed")
        wrapped_missing.__cause__ = missing
        with patch("scripts.update_alpha013_data._fetch", side_effect=wrapped_missing):
            self.assertFalse(alpha013_publisher_has_day(day))

        unavailable = HTTPError(
            "https://example.test", 503, "Service Unavailable", {}, None
        )
        wrapped_unavailable = Alpha013DownloadError("download failed")
        wrapped_unavailable.__cause__ = unavailable
        with patch("scripts.update_alpha013_data._fetch", side_effect=wrapped_unavailable):
            with self.assertRaises(Alpha013DownloadError):
                alpha013_publisher_has_day(day)

    def test_forward_updaters_catch_up_old_days_before_waiting_on_latest(self) -> None:
        days = [
            datetime(2026, 10, 10).date(),
            datetime(2026, 10, 11).date(),
            datetime(2026, 10, 12).date(),
        ]
        with patch(
            "scripts.update_alpha011_data.publisher_has_day",
            side_effect=lambda day, timeout: day < days[-1],
        ) as alpha011_probe:
            self.assertEqual(alpha011_ready_prefix(days), days[:2])
            self.assertEqual(alpha011_probe.call_count, 3)
        with patch(
            "scripts.update_alpha013_data.publisher_has_day",
            side_effect=lambda day, timeout: day < days[-1],
        ) as alpha013_probe:
            self.assertEqual(alpha013_ready_prefix(days), days[:2])
            self.assertEqual(alpha013_probe.call_count, 3)

        with (
            patch("scripts.update_alpha011_data.reconcile_completed_months", return_value=0) as reconcile,
            patch("scripts.update_alpha011_data.analyze_and_sync", return_value=0) as analyze,
        ):
            self.assertIsNone(
                finish_alpha011_development(
                    {"closed_development_days": 59},
                    now=datetime(2026, 12, 10, tzinfo=timezone.utc),
                )
            )
            self.assertEqual(
                finish_alpha011_development(
                    {
                        "closed_development_days": 60,
                        "current_protocol_manifest": "manifest.json",
                        "performance_calculated": False,
                    },
                    now=datetime(2026, 12, 10, tzinfo=timezone.utc),
                ),
                0,
            )
            reconcile.assert_called_once()
            analyze.assert_called_once_with(Path("manifest.json"))

    def test_alpha013_status_distinguishes_publication_lag_from_overdue_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_root = Path(directory)
            scheduler_path = raw_root / "scheduler.json"
            forward_report_path = raw_root / "forward.json"
            day_manifests: list[dict[str, str]] = []
            forward_report = {
                "candidate": "ALPHA_013_FUNDING_DISPERSION_REVERSAL",
                "preregistration_sha256": sha256(
                    (
                        Path(__file__).resolve().parents[1]
                        / "research"
                        / "alpha013-preregistration.json"
                    ).read_bytes()
                ).hexdigest(),
                "data_integrity": {
                    "sealed_days": 0,
                    "day_manifests": day_manifests,
                    "source_inventory_sha256": alpha013_manifest_inventory_digest(
                        day_manifests
                    ),
                    "protocol_sha256": alpha013_protocol_digests(),
                },
            }
            forward_report_path.write_text(
                json.dumps(forward_report), encoding="utf-8"
            )
            within_grace = alpha013_status_payload(
                now=datetime(2026, 10, 11, 4, tzinfo=timezone.utc),
                raw_root=raw_root,
                scheduler_path=scheduler_path,
                forward_report_path=forward_report_path,
            )
            self.assertEqual(within_grace["status"], "WAITING_PUBLISHER_ARCHIVE")
            self.assertEqual(within_grace["pending_publication_days"], ["2026-10-10"])
            self.assertEqual(within_grace["overdue_missing_days"], [])
            self.assertTrue(within_grace["forward_report_current"])

            forward_report["data_integrity"]["source_inventory_sha256"] = "0" * 64
            forward_report_path.write_text(
                json.dumps(forward_report), encoding="utf-8"
            )
            tampered = alpha013_status_payload(
                now=datetime(2026, 10, 11, 4, tzinfo=timezone.utc),
                raw_root=raw_root,
                scheduler_path=scheduler_path,
                forward_report_path=forward_report_path,
            )
            self.assertFalse(tampered["forward_report_current"])

            overdue = alpha013_status_payload(
                now=datetime(2026, 10, 12, tzinfo=timezone.utc),
                raw_root=raw_root,
                scheduler_path=scheduler_path,
                forward_report_path=forward_report_path,
            )
            self.assertEqual(overdue["status"], "DOWNLOAD_REQUIRED")
            self.assertEqual(overdue["pending_publication_days"], ["2026-10-11"])
            self.assertEqual(overdue["overdue_missing_days"], ["2026-10-10"])

    def test_alpha013_funding_reconciliation_rejects_rate_or_time_drift(self) -> None:
        rest = [
            {"fundingTime": 1000, "fundingRate": "0.0001"},
            {"fundingTime": 2000, "fundingRate": "-0.0002"},
        ]
        matched = compare_alpha013_funding_rows(
            rest, [(1000, Decimal("0.0001")), (2000, Decimal("-0.0002"))]
        )
        self.assertEqual(matched["status"], "PASS")
        drifted = compare_alpha013_funding_rows(
            rest, [(1000, Decimal("0.0001")), (2000, Decimal("-0.0003"))]
        )
        self.assertEqual(drifted["status"], "FAIL")
        self.assertEqual(len(drifted["missing_from_monthly"]), 1)
        self.assertEqual(len(drifted["unexpected_monthly"]), 1)

    def test_alpha011_status_counts_only_current_protocol_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publication_lag = alpha011_status_payload(
                now=datetime(2026, 10, 10, 4, tzinfo=timezone.utc), root=root
            )
            self.assertEqual(publication_lag["status"], "WAITING_PUBLISHER_ARCHIVE")
            self.assertEqual(publication_lag["pending_publication_days"], ["2026-10-09"])
            self.assertEqual(publication_lag["overdue_missing_archive_days"], [])

            archive = root / "BTCUSDT-aggTrades-2026-10-09.zip"
            checksum = root / f"{archive.name}.CHECKSUM"
            archive.write_bytes(b"sealed")
            digest = sha256(archive.read_bytes()).hexdigest()
            checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            funding_dir = root / "funding"
            funding_dir.mkdir()
            funding = funding_dir / "BTCUSDT-funding-2026-10-09.json"
            funding_payload = json.dumps(
                [
                    {
                        "symbol": "BTCUSDT",
                        "fundingTime": 1791504000000,
                        "fundingRate": "0.0001",
                        "markPrice": "90000.0",
                        "rateType": "Regular",
                    }
                ]
            ).encode()
            canonical_funding, funding_rows, funding_metadata = validate_funding_payload(
                funding_payload, symbol="BTCUSDT", day=datetime(2026, 10, 9).date()
            )
            funding.write_bytes(canonical_funding)
            without_manifest = alpha011_status_payload(
                now=datetime(2026, 10, 11, tzinfo=timezone.utc), root=root
            )
            self.assertEqual(without_manifest["sealed_eligible_archives"], 0)
            self.assertEqual(without_manifest["status"], "DOWNLOAD_REQUIRED")

            from scripts.download_binance_aggtrades import protocol_digests as alpha011_protocol

            manifest = {
                "status": "PASS",
                "symbol": "BTCUSDT",
                "start": "2026-10-09",
                "end_exclusive": "2026-10-10",
                "verified_days": 1,
                "protocol_sha256": alpha011_protocol(),
                "files": [
                    {
                        "date": "2026-10-09",
                        "archive": str(archive),
                        "checksum": str(checksum),
                        "funding_file": str(funding),
                        "funding_query": funding_query_url(
                            symbol="BTCUSDT", day=datetime(2026, 10, 9).date()
                        ),
                        "sha256": digest,
                        "compressed_bytes": archive.stat().st_size,
                        "archive_mtime_ns": archive.stat().st_mtime_ns,
                        "first_agg_trade_id": 100,
                        "last_agg_trade_id": 200,
                        **funding_metadata,
                    }
                ],
            }
            (root / "manifest-2026-10-09-2026-10-10.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with_manifest = alpha011_status_payload(
                now=datetime(2026, 10, 11, tzinfo=timezone.utc), root=root
            )
            self.assertEqual(with_manifest["sealed_eligible_archives"], 1)
            self.assertEqual(with_manifest["contiguous_sealed_days"], 1)
            self.assertEqual(with_manifest["closed_development_days"], 0)
            self.assertEqual(with_manifest["invalid_manifest_count"], 0)

            original_funding = funding.read_bytes()
            funding.write_bytes(original_funding.replace(b"0.0001", b"0.0002"))
            funding_tampered = alpha011_status_payload(
                now=datetime(2026, 10, 11, tzinfo=timezone.utc), root=root
            )
            self.assertEqual(funding_tampered["sealed_eligible_archives"], 0)
            self.assertIn(
                "file_0_funding_funding_sha256_mismatch",
                funding_tampered["invalid_manifests"][0]["failures"],
            )
            funding.write_bytes(original_funding)

            archive.write_bytes(b"same manifest, different size")
            tampered = alpha011_status_payload(
                now=datetime(2026, 10, 11, tzinfo=timezone.utc), root=root
            )
            self.assertEqual(tampered["sealed_eligible_archives"], 0)
            self.assertEqual(tampered["invalid_manifest_count"], 1)
            self.assertIn(
                "file_0_archive_metadata_mismatch",
                tampered["invalid_manifests"][0]["failures"],
            )

    @staticmethod
    def _safe_binance_permissions() -> dict:
        return {
            "ipRestrict": True,
            "enableReading": True,
            "enableFutures": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "enableMargin": False,
            "permitsUniversalTransfer": False,
            "enableVanillaOptions": False,
            "enableFixApiTrade": False,
            "enableFixReadOnly": False,
            "enableSpotAndMarginTrading": False,
            "enablePortfolioMarginTrading": False,
        }

    def test_binance_permission_policy_is_least_privilege_and_fail_closed(self) -> None:
        safe = self._safe_binance_permissions()
        self.assertEqual(permission_failures(safe), [])

        excessive = dict(safe)
        excessive["enableWithdrawals"] = True
        excessive["enableSpotAndMarginTrading"] = True
        failures = permission_failures(excessive)
        self.assertIn("enableWithdrawals must be false", failures)
        self.assertIn("enableSpotAndMarginTrading must be false", failures)

        missing = dict(safe)
        del missing["ipRestrict"]
        self.assertIn("ipRestrict must be true", permission_failures(missing))

        changed_schema = dict(safe)
        changed_schema["enableNewProduct"] = False
        self.assertTrue(
            any("unreviewed permission fields" in item for item in permission_failures(changed_schema))
        )

    def test_binance_permission_receipt_is_fresh_and_bound_to_current_key(self) -> None:
        now = datetime(2025, 1, 2, tzinfo=timezone.utc)
        api_key = "A" * 32
        receipt = build_receipt(self._safe_binance_permissions(), api_key=api_key, observed_at=now)
        self.assertEqual(receipt["api_key_fingerprint"], api_key_fingerprint(api_key))
        self.assertEqual(
            permission_audit_receipt_failures(receipt, expected_api_key=api_key, now=now),
            [],
        )

        wrong_key = permission_audit_receipt_failures(receipt, expected_api_key="B" * 32, now=now)
        self.assertIn("permission audit is bound to a different API key", wrong_key)
        stale = permission_audit_receipt_failures(
            receipt,
            expected_api_key=api_key,
            now=now + timedelta(seconds=901),
        )
        self.assertIn("permission audit is stale or future-dated", stale)

        tampered = json.loads(json.dumps(receipt))
        tampered["permissions"]["enableWithdrawals"] = True
        tampered_failures = permission_audit_receipt_failures(tampered, expected_api_key=api_key, now=now)
        self.assertIn("permission audit: enableWithdrawals must be false", tampered_failures)
        self.assertIn("permission audit status does not match its permission fields", tampered_failures)

    @staticmethod
    def _safe_binance_account_state(symbols: list[str]) -> dict:
        return normalize_account_state(
            account={
                "canTrade": True,
                "multiAssetsMargin": False,
                "totalPositionInitialMargin": "0.00000000",
                "totalOpenOrderInitialMargin": "0.00000000",
                "positions": [{"symbol": symbol, "positionAmt": "0"} for symbol in symbols],
            },
            position_mode={"dualSidePosition": False},
            multi_asset_mode={"multiAssetsMargin": False},
            open_orders=[],
            open_algo_orders=[],
            symbol_configurations=[
                {
                    "symbol": symbol,
                    "marginType": "ISOLATED",
                    "isAutoAddMargin": False,
                    "leverage": 1,
                }
                for symbol in symbols
            ],
            required_symbols=symbols,
        )

    def test_binance_account_audit_requires_flat_isolated_one_way_state(self) -> None:
        symbols = ["BTCUSDT", "ETHUSDT"]
        state = self._safe_binance_account_state(symbols)
        self.assertEqual(account_state_failures(state, required_symbols=symbols), [])

        unsafe = json.loads(json.dumps(state))
        unsafe["dual_side_position"] = True
        unsafe["standard_open_order_count"] = 1
        unsafe["symbol_configurations"][0]["margin_type"] = "CROSSED"
        unsafe["symbol_configurations"][1]["leverage"] = 3
        failures = account_state_failures(unsafe, required_symbols=symbols)
        self.assertIn("account must use one-way position mode", failures)
        self.assertIn("USD-M account has standard open orders or an invalid response", failures)
        self.assertTrue(any("margin type must already be ISOLATED" in item for item in failures))
        self.assertTrue(any("leverage must already be 1" in item for item in failures))

    def test_binance_account_receipt_is_fresh_and_bound_to_key_and_symbols(self) -> None:
        now = datetime(2025, 1, 2, tzinfo=timezone.utc)
        symbols = ["BTCUSDT", "ETHUSDT"]
        api_key = "A" * 32
        receipt = build_account_receipt(
            self._safe_binance_account_state(symbols),
            api_key=api_key,
            required_symbols=symbols,
            observed_at=now,
        )
        self.assertEqual(
            account_audit_receipt_failures(
                receipt,
                expected_api_key=api_key,
                expected_symbols=symbols,
                now=now,
            ),
            [],
        )
        wrong_symbols = account_audit_receipt_failures(
            receipt,
            expected_api_key=api_key,
            expected_symbols=["BTCUSDT", "SOLUSDT"],
            now=now,
        )
        self.assertIn("account audit is bound to a different symbol whitelist", wrong_symbols)

    def test_futures_config_pair_names_are_bound_to_binance_symbols(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        config = json.loads((project_root / "user_data" / "config.futures.json").read_text())
        self.assertEqual(
            config_symbols(config),
            ["BNBUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"],
        )

    def test_live_compose_api_must_bind_host_loopback(self) -> None:
        safe = """services:\n  freqtrade-futures:\n    ports:\n      - \"127.0.0.1:8081:8080\"\n"""
        unsafe = """services:\n  freqtrade-futures:\n    ports:\n      - \"8081:8080\"\n"""
        self.assertEqual(compose_api_binding_failures([safe]), [])
        failures = compose_api_binding_failures([unsafe])
        self.assertEqual(len(failures), 1)
        self.assertIn("outside loopback", failures[0])

    def test_live_promotion_requires_current_complete_fresh_research_evidence(self) -> None:
        now = datetime(2025, 1, 2, tzinfo=timezone.utc)
        protocol = {"strategy.py": "abc"}
        report = {
            "candidate": "FuturesBtcRegimeRiskBudgetLong",
            "status": "PASS",
            "protocol_current": True,
            "data_integrity": {"status": "PASS"},
            "gate": {"status": "PASS", "checks": [{"name": "all", "passed": True}]},
            "mtm": {
                "last_observed_at": "2025-01-01T23:59:00Z",
                "monthly": {"complete_months": 12},
            },
            "trades": {"natural_closed_trades": 100, "open_trades": 0},
            "database": "user_data/current.sqlite",
        }
        state = {
            "protocol_sha256": protocol,
            "database": "user_data/current.sqlite",
        }
        artifact_root = tempfile.TemporaryDirectory()
        self.addCleanup(artifact_root.cleanup)
        artifact_root_path = Path(artifact_root.name)
        lookahead_evidence = (
            "filename,strategy,has_bias,total_signals,biased_entry_signals,"
            "biased_exit_signals,biased_indicators\n"
            "FuturesBtcRegimeStrategy.py,FuturesBtcRegimeRiskBudgetLong,"
            "False,30,0,0,\n"
        )
        recursive_evidence = (
            "No lookahead bias on indicators found.\n"
            "│ Indicators │ 1500 (from strategy) │ 2000 │ 2400 │\n"
            "│ ema_200 │ 0.010% │ 0.000% │ - │\n"
        )
        (artifact_root_path / "lookahead.csv").write_text(lookahead_evidence)
        (artifact_root_path / "recursive.log").write_text(recursive_evidence)
        bias_audit = {
            "schema_version": 1,
            "policy": BIAS_AUDIT_POLICY,
            "candidate": "FuturesBtcRegimeRiskBudgetLong",
            "status": "PASS",
            "failures": [],
            "freqtrade_version": "2026.8",
            "timerange": "20220101-20260901",
            "protocol_sha256": protocol,
            "lookahead": {
                "passed": True,
                "has_bias": False,
                "total_signals": 30,
                "biased_entry_signals": 0,
                "biased_exit_signals": 0,
                "artifact_path": "lookahead.csv",
                "artifact_sha256": sha256(lookahead_evidence.encode()).hexdigest(),
            },
            "recursive": {
                "passed": True,
                "startup_candle_count": 1500,
                "maximum_abs_variance_pct": 0.01,
                "indicator_abs_variance_pct": {"ema_200": 0.01},
                "indicator_lookahead_bias": False,
                "artifact_path": "recursive.log",
                "artifact_sha256": sha256(recursive_evidence.encode()).hexdigest(),
            },
        }
        accepted = research_promotion_failures(
            report,
            state,
            current_protocol=protocol,
            bias_audit=bias_audit,
            project_root=artifact_root_path,
            database={"open_trades": 0},
            forward_running=False,
            now=now,
        )
        self.assertEqual(accepted, [])

        report["status"] = "ACCUMULATING"
        rejected = research_promotion_failures(
            report,
            state,
            current_protocol=protocol,
            bias_audit=bias_audit,
            project_root=artifact_root_path,
            database={"open_trades": 0},
            forward_running=True,
            now=now,
        )
        self.assertIn("research report status is not PASS", rejected)
        self.assertIn("forward dry-run must be stopped before live review", rejected)

        (artifact_root_path / "lookahead.csv").write_text("tampered\n")
        tampered = bias_audit_receipt_failures(
            bias_audit,
            expected_protocol=protocol,
            project_root=artifact_root_path,
        )
        self.assertIn("lookahead evidence artifact SHA-256 does not match", tampered)

    def test_live_promotion_rejects_missing_or_stale_bias_audit(self) -> None:
        protocol = {"strategy.py": "abc"}
        self.assertEqual(
            bias_audit_receipt_failures(None, expected_protocol=protocol),
            ["current-protocol bias audit receipt is missing"],
        )
        stale = {
            "schema_version": 1,
            "policy": BIAS_AUDIT_POLICY,
            "candidate": "FuturesBtcRegimeRiskBudgetLong",
            "status": "PASS",
            "failures": [],
            "freqtrade_version": "2026.8",
            "timerange": "20220101-20260901",
            "protocol_sha256": {"strategy.py": "old"},
            "lookahead": {
                "passed": True,
                "has_bias": False,
                "total_signals": 30,
                "biased_entry_signals": 0,
                "biased_exit_signals": 0,
                "artifact_sha256": "a" * 64,
            },
            "recursive": {
                "passed": True,
                "startup_candle_count": 1500,
                "maximum_abs_variance_pct": 0.01,
                "artifact_sha256": "b" * 64,
            },
        }
        failures = bias_audit_receipt_failures(stale, expected_protocol=protocol)
        self.assertIn("bias audit is not bound to the current research protocol", failures)

    def test_combined_research_health_fails_closed(self) -> None:
        self.assertEqual(classify_research_health("RUNNING_HEALTHY", "RUNNING_HEALTHY"), "HEALTHY")
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "COMPLETE",
            ),
            "HEALTHY",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_PUBLISHER_ARCHIVE",
                "COMPLETE",
            ),
            "HEALTHY",
        )
        self.assertEqual(
            classify_research_health("RUNNING_HEALTHY", "RUNNING_WITH_DATA_WARNING"),
            "DEGRADED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                risk_policy_health="FAIL",
            ),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                alpha001_entry_evidence_health="EVIDENCE_DISCONTINUITY",
            ),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            classify_research_health("RUNNING_EQUITY_STALE", "RUNNING_HEALTHY"),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            resolve_alpha013_health(
                {"status": "ACCUMULATING_SIGNAL_LOOKBACK", "forward_report_current": True},
                "RUNNING_HEALTHY",
            ),
            "ACCUMULATING_SIGNAL_LOOKBACK",
        )
        self.assertEqual(
            resolve_alpha013_health(
                {"status": "ACCUMULATING_SIGNAL_LOOKBACK", "forward_report_current": False},
                "RUNNING_HEALTHY",
            ),
            "FORWARD_REPORT_STALE",
        )
        self.assertEqual(
            resolve_alpha013_health(
                {"status": "CAUSAL_AVAILABILITY_VETO", "forward_report_current": True},
                "STOPPED",
            ),
            "CAUSAL_AVAILABILITY_VETO",
        )
        self.assertTrue(alpha013_is_terminal_status("CAUSAL_AVAILABILITY_VETO"))
        self.assertTrue(alpha013_is_terminal_status("DEVELOPMENT_FAIL"))
        self.assertFalse(alpha013_is_terminal_status("ACCUMULATING_SIGNAL_LOOKBACK"))
        self.assertEqual(
            alpha013_terminal_scheduler_state("CAUSAL_AVAILABILITY_VETO"),
            "TERMINAL_CAUSAL_AVAILABILITY_VETO",
        )
        self.assertIsNone(alpha013_terminal_scheduler_state("ACCUMULATING_SIGNAL_LOOKBACK"))
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY", "RUNNING_HEALTHY", "DOWNLOAD_REQUIRED"
            ),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "SCHEDULER_DOWN",
            ),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "ACCUMULATING_WITH_GAPS",
            ),
            "DEGRADED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "STOPPED",
            ),
            "ATTENTION_REQUIRED",
        )
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                "WAITING_FIRST_COMPLETE_UTC_DAY",
                "RUNNING_HEALTHY",
                "DATA_INTEGRITY_FAIL",
            ),
            "ATTENTION_REQUIRED",
        )

    def test_combined_research_health_marks_liquidation_gaps_degraded(self) -> None:
        self.assertEqual(
            classify_research_health(
                "RUNNING_HEALTHY",
                "RUNNING_HEALTHY",
                liquidation_telemetry_health="RUNNING_WITH_DATA_GAPS",
            ),
            "DEGRADED",
        )

    def test_research_health_rejects_mtm_timestamp_or_gap_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "equity.jsonl"
            path.write_text(
                '{"observed_at":"2026-01-01T00:00:00Z"}\n'
                '{"observed_at":"2026-01-01T00:01:00Z"}\n'
                '{"observed_at":',
                encoding="utf-8",
            )
            passing = assess_mtm_continuity(path)
            self.assertEqual(passing["status"], "PASS")
            self.assertEqual(passing["observations"], 2)
            self.assertEqual(passing["maximum_gap_seconds"], 60.0)

            path.write_text(
                '{"observed_at":"2026-01-01T00:00:00Z"}\n'
                '{"observed_at":"2026-01-01T00:02:01Z"}\n',
                encoding="utf-8",
            )
            gapped = assess_mtm_continuity(path)
            self.assertEqual(gapped["status"], "FAIL_CLOSED")
            self.assertEqual(gapped["maximum_gap_seconds"], 121.0)

    def test_alpha001_entry_execution_recovery_gap_is_not_promotion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "research" / "execution" / "raw"
            raw_root.mkdir(parents=True)
            protocol = {"strategy.py": "a" * 64}
            (raw_root / "alpha001-signal-entry-failed.manifest.json").write_text(
                json.dumps(
                    {
                        "protocol_sha256": protocol,
                        "status": "FAILED",
                        "trigger_log_start_offset": 10,
                        "trigger_log_last_offset": 20,
                    }
                ),
                encoding="utf-8",
            )
            (raw_root / "alpha001-signal-entry-active.manifest.json").write_text(
                json.dumps(
                    {
                        "protocol_sha256": protocol,
                        "status": "ACTIVE",
                        "trigger_log_start_offset": 30,
                        "trigger_log_last_offset": 40,
                    }
                ),
                encoding="utf-8",
            )
            report = assess_alpha001_entry_execution_continuity(
                {
                    "protocol_sha256": protocol,
                    "entry_execution_recovery_history": [
                        {"uncovered_interval_replayed": False}
                    ],
                },
                raw_root=raw_root,
            )
        self.assertEqual(report["status"], "EVIDENCE_DISCONTINUITY")
        self.assertFalse(report["promotion_evidence_eligible"])
        self.assertIn(
            "entry_execution_recovery_contains_unreplayed_interval",
            report["failures"],
        )

    def test_active_alpha001_entry_execution_segment_is_not_yet_promotion_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "research" / "execution" / "raw"
            raw_root.mkdir(parents=True)
            protocol = {"strategy.py": "a" * 64}
            (raw_root / "alpha001-signal-entry-active.manifest.json").write_text(
                json.dumps({"protocol_sha256": protocol, "status": "ACTIVE"}),
                encoding="utf-8",
            )
            report = assess_alpha001_entry_execution_continuity(
                {"protocol_sha256": protocol}, raw_root=raw_root
            )
        self.assertEqual(report["status"], "CONTINUOUS")
        self.assertEqual(report["promotion_evidence_readiness"], "ACTIVE_OR_INCOMPLETE")
        self.assertFalse(report["promotion_evidence_eligible"])

    def test_liquidation_campaign_surfaces_failed_chunks_as_data_gaps(self) -> None:
        state = {
            "status": "RUNNING",
            "pid": 123,
            "chunk_seconds": 3600,
            "current_chunk_started_at": datetime.now(timezone.utc).isoformat(),
            "failed_chunks": 1,
        }
        with (
            patch(
                "scripts.manage_liquidation_telemetry_campaign._state",
                return_value=state,
            ),
            patch(
                "scripts.manage_liquidation_telemetry_campaign._running",
                return_value=True,
            ),
            patch(
                "scripts.manage_liquidation_telemetry_campaign._latest_audit",
                return_value=None,
            ),
            patch(
                "scripts.manage_liquidation_telemetry_campaign.inspect_caffeinate_guard",
                return_value={"available": True, "running": True},
            ),
        ):
            payload = liquidation_campaign_status_payload(Path("unused"))
        self.assertEqual(payload["health"], "RUNNING_WITH_DATA_GAPS")

    def test_forward_trade_database_is_reused_only_for_the_same_protocol(self) -> None:
        first_protocol = {"strategy.py": "abc"}
        second_protocol = {"strategy.py": "def"}
        first = protocol_database_path(first_protocol)
        second = protocol_database_path(second_protocol)
        self.assertNotEqual(first, second)
        existing = {
            "protocol_sha256": first_protocol,
            "database": "user_data/existing.sqlite",
        }
        self.assertEqual(
            select_database_path(existing, first_protocol),
            Path(__file__).resolve().parents[1] / "user_data" / "existing.sqlite",
        )
        self.assertEqual(select_database_path(existing, second_protocol), second)

    def test_alpha001_clean_forward_cohort_preserves_safeguards(self) -> None:
        cohort = load_forward_cohort()
        self.assertEqual(cohort["status"], "IMMUTABLE_PREREGISTRATION")
        self.assertFalse(cohort["strategy_or_risk_settings_changed"])
        self.assertEqual(cohort["live_order_authorization"], "CLOSED")

    def test_forward_manifest_binds_active_stream_to_current_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "active.manifest.json"
            protocol = {"strategy.py": "abc"}
            state = {
                "equity_output": "active.jsonl",
                "equity_manifest": "active.manifest.json",
                "started_at": "2025-01-01T00:00:00+00:00",
                "strategy": "Alpha001",
                "database": "database.sqlite",
                "protocol_sha256": protocol,
            }
            manifest = {
                "schema_version": 1,
                "status": "ACTIVE",
                "evidence_kind": "freqtrade_dryrun_mark_to_market",
                "equity_path": "active.jsonl",
                "started_at": state["started_at"],
                "strategy": state["strategy"],
                "database_path": "database.sqlite",
                "protocol_sha256": protocol,
                "source": "authenticated_loopback_freqtrade_net_liquidation",
                "seal": None,
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            healthy = inspect_equity_manifest(
                "active.manifest.json", state=state, current_protocol=protocol, project_root=root
            )
            self.assertTrue(healthy["valid"])

            manifest["protocol_sha256"] = {"strategy.py": "tampered"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            tampered = inspect_equity_manifest(
                "active.manifest.json", state=state, current_protocol=protocol, project_root=root
            )
            self.assertFalse(tampered["valid"])
            self.assertIn("launch_protocol", tampered["reason"])
            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health={"recent_error_count": 0, "heartbeat_age_seconds": 1},
                    started_at=state["started_at"],
                    equity_recorder_running=True,
                    equity_age_seconds=1,
                    equity_manifest_valid=tampered["valid"],
                    now=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
                ),
                "RUNNING_EQUITY_MANIFEST_INVALID",
            )

    def test_forward_database_binding_rejects_cross_run_trade_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "segment.manifest.json"
            manifest.write_text(json.dumps({"database_path": "user_data/current.sqlite"}), encoding="utf-8")
            integrity = {
                "segments": [
                    {
                        "manifest": "segment.manifest.json",
                        "integrity": "PASS",
                        "protocol_current": True,
                    }
                ]
            }
            accepted = audit_database_binding(
                integrity, project_root=root, expected_database="user_data/current.sqlite"
            )
            rejected = audit_database_binding(
                integrity, project_root=root, expected_database="user_data/other.sqlite"
            )
        self.assertEqual(accepted["status"], "PASS")
        self.assertEqual(rejected["status"], "FAIL_CLOSED")
        self.assertIn("database binding mismatch", rejected["segments"][0]["reason"])

    def test_forward_report_recomputes_net_liquidation_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "equity.jsonl"
            row = {
                "equity": 998.5,
                "balance_total_bot": 1000.0,
                "open_trade_net_liquidation_adjustment": -1.5,
                "open_funding_pnl": -0.2,
                "open_trades": 1,
                "equity_method": "balance_total_bot_plus_open_trade_net_liquidation_adjustment",
            }
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(audit_net_liquidation_rows([path])["status"], "PASS")
            row["equity"] = 999.0
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            rejected = audit_net_liquidation_rows([path])
        self.assertEqual(rejected["status"], "FAIL_CLOSED")
        self.assertIn("identity mismatch", rejected["failures"][0]["reason"])

    def test_mtm_recorder_recovers_from_transient_timeout_without_fabricating_rows(self) -> None:
        responses = iter(
            [
                URLError("temporary timeout"),
                {"note": "Simulated balances", "total_bot": 900.0, "starting_capital": 900.0, "trade_count": 0},
            ]
        )

        def fetcher(*_args):
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "equity.jsonl"
            result = collect_equity(
                url="http://127.0.0.1",
                username="u",
                password="p",
                timeout_seconds=1,
                duration_seconds=60,
                interval_seconds=1,
                error_backoff_seconds=0,
                maximum_consecutive_errors=5,
                output=output,
                fetcher=fetcher,
                trade_fetcher=lambda *_args: [],
                sleeper=lambda _seconds: None,
                maximum_observations=1,
            )
            rows = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(result["transient_errors"], 1)
        self.assertEqual(result["observations"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["equity"], 900.0)

    def test_mtm_recorder_marks_open_futures_to_net_liquidation_value(self) -> None:
        valuation = calculate_net_liquidation_equity(
            {
                "total_bot": 1010.0,
                "currencies": [
                    {
                        "currency": "BTC/USDT:USDT",
                        "is_position": True,
                        "est_stake": 110.0,
                    }
                ],
            },
            [
                {
                    "pair": "BTC/USDT:USDT",
                    "stake_amount": 100.0,
                    "profit_abs": 8.8,
                    "funding_fees": -0.1,
                }
            ],
        )
        self.assertAlmostEqual(valuation["equity"], 1008.8)
        self.assertAlmostEqual(valuation["open_trade_net_liquidation_adjustment"], -1.2)
        self.assertAlmostEqual(valuation["open_funding_pnl"], -0.1)

    def test_forward_dryrun_health_requires_fresh_heartbeat_and_no_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "forward.log"
            log_path.write_text(
                "2025-01-01 00:01:30,000 - freqtrade.worker - INFO - Bot heartbeat. PID=123\n",
                encoding="utf-8",
            )
            now = datetime(2025, 1, 1, 0, 2, tzinfo=timezone.utc)
            healthy_log = inspect_bot_log(log_path, now=now, local_timezone=timezone.utc)
            self.assertEqual(healthy_log["heartbeat_age_seconds"], 30.0)
            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health=healthy_log,
                    started_at="2025-01-01T00:00:00+00:00",
                    now=now,
                ),
                "RUNNING_HEALTHY",
            )

            stale_now = datetime(2025, 1, 1, 0, 5, 1, tzinfo=timezone.utc)
            stale_log = inspect_bot_log(log_path, now=stale_now, local_timezone=timezone.utc)
            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health=stale_log,
                    started_at="2025-01-01T00:00:00+00:00",
                    now=stale_now,
                ),
                "RUNNING_HEARTBEAT_STALE",
            )

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("2025-01-01 00:02:00,000 - freqtrade.worker - ERROR - simulated failure\n")
            error_log = inspect_bot_log(log_path, now=now, local_timezone=timezone.utc)
            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health=error_log,
                    started_at="2025-01-01T00:00:00+00:00",
                    now=now,
                ),
                "RUNNING_WITH_ERRORS",
            )

            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health=healthy_log,
                    started_at="2025-01-01T00:00:00+00:00",
                    equity_recorder_running=False,
                    now=now,
                ),
                "RUNNING_EQUITY_RECORDER_MISSING",
            )
            self.assertEqual(
                forward_operational_health(
                    state_status="RUNNING",
                    running=True,
                    protocol_current=True,
                    log_health=healthy_log,
                    started_at="2025-01-01T00:00:00+00:00",
                    equity_recorder_running=True,
                    equity_age_seconds=181,
                    now=now,
                ),
                "RUNNING_EQUITY_STALE",
            )

            foreign_pid_log = inspect_bot_log(
                log_path,
                now=now,
                local_timezone=timezone.utc,
                expected_pid=999,
            )
            self.assertIsNone(foreign_pid_log["last_heartbeat_at"])
            self.assertIsNone(foreign_pid_log["heartbeat_pid"])

            scoped_path = Path(directory) / "scoped.log"
            scoped_path.write_text(
                "2025-01-01 00:00:00,000 - worker - ERROR - old failure\n"
                "2025-01-01 00:01:30,000 - worker - INFO - Bot heartbeat. PID=123\n",
                encoding="utf-8",
            )
            scoped = inspect_bot_log(
                scoped_path,
                now=now,
                local_timezone=timezone.utc,
                expected_pid=123,
                since=datetime(2025, 1, 1, 0, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(scoped["recent_error_count"], 0)
            self.assertEqual(scoped["heartbeat_pid"], 123)

    def test_forward_dryrun_running_boot_requires_explicit_dryrun_gate(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        command = [
            sys.executable,
            str(project_root / "scripts" / "live_preflight.py"),
            "--base",
            str(project_root / "user_data" / "config.futures.json"),
            "--overlay",
            str(project_root / "user_data" / "config.futures.forward-dryrun.json"),
        ]
        ordinary = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(ordinary.returncode, 2)
        self.assertIn("FAIL: safe_boot_state", ordinary.stdout)

        explicitly_dry = subprocess.run(
            [*command, "--allow-running-dry-run"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(explicitly_dry.returncode, 0)
        self.assertIn("PASS: structural safeguards", explicitly_dry.stdout)

        unsafe_overlay = json.loads(
            (project_root / "user_data" / "config.futures.forward-dryrun.json").read_text(encoding="utf-8")
        )
        unsafe_overlay["api_server"]["listen_ip_address"] = "0.0.0.0"
        with tempfile.TemporaryDirectory() as directory:
            unsafe_path = Path(directory) / "unsafe.json"
            unsafe_path.write_text(json.dumps(unsafe_overlay), encoding="utf-8")
            unsafe = subprocess.run(
                [
                    sys.executable,
                    str(project_root / "scripts" / "live_preflight.py"),
                    "--base",
                    str(project_root / "user_data" / "config.futures.json"),
                    "--overlay",
                    str(unsafe_path),
                    "--allow-running-dry-run",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn("automated dry-run API must bind only to loopback", unsafe.stdout)

    def test_daily_equity_boundary_is_causal_and_rejects_stale_input(self) -> None:
        observations = [
            {"observed_at": "2025-01-01T23:59:15Z", "equity": 1000},
            {"observed_at": "2025-01-02T23:59:30Z", "equity": 1010},
            {"observed_at": "2025-01-03T00:00:20Z", "equity": 9999},
        ]
        rows = select_causal_daily_boundaries(observations, maximum_age_seconds=60)
        self.assertEqual([item["equity"] for item in rows], [1000.0, 1010.0])
        self.assertEqual(rows[-1]["timestamp"], "2025-01-03T00:00:00Z")
        with self.assertRaisesRegex(ValueError, "stale equity observation"):
            select_causal_daily_boundaries(observations, maximum_age_seconds=10)

    def test_shadow_maker_respects_queue_and_taker_side(self) -> None:
        trades = [
            {"a": 1, "T": 1, "p": "100", "q": "1", "m": False},
            {"a": 2, "T": 2, "p": "100", "q": "1", "m": True},
            {"a": 3, "T": 3, "p": "100", "q": "2", "m": True},
        ]
        result = estimate_shadow_fill(
            side="buy", limit_price=100, order_qty=2, queue_ahead_qty=2, aggregate_trades=trades
        )
        self.assertAlmostEqual(result["filled_qty"], 1)
        self.assertEqual(result["eligible_trade_count"], 2)
        self.assertEqual(result["first_fill_time_ms"], 3)
        self.assertIsNone(result["full_fill_time_ms"])

    def test_shadow_maker_trade_through_fills_order(self) -> None:
        result = estimate_shadow_fill(
            side="sell",
            limit_price=100,
            order_qty=3,
            queue_ahead_qty=50,
            aggregate_trades=[{"a": 1, "T": 1, "p": "101", "q": "0.1", "m": False}],
        )
        self.assertAlmostEqual(result["filled_qty"], 3)
        self.assertEqual(result["full_fill_time_ms"], 1)


class DataValidationTests(unittest.TestCase):
    def test_alpha011_monthly_funding_reconciliation_detects_mismatch(self) -> None:
        csv_payload = (
            "calc_time,funding_interval_hours,last_funding_rate\n"
            "1788220800005,8,0.00008482\n"
            "1788249600000,8,0.00010000\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "BTCUSDT-fundingRate-2026-09.zip"
            with ZipFile(path, "w") as archive:
                archive.writestr("BTCUSDT-fundingRate-2026-09.csv", csv_payload)
            digest = sha256(path.read_bytes()).hexdigest()
            monthly = inspect_monthly_archive(
                path,
                expected_digest=digest,
                expected_csv_name="BTCUSDT-fundingRate-2026-09.csv",
                month="2026-09",
            )
        rest = [
            {
                "fundingTime": 1788220800005,
                "fundingRate": "0.00008482",
                "rateType": "Regular",
            },
            {
                "fundingTime": 1788249600000,
                "fundingRate": "0.00010000",
                "rateType": "Regular",
            },
            {
                "fundingTime": 1788249600000,
                "fundingRate": "0.00001000",
                "rateType": "Special",
            },
        ]
        self.assertEqual(compare_funding_rows(rest, monthly)["status"], "PASS")
        rest[1]["fundingRate"] = "0.00020000"
        self.assertEqual(compare_funding_rows(rest, monthly)["status"], "FAIL")
        self.assertEqual(
            completed_months_in_files(
                [
                    {"date": "2026-10-09"},
                    {"date": "2026-11-30"},
                    {"date": "2026-12-09"},
                ]
            ),
            ["2026-10", "2026-11"],
        )

    def test_alpha011_funding_seals_types_and_aggregates_same_time(self) -> None:
        day = datetime(2026, 10, 9).date()
        rows = [
            {
                "symbol": "BTCUSDT",
                "fundingTime": 1791504000000,
                "fundingRate": "0.00010",
                "markPrice": "90000.0",
                "rateType": "Regular",
            },
            {
                "symbol": "BTCUSDT",
                "fundingTime": 1791504000000,
                "fundingRate": "0.00002",
                "markPrice": "90000.0",
                "rateType": "Special",
            },
        ]
        canonical, normalized, metadata = validate_funding_payload(
            json.dumps(rows).encode(), symbol="BTCUSDT", day=day
        )
        self.assertEqual(metadata["funding_events"], 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "funding.json"
            path.write_bytes(canonical)
            events = load_alpha011_funding(
                [{"funding_rows_data": normalized}], end_ms=1791504000000
            )
        self.assertEqual(len(events), 2)
        self.assertAlmostEqual(sum(event[1] for event in events), 0.00012)
        self.assertTrue(all(event[2] == 90000.0 for event in events))

        duplicate = rows + [dict(rows[0])]
        with self.assertRaisesRegex(Exception, "duplicate time and rateType"):
            validate_funding_payload(json.dumps(duplicate).encode(), symbol="BTCUSDT", day=day)

    def test_alpha011_aggtrades_archive_requires_continuous_ids_and_utc_day(self) -> None:
        header = (
            "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
        )
        rows = (
            "10,100.0,1.5,20,21,1798761600000,False\n"
            "11,100.1,0.5,22,22,1798761600100,True\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "BTCUSDT-aggTrades-2027-01-01.zip"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("BTCUSDT-aggTrades-2027-01-01.csv", header + rows)
            digest = sha256(archive_path.read_bytes()).hexdigest()
            result = inspect_aggtrades_archive(
                archive_path,
                expected_digest=digest,
                expected_csv_name="BTCUSDT-aggTrades-2027-01-01.csv",
                day=datetime(2027, 1, 1).date(),
            )
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["first_agg_trade_id"], 10)

            bad_path = Path(directory) / "bad.zip"
            with ZipFile(bad_path, "w") as archive:
                archive.writestr(
                    "BTCUSDT-aggTrades-2027-01-01.csv",
                    header + rows.replace("11,100.1", "12,100.1"),
                )
            bad_digest = sha256(bad_path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(Exception, "not exactly continuous"):
                inspect_aggtrades_archive(
                    bad_path,
                    expected_digest=bad_digest,
                    expected_csv_name="BTCUSDT-aggTrades-2027-01-01.csv",
                    day=datetime(2027, 1, 1).date(),
                )

    def test_binance_metrics_archive_requires_publisher_checksum_and_schema(self) -> None:
        name = "BTCUSDT-metrics-2024-01-01.zip"
        csv_name = name.removesuffix(".zip") + ".csv"
        header = (
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
        )
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(csv_name, header)
        payload = buffer.getvalue()
        digest = sha256(payload).hexdigest()
        self.assertEqual(parse_checksum(f"{digest}  {name}\n".encode(), expected_name=name), digest)
        verified = verify_archive(payload, expected_digest=digest, expected_csv_name=csv_name)
        self.assertEqual(verified["sha256"], digest)
        with self.assertRaisesRegex(Exception, "SHA-256"):
            verify_archive(payload, expected_digest="0" * 64, expected_csv_name=csv_name)

    def test_detects_duplicates_gaps_and_bad_ohlcv(self) -> None:
        report = validate_ohlcv_rows(
            [
                {"timestamp": 0, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
                {"timestamp": 0, "open": 10, "high": 9, "low": 8, "close": 10, "volume": 0},
                {"timestamp": 2_000, "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1},
            ],
            interval_ms=1_000,
        )
        self.assertFalse(report.passed)
        self.assertIn("duplicate_timestamp", {issue.code for issue in report.issues})
        self.assertIn("zero_volume", {issue.code for issue in report.issues})


class OrderBookTests(unittest.TestCase):
    def test_campaign_chunk_watchdog_terminates_stuck_collector(self) -> None:
        self.assertEqual(chunk_wall_timeout_seconds(3600), 7200)
        result = run_chunk_with_watchdog(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=Path(__file__).resolve().parents[1],
            wall_timeout_seconds=0.1,
            poll_interval_seconds=0.01,
            terminate_grace_seconds=0.2,
        )
        self.assertTrue(result["watchdog_timed_out"])
        self.assertEqual(result["returncode"], 124)

    def test_campaign_coverage_budget_detects_unreachable_collection_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "campaign.jsonl"
            log.write_text(
                json.dumps({"event": "chunk_complete", "duration_seconds": 3600}) + "\n"
                + json.dumps({"event": "chunk_failed", "duration_seconds": 3600}) + "\n",
                encoding="utf-8",
            )
            payload = {
                "campaign_log": "campaign.jsonl",
                "started_at": "2025-01-01T00:00:00+00:00",
                "hours": 10,
            }
            budget = campaign_coverage_budget(
                payload,
                project_root=root,
                now=datetime(2025, 1, 1, 9, tzinfo=timezone.utc),
                required_complete_days=1,
            )
        self.assertFalse(budget["target_still_reachable"])
        self.assertEqual(budget["sealed_success_seconds"], 3600)
        self.assertEqual(budget["excluded_failed_chunk_seconds"], 3600)

    def test_campaign_status_cross_checks_collector_and_staleness(self) -> None:
        now = datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc)
        payload = {
            "status": "RUNNING",
            "chunk_seconds": 3600,
            "current_chunk_started_at": "2025-01-01T00:30:00+00:00",
        }
        healthy = derive_status_health(
            payload,
            supervisor_running=True,
            collectors=[{"pid": 123, "elapsed": "30:00", "command": "collect_microstructure.py"}],
            now=now,
        )
        self.assertEqual(healthy["health"], "RUNNING_HEALTHY")
        self.assertTrue(healthy["collector_running"])

        stale_payload = dict(payload, current_chunk_started_at="2024-12-31T23:45:00+00:00")
        stale = derive_status_health(
            stale_payload,
            supervisor_running=True,
            collectors=[{"pid": 123, "elapsed": "01:15:00", "command": "collect_microstructure.py"}],
            now=now,
        )
        self.assertEqual(stale["health"], "RUNNING_CHUNK_STALE")

        active_extended = derive_status_health(
            stale_payload,
            supervisor_running=True,
            collectors=[{"pid": 123, "elapsed": "01:15:00", "command": "collect_microstructure.py"}],
            collector_output_age_seconds=5,
            now=now,
        )
        self.assertEqual(active_extended["health"], "RUNNING_ACTIVE_EXTENDED")

        missing = derive_status_health(
            payload,
            supervisor_running=False,
            collectors=[],
            now=now,
        )
        self.assertEqual(missing["health"], "SUPERVISOR_MISSING")

        audit_failure = derive_status_health(
            payload,
            supervisor_running=True,
            collectors=[{"pid": 123, "elapsed": "30:00", "command": "collect_microstructure.py"}],
            latest_run_audit={"status": "FAIL"},
            now=now,
        )
        self.assertEqual(audit_failure["health"], "LATEST_CHUNK_AUDIT_FAILED")

    def test_microstructure_run_audit_separates_critical_and_quality_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "20250101T000000Z-test"
            run_dir.mkdir()
            files = []
            for name in ("depth.jsonl.gz", "book_features.jsonl.gz", "agg_trades.jsonl.gz"):
                path = run_dir / name
                with gzip.open(path, "wt", encoding="utf-8") as handle:
                    handle.write("{}\n")
                files.append({"path": name, "bytes": path.stat().st_size, "sha256": sha256(path.read_bytes()).hexdigest()})
            manifest = {
                "schema_version": 1,
                "run_id": run_dir.name,
                "symbol": "BTCUSDT",
                "depth_levels": 5,
                "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                "protocol_sha256": protocol_digests(Path(__file__).resolve().parents[1]),
                "result": {
                    "requested_duration_seconds": 300,
                    "timing": {"active_wall_seconds": 300.0, "healthy": True},
                    "depth": {
                        "connections": 1,
                        "reconnects": 0,
                        "last_error": None,
                        "last_sequence_error": None,
                        "sequence_resyncs": 1,
                        "stale_depth_events": 0,
                        "feature_rows": 10,
                    },
                    "trades": {"connections": 1, "reconnects": 0, "last_error": None, "aggregate_trades": 10},
                },
                "files": files,
            }
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(microstructure_run_audit(run_dir)["status"], "PASS")

            manifest["result"]["depth"]["reconnects"] = 1
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(microstructure_run_audit(run_dir)["status"], "QUALITY_WARNING")

            manifest["result"]["depth"]["reconnects"] = 0
            manifest["result"]["timing"] = {"active_wall_seconds": 900.0, "healthy": False}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            timing_audit = microstructure_run_audit(run_dir)
            self.assertEqual(timing_audit["status"], "QUALITY_WARNING")
            self.assertTrue(timing_audit["checks"]["manifest_integrity"])
            self.assertFalse(timing_audit["checks"]["timing_healthy"])

            manifest["protocol_sha256"] = {key: "0" * 64 for key in manifest["protocol_sha256"]}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(microstructure_run_audit(run_dir)["status"], "FAIL")

    def test_campaign_supervisor_rejects_duplicate_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_path = root / ".campaign.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve().parents[1] / "scripts" / "run_microstructure_campaign.py"),
                        "--hours",
                        "1",
                        "--chunk-seconds",
                        "300",
                        "--output-root",
                        str(root),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertEqual(completed.returncode, 4)
            self.assertIn("already holds", completed.stderr)

    def test_snapshot_bridge_updates_absolute_levels_and_features(self) -> None:
        book = LocalOrderBook()
        book.load_snapshot({"lastUpdateId": 100, "bids": [["99", "2"], ["98", "3"]], "asks": [["101", "1"], ["102", "4"]]})
        self.assertTrue(book.apply({"U": 99, "u": 101, "pu": 98, "b": [["99", "5"], ["98", "0"]], "a": [["101", "2"]]}))
        features = book.features(2)
        self.assertEqual(features["best_bid_qty"], 5.0)
        self.assertNotIn(98.0, book.bids)
        self.assertGreater(features["touch_imbalance"], 0)
        self.assertGreater(features["microprice_edge_bps"], 0)

    def test_sequence_gap_fails_closed(self) -> None:
        book = LocalOrderBook()
        book.load_snapshot({"lastUpdateId": 10, "bids": [["99", "1"]], "asks": [["101", "1"]]})
        book.apply({"U": 10, "u": 11, "pu": 9, "b": [], "a": []})
        with self.assertRaises(OrderBookSequenceError):
            book.apply({"U": 13, "u": 14, "pu": 12, "b": [], "a": []})

    def test_microstructure_signal_uses_past_flow_and_queue_trades(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            day_ms = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            features = []
            for offset in range(5_000, 71_000, 1_000):
                mid = 101.0 if offset >= 11_000 else 100.0
                features.append(
                    {
                        "transaction_time_ms": day_ms + offset,
                        "last_update_id": offset,
                        "depth_imbalance": 0.8,
                        "best_bid": mid - 0.1,
                        "best_bid_qty": 1.0,
                        "best_ask": mid + 0.1,
                        "best_ask_qty": 1.0,
                        "mid": mid,
                    }
                )
            trades = [
                {"a": 1, "T": day_ms + 4_900, "p": "100.1", "q": "10", "m": False},
                {"a": 2, "T": day_ms + 6_000, "p": "99.9", "q": "2", "m": True},
            ]
            with gzip.open(run_dir / "book_features.jsonl.gz", "wt", encoding="utf-8") as handle:
                for row in features:
                    handle.write(json.dumps(row) + "\n")
            with gzip.open(run_dir / "agg_trades.jsonl.gz", "wt", encoding="utf-8") as handle:
                for row in trades:
                    handle.write(json.dumps({"payload": row}) + "\n")
            signals = _run_signals(run_dir, {"2025-01-01": "DEVELOPMENT"})
            self.assertEqual(len(signals), 1)
            self.assertTrue(signals[0]["fully_filled"])
            self.assertGreater(signals[0]["net_markout"]["5"], 0)

    def test_microstructure_decision_waits_for_local_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            day = datetime(2025, 1, 1, tzinfo=timezone.utc)
            day_ms = int(day.timestamp() * 1000)
            features = [
                {
                    "transaction_time_ms": day_ms + 900,
                    "received_at": (day + timedelta(milliseconds=1_100)).isoformat(),
                    "last_update_id": 1,
                    "depth_imbalance": 0.8,
                    "best_bid": 99.9,
                    "best_bid_qty": 1.0,
                    "best_ask": 100.1,
                    "best_ask_qty": 1.0,
                    "mid": 100.0,
                }
            ]
            trades = [
                {
                    "received_at": (day + timedelta(milliseconds=1_200)).isoformat(),
                    "payload": {"a": 1, "T": day_ms + 800, "p": "100.1", "q": "10", "m": False},
                }
            ]
            with gzip.open(run_dir / "book_features.jsonl.gz", "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(features[0]) + "\n")
            with gzip.open(run_dir / "agg_trades.jsonl.gz", "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(trades[0]) + "\n")
            loaded_features, loaded_trades = _load_run(run_dir)
            self.assertEqual(loaded_features[0]["_available_time_ms"], day_ms + 1_100)
            self.assertEqual(loaded_trades[0]["T"], day_ms + 1_200)

    def test_microstructure_streaming_preserves_cross_chunk_markout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "run-1", root / "run-2"
            first.mkdir()
            second.mkdir()
            day_ms = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            for run_dir, offsets in ((first, range(5_000, 11_000, 1_000)), (second, range(11_000, 72_000, 1_000))):
                with gzip.open(run_dir / "book_features.jsonl.gz", "wt", encoding="utf-8") as handle:
                    for offset in offsets:
                        mid = 101.0 if offset >= 11_000 else 100.0
                        handle.write(
                            json.dumps(
                                {
                                    "transaction_time_ms": day_ms + offset,
                                    "last_update_id": offset,
                                    "depth_imbalance": 0.8,
                                    "best_bid": mid - 0.1,
                                    "best_bid_qty": 1.0,
                                    "best_ask": mid + 0.1,
                                    "best_ask_qty": 1.0,
                                    "mid": mid,
                                }
                            )
                            + "\n"
                        )
                with gzip.open(run_dir / "agg_trades.jsonl.gz", "wt", encoding="utf-8") as handle:
                    if run_dir == first:
                        for trade in (
                            {"a": 1, "T": day_ms + 4_900, "p": "100.1", "q": "10", "m": False},
                            {"a": 2, "T": day_ms + 6_000, "p": "99.9", "q": "2", "m": True},
                        ):
                            handle.write(json.dumps({"payload": trade}) + "\n")
            signals = _stream_signals([first, second], {"2025-01-01": "DEVELOPMENT"})
            self.assertEqual(len(signals), 1)
            self.assertGreater(signals[0]["net_markout"]["5"], 0)

    def test_microstructure_markout_gap_uses_local_availability_clock(self) -> None:
        features = [
            {
                "_available_time_ms": 10_004,
                # Deliberately far from the local clock to catch clock mixing.
                "transaction_time_ms": 1,
            }
        ]
        self.assertIs(
            _future_feature(features, [10_004], 10_000),
            features[0],
        )
        self.assertIsNone(_future_feature(features, [10_004], 4_000))

    def test_microstructure_manifest_tampering_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            files = []
            for name in ("depth.jsonl.gz", "book_features.jsonl.gz", "agg_trades.jsonl.gz"):
                path = run_dir / name
                path.write_bytes(b"original")
                files.append({"path": name, "bytes": 8, "sha256": sha256(b"original").hexdigest()})
            (run_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "run_id": "test",
                        "symbol": "BTCUSDT",
                        "depth_levels": 5,
                        "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                        "files": files,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_verify_run(run_dir)["files_verified"], 3)
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            manifest["protocol_sha256"] = {"scripts/collect_microstructure.py": "bad"}
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "protocol fingerprint"):
                _verify_run(run_dir)
            manifest.pop("protocol_sha256")
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (run_dir / "depth.jsonl.gz").write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "integrity failure"):
                _verify_run(run_dir)

    def test_campaign_preflight_requires_a_healthy_five_minute_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            files = []
            for name in ("depth.jsonl.gz", "book_features.jsonl.gz", "agg_trades.jsonl.gz"):
                path = run_dir / name
                path.write_bytes(b"sample")
                files.append({"path": name, "bytes": 6, "sha256": sha256(b"sample").hexdigest()})
            manifest = {
                "schema_version": 1,
                "run_id": "run",
                "symbol": "BTCUSDT",
                "depth_levels": 5,
                "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                "result": {
                    "started_at": "2025-01-01T00:00:00+00:00",
                    "ended_at": "2025-01-01T00:05:00+00:00",
                    "requested_duration_seconds": 300,
                    "depth": {
                        "connections": 1,
                        "reconnects": 0,
                        "sequence_resyncs": 1,
                        "last_error": None,
                        "last_sequence_error": None,
                        "feature_rows": 100,
                    },
                    "trades": {
                        "connections": 1,
                        "reconnects": 0,
                        "last_error": None,
                        "aggregate_trades": 100,
                    },
                },
                "protocol_sha256": protocol_digests(Path(__file__).resolve().parents[1]),
                "files": files,
            }
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = microstructure_campaign_preflight(
                root,
                campaign_hours=1,
                minimum_free_after_gib=0,
                disk_path=root,
            )
            self.assertEqual(report["status"], "READY")
            self.assertTrue(report["checks"]["healthy_current_protocol_five_minute_sample"])
            manifest["result"]["depth"]["sequence_resyncs"] = 2
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = microstructure_campaign_preflight(
                root,
                campaign_hours=1,
                minimum_free_after_gib=0,
                disk_path=root,
            )
            self.assertEqual(report["status"], "NOT_READY")
            manifest["result"]["depth"]["sequence_resyncs"] = 1
            manifest["result"]["ended_at"] = "2025-01-01T00:20:00+00:00"
            (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = microstructure_campaign_preflight(
                root,
                campaign_hours=1,
                minimum_free_after_gib=0,
                disk_path=root,
            )
            self.assertFalse(report["sample_runs"][0]["timing_healthy"])
            self.assertEqual(report["status"], "NOT_READY")

    def test_microstructure_analysis_excludes_bad_timing_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for run_name, ended_at in (("healthy", "2025-01-01T00:05:00+00:00"), ("clock-jump", "2025-01-01T00:20:00+00:00")):
                run_dir = root / run_name
                run_dir.mkdir()
                files = []
                for name in ("depth.jsonl.gz", "book_features.jsonl.gz", "agg_trades.jsonl.gz"):
                    path = run_dir / name
                    with gzip.open(path, "wt", encoding="utf-8"):
                        pass
                    payload = path.read_bytes()
                    files.append({"path": name, "bytes": len(payload), "sha256": sha256(payload).hexdigest()})
                manifest = {
                    "schema_version": 1,
                    "run_id": run_name,
                    "symbol": "BTCUSDT",
                    "depth_levels": 5,
                    "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                    "result": {
                        "started_at": "2025-01-01T00:00:00+00:00",
                        "ended_at": ended_at,
                        "requested_duration_seconds": 300,
                    },
                    "files": files,
                }
                (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            report = analyze_microstructure(root)
            self.assertEqual(report["runs"], 2)
            self.assertEqual(report["eligible_timing_runs"], 1)
            self.assertEqual(report["excluded_timing_runs"], 1)
            self.assertEqual(report["data_integrity"]["status"], "PASS_WITH_TIMING_EXCLUSIONS")

    def test_microstructure_complete_days_require_current_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            run_dir.mkdir()
            manifest = {
                "run_id": "run-current",
                "result": {
                    "started_at": "2025-01-01T00:00:00+00:00",
                    "ended_at": "2025-01-01T00:05:00+00:00",
                },
                "protocol_sha256": protocol_digests(
                    Path(__file__).resolve().parents[1]
                ),
            }
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            report = {
                "coverage": [{"day": "2025-01-01", "complete": True}],
                "data_integrity": {
                    "verified_runs": [
                        {"run_id": "run-current", "timing": {"healthy": True}}
                    ]
                },
            }
            audit = audit_complete_day_protocols(
                root,
                report,
                project_root=Path(__file__).resolve().parents[1],
            )
            self.assertEqual(audit["status"], "PASS")
            manifest["protocol_sha256"] = {
                key: "0" * 64 for key in manifest["protocol_sha256"]
            }
            (run_dir / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "obsolete protocol runs"):
                audit_complete_day_protocols(
                    root,
                    report,
                    project_root=Path(__file__).resolve().parents[1],
                )

    def test_research_health_replays_alpha007_complete_day_protocol_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            raw_root = temporary / "raw"
            run_dir = raw_root / "run"
            run_dir.mkdir(parents=True)
            project_root = Path(__file__).resolve().parents[1]
            manifest = {
                "run_id": "run-current",
                "result": {
                    "started_at": "2025-01-01T00:00:00+00:00",
                    "ended_at": "2025-01-01T00:05:00+00:00",
                },
                "protocol_sha256": protocol_digests(project_root),
            }
            manifest_path = run_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            report = {
                "complete_days": 1,
                "coverage": [{"day": "2025-01-01", "complete": True}],
                "data_integrity": {
                    "verified_runs": [
                        {"run_id": "run-current", "timing": {"healthy": True}}
                    ]
                },
            }
            report["complete_day_protocol_audit"] = audit_complete_day_protocols(
                raw_root,
                report,
                project_root=project_root,
            )
            report_path = temporary / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            healthy = alpha007_complete_day_protocol_health(
                report_path=report_path,
                raw_root=raw_root,
                project_root=project_root,
            )
            self.assertEqual(healthy["health"], "PASS")

            hidden_dir = raw_root / "hidden"
            hidden_dir.mkdir()
            hidden_manifest = dict(manifest)
            hidden_manifest["run_id"] = "run-hidden"
            hidden_manifest["result"] = {
                "started_at": "2025-01-01T00:01:00+00:00",
                "ended_at": "2025-01-01T00:02:00+00:00",
            }
            (hidden_dir / "manifest.json").write_text(
                json.dumps(hidden_manifest), encoding="utf-8"
            )
            omitted = alpha007_complete_day_protocol_health(
                report_path=report_path,
                raw_root=raw_root,
                project_root=project_root,
            )
            self.assertEqual(omitted["health"], "FAIL")
            self.assertIn(
                "source_manifest_inventory_mismatch_through_report_cutoff",
                omitted["failures"],
            )
            (hidden_dir / "manifest.json").unlink()

            manifest["protocol_sha256"] = {
                key: "0" * 64 for key in manifest["protocol_sha256"]
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            failed = alpha007_complete_day_protocol_health(
                report_path=report_path,
                raw_root=raw_root,
                project_root=project_root,
            )
            self.assertEqual(failed["health"], "FAIL")
            self.assertTrue(failed["failures"])


class UniverseTests(unittest.TestCase):
    @staticmethod
    def _forward_universe_policy(start: str = "2025-01-01") -> dict:
        return {
            "eligible_start_date": start,
            "sampling": {
                "start_utc": "00:30:00",
                "window_seconds": 900,
                "depth_pairs": 50,
                "maximum_collection_duration_seconds": 900,
                "detailed_candle_limit": 501,
                "minimum_detailed_coverage_ratio": 0.9,
            },
            "readiness": {
                "required_consecutive_days": 2,
                "maximum_latest_age_days": 1,
            },
        }

    def test_forward_universe_collection_requires_fixed_utc_window(self) -> None:
        policy = self._forward_universe_policy()
        validate_forward_collection_start(
            datetime(2025, 1, 1, 0, 35, tzinfo=timezone.utc),
            scheduled_date=datetime(2025, 1, 1).date(),
            policy=policy,
        )
        with self.assertRaisesRegex(ValueError, "must start"):
            validate_forward_collection_start(
                datetime(2025, 1, 1, 1, 0, tzinfo=timezone.utc),
                scheduled_date=datetime(2025, 1, 1).date(),
                policy=policy,
            )

    def test_forward_universe_remote_candles_exclude_incomplete_hour(self) -> None:
        observed = datetime(2025, 1, 22, 0, 30, tzinfo=timezone.utc)
        current_open_ms = int(observed.replace(minute=0).timestamp() * 1000)

        class FakeExchange:
            def fetch_ohlcv(self, pair, timeframe, limit):
                self.request = (pair, timeframe, limit)
                return [
                    [current_open_ms - (500 - index) * 3_600_000, 1, 2, 0.5, 1.5, 10]
                    for index in range(501)
                ]

        exchange = FakeExchange()
        count, continuous, fresh, latest = _remote_candle_status(
            exchange,
            "BTC/USDT:USDT",
            observed_at=observed,
            limit=501,
        )
        self.assertEqual(exchange.request, ("BTC/USDT:USDT", "1h", 501))
        self.assertEqual(count, 500)
        self.assertTrue(continuous)
        self.assertTrue(fresh)
        self.assertEqual(latest, "2025-01-21T23:00:00+00:00")

    def test_forward_universe_seal_chain_detects_tampering(self) -> None:
        protocol = {"collector.py": "a" * 64}
        policy = self._forward_universe_policy()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = []
            for day in (1, 2):
                observed = datetime(2025, 1, day, 0, 35, tzinfo=timezone.utc)
                snapshot = write_snapshot_batch(
                    directory=root,
                    observed_at=observed,
                    instruments=[
                        {
                            "symbol": "BTC/USDT:USDT",
                            "timestamp": observed.isoformat(),
                            "eligible": True,
                            "depth_collected": True,
                        }
                    ],
                    metadata={
                        "all_usdt_perpetual": True,
                        "depth_pairs": 50,
                        "evidence_role": "daily_forward_universe",
                        "detailed_coverage": {
                            "requested": 50,
                            "ratios": {
                                "depth": 1.0,
                                "candles": 1.0,
                                "funding": 1.0,
                                "open_interest": 1.0,
                            },
                        },
                    },
                )
                seal_snapshot_batch(
                    snapshot,
                    protocol_sha256=protocol,
                    evidence_role="daily_forward_universe",
                    scheduled_date=observed.date().isoformat(),
                    collection_started_at=observed - timedelta(minutes=4),
                    collection_completed_at=observed + timedelta(minutes=1),
                )
                snapshots.append(snapshot)
            ready = assess_forward_universe_history(
                root,
                expected_protocol=protocol,
                policy=policy,
                as_of=datetime(2025, 1, 2, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(ready["status"], "READY")
            self.assertEqual(ready["consecutive_days_ending_latest"], 2)

            diagnostic_observed = datetime(2025, 1, 2, 12, tzinfo=timezone.utc)
            diagnostic = write_snapshot_batch(
                directory=root,
                observed_at=diagnostic_observed,
                instruments=[
                    {
                        "symbol": "ETH/USDT:USDT",
                        "timestamp": diagnostic_observed.isoformat(),
                        "eligible": False,
                        "depth_collected": True,
                    }
                ],
                metadata={"all_usdt_perpetual": True, "depth_pairs": 50},
            )
            seal_snapshot_batch(
                diagnostic,
                protocol_sha256=protocol,
                evidence_role="diagnostic",
                scheduled_date=None,
                collection_started_at=diagnostic_observed,
                collection_completed_at=diagnostic_observed + timedelta(minutes=1),
            )
            with_diagnostic = assess_forward_universe_history(
                root,
                expected_protocol=protocol,
                policy=policy,
                as_of=datetime(2025, 1, 2, 13, tzinfo=timezone.utc),
            )
            self.assertEqual(with_diagnostic["status"], "READY")
            self.assertEqual(with_diagnostic["diagnostic_days"], ["2025-01-02"])

            payload = json.loads(snapshots[-1].read_text(encoding="utf-8"))
            payload["instruments"][0]["eligible"] = False
            snapshots[-1].write_text(json.dumps(payload), encoding="utf-8")
            tampered = assess_forward_universe_history(
                root,
                expected_protocol=protocol,
                policy=policy,
                as_of=datetime(2025, 1, 2, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(tampered["status"], "DATA_INTEGRITY_FAIL")
            self.assertTrue(tampered["invalid_snapshots"])

    def test_alpha015_activation_lock_binds_prior_day_manifest_calendar(self) -> None:
        protocol = {"collector.py": "a" * 64}
        policy = self._forward_universe_policy()
        policy["readiness"]["required_consecutive_days"] = 180
        candidate_policy = {
            "candidate": "CANDIDATE_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            "activation_requirements": {
                "point_in_time_universe_consecutive_days": 180,
            },
        }
        amendment = {
            "candidate": candidate_policy["candidate"],
            "status": "PRE_DATA_FAIL_CLOSED_AMENDMENT",
            "evidence_state": {"real_return_archives_downloaded": False},
            "clarification": {
                "universe_snapshot_days": 180,
                "evaluation_days": 180,
                "development_days": 120,
                "locked_validation_days": 60,
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2025, 1, 1)
            for offset in range(180):
                day = start + timedelta(days=offset)
                observed = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=35)
                instruments = [
                    {
                        "symbol": f"S{number:02d}USDT",
                        "timestamp": observed.isoformat(),
                        "eligible": True,
                        "depth_collected": number < 30,
                    }
                    for number in range(30)
                ]
                snapshot = write_snapshot_batch(
                    directory=root,
                    observed_at=observed,
                    instruments=instruments,
                    metadata={
                        "all_usdt_perpetual": True,
                        "depth_pairs": 50,
                        "evidence_role": "daily_forward_universe",
                        "detailed_coverage": {
                            "requested": 50,
                            "ratios": {
                                "depth": 1.0,
                                "candles": 1.0,
                                "funding": 1.0,
                                "open_interest": 1.0,
                            },
                        },
                    },
                )
                seal_snapshot_batch(
                    snapshot,
                    protocol_sha256=protocol,
                    evidence_role="daily_forward_universe",
                    scheduled_date=day.isoformat(),
                    collection_started_at=observed - timedelta(minutes=4),
                    collection_completed_at=observed + timedelta(minutes=1),
                )
            final_day = start + timedelta(days=179)
            lock = build_alpha015_activation_lock(
                snapshot_directory=root,
                universe_policy=policy,
                expected_protocol=protocol,
                candidate_policy=candidate_policy,
                amendment=amendment,
                as_of=datetime.combine(final_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1),
            )
            self.assertEqual(lock["status"], "LOCKED_FOR_PREREGISTRATION_ONLY")
            self.assertEqual(lock["formation_snapshot_count"], 180)
            self.assertEqual(len(lock["development"]), 120)
            self.assertEqual(len(lock["locked_validation"]), 60)
            self.assertEqual(lock["development"][0], (start + timedelta(days=1)).isoformat())
            self.assertEqual(lock["evaluation_end"], (final_day + timedelta(days=1)).isoformat())
            first = lock["bindings"][0]
            self.assertEqual(first["membership_snapshot_date"], start.isoformat())
            self.assertEqual(first["evaluation_date"], (start + timedelta(days=1)).isoformat())
            self.assertEqual(first["eligible_count"], 30)
            self.assertEqual(len(first["manifest_sha256"]), 64)

            snapshot = next(root.glob("universe-*.json"))
            payload = json.loads(snapshot.read_text(encoding="utf-8"))
            payload["instruments"][0]["eligible"] = False
            snapshot.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(Alpha015ActivationLockError, "not ready"):
                build_alpha015_activation_lock(
                    snapshot_directory=root,
                    universe_policy=policy,
                    expected_protocol=protocol,
                    candidate_policy=candidate_policy,
                    amendment=amendment,
                    as_of=datetime.combine(final_day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1),
                )

    def test_full_market_universe_coverage_requires_consecutive_fresh_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day in (1, 2, 4):
                observed = datetime(2025, 1, day, 12, tzinfo=timezone.utc)
                write_snapshot_batch(
                    directory=root,
                    observed_at=observed,
                    instruments=[{"eligible": True, "depth_collected": True}],
                    metadata={"all_usdt_perpetual": True},
                )
            accumulating = assess_universe_history(
                root,
                as_of=datetime(2025, 1, 4, 18, tzinfo=timezone.utc),
                required_consecutive_days=3,
            )
            self.assertEqual(accumulating["status"], "ACCUMULATING")
            self.assertEqual(accumulating["consecutive_days_ending_latest"], 1)
            observed = datetime(2025, 1, 3, 12, tzinfo=timezone.utc)
            write_snapshot_batch(
                directory=root,
                observed_at=observed,
                instruments=[{"eligible": True, "depth_collected": True}],
                metadata={"all_usdt_perpetual": True},
            )
            ready = assess_universe_history(
                root,
                as_of=datetime(2025, 1, 4, 18, tzinfo=timezone.utc),
                required_consecutive_days=3,
            )
            self.assertEqual(ready["status"], "READY")
            self.assertEqual(ready["consecutive_days_ending_latest"], 4)

    def test_future_snapshot_cannot_change_past_universe(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        policy = UniversePolicy(min_quote_volume_24h=100, min_listing_age_days=1, min_candles=10, max_spread_bps=10, min_orderbook_depth=100)
        btc = InstrumentSnapshot("BTC/USDT:USDT", start, start - timedelta(days=100), 1_000, 100, True, 2, 1_000)
        future_coin = InstrumentSnapshot("FUTURE/USDT:USDT", start + timedelta(days=30), start + timedelta(days=29), 1_000, 100, True, 2, 1_000)
        decision = select_universe([btc, future_coin], as_of=start, policy=policy)
        self.assertEqual(decision.selected, ("BTC/USDT:USDT",))
        history = build_universe_history([btc, future_coin], as_of_dates=[start], policy=policy)
        self.assertEqual(history["2025-01-01"], ["BTC/USDT:USDT"])

    def test_snapshot_batches_are_immutable_and_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            observed = datetime(2025, 1, 1, tzinfo=timezone.utc)
            write_snapshot_batch(
                directory=Path(directory),
                observed_at=observed,
                instruments=[
                    {
                        "symbol": "BTC/USDT:USDT",
                        "listed_at": "2020-01-01T00:00:00+00:00",
                        "quote_volume": 1_000,
                        "candle_count": 1_000,
                        "data_continuous": True,
                        "spread_bps": 1.0,
                        "depth_10bps": 1_000,
                        "listing_status": "active",
                    }
                ],
            )
            self.assertEqual(len(load_instrument_snapshots(Path(directory))), 1)
            with self.assertRaises(FileExistsError):
                write_snapshot_batch(directory=Path(directory), observed_at=observed, instruments=[])


class RegimeTests(unittest.TestCase):
    @staticmethod
    def frame(length: int) -> pd.DataFrame:
        close = pd.Series([100 + index * 0.2 for index in range(length)], dtype=float)
        return pd.DataFrame({"close": close, "high": close + 1, "low": close - 1})

    def test_future_candles_do_not_change_prior_labels(self) -> None:
        classifier = RegimeClassifier(RegimeConfig.alpha1_compatible())
        past = self.frame(450)
        future = pd.DataFrame({"close": [190, 100, 250], "high": [191, 101, 251], "low": [189, 99, 249]})
        prior = classifier.annotate(past)
        extended = classifier.annotate(pd.concat([past, future], ignore_index=True)).iloc[: len(past)]
        pd.testing.assert_series_equal(prior["regime"], extended["regime"])
        pd.testing.assert_series_equal(prior["panic_vol_threshold"], extended["panic_vol_threshold"])

    def test_alpha1_compatibility_is_price_and_ema_filter(self) -> None:
        classifier = RegimeClassifier(RegimeConfig.alpha1_compatible())
        annotated = classifier.annotate(self.frame(450))
        expected = (annotated["close"] > annotated["ema_200"]) & (annotated["ema_50"] > annotated["ema_200"])
        self.assertTrue((annotated.loc[expected, "regime"] == Regime.BULL.value).all())
        evidence = classifier.evidence_at(annotated)
        self.assertEqual(evidence["regime"], Regime.BULL.value)
        self.assertTrue(evidence["btc_above_ema200"])


class ResearchTests(unittest.TestCase):
    def test_signal_diagnostics_measure_rank_ic_spread_persistence_and_turnover(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        observations = []
        for period in range(3):
            decision = start + timedelta(hours=period)
            for index in range(5):
                observations.append(
                    SignalObservation(
                        decision_at=decision,
                        forward_start_at=decision + timedelta(minutes=1),
                        forward_end_at=decision + timedelta(minutes=11),
                        symbol=f"S{index}",
                        signal=float(index),
                        forward_return=index / 100.0,
                    )
                )
        result = summarize_signal_diagnostics(
            observations, minimum_cross_section=5, quantile_fraction=0.20
        )
        self.assertEqual(result["status"], "DIAGNOSTIC_COMPLETE")
        self.assertFalse(result["promotion_eligible"])
        self.assertEqual(result["live_order_authorization"], "CLOSED")
        self.assertAlmostEqual(result["signal_quality"]["rank_ic"]["mean"], 1.0)
        self.assertAlmostEqual(
            result["signal_quality"]["top_bottom_gross_return"]["mean"], 0.04
        )
        self.assertAlmostEqual(
            result["signal_quality"]["signal_rank_autocorrelation"]["mean"], 1.0
        )
        self.assertAlmostEqual(result["signal_quality"]["one_way_turnover"]["mean"], 0.0)

    def test_signal_diagnostics_expose_full_rank_reversal_turnover(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        observations = []
        for period, signals in enumerate(([0, 1, 2, 3], [3, 2, 1, 0])):
            decision = start + timedelta(hours=period)
            for index, signal in enumerate(signals):
                observations.append(
                    SignalObservation(
                        decision_at=decision,
                        forward_start_at=decision + timedelta(minutes=1),
                        forward_end_at=decision + timedelta(minutes=11),
                        symbol=f"S{index}",
                        signal=float(signal),
                        forward_return=float(signal) / 100.0,
                    )
                )
        result = summarize_signal_diagnostics(
            observations, minimum_cross_section=4, quantile_fraction=0.25
        )
        self.assertAlmostEqual(result["periods"][1]["one_way_turnover"], 2.0)
        self.assertAlmostEqual(
            result["periods"][1]["signal_rank_autocorrelation"], -1.0
        )

    def test_signal_diagnostics_report_overlap_aware_hac_lag(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        observations = []
        for period in range(4):
            decision = start + timedelta(hours=period)
            for index in range(4):
                observations.append(
                    SignalObservation(
                        decision_at=decision,
                        forward_start_at=decision + timedelta(minutes=1),
                        forward_end_at=decision + timedelta(hours=2),
                        symbol=f"S{index}",
                        signal=float(index),
                        forward_return=(index + period) / 100.0,
                    )
                )
        result = summarize_signal_diagnostics(
            observations, minimum_cross_section=4, quantile_fraction=0.25
        )
        quality = result["signal_quality"]["top_bottom_gross_return"]
        self.assertEqual(result["coverage"]["overlap_aware_hac_lag"], 1)
        self.assertEqual(quality["overlap_aware_hac_lag"], 1)
        self.assertIsNotNone(quality["information_ratio"])
        self.assertIsNotNone(quality["overlap_aware_t_statistic"])

    def test_alpha013_signal_panel_uses_delayed_entry_and_completed_raw_labels(self) -> None:
        decision = pd.Timestamp("2024-01-08T04:00:00Z")
        price_index = pd.date_range("2024-01-01T00:00:00Z", periods=15 * 24, freq="h")
        funding_index = pd.date_range("2024-01-01T00:00:00Z", periods=50, freq="8h")
        markets = {}
        rates = {
            "BTCUSDT": 0.0001,
            "ETHUSDT": 0.0002,
            "BNBUSDT": -0.0002,
            "SOLUSDT": -0.0001,
            "XRPUSDT": 0.0003,
        }
        for index, (symbol, rate) in enumerate(rates.items()):
            opens = pd.Series(
                [100.0 + index + hour * (0.01 + index * 0.001) for hour in range(len(price_index))],
                index=price_index,
            )
            markets[symbol] = {
                "price": pd.DataFrame({"open": opens}),
                "funding": pd.DataFrame({"funding_rate": rate}, index=funding_index),
            }
        observations, coverage = build_funding_dispersion_signal_panel(
            markets, first_decision=decision
        )
        self.assertEqual(coverage["completed_decisions"], 1)
        self.assertEqual(coverage["threshold_eligible_decisions"], 1)
        self.assertEqual(coverage["rows"], 5)
        self.assertEqual([item.symbol for item in observations], sorted(rates))
        self.assertTrue(all(item.forward_start_at == (decision + timedelta(hours=1)).to_pydatetime() for item in observations))
        self.assertTrue(all(item.forward_end_at == (decision + timedelta(days=7, hours=1)).to_pydatetime() for item in observations))
        self.assertEqual(max(observations, key=lambda item: item.signal).symbol, "BNBUSDT")

    def test_alpha013_signal_panel_receipt_rejects_tampered_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel_path = Path(temporary) / "signal-panel.csv"
            receipt_path = Path(temporary) / "signal-panel.json"
            panel = ",".join(SIGNAL_DIAGNOSTIC_HEADER) + "\n"
            panel_path.write_text(panel, encoding="utf-8")
            protocol = {
                relative: sha256((Path.cwd() / relative).read_bytes()).hexdigest()
                for relative in PANEL_PROTOCOL_FILES
            }
            receipt = {
                "kind": "ALPHA013_SOURCE_BOUND_SIGNAL_PANEL",
                "status": "ACCUMULATING",
                "promotion_eligible": False,
                "live_order_authorization": "CLOSED",
                "panel": {
                    "path": "research/alpha013/signal-panel.csv",
                    "sha256": sha256(panel.encode("utf-8")).hexdigest(),
                    "header": SIGNAL_DIAGNOSTIC_HEADER,
                },
                "coverage": {"rows": 0},
                "source_integrity": {"source_inventory_sha256": "source-hash"},
                "protocol_sha256": protocol,
            }
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            health = signal_panel_health(
                expected_source_inventory_sha256="source-hash",
                panel_path=panel_path,
                receipt_path=receipt_path,
                panel_reference="research/alpha013/signal-panel.csv",
            )
            self.assertTrue(health["valid"])

            panel_path.write_text(panel + "tampered\n", encoding="utf-8")
            tampered = signal_panel_health(
                expected_source_inventory_sha256="source-hash",
                panel_path=panel_path,
                receipt_path=receipt_path,
                panel_reference="research/alpha013/signal-panel.csv",
            )
            self.assertFalse(tampered["valid"])
            self.assertEqual(tampered["status"], "INVALID")

    def test_alpha013_signal_panel_requires_decision_after_preregistration(self) -> None:
        preregistration = json.loads(
            (Path.cwd() / "research" / "alpha013-preregistration.json").read_text(
                encoding="utf-8"
            )
        )
        preregistration["registered_at_utc"] = "2026-10-20T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "must follow preregistration"):
            assert_alpha013_signal_contract(preregistration)

    def test_alpha013_funding_decision_requires_receipt_before_entry(self) -> None:
        preregistration = json.loads(
            (Path.cwd() / "research" / "alpha013-preregistration.json").read_text(
                encoding="utf-8"
            )
        )
        decision = datetime(2026, 10, 19, 4, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            missing = audit_decision_availability(
                preregistration,
                preregistration_sha256="a" * 64,
                receipt_directory=directory,
                now=decision + timedelta(hours=1, minutes=1),
            )
            self.assertEqual(missing["status"], "CAUSAL_AVAILABILITY_VETO")
            self.assertEqual(missing["failures"][0]["reason"], "missing_receipt")

            event_times = [
                (decision - timedelta(hours=8 * (offset + 1))).isoformat()
                for offset in range(21)
            ]
            receipt = {
                "schema_version": 1,
                "kind": "FUNDING_DECISION_AVAILABILITY_RECEIPT",
                "decision_at_utc": decision.isoformat(),
                "captured_at_utc": (decision + timedelta(minutes=5)).isoformat(),
                "preregistration_sha256": "a" * 64,
                "funding_events": {
                    symbol: [{"funding_at_utc": item} for item in event_times]
                    for symbol in preregistration["fixed_signal_and_execution"]["universe"]
                },
            }
            (directory / receipt_filename(decision)).write_text(
                json.dumps(receipt), encoding="utf-8"
            )
            valid = audit_decision_availability(
                preregistration,
                preregistration_sha256="a" * 64,
                receipt_directory=directory,
                now=decision + timedelta(hours=1, minutes=1),
            )
            self.assertEqual(valid["status"], "PASS_AVAILABILITY_ONLY")
            self.assertFalse(valid["promotion_eligible"])

    def test_signal_diagnostic_loader_rejects_duplicate_and_noncausal_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "signals.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(SIGNAL_DIAGNOSTIC_HEADER)
                writer.writerow(
                    [
                        "2024-01-01T00:00:00Z",
                        "2024-01-01T00:01:00Z",
                        "2024-01-01T00:11:00Z",
                        "BTCUSDT",
                        "1",
                        "0.01",
                    ]
                )
            loaded = load_signal_observations(path)
            self.assertEqual(loaded[0].symbol, "BTCUSDT")

            with path.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(
                    [
                        "2024-01-01T00:00:00Z",
                        "2024-01-01T00:01:00Z",
                        "2024-01-01T00:11:00Z",
                        "BTCUSDT",
                        "2",
                        "0.02",
                    ]
                )
            with self.assertRaisesRegex(ValueError, "duplicates"):
                load_signal_observations(path)

            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(SIGNAL_DIAGNOSTIC_HEADER)
                writer.writerow(
                    [
                        "2024-01-01T00:00:00Z",
                        "2024-01-01T00:00:00Z",
                        "2024-01-01T00:11:00Z",
                        "BTCUSDT",
                        "1",
                        "0.01",
                    ]
                )
            with self.assertRaisesRegex(ValueError, "causal"):
                load_signal_observations(path)

    def test_signal_diagnostics_reject_mixed_intervals_and_duplicate_symbols(self) -> None:
        decision = datetime(2024, 1, 1, tzinfo=timezone.utc)
        observations = [
            SignalObservation(
                decision_at=decision,
                forward_start_at=decision + timedelta(minutes=1),
                forward_end_at=decision + timedelta(minutes=11 + index),
                symbol=f"S{index}",
                signal=float(index),
                forward_return=index / 100.0,
            )
            for index in range(3)
        ]
        with self.assertRaisesRegex(ValueError, "mixed forward intervals"):
            summarize_signal_diagnostics(observations, minimum_cross_section=3)

        duplicate = [
            SignalObservation(
                decision_at=decision,
                forward_start_at=decision + timedelta(minutes=1),
                forward_end_at=decision + timedelta(minutes=11),
                symbol="BTCUSDT",
                signal=float(index),
                forward_return=index / 100.0,
            )
            for index in range(3)
        ]
        with self.assertRaisesRegex(ValueError, "duplicate symbols"):
            summarize_signal_diagnostics(duplicate, minimum_cross_section=3)

    def test_liquidation_validation_lock_starts_next_day_and_rejects_partial_report(self) -> None:
        protocol = {"collector.py": "a" * 64}
        days = [
            (datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=index)).date().isoformat()
            for index in range(60)
        ]
        development = {
            "candidate": "RISK_OVERLAY_001_OBSERVED_LIQUIDATION_SHOCK",
            "status": "DEVELOPMENT_PASS_REQUIRES_EXPLICIT_LOCK_OPEN",
            "performance_calculated": True,
            "locked_validation_opened": False,
            "protocol_sha256": protocol,
            "required_complete_utc_days": 60,
            "selected_development_days": days,
            "latest_opened_utc_day": days[-1],
        }
        receipt = build_validation_lock(
            development,
            development_sha256="b" * 64,
            current_protocol=protocol,
            now=datetime(2026, 12, 5, 13, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(receipt["validation_start_utc"], "2026-12-06T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "not eligible"):
            build_validation_lock(
                {**development, "status": "ACCUMULATING"},
                development_sha256="b" * 64,
                current_protocol=protocol,
                now=datetime(2026, 12, 5, 13, 30, tzinfo=timezone.utc),
            )

    def test_scoreboard_keeps_auxiliary_gates_outside_experiment_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "reports" / "execution-calibration.json").write_text(
                json.dumps({"status": "CALIBRATION_PASS", "promotion_eligible": False}),
                encoding="utf-8",
            )
            snapshot = auxiliary_gate_snapshot(root)
            self.assertEqual(snapshot["execution_calibration"]["status"], "CALIBRATION_PASS")
            self.assertFalse(snapshot["execution_calibration"]["promotion_eligible"])
            self.assertEqual(
                snapshot["alpha015_frozen_calibration_reference"]["status"],
                "MISSING_OR_INVALID",
            )
            self.assertEqual(snapshot["alpha007_ten_bps_cost_stress"]["status"], "MISSING_OR_INVALID")

    def test_scoreboard_exposes_alpha015_frozen_calibration_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "research").mkdir()
            (root / "reports" / "execution-calibration.json").write_text(
                json.dumps({"status": "CALIBRATION_PASS"}), encoding="utf-8"
            )
            (root / "research" / "alpha015-methodology-audit.json").write_text(
                json.dumps(
                    {"frozen_transfer_choices": {"execution_calibration_sha256": "a" * 64}}
                ),
                encoding="utf-8",
            )
            snapshot = auxiliary_gate_snapshot(root)
            binding = snapshot["alpha015_frozen_calibration_reference"]
            self.assertEqual(binding["status"], "FROZEN_HASH_MISMATCH")
            self.assertFalse(binding["promotion_eligible"])

    def test_scoreboard_reads_versioned_alpha015_calibration_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "reports").mkdir()
            (root / "research").mkdir()
            receipt = root / "reports" / "execution-calibration-locked.json"
            receipt.write_text(json.dumps({"status": "CALIBRATION_PASS"}), encoding="utf-8")
            (root / "research" / "alpha015-methodology-audit.json").write_text(
                json.dumps(
                    {
                        "frozen_transfer_choices": {
                            "execution_calibration_path": "reports/execution-calibration-locked.json",
                            "execution_calibration_sha256": sha256(receipt.read_bytes()).hexdigest(),
                        }
                    }
                ),
                encoding="utf-8",
            )
            binding = auxiliary_gate_snapshot(root)["alpha015_frozen_calibration_reference"]
            self.assertEqual(binding["status"], "PASS")
            self.assertEqual(binding["path"], "reports/execution-calibration-locked.json")

    def test_liquidation_history_requires_protocol_hash_and_immutable_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run-001"
            run.mkdir()
            source = run / "telemetry.jsonl.gz"
            source.write_bytes(b"sealed")
            digest = sha256(source.read_bytes()).hexdigest()
            protocol = {"collector.py": "a" * 64}
            manifest = {
                "status": "PASS",
                "purpose": "forward_risk_overlay_telemetry_not_promotion_evidence",
                "feed_semantics": "censored_latest_snapshot_not_total_liquidation_volume",
                "protocol_sha256": protocol,
                "result": {
                    "started_at": "2026-10-10T00:00:00+00:00",
                    "ended_at": "2026-10-10T23:00:00+00:00",
                    "rows": {"liquidation_rows": 3, "mark_rows": 10},
                    "open_interest": {"rows": 5},
                },
                "files": [
                    {"path": source.name, "bytes": source.stat().st_size, "sha256": digest}
                ],
            }
            (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            policy = {
                "candidate": "RISK_OVERLAY_001_OBSERVED_LIQUIDATION_SHOCK",
                "role": "risk overlay",
                "eligible_start_utc": "2026-10-10T00:00:00Z",
                "development_gate": {
                    "minimum_stream_coverage_per_day": 0.95,
                    "complete_utc_days": 1,
                },
            }
            valid = assess_liquidation_history(
                root,
                policy=policy,
                expected_protocol=protocol,
                now=datetime(2026, 10, 11, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(valid["status"], "DEVELOPMENT_TELEMETRY_READY_ANALYSIS_NOT_RUN")
            self.assertEqual(valid["complete_utc_days_count"], 1)
            source.write_bytes(b"tampered")
            tampered = assess_liquidation_history(
                root,
                policy=policy,
                expected_protocol=protocol,
                now=datetime(2026, 10, 11, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(tampered["status"], "DATA_INTEGRITY_ATTENTION")
            self.assertIn("file_mismatch:telemetry.jsonl.gz", tampered["invalid_chunks"][0]["failures"])

    def test_alpha007_external_cost_stress_can_only_make_gate_stricter(self) -> None:
        calibration = {
            "status": "CALIBRATION_PASS",
            "promotion_eligible": False,
            "cost_reference": {"includes_fee": True},
            "per_symbol": {
                "BTCUSDT": {"conservative_all_in_execution_drag_bps": 4.77}
            },
        }
        policy = {
            "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
            "base_round_trip_cost_bps": 7.0,
            "stressed_round_trip_cost_bps": 10.0,
            "additional_deduction_bps": 3.0,
            "source_calibration": {"btc_observed_all_in_entry_drag_bps": 4.77},
        }
        primary = {
            "status": "PASS",
            "complete_days": 17,
            "configuration": {"entry_fee": 0.0002, "exit_fee": 0.0005},
            "phases": {
                "LOCKED_OOS": {
                    "net_markout_60s": {"mean": 0.0002},
                    "hour_block_bootstrap_95pct_lower_mean_60s": 0.0001,
                    "latency_100ms_net_markout_60s": {"mean": 0.0002},
                }
            },
        }
        failed = evaluate_alpha007_external_cost_stress(primary, calibration, policy)
        self.assertEqual(failed["status"], "FAIL")
        primary["phases"]["LOCKED_OOS"]["net_markout_60s"]["mean"] = 0.0005
        primary["phases"]["LOCKED_OOS"]["hour_block_bootstrap_95pct_lower_mean_60s"] = 0.0004
        primary["phases"]["LOCKED_OOS"]["latency_100ms_net_markout_60s"]["mean"] = 0.0005
        passed = evaluate_alpha007_external_cost_stress(primary, calibration, policy)
        self.assertEqual(passed["status"], "PASS")

    def test_alpha007_partial_fill_stress_charges_every_exposure_and_only_vetoes(self) -> None:
        policy = {
            "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
            "position_accounting": {
                "round_trip_cost_bps": 10.0,
                "include_every_positive_estimated_fill_quantity": True,
                "unfilled_orders_contribute_zero": True,
            },
            "locked_requirements": {
                "complete_days_minimum": 17,
                "base_execution_coverage_ratio": 1.0,
                "latency_execution_coverage_ratio_minimum": 0.95,
                "executed_exposures_minimum": 100,
                "minimum_eligible_orders_per_side": 30,
            },
        }
        records = []
        for index in range(120):
            side = "buy" if index % 2 == 0 else "sell"
            fill_fraction = 0.5 if index == 0 else 1.0
            for path in ("base", "latency_100ms"):
                records.append(
                    {
                        "path": path,
                        "hour": f"2026-11-{1 + index // 24:02d}T{index % 24:02d}",
                        "side": side,
                        "execution_available": True,
                        "fill_fraction": fill_fraction,
                        "net_return": 0.002,
                    }
                )
        passed = evaluate_partial_fill_stress(
            records,
            primary_status="PASS",
            complete_days=17,
            policy=policy,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertFalse(passed["promotion_eligible"])
        self.assertEqual(passed["base"]["partial_fill_orders"], 1)
        self.assertGreater(passed["partial_fill_total_contribution"], 0)

        adverse = [dict(item) for item in records]
        adverse[0]["net_return"] = -0.1
        failed = evaluate_partial_fill_stress(
            adverse,
            primary_status="PASS",
            complete_days=17,
            policy=policy,
        )
        partial_check = next(
            item
            for item in failed["checks"]
            if item["name"] == "partial_fill_total_contribution"
        )
        self.assertFalse(partial_check["passed"])
        self.assertEqual(failed["status"], "FAIL")

        accumulating = evaluate_partial_fill_stress(
            records,
            primary_status="ACCUMULATING",
            complete_days=1,
            policy=policy,
        )
        self.assertEqual(accumulating["status"], "ACCUMULATING")
        self.assertFalse(accumulating["promotion_eligible"])

    def test_liquidation_trigger_is_causal_and_episode_freeze_extends(self) -> None:
        trigger_time = 1_800_000_060_000
        marks = [
            {"symbol": "BTCUSDT", "event_time_ms": trigger_time - 60_000, "mark_price": 100.0},
            {"symbol": "BTCUSDT", "event_time_ms": trigger_time, "mark_price": 99.0},
        ]
        liquidations = [
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "event_time_ms": trigger_time,
                "observed_snapshot_notional": 200.0,
            }
        ]
        oi = [
            {
                "symbol": "BTCUSDT",
                "event_time_ms": trigger_time,
                "open_interest_base_qty": 1_000.0,
            }
        ]
        before_future = liquidation_qualifying_triggers(
            liquidations,
            oi,
            marks,
            lookback_seconds=60,
            minimum_ratio=0.001,
            minimum_mark_move=0.005,
        )
        after_future = liquidation_qualifying_triggers(
            liquidations,
            oi,
            marks
            + [
                {
                    "symbol": "BTCUSDT",
                    "event_time_ms": trigger_time + 1,
                    "mark_price": 1_000.0,
                }
            ],
            lookback_seconds=60,
            minimum_ratio=0.001,
            minimum_mark_move=0.005,
        )
        self.assertEqual(before_future, after_future)
        self.assertEqual(len(before_future), 1)
        clustered = [
            {**before_future[0], "event_time_ms": trigger_time},
            {**before_future[0], "event_time_ms": trigger_time + 10 * 60_000},
            {**before_future[0], "event_time_ms": trigger_time + 35 * 60_000},
        ]
        episodes = collapse_liquidation_episodes(clustered, freeze_minutes=30)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["qualifying_trigger_count"], 3)
        self.assertEqual(episodes[0]["freeze_end_ms"], trigger_time + 65 * 60_000)

    def test_liquidation_overlay_uses_past_week_baseline_and_locked_gate(self) -> None:
        day_ms = 86_400_000
        horizon_ms = 30 * 60_000
        t1 = int(datetime(2026, 10, 20, 12, tzinfo=timezone.utc).timestamp() * 1_000)
        t2 = t1 + day_ms
        marks = []
        liquidations = []
        oi = []
        complete_days = set()
        for trigger_time in (t1, t2):
            baseline = trigger_time - 7 * day_ms
            complete_days.update(
                {
                    datetime.fromtimestamp(trigger_time / 1_000, timezone.utc).date(),
                    datetime.fromtimestamp(baseline / 1_000, timezone.utc).date(),
                }
            )
            marks.extend(
                [
                    {"symbol": "BTCUSDT", "event_time_ms": trigger_time - 60_000, "mark_price": 100.0},
                    {"symbol": "BTCUSDT", "event_time_ms": trigger_time, "mark_price": 99.0},
                    {"symbol": "BTCUSDT", "event_time_ms": trigger_time + horizon_ms, "mark_price": 96.0},
                    {"symbol": "BTCUSDT", "event_time_ms": baseline, "mark_price": 100.0},
                    {"symbol": "BTCUSDT", "event_time_ms": baseline + horizon_ms, "mark_price": 99.5},
                ]
            )
            liquidations.append(
                {"symbol": "BTCUSDT", "side": "SELL", "event_time_ms": trigger_time, "observed_snapshot_notional": 200.0}
            )
            oi.append(
                {"symbol": "BTCUSDT", "event_time_ms": trigger_time, "open_interest_base_qty": 1_000.0}
            )
        policy = {
            "fixed_trigger_for_later_evaluation": {
                "lookback_seconds": 60,
                "minimum_observed_snapshot_notional_to_open_interest_notional": 0.001,
                "minimum_same_direction_mark_return_abs": 0.005,
                "entry_freeze_minutes": 30,
            },
            "development_gate": {
                "minimum_nonoverlapping_triggers": 2,
                "triggered_30m_absolute_return_median_at_least_matched_baseline_multiple": 1.5,
                "triggered_30m_one_percent_adverse_move_rate_minus_matched_baseline_minimum": 0.5,
                "single_symbol_trigger_concentration_maximum": 1.0,
            },
        }
        result = evaluate_liquidation_overlay(
            liquidations, oi, marks, policy=policy, complete_days=complete_days
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["matched_episodes"], 2)
        self.assertGreater(result["median_tail_multiple"], 1.5)

    def test_liquidation_coverage_merges_overlaps_without_double_counting(self) -> None:
        start = datetime(2026, 10, 10, tzinfo=timezone.utc)
        self.assertEqual(
            merged_seconds(
                [
                    (start, start + timedelta(seconds=10)),
                    (start + timedelta(seconds=5), start + timedelta(seconds=20)),
                    (start + timedelta(seconds=30), start + timedelta(seconds=40)),
                ]
            ),
            30.0,
        )

    def test_liquidation_telemetry_names_censored_snapshot_and_filters_universe(self) -> None:
        payload = [
            {
                "e": "forceOrder",
                "E": 1_800_000_000_000,
                "o": {
                    "s": "BTCUSDT",
                    "S": "SELL",
                    "q": "2",
                    "z": "1.5",
                    "l": "0.5",
                    "ap": "100",
                    "T": 1_800_000_000_000,
                },
            },
            {
                "e": "forceOrder",
                "E": 1_800_000_000_000,
                "o": {
                    "s": "UNREGISTEREDUSDT",
                    "S": "BUY",
                    "q": "1",
                    "z": "1",
                    "l": "1",
                    "ap": "10",
                    "T": 1_800_000_000_000,
                },
            },
        ]
        rows = liquidation_events(payload, symbols={"BTCUSDT"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["liquidated_position_side"], "LONG")
        self.assertEqual(rows[0]["observed_snapshot_notional"], 150.0)
        self.assertIn("CENSORED", rows[0]["feed_completeness"])
        marks = mark_prices(
            [{"E": 1_800_000_000_001, "s": "BTCUSDT", "p": "101.5"}],
            symbols={"BTCUSDT"},
        )
        self.assertEqual(marks[0]["mark_price"], 101.5)

    def test_deflated_sharpe_penalizes_the_full_trial_inventory(self) -> None:
        rng = np.random.default_rng(20261010)
        selected = rng.normal(0.001, 0.01, 400)
        one_trial = deflated_sharpe_ratio(selected, [periodic_sharpe(selected)])
        many_trials = deflated_sharpe_ratio(selected, np.linspace(-0.5, 0.5, 100))
        self.assertGreater(one_trial["probability"], 0.95)
        self.assertLess(many_trials["probability"], one_trial["probability"])
        self.assertGreater(
            many_trials["expected_maximum_periodic_sharpe"],
            one_trial["expected_maximum_periodic_sharpe"],
        )

    def test_cscv_pbo_separates_stable_edge_from_noise(self) -> None:
        rng = np.random.default_rng(20261010)
        observations = 320
        stable = np.column_stack(
            [
                rng.normal(0.0015, 0.004, observations),
                rng.normal(0.0002, 0.004, observations),
                rng.normal(-0.0001, 0.004, observations),
                rng.normal(0.0, 0.005, observations),
            ]
        )
        stable_pbo = probability_backtest_overfitting(stable, slices=8)
        noise = rng.normal(0.0, 0.01, (observations, 20))
        noise_pbo = probability_backtest_overfitting(noise, slices=8)
        self.assertEqual(stable_pbo["combinations"], 70)
        self.assertEqual(stable_pbo["probability"], 0.0)
        self.assertGreater(noise_pbo["probability"], 0.10)

    def test_selection_bias_audit_binds_selected_returns_and_inventory(self) -> None:
        rng = np.random.default_rng(20261010)
        matrix = np.column_stack(
            [
                rng.normal(0.0015, 0.004, 320),
                rng.normal(0.0002, 0.004, 320),
                rng.normal(-0.0001, 0.004, 320),
                rng.normal(0.0, 0.005, 320),
            ]
        )
        audit = evaluate_selection_bias(
            matrix[:, 0],
            matrix,
            attempted_trials=4,
            trial_ids=["selected", "b", "c", "d"],
            selected_trial_id="selected",
            trial_inventory_complete=True,
        )
        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(len(audit["trial_return_matrix_sha256"]), 64)
        self.assertEqual(len(audit["selected_return_sha256"]), 64)

        incomplete = evaluate_selection_bias(
            matrix[:, 0],
            matrix,
            attempted_trials=4,
            trial_ids=["selected", "b", "c", "d"],
            selected_trial_id="selected",
            trial_inventory_complete=False,
        )
        self.assertEqual(incomplete["status"], "FAIL")
        with self.assertRaisesRegex(ValueError, "selected returns"):
            evaluate_selection_bias(
                matrix[:, 0] + 0.0001,
                matrix,
                attempted_trials=4,
                trial_ids=["selected", "b", "c", "d"],
                selected_trial_id="selected",
                trial_inventory_complete=True,
            )

    def test_trial_inventory_must_be_sealed_before_the_common_sample(self) -> None:
        inventory = {
            "schema_version": 1,
            "inventory_id": "INV-001",
            "family_id": "ALPHA_FAMILY",
            "sample_role": "locked_forward",
            "return_periodicity": "daily",
            "return_definition": "net_of_fees_slippage_funding",
            "complete_attempted_trial_inventory": True,
            "selection_rule_preregistered": True,
            "selection_rule_sha256": "a" * 64,
            "inventory_closed_at_utc": "2025-12-31T00:00:00Z",
            "selected_at_utc": "2025-12-30T00:00:00Z",
            "sample_started_at_utc": "2026-01-01T00:00:00Z",
            "selected_trial_id": "selected",
            "trials": [
                {
                    "attempt_number": 1,
                    "trial_id": "selected",
                    "configuration_sha256": "b" * 64,
                    "registered_at_utc": "2025-12-29T00:00:00Z",
                },
                {
                    "attempt_number": 2,
                    "trial_id": "alternate",
                    "configuration_sha256": "c" * 64,
                    "registered_at_utc": "2025-12-29T00:00:00Z",
                },
            ],
        }
        valid = validate_trial_inventory(
            inventory,
            trial_ids=["selected", "alternate"],
            first_observation_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(valid["status"], "PASS")

        inventory["trials"][1]["registered_at_utc"] = "2026-01-02T00:00:00Z"
        invalid = validate_trial_inventory(
            inventory,
            trial_ids=["selected", "alternate"],
            first_observation_at="2026-01-01T00:00:00Z",
        )
        self.assertEqual(invalid["status"], "FAIL")
        self.assertTrue(any("registration" in item for item in invalid["failures"]))

    def test_selection_bias_receipt_is_recomputed_from_sealed_sources(self) -> None:
        rng = np.random.default_rng(20261010)
        trial_ids = ["selected", "b", "c", "d"]
        matrix = np.column_stack(
            [
                rng.normal(0.0015, 0.004, 320),
                rng.normal(0.0002, 0.004, 320),
                rng.normal(-0.0001, 0.004, 320),
                rng.normal(0.0, 0.005, 320),
            ]
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            returns_path = root / "trial-returns.csv"
            inventory_path = root / "inventory.json"
            audit_path = root / "audit.json"
            with returns_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", *trial_ids])
                start = datetime(2025, 1, 1, tzinfo=timezone.utc)
                for offset, row in enumerate(matrix):
                    writer.writerow([(start + timedelta(days=offset)).isoformat(), *row])
            inventory = {
                "schema_version": 1,
                "inventory_id": "TEST-REPLAY",
                "family_id": "TEST",
                "sample_role": "locked_forward",
                "return_periodicity": "daily",
                "return_definition": "net_of_fees_slippage_funding",
                "complete_attempted_trial_inventory": True,
                "selection_rule_preregistered": True,
                "selection_rule_sha256": "a" * 64,
                "selected_at_utc": "2024-12-29T00:00:00Z",
                "inventory_closed_at_utc": "2024-12-30T00:00:00Z",
                "sample_started_at_utc": "2025-01-01T00:00:00Z",
                "selected_trial_id": "selected",
                "trials": [
                    {
                        "attempt_number": number,
                        "trial_id": trial_id,
                        "configuration_sha256": sha256(trial_id.encode()).hexdigest(),
                        "registered_at_utc": "2024-12-28T00:00:00Z",
                    }
                    for number, trial_id in enumerate(trial_ids, start=1)
                ],
            }
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    "scripts/selection_bias_audit.py",
                    "--trial-returns",
                    str(returns_path),
                    "--trial-inventory",
                    str(inventory_path),
                    "--output",
                    str(audit_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            replayed, sources = _load_selection_bias_audit(audit_path)
            self.assertEqual(replayed["status"], "PASS")
            self.assertIn("trial_inventory", sources)

            tampered = json.loads(audit_path.read_text(encoding="utf-8"))
            tampered["deflated_sharpe"]["probability"] = 1.0
            audit_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "does not match source replay"):
                _load_selection_bias_audit(audit_path)

    def test_portfolio_risk_uses_daily_mtm_and_two_cluster_horizons(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "portfolio-equity.csv"
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            rows = ["timestamp,equity,equity_type"]
            for offset in range(366):
                timestamp = start + timedelta(days=offset)
                rows.append(
                    f"{timestamp.isoformat()},{1000.0 * (1.004 ** offset)},mark_to_market"
                )
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            equity = load_portfolio_equity(path)
            low_simulation_result = evaluate_portfolio_risk(
                equity, PortfolioRiskPolicy(simulations_per_block=100)
            )
            self.assertEqual(low_simulation_result["status"], "FAIL")
            result = evaluate_portfolio_risk(
                equity, PortfolioRiskPolicy(simulations_per_block=5_000)
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["observations"], 365)
            self.assertEqual(
                {item["block_days"] for item in result["scenarios"]}, {7, 28}
            )
            self.assertEqual(result["aggregate"]["probability_of_ruin"], 0.0)
            self.assertGreater(
                result["aggregate"]["probability_of_ruin_upper_confidence"], 0.0
            )

            path.write_text("\n".join(rows[:100] + rows[101:]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "every consecutive UTC day"):
                load_portfolio_equity(path)

            tail_returns = [0.01] * 365
            tail_returns[100] = -0.10
            tail_result = evaluate_portfolio_risk(
                {
                    "daily_returns": tail_returns,
                    "historical_maximum_drawdown": 0.10,
                },
                PortfolioRiskPolicy(simulations_per_block=500),
            )
            tail_check = next(
                item
                for item in tail_result["checks"]
                if item["name"] == "deterministic_tail_stress_drawdown"
            )
            self.assertFalse(tail_check["passed"])
            self.assertGreater(tail_check["actual"], 0.20)

    def test_portfolio_risk_receipt_is_recomputed_from_mtm_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            equity_path = root / "portfolio-equity.csv"
            audit_path = root / "portfolio-risk.json"
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            rows = ["timestamp,equity,equity_type"]
            for offset in range(366):
                rows.append(
                    f"{(start + timedelta(days=offset)).isoformat()},"
                    f"{1000.0 * (1.004 ** offset)},mark_to_market"
                )
            equity_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            run = subprocess.run(
                [
                    sys.executable,
                    "scripts/portfolio_risk_audit.py",
                    "--equity",
                    str(equity_path),
                    "--output",
                    str(audit_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            replayed, sources = _load_portfolio_risk_audit(audit_path)
            self.assertEqual(replayed["status"], "PASS")
            self.assertIn("mark_to_market_equity", sources)

            tampered = json.loads(audit_path.read_text(encoding="utf-8"))
            tampered["aggregate"]["probability_reaching_annual_target"] = 0.123
            audit_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "does not match source replay"):
                _load_portfolio_risk_audit(audit_path)

    def test_portfolio_composition_reconciles_source_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "portfolio-contributions.csv"
            sleeve_pnl_path = Path(raw) / "portfolio-sleeve-pnl.csv"
            start = datetime(2025, 1, 2, tzinfo=timezone.utc)
            rows = ["timestamp,ALPHA_LONG,ALPHA_SHORT"]
            sleeve_pnl_rows = ["timestamp,ALPHA_LONG,ALPHA_SHORT"]
            prior_equity_fraction = 1.0
            for offset in range(365):
                variation = 0.0005 if offset % 2 == 0 else -0.0005
                long_contribution = 0.002 + variation
                short_contribution = 0.002 - variation
                rows.append(
                    f"{(start + timedelta(days=offset)).isoformat()},"
                    f"{long_contribution},{short_contribution}"
                )
                sleeve_pnl_rows.append(
                    f"{(start + timedelta(days=offset)).isoformat()},"
                    f"{long_contribution * prior_equity_fraction},"
                    f"{short_contribution * prior_equity_fraction}"
                )
                prior_equity_fraction *= 1.004
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            sleeve_pnl_path.write_text(
                "\n".join(sleeve_pnl_rows) + "\n", encoding="utf-8"
            )
            matrix = load_portfolio_contributions(
                path, ["ALPHA_LONG", "ALPHA_SHORT"]
            )
            sleeve_pnl = load_sleeve_daily_pnl(
                sleeve_pnl_path, ["ALPHA_LONG", "ALPHA_SHORT"]
            )
            equity = {
                "first_timestamp": "2025-01-01T00:00:00+00:00",
                "daily_returns": [0.004] * 365,
                "daily_returns_sha256": "a" * 64,
            }
            result = evaluate_portfolio_composition(
                matrix,
                equity,
                sleeve_pnl,
                {"ALPHA_LONG": 0.5, "ALPHA_SHORT": 0.5},
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(
                result["pairwise_daily_correlation_basis"],
                "self_financing_sleeve_daily_returns",
            )
            self.assertEqual(
                result["positive_pnl_share_basis"],
                "source_sleeve_daily_net_pnl_fraction_of_initial_portfolio_equity",
            )
            long_equity = short_equity = 0.5
            long_returns: list[float] = []
            short_returns: list[float] = []
            for long_pnl, short_pnl in zip(
                sleeve_pnl["daily_net_pnl"]["ALPHA_LONG"],
                sleeve_pnl["daily_net_pnl"]["ALPHA_SHORT"],
            ):
                long_returns.append(long_pnl / long_equity)
                short_returns.append(short_pnl / short_equity)
                long_equity += long_pnl
                short_equity += short_pnl
            expected_correlation = float(np.corrcoef(long_returns, short_returns)[0, 1])
            self.assertAlmostEqual(
                result["pairwise_daily_correlations"][0]["correlation"],
                expected_correlation,
            )
            positive_long = sum(
                max(0.0, value)
                for value in sleeve_pnl["daily_net_pnl"]["ALPHA_LONG"]
            )
            positive_short = sum(
                max(0.0, value)
                for value in sleeve_pnl["daily_net_pnl"]["ALPHA_SHORT"]
            )
            self.assertAlmostEqual(
                result["positive_pnl_share_by_sleeve"]["ALPHA_LONG"],
                positive_long / (positive_long + positive_short),
            )

            equity["daily_returns"][100] = 0.005
            drifted = evaluate_portfolio_composition(
                matrix,
                equity,
                sleeve_pnl,
                {"ALPHA_LONG": 0.5, "ALPHA_SHORT": 0.5},
            )
            self.assertEqual(drifted["status"], "FAIL")
            reconciliation = next(
                item
                for item in drifted["checks"]
                if item["name"] == "daily_mtm_return_reconciliation"
            )
            self.assertFalse(reconciliation["passed"])

            equity["daily_returns"][100] = 0.004
            sleeve_pnl["daily_net_pnl"]["ALPHA_LONG"][0] += 0.001
            forged_contribution = evaluate_portfolio_composition(
                matrix,
                equity,
                sleeve_pnl,
                {"ALPHA_LONG": 0.5, "ALPHA_SHORT": 0.5},
            )
            binding = next(
                item
                for item in forged_contribution["checks"]
                if item["name"] == "sleeve_contribution_binding"
            )
            self.assertFalse(binding["passed"])

            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "timestamp,ALPHA_LONG,ALPHA_SHORT",
                    "timestamp,ALPHA_SHORT,ALPHA_LONG",
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "exact order"):
                load_portfolio_contributions(path, ["ALPHA_LONG", "ALPHA_SHORT"])

    def test_portfolio_trade_template_matches_locked_loader_schema(self) -> None:
        header = Path("research/portfolio-trades.template.csv").read_text(
            encoding="utf-8"
        ).strip().split(",")
        self.assertEqual(header, TRADE_HEADER)

        exposure_header = Path("research/portfolio-exposure.template.csv").read_text(
            encoding="utf-8"
        ).strip().split(",")
        self.assertEqual(exposure_header, EXPOSURE_HEADER)

        manifest_template = json.loads(
            Path("research/portfolio-manifest.template.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("selection_bias_family_id", manifest_template)
        self.assertIn("selection_bias_inventory_id", manifest_template)
        self.assertGreaterEqual(len(manifest_template["sleeves"]), 2)
        for sleeve in manifest_template["sleeves"]:
            self.assertIn("selection_bias_family_id", sleeve)
            self.assertIn("selection_bias_inventory_id", sleeve)

    def test_portfolio_performance_recomputes_monthly_cost_and_leverage_gates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            equity_path = root / "equity.csv"
            exposure_path = root / "exposure.csv"
            accounting_path = root / "accounting.csv"
            trades_path = root / "trades.csv"
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            equity_rows = ["timestamp,equity,equity_type"]
            exposure_rows = [
                "timestamp,maximum_gross_leverage,maximum_absolute_net_leverage"
            ]
            accounting_rows = [
                "timestamp,gross_price_pnl,trading_fees,funding_pnl,modeled_slippage"
            ]
            equity_value = 1000.0
            for offset in range(366):
                timestamp = start + timedelta(days=offset)
                equity_rows.append(
                    f"{timestamp.isoformat()},{equity_value},mark_to_market"
                )
                if offset > 0:
                    exposure_rows.append(f"{timestamp.isoformat()},2.0,1.0")
                    previous_equity = equity_value / 1.004
                    equity_change = equity_value - previous_equity
                    accounting_rows.append(
                        f"{timestamp.isoformat()},{equity_change + 0.2},0.1,0.0,0.1"
                    )
                equity_value *= 1.004
            equity_path.write_text("\n".join(equity_rows) + "\n", encoding="utf-8")
            exposure_path.write_text(
                "\n".join(exposure_rows) + "\n", encoding="utf-8"
            )
            accounting_path.write_text(
                "\n".join(accounting_rows) + "\n", encoding="utf-8"
            )
            trade_rows = [",".join([
                "trade_id",
                "candidate",
                "symbol",
                "position_side",
                "opened_at",
                "closed_at",
                "gross_price_pnl",
                "trading_fees",
                "funding_pnl",
                "modeled_slippage",
                "entry_notional",
                "exit_notional",
                "maximum_leverage",
                "maximum_adverse_excursion",
                "exit_order_role",
                "exit_reason",
            ])]
            for index in range(100):
                opened = start + timedelta(days=index + 1, hours=1)
                closed = opened + timedelta(hours=1)
                candidate = "ALPHA_LONG" if index % 2 == 0 else "ALPHA_SHORT"
                side = "long" if index % 2 == 0 else "short"
                gross = 5.0 if index < 80 else -5.0
                trade_rows.append(
                    f"T{index},{candidate},BTCUSDT,{side},{opened.isoformat()},"
                    f"{closed.isoformat()},{gross},0.1,0.0,0.1,100.0,100.0,"
                    "1.0,0.02,exit,signal_exit"
                )
            recorded_final_equity = 1000.0 * (1.004**365)
            organic_net_pnl = 80 * 4.8 + 20 * -5.2
            accounting_cost_each = 365 * 0.1
            boundary_cost_each = accounting_cost_each - 100 * 0.1
            boundary_gross = (
                recorded_final_equity
                - 1000.0
                - organic_net_pnl
                + 2 * boundary_cost_each
            )
            boundary_open = start + timedelta(days=364, hours=20)
            boundary_close = start + timedelta(days=365)
            trade_rows.append(
                f"T_BOUNDARY,ALPHA_LONG,BTCUSDT,long,{boundary_open.isoformat()},"
                f"{boundary_close.isoformat()},{boundary_gross},{boundary_cost_each},0.0,"
                f"{boundary_cost_each},100.0,100.0,1.0,0.02,exit,force_exit"
            )
            trades_path.write_text("\n".join(trade_rows) + "\n", encoding="utf-8")
            loaded_trades = load_portfolio_trades(
                trades_path, ["ALPHA_LONG", "ALPHA_SHORT"]
            )
            loaded_exposure = load_portfolio_exposure(exposure_path)
            loaded_accounting = load_portfolio_accounting(accounting_path)
            loaded_equity = load_mark_to_market_equity(equity_path)
            safe_risk = {
                "aggregate": {
                    "probability_of_ruin_upper_confidence": 0.001,
                    "probability_terminal_loss_upper_confidence": 0.01,
                    "probability_reaching_annual_target_lower_confidence": 0.90,
                    "maximum_drawdown_p95": 0.10,
                }
            }
            locked_evidence = {
                "sample_role": "locked_forward",
                "pre_registered": True,
                "parameter_changes_after_start": 0,
                "registered_at_utc": "2024-12-01T00:00:00Z",
                "sample_started_at_utc": "2025-01-01T00:00:00Z",
            }
            execution_scopes = [
                {
                    "candidate": candidate,
                    "symbol": "BTCUSDT",
                    "position_side": side,
                    "order_role": role,
                    "fee_bps": 2.0,
                    "maximum_all_in_drag_bps": 5.0,
                }
                for candidate, side in (
                    ("ALPHA_LONG", "long"),
                    ("ALPHA_SHORT", "short"),
                )
                for role in ("entry", "exit")
            ]
            result = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["monthly_target_gate"]["status"], "PASS")
            self.assertEqual(result["organic_trades"], 100)
            self.assertGreater(result["stress_returns"]["simultaneous"], 0.0)
            self.assertAlmostEqual(
                result["maximum_trade_leverage_adjusted_mae"], 0.02
            )
            self.assertEqual(
                result["exposure_reconciliation"][
                    "maximum_gross_leverage_understatement"
                ],
                0.0,
            )
            self.assertEqual(
                result["boundary_force_exit_audit"][
                    "invalid_force_exit_trade_ids"
                ],
                [],
            )
            self.assertIsNone(result["daily_mtm_profit_factor"])
            self.assertGreater(result["daily_mtm_gross_profit"], 0.0)
            self.assertEqual(result["daily_mtm_gross_loss"], 0.0)
            self.assertFalse(result["stress_paths"]["simultaneous"]["ruined"])
            self.assertLessEqual(
                result["stress_paths"]["simultaneous"]["maximum_drawdown"], 0.20
            )

            recovered = _stressed_equity_path(
                [
                    {
                        "gross_price_pnl": -30.0,
                        "trading_fees": 0.0,
                        "funding_pnl": 0.0,
                        "modeled_slippage": 0.0,
                    },
                    {
                        "gross_price_pnl": 40.0,
                        "trading_fees": 0.0,
                        "funding_pnl": 0.0,
                        "modeled_slippage": 0.0,
                    },
                ],
                initial_equity=100.0,
                fee_multiplier=2.0,
                slippage_multiplier=2.0,
                adverse_funding=True,
            )
            self.assertAlmostEqual(recovered["terminal_return"], 0.10)
            self.assertAlmostEqual(recovered["maximum_drawdown"], 0.30)
            self.assertFalse(recovered["ruined"])
            insolvent_then_recovered = _stressed_equity_path(
                [
                    {
                        "gross_price_pnl": -110.0,
                        "trading_fees": 0.0,
                        "funding_pnl": 0.0,
                        "modeled_slippage": 0.0,
                    },
                    {
                        "gross_price_pnl": 120.0,
                        "trading_fees": 0.0,
                        "funding_pnl": 0.0,
                        "modeled_slippage": 0.0,
                    },
                ],
                initial_equity=100.0,
                fee_multiplier=2.0,
                slippage_multiplier=2.0,
                adverse_funding=True,
            )
            self.assertAlmostEqual(insolvent_then_recovered["terminal_return"], 0.10)
            self.assertEqual(insolvent_then_recovered["maximum_drawdown"], 1.0)
            self.assertTrue(insolvent_then_recovered["ruined"])

            loaded_trades[0]["gross_price_pnl"] -= 0.2
            loaded_trades[0]["trading_fees"] = 0.0
            loaded_trades[0]["modeled_slippage"] = 0.0
            undercharged = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            undercharged_checks = {
                item["name"]: item["passed"] for item in undercharged["checks"]
            }
            self.assertTrue(
                undercharged_checks["closed_trade_mtm_total_reconciliation"]
            )
            self.assertFalse(
                undercharged_checks["closed_trade_mtm_component_reconciliation"]
            )
            self.assertFalse(undercharged_checks["preregistered_execution_cost_floor"])
            loaded_trades[0]["gross_price_pnl"] += 0.2
            loaded_trades[0]["trading_fees"] = 0.1
            loaded_trades[0]["modeled_slippage"] = 0.1

            original_gross_exposure = loaded_exposure[1]["maximum_gross_leverage"]
            original_net_exposure = loaded_exposure[1][
                "maximum_absolute_net_leverage"
            ]
            loaded_exposure[1]["maximum_gross_leverage"] = 0.01
            loaded_exposure[1]["maximum_absolute_net_leverage"] = 0.01
            understated_exposure = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            exposure_check = next(
                item
                for item in understated_exposure["checks"]
                if item["name"] == "intraday_exposure_reconciliation"
            )
            self.assertFalse(exposure_check["passed"])
            self.assertGreater(
                exposure_check["actual"]["maximum_gross_leverage_understatement"],
                0.0,
            )
            loaded_exposure[1]["maximum_gross_leverage"] = original_gross_exposure
            loaded_exposure[1][
                "maximum_absolute_net_leverage"
            ] = original_net_exposure

            loaded_trades[-1]["closed_at"] -= timedelta(hours=1)
            early_force_exit = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            force_exit_check = next(
                item
                for item in early_force_exit["checks"]
                if item["name"] == "boundary_force_exit_integrity"
            )
            self.assertFalse(force_exit_check["passed"])
            loaded_trades[-1]["closed_at"] += timedelta(hours=1)

            loaded_trades[0]["opened_at"] = datetime(
                2024, 12, 31, 23, tzinfo=timezone.utc
            )
            misaligned = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            alignment = next(
                item
                for item in misaligned["checks"]
                if item["name"] == "trade_sample_alignment"
            )
            self.assertFalse(alignment["passed"])

            loaded_trades[0]["opened_at"] = start + timedelta(days=1, hours=1)
            loaded_accounting[0]["gross_price_pnl"] += 1.0
            unreconciled = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            reconciliation = next(
                item
                for item in unreconciled["checks"]
                if item["name"] == "daily_accounting_mtm_reconciliation"
            )
            self.assertFalse(reconciliation["passed"])

            loaded_accounting[0]["gross_price_pnl"] -= 1.0
            loaded_trades[-1]["gross_price_pnl"] += 1.0
            split_manipulated = evaluate_portfolio_performance(
                trades=loaded_trades,
                exposure=loaded_exposure,
                accounting=loaded_accounting,
                equity=loaded_equity,
                portfolio_risk_audit=safe_risk,
                evidence=locked_evidence,
                candidates=["ALPHA_LONG", "ALPHA_SHORT"],
                execution_scopes=execution_scopes,
            )
            trade_reconciliation = next(
                item
                for item in split_manipulated["checks"]
                if item["name"] == "closed_trade_mtm_total_reconciliation"
            )
            self.assertFalse(trade_reconciliation["passed"])

            bad_trades = trades_path.read_text(encoding="utf-8").replace(
                ",0.1,0.0,0.1,100.0,100.0,1.0,0.02,exit,signal_exit",
                ",-0.1,0.0,0.1,100.0,100.0,1.0,0.02,exit,signal_exit",
                1,
            )
            trades_path.write_text(bad_trades, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-negative"):
                load_portfolio_trades(
                    trades_path, ["ALPHA_LONG", "ALPHA_SHORT"]
                )

    def test_portfolio_execution_replays_raw_scope_costs_and_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "execution.csv"
            header = (
                "observation_id,signal_id,market_data_segment_id,observed_at,evidence_window_end_at,candidate,symbol,position_side,"
                "order_role,action_side,order_type,trigger_kind,probe_attempted,"
                "filled,cost_observed,all_in_drag_bps,limit_price,order_qty,"
                "queue_ahead_qty,fee_bps,first_depth_update_id,"
                "last_depth_update_id,first_agg_trade_id,last_agg_trade_id"
            )
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            rows = [header]
            for index in range(30):
                observed = start + timedelta(hours=1)
                rows.append(
                    f"O{index},S{index},SEGMENT_001,{observed.isoformat()},"
                    f"{(observed + timedelta(minutes=5)).isoformat()},ALPHA_LONG,BTCUSDT,long,"
                    f"entry,buy,maker_limit,breakout,1,{1 if index < 10 else 0},1,"
                    f"0.0,99.9,0.5,1.0,2.0,{1000 + 2 * index},{1001 + 2 * index},"
                    f"{2000 + 2 * index},{2001 + 2 * index}"
                )
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            raw_depth = Path(raw) / "depth.jsonl"
            raw_trades = Path(raw) / "agg_trades.jsonl"
            raw_features = Path(raw) / "book_features.jsonl"
            raw_market_manifest = Path(raw) / "market.manifest.json"
            raw_depth.write_text(
                json.dumps(
                    {
                        "kind": "depth",
                        "payload": {"s": "BTCUSDT", "U": 1000, "u": 1100},
                        "received_at": (start + timedelta(hours=1)).isoformat(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            raw_trades.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "kind": "agg_trade",
                            "payload": {
                                "s": "BTCUSDT",
                                "a": event_id,
                                "p": "99.9" if event_id < 2020 else "100.1",
                                "q": "2.0",
                                "m": event_id < 2020,
                                "T": int((start + timedelta(hours=1)).timestamp() * 1000),
                            },
                            "received_at": (start + timedelta(hours=1)).isoformat(),
                        }
                    )
                    for event_id in range(2000, 2101)
                )
                + "\n",
                encoding="utf-8",
            )
            raw_features.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "kind": "book_feature",
                            "symbol": "BTCUSDT",
                            "received_at": (
                                start
                                + timedelta(hours=1)
                                + (timedelta(minutes=1) if update_id % 2 else timedelta())
                            ).isoformat(),
                            "last_update_id": update_id,
                            "best_bid": 99.9,
                            "best_ask": 100.1,
                            "best_bid_qty": 1.0,
                            "best_ask_qty": 1.0,
                            "mid": 100.0,
                        }
                    )
                    for update_id in range(1000, 1060)
                )
                + "\n",
                encoding="utf-8",
            )
            raw_market_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "symbol": "BTCUSDT",
                        "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                        "protocol_sha256": {
                            relative: sha256(Path(relative).read_bytes()).hexdigest()
                            for relative in MARKET_DATA_PROTOCOL_FILES
                        },
                        "result": {
                            "started_at": start.isoformat(),
                            "ended_at": (start + timedelta(days=30)).isoformat(),
                        },
                        "files": [
                            {
                                "path": raw_depth.name,
                                "sha256": sha256(raw_depth.read_bytes()).hexdigest(),
                            },
                            {
                                "path": raw_trades.name,
                                "sha256": sha256(raw_trades.read_bytes()).hexdigest(),
                            },
                            {
                                "path": raw_features.name,
                                "sha256": sha256(raw_features.read_bytes()).hexdigest(),
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            manifest_path = Path(raw) / "execution.manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "SEALED",
                        "source_kind": "binance_usdtm_public_depth_and_aggtrade",
                        "portfolio_manifest_sha256": "d" * 64,
                        "portfolio_promotion_policy_sha256": "e" * 64,
                        "protocol_sha256": {
                            relative: sha256(Path(relative).read_bytes()).hexdigest()
                            for relative in PORTFOLIO_EXECUTION_PROTOCOL_FILES
                        },
                        "segments": [
                            {
                                "segment_id": "SEGMENT_001",
                                "path": path.name,
                                "sha256": sha256(path.read_bytes()).hexdigest(),
                                "market_data_manifest_path": raw_market_manifest.name,
                                "market_data_manifest_sha256": sha256(
                                    raw_market_manifest.read_bytes()
                                ).hexdigest(),
                                "previous_segment_sha256": None,
                                "rows": 30,
                                "first_observed_at": (start + timedelta(hours=1)).isoformat(),
                                "last_observed_at": (start + timedelta(hours=1)).isoformat(),
                                "sealed_at_utc": (
                                    start + timedelta(days=30)
                                ).isoformat(),
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            bundle = load_execution_observation_manifest(
                manifest_path,
                portfolio_manifest_sha256="d" * 64,
                portfolio_policy_sha256="e" * 64,
            )
            self.assertEqual(len(bundle["observations"]), 30)
            self.assertEqual(len(bundle["segments"]), 1)
            self.assertTrue(bundle["observations"][0]["replayed_filled"])
            self.assertEqual(
                bundle["observations"][0]["replayed_all_in_drag_bps"], 0.0
            )
            expected = [
                {
                    "candidate": "ALPHA_LONG",
                    "symbol": "BTCUSDT",
                    "position_side": "long",
                    "order_role": "entry",
                    "action_side": "buy",
                    "order_type": "maker_limit",
                    "trigger_kind": "breakout",
                    "fee_bps": 2.0,
                    "maximum_all_in_drag_bps": 5.0,
                }
            ]
            observations = bundle["observations"]
            result = evaluate_portfolio_execution(
                observations,
                expected,
                minimum_observations_per_scope=30,
                minimum_passive_fills_per_scope=10,
                coverage_start=start,
                coverage_end=start + timedelta(days=30),
            )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["scopes"][0]["fills"], 10)
            self.assertLess(result["scopes"][0]["p95_all_in_drag_bps"], 5.0)

            wrong_fee_scope = [dict(expected[0], fee_bps=1.0)]
            wrong_fee = evaluate_portfolio_execution(
                observations,
                wrong_fee_scope,
                minimum_observations_per_scope=30,
                minimum_passive_fills_per_scope=10,
                coverage_start=start,
                coverage_end=start + timedelta(days=30),
            )
            self.assertEqual(wrong_fee["status"], "FAIL")

            observations[0]["probe_attempted"] = False
            observations[0]["filled"] = False
            observations[0]["cost_observed"] = False
            observations[0]["all_in_drag_bps"] = None
            incomplete = evaluate_portfolio_execution(
                observations,
                expected,
                minimum_observations_per_scope=30,
                minimum_passive_fills_per_scope=10,
                coverage_start=start,
                coverage_end=start + timedelta(days=30),
            )
            self.assertEqual(incomplete["status"], "FAIL")
            self.assertLess(incomplete["scopes"][0]["trigger_coverage_ratio"], 1.0)

            bad_segment = Path(raw) / "execution-bad.csv"
            bad_segment.write_text(
                path.read_text(encoding="utf-8").replace(
                    ",2000,2001\n", ",2000,9999\n", 1
                ),
                encoding="utf-8",
            )
            bad_manifest_document = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            bad_manifest_document["segments"][0]["path"] = bad_segment.name
            bad_manifest_document["segments"][0]["sha256"] = sha256(
                bad_segment.read_bytes()
            ).hexdigest()
            bad_manifest = Path(raw) / "execution-bad.manifest.json"
            bad_manifest.write_text(
                json.dumps(bad_manifest_document) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "raw Binance events"):
                load_execution_observation_manifest(
                    bad_manifest,
                    portfolio_manifest_sha256="d" * 64,
                    portfolio_policy_sha256="e" * 64,
                )

            bad_cost_segment = Path(raw) / "execution-bad-cost.csv"
            bad_cost_segment.write_text(
                path.read_text(encoding="utf-8").replace(
                    ",0.0,99.9,0.5,1.0,2.0,", ",1.0,99.9,0.5,1.0,2.0,", 1
                ),
                encoding="utf-8",
            )
            bad_cost_document = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            bad_cost_document["segments"][0]["path"] = bad_cost_segment.name
            bad_cost_document["segments"][0]["sha256"] = sha256(
                bad_cost_segment.read_bytes()
            ).hexdigest()
            bad_cost_manifest = Path(raw) / "execution-bad-cost.manifest.json"
            bad_cost_manifest.write_text(
                json.dumps(bad_cost_document) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "does not replay"):
                load_execution_observation_manifest(
                    bad_cost_manifest,
                    portfolio_manifest_sha256="d" * 64,
                    portfolio_policy_sha256="e" * 64,
                )

            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_execution_observation_manifest(
                    manifest_path,
                    portfolio_manifest_sha256="d" * 64,
                    portfolio_policy_sha256="e" * 64,
                )

    def test_portfolio_execution_audit_cli_replays_sealed_segments(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            portfolio_manifest = root / "portfolio.json"
            observation_segment = root / "segment.csv"
            observation_manifest = root / "observations.manifest.json"
            raw_depth = root / "depth.jsonl"
            raw_trades = root / "agg_trades.jsonl"
            raw_features = root / "book_features.jsonl"
            raw_market_manifest = root / "market.manifest.json"
            equity = root / "equity.csv"
            output = root / "audit.json"
            portfolio_manifest.write_text(
                json.dumps(
                    {
                        "sleeves": [
                            {
                                "candidate": "ALPHA_LONG",
                                "symbols": ["BTCUSDT"],
                                "position_sides": ["long"],
                                "orders": {
                                    role: {
                                        "order_type": "maker_limit",
                                        "fee_bps": 2.0,
                                        "maximum_all_in_drag_bps": 5.0,
                                        "trigger_kind": f"{role}_signal",
                                    }
                                    for role in ("entry", "exit", "protective_stop")
                                },
                            }
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            header = (
                "observation_id,signal_id,market_data_segment_id,observed_at,evidence_window_end_at,"
                "candidate,symbol,position_side,order_role,action_side,order_type,"
                "trigger_kind,probe_attempted,filled,cost_observed,all_in_drag_bps,"
                "limit_price,order_qty,queue_ahead_qty,fee_bps,"
                "first_depth_update_id,last_depth_update_id,first_agg_trade_id,"
                "last_agg_trade_id"
            )
            observed = datetime(2025, 1, 1, 1, tzinfo=timezone.utc)
            rows = [header]
            row_index = 0
            for role in ("entry", "exit", "protective_stop"):
                action = "buy" if role == "entry" else "sell"
                for index in range(30):
                    rows.append(
                        f"O{row_index},S{row_index},SEGMENT_001,{observed.isoformat()},"
                        f"{(observed + timedelta(minutes=1)).isoformat()},"
                        f"ALPHA_LONG,BTCUSDT,long,{role},{action},maker_limit,"
                        f"{role}_signal,1,{1 if index < 10 else 0},1,0.0,"
                        f"{99.9 if action == 'buy' else 100.1},0.5,1.0,2.0,"
                        f"{1000 + 2 * row_index},{1001 + 2 * row_index},"
                        f"{2000 + 2 * row_index},{2001 + 2 * row_index}"
                    )
                    row_index += 1
            observation_segment.write_text(
                "\n".join(rows) + "\n", encoding="utf-8"
            )
            raw_depth.write_text(
                json.dumps(
                    {
                        "kind": "depth",
                        "payload": {"s": "BTCUSDT", "U": 1000, "u": 1200},
                        "received_at": observed.isoformat(),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            raw_trades.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "kind": "agg_trade",
                            "payload": {
                                "s": "BTCUSDT",
                                "a": event_id,
                                "p": (
                                    "99.9"
                                    if (event_id - 2000) // 2 < 30
                                    else "100.1"
                                ),
                                "q": "2.0",
                                "m": (
                                    ((event_id - 2000) // 2) % 30 < 10
                                    if (event_id - 2000) // 2 < 30
                                    else not (((event_id - 2000) // 2) % 30 < 10)
                                ),
                                "T": int(observed.timestamp() * 1000),
                            },
                            "received_at": observed.isoformat(),
                        }
                    )
                    for event_id in range(2000, 2180)
                )
                + "\n",
                encoding="utf-8",
            )
            raw_features.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "kind": "book_feature",
                            "symbol": "BTCUSDT",
                            "received_at": (
                                observed
                                + (timedelta(minutes=1) if update_id % 2 else timedelta())
                            ).isoformat(),
                            "last_update_id": update_id,
                            "best_bid": 99.9,
                            "best_ask": 100.1,
                            "best_bid_qty": 1.0,
                            "best_ask_qty": 1.0,
                            "mid": 100.0,
                        }
                    )
                    for update_id in range(1000, 1180)
                )
                + "\n",
                encoding="utf-8",
            )
            raw_market_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "symbol": "BTCUSDT",
                        "probe_kind": "public_market_data_only_no_orders_or_api_keys",
                        "protocol_sha256": {
                            relative: sha256(Path(relative).read_bytes()).hexdigest()
                            for relative in MARKET_DATA_PROTOCOL_FILES
                        },
                        "result": {
                            "started_at": (
                                observed - timedelta(minutes=1)
                            ).isoformat(),
                            "ended_at": (observed + timedelta(minutes=1)).isoformat(),
                        },
                        "files": [
                            {
                                "path": raw_depth.name,
                                "sha256": sha256(raw_depth.read_bytes()).hexdigest(),
                            },
                            {
                                "path": raw_trades.name,
                                "sha256": sha256(raw_trades.read_bytes()).hexdigest(),
                            },
                            {
                                "path": raw_features.name,
                                "sha256": sha256(raw_features.read_bytes()).hexdigest(),
                            },
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            policy_path = Path("research/portfolio-promotion-policy.json")
            observation_manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "SEALED",
                        "source_kind": "binance_usdtm_public_depth_and_aggtrade",
                        "portfolio_manifest_sha256": sha256(
                            portfolio_manifest.read_bytes()
                        ).hexdigest(),
                        "portfolio_promotion_policy_sha256": sha256(
                            policy_path.read_bytes()
                        ).hexdigest(),
                        "protocol_sha256": {
                            relative: sha256(Path(relative).read_bytes()).hexdigest()
                            for relative in PORTFOLIO_EXECUTION_PROTOCOL_FILES
                        },
                        "segments": [
                            {
                                "segment_id": "SEGMENT_001",
                                "path": observation_segment.name,
                                "sha256": sha256(
                                    observation_segment.read_bytes()
                                ).hexdigest(),
                                "market_data_manifest_path": raw_market_manifest.name,
                                "market_data_manifest_sha256": sha256(
                                    raw_market_manifest.read_bytes()
                                ).hexdigest(),
                                "previous_segment_sha256": None,
                                "rows": 90,
                                "first_observed_at": observed.isoformat(),
                                "last_observed_at": observed.isoformat(),
                                "sealed_at_utc": (
                                    observed + timedelta(hours=1)
                                ).isoformat(),
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            equity.write_text(
                "timestamp,equity,equity_type\n"
                "2025-01-01T00:00:00+00:00,1000,mark_to_market\n"
                "2025-01-02T00:00:00+00:00,1010,mark_to_market\n",
                encoding="utf-8",
            )
            command = [
                sys.executable,
                "scripts/portfolio_execution_audit.py",
                "--manifest",
                str(portfolio_manifest),
                "--observation-manifest",
                str(observation_manifest),
                "--portfolio-equity",
                str(equity),
                "--output",
                str(output),
            ]
            completed = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            audit = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "PASS")
            self.assertEqual(audit["observation_rows"], 90)

            observation_segment.write_text(
                observation_segment.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            tampered = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("digest mismatch", tampered.stderr or tampered.stdout)

    @staticmethod
    def _passing_portfolio_inputs() -> tuple[dict, dict, dict, dict, str, str]:
        digest = "d" * 64
        policy_digest = "e" * 64
        order_specs = {
            "entry": {
                "order_type": "maker_limit",
                "fee_bps": 2.0,
                "maximum_all_in_drag_bps": 10.0,
                "trigger_kind": "strategy_entry_attempt",
            },
            "exit": {
                "order_type": "aggressive_limit",
                "fee_bps": 5.0,
                "maximum_all_in_drag_bps": 15.0,
                "trigger_kind": "strategy_exit_attempt",
            },
            "protective_stop": {
                "order_type": "market",
                "fee_bps": 5.0,
                "maximum_all_in_drag_bps": 30.0,
                "trigger_kind": "strategy_stop_attempt",
            },
        }
        manifest = {
            "schema_version": 1,
            "candidate": "PORTFOLIO_001",
            "selection_bias_family_id": "PORTFOLIO_001",
            "selection_bias_inventory_id": "PORTFOLIO_001_SELECTION_V1",
            "registered_at_utc": "2024-12-31T00:00:00Z",
            "portfolio_promotion_policy_sha256": policy_digest,
            "sleeves": [
                {
                    "candidate": "ALPHA_LONG",
                    "selection_bias_family_id": "ALPHA_LONG",
                    "selection_bias_inventory_id": "ALPHA_LONG_SELECTION_V1",
                    "allocation_weight": 0.5,
                    "symbols": ["BTCUSDT"],
                    "position_sides": ["long"],
                    "orders": order_specs,
                },
                {
                    "candidate": "ALPHA_SHORT",
                    "selection_bias_family_id": "ALPHA_SHORT",
                    "selection_bias_inventory_id": "ALPHA_SHORT_SELECTION_V1",
                    "allocation_weight": 0.5,
                    "symbols": ["BTCUSDT"],
                    "position_sides": ["short"],
                    "orders": order_specs,
                },
            ],
        }

        def selection_bias_audit(
            trial_ids: list[str],
            observations: int,
            *,
            single_preregistered: bool = False,
            selected_return_sha256: str = "3" * 64,
            family_id: str,
            inventory_id: str,
        ) -> dict:
            trials = len(trial_ids)
            return {
                "schema_version": 1,
                "status": "PASS",
                "policy": asdict(SelectionBiasPolicy()),
                "protocol_current": True,
                "protocol_sha256": {
                    "quant/research/selection_bias.py": "9" * 64,
                    "scripts/selection_bias_audit.py": "8" * 64,
                },
                "attempted_trials": trials,
                "trial_ids": trial_ids,
                "selected_trial_id": trial_ids[0],
                "selected_return_sha256": selected_return_sha256,
                "trial_return_matrix_sha256": "f" * 64,
                "source": {
                    "path": "reports/test-trial-returns.csv",
                    "sha256": "6" * 64,
                    "timestamp_column": "timestamp",
                    "first_timestamp": "2025-01-02T00:00:00+00:00",
                    "last_timestamp": "2026-01-01T00:00:00+00:00",
                    "rows": observations,
                },
                "trial_inventory_complete": True,
                "pre_registered_single_trial": single_preregistered,
                "trial_inventory": {
                    "path": "research/test-trial-inventory.json",
                    "sha256": "7" * 64,
                    "validation": {
                        "status": "PASS",
                        "failures": [],
                        "inventory_id": inventory_id,
                        "family_id": family_id,
                        "selected_trial_id": trial_ids[0],
                        "attempted_trials": trials,
                        "pre_registered_single_trial": single_preregistered,
                    },
                },
                "deflated_sharpe": {
                    "probability": 0.99,
                    "observations": observations,
                    "trials": trials,
                },
                "pbo": {
                    "status": (
                        "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL"
                        if single_preregistered
                        else "CALCULATED"
                    ),
                    "probability": 0.0 if single_preregistered else 0.05,
                    "slices": None if single_preregistered else 8,
                    "trials": trials,
                    "observations": observations,
                },
                "checks": [
                    {"name": name, "passed": True}
                    for name in (
                        "trial_inventory_complete",
                        "minimum_observations",
                        "deflated_sharpe_probability",
                        "probability_backtest_overfitting",
                    )
                ],
            }

        def portfolio_risk_audit() -> dict:
            _, ruin_upper = wilson_bounds(0, 5_000, 0.95)
            _, loss_upper = wilson_bounds(200, 5_000, 0.95)
            target_lower, _ = wilson_bounds(3_000, 5_000, 0.95)
            stress_scenarios = []
            for name, window_days, repeats in (
                ("worst_day_x3", 1, 3),
                ("worst_7d_x4", 7, 4),
                ("worst_28d_x2", 28, 2),
            ):
                terminal_return = 0.98**repeats - 1.0
                stress_scenarios.append(
                    {
                        "name": name,
                        "source_window_days": window_days,
                        "repeats": repeats,
                        "source_start_return_index": 10,
                        "source_block_return": -0.02,
                        "stressed_days": window_days * repeats,
                        "terminal_return": terminal_return,
                        "maximum_drawdown": -terminal_return,
                    }
                )
            deterministic_drawdown = max(
                item["maximum_drawdown"] for item in stress_scenarios
            )
            deterministic_terminal = min(
                item["terminal_return"] for item in stress_scenarios
            )
            return {
                "schema_version": 1,
                "status": "PASS",
                "policy": asdict(PortfolioRiskPolicy()),
                "protocol_current": True,
                "protocol_sha256": {
                    "quant/research/portfolio_risk.py": "6" * 64,
                    "scripts/portfolio_risk_audit.py": "5" * 64,
                },
                "source": {
                    "path": "reports/test-portfolio-equity.csv",
                    "sha256": "4" * 64,
                },
                "equity": {
                    "first_timestamp": "2025-01-01T00:00:00+00:00",
                    "last_timestamp": "2026-01-01T00:00:00+00:00",
                    "snapshots": 366,
                    "daily_returns_sha256": "3" * 64,
                    "historical_maximum_drawdown": 0.10,
                },
                "observations": 365,
                "scenarios": [
                    {
                        "block_days": block_days,
                        "simulations": 5_000,
                        "binomial_confidence_level": 0.95,
                        "event_counts": {
                            "ruin": 0,
                            "drawdown_exceeds_threshold": 100,
                            "terminal_loss": 200,
                            "reaches_annual_target": 3_000,
                        },
                        "probability_of_ruin": 0.0,
                        "probability_of_ruin_upper_confidence": ruin_upper,
                        "probability_drawdown_exceeds_threshold": 0.02,
                        "probability_terminal_loss": 0.04,
                        "probability_terminal_loss_upper_confidence": loss_upper,
                        "probability_reaching_annual_target": 0.60,
                        "probability_reaching_annual_target_lower_confidence": target_lower,
                        "terminal_return_p05": 0.10,
                        "maximum_drawdown_p95": 0.10,
                    }
                    for block_days in (7, 28)
                ],
                "aggregate": {
                    "probability_of_ruin": 0.0,
                    "probability_of_ruin_upper_confidence": ruin_upper,
                    "probability_drawdown_exceeds_threshold": 0.02,
                    "probability_terminal_loss": 0.04,
                    "probability_terminal_loss_upper_confidence": loss_upper,
                    "probability_reaching_annual_target": 0.60,
                    "probability_reaching_annual_target_lower_confidence": target_lower,
                    "terminal_return_p05": 0.10,
                    "maximum_drawdown_p95": 0.10,
                },
                "deterministic_stress": {
                    "scenarios": stress_scenarios,
                    "maximum_drawdown": deterministic_drawdown,
                    "worst_terminal_return": deterministic_terminal,
                },
                "checks": [
                    {
                        "name": "minimum_daily_returns",
                        "passed": True,
                        "actual": 365,
                        "required": 365,
                    },
                    {
                        "name": "historical_maximum_drawdown",
                        "passed": True,
                        "actual": 0.10,
                        "required": 0.10,
                    },
                    {
                        "name": "probability_of_ruin",
                        "passed": True,
                        "actual": ruin_upper,
                        "required": 0.005,
                    },
                    {
                        "name": "probability_terminal_loss",
                        "passed": True,
                        "actual": loss_upper,
                        "required": 0.05,
                    },
                    {
                        "name": "probability_reaching_annual_target",
                        "passed": True,
                        "actual": target_lower,
                        "required": 0.0,
                    },
                    {
                        "name": "bootstrap_maximum_drawdown_p95",
                        "passed": True,
                        "actual": 0.10,
                        "required": 0.10,
                    },
                    {
                        "name": "deterministic_tail_stress_drawdown",
                        "passed": True,
                        "actual": deterministic_drawdown,
                        "required": 0.10,
                    },
                ],
            }

        def portfolio_composition_audit() -> dict:
            pairwise = [
                {
                    "left": "ALPHA_LONG",
                    "right": "ALPHA_SHORT",
                    "correlation": -0.2,
                }
            ]
            shares = {"ALPHA_LONG": 0.55, "ALPHA_SHORT": 0.45}
            return {
                "schema_version": 1,
                "status": "PASS",
                "protocol_current": True,
                "protocol_sha256": {
                    "quant/research/portfolio_composition.py": "2" * 64,
                    "quant/research/selection_bias.py": "3" * 64,
                    "scripts/portfolio_promotion_report.py": "1" * 64,
                },
                "source": {
                    "path": "reports/test-portfolio-contributions.csv",
                    "sha256": "0" * 64,
                },
                "sleeve_pnl_source": {
                    "path": "reports/test-portfolio-sleeve-pnl.csv",
                    "sha256": "f" * 64,
                },
                "candidate_order": ["ALPHA_LONG", "ALPHA_SHORT"],
                "observations": 365,
                "first_timestamp": "2025-01-02T00:00:00+00:00",
                "last_timestamp": "2026-01-01T00:00:00+00:00",
                "portfolio_returns_sha256": "a" * 64,
                "equity_daily_returns_sha256": "3" * 64,
                "maximum_daily_reconciliation_error": 0.0,
                "maximum_daily_sleeve_pnl_reconciliation_error": 0.0,
                "maximum_sleeve_contribution_binding_error": 0.0,
                "sleeve_net_pnl_fraction_sha256": {
                    "ALPHA_LONG": "1" * 64,
                    "ALPHA_SHORT": "2" * 64,
                },
                "sleeve_daily_return_sha256": {
                    "ALPHA_LONG": "4" * 64,
                    "ALPHA_SHORT": "5" * 64,
                },
                "minimum_sleeve_equity_fraction": 0.4,
                "pairwise_daily_correlation_basis": "self_financing_sleeve_daily_returns",
                "pairwise_daily_correlations": pairwise,
                "positive_pnl_share_basis": "source_sleeve_daily_net_pnl_fraction_of_initial_portfolio_equity",
                "positive_pnl_share_by_sleeve": shares,
                "checks": [
                    {"name": name, "passed": True}
                    for name in (
                        "exact_mtm_date_alignment",
                        "daily_mtm_return_reconciliation",
                        "sleeve_pnl_exact_mtm_date_alignment",
                        "sleeve_pnl_mtm_reconciliation",
                        "sleeve_contribution_binding",
                        "self_financing_sleeve_return_reconstruction",
                        "pairwise_self_financing_return_correlations_defined",
                        "source_net_pnl_positive_share_available",
                    )
                ],
            }

        def portfolio_performance_audit() -> dict:
            monthly_gate = {
                "status": "PASS",
                "policy": asdict(PORTFOLIO_MONTHLY_POLICY),
                "checks": [
                    {"name": name, "passed": True}
                    for name in sorted(REQUIRED_MONTHLY_CHECK_NAMES)
                ],
            }
            return {
                "schema_version": 1,
                "status": "PASS",
                "protocol_current": True,
                "protocol_sha256": {
                    "quant/research/equity.py": "4" * 64,
                    "quant/research/monthly_target.py": "3" * 64,
                    "quant/research/portfolio_performance.py": "2" * 64,
                    "scripts/portfolio_promotion_report.py": "1" * 64,
                },
                "sources": {
                    "trades": {
                        "path": "reports/test-portfolio-trades.csv",
                        "sha256": "6" * 64,
                    },
                    "exposure": {
                        "path": "reports/test-portfolio-exposure.csv",
                        "sha256": "5" * 64,
                    },
                    "accounting": {
                        "path": "reports/test-portfolio-accounting.csv",
                        "sha256": "7" * 64,
                    },
                },
                "candidate_order": ["ALPHA_LONG", "ALPHA_SHORT"],
                "trade_rows": 100,
                "organic_trades": 100,
                "accounting_rows": 365,
                "maximum_daily_accounting_reconciliation_error": 0.0,
                "closed_trade_mtm_total_reconciliation_error": 0.0,
                "closed_trade_mtm_component_reconciliation_errors": {
                    "gross_price_pnl": 0.0,
                    "trading_fees": 0.0,
                    "funding_pnl": 0.0,
                    "modeled_slippage": 0.0,
                },
                "execution_cost_floor": {
                    "trades_evaluated": 100,
                    "total_trades": 100,
                    "duplicate_scope_roles": False,
                    "minimum_total_trading_fees": 4.0,
                    "minimum_total_all_in_cost": 10.0,
                    "recorded_total_trading_fees": 10.0,
                    "recorded_total_modeled_slippage": 10.0,
                    "maximum_trade_fee_shortfall": 0.0,
                    "maximum_trade_all_in_shortfall": 0.0,
                },
                "monthly_target_gate": monthly_gate,
                "daily_mtm_profit_factor": 1.5,
                "daily_mtm_gross_profit": 30.0,
                "daily_mtm_gross_loss": 20.0,
                "maximum_gross_leverage": 1.0,
                "maximum_absolute_net_leverage": 1.0,
                "maximum_trade_leverage_adjusted_mae": 0.02,
                "exposure_reconciliation": {
                    "rows_evaluated": 365,
                    "reported_rows": 365,
                    "reconstructed_maximum_gross_leverage": 0.5,
                    "reconstructed_maximum_absolute_net_leverage": 0.5,
                    "maximum_gross_leverage_understatement": 0.0,
                    "maximum_absolute_net_leverage_understatement": 0.0,
                    "notional_convention": "max(entry_notional, exit_notional) held for the full trade interval",
                    "equity_denominator": "min(start_of_day_mtm_equity, end_of_day_mtm_equity)",
                },
                "boundary_force_exit_audit": {
                    "force_exit_trades": 1,
                    "invalid_force_exit_trade_ids": [],
                    "required_close_at_utc": "2026-01-01T00:00:00+00:00",
                },
                "liquidations": 0,
                "stress_returns": {
                    "double_fee": 0.02,
                    "double_slippage": 0.02,
                    "adverse_funding": 0.02,
                    "simultaneous": 0.01,
                },
                "stress_paths": {
                    name: {
                        "terminal_return": terminal_return,
                        "maximum_drawdown": 0.05,
                        "minimum_equity_fraction": 0.95,
                        "ruined": False,
                    }
                    for name, terminal_return in {
                        "double_fee": 0.02,
                        "double_slippage": 0.02,
                        "adverse_funding": 0.02,
                        "simultaneous": 0.01,
                    }.items()
                },
                "checks": [
                    {"name": name, "passed": True}
                    for name in (
                        "candidate_trade_inventory",
                        "daily_exposure_alignment",
                        "intraday_exposure_reconciliation",
                        "daily_accounting_alignment",
                        "daily_accounting_mtm_reconciliation",
                        "trade_sample_alignment",
                        "boundary_force_exit_integrity",
                        "closed_trade_mtm_total_reconciliation",
                        "closed_trade_mtm_component_reconciliation",
                        "preregistered_execution_cost_floor",
                        "monthly_target_gate",
                        "daily_mtm_profit_factor",
                        "simultaneous_cost_funding_stress",
                        "simultaneous_cost_funding_stress_drawdown",
                    )
                ],
            }

        performance_audit = portfolio_performance_audit()

        report = {
            "portfolio_manifest_sha256": digest,
            "portfolio_promotion_policy_sha256": policy_digest,
            "monthly_target_gate": json.loads(
                json.dumps(performance_audit["monthly_target_gate"])
            ),
            "evidence": {
                "sample_role": "locked_forward",
                "pre_registered": True,
                "parameter_changes_after_start": 0,
                "registered_at_utc": "2024-12-31T00:00:00Z",
                "sample_started_at_utc": "2025-01-01T00:00:00Z",
                "sample_ended_at_utc": "2026-01-01T00:00:00Z",
            },
            "data_integrity": {"status": "PASS"},
            "common_daily_observations": 365,
            "selection_bias_audit": selection_bias_audit(
                ["PORTFOLIO_001", "PORTFOLIO_001_ALTERNATE"],
                365,
                family_id="PORTFOLIO_001",
                inventory_id="PORTFOLIO_001_SELECTION_V1",
            ),
            "portfolio_risk_audit": portfolio_risk_audit(),
            "portfolio_composition_audit": portfolio_composition_audit(),
            "portfolio_performance_audit": performance_audit,
            "maximum_gross_leverage": 1.0,
            "maximum_absolute_net_leverage": 1.0,
            "maximum_trade_leverage_adjusted_mae": 0.02,
            "liquidations": 0,
            "simultaneous_cost_funding_stress_net_return": 0.01,
            "simultaneous_cost_funding_stress_max_drawdown": 0.05,
            "pairwise_daily_correlations": [
                {"left": "ALPHA_LONG", "right": "ALPHA_SHORT", "correlation": -0.2}
            ],
            "positive_pnl_share_by_sleeve": {"ALPHA_LONG": 0.55, "ALPHA_SHORT": 0.45},
        }
        sleeve_reports = {
            candidate: {
                "candidate": candidate,
                "status": "PASS",
                "protocol_current": True,
                "data_integrity": {"status": "PASS"},
                "evidence": {
                    "sample_role": "locked_forward",
                    "pre_registered": True,
                    "parameter_changes_after_start": 0,
                    "registered_at_utc": "2024-12-31T00:00:00Z",
                    "sample_started_at_utc": "2025-01-01T00:00:00Z",
                    "sample_ended_at_utc": "2026-01-01T00:00:00Z",
                    "daily_net_pnl_fraction_sha256": (
                        "1" * 64 if candidate == "ALPHA_LONG" else "2" * 64
                    ),
                },
                "bias_audit": {
                    "candidate": candidate,
                    "status": "PASS",
                    "protocol_current": True,
                    "protocol_sha256": {"strategy.py": "a" * 64},
                    "lookahead": {
                        "passed": True,
                        "biased_entry_signals": 0,
                        "biased_exit_signals": 0,
                        "artifact_sha256": "b" * 64,
                    },
                    "recursive": {
                        "passed": True,
                        "indicator_lookahead_bias": False,
                        "artifact_sha256": "c" * 64,
                    },
                },
                "selection_bias_audit": selection_bias_audit(
                    [candidate],
                    365,
                    single_preregistered=True,
                    selected_return_sha256=(
                        "4" * 64 if candidate == "ALPHA_LONG" else "5" * 64
                    ),
                    family_id=candidate,
                    inventory_id=f"{candidate}_SELECTION_V1",
                ),
            }
            for candidate in ("ALPHA_LONG", "ALPHA_SHORT")
        }
        scopes, failures = expected_execution_scopes(manifest)
        if failures:
            raise AssertionError(failures)
        audited_scopes = []
        for scope in scopes:
            maker = scope["order_type"] == "maker_limit"
            audited_scopes.append(
                {
                    **scope,
                    "status": "PASS",
                    "signal_triggered_observations": 30,
                    "fills": 10 if maker else 30,
                    "trigger_coverage_ratio": 1.0,
                    "cost_coverage_ratio": 1.0,
                    "raw_replay_coverage_ratio": 1.0,
                    "mean_all_in_drag_bps": scope["maximum_all_in_drag_bps"] - 2.0,
                    "p95_all_in_drag_bps": scope["maximum_all_in_drag_bps"] - 1.0,
                }
            )
        execution = {
            "portfolio_manifest_sha256": digest,
            "portfolio_promotion_policy_sha256": policy_digest,
            "status": "PASS",
            "evidence_purpose": "live_promotion_candidate",
            "protocol_current": True,
            "protocol_sha256": {
                relative: f"{index + 1:x}" * 64
                for index, relative in enumerate(PORTFOLIO_EXECUTION_PROTOCOL_FILES)
            },
            "sources": {
                "observation_manifest": {
                    "path": "reports/test-portfolio-execution-observations.manifest.json",
                    "sha256": "a" * 64,
                    "segments": [
                        {
                            "segment_id": "SEGMENT_001",
                            "path": "reports/test-portfolio-execution-observations.csv",
                            "sha256": "c" * 64,
                            "market_data_manifest": {
                                "path": "reports/test-market-data-manifest.json",
                                "sha256": "d" * 64,
                                "files": [
                                    {
                                        "path": "reports/test-depth.jsonl.gz",
                                        "sha256": "e" * 64,
                                    },
                                    {
                                        "path": "reports/test-agg-trades.jsonl.gz",
                                        "sha256": "f" * 64,
                                    },
                                ],
                            },
                        }
                    ],
                },
                "mark_to_market_equity": {
                    "path": "reports/test-portfolio-equity.csv",
                    "sha256": "b" * 64,
                },
            },
            "observation_rows": 180,
            "scopes": audited_scopes,
            "checks": [
                {"name": name, "passed": True}
                for name in (
                    "exact_execution_scope_inventory",
                    "execution_observations_within_mtm_sample",
                    "all_execution_scopes_pass",
                )
            ],
        }
        return manifest, report, sleeve_reports, execution, digest, policy_digest

    def test_portfolio_promotion_requires_every_long_short_execution_path(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        passed = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertEqual(len(passed["expected_execution_scopes"]), 6)

        execution["scopes"] = [
            item
            for item in execution["scopes"]
            if not (
                item["candidate"] == "ALPHA_SHORT"
                and item["position_side"] == "short"
                and item["order_role"] == "protective_stop"
            )
        ]
        missing_stop = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in missing_stop["checks"] if not item["passed"]}
        self.assertNotEqual(missing_stop["status"], "PASS")
        self.assertIn("complete_execution_path_evidence", failed)

    def test_portfolio_promotion_requires_current_bias_audit_for_every_sleeve(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        sleeves["ALPHA_SHORT"].pop("bias_audit")
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("current_sleeve_bias_audits", failed)

    def test_portfolio_promotion_requires_source_bound_sleeve_contributions(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["portfolio_composition_audit"][
            "sleeve_net_pnl_fraction_sha256"
        ]["ALPHA_SHORT"] = "3" * 64
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("standalone_sleeve_promotion", failed)

        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        composition = report["portfolio_composition_audit"]
        composition["maximum_sleeve_contribution_binding_error"] = 0.001
        next(
            item
            for item in composition["checks"]
            if item["name"] == "sleeve_contribution_binding"
        )["passed"] = False
        forged = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        forged_failed = {
            item["name"] for item in forged["checks"] if not item["passed"]
        }
        self.assertNotEqual(forged["status"], "PASS")
        self.assertIn("portfolio_composition_source_replay", forged_failed)

    def test_portfolio_promotion_rejects_profitable_stress_path_with_excess_drawdown(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        performance = report["portfolio_performance_audit"]
        performance["stress_paths"]["simultaneous"]["maximum_drawdown"] = 0.25
        drawdown_check = next(
            item
            for item in performance["checks"]
            if item["name"] == "simultaneous_cost_funding_stress_drawdown"
        )
        drawdown_check["passed"] = False
        drawdown_check["actual"] = 0.25
        report["simultaneous_cost_funding_stress_max_drawdown"] = 0.25
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("simultaneous_cost_funding_stress_drawdown", failed)

        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["portfolio_performance_audit"]["stress_returns"]["simultaneous"] = "invalid"
        malformed = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        malformed_failed = {
            item["name"] for item in malformed["checks"] if not item["passed"]
        }
        self.assertEqual(malformed["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", malformed_failed)

    def test_portfolio_promotion_rejects_undercharged_execution_cost_ledger(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        performance = report["portfolio_performance_audit"]
        performance["execution_cost_floor"]["maximum_trade_fee_shortfall"] = 0.01
        performance["closed_trade_mtm_component_reconciliation_errors"][
            "trading_fees"
        ] = 0.001
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("preregistered_execution_cost_floor", failed)

    def test_portfolio_promotion_rejects_excess_intratrade_leveraged_mae(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["maximum_trade_leverage_adjusted_mae"] = 0.25
        report["portfolio_performance_audit"][
            "maximum_trade_leverage_adjusted_mae"
        ] = 0.25
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("maximum_intratrade_leverage_adjusted_mae", failed)

    def test_portfolio_promotion_rejects_underreported_intraday_exposure(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        reconciliation = report["portfolio_performance_audit"][
            "exposure_reconciliation"
        ]
        reconciliation["maximum_gross_leverage_understatement"] = 0.25
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("intraday_exposure_reconciliation", failed)

    def test_portfolio_promotion_rejects_early_force_exit_reclassification(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["portfolio_performance_audit"]["boundary_force_exit_audit"][
            "invalid_force_exit_trade_ids"
        ] = ["LOSS_RELABELED_FORCE_EXIT"]
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("boundary_force_exit_integrity", failed)

    def test_portfolio_promotion_rejects_weak_daily_mtm_profit_factor(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        performance = report["portfolio_performance_audit"]
        performance["daily_mtm_profit_factor"] = 1.0
        performance["daily_mtm_gross_profit"] = 20.0
        performance["daily_mtm_gross_loss"] = 20.0
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_performance_source_replay", failed)
        self.assertIn("daily_mtm_profit_factor", failed)

    def test_portfolio_promotion_rejects_selection_bias_or_hidden_trials(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["selection_bias_audit"]["deflated_sharpe"]["probability"] = 0.80
        report["selection_bias_audit"]["checks"][2]["passed"] = False
        sleeves["ALPHA_SHORT"]["selection_bias_audit"]["trial_inventory_complete"] = False
        sleeves["ALPHA_SHORT"]["selection_bias_audit"]["checks"][0]["passed"] = False
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_selection_bias", failed)
        self.assertIn("current_sleeve_selection_bias_audits", failed)

    def test_portfolio_promotion_binds_selection_inventory_identity_to_manifest(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["selection_bias_audit"]["trial_inventory"]["validation"][
            "inventory_id"
        ] = "PORTFOLIO_001_CHERRY_PICKED_INVENTORY"
        sleeves["ALPHA_SHORT"]["selection_bias_audit"]["trial_inventory"][
            "validation"
        ]["family_id"] = "UNRELATED_SHORT_FAMILY"
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("portfolio_selection_bias", failed)
        self.assertIn("current_sleeve_selection_bias_audits", failed)

    def test_portfolio_promotion_binds_selected_trials_to_promoted_returns(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["selection_bias_audit"]["selected_return_sha256"] = "9" * 64
        sleeves["ALPHA_SHORT"]["selection_bias_audit"][
            "selected_return_sha256"
        ] = "8" * 64
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_selection_bias", failed)
        self.assertIn("current_sleeve_selection_bias_audits", failed)

    def test_portfolio_promotion_rejects_weak_or_incomplete_risk_audit(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["portfolio_risk_audit"]["aggregate"][
            "probability_of_ruin_upper_confidence"
        ] = 0.02
        report["portfolio_risk_audit"]["checks"][2]["passed"] = False
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("portfolio_multi_block_risk", failed)

        _, malformed_report, _, _, _, _ = self._passing_portfolio_inputs()
        malformed_report["portfolio_risk_audit"]["equity"] = []
        malformed_report["portfolio_risk_audit"]["aggregate"] = []
        malformed = evaluate_portfolio_promotion(
            manifest,
            malformed_report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        malformed_failed = {
            item["name"] for item in malformed["checks"] if not item["passed"]
        }
        self.assertEqual(malformed["status"], "FAIL")
        self.assertIn("portfolio_multi_block_risk", malformed_failed)

    def test_portfolio_promotion_rejects_fake_monthly_check_inventory(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["monthly_target_gate"]["checks"] = [{"name": "all", "passed": True}]
        report["portfolio_performance_audit"]["monthly_target_gate"]["checks"] = [
            {"name": "all", "passed": True}
        ]
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        monthly = next(
            item for item in rejected["checks"] if item["name"] == "combined_monthly_target_gate"
        )
        self.assertFalse(monthly["passed"])
        self.assertFalse(monthly["actual"]["check_inventory_valid"])

    def test_portfolio_promotion_rejects_postdated_registration(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        manifest["registered_at_utc"] = "2025-01-01T00:00:00Z"
        report["evidence"]["registered_at_utc"] = "2025-01-01T00:00:00Z"
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertIn("portfolio_locked_forward_evidence", failed)

    def test_portfolio_promotion_binds_claimed_window_to_replayed_sources(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["evidence"]["sample_started_at_utc"] = "2025-02-01T00:00:00Z"
        report["evidence"]["sample_ended_at_utc"] = "2026-02-01T00:00:00Z"
        for sleeve in sleeves.values():
            sleeve["evidence"]["sample_started_at_utc"] = "2025-02-01T00:00:00Z"
            sleeve["evidence"]["sample_ended_at_utc"] = "2026-02-01T00:00:00Z"
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("locked_sample_source_window_binding", failed)

        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        sleeves["ALPHA_SHORT"]["evidence"]["sample_ended_at_utc"] = (
            "2025-12-31T00:00:00Z"
        )
        mismatched = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        mismatched_failed = {
            item["name"] for item in mismatched["checks"] if not item["passed"]
        }
        self.assertNotEqual(mismatched["status"], "PASS")
        self.assertIn("standalone_sleeve_promotion", mismatched_failed)

    def test_portfolio_promotion_rejects_wrong_short_action_and_concentration(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        short_entry = next(
            item
            for item in execution["scopes"]
            if item["candidate"] == "ALPHA_SHORT" and item["order_role"] == "entry"
        )
        short_entry["action_side"] = "buy"
        report["pairwise_daily_correlations"][0]["correlation"] = 0.95
        report["positive_pnl_share_by_sleeve"] = {"ALPHA_LONG": 0.9, "ALPHA_SHORT": 0.1}
        report["portfolio_composition_audit"]["pairwise_daily_correlations"][0][
            "correlation"
        ] = 0.95
        report["portfolio_composition_audit"]["positive_pnl_share_by_sleeve"] = {
            "ALPHA_LONG": 0.9,
            "ALPHA_SHORT": 0.1,
        }
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("complete_execution_path_evidence", failed)
        self.assertIn("maximum_absolute_pairwise_correlation", failed)
        self.assertIn("single_sleeve_positive_pnl_concentration", failed)

    def test_portfolio_promotion_requires_source_bound_diversification_bases(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        report["portfolio_composition_audit"]["pairwise_daily_correlation_basis"] = (
            "portfolio_equity_scaled_contributions"
        )
        report["portfolio_composition_audit"]["positive_pnl_share_basis"] = (
            "portfolio_equity_scaled_contributions"
        )
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("portfolio_composition_source_replay", failed)

    def test_portfolio_promotion_policy_change_invalidates_old_evidence(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        changed_policy_digest = "f" * 64
        rejected = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=changed_policy_digest,
        )
        failed = {item["name"] for item in rejected["checks"] if not item["passed"]}
        self.assertNotEqual(rejected["status"], "PASS")
        self.assertIn("portfolio_manifest_policy_binding", failed)
        self.assertIn("portfolio_report_policy_binding", failed)
        execution_check = next(
            item for item in rejected["checks"] if item["name"] == "complete_execution_path_evidence"
        )
        self.assertTrue(
            any("portfolio policy" in reason for reason in execution_check["actual"])
        )

    def test_goal_readiness_never_promotes_calibration_or_registry_counts(self) -> None:
        readiness = build_goal_readiness(
            {
                "experiments": 2,
                "status_counts": {"PASS": 2},
                "experiments_detail": [],
                "auxiliary_gates": {
                    "execution_calibration": {
                        "status": "CALIBRATION_PASS",
                        "promotion_eligible": False,
                    },
                    "alpha007_exchange_filter_feasibility": {
                        "status": "FAIL",
                        "promotion_eligible": False,
                    }
                },
            },
            {"status": "HEALTHY"},
        )
        self.assertFalse(readiness["objective_proven"])
        self.assertEqual(readiness["portfolio_promotion_status"], "NOT_OPENED")
        self.assertEqual(readiness["live_order_authorization"], "CLOSED")
        self.assertEqual(
            readiness["return_objective"]["monthly_return_target"],
            "NOT_A_GATE",
        )
        self.assertEqual(
            readiness["return_objective"]["promotion_requirement"],
            "positive_long_horizon_cost_after_expectancy",
        )
        self.assertTrue(readiness["thresholds"]["require_slippage_stress_positive"])
        self.assertTrue(readiness["thresholds"]["require_funding_stress_positive"])
        self.assertTrue(
            any("Alpha 007" in blocker for blocker in readiness["ordered_blockers"])
        )

    def test_risk_policy_contract_rejects_permissive_or_drifting_objective(self) -> None:
        risk = {
            "policy_id": "CAPITAL_PRESERVATION_RISK_POLICY_V1",
            "policy_version": "1.0.0",
            "live_order_authorization": "CLOSED",
            "primary_objective": "Validate positive long-horizon cost-after expectancy while preserving capital; a monthly return target is not a pass/fail gate.",
            "non_negotiables": {
                "maximum_cost_after_mtm_drawdown": 0.1,
                "maximum_cost_stress_mtm_drawdown": 0.1,
                "liquidation_exits": 0,
                "rule_violations": 0,
                "maximum_planned_account_risk_per_trade": 0.0025,
                "maximum_planned_leverage": 1.0,
                "maximum_simultaneous_positions": 2,
                "adverse_position_adds": "FORBIDDEN",
                "cost_stress_net_return": "> 0",
                "lookahead_analysis": "PASS",
                "recursive_analysis": "PASS",
                "data_integrity": "PASS",
            },
        }
        objective = {
            "live_order_authorization": "CLOSED",
            "return_objective": {
                "monthly_return_target": "NOT_A_GATE",
                "promotion_requirement": "positive_long_horizon_cost_after_expectancy",
            },
            "capital_preservation_requirements": {
                "maximum_historical_drawdown": 0.1,
                "maximum_combined_cost_stress_drawdown": 0.1,
                "liquidation_exits": 0,
                "rule_violations": 0,
                "maximum_planned_account_risk_per_trade": 0.0025,
                "maximum_planned_leverage": 1.0,
                "maximum_simultaneous_positions": 2,
                "adverse_position_adds": "FORBIDDEN",
                "double_fee_plus_modeled_slippage_stress_net_return": "> 0",
                "data_integrity": "PASS",
                "lookahead_analysis": "PASS",
                "recursive_analysis": "PASS",
            },
        }
        self.assertEqual(validate_risk_policy_contract(risk, objective)["status"], "PASS")
        missing_version = dict(risk)
        missing_version.pop("policy_version")
        version_rejected = validate_risk_policy_contract(missing_version, objective)
        self.assertEqual(version_rejected["status"], "FAIL")
        self.assertIn("semantic versioning", " ".join(version_rejected["failures"]))
        objective["capital_preservation_requirements"]["rule_violations"] = 1
        rule_budget_rejected = validate_risk_policy_contract(risk, objective)
        self.assertEqual(rule_budget_rejected["status"], "FAIL")
        self.assertIn("rule_violations", " ".join(rule_budget_rejected["failures"]))
        objective["capital_preservation_requirements"]["rule_violations"] = 0
        objective["return_objective"]["monthly_return_target"] = 0.1
        rejected = validate_risk_policy_contract(risk, objective)
        self.assertEqual(rejected["status"], "FAIL")
        self.assertIn("monthly return gate", " ".join(rejected["failures"]))

    def test_goal_readiness_exposes_historical_policy_as_non_promotion_evidence(self) -> None:
        readiness = build_goal_readiness(
            {"experiments": 0, "status_counts": {}, "experiments_detail": [], "auxiliary_gates": {}},
            {"status": "HEALTHY"},
            historical_evidence_policy={
                "status": "PASS",
                "kind": "HISTORICAL_EVIDENCE_GOVERNANCE_ONLY",
                "historical_point_in_time_available_now": False,
                "checks": [
                    {
                        "name": "historical_open_interest_blocked_without_publication_time",
                        "passed": True,
                    }
                ],
            },
            historical_cost_policy={
                "status": "PASS",
                "kind": "HISTORICAL_COST_GOVERNANCE_ONLY",
            },
        )
        policy = readiness["supporting_non_promotion_gates"]["historical_evidence_policy"]
        self.assertEqual(policy["status"], "PASS")
        self.assertFalse(policy["promotion_eligible"])
        self.assertFalse(policy["historical_point_in_time_available_now"])
        self.assertTrue(policy["historical_open_interest_decision_feature_blocked"])
        cost = readiness["supporting_non_promotion_gates"]["historical_cost_policy"]
        self.assertEqual(cost["status"], "PASS")
        self.assertFalse(cost["promotion_eligible"])

        readiness = build_goal_readiness(
            {"status_counts": {}, "experiments_detail": []}, {"status": "HEALTHY"},
            historical_execution_coverage={"status": "PASS", "kind": "HISTORICAL_NEXT_OPEN_DATA_COVERAGE_ONLY", "universe_sha256": "a" * 64},
            historical_funding_coverage={"status": "PASS", "kind": "HISTORICAL_FUNDING_AVAILABILITY_ONLY", "universe_sha256": "a" * 64},
            historical_funding_ledger_audit={"status": "PASS", "kind": "HISTORICAL_FUNDING_LEDGER_REPLAY_ONLY", "universe_sha256": "a" * 64},
        )
        self.assertEqual(readiness["supporting_non_promotion_gates"]["historical_one_day_funding_coverage"]["status"], "PASS")
        self.assertFalse(readiness["supporting_non_promotion_gates"]["historical_funding_ledger_replay"]["promotion_eligible"])

    def test_goal_readiness_marks_legacy_forward_cohort_ineligible_under_current_policy(self) -> None:
        readiness = build_goal_readiness(
            {"experiments": 0, "status_counts": {}, "experiments_detail": [], "auxiliary_gates": {}},
            {"status": "HEALTHY"},
            alpha001_forward={
                "candidate": "FuturesBtcRegimeRiskBudgetLong",
                "policy_scope": {
                    "cohort": "FROZEN_LEGACY_FORWARD_COHORT",
                    "current_capital_preservation_policy_eligibility": False,
                    "reason": "sealed launch terms",
                },
            },
        )
        evidence = readiness["current_single_candidate_evidence"]
        self.assertEqual(evidence["cohort"], "FROZEN_LEGACY_FORWARD_COHORT")
        self.assertFalse(evidence["current_capital_preservation_policy_eligible"])
        self.assertTrue(any("frozen legacy" in item for item in readiness["ordered_blockers"]))

    def test_goal_readiness_requires_portfolio_pass_and_still_keeps_live_closed(self) -> None:
        manifest, report, sleeves, execution, digest, policy_digest = self._passing_portfolio_inputs()
        promotion = evaluate_portfolio_promotion(
            manifest,
            report,
            sleeves,
            execution,
            manifest_sha256=digest,
            policy_sha256=policy_digest,
        )
        readiness = build_goal_readiness(
            {
                "experiments": 2,
                "status_counts": {"PASS": 2},
                "experiments_detail": [],
                "auxiliary_gates": {},
            },
            {"status": "HEALTHY"},
            portfolio_promotion=promotion,
            portfolio_receipt_integrity={"status": "PASS", "failures": []},
        )
        self.assertTrue(readiness["objective_proven"])
        self.assertEqual(readiness["status"], "PROMOTION_PREREQUISITES_PASS_LIVE_CLOSED")
        self.assertEqual(readiness["live_order_authorization"], "CLOSED")

    def test_goal_readiness_rejects_stale_promotion_receipt_sources(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            policy = root / "policy.json"
            manifest = root / "manifest.json"
            portfolio = root / "portfolio.json"
            execution = root / "execution.json"
            execution_observations = root / "execution-observations.csv"
            execution_observation_segment = root / "execution-observations-segment.csv"
            execution_market_manifest = root / "execution-market-manifest.json"
            execution_depth = root / "execution-depth.jsonl.gz"
            execution_trades = root / "execution-agg-trades.jsonl.gz"
            sleeve = root / "sleeve.json"
            portfolio_selection = root / "portfolio-selection.json"
            portfolio_matrix = root / "portfolio-trials.csv"
            portfolio_inventory = root / "portfolio-trial-inventory.json"
            sleeve_selection = root / "sleeve-selection.json"
            sleeve_matrix = root / "sleeve-trials.csv"
            sleeve_inventory = root / "sleeve-trial-inventory.json"
            portfolio_risk = root / "portfolio-risk.json"
            portfolio_equity = root / "portfolio-equity.csv"
            portfolio_contributions = root / "portfolio-contributions.csv"
            portfolio_sleeve_pnl = root / "portfolio-sleeve-pnl.csv"
            portfolio_trades = root / "portfolio-trades.csv"
            portfolio_exposure = root / "portfolio-exposure.csv"
            portfolio_accounting = root / "portfolio-accounting.csv"
            for path in (
                policy,
                manifest,
                portfolio,
                execution,
                execution_observations,
                execution_observation_segment,
                execution_market_manifest,
                execution_depth,
                execution_trades,
                sleeve,
                portfolio_selection,
                portfolio_matrix,
                portfolio_inventory,
                sleeve_selection,
                sleeve_matrix,
                sleeve_inventory,
                portfolio_risk,
                portfolio_equity,
                portfolio_contributions,
                portfolio_sleeve_pnl,
                portfolio_trades,
                portfolio_exposure,
                portfolio_accounting,
            ):
                path.write_text(path.name, encoding="utf-8")
            for relative in PROMOTION_PROTOCOL_FILES:
                protocol_path = root / relative
                protocol_path.parent.mkdir(parents=True, exist_ok=True)
                protocol_path.write_text(relative, encoding="utf-8")
            receipt = {
                "inputs": {
                    "manifest": str(manifest),
                    "manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
                    "policy": str(policy),
                    "policy_sha256": sha256(policy.read_bytes()).hexdigest(),
                    "portfolio_report": str(portfolio),
                    "portfolio_report_sha256": sha256(portfolio.read_bytes()).hexdigest(),
                    "execution_audit": str(execution),
                    "execution_audit_sha256": sha256(execution.read_bytes()).hexdigest(),
                    "execution_observations": {
                        "path": str(execution_observations),
                        "sha256": sha256(
                            execution_observations.read_bytes()
                        ).hexdigest(),
                        "segments": [
                            {
                                "segment_id": "SEGMENT_001",
                                "path": str(execution_observation_segment),
                                "sha256": sha256(
                                    execution_observation_segment.read_bytes()
                                ).hexdigest(),
                                "market_data_manifest": {
                                    "path": str(execution_market_manifest),
                                    "sha256": sha256(
                                        execution_market_manifest.read_bytes()
                                    ).hexdigest(),
                                    "files": [
                                        {
                                            "path": str(execution_depth),
                                            "sha256": sha256(
                                                execution_depth.read_bytes()
                                            ).hexdigest(),
                                        },
                                        {
                                            "path": str(execution_trades),
                                            "sha256": sha256(
                                                execution_trades.read_bytes()
                                            ).hexdigest(),
                                        },
                                    ],
                                },
                            }
                        ],
                    },
                    "sleeve_reports": {
                        "ALPHA": {
                            "path": str(sleeve),
                            "sha256": sha256(sleeve.read_bytes()).hexdigest(),
                        }
                    },
                    "portfolio_selection_bias_audit": {
                        "path": str(portfolio_selection),
                        "sha256": sha256(portfolio_selection.read_bytes()).hexdigest(),
                        "trial_return_matrix": {
                            "path": str(portfolio_matrix),
                            "sha256": sha256(portfolio_matrix.read_bytes()).hexdigest(),
                        },
                        "trial_inventory": {
                            "path": str(portfolio_inventory),
                            "sha256": sha256(portfolio_inventory.read_bytes()).hexdigest(),
                        },
                    },
                    "portfolio_risk_audit": {
                        "path": str(portfolio_risk),
                        "sha256": sha256(portfolio_risk.read_bytes()).hexdigest(),
                        "mark_to_market_equity": {
                            "path": str(portfolio_equity),
                            "sha256": sha256(portfolio_equity.read_bytes()).hexdigest(),
                        },
                    },
                    "portfolio_contributions": {
                        "path": str(portfolio_contributions),
                        "sha256": sha256(
                            portfolio_contributions.read_bytes()
                        ).hexdigest(),
                    },
                    "portfolio_sleeve_pnl": {
                        "path": str(portfolio_sleeve_pnl),
                        "sha256": sha256(
                            portfolio_sleeve_pnl.read_bytes()
                        ).hexdigest(),
                    },
                    "portfolio_performance": {
                        "trades": {
                            "path": str(portfolio_trades),
                            "sha256": sha256(portfolio_trades.read_bytes()).hexdigest(),
                        },
                        "exposure": {
                            "path": str(portfolio_exposure),
                            "sha256": sha256(
                                portfolio_exposure.read_bytes()
                            ).hexdigest(),
                        },
                        "accounting": {
                            "path": str(portfolio_accounting),
                            "sha256": sha256(
                                portfolio_accounting.read_bytes()
                            ).hexdigest(),
                        },
                    },
                    "sleeve_selection_bias_audits": {
                        "ALPHA": {
                            "path": str(sleeve_selection),
                            "sha256": sha256(sleeve_selection.read_bytes()).hexdigest(),
                            "trial_return_matrix": {
                                "path": str(sleeve_matrix),
                                "sha256": sha256(sleeve_matrix.read_bytes()).hexdigest(),
                            },
                            "trial_inventory": {
                                "path": str(sleeve_inventory),
                                "sha256": sha256(sleeve_inventory.read_bytes()).hexdigest(),
                            },
                        }
                    },
                    "evaluator_protocol_sha256": {
                        relative: sha256((root / relative).read_bytes()).hexdigest()
                        for relative in PROMOTION_PROTOCOL_FILES
                    },
                }
            }
            self.assertEqual(
                audit_portfolio_promotion_receipt(
                    receipt, project_root=root, current_policy=policy
                )["status"],
                "PASS",
            )
            sleeve.write_text("tampered", encoding="utf-8")
            audit = audit_portfolio_promotion_receipt(
                receipt, project_root=root, current_policy=policy
            )
            self.assertEqual(audit["status"], "FAIL")
            self.assertTrue(any("sleeve report" in item for item in audit["failures"]))
            sleeve.write_text(sleeve.name, encoding="utf-8")
            portfolio_sleeve_pnl.write_text("tampered", encoding="utf-8")
            sleeve_pnl_audit = audit_portfolio_promotion_receipt(
                receipt, project_root=root, current_policy=policy
            )
            self.assertEqual(sleeve_pnl_audit["status"], "FAIL")
            self.assertTrue(
                any(
                    "sleeve PnL matrix" in item
                    for item in sleeve_pnl_audit["failures"]
                )
            )
            portfolio_sleeve_pnl.write_text(
                portfolio_sleeve_pnl.name, encoding="utf-8"
            )
            portfolio_inventory.write_text("tampered", encoding="utf-8")
            inventory_audit = audit_portfolio_promotion_receipt(
                receipt, project_root=root, current_policy=policy
            )
            self.assertEqual(inventory_audit["status"], "FAIL")
            self.assertTrue(
                any("sealed trial inventory" in item for item in inventory_audit["failures"])
            )

    def test_goal_readiness_rejects_tampered_promotion_verdict(self) -> None:
        recomputed = {
            "schema_version": 1,
            "status": "FAIL",
            "checks": [{"name": "risk", "passed": False}],
            "generated_at_utc": "2026-01-01T00:00:00+00:00",
            "inputs": {"manifest": "original.json"},
        }
        identical = {
            **recomputed,
            "generated_at_utc": "2026-01-02T00:00:00+00:00",
            "inputs": {"manifest": "same-sources-different-rendering.json"},
        }
        self.assertEqual(
            compare_portfolio_promotion_replay(identical, recomputed)["status"],
            "PASS",
        )
        tampered = {
            **identical,
            "status": "PASS",
            "checks": [{"name": "risk", "passed": True}],
        }
        audit = compare_portfolio_promotion_replay(tampered, recomputed)
        self.assertEqual(audit["status"], "FAIL")
        self.assertIn("full source replay", audit["failures"][0])

    def test_alpha015_activation_waits_for_point_in_time_universe(self) -> None:
        policy = {
            "candidate": "CANDIDATE_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            "status": "DEFERRED_NOT_AN_EXPERIMENT",
            "activation_requirements": {
                "point_in_time_universe_consecutive_days": 180,
                "universe_protocol_status": "READY",
            },
        }
        screening = {
            "selected_next_step": "ALPHA_014_CROSS_SECTIONAL_PERP_BASIS_CATCHUP",
            "candidates": [
                {
                    "name": "MINUTE_CROSS_CRYPTO_PREDICTABILITY",
                    "decision": "DEFER_HIGH_PRIORITY",
                }
            ],
        }
        methodology = {
            "candidate": "CANDIDATE_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            "experiment_0015_open": False,
            "resolution_status": {"all_fixed": True, "contract_terms_consistent": True},
            "artifact_integrity": {"status": "PASS"},
        }
        waiting = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "ACCUMULATING",
                "consecutive_days_ending_latest": 1,
                "required_consecutive_days": 180,
            },
            registry={"experiments": []},
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=False,
        )
        self.assertEqual(waiting["status"], "WAITING_POINT_IN_TIME_UNIVERSE")
        self.assertFalse(waiting["return_data_authorized"])
        self.assertEqual(waiting["days_remaining"], 179)

        ready = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 180,
            },
            registry={"experiments": []},
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=True,
            preregistration_verified=False,
        )
        self.assertEqual(ready["status"], "READY_FOR_PREREGISTRATION_ONLY")
        self.assertTrue(ready["preregistration_authorized"])
        self.assertFalse(ready["return_data_authorized"])

        premature = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 180,
            },
            registry={
                "experiments": [
                    {
                        "strategy": "ALPHA_015_LEAKED_TEST",
                        "trial_accounting": {"alpha_number": 15},
                    }
                ]
            },
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=True,
        )
        self.assertEqual(premature["status"], "PREMATURE_EXPERIMENT_REGISTERED")
        self.assertFalse(premature["return_data_authorized"])

        verified_registration = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 180,
            },
            registry={
                "experiments": [
                    {
                        "strategy": "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
                        "trial_accounting": {"alpha_number": 15},
                    }
                ]
            },
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=True,
            preregistration_verified=True,
        )
        self.assertEqual(verified_registration["status"], "READY_FOR_DATA_ACQUISITION")
        self.assertTrue(verified_registration["return_data_authorized"])

        mutated_policy = {**policy, "status": "ACTIVE_EXPERIMENT"}
        contract_block = evaluate_alpha015_activation(
            policy=mutated_policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 180,
            },
            registry={"experiments": []},
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=True,
        )
        self.assertEqual(contract_block["status"], "ACTIVATION_CONTRACT_BLOCK")
        self.assertFalse(contract_block["return_data_authorized"])

        forged_requirement = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 30,
            },
            registry={"experiments": []},
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=True,
        )
        self.assertEqual(forged_requirement["status"], "ACTIVATION_CONTRACT_BLOCK")
        self.assertFalse(forged_requirement["return_data_authorized"])

        missing_lock = evaluate_alpha015_activation(
            policy=policy,
            universe_report={
                "status": "READY",
                "consecutive_days_ending_latest": 180,
                "required_consecutive_days": 180,
            },
            registry={"experiments": []},
            screening=screening,
            methodology_audit=methodology,
            activation_lock_verified=False,
        )
        self.assertEqual(missing_lock["status"], "WAITING_ACTIVATION_LOCK")
        self.assertFalse(missing_lock["return_data_authorized"])

    def test_alpha015_methodology_hashes_are_recomputed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for name in ("kernel.py", "guard.py", "policy.json", "calibration.json"):
                path = root / name
                path.write_text(name, encoding="utf-8")
                paths.append(path)
            audit = {
                "resolution_status": {},
                "causal_kernel": {
                    "path": paths[0].name,
                    "sha256": sha256(paths[0].read_bytes()).hexdigest(),
                },
                "data_guard": {
                    "path": paths[1].name,
                    "sha256": sha256(paths[1].read_bytes()).hexdigest(),
                },
                "activation_policy": {
                    "path": paths[2].name,
                    "sha256": sha256(paths[2].read_bytes()).hexdigest(),
                },
                "frozen_transfer_choices": {
                    "execution_calibration_path": paths[3].name,
                    "execution_calibration_sha256": sha256(paths[3].read_bytes()).hexdigest(),
                },
            }
            current = verify_methodology_artifacts(audit, project_root=root)
            self.assertEqual(current["artifact_integrity"]["status"], "PASS")
            self.assertTrue(current["resolution_status"]["protocol_artifacts_current"])
            paths[0].write_text("changed", encoding="utf-8")
            changed = verify_methodology_artifacts(audit, project_root=root)
            self.assertEqual(changed["artifact_integrity"]["status"], "FAIL")
            self.assertFalse(changed["resolution_status"]["protocol_artifacts_current"])

            health_path = root / "scripts" / "research_health.py"
            health_path.parent.mkdir()
            health_path.write_text("observer", encoding="utf-8")
            audit["activation_lock"] = {
                "artifacts": [
                    {
                        "path": "scripts/research_health.py",
                        "sha256": sha256(health_path.read_bytes()).hexdigest(),
                    }
                ]
            }
            observer_bound = verify_methodology_artifacts(audit, project_root=root)
            self.assertEqual(observer_bound["artifact_integrity"]["status"], "FAIL")
            self.assertIn(
                "artifact_4_operational_observer_disallowed",
                observer_bound["artifact_integrity"]["failures"],
            )

    def test_alpha015_contract_semantics_rejects_template_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            amendment_path = root / "alpha015-contract-amendment-011.json"
            preservation_path = root / "alpha015-capital-preservation-amendment-012.json"
            template_path = root / "alpha015-preregistration-template.json"
            calibration_path = root / "calibration.json"
            calibration_path.write_text(
                json.dumps(
                    {
                        "cost_reference": {
                            "descriptive_research_stress_floor_bps": 9.0,
                        },
                        "promotion_eligible": False,
                        "measured_scopes": ["long_entry"],
                        "unmeasured_scopes": [
                            "long_exit",
                            "short_entry",
                            "short_exit",
                            "protective_stop",
                        ],
                    }
                ),
                encoding="utf-8",
            )
            amendment_path.write_text(
                json.dumps(
                    {
                        "authoritative_contract": {
                            "cadence": {
                                "decision_grid_minutes": 10,
                                "entry_latency_minutes": 1,
                                "holding_minutes": 10,
                            },
                            "costs": {
                                "base_all_in_basis_points_per_unit_realized_turnover": 10.0,
                                "stress_all_in_basis_points_per_unit_realized_turnover": 20.0,
                            },
                            "execution_calibration": {
                                "path": calibration_path.name,
                                "sha256": sha256(calibration_path.read_bytes()).hexdigest(),
                                "observed_descriptive_floor_basis_points": 9.0,
                                "measured_scopes": ["long_entry"],
                                "required_unmeasured_scopes": [
                                    "long_exit",
                                    "short_entry",
                                    "short_exit",
                                    "protective_stop",
                                ],
                                "promotion_eligible": False,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            preservation_path.write_text(
                json.dumps(
                    {
                        "amendment_id": "ALPHA015_CAPITAL_PRESERVATION_AMENDMENT_012",
                        "status": "PRE_RETURN_POLICY_ALIGNMENT",
                        "policy_binding": {
                            "risk_policy_id": "CAPITAL_PRESERVATION_RISK_POLICY_V1",
                            "capital_objective_id": "CAPITAL_PRESERVATION_RESEARCH_OBJECTIVE_V2",
                        },
                        "authoritative_risk_contract": {
                            "maximum_base_minute_mark_to_market_drawdown_fraction": 0.10,
                            "maximum_stress_minute_mark_to_market_drawdown_fraction": 0.10,
                            "maximum_simultaneous_positions": 2,
                        },
                        "candidate_compatibility": {
                            "status": "COMPATIBLE",
                            "policy_limit": 2,
                            "minimum_concurrent_futures_positions": 2,
                        },
                    }
                ),
                encoding="utf-8",
            )
            template = {
                "portfolio": {
                    "buckets": 5,
                    "decision_grid": "10-minute UTC epoch grid",
                    "entry_latency_minutes": 1,
                    "holding_minutes": 10,
                },
                "model": {"minimum_symbols": 5},
                "costs": {
                    "base_all_in_basis_points_per_unit_realized_turnover": 10.0,
                    "stress_all_in_basis_points_per_unit_realized_turnover": 20.0,
                    "cost_contract_amendment": amendment_path.name,
                },
                "capital_preservation_policy_amendment": preservation_path.name,
                "development_gate": {
                    "checks": {
                        "base_maximum_minute_mark_to_market_drawdown_fraction": 0.10,
                        "stress_maximum_minute_mark_to_market_drawdown_fraction": 0.10,
                    }
                },
                "locked_validation_gate": {
                    "checks": {
                        "base_maximum_minute_mark_to_market_drawdown_fraction": 0.10,
                        "stress_maximum_minute_mark_to_market_drawdown_fraction": 0.10,
                    }
                },
                "post_validation_requirements": [],
            }
            template_path.write_text(json.dumps(template), encoding="utf-8")
            audit = {
                "activation_policy": {
                    "amendments": [
                        {"path": amendment_path.name},
                        {"path": preservation_path.name},
                    ]
                },
                "preregistration_pipeline": {"artifacts": [{"path": template_path.name}]},
                "frozen_transfer_choices": {
                    "execution_calibration_path": calibration_path.name,
                    "execution_calibration_sha256": sha256(calibration_path.read_bytes()).hexdigest(),
                },
            }
            self.assertEqual(
                verify_alpha015_contract_semantics(audit, project_root=root)["status"],
                "PASS",
            )
            template["development_gate"]["checks"][
                "base_maximum_minute_mark_to_market_drawdown_fraction"
            ] = 0.15
            template_path.write_text(json.dumps(template), encoding="utf-8")
            risk_drift = verify_alpha015_contract_semantics(audit, project_root=root)
            self.assertEqual(risk_drift["status"], "FAIL")
            self.assertIn(
                "template_development_capital_preservation_drawdown_mismatch",
                risk_drift["failures"],
            )
            template["development_gate"]["checks"][
                "base_maximum_minute_mark_to_market_drawdown_fraction"
            ] = 0.10
            template["portfolio"]["holding_minutes"] = 13
            template_path.write_text(json.dumps(template), encoding="utf-8")
            invalid = verify_alpha015_contract_semantics(audit, project_root=root)
            self.assertEqual(invalid["status"], "FAIL")
            self.assertIn("template_holding_minutes_mismatch", invalid["failures"])
            template["portfolio"]["holding_minutes"] = "not-a-number"
            template_path.write_text(json.dumps(template), encoding="utf-8")
            malformed = verify_alpha015_contract_semantics(audit, project_root=root)
            self.assertEqual(malformed["status"], "FAIL")
            self.assertIn("template_holding_minutes_mismatch", malformed["failures"])

    def test_alpha015_preregistration_binds_lock_sources_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = (
                "template",
                "methodology_audit",
                "methodology_amendment_002",
                "methodology_amendment_003",
                "data_amendment_004",
                "funding_amendment_005",
                "rebalance_amendment_006",
                "timestamp_amendment_007",
                "acquisition_amendment_008",
                "selection_amendment_009",
                "risk_amendment_010",
                "contract_amendment_011",
                "capital_preservation_amendment_012",
                "activation_lock",
                "primary_source_manifest",
                "primary_source_pdf",
                "causal_kernel",
                "simulation_engine",
                "development_statistics",
                "daily_replay_engine",
                "development_runner",
                "validation_unlock_runner",
                "validation_runner",
                "funding_reconciliation_runner",
                "acquisition_protocol",
                "acquisition_runner",
                "data_guard",
                "execution_calibration",
            )
            paths = {}
            for name in names:
                path = root / f"{name}.source"
                path.write_bytes(name.encode())
                paths[name] = path
            template = {
                "status": "LOCKED_TEMPLATE_NOT_AN_EXPERIMENT",
                "candidate": "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
                "hypothesis": "fixed synthetic hypothesis",
                "activation": {},
                "model": {},
                "portfolio": {},
                "costs": {},
                "development_gate": {},
                "locked_validation_gate": {},
                "post_validation_requirements": [],
                "prohibited": [],
            }
            lock_body = {
                "status": "LOCKED_FOR_PREREGISTRATION_ONLY",
                "source_cutoff_at_utc": "2026-01-01T00:30:00+00:00",
                "formation_snapshot_count": 180,
                "development": [f"d{index}" for index in range(120)],
                "locked_validation": [f"v{index}" for index in range(60)],
                "validation_open": False,
                "split_sha256": "a" * 64,
                "formation_start": "2025-01-01",
                "formation_end": "2025-06-29",
                "evaluation_start": "2025-01-02",
                "evaluation_end": "2025-06-30",
            }
            lock = {**lock_body, "lock_sha256": alpha015_canonical_sha256(lock_body)}
            paths["activation_lock"].write_text(json.dumps(lock), encoding="utf-8")
            prereg = build_alpha015_preregistration(
                template=template,
                activation_lock=lock,
                artifact_paths=paths,
                project_root=root,
            )
            result = {
                "source_preregistration": "research/alpha015-preregistration.json",
                "preregistration_sha256": prereg["preregistration_sha256"],
                "activation_lock_sha256": lock["lock_sha256"],
                "return_data_acquired": False,
                "validation_opened": False,
            }
            registry = {
                "experiments": [
                    {
                        "strategy": prereg["candidate"],
                        "hypothesis": prereg["hypothesis"],
                        "result": result,
                        "trial_accounting": {
                            "alpha_number": 15,
                            "family": "minute_cross_crypto_predictability",
                            "family_trial": 1,
                        },
                    }
                ]
            }
            self.assertTrue(
                verify_alpha015_preregistration(
                    stored=prereg,
                    expected=prereg,
                    registry=registry,
                )
            )
            tampered = {**prereg, "hypothesis": "changed"}
            self.assertFalse(
                verify_alpha015_preregistration(
                    stored=tampered,
                    expected=prereg,
                    registry=registry,
                )
            )

    def test_alpha015_acquisition_plan_requires_receipt_and_exact_storage(self) -> None:
        start = date(2026, 1, 1)
        development = [
            (start + timedelta(days=offset)).isoformat() for offset in range(120)
        ]
        validation = [
            (start + timedelta(days=120 + offset)).isoformat()
            for offset in range(60)
        ]
        bindings = []
        symbols = [f"S{number:02d}/USDT:USDT" for number in range(30)]
        for evaluation in development + validation:
            evaluation_day = date.fromisoformat(evaluation)
            bindings.append(
                {
                    "evaluation_date": evaluation,
                    "membership_snapshot_date": (
                        evaluation_day - timedelta(days=1)
                    ).isoformat(),
                    "eligible_symbols": symbols,
                }
            )
        lock_body = {
            "status": "LOCKED_FOR_PREREGISTRATION_ONLY",
            "validation_open": False,
            "split_sha256": "a" * 64,
            "development": development,
            "locked_validation": validation,
            "bindings": bindings,
        }
        lock = {**lock_body, "lock_sha256": alpha015_canonical_sha256(lock_body)}
        prereg_body = {
            "candidate": "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            "status": "PREREGISTERED_SOURCE_ACQUISITION_LOCKED",
            "activation_binding": {
                "lock_sha256": lock["lock_sha256"],
                "split_sha256": lock["split_sha256"],
            },
            "return_data_acquired": False,
            "locked_validation_open": False,
            "live_order_authorization": "CLOSED",
        }
        prereg = {
            **prereg_body,
            "preregistration_sha256": alpha015_canonical_sha256(prereg_body),
        }
        readiness = {
            "status": "READY_FOR_DATA_ACQUISITION",
            "experiment_registered": True,
            "preregistration_authorized": False,
            "return_data_authorized": True,
            "unresolved_methodology": [],
            "checks": {"sources": True, "registry": True},
        }
        plan = build_development_acquisition_plan(
            readiness=readiness,
            preregistration=prereg,
            activation_lock=lock,
            now=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(plan["status"], "AUTHORIZED_PLAN_NO_BYTES_ACQUIRED")
        self.assertFalse(plan["locked_validation_open"])
        validation_unlock = {
            "status": "AUTHORIZED",
            "development_gates_pass": True,
            "preregistration_sha256": prereg["preregistration_sha256"],
            "development_report_sha256": "c" * 64,
            "calendar_sha256": lock["split_sha256"],
        }
        validation_plan = build_phase_acquisition_plan(
            readiness=readiness,
            preregistration=prereg,
            activation_lock=lock,
            phase="locked_validation",
            validation_unlock=validation_unlock,
            expected_development_report_sha256="c" * 64,
            now=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(validation_plan["locked_validation_open"])
        self.assertEqual(validation_plan["evaluation_start"], validation[0])
        self.assertEqual(validation_plan["development_report_sha256"], "c" * 64)
        with self.assertRaisesRegex(Alpha015AcquisitionError, "remains inaccessible"):
            build_phase_acquisition_plan(
                readiness=readiness,
                preregistration=prereg,
                activation_lock=lock,
                phase="locked_validation",
                now=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )
        archive_sizes = {
            (item["dataset"], item["symbol"], item["date"]): 100
            for item in plan["files"]
            if item["dataset"] != "funding"
        }
        budget = alpha015_storage_budget(
            plan,
            archive_content_lengths=archive_sizes,
            free_bytes=100 * 1024**3,
        )
        self.assertEqual(budget["status"], "PASS")
        size_receipt = alpha015_build_size_inventory_receipt(
            plan,
            [
                {
                    "dataset": dataset,
                    "symbol": symbol,
                    "date": day,
                    "content_length": length,
                }
                for (dataset, symbol, day), length in archive_sizes.items()
            ],
        )
        self.assertEqual(size_receipt["status"], "COMPLETE")
        self.assertTrue(
            alpha015_verify_size_inventory_receipt(size_receipt, plan)
        )
        with self.assertRaisesRegex(Alpha015AcquisitionError, "not exact"):
            alpha015_storage_budget(
                plan,
                archive_content_lengths=dict(list(archive_sizes.items())[1:]),
                free_bytes=100 * 1024**3,
            )
        first = plan["files"][0]
        completed = {
            "dataset": first["dataset"],
            "symbol": first["symbol"],
            "date": first["date"],
            "sha256": "b" * 64,
            "bytes": 100,
            "status": "VERIFIED_AND_SEALED",
            "archive": first["archive"],
            "checksum": first["checksum"],
        }
        progress = alpha015_build_progress_receipt(plan, [completed])
        self.assertEqual(progress["status"], "ACCUMULATING")
        self.assertTrue(alpha015_verify_progress_receipt(progress, plan))
        progress["completed_files"] = 2
        self.assertFalse(alpha015_verify_progress_receipt(progress, plan))
        denied = dict(readiness, return_data_authorized=False)
        with self.assertRaisesRegex(Alpha015AcquisitionError, "not authorized"):
            build_development_acquisition_plan(
                readiness=denied,
                preregistration=prereg,
                activation_lock=lock,
                now=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )

    def test_cross_crypto_forecast_is_invariant_to_every_future_row(self) -> None:
        rng = np.random.default_rng(20261010)
        index = pd.date_range("2026-01-01T00:00:00Z", periods=760, freq="1min")
        symbols = [f"S{number:02d}" for number in range(30)]
        returns = pd.DataFrame(
            rng.normal(0.0, 0.001, size=(len(index), len(symbols))),
            index=index,
            columns=symbols,
        )
        decision = index[730]
        config = AdaptiveLassoKernelConfig(maximum_iterations=100)
        original = forecast_cross_section(
            returns, decision_at=decision, penalty=1.0, config=config
        )
        changed = returns.copy()
        changed.loc[changed.index > decision] = rng.normal(
            100.0, 50.0, size=changed.loc[changed.index > decision].shape
        )
        rerun = forecast_cross_section(
            changed, decision_at=decision, penalty=1.0, config=config
        )
        self.assertEqual(original["forecasts"], rerun["forecasts"])
        self.assertEqual(original["selected_predictors"], rerun["selected_predictors"])
        self.assertEqual(original["receipt"]["future_rows_read"], 0)
        self.assertEqual(original["receipt"]["target_end_at"], decision.isoformat())
        self.assertEqual(
            original["receipt"]["feature_end_at"],
            (decision - pd.to_timedelta(1, unit="min")).isoformat(),
        )
        self.assertEqual(
            original["execution_window"],
            {
                "decision_at": decision.isoformat(),
                "earliest_entry_at": (decision + pd.to_timedelta(1, unit="min")).isoformat(),
                "exit_at": (decision + pd.to_timedelta(11, unit="min")).isoformat(),
                "next_decision_at": (decision + pd.to_timedelta(10, unit="min")).isoformat(),
                "overlap": False,
                "gate_horizon_minutes": 10,
                "diagnostic_markouts_minutes": [1, 5, 10],
                "daily_boundary_policy": "entry_and_exit_same_utc_date_as_decision",
            },
        )
        self.assertTrue(
            forecast_future_row_invariance(
                returns,
                decision_at=decision,
                config=config,
                penalty=1.0,
            )
        )

    def test_cross_crypto_execution_window_rejects_non_grid_decision(self) -> None:
        decision = pd.Timestamp("2026-01-01T12:10:00Z")
        receipt = cross_crypto_execution_window(decision)
        self.assertEqual(receipt["earliest_entry_at"], "2026-01-01T12:11:00+00:00")
        self.assertEqual(receipt["exit_at"], "2026-01-01T12:21:00+00:00")
        with self.assertRaisesRegex(ValueError, "10-minute UTC epoch grid"):
            cross_crypto_execution_window("2026-01-01T12:11:00Z")
        with self.assertRaisesRegex(ValueError, "UTC evaluation-day boundary"):
            cross_crypto_execution_window("2026-01-01T23:50:00Z")
        final = cross_crypto_execution_window("2026-01-01T23:40:00Z")
        self.assertEqual(final["exit_at"], "2026-01-01T23:51:00+00:00")

    def test_cross_crypto_training_window_fails_closed_on_missing_minute(self) -> None:
        rng = np.random.default_rng(7)
        index = pd.date_range("2026-01-01T00:00:00Z", periods=722, freq="1min")
        returns = pd.DataFrame(
            rng.normal(0.0, 0.001, size=(len(index), 30)),
            index=index,
            columns=[f"S{number:02d}" for number in range(30)],
        ).drop(index[-5])
        with self.assertRaisesRegex(ValueError, "missing or irregular minute"):
            causal_training_matrices(
                returns,
                decision_at=index[-1],
                target="S00",
            )

    def test_adaptive_lasso_zero_penalty_matches_least_squares_prediction(self) -> None:
        rng = np.random.default_rng(11)
        x = rng.normal(size=(200, 4))
        truth = np.array([0.5, -0.25, 0.0, 0.1])
        y = 0.02 + x @ truth
        model = fit_adaptive_lasso(x, y, penalty=0.0)
        expected, *_ = np.linalg.lstsq(x, y - y.mean(), rcond=None)
        np.testing.assert_allclose(model["coefficients"], expected, atol=1e-8)
        self.assertTrue(model["converged"])

    def test_cross_crypto_penalty_uses_chronological_return_mse_only(self) -> None:
        rng = np.random.default_rng(23)
        x = rng.normal(size=(720, 4))
        y = 0.001 + 0.4 * x[:, 0] - 0.2 * x[:, 1] + rng.normal(
            0.0, 0.02, size=720
        )
        config = AdaptiveLassoKernelConfig(
            inner_training_pairs=600,
            maximum_iterations=5_000,
        )
        first = select_penalty_by_forward_mse(x, y, config=config)
        second = select_penalty_by_forward_mse(x, y, config=config)
        self.assertEqual(first, second)
        self.assertEqual(first["inner_training_pairs"], 600)
        self.assertEqual(first["inner_validation_pairs"], 120)
        self.assertEqual(
            first["selection_objective"],
            "one_step_return_mse_not_portfolio_pnl",
        )
        self.assertIn(
            first["penalty"],
            [evaluation["penalty"] for evaluation in first["evaluations"]],
        )

    def test_cross_crypto_weights_and_costs_charge_realized_turnover(self) -> None:
        forecasts = {f"S{number:02d}": 0.0 for number in range(30)}
        weights = quintile_target_weights(forecasts)
        self.assertEqual(
            set(symbol for symbol, value in weights.items() if value < 0),
            {"S00", "S01", "S02", "S03", "S04", "S05"},
        )
        self.assertEqual(
            set(symbol for symbol, value in weights.items() if value > 0),
            {"S24", "S25", "S26", "S27", "S28", "S29"},
        )
        self.assertAlmostEqual(sum(weights.values()), 0.0)
        self.assertAlmostEqual(sum(abs(value) for value in weights.values()), 1.0)
        self.assertEqual(decile_target_weights(forecasts), weights)
        entry = cross_crypto_execution_cost(
            {}, weights, fee_per_fill=0.0005, slippage_per_fill=0.0
        )
        exit_cost = cross_crypto_execution_cost(
            weights, {}, fee_per_fill=0.0005, slippage_per_fill=0.0
        )
        self.assertAlmostEqual(entry["turnover"], 1.0)
        self.assertAlmostEqual(entry["total"], 0.0005)
        self.assertAlmostEqual(exit_cost["total"], 0.0005)
        daily = target_schedule_turnover_floor([weights, weights])
        self.assertAlmostEqual(daily["entry_and_rebalance_turnovers"][0], 1.0)
        self.assertAlmostEqual(daily["entry_and_rebalance_turnovers"][1], 0.0)
        self.assertAlmostEqual(daily["final_exit_turnover"], 1.0)
        self.assertAlmostEqual(daily["total_turnover"], 2.0)

    def test_cross_crypto_funding_uses_signed_weight_and_open_interval(self) -> None:
        event_time = int(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc).timestamp() * 1000
        )
        exit_time = int(
            datetime(2026, 1, 1, 8, 1, tzinfo=timezone.utc).timestamp() * 1000
        )
        events = {
            "BTCUSDT": [
                {"fundingTime": event_time, "fundingRate": "0.0001"},
                {"fundingTime": exit_time, "fundingRate": "0.0002"},
            ],
            "ETHUSDT": [
                {"fundingTime": event_time, "fundingRate": "-0.0003"}
            ],
        }
        receipt = funding_pnl_fraction(
            {"BTCUSDT": 0.5, "ETHUSDT": -0.5},
            events,
            entry_at="2026-01-01T07:59:00Z",
            exit_at="2026-01-01T08:01:00Z",
        )
        self.assertEqual(receipt["events_applied"], 2)
        self.assertAlmostEqual(receipt["funding_pnl_fraction"], -0.0002)

    def test_alpha015_daily_simulation_nets_intermediate_rebalances(self) -> None:
        universe = [f"S{number:02d}" for number in range(30)]
        weights = quintile_target_weights(
            {symbol: float(number) for number, symbol in enumerate(universe)}
        )
        prices = {symbol: 100.0 for symbol in weights}
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        decisions = [
            {
                "decision_at": start + pd.to_timedelta(offset * 10, unit="min"),
                "universe_symbols": universe,
                "target_weights": weights,
                "entry_prices": prices,
                "exit_prices": prices,
            }
            for offset in range(143)
        ]
        result = simulate_daily_path(decisions, funding_events={})
        self.assertEqual(result["status"], "PASS")
        self.assertAlmostEqual(result["turnover"]["total"], 2.0)
        self.assertAlmostEqual(result["gross_price_return"], 0.0)
        self.assertAlmostEqual(result["base_net_return"], 0.9995**2 - 1.0)
        self.assertAlmostEqual(result["stress_net_return"], 0.999**2 - 1.0)

    def test_alpha015_daily_simulation_charges_marked_weight_drift(self) -> None:
        universe = [f"S{number:02d}" for number in range(30)]
        weights = quintile_target_weights(
            {symbol: float(number) for number, symbol in enumerate(universe)}
        )
        moved_symbol = next(symbol for symbol, weight in weights.items() if weight > 0)
        boundaries = []
        for offset in range(144):
            prices = {symbol: 100.0 for symbol in weights}
            if offset >= 1:
                prices[moved_symbol] = 110.0
            boundaries.append(prices)
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        decisions = [
            {
                "decision_at": start + pd.to_timedelta(offset * 10, unit="min"),
                "universe_symbols": universe,
                "target_weights": weights,
                "entry_prices": boundaries[offset],
                "exit_prices": boundaries[offset + 1],
            }
            for offset in range(143)
        ]
        result = simulate_daily_path(decisions, funding_events={})
        self.assertGreater(result["turnover"]["total"], 2.0)

    def test_alpha015_minute_mtm_uses_execution_boundaries_and_marks_drawdown(self) -> None:
        universe = [f"S{number:02d}" for number in range(30)]
        weights = quintile_target_weights(
            {symbol: float(number) for number, symbol in enumerate(universe)}
        )
        prices = {symbol: 100.0 for symbol in weights}
        start = pd.Timestamp("2026-01-01T00:00:00Z")
        decisions = [
            {
                "decision_at": start + pd.to_timedelta(offset * 10, unit="min"),
                "universe_symbols": universe,
                "target_weights": weights,
                "entry_prices": prices,
                "exit_prices": prices,
            }
            for offset in range(143)
        ]
        marks = {}
        for decision in decisions:
            entry = decision["decision_at"] + pd.to_timedelta(1, unit="min")
            for offset in range(1, 10):
                marks[entry + pd.to_timedelta(offset, unit="min")] = dict(prices)
        adverse_long = next(symbol for symbol, weight in weights.items() if weight > 0)
        first_mark = min(marks)
        marks[first_mark][adverse_long] = 50.0
        result = simulate_daily_path(
            decisions,
            funding_events={},
            interior_mark_prices=marks,
        )
        base = result["mark_to_market"]["base"]
        stress = result["mark_to_market"]["stress"]
        self.assertEqual(base["boundary_price_policy"], "verified_aggtrade")
        self.assertEqual(base["interior_mark_policy"], "closed_one_minute_mark")
        self.assertGreater(base["maximum_drawdown"], 0.04)
        self.assertGreaterEqual(stress["maximum_drawdown"], base["maximum_drawdown"])
        self.assertAlmostEqual(base["final_return"], result["base_net_return"])
        self.assertAlmostEqual(stress["final_return"], result["stress_net_return"])

    def test_alpha015_development_statistics_use_the_frozen_120_day_screen(self) -> None:
        returns = np.linspace(-0.001, 0.003, 120)
        nw = alpha015_newey_west_mean_t_statistic(returns)
        self.assertEqual(nw["observations"], 120)
        self.assertEqual(nw["maximum_lag_days"], 5)
        self.assertEqual(nw["kernel"], "Bartlett")
        self.assertGreater(nw["t_statistic"], 2.0)
        audit = evaluate_alpha015_development_selection_bias(
            returns,
            returns.reshape(-1, 1),
            trial_ids=["ALPHA_015_FIXED"],
            selected_trial_id="ALPHA_015_FIXED",
            trial_inventory_complete=True,
            pre_registered_single_trial=True,
        )
        self.assertEqual(
            audit["policy"]["minimum_observations"],
            ALPHA015_DEVELOPMENT_SELECTION_POLICY.minimum_observations,
        )
        self.assertEqual(
            audit["pbo"]["status"], "NOT_APPLICABLE_SINGLE_PREREGISTERED_TRIAL"
        )
        with self.assertRaisesRegex(ValueError, "at least 100"):
            alpha015_newey_west_mean_t_statistic(returns[:99])

    def test_alpha015_development_gate_requires_full_source_bound_daily_evidence(self) -> None:
        start = date(2026, 1, 1)
        replays = []
        for offset in range(120):
            base = 0.001 + 0.0015 * np.sin(offset * 0.37)
            stress = 0.0008 + 0.0012 * np.sin(offset * 0.37 + 0.2)
            gross = 0.0015 + 0.0015 * np.sin(offset * 0.37 + 0.1)
            replays.append(
                {
                    "status": "PASS",
                    "evaluation_date": (start + timedelta(days=offset)).isoformat(),
                    "decision_receipts": [{"future_rows_read": 0}] * 143,
                    "source_validation": {
                        "inventory": {"status": "PASS"},
                        "aggtrade_chain": {"status": "PASS"},
                    },
                    "simulation": {
                        "status": "PASS",
                        "base_net_return": base,
                        "stress_net_return": stress,
                        "gross_price_return": gross,
                        "mark_to_market": {
                            "base": {
                                "points": [{"equity": 1.0}, {"equity": 1.0 + base}],
                                "final_equity": 1.0 + base,
                            },
                            "stress": {
                                "points": [{"equity": 1.0}, {"equity": 1.0 + stress}],
                                "final_equity": 1.0 + stress,
                            },
                        },
                    },
                }
            )
        passed = evaluate_alpha015_development(
            replays,
            lookahead_analysis_passed=True,
            future_row_invariance_passed=True,
        )
        self.assertEqual(passed["status"], "PASS")
        self.assertLess(passed["minute_mark_to_market_drawdown"]["stress"], 0.15)
        failed = evaluate_alpha015_development(
            replays,
            lookahead_analysis_passed=True,
            future_row_invariance_passed=False,
        )
        self.assertEqual(failed["status"], "FAIL")
        self.assertFalse(
            next(
                item["passed"]
                for item in failed["checks"]
                if item["name"] == "future_row_invariance"
            )
        )

    def test_alpha015_locked_validation_is_review_only_and_requires_all_sixty_days(self) -> None:
        start = date(2026, 3, 1)
        replays = []
        for offset in range(60):
            base = 0.0015 + 0.0008 * np.sin(offset * 0.41)
            stress = 0.0012 + 0.0006 * np.sin(offset * 0.41 + 0.2)
            if offset in {11, 37}:
                base, stress = -0.0004, -0.0003
            replays.append(
                {
                    "status": "PASS",
                    "evaluation_date": (start + timedelta(days=offset)).isoformat(),
                    "decision_receipts": [{"future_rows_read": 0}] * 143,
                    "source_validation": {
                        "inventory": {"status": "PASS"},
                        "aggtrade_chain": {"status": "PASS"},
                    },
                    "simulation": {
                        "status": "PASS",
                        "base_net_return": base,
                        "stress_net_return": stress,
                        "mark_to_market": {
                            "base": {
                                "points": [{"equity": 1.0}, {"equity": 1.0 + base}],
                                "final_equity": 1.0 + base,
                            },
                            "stress": {
                                "points": [{"equity": 1.0}, {"equity": 1.0 + stress}],
                                "final_equity": 1.0 + stress,
                            },
                        },
                    },
                }
            )
        report = evaluate_alpha015_locked_validation(
            replays,
            all_funding_reconciled=True,
            no_parameter_or_universe_retuning=True,
            lookahead_analysis_passed=True,
            future_row_invariance_passed=True,
        )
        self.assertEqual(report["status"], "REVIEW")
        self.assertFalse(report["promotion_eligible"])
        self.assertEqual(report["newey_west"]["observations"], 60)
        with self.assertRaisesRegex(ValueError, "exactly 60"):
            evaluate_alpha015_locked_validation(
                replays[:-1],
                all_funding_reconciled=True,
                no_parameter_or_universe_retuning=True,
                lookahead_analysis_passed=True,
                future_row_invariance_passed=True,
            )

    def test_alpha015_validation_unlock_requires_a_complete_source_bound_development_pass(self) -> None:
        preregistration_body = {
            "candidate": "ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            "status": "PREREGISTERED_SOURCE_ACQUISITION_LOCKED",
            "return_data_acquired": False,
            "locked_validation_open": False,
            "live_order_authorization": "CLOSED",
            "activation_binding": {"lock_sha256": ""},
        }
        activation_body = {
            "status": "LOCKED_FOR_PREREGISTRATION_ONLY",
            "validation_open": False,
            "split_sha256": "a" * 64,
        }
        activation_lock = {
            **activation_body,
            "lock_sha256": alpha015_canonical_sha256(activation_body),
        }
        preregistration_body["activation_binding"]["lock_sha256"] = activation_lock[
            "lock_sha256"
        ]
        preregistration_body["activation_binding"]["split_sha256"] = activation_lock[
            "split_sha256"
        ]
        preregistration = {
            **preregistration_body,
            "preregistration_sha256": alpha015_canonical_sha256(preregistration_body),
        }
        report = {
            "candidate": preregistration["candidate"],
            "stage": "development",
            "status": "PASS",
            "days": 120,
            "locked_validation_open": False,
            "live_order_authorization": "CLOSED",
            "checks": [{"name": "all", "passed": True}],
            "source": {
                "preregistration_sha256": preregistration["preregistration_sha256"],
                "activation_lock_sha256": activation_lock["lock_sha256"],
                "development_manifest_sha256": "b" * 64,
                "replay_engine_sha256": "c" * 64,
            },
        }
        receipt = build_alpha015_validation_unlock(
            report,
            preregistration,
            activation_lock,
            report_sha256="d" * 64,
            now=datetime(2026, 10, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(receipt["status"], "AUTHORIZED")
        self.assertTrue(receipt["development_gates_pass"])
        self.assertEqual(receipt["calendar_sha256"], "a" * 64)
        self.assertTrue(
            validate_alpha015_validation_unlock(
                receipt,
                report=report,
                preregistration=preregistration,
                activation_lock=activation_lock,
                report_sha256="d" * 64,
            )
        )
        report["checks"][0]["passed"] = False
        with self.assertRaisesRegex(RuntimeError, "not an eligible"):
            build_alpha015_validation_unlock(
                report,
                preregistration,
                activation_lock,
                report_sha256="d" * 64,
                now=datetime(2026, 10, 11, tzinfo=timezone.utc),
            )

    def test_alpha015_funding_reconciliation_requires_all_180_locked_symbol_days(self) -> None:
        start = date(2026, 1, 1)
        authorized = [("BTCUSDT", (start + timedelta(days=offset)).isoformat()) for offset in range(180)]
        daily_events = [
            {
                "symbol": symbol,
                "fundingTime": int(
                    datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp()
                    * 1000
                ),
                "fundingRate": "0.0001",
            }
            for symbol, day in authorized
        ]
        receipt = build_alpha015_funding_reconciliation_receipt(
            candidate="ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
            preregistration_sha256="a" * 64,
            development_report_sha256="b" * 64,
            activation_lock_sha256="c" * 64,
            development_manifest_sha256="d" * 64,
            validation_manifest_sha256="e" * 64,
            authorized_symbol_days=authorized,
            daily_events=daily_events,
            monthly_records=[
                {
                    "symbol": "BTCUSDT",
                    "month": "2026-01-01",
                    "sha256": "f" * 64,
                    "checksum_sha256": "e" * 64,
                    "events": 180,
                }
            ],
            monthly_events=daily_events,
        )
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["authorized_evaluation_days"], 180)
        self.assertTrue(receipt["all_daily_funding_events_reconciled"])
        self.assertTrue(verify_alpha015_funding_reconciliation_receipt(receipt))
        tampered = dict(receipt)
        tampered["authorized_evaluation_days"] = 181
        self.assertFalse(verify_alpha015_funding_reconciliation_receipt(tampered))
        with self.assertRaisesRegex(RuntimeError, "exactly 180"):
            build_alpha015_funding_reconciliation_receipt(
                candidate="ALPHA_015_MINUTE_CROSS_CRYPTO_PREDICTABILITY",
                preregistration_sha256="a" * 64,
                development_report_sha256="b" * 64,
                activation_lock_sha256="c" * 64,
                development_manifest_sha256="d" * 64,
                validation_manifest_sha256="e" * 64,
                authorized_symbol_days=authorized[:-1],
                daily_events=daily_events[:-1],
                monthly_records=[],
                monthly_events=daily_events[:-1],
            )

    def test_alpha015_kline_return_label_and_trade_latency_are_causal(self) -> None:
        start_ms = int(
            datetime(2026, 1, 1, 23, 58, tzinfo=timezone.utc).timestamp() * 1000
        )
        rows = [
            {"open_time": start_ms + offset * 60_000, "close": 100.0 + offset}
            for offset in range(4)
        ]
        panel = returns_from_closed_klines(
            {f"S{number:02d}": rows for number in range(30)}
        )
        self.assertEqual(panel.index[0], pd.Timestamp("2026-01-02T00:00:00Z"))
        self.assertEqual(panel.index[-1], pd.Timestamp("2026-01-02T00:02:00Z"))
        boundary = pd.Timestamp("2026-01-02T00:01:00Z")
        boundary_ms = int(boundary.timestamp() * 1000)
        receipt = first_trade_price_at_or_after(
            [
                {
                    "transact_time": boundary_ms - 1,
                    "price": "100",
                    "agg_trade_id": 10,
                },
                {
                    "transact_time": boundary_ms + 250,
                    "price": "101",
                    "agg_trade_id": 11,
                },
            ],
            boundary_at=boundary,
        )
        self.assertEqual(receipt["price"], 101.0)
        self.assertEqual(receipt["delay_ms"], 250)
        with self.assertRaisesRegex(ValueError, "delay ceiling"):
            first_trade_price_at_or_after(
                [
                    {
                        "transact_time": boundary_ms + 60_001,
                        "price": "101",
                        "agg_trade_id": 11,
                    }
                ],
                boundary_at=boundary,
            )

    def test_alpha015_downloader_binds_archive_to_publisher_checksum(self) -> None:
        day = date(2026, 1, 1)
        spec = alpha015_archive_spec("klines_1m", "BTCUSDT", day)
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            header = (
                "open_time,open,high,low,close,volume,close_time,quote_volume,"
                "count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            )
            start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            rows = [
                f"{start_ms + minute * 60_000},1,1,1,1,1,"
                f"{start_ms + minute * 60_000 + 59_999},1,1,1,1,0"
                for minute in range(1440)
            ]
            archive.writestr(spec["csv_name"], header + "\n".join(rows) + "\n")
        payload = buffer.getvalue()
        checksum = f"{sha256(payload).hexdigest()}  {spec['archive_name']}\n".encode()

        def fetch(url: str) -> bytes:
            return checksum if url.endswith(".CHECKSUM") else payload

        with tempfile.TemporaryDirectory() as directory:
            result = download_and_seal_archive(
                dataset="klines_1m",
                symbol="BTCUSDT",
                day=day,
                output_dir=Path(directory),
                fetch=fetch,
            )
            self.assertEqual(result["status"], "VERIFIED_AND_SEALED")
            self.assertEqual(result["sha256"], sha256(payload).hexdigest())
            self.assertTrue(Path(result["archive"]).is_file())
            self.assertTrue(Path(result["checksum"]).is_file())
            streamed_rows = list(
                iter_reverified_archive_rows(result, archive_root=Path(directory))
            )
            self.assertEqual(len(streamed_rows), 1440)
            self.assertEqual(streamed_rows[0]["open_time"], str(start_ms))
            calendar = build_locked_calendar(
                [day + timedelta(days=offset) for offset in range(180)]
            )
            loaded = assert_manifest_phase_access(
                {"files": [result]},
                calendar,
                archive_root=Path(directory),
                phase="development",
            )
            self.assertEqual(loaded[0]["sha256"], result["sha256"])
            Path(result["archive"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(Alpha015DataError, "SHA-256"):
                assert_manifest_phase_access(
                    {"files": [result]},
                    calendar,
                    archive_root=Path(directory),
                    phase="development",
                )

    def test_alpha015_streaming_execution_replay_uses_first_trade_per_boundary(self) -> None:
        first = pd.Timestamp("2026-01-01T00:01:00Z")
        second = first + pd.to_timedelta(10, unit="min")
        prices = aggregate_trade_boundary_prices(
            [
                {
                    "agg_trade_id": 1,
                    "transact_time": int((first - pd.to_timedelta(1, unit="ms")).timestamp() * 1000),
                    "price": "99",
                },
                {
                    "agg_trade_id": 2,
                    "transact_time": int((first + pd.to_timedelta(200, unit="ms")).timestamp() * 1000),
                    "price": "100",
                },
                {
                    "agg_trade_id": 3,
                    "transact_time": int((second + pd.to_timedelta(500, unit="ms")).timestamp() * 1000),
                    "price": "101",
                },
            ],
            boundaries=[first, second],
        )
        self.assertEqual(prices[first]["price"], 100.0)
        self.assertEqual(prices[second]["price"], 101.0)
        with self.assertRaisesRegex(Alpha015ReplayError, "delay ceiling"):
            aggregate_trade_boundary_prices(
                [
                    {
                        "agg_trade_id": 1,
                        "transact_time": int((first + pd.to_timedelta(60_001, unit="ms")).timestamp() * 1000),
                        "price": "100",
                    }
                ],
                boundaries=[first],
            )

    def test_alpha015_streamed_archive_is_verified_before_atomic_seal(self) -> None:
        day = date(2026, 1, 1)
        spec = alpha015_archive_spec("klines_1m", "BTCUSDT", day)
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            header = (
                "open_time,open,high,low,close,volume,close_time,quote_volume,"
                "count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
            )
            start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            rows = [
                f"{start_ms + minute * 60_000},1,1,1,1,1,"
                f"{start_ms + minute * 60_000 + 59_999},1,1,1,1,0"
                for minute in range(1440)
            ]
            archive.writestr(spec["csv_name"], header + "\n".join(rows) + "\n")
        payload = buffer.getvalue()
        checksum = f"{sha256(payload).hexdigest()}  {spec['archive_name']}\n".encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "partial.download"
            temporary.write_bytes(payload)
            result = seal_downloaded_archive_file(
                temporary,
                checksum,
                dataset="klines_1m",
                symbol="BTCUSDT",
                day=day,
                output_dir=root / "sealed",
            )
            self.assertFalse(temporary.exists())
            self.assertTrue(Path(result["archive"]).is_file())
            self.assertEqual(result["status"], "VERIFIED_AND_SEALED")

            bad = root / "bad.download"
            bad.write_bytes(b"not a zip")
            with self.assertRaisesRegex(Alpha015DataError, "SHA-256"):
                seal_downloaded_archive_file(
                    bad,
                    checksum,
                    dataset="klines_1m",
                    symbol="BTCUSDT",
                    day=day,
                    output_dir=root / "other",
                )
            self.assertTrue(bad.exists())

    def test_alpha015_locked_validation_is_inaccessible_to_development(self) -> None:
        start = date(2026, 1, 1)
        calendar = build_locked_calendar(
            [start + timedelta(days=offset) for offset in range(180)]
        )
        self.assertEqual(
            len(alpha015_authorized_days(calendar, phase="development")), 120
        )
        development_inputs = alpha015_authorized_archive_days(
            calendar,
            phase="development",
            dataset="klines_1m",
        )
        self.assertEqual(len(development_inputs), 121)
        self.assertIn((start - timedelta(days=1)).isoformat(), development_inputs)
        self.assertEqual(
            alpha015_authorized_archive_days(
                calendar,
                phase="development",
                dataset="aggTrades",
            ),
            alpha015_authorized_days(calendar, phase="development"),
        )
        with self.assertRaisesRegex(Alpha015DataError, "remains inaccessible"):
            alpha015_authorized_days(calendar, phase="locked_validation")
        unlock = {
            "status": "AUTHORIZED",
            "development_gates_pass": True,
            "preregistration_sha256": "a" * 64,
            "development_report_sha256": "b" * 64,
            "calendar_sha256": calendar["split_sha256"],
        }
        self.assertEqual(
            len(
                alpha015_authorized_days(
                    calendar,
                    phase="locked_validation",
                    validation_unlock=unlock,
                    expected_preregistration_sha256="a" * 64,
                    expected_development_report_sha256="b" * 64,
                )
            ),
            60,
        )
        validation_inputs = alpha015_authorized_archive_days(
            calendar,
            phase="locked_validation",
            dataset="klines_1m",
            validation_unlock=unlock,
            expected_preregistration_sha256="a" * 64,
            expected_development_report_sha256="b" * 64,
        )
        self.assertEqual(len(validation_inputs), 61)
        self.assertIn(calendar["development"][-1], validation_inputs)
        validation_day = calendar["locked_validation"][0]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Alpha015DataError, "phase boundary"):
                assert_manifest_phase_access(
                    {
                        "files": [
                            {
                                "date": validation_day,
                                "dataset": "klines_1m",
                                "status": "VERIFIED_AND_SEALED",
                                "archive": "must-not-be-exposed.zip",
                            }
                        ]
                    },
                    calendar,
                    archive_root=Path(directory),
                    phase="development",
                )

    def test_alpha015_archive_inventory_uses_exact_prior_day_membership(self) -> None:
        self.assertEqual(binance_archive_symbol("BTC/USDT:USDT"), "BTCUSDT")
        with self.assertRaisesRegex(Alpha015DataError, "USDT-M perpetual"):
            binance_archive_symbol("BTC/USD:BTC")
        start = date(2026, 1, 1)
        development = [
            (start + timedelta(days=offset)).isoformat() for offset in range(120)
        ]
        validation = [
            (start + timedelta(days=120 + offset)).isoformat()
            for offset in range(60)
        ]
        bindings = []
        for evaluation in development + validation:
            evaluation_day = date.fromisoformat(evaluation)
            bindings.append(
                {
                    "evaluation_date": evaluation,
                    "membership_snapshot_date": (
                        evaluation_day - timedelta(days=1)
                    ).isoformat(),
                    "eligible_symbols": [
                        f"S{number:02d}/USDT:USDT" for number in range(30)
                    ],
                }
            )
        lock = {
            "development": development,
            "locked_validation": validation,
            "bindings": bindings,
        }
        specs = required_phase_archive_specs(lock, phase="development")
        keys = {
            (item["dataset"], item["symbol"], item["date"]) for item in specs
        }
        self.assertIn(("klines_1m", "S00USDT", "2025-12-31"), keys)
        self.assertIn(("klines_1m", "S00USDT", "2026-01-01"), keys)
        self.assertIn(("aggTrades", "S00USDT", "2026-01-01"), keys)
        self.assertIn(("funding", "S00USDT", "2026-01-01"), keys)
        self.assertNotIn(("aggTrades", "S00USDT", "2025-12-31"), keys)
        self.assertNotIn(("funding", "S00USDT", "2025-12-31"), keys)
        receipt = validate_phase_archive_inventory(
            {"files": specs}, lock, phase="development"
        )
        self.assertEqual(receipt["status"], "PASS")
        with self.assertRaisesRegex(Alpha015DataError, "inventory mismatch"):
            validate_phase_archive_inventory(
                {"files": specs[:-1]}, lock, phase="development"
            )

    def test_alpha015_aggtrade_chain_rejects_midnight_gap(self) -> None:
        files = [
            {
                "dataset": "aggTrades",
                "symbol": "BTCUSDT",
                "date": "2026-01-01",
                "first_agg_trade_id": 100,
                "last_agg_trade_id": 199,
            },
            {
                "dataset": "aggTrades",
                "symbol": "BTCUSDT",
                "date": "2026-01-02",
                "first_agg_trade_id": 200,
                "last_agg_trade_id": 250,
            },
            {
                "dataset": "aggTrades",
                "symbol": "ETHUSDT",
                "date": "2026-01-01",
                "first_agg_trade_id": 300,
                "last_agg_trade_id": 350,
            },
        ]
        receipt = validate_aggtrade_day_chain(files)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["adjacent_day_seams_checked"], 1)
        files[1]["first_agg_trade_id"] = 201
        with self.assertRaisesRegex(Alpha015DataError, "across UTC days"):
            validate_aggtrade_day_chain(files)

    def test_alpha015_funding_payload_is_canonical_and_sealed(self) -> None:
        day = date(2026, 1, 1)
        timestamp = int(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc).timestamp() * 1000
        )
        raw = json.dumps(
            [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": timestamp,
                    "fundingRate": "0.00010000",
                    "markPrice": "100000.0",
                }
            ]
        ).encode()
        canonical, details = alpha015_validate_funding_payload(
            raw, symbol="BTCUSDT", day=day
        )
        self.assertEqual(details["events"], 1)
        self.assertIn(b'"rateType":"NOT_PROVIDED"', canonical)
        with tempfile.TemporaryDirectory() as directory:
            result = download_and_seal_funding(
                symbol="BTCUSDT",
                day=day,
                output_dir=Path(directory),
                fetch=lambda _: raw,
            )
            self.assertEqual(result["status"], "API_VERIFIED_AND_SEALED")
            self.assertEqual(result["monthly_publisher_reconciliation"], "PENDING")
            loaded = Path(result["file"]).read_bytes()
            self.assertEqual(loaded, canonical)
            self.assertEqual(result["sha256"], sha256(canonical).hexdigest())
            replayed_events = load_reverified_funding_events(
                result, archive_root=Path(directory)
            )
            self.assertEqual(len(replayed_events), details["events"])
            self.assertEqual(replayed_events[0]["fundingTime"], timestamp)

    def test_alpha015_funding_reconciles_to_monthly_publisher_archive(self) -> None:
        month = date(2026, 1, 1)
        spec = monthly_funding_archive_spec("BTCUSDT", month)
        first = int(
            datetime(2026, 1, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        second = int(
            datetime(2026, 1, 1, 8, tzinfo=timezone.utc).timestamp() * 1000
        )
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr(
                spec["csv_name"],
                "calc_time,funding_interval_hours,last_funding_rate\n"
                f"{first},8,0.00010000\n"
                f"{second},8,-0.00020000\n",
            )
        payload = buffer.getvalue()
        checksum = f"{sha256(payload).hexdigest()}  {spec['archive_name']}\n".encode()
        monthly = validate_monthly_funding_archive(
            payload,
            checksum,
            symbol="BTCUSDT",
            month=month,
        )
        daily = [
            {
                "symbol": "BTCUSDT",
                "fundingTime": first,
                "fundingRate": "0.0001",
            },
            {
                "symbol": "BTCUSDT",
                "fundingTime": second,
                "fundingRate": "-0.0002",
            },
        ]
        receipt = reconcile_funding_events(
            daily,
            monthly["events"],
            authorized_symbol_days=[("BTCUSDT", "2026-01-01")],
        )
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["events_reconciled"], 2)
        daily[1]["fundingRate"] = "-0.0003"
        with self.assertRaisesRegex(Alpha015DataError, "does not match"):
            reconcile_funding_events(
                daily,
                monthly["events"],
                authorized_symbol_days=[("BTCUSDT", "2026-01-01")],
            )

    def test_alpha015_aggtrades_rejects_id_gaps(self) -> None:
        day = date(2026, 1, 1)
        spec = alpha015_archive_spec("aggTrades", "BTCUSDT", day)
        start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)

        def payload(second_id: int) -> bytes:
            buffer = BytesIO()
            with ZipFile(buffer, "w") as archive:
                archive.writestr(
                    spec["csv_name"],
                    "agg_trade_id,price,quantity,first_trade_id,last_trade_id,"
                    "transact_time,is_buyer_maker\n"
                    f"10,100,1,20,20,{start_ms},true\n"
                    f"{second_id},101,1,21,21,{start_ms + 1},false\n",
                )
            return buffer.getvalue()

        good = payload(11)
        with tempfile.TemporaryDirectory() as directory:
            result = download_and_seal_archive(
                dataset="aggTrades",
                symbol="BTCUSDT",
                day=day,
                output_dir=Path(directory),
                fetch=lambda url: (
                    f"{sha256(good).hexdigest()}  {spec['archive_name']}\n".encode()
                    if url.endswith(".CHECKSUM")
                    else good
                ),
            )
            self.assertEqual(result["rows"], 2)
        bad = payload(12)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Alpha015DataError, "not continuous"):
                download_and_seal_archive(
                    dataset="aggTrades",
                    symbol="BTCUSDT",
                    day=day,
                    output_dir=Path(directory),
                    fetch=lambda url: (
                        f"{sha256(bad).hexdigest()}  {spec['archive_name']}\n".encode()
                        if url.endswith(".CHECKSUM")
                        else bad
                    ),
                )

    def test_same_asset_spot_perp_hedge_removes_common_price_move(self) -> None:
        self.assertAlmostEqual(
            same_asset_basis_pnl(
                2.0,
                spot_entry=100.0,
                spot_exit=125.0,
                perpetual_entry=101.0,
                perpetual_exit=126.0,
            ),
            0.0,
        )
        self.assertAlmostEqual(
            same_asset_basis_pnl(
                2.0,
                spot_entry=100.0,
                spot_exit=125.0,
                perpetual_entry=102.0,
                perpetual_exit=126.0,
            ),
            2.0,
        )

    def test_same_asset_short_receives_positive_funding(self) -> None:
        self.assertAlmostEqual(short_funding_pnl(2.0, 0.0001, 100.0), 0.02)
        self.assertAlmostEqual(short_funding_pnl(2.0, -0.0001, 100.0), -0.02)

    def test_spot_perp_monthly_boundaries_include_final_utc_day(self) -> None:
        observations = [
            (pd.Timestamp("2022-01-01T00:00:00Z"), 100.0),
            (pd.Timestamp("2022-01-31T12:00:00Z"), 110.0),
            (pd.Timestamp("2022-02-28T12:00:00Z"), 121.0),
        ]
        result = spot_perp_monthly_metrics(
            observations,
            start_at=pd.Timestamp("2022-01-01T00:00:00Z"),
            end_at=pd.Timestamp("2022-03-01T00:00:00Z"),
            starting_equity=100.0,
        )
        self.assertEqual(result["complete_months"], 2)
        self.assertEqual([row["month"] for row in result["series"]], ["2022-01", "2022-02"])
        self.assertAlmostEqual(result["series"][0]["return"], 0.10)
        self.assertAlmostEqual(result["series"][1]["return"], 0.10)

    def test_quarter_hour_flow_waits_for_fixed_buffer_and_charges_funding(self) -> None:
        rows = [
            {
                "agg_trade_id": 1,
                "transact_time": 0,
                "price": 100.0,
                "quantity": 2.0,
                "is_buyer_maker": False,
            },
            {
                "agg_trade_id": 2,
                "transact_time": 9_999,
                "price": 100.0,
                "quantity": 1.0,
                "is_buyer_maker": True,
            },
            {
                "agg_trade_id": 3,
                "transact_time": 10_500,
                "price": 999.0,
                "quantity": 1.0,
                "is_buyer_maker": False,
            },
            {
                "agg_trade_id": 4,
                "transact_time": 11_000,
                "price": 100.0,
                "quantity": 1.0,
                "is_buyer_maker": False,
            },
            {
                "agg_trade_id": 5,
                "transact_time": 43_200_000,
                "price": 110.0,
                "quantity": 1.0,
                "is_buyer_maker": False,
            },
        ]
        trades = simulate_quarter_hour_trade_stream(
            rows,
            boundaries_ms=[0],
            funding_events=[
                (28_800_000, 0.001, 110.0),
                (43_200_000, 0.002, 110.0),
                (43_200_000, -0.003, 110.0),
            ],
            fee_per_side_bps=5,
            slippage_per_side_bps=2,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["entry_time_ms"], 11_000)
        self.assertEqual(trades[0]["direction"], "long")
        self.assertAlmostEqual(trades[0]["order_imbalance"], 1 / 3)
        self.assertAlmostEqual(trades[0]["funding_return"], -0.003 * 110.0 / 100.02)
        self.assertEqual(trades[0]["funding_events"], 2)
        self.assertGreater(trades[0]["net_return"], 0)
        self.assertAlmostEqual(trades[0]["mfe"], 0.10)
        self.assertEqual(trades[0]["mae"], 0.0)
        stressed = reprice_quarter_hour_trades(
            trades, fee_per_side_bps=10, slippage_per_side_bps=5
        )
        self.assertLess(stressed[0]["net_return"], trades[0]["net_return"])

    def test_quarter_hour_flow_fails_closed_on_trade_id_gap(self) -> None:
        rows = [
            {"agg_trade_id": 1, "transact_time": 0, "price": 100, "quantity": 1, "is_buyer_maker": False},
            {"agg_trade_id": 3, "transact_time": 11_000, "price": 100, "quantity": 1, "is_buyer_maker": False},
        ]
        with self.assertRaisesRegex(ValueError, "not continuous"):
            simulate_quarter_hour_trade_stream(
                rows,
                boundaries_ms=[0],
                funding_events=[],
                fee_per_side_bps=5,
                slippage_per_side_bps=2,
            )

    def test_quarter_hour_flow_is_unchanged_by_future_trades(self) -> None:
        base = [
            {"agg_trade_id": 1, "transact_time": 0, "price": 100, "quantity": 2, "is_buyer_maker": False},
            {"agg_trade_id": 2, "transact_time": 9_000, "price": 100, "quantity": 1, "is_buyer_maker": True},
            {"agg_trade_id": 3, "transact_time": 11_000, "price": 100, "quantity": 1, "is_buyer_maker": False},
            {"agg_trade_id": 4, "transact_time": 43_200_000, "price": 101, "quantity": 1, "is_buyer_maker": False},
        ]
        future = base + [
            {"agg_trade_id": 5, "transact_time": 43_200_100, "price": 1, "quantity": 1, "is_buyer_maker": True},
            {"agg_trade_id": 6, "transact_time": 50_000_000, "price": 10_000, "quantity": 1, "is_buyer_maker": False},
        ]
        kwargs = {
            "boundaries_ms": [0],
            "funding_events": [],
            "fee_per_side_bps": 5,
            "slippage_per_side_bps": 2,
        }
        self.assertEqual(
            simulate_quarter_hour_trade_stream(base, **kwargs),
            simulate_quarter_hour_trade_stream(future, **kwargs),
        )

    def test_quarter_hour_short_stop_uses_worse_gap_price(self) -> None:
        rows = [
            {"agg_trade_id": 1, "transact_time": 0, "price": 100, "quantity": 1, "is_buyer_maker": True},
            {"agg_trade_id": 2, "transact_time": 11_000, "price": 100, "quantity": 1, "is_buyer_maker": True},
            {"agg_trade_id": 3, "transact_time": 12_000, "price": 106, "quantity": 1, "is_buyer_maker": False},
        ]
        trades = simulate_quarter_hour_trade_stream(
            rows,
            boundaries_ms=[0],
            funding_events=[],
            fee_per_side_bps=5,
            slippage_per_side_bps=2,
        )
        self.assertEqual(trades[0]["direction"], "short")
        self.assertEqual(trades[0]["exit_reason"], "protective_stop")
        self.assertEqual(trades[0]["raw_exit"], 106.0)
        self.assertLess(trades[0]["mae"], -0.05)

    def test_quarter_hour_summary_and_bootstrap_are_deterministic(self) -> None:
        trades = [
            {
                "net_return": value,
                "direction": "long" if index % 2 == 0 else "short",
                "raw_price_return": value + 0.0014,
                "trading_fee_drag": 0.001,
                "slippage_drag": 0.0004,
                "funding_return": 0.0,
                "exit_reason": "time_exit",
                "mae": min(0.0, value),
                "mfe": max(0.0, value),
                "holding_hours": 12.0,
            }
            for index, value in enumerate([0.01, -0.004] * 10)
        ]
        summary = summarize_quarter_hour_trades(trades)
        self.assertGreater(summary["compounded_net_return"], 0)
        first = quarter_hour_bootstrap_loss_probability(trades, paths=100)
        second = quarter_hour_bootstrap_loss_probability(trades, paths=100)
        self.assertEqual(first, second)

    def test_spot_perp_flow_divergence_uses_only_completed_opposing_flow(self) -> None:
        four_hours = 4 * 60 * 60 * 1000
        spot = [
            {"agg_trade_id": 1, "transact_time": four_hours - 600_000, "price": 99.0, "quantity": 2.0, "is_buyer_maker": True},
            {"agg_trade_id": 2, "transact_time": four_hours - 1, "price": 99.0, "quantity": 1.0, "is_buyer_maker": True},
            {"agg_trade_id": 3, "transact_time": four_hours + 60_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
            {"agg_trade_id": 4, "transact_time": four_hours + 60_000 + four_hours, "price": 110.0, "quantity": 1.0, "is_buyer_maker": False},
            {"agg_trade_id": 5, "transact_time": 2 * four_hours + 60_000 + four_hours, "price": 111.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        perpetual = [
            {"agg_trade_id": 101, "transact_time": four_hours - 600_000, "price": 99.0, "quantity": 2.0, "is_buyer_maker": False},
            {"agg_trade_id": 102, "transact_time": four_hours - 1, "price": 99.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        config = SpotPerpFlowConfig()
        trades = simulate_flow_divergence(
            spot, perpetual, boundaries_ms=[four_hours, 2 * four_hours], config=config
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "simulated_spot_long")
        self.assertAlmostEqual(trades[0]["perp_imbalance"], 1.0)
        self.assertAlmostEqual(trades[0]["spot_imbalance"], -1.0)
        self.assertEqual(trades[0]["entry_time_ms"], four_hours + 60_000)
        self.assertEqual(trades[0]["exit_time_ms"], four_hours + 60_000 + four_hours)
        self.assertEqual(trades[0]["funding_return"], 0.0)
        self.assertLess(trades[0]["net_return"], trades[0]["raw_price_return"])
        stressed = reprice_spot_perp_flow_trades(
            trades, fee_per_side_bps=20.0, slippage_per_side_bps=5.0
        )
        self.assertLess(stressed[0]["net_return"], trades[0]["net_return"])
        summary = summarize_spot_perp_flow_trades(trades)
        self.assertEqual(summary["trades"], 1)
        self.assertEqual(summary["funding_return"], 0.0)
        controls = matched_random_baseline(
            spot,
            boundaries_ms=[four_hours, 2 * four_hours],
            signal_trades=trades,
            config=config,
        )
        self.assertEqual(len(controls), 1)
        bootstrap = daily_block_bootstrap_excess_return(
            trades,
            controls,
            calendar_start_utc=date(1970, 1, 1),
            calendar_end_exclusive_utc=date(1970, 1, 2),
            block_days=1,
            paths=100,
        )
        self.assertIsNotNone(bootstrap["ci95_lower"])

    def test_spot_perp_flow_divergence_rejects_a_trade_id_gap(self) -> None:
        four_hours = 4 * 60 * 60 * 1000
        spot = [
            {"agg_trade_id": 1, "transact_time": four_hours - 600_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": True},
            {"agg_trade_id": 3, "transact_time": four_hours + 60_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        perpetual = [
            {"agg_trade_id": 1, "transact_time": four_hours - 600_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        with self.assertRaisesRegex(ValueError, "spot aggregate-trade IDs are not continuous"):
            simulate_flow_divergence(spot, perpetual, boundaries_ms=[four_hours])

    def test_spot_perp_flow_divergence_rejects_delayed_reference_or_coverage_gap(self) -> None:
        four_hours = 4 * 60 * 60 * 1000
        spot = [
            {"agg_trade_id": 1, "transact_time": four_hours - 600_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": True},
            {"agg_trade_id": 2, "transact_time": four_hours - 1, "price": 100.0, "quantity": 1.0, "is_buyer_maker": True},
            {"agg_trade_id": 3, "transact_time": four_hours + 180_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        perpetual = [
            {"agg_trade_id": 1, "transact_time": four_hours - 600_000, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
            {"agg_trade_id": 2, "transact_time": four_hours - 1, "price": 100.0, "quantity": 1.0, "is_buyer_maker": False},
        ]
        with self.assertRaisesRegex(ValueError, "entry reference delay"):
            simulate_flow_divergence(spot, perpetual, boundaries_ms=[four_hours])
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            validate_trade_stream_coverage(
                spot,
                market="spot",
                start_ms=four_hours,
                end_ms=four_hours + 180_000,
                maximum_intertrade_gap_ms=60_000,
            )

    def test_spot_perp_flow_bootstrap_preserves_zero_signal_calendar_days(self) -> None:
        day_ms = 24 * 60 * 60 * 1000
        signals = [
            {"signal_at_ms": 0, "net_return": 0.01},
            {"signal_at_ms": 4 * day_ms, "net_return": 0.01},
            {"signal_at_ms": 11 * day_ms, "net_return": 0.01},
        ]
        controls = [
            {"net_return": 0.0},
            {"net_return": 0.0},
            {"net_return": 0.0},
        ]
        result = daily_block_bootstrap_excess_return(
            signals,
            controls,
            calendar_start_utc=date(1970, 1, 1),
            calendar_end_exclusive_utc=date(1970, 1, 15),
            block_days=7,
            paths=100,
        )
        self.assertEqual(result["days"], 14)
        self.assertEqual(result["signal_days"], 3)

    def test_spot_perp_flow_protocol_hides_oos_until_all_development_checks_pass(self) -> None:
        preregistration = {
            "candidate": "ALPHA_016_SPOT_PERP_FLOW_DIVERGENCE",
            "status": "PREREGISTERED_HISTORICAL_DEVELOPMENT_LOCKED",
            "research_scope": {
                "orders_submitted": False,
                "real_or_test_positions": False,
                "live_promotion_eligible": False,
            },
            "development_gate": {
                "complete_utc_days_minimum": 90,
                "natural_virtual_trades_minimum": 100,
                "profit_factor_minimum": 1.15,
            },
            "locked_oos_gate": {
                "complete_utc_days_minimum": 90,
                "natural_virtual_trades_minimum": 100,
                "profit_factor_minimum": 1.20,
            },
        }
        development = spot_perp_flow_phase_calendar("development")
        oos = spot_perp_flow_phase_calendar("locked_oos")
        self.assertEqual(development["decision_count"], 539)
        self.assertEqual(oos["decision_count"], 539)
        with self.assertRaisesRegex(SpotPerpFlowProtocolError, "remains unopened"):
            build_spot_perp_flow_phase_plan(preregistration, phase="locked_oos")
        failed = evaluate_spot_perp_flow_gate(
            preregistration,
            phase="development",
            evidence={},
        )
        self.assertEqual(failed["status"], "FAIL")
        with self.assertRaisesRegex(SpotPerpFlowProtocolError, "did not pass"):
            build_spot_perp_flow_phase_plan(
                preregistration, phase="locked_oos", development_report=failed
            )
        passed = evaluate_spot_perp_flow_gate(
            preregistration,
            phase="development",
            evidence={
                "complete_utc_days": 90,
                "natural_virtual_trades": 100,
                "net_return": 0.01,
                "profit_factor": 1.2,
                "double_fee_net_return": 0.01,
                "slippage_stress_net_return": 0.01,
                "random_control_excess_return": 0.01,
                "bootstrap_ci95_lower": 0.001,
                "data_integrity_pass": True,
                "lookahead_pass": True,
            },
        )
        plan = build_spot_perp_flow_phase_plan(
            preregistration, phase="locked_oos", development_report=passed
        )
        self.assertEqual(plan["status"], "AUTHORIZED_PLAN_NO_SOURCE_BYTES_ACQUIRED")
        self.assertFalse(plan["orders_submitted"])

    def test_spot_perp_flow_data_admission_checks_market_schema_and_day_chain(self) -> None:
        day = date(2025, 1, 1)
        header = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker,is_best_match\n"
        row = "100,100.0,0.1,100,100,1735689600000,False,True\n"
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "BTCUSDT-aggTrades-2025-01-01.zip"
            with ZipFile(archive, "w") as output:
                output.writestr("BTCUSDT-aggTrades-2025-01-01.csv", header + row)
            metadata = inspect_spot_perp_flow_archive(
                archive,
                market="spot",
                symbol="BTCUSDT",
                day=day,
                expected_digest=sha256(archive.read_bytes()).hexdigest(),
            )
            self.assertEqual(metadata["rows"], 1)
            archive_without_header = Path(directory) / "BTCUSDT-aggTrades-2025-01-01-no-header.zip"
            with ZipFile(archive_without_header, "w") as output:
                output.writestr("BTCUSDT-aggTrades-2025-01-01.csv", row)
            self.assertEqual(
                inspect_spot_perp_flow_archive(
                    archive_without_header,
                    market="spot",
                    symbol="BTCUSDT",
                    day=day,
                    expected_digest=sha256(archive_without_header.read_bytes()).hexdigest(),
                )["rows"],
                1,
            )
            microsecond_row = "100,100.0,0.1,100,100,1735689600000866,False,True\n"
            archive_with_microseconds = Path(directory) / "BTCUSDT-aggTrades-2025-01-01-microseconds.zip"
            with ZipFile(archive_with_microseconds, "w") as output:
                output.writestr("BTCUSDT-aggTrades-2025-01-01.csv", microsecond_row)
            microsecond_metadata = inspect_spot_perp_flow_archive(
                archive_with_microseconds,
                market="spot",
                symbol="BTCUSDT",
                day=day,
                expected_digest=sha256(archive_with_microseconds.read_bytes()).hexdigest(),
            )
            self.assertEqual(microsecond_metadata["first_transact_time_ms"], 1735689600000)
            self.assertEqual(microsecond_metadata["transact_time_precision"], "microseconds")
            chained = verify_spot_perp_flow_day_chains(
                [
                    {**metadata, "date": "2025-01-01", "first_agg_trade_id": 100, "last_agg_trade_id": 101},
                    {**metadata, "date": "2025-01-02", "first_agg_trade_id": 102, "last_agg_trade_id": 103},
                ]
            )
            self.assertEqual(chained["status"], "PASS")
            with self.assertRaisesRegex(SpotPerpFlowDataError, "discontinuous"):
                verify_spot_perp_flow_day_chains(
                    [
                        {**metadata, "date": "2025-01-01", "first_agg_trade_id": 100, "last_agg_trade_id": 101},
                        {**metadata, "date": "2025-01-02", "first_agg_trade_id": 103, "last_agg_trade_id": 103},
                    ]
                )

    def test_spot_perp_flow_streaming_extractor_bounds_gaps_and_references(self) -> None:
        minute = 60_000
        boundary = 20 * minute
        rows = [
            {
                "agg_trade_id": index,
                "transact_time": index * minute,
                "price": 100.0 + index,
                "quantity": 1.0,
                "is_buyer_maker": index % 2 == 0,
            }
            for index in range(1, 28)
        ]
        result = extract_spot_perp_flow_market_observations(
            rows,
            market="spot",
            boundaries_ms=[boundary],
            coverage_start_ms=10 * minute,
            coverage_end_ms=28 * minute,
            config=SpotPerpFlowConfig(
                observation_window_ms=10 * minute,
                entry_delay_ms=minute,
                holding_ms=4 * minute,
                maximum_reference_delay_ms=minute,
                maximum_intertrade_gap_ms=minute,
            ),
            spot_references=True,
        )
        self.assertEqual(result["coverage"]["status"], "PASS")
        self.assertEqual(result["spot_references"][boundary].entry_time_ms, 21 * minute)
        self.assertEqual(result["spot_references"][boundary].exit_time_ms, 25 * minute)
        time_gap_rows = [
            {**row, "agg_trade_id": index}
            for index, row in enumerate(
                (row for row in rows if row["transact_time"] != 15 * minute), start=1
            )
        ]
        with self.assertRaisesRegex(SpotPerpFlowEvaluationError, "coverage gap"):
            extract_spot_perp_flow_market_observations(
                time_gap_rows,
                market="perpetual",
                boundaries_ms=[boundary],
                coverage_start_ms=10 * minute,
                coverage_end_ms=28 * minute,
                config=SpotPerpFlowConfig(
                    observation_window_ms=10 * minute,
                    maximum_intertrade_gap_ms=minute,
                ),
            )

    def test_spot_perp_flow_streaming_results_preserve_nonoverlap_and_controls(self) -> None:
        boundary = 20 * 60_000
        later_boundary = boundary + 4 * 60 * 60_000
        references = {
            boundary: SpotReference(boundary + 60_000, 100.0, boundary + 4 * 60 * 60_000 + 60_000, 101.0),
            later_boundary: SpotReference(later_boundary + 60_000, 101.0, later_boundary + 4 * 60 * 60_000 + 60_000, 102.0),
        }
        spot = {"imbalances": {boundary: -0.2, later_boundary: -0.2}, "spot_references": references}
        perpetual = {"imbalances": {boundary: 0.3, later_boundary: 0.3}}
        trades = simulate_extracted_spot_perp_flow(
            spot, perpetual, boundaries_ms=[boundary, later_boundary]
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["signal_at_ms"], boundary)
        controls = matched_spot_perp_flow_controls(
            trades, references=references, boundaries_ms=[boundary, later_boundary]
        )
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["signal_at_ms"], later_boundary)

    def test_metrics_normalization_collapses_only_exact_duplicates(self) -> None:
        times = pd.date_range("2021-01-01", periods=288, freq="5min", tz="UTC")
        frame = pd.DataFrame(
            {
                "create_time": list(times) + [times[10]],
                "symbol": ["BTCUSDT"] * 289,
                "sum_open_interest": list(np.arange(288, dtype=float)) + [10.0],
            }
        )
        normalized, integrity = normalize_metrics_day(frame, day="2021-01-01")
        self.assertEqual(len(normalized), 288)
        self.assertEqual(integrity["exact_duplicate_rows_removed"], 1)
        self.assertEqual(integrity["missing_grid_rows"], 0)

    def test_metrics_normalization_rejects_conflicting_duplicates(self) -> None:
        timestamp = pd.Timestamp("2021-01-01T00:00:00Z")
        frame = pd.DataFrame(
            {
                "create_time": [timestamp, timestamp],
                "symbol": ["BTCUSDT", "BTCUSDT"],
                "sum_open_interest": [100.0, 101.0],
            }
        )
        with self.assertRaisesRegex(ValueError, "conflicting rows"):
            normalize_metrics_day(frame, day="2021-01-01")

    def test_metrics_normalization_preserves_missing_grid_rows_as_nan(self) -> None:
        times = pd.date_range("2021-01-01", periods=288, freq="5min", tz="UTC").delete(17)
        frame = pd.DataFrame(
            {
                "create_time": times,
                "symbol": ["BTCUSDT"] * len(times),
                "sum_open_interest": np.arange(len(times), dtype=float),
            }
        )
        normalized, integrity = normalize_metrics_day(frame, day="2021-01-01")
        self.assertEqual(len(normalized), 288)
        self.assertEqual(integrity["missing_grid_rows"], 1)
        missing = normalized.loc[
            normalized["create_time"] == pd.Timestamp("2021-01-01T01:25:00Z"),
            "sum_open_interest",
        ]
        self.assertTrue(missing.isna().all())

    def test_leverage_crowding_features_are_unchanged_by_future_rows(self) -> None:
        base_index = pd.date_range("2021-01-01", periods=288 * 200, freq="5min", tz="UTC")
        full_index = pd.date_range("2021-01-01", periods=288 * 210, freq="5min", tz="UTC")

        def frames(index: pd.DatetimeIndex):
            sequence = np.arange(len(index), dtype=float)
            metrics = pd.DataFrame(
                {
                    "create_time": index,
                    "sum_open_interest": 100_000 + sequence + 200 * np.sin(sequence / 500),
                }
            )
            price = 30_000 + sequence * 0.02 + 100 * np.sin(sequence / 300)
            candles = pd.DataFrame(
                {"date": index, "open": price, "high": price + 10, "close": price + 1}
            )
            funding = pd.DataFrame(
                {"date": index[::96], "funding_rate": [0.0001] * len(index[::96])}
            )
            return metrics, candles, funding

        short = build_decision_frame(*frames(base_index))
        extended = build_decision_frame(*frames(full_index))
        causal_columns = [
            column
            for column in short.columns
            if column not in {"planned_exit_open", "forward_return_8h"}
        ]
        pd.testing.assert_frame_equal(
            short[causal_columns],
            extended.loc[short.index, causal_columns],
            check_freq=False,
        )

    def test_leverage_crowding_short_execution_includes_costs_and_funding(self) -> None:
        decision_time = pd.Timestamp("2025-01-01T00:00:00Z")
        entry_time = decision_time + timedelta(minutes=5)
        exit_time = entry_time + timedelta(hours=8)
        index = pd.date_range(entry_time, exit_time, freq="5min", tz="UTC")
        candles = pd.DataFrame(
            {
                "date": index,
                "open": [100.0] * (len(index) - 1) + [99.0],
                "high": [101.0] * len(index),
                "close": [100.0] * len(index),
            }
        )
        funding = pd.DataFrame(
            {"date": [pd.Timestamp("2025-01-01T08:00:00Z")], "funding_rate": [0.001]}
        )
        decisions = pd.DataFrame(
            {
                "signal": [True],
                "entry_time": [entry_time],
                "planned_exit_time": [exit_time],
                "forward_return_8h": [-0.01],
            },
            index=pd.DatetimeIndex([decision_time]),
        )
        end = pd.Timestamp("2025-01-02T00:00:00Z")
        trades = simulate_trades(
            decisions,
            candles,
            funding,
            start=decision_time,
            end_exclusive=end,
            fee_per_side_bps=5,
            slippage_per_side_bps=2,
        )
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["exit_reason"], "time_exit")
        self.assertAlmostEqual(trades[0]["funding_return"], 0.001)
        self.assertGreater(trades[0]["net_return"], 0)
        summary = summarize_trades(
            trades,
            decisions,
            start=decision_time,
            end_exclusive=end,
        )
        self.assertEqual(summary["maximum_drawdown"], 0.0)
        self.assertEqual(summary["trades"], 1)

    def test_forward_mtm_audit_accepts_current_protocol_and_excludes_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "research" / "equity" / "raw"
            evidence_root.mkdir(parents=True)
            current_protocol = {"strategy.py": "current"}

            current = evidence_root / "current.jsonl"
            current.write_text('{"equity": 1000}\n', encoding="utf-8")
            current.with_suffix(".manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "ACTIVE",
                        "evidence_kind": "freqtrade_dryrun_mark_to_market",
                        "equity_path": str(current.relative_to(root)),
                        "protocol_sha256": current_protocol,
                        "source": "authenticated_loopback_freqtrade_net_liquidation",
                    }
                ),
                encoding="utf-8",
            )

            legacy = evidence_root / "legacy.jsonl"
            legacy.write_text('{"equity": 900}\n', encoding="utf-8")
            legacy.with_suffix(".manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "ACTIVE",
                        "evidence_kind": "freqtrade_dryrun_mark_to_market",
                        "equity_path": str(legacy.relative_to(root)),
                        "protocol_sha256": {"strategy.py": "legacy"},
                        "source": "authenticated_loopback_freqtrade_balance_total_bot",
                    }
                ),
                encoding="utf-8",
            )

            audit = audit_mtm_segments(
                [legacy, current], project_root=root, current_protocol=current_protocol
            )

        self.assertEqual(audit["status"], "PASS")
        self.assertEqual(audit["eligible_paths"], [current])
        self.assertEqual(audit["eligible_segments"], 1)
        self.assertEqual(audit["excluded_segments"], 1)
        self.assertEqual(audit["segments"][0]["exclusion_reason"], "LEGACY_PROTOCOL")

    def test_forward_mtm_audit_rejects_tampered_sealed_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_root = root / "research" / "equity" / "raw"
            evidence_root.mkdir(parents=True)
            current_protocol = {"strategy.py": "current"}
            evidence = evidence_root / "sealed.jsonl"
            original = '{"equity": 1000}\n'
            evidence.write_text(original, encoding="utf-8")
            evidence.with_suffix(".manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "SEALED_ON_STOP",
                        "evidence_kind": "freqtrade_dryrun_mark_to_market",
                        "equity_path": str(evidence.relative_to(root)),
                        "protocol_sha256": current_protocol,
                        "source": "authenticated_loopback_freqtrade_net_liquidation",
                        "seal": {
                            "bytes": len(original.encode()),
                            "sha256": sha256(original.encode()).hexdigest(),
                            "observations": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )
            evidence.write_text(original + '{"equity": 2000}\n', encoding="utf-8")

            audit = audit_mtm_segments(
                [evidence], project_root=root, current_protocol=current_protocol
            )

        self.assertEqual(audit["status"], "FAIL_CLOSED")
        self.assertEqual(audit["eligible_paths"], [])
        self.assertEqual(audit["segments"][0]["integrity"], "FAIL")
        self.assertIn("sealed MTM segment integrity failure", audit["segments"][0]["exclusion_reason"])

    def test_forward_report_integrity_failure_overrides_performance_status(self) -> None:
        report = {"status": "PASS", "warning": "performance warning"}
        result = enforce_data_integrity(report, {"status": "FAIL_CLOSED"})
        self.assertEqual(result["status"], "FAIL_CLOSED_DATA_INTEGRITY")
        self.assertIn("must not be used", result["warning"])

        report = {"status": "PASS", "warning": "performance warning"}
        result = enforce_data_integrity(report, {"status": "PASS"}, protocol_current=False)
        self.assertEqual(result["status"], "FAIL_CLOSED_PROTOCOL_DRIFT")
        self.assertIn("must not be used", result["warning"])

    def test_forward_mtm_uses_complete_calendar_months_and_stays_accumulating(self) -> None:
        observations = []
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for day in range(32):
            observed = start + timedelta(days=day)
            equity = 1000.0 if day < 31 else 1100.0
            observations.append(
                {
                    "observed_at": observed.isoformat().replace("+00:00", "Z"),
                    "equity": equity,
                    "equity_type": "mark_to_market",
                    "source": "freqtrade_rest_net_liquidation_equity",
                    "starting_capital": 1000.0,
                    "dry_run_note": "Simulated balances",
                }
            )
        mtm = summarize_mtm(observations, maximum_gap_seconds=86_400)
        self.assertEqual(mtm["monthly"]["complete_months"], 1)
        self.assertAlmostEqual(mtm["monthly"]["series"][0]["return"], 0.10)

        trades = {
            "all_trades": 0,
            "open_trades": 0,
            "closed_trades": 0,
            "natural_closed_trades": 0,
            "forced_closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "net_profit": 0.0,
            "profit_factor": None,
            "trading_fees": 0.0,
            "funding_pnl": 0.0,
            "favorable_funding_removed": 0.0,
            "adverse_funding_paid": 0.0,
            "funding_stress_net_profit": 0.0,
            "price_pnl_estimate": 0.0,
            "double_fee_net_profit": 0.0,
            "slippage_bps_per_side": 5.0,
            "modeled_slippage": 0.0,
            "slippage_stress_net_profit": 0.0,
            "trade_risk": {
                "excursion_coverage_ratio": 0.0,
                "slippage_coverage_ratio": 0.0,
                "funding_coverage_ratio": 0.0,
                "maximum_leverage": None,
                "maximum_leverage_adjusted_mae": None,
                "liquidation_exits": 0,
            },
        }
        report = build_forward_report(observations=observations, trades=trades, protocol_current=True)
        self.assertEqual(report["status"], "ACCUMULATING")
        self.assertEqual(report["policy_scope"]["cohort"], "FROZEN_LEGACY_FORWARD_COHORT")
        self.assertFalse(report["policy_scope"]["current_capital_preservation_policy_eligibility"])

    def test_forward_trade_loader_excludes_forced_exits_and_decomposes_costs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "forward.sqlite"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    "CREATE TABLE trades (id INTEGER, is_open BOOLEAN, exit_reason TEXT, close_profit_abs REAL, "
                    "fee_open_cost REAL, fee_close_cost REAL, funding_fees REAL, open_rate REAL, min_rate REAL, "
                    "max_rate REAL, is_short BOOLEAN, leverage REAL, close_rate REAL, amount REAL, contract_size REAL)"
                )
                connection.execute(
                    "INSERT INTO trades VALUES (1, 0, 'exit_signal', 10, 1, 1, 3, 100, 90, 120, 0, 2, 110, 1, 1)"
                )
                connection.execute(
                    "INSERT INTO trades VALUES (2, 0, 'force_exit', 100, 1, 1, 0, 100, 90, 110, 0, 1, 105, 1, 1)"
                )
                connection.commit()
            finally:
                connection.close()
            result = load_forward_trades(database)
        self.assertEqual(result["closed_trades"], 2)
        self.assertEqual(result["natural_closed_trades"], 1)
        self.assertEqual(result["forced_closed_trades"], 1)
        self.assertEqual(result["net_profit"], 10.0)
        self.assertEqual(result["trading_fees"], 2.0)
        self.assertEqual(result["funding_pnl"], 3.0)
        self.assertEqual(result["favorable_funding_removed"], 3.0)
        self.assertEqual(result["adverse_funding_paid"], 0.0)
        self.assertEqual(result["funding_stress_net_profit"], 7.0)
        self.assertEqual(result["price_pnl_estimate"], 9.0)
        self.assertEqual(result["double_fee_net_profit"], 8.0)
        self.assertAlmostEqual(result["modeled_slippage"], 0.105)
        self.assertAlmostEqual(result["slippage_stress_net_profit"], 9.895)
        self.assertEqual(result["trade_risk"]["slippage_coverage_ratio"], 1.0)
        self.assertEqual(result["trade_risk"]["funding_coverage_ratio"], 1.0)
        self.assertAlmostEqual(result["trade_risk"]["maximum_leverage_adjusted_mae"], 0.2)

    def test_funding_carry_uses_mark_notional_and_opposite_payment_signs(self) -> None:
        index = pd.date_range("2025-01-01", periods=9, freq="1h", tz="UTC")
        price = pd.DataFrame(
            {"open": [100.0] * 9, "high": [100.0] * 9, "low": [100.0] * 9, "close": [100.0] * 9},
            index=index,
        )
        mark = price.copy()
        mark.loc[index[-1], "close"] = 1000.0  # Future within the funding hour must not affect payment.
        funding = pd.DataFrame({"funding_rate": [0.001]}, index=[index[-1]])
        market = {"price": price, "mark": mark, "funding": funding}
        config = CarryConfig(fee_per_fill=0.0, slippage_per_fill=0.0)
        long = _simulate_leg(
            market,
            symbol="BTC",
            side="long",
            entry_at=index[0],
            planned_exit_at=index[-1],
            notional=100.0,
            config=config,
        )
        short = _simulate_leg(
            market,
            symbol="BTC",
            side="short",
            entry_at=index[0],
            planned_exit_at=index[-1],
            notional=100.0,
            config=config,
        )
        self.assertAlmostEqual(long["funding_pnl"], -0.1)
        self.assertAlmostEqual(short["funding_pnl"], 0.1)
        self.assertAlmostEqual(long["net_pnl"], -0.1)
        self.assertAlmostEqual(short["net_pnl"], 0.1)

    def test_funding_dispersion_forward_uses_only_predecision_score_and_costs(self) -> None:
        index = pd.date_range(
            "2026-10-12T00:00:00Z", "2026-10-26T05:00:00Z", freq="1h"
        )
        decision = pd.Timestamp("2026-10-19T04:00:00Z")
        entry = decision + timedelta(hours=1)
        exit_at = entry + timedelta(days=7)
        funding_index = pd.date_range(
            "2026-10-12T00:00:00Z", "2026-10-26T00:00:00Z", freq="8h"
        )
        markets = {}
        funding_rates = {
            "BTCUSDT": -0.0001,
            "ETHUSDT": 0.0001,
            "BNBUSDT": 0.0,
            "SOLUSDT": 0.0,
            "XRPUSDT": 0.0,
        }
        for symbol in funding_rates:
            price = pd.DataFrame(
                {"open": 100.0, "high": 100.2, "low": 99.8, "close": 100.0},
                index=index,
            )
            holding = price.index >= entry
            progress = np.linspace(0.0, 1.0, int(holding.sum()))
            if symbol == "BTCUSDT":
                price.loc[holding, "open"] = 100.0 + progress
                price.loc[holding, "close"] = 100.0 + progress
                price.loc[holding, "high"] = 100.2 + progress
                price.loc[holding, "low"] = 99.8 + progress
            elif symbol == "ETHUSDT":
                price.loc[holding, "open"] = 100.0 - progress
                price.loc[holding, "close"] = 100.0 - progress
                price.loc[holding, "high"] = 100.2 - progress
                price.loc[holding, "low"] = 99.8 - progress
            funding = pd.DataFrame(
                {"funding_rate": funding_rates[symbol]}, index=funding_index
            )
            markets[symbol] = {"price": price, "mark": price.copy(), "funding": funding}

        config = FundingDispersionConfig(maximum_development_weeks=1)
        result = simulate_funding_dispersion_forward(
            markets, first_decision=decision, config=config
        )
        self.assertEqual(result["eligible_weeks"], 1)
        self.assertEqual(result["weeks"][0]["long_symbol"], "BTCUSDT")
        self.assertEqual(result["weeks"][0]["short_symbol"], "ETHUSDT")
        self.assertGreater(result["pnl_components"]["raw_relative_price_pnl"], 0)
        self.assertLess(result["pnl_components"]["trading_fees"], 0)
        self.assertLess(result["pnl_components"]["modeled_slippage"], 0)

        self.assertEqual(result["liquidations"], 0)

        future_changed = {
            symbol: {name: frame.copy() for name, frame in market.items()}
            for symbol, market in markets.items()
        }
        future_changed["SOLUSDT"]["funding"].loc[
            future_changed["SOLUSDT"]["funding"].index > decision, "funding_rate"
        ] = 1.0
        rerun = simulate_funding_dispersion_forward(
            future_changed, first_decision=decision, config=config
        )
        self.assertEqual(rerun["weeks"][0]["long_symbol"], "BTCUSDT")
        self.assertEqual(rerun["weeks"][0]["short_symbol"], "ETHUSDT")
        self.assertEqual(rerun["weeks"][0]["score_spread"], result["weeks"][0]["score_spread"])

    def test_alpha013_registry_sync_never_promotes_development_pass(self) -> None:
        base = {
            "eligible_weeks": 30,
            "closed_legs": 60,
            "net_return": 0.12,
            "profit_factor_by_week": 1.4,
            "maximum_mark_to_market_drawdown": 0.08,
            "largest_winning_week_concentration": 0.2,
            "median_score_spread": 0.004,
            "pnl_components": {
                "raw_relative_price_pnl": 150.0,
                "trading_fees": -20.0,
                "funding_pnl": 5.0,
                "modeled_slippage": -15.0,
                "net_pnl": 120.0,
            },
        }
        report = {
            "status": "PASS_DEVELOPMENT",
            "data_integrity": {
                "source_inventory_sha256": "a" * 64,
                "protocol_sha256": {"strategy.py": "b" * 64},
            },
            "development": {
                "base": base,
                "double_fee": {"net_return": 0.08},
                "five_bps_slippage": {"net_return": 0.06},
            },
            "gate": {"eligible_weeks_minimum_30": True},
        }
        result = build_alpha013_registry_result(
            report,
            report_sha256="c" * 64,
            evidence_archive="research/evidence/alpha013/c.json.gz",
            preregistration_sha256="d" * 64,
        )
        self.assertEqual(alpha013_registry_status("PASS_DEVELOPMENT"), "REVIEW")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["gate"]["status"], "REVIEW")
        self.assertFalse(result["promotion_eligible"])
        self.assertFalse(result["locked_validation_opened"])
        self.assertEqual(result["development"]["organic"]["trades"], 30)

        events = [
            {
                "event_id": 1,
                "experiment_id": 13,
                "event_type": "result_update",
                "payload": {"result": result},
            }
        ]
        self.assertEqual(
            alpha013_latest_snapshot_key(events, 13),
            ("PASS_DEVELOPMENT", 30),
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            alpha013_registry_status("PASS")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            source_hash, archive_path = archive_alpha013_report(
                report_path, root / "evidence"
            )
            self.assertEqual(source_hash, sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(gzip.decompress(archive_path.read_bytes()), report_path.read_bytes())

    def test_alpha013_causal_veto_is_not_attributed_as_pnl_failure(self) -> None:
        audit = {
            "schema_version": 1,
            "kind": "FUNDING_DECISION_AVAILABILITY_AUDIT",
            "status": "CAUSAL_AVAILABILITY_VETO",
            "preregistration_sha256": "a" * 64,
            "promotion_eligible": False,
            "live_order_authorization": "CLOSED",
            "elapsed_decisions": 1,
            "verified_decisions": [],
            "failures": [{"decision_at": "2026-10-19T04:00:00+00:00", "reason": "missing_receipt"}],
        }
        validate_alpha013_causal_veto_audit(audit, preregistration_sha256="a" * 64)
        result = build_alpha013_causal_veto_registry_result(
            audit,
            audit_sha256="b" * 64,
            evidence_archive="research/evidence/alpha013-causal-availability/b.json.gz",
            preregistration_sha256="a" * 64,
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["non_performance_veto"])
        scoreboard = build_scoreboard(
            {
                "experiments": [{"experiment_id": 13, "strategy": "ALPHA_013", "result": {}}],
                "events": [{"event_id": 1, "experiment_id": 13, "event_type": "result_update", "payload": {"result": result}}],
            }
        )
        row = scoreboard["experiments_detail"][0]
        self.assertEqual(row["primary_failure"], "CAUSAL_AVAILABILITY_VETO")
        self.assertNotIn("NO_SIGNAL_EDGE", row["failure_reasons"])
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            validate_alpha013_causal_veto_audit(
                dict(audit, preregistration_sha256="c" * 64),
                preregistration_sha256="a" * 64,
            )

    def test_alpha013_validation_lock_uses_next_monday_and_requires_full_development_pass(self) -> None:
        self.assertEqual(
            next_monday_0400(datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)),
            datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_monday_0400(datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)),
            datetime(2026, 10, 19, 4, 0, tzinfo=timezone.utc),
        )
        acquisition = {"acquisition.py": "a" * 64}
        validation = {"validation.py": "b" * 64}
        report = {
            "status": "PASS_DEVELOPMENT",
            "locked_validation_opened": False,
            "data_integrity": {"protocol_sha256": acquisition},
            "gate": {"eligible_weeks_minimum_30": True, "net_return_positive": True},
        }
        lock = build_alpha013_validation_lock(
            report,
            {
                "valid": True,
                "registry_event_id": 50,
                "source_inventory_sha256": "c" * 64,
            },
            report_sha256="d" * 64,
            receipt_sha256="e" * 64,
            acquisition_protocol=acquisition,
            validation_protocol=validation,
            now=datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(lock["first_validation_decision_utc"], "2026-10-19T04:00:00+00:00")
        self.assertEqual(lock["required_eligible_weeks"], 26)
        self.assertFalse(lock["promotion_eligible"])
        failed = json.loads(json.dumps(report))
        failed["gate"]["net_return_positive"] = False
        with self.assertRaisesRegex(ValueError, "not eligible"):
            build_alpha013_validation_lock(
                failed,
                {"valid": True, "registry_event_id": 50, "source_inventory_sha256": "c" * 64},
                report_sha256="d" * 64,
                receipt_sha256="e" * 64,
                acquisition_protocol=acquisition,
                validation_protocol=validation,
                now=datetime(2026, 10, 12, tzinfo=timezone.utc),
            )

    def test_alpha013_locked_validation_gate_and_registry_mapping_are_strict(self) -> None:
        base = {
            "eligible_weeks": 26,
            "closed_legs": 52,
            "net_return": 0.01,
            "profit_factor_by_week": 1.25,
            "maximum_mark_to_market_drawdown": 0.12,
            "largest_winning_week_concentration": 0.20,
            "liquidations": 0,
            "pnl_components": {
                "raw_relative_price_pnl": 20.0,
                "price_after_fees_and_slippage": 10.0,
                "trading_fees": -4.0,
                "funding_pnl": 1.0,
                "modeled_slippage": -6.0,
                "net_pnl": 11.0,
            },
        }
        stress = {"net_return": 0.001}
        checks = alpha013_locked_validation_gate(
            base, stress, stress, funding_reconciled=True
        )
        self.assertTrue(all(checks.values()))
        concentrated = dict(base)
        concentrated["largest_winning_week_concentration"] = 0.200001
        self.assertFalse(
            alpha013_locked_validation_gate(
                concentrated, stress, stress, funding_reconciled=True
            )["largest_winning_week_concentration_at_most_0_20"]
        )
        report = {
            "status": "PASS",
            "validation_protocol_sha256": {"validation.py": "a" * 64},
            "data_integrity": {"source_inventory_sha256": "b" * 64},
            "validation": {"base": base},
            "gate": checks,
        }
        result = build_alpha013_validation_registry_result(
            report,
            report_sha256="c" * 64,
            lock_sha256="d" * 64,
            evidence_archive="research/evidence/alpha013-validation/result.json.gz",
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["gate"]["stage"], "locked_forward_validation")
        self.assertFalse(result["promotion_eligible"])
        self.assertTrue(result["locked_validation_opened"])

    def test_alpha013_development_receipt_detects_report_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            research = root / "research"
            evidence = research / "evidence"
            reports.mkdir()
            evidence.mkdir(parents=True)
            preregistration = research / "alpha013-preregistration.json"
            preregistration.write_text('{"candidate":"alpha013"}\n', encoding="utf-8")
            report = {
                "status": "PASS_DEVELOPMENT",
                "data_integrity": {
                    "source_inventory_sha256": "a" * 64,
                    "protocol_sha256": {"sealed.py": "b" * 64},
                },
            }
            report_path = reports / "alpha013-forward.json"
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            report_hash = sha256(report_path.read_bytes()).hexdigest()
            archive = evidence / "report.json.gz"
            archive.write_bytes(gzip.compress(report_path.read_bytes(), mtime=0))
            registry = research / "registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "event_id": 51,
                                "payload": {
                                    "result": {
                                        "source_sha256": report_hash,
                                        "source_inventory_sha256": "a" * 64,
                                        "development_status": "PASS_DEVELOPMENT",
                                    }
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            synchronizer = root / "sync.py"
            synchronizer.write_text("# frozen\n", encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "status": "PASS",
                "report_status": "PASS_DEVELOPMENT",
                "report_sha256": report_hash,
                "preregistration_sha256": sha256(preregistration.read_bytes()).hexdigest(),
                "source_inventory_sha256": "a" * 64,
                "protocol_sha256": {"sealed.py": "b" * 64},
                "synchronizer_sha256": sha256(synchronizer.read_bytes()).hexdigest(),
                "source_replay": "PASS",
                "evidence_archive": "research/evidence/report.json.gz",
                "registry_event_id": 51,
                "promotion_eligible": False,
                "locked_validation_opened": False,
            }
            receipt_path = reports / "alpha013-development-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertTrue(
                alpha013_development_receipt_health(
                    receipt_path=receipt_path,
                    report_path=report_path,
                    registry_path=registry,
                    preregistration_path=preregistration,
                    synchronizer_path=synchronizer,
                    project_root=root,
                )["valid"]
            )
            report_path.write_text(json.dumps({**report, "tampered": True}), encoding="utf-8")
            self.assertFalse(
                alpha013_development_receipt_health(
                    receipt_path=receipt_path,
                    report_path=report_path,
                    registry_path=registry,
                    preregistration_path=preregistration,
                    synchronizer_path=synchronizer,
                    project_root=root,
                )["valid"]
            )

    def test_alpha013_funding_publication_lag_is_not_mislabeled_as_strategy_failure(self) -> None:
        checks = {
            "net_return_positive": True,
            "profit_factor_minimum": True,
            "all_funding_rows_reconciled": False,
        }
        self.assertTrue(
            alpha013_development_funding_pending(
                {"status": "FAIL_DEVELOPMENT", "gate": checks}
            )
        )
        self.assertTrue(alpha013_validation_funding_pending(checks))
        economic_failure = {**checks, "net_return_positive": False}
        self.assertFalse(
            alpha013_development_funding_pending(
                {"status": "FAIL_DEVELOPMENT", "gate": economic_failure}
            )
        )
        self.assertFalse(alpha013_validation_funding_pending(economic_failure))

    def test_alpha011_terminal_receipt_is_source_bound_and_review_only(self) -> None:
        protocol = {"frozen.py": "a" * 64}
        base = {
            "trades": 120,
            "sum_net_return": 0.14,
            "compounded_net_return": 0.15,
            "profit_factor": 1.3,
            "price_return_sum": 0.22,
            "trading_fee_drag_sum": 0.04,
            "funding_return_sum": -0.01,
            "slippage_drag_sum": 0.03,
            "maximum_drawdown": 0.12,
            "top_three_winner_profit_concentration": 0.4,
            "long_expectancy": 0.001,
            "short_expectancy": 0.001,
            "mae_mean": -0.002,
            "mfe_mean": 0.004,
            "median_holding_hours": 8.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw_root = root / "research" / "alpha011" / "raw" / "BTCUSDT"
            raw_root.mkdir(parents=True)
            manifest_path = raw_root / "manifest.json"
            manifest_path.write_text('{"sealed":true}\n', encoding="utf-8")
            report = {
                "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
                "status": "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
                "protocol_sha256": protocol,
                "validation_opened": False,
                "data_integrity": {
                    "status": "PASS",
                    "manifest": str(manifest_path.relative_to(root)),
                    "verified_days_used": 61,
                    "complete_development_days": 60,
                },
                "development": {"base": base},
                "development_gate": {
                    "checks": [{"name": "all", "passed": True}]
                },
            }
            reports = root / "reports"
            reports.mkdir()
            report_path = reports / "alpha011-development.json"
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            report_hash = sha256(report_path.read_bytes()).hexdigest()
            manifest_hash = sha256(manifest_path.read_bytes()).hexdigest()
            result = build_alpha011_registry_result(
                report,
                report_sha256=report_hash,
                manifest_sha256=manifest_hash,
                evidence_archive="research/evidence/alpha011/report.json.gz",
            )
            self.assertEqual(alpha011_registry_status(report["status"]), "REVIEW")
            self.assertEqual(result["status"], "REVIEW")
            self.assertFalse(result["promotion_eligible"])
            self.assertFalse(result["locked_validation_opened"])

            registry_path = root / "research" / "registry.json"
            registry_path.parent.mkdir(exist_ok=True)
            registry_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiments": [],
                        "events": [
                            {
                                "event_id": 7,
                                "payload": {"result": result},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            synchronizer_path = root / "sync.py"
            synchronizer_path.write_text("# sealed synchronizer\n", encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "status": "PASS",
                "report_status": report["status"],
                "report_sha256": report_hash,
                "manifest_sha256": manifest_hash,
                "protocol_sha256": protocol,
                "synchronizer_sha256": sha256(
                    synchronizer_path.read_bytes()
                ).hexdigest(),
                "source_replay": "PASS",
                "registry_event_id": 7,
                "promotion_eligible": False,
                "locked_validation_opened": False,
            }
            receipt_path = reports / "alpha011-development-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            health = alpha011_receipt_health(
                receipt_path=receipt_path,
                report_path=report_path,
                registry_path=registry_path,
                project_root=root,
                expected_protocol=protocol,
                synchronizer_path=synchronizer_path,
            )
            self.assertTrue(health["valid"])
            report_path.write_text(json.dumps(report) + " \n", encoding="utf-8")
            tampered = alpha011_receipt_health(
                receipt_path=receipt_path,
                report_path=report_path,
                registry_path=registry_path,
                project_root=root,
                expected_protocol=protocol,
                synchronizer_path=synchronizer_path,
            )
            self.assertFalse(tampered["valid"])

            source_hash, archive_path = archive_alpha011_report(
                report_path, root / "evidence"
            )
            self.assertEqual(source_hash, sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(gzip.decompress(archive_path.read_bytes()), report_path.read_bytes())

    def test_alpha011_validation_lock_starts_next_utc_day_and_is_review_only(self) -> None:
        protocol = {"frozen.py": "a" * 64}
        validation_protocol = {"validation.py": "b" * 64}
        report = {
            "status": "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
            "validation_opened": False,
            "protocol_sha256": protocol,
            "development_gate": {
                "status": "PASS",
                "checks": [
                    {"name": "net_return_positive", "passed": True},
                    {"name": "profit_factor_minimum", "passed": True},
                ],
            },
        }
        lock = build_alpha011_validation_lock(
            report,
            {"valid": True, "registry_event_id": 46},
            report_sha256="c" * 64,
            development_receipt_sha256="d" * 64,
            development_protocol=protocol,
            validation_protocol=validation_protocol,
            now=datetime(2026, 12, 10, 23, 59, tzinfo=timezone.utc),
        )
        self.assertEqual(lock["validation_start_utc"], "2026-12-11T00:00:00+00:00")
        self.assertEqual(lock["required_complete_utc_days"], 60)
        self.assertEqual(lock["required_archives_for_final_exit"], 61)
        self.assertEqual(lock["development_receipt_sha256"], "d" * 64)
        self.assertFalse(lock["promotion_eligible"])

        partial = json.loads(json.dumps(report))
        partial["development_gate"]["checks"][0]["passed"] = False
        with self.assertRaisesRegex(ValueError, "not eligible"):
            build_alpha011_validation_lock(
                partial,
                {"valid": True, "registry_event_id": 46},
                report_sha256="c" * 64,
                development_receipt_sha256="d" * 64,
                development_protocol=protocol,
                validation_protocol=validation_protocol,
                now=datetime(2026, 12, 10, tzinfo=timezone.utc),
            )

    def test_alpha011_locked_validation_boundaries_and_gate_are_exact(self) -> None:
        boundaries = alpha011_validation_boundaries(date(2026, 12, 11))
        self.assertEqual(len(boundaries), 120)
        self.assertEqual(
            datetime.fromtimestamp(boundaries[0] / 1000, timezone.utc).isoformat(),
            "2026-12-11T00:15:00+00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(boundaries[-1] / 1000, timezone.utc).isoformat(),
            "2027-02-08T12:15:00+00:00",
        )
        base = {
            "trades": 100,
            "compounded_net_return": 0.01,
            "profit_factor": 1.20,
            "maximum_drawdown": 0.20,
            "top_three_winner_profit_concentration": 0.35,
            "long_expectancy": 0.0001,
            "short_expectancy": 0.0001,
        }
        stress = {"compounded_net_return": 0.0001}
        checks = alpha011_locked_validation_gate(
            base,
            stress,
            stress,
            complete_days=60,
            bootstrap_loss_probability=0.05,
        )
        self.assertTrue(all(checks.values()))
        concentrated = dict(base)
        concentrated["top_three_winner_profit_concentration"] = 0.3500001
        checks = alpha011_locked_validation_gate(
            concentrated,
            stress,
            stress,
            complete_days=60,
            bootstrap_loss_probability=0.05,
        )
        self.assertFalse(checks["top_three_winner_profit_concentration_maximum"])

    def test_alpha011_validation_registry_result_is_source_bound_and_not_live_eligible(self) -> None:
        report = {
            "status": "PASS",
            "source_manifest": "research/alpha011/raw/BTCUSDT/manifest.json",
            "validation_protocol_sha256": {"validation.py": "a" * 64},
            "validation": {
                "base": {
                    "trades": 110,
                    "sum_net_return": 0.08,
                    "compounded_net_return": 0.083,
                    "profit_factor": 1.25,
                    "price_return_sum": 0.15,
                    "trading_fee_drag_sum": 0.055,
                    "funding_return_sum": -0.002,
                    "slippage_drag_sum": 0.013,
                }
            },
            "gate": {
                "status": "PASS",
                "checks": [{"name": "profit_factor_minimum", "passed": True}],
            },
        }
        result = build_alpha011_validation_registry_result(
            report,
            report_sha256="b" * 64,
            manifest_sha256="c" * 64,
            lock_sha256="d" * 64,
            evidence_archive="research/evidence/alpha011-validation/report.json.gz",
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["gate"]["stage"], "locked_forward_validation")
        self.assertEqual(result["validation_lock_sha256"], "d" * 64)
        self.assertFalse(result["promotion_eligible"])
        self.assertTrue(result["locked_validation_opened"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "validation.json"
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            source_hash, archive_path = archive_alpha011_validation_report(
                report_path, root / "evidence"
            )
            self.assertEqual(source_hash, sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(gzip.decompress(archive_path.read_bytes()), report_path.read_bytes())

    def test_alpha011_validation_lock_rechecks_development_evidence_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            report_path = reports / "alpha011-development.json"
            receipt_path = reports / "alpha011-development-receipt.json"
            report_path.write_text('{"status":"sealed"}\n', encoding="utf-8")
            receipt_path.write_text('{"status":"PASS"}\n', encoding="utf-8")
            development_protocol = {"development.py": "a" * 64}
            validation_protocol = {"validation.py": "b" * 64}
            lock = {
                "status": "OPEN",
                "immutable": True,
                "candidate": "ALPHA_011_QUARTER_HOUR_FLOW_CONTINUATION",
                "validation_protocol_sha256": validation_protocol,
                "development_protocol_sha256": development_protocol,
                "required_complete_utc_days": 60,
                "required_archives_for_final_exit": 61,
                "promotion_eligible": False,
                "development_report": "reports/alpha011-development.json",
                "development_report_sha256": sha256(report_path.read_bytes()).hexdigest(),
                "development_receipt": "reports/alpha011-development-receipt.json",
                "development_receipt_sha256": sha256(receipt_path.read_bytes()).hexdigest(),
                "development_registry_event_id": 46,
                "validation_start_utc": "2026-12-11T00:00:00+00:00",
            }
            audit = validate_alpha011_lock_chain(
                lock,
                project_root=root,
                expected_validation_protocol=validation_protocol,
                expected_development_protocol=development_protocol,
                development_receipt_audit={
                    "valid": True,
                    "report_status": "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
                    "registry_event_id": 46,
                },
            )
            self.assertEqual(audit["status"], "PASS")
            report_path.write_text('{"status":"tampered"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "report has changed"):
                validate_alpha011_lock_chain(
                    lock,
                    project_root=root,
                    expected_validation_protocol=validation_protocol,
                    expected_development_protocol=development_protocol,
                    development_receipt_audit={
                        "valid": True,
                        "report_status": "DEVELOPMENT_PASS_LOCKED_VALIDATION_UNOPENED",
                        "registry_event_id": 46,
                    },
                )

    def test_perpetual_basis_catchup_uses_completed_signal_candle_and_delayed_entry(self) -> None:
        index = pd.date_range(
            "2021-12-31T23:00:00Z", "2022-01-02T01:00:00Z", freq="1h"
        )
        signal_at = pd.Timestamp("2021-12-31T23:00:00Z")
        entry_at = pd.Timestamp("2022-01-01T01:00:00Z")
        exit_at = pd.Timestamp("2022-01-02T01:00:00Z")
        markets = {}
        for symbol in ("BTC", "ETH", "BNB", "SOL", "XRP"):
            perpetual = pd.DataFrame(
                {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
                index=index,
            )
            spot = perpetual.copy()
            if symbol == "BTC":
                perpetual.loc[signal_at, "close"] = 99.0
                progress = np.linspace(0.0, 2.0, len(perpetual.loc[entry_at:exit_at]))
                perpetual.loc[entry_at:exit_at, "open"] = 100.0 + progress
                perpetual.loc[entry_at:exit_at, "close"] = 100.0 + progress
                perpetual.loc[entry_at:exit_at, "high"] = 100.1 + progress
                perpetual.loc[entry_at:exit_at, "low"] = 99.9 + progress
            elif symbol == "XRP":
                perpetual.loc[signal_at, "close"] = 101.0
                progress = np.linspace(0.0, 1.0, len(perpetual.loc[entry_at:exit_at]))
                perpetual.loc[entry_at:exit_at, "open"] = 100.0 - progress
                perpetual.loc[entry_at:exit_at, "close"] = 100.0 - progress
                perpetual.loc[entry_at:exit_at, "high"] = 100.1 - progress
                perpetual.loc[entry_at:exit_at, "low"] = 99.9 - progress
            markets[symbol] = {
                "spot": spot,
                "perpetual": perpetual,
                "mark": perpetual.copy(),
                "funding": pd.DataFrame(
                    {"funding_rate": []}, index=pd.DatetimeIndex([], tz="UTC")
                ),
            }

        result = simulate_perp_basis_development(
            markets,
            start="2022-01-01T00:00:00Z",
            end_exclusive="2022-01-03T00:00:00Z",
            config=PerpBasisCatchupConfig(fee_per_fill=0.0, slippage_per_fill=0.0),
        )
        self.assertEqual(result["closed_daily_spreads"], 1)
        self.assertEqual(result["days"][0]["signal_at"], signal_at.isoformat())
        self.assertEqual(result["days"][0]["entry_at"], entry_at.isoformat())
        self.assertEqual(result["days"][0]["long_symbol"], "BTC")
        self.assertEqual(result["days"][0]["short_symbol"], "XRP")
        self.assertGreater(result["pnl_components"]["raw_relative_price_pnl"], 0)
        self.assertGreater(perpetual_basis_score(100.0, 99.0), 0)

    def test_perpetual_basis_catchup_fails_closed_on_missing_holding_hour(self) -> None:
        index = pd.date_range(
            "2021-12-31T23:00:00Z", "2022-01-02T01:00:00Z", freq="1h"
        )
        markets = {}
        for symbol in ("BTC", "ETH", "BNB", "SOL", "XRP"):
            frame = pd.DataFrame(
                {"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0},
                index=index,
            )
            spot = frame.copy()
            spot.loc[index[0], "close"] += {
                "BTC": 2.0, "ETH": 1.0, "BNB": 0.0, "SOL": -1.0, "XRP": -2.0
            }[symbol]
            if symbol == "BTC":
                frame = frame.drop(pd.Timestamp("2022-01-01T12:00:00Z"))
            markets[symbol] = {
                "spot": spot,
                "perpetual": frame,
                "mark": frame.copy(),
                "funding": pd.DataFrame(
                    {"funding_rate": []}, index=pd.DatetimeIndex([], tz="UTC")
                ),
            }
        with self.assertRaisesRegex(ValueError, "incomplete holding window"):
            simulate_perp_basis_development(
                markets,
                start="2022-01-01T00:00:00Z",
                end_exclusive="2022-01-03T00:00:00Z",
                config=PerpBasisCatchupConfig(fee_per_fill=0.0, slippage_per_fill=0.0),
            )

    def test_inverse_xbt_episode_reconstruction(self) -> None:
        start = datetime(2025, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        fills = [
            {
                "time": start,
                "exec_id": "1",
                "order_id": "entry",
                "side": "Buy",
                "qty": 10.0,
                "price": 100.0,
                "commission_xbt": 0.0,
                "liquidity": "AddedLiquidity",
                "liquidation": False,
            },
            {
                "time": start + timedelta(minutes=5),
                "exec_id": "2",
                "order_id": "exit",
                "side": "Sell",
                "qty": 10.0,
                "price": 110.0,
                "commission_xbt": 0.0,
                "liquidity": "RemovedLiquidity",
                "liquidation": False,
            },
        ]
        episodes, open_position = reconstruct_xbtusd_episodes(fills)
        self.assertEqual(open_position, 0.0)
        self.assertEqual(len(episodes), 1)
        self.assertAlmostEqual(episodes[0]["net_pnl_xbt"], 10 * (1 / 100 - 1 / 110))
        self.assertAlmostEqual(episodes[0]["net_return_proxy"], episodes[0]["net_pnl_xbt"] * 100 / 10)
        summary = summarize_episodes(episodes)
        self.assertEqual(summary["episodes"], 1)
        self.assertEqual(summary["behavior_segments"]["entry_order_count"]["one"]["episodes"], 1)

    def test_behavior_features_and_labels_are_causal(self) -> None:
        start = datetime(2024, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
        bars = []
        for index in range(260):
            timestamp = start + timedelta(hours=index)
            price = 100.0 + index * 0.1
            bars.append(
                {
                    "timestamp": timestamp.isoformat() + "Z",
                    "open": price - 0.05,
                    "high": price + 0.1,
                    "low": price - 0.1,
                    "close": price,
                    "volume": 1000 + index,
                }
            )
        features = build_causal_features(bars)
        entry_time = start + timedelta(hours=230, minutes=15)
        episodes = [
            {
                "side": "long",
                "start": entry_time,
                "end": entry_time + timedelta(minutes=30),
                "net_pnl_xbt": 1.0,
                "net_return_proxy": 0.01,
                "duration_minutes": 30.0,
                "maker_entry_qty": 1.0,
                "taker_entry_qty": 0.0,
                "maker_exit_qty": 1.0,
                "taker_exit_qty": 0.0,
            }
        ]
        candidates, labeled = attach_episode_labels(features, episodes)
        action = candidates[candidates["action"] == 1].iloc[0]
        self.assertEqual(action["timestamp"], start + timedelta(hours=230))
        self.assertEqual(int(action["direction"]), 1)
        self.assertEqual(len(labeled), 1)

    def test_aoa_forward_audit_binds_locked_strategy_result_and_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            strategy_path = root / "user_data/strategies/AoaDistilledPolicyPrototype.py"
            model_path = root / "research/aoa-behavior-model-spec.json"
            config_path = root / "user_data/config.futures.aoa-distilled.research.json"
            for path in (strategy_path, model_path, config_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            strategy_path.write_text("# frozen strategy\n", encoding="utf-8")
            model_path.write_text("{}\n", encoding="utf-8")
            config_path.write_text("{}\n", encoding="utf-8")
            preregistration_path = root / "research/aoa-forward-preregistration.json"
            preregistration = {
                "schema_version": 1,
                "registered_at_utc": "2026-10-06T00:00:00Z",
                "policy": "AoaDistilledPolicyPrototype",
                "status_before_open": "LOCKED_UNSEEN",
                "locked_timerange": "20260901-20260916",
                "decision_rule": {"pass_allowed": False},
                "frozen_artifacts": {
                    str(path.relative_to(root)): "sha256:" + sha256(path.read_bytes()).hexdigest()
                    for path in (strategy_path, model_path, config_path)
                },
            }
            preregistration_path.write_text(
                json.dumps(preregistration) + "\n", encoding="utf-8"
            )
            preregistration_sha256 = sha256(
                preregistration_path.read_bytes()
            ).hexdigest()
            result_path = root / "backtest-result.zip"
            result = {
                "strategy": {
                    "AoaDistilledPolicyPrototype": {
                        "trades": [],
                        "starting_balance": 1000.0,
                        "backtest_days": 15,
                        "backtest_start": "2026-09-01 00:00:00",
                        "backtest_end": "2026-09-16 00:00:00",
                        "timerange": "20260901-20260916",
                        "enable_protections": True,
                        "daily_profit": [],
                        "max_drawdown_account": 0.0,
                        "total_volume": 0.0,
                        "winning_days": 0,
                    }
                }
            }
            embedded_config = {
                "strategy": "AoaDistilledPolicyPrototype",
                "timeframe": "5m",
                "dry_run": True,
                "trading_mode": "futures",
                "margin_mode": "isolated",
                "max_open_trades": 1,
                "exchange": {"pair_whitelist": ["BTC/USDT:USDT"]},
            }
            with ZipFile(result_path, "w") as archive:
                archive.writestr("result.json", json.dumps(result))
                archive.writestr("result_config.json", json.dumps(embedded_config))
                archive.writestr(
                    "result_AoaDistilledPolicyPrototype.py",
                    strategy_path.read_bytes(),
                )
            result_path.with_suffix(".meta.json").write_text(
                json.dumps(
                    {
                        "AoaDistilledPolicyPrototype": {
                            "timeframe": "5m",
                            "timeframe_detail": None,
                            "notes": (
                                "AOA_FORWARD_LOCKED_V1 fee_per_fill=0.0005 "
                                f"prereg_sha256={preregistration_sha256}"
                            ),
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            data_path = root / "BTC-5m.feather"
            data_path.write_bytes(b"sealed-market-data")
            report = audit_aoa_forward(
                project_root=root,
                preregistration_path=preregistration_path,
                result_path=result_path,
                data_paths=[data_path],
            )
            self.assertEqual(report["status"], "HISTORICAL_HOLDOUT_COMPLETE_NO_ACTIVITY")
            self.assertFalse(report["promotion_eligible"])
            self.assertEqual(report["metrics"]["organic_trades"], 0)
            self.assertFalse(report["integrity"]["registration_precedes_sample"])

            strategy_path.write_text("# tampered strategy\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "frozen artifact changed"):
                audit_aoa_forward(
                    project_root=root,
                    preregistration_path=preregistration_path,
                    result_path=result_path,
                    data_paths=[data_path],
                )

    def test_registry_is_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ExperimentRegistry(Path(directory) / "registry.json")
            first = registry.register(strategy="A", hypothesis="h1", reason="r1", result={"status": "REVIEW"})
            second = registry.register(
                strategy="B",
                hypothesis="h2",
                reason="r2",
                result={"status": "FAIL"},
                parent_experiment=first["experiment_id"],
                reason_for_change="pre-registered second hypothesis",
            )
            self.assertEqual((first["experiment_id"], second["experiment_id"]), (1, 2))
            self.assertEqual(second["parent_experiment"], 1)
            self.assertIsNone(second["trial_accounting"])
            event = registry.append_event(
                experiment_id=first["experiment_id"],
                event_type="result_update",
                payload={"result": {"gate": {"status": "FAIL"}, "development": {"organic": {"trades": 5}}}},
            )
            stored = json.loads((Path(directory) / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(event["event_id"], 1)
            self.assertEqual(len(stored["experiments"]), 2)
            self.assertEqual(len(stored["events"]), 1)

    def test_registry_unique_strategy_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = ExperimentRegistry(Path(directory) / "registry.json")
            arguments = {
                "strategy": "ALPHA_015_UNIQUE",
                "hypothesis": "fixed",
                "reason": "test",
                "result": {"status": "ACCUMULATING"},
                "trial_accounting": {"alpha_number": 15},
            }
            first, first_created = registry.register_unique_strategy(**arguments)
            second, second_created = registry.register_unique_strategy(**arguments)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first, second)
            stored = json.loads(
                (Path(directory) / "registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(stored["experiments"]), 1)

    def test_registry_serializes_concurrent_event_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            registry = ExperimentRegistry(path)
            experiment = registry.register(
                strategy="CONCURRENT",
                hypothesis="lock",
                reason="test",
                result={"status": "ACCUMULATING"},
            )
            code = (
                "from pathlib import Path; "
                "from quant.research.registry import ExperimentRegistry; "
                f"ExperimentRegistry(Path({str(path)!r})).append_event("
                f"experiment_id={experiment['experiment_id']}, event_type='probe', "
                "payload={'writer': __import__('sys').argv[1]})"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(index)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for index in range(12)
            ]
            failures = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=10)
                if process.returncode != 0:
                    failures.append((process.returncode, stdout, stderr))
            self.assertEqual(failures, [])
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["events"]), 12)
            self.assertEqual(
                sorted(item["event_id"] for item in stored["events"]),
                list(range(1, 13)),
            )

    def test_alpha007_registry_snapshot_is_compact_and_exact_report_is_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "alpha007.json"
            report = {
                "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
                "status": "ACCUMULATING",
                "runs": 2,
                "eligible_timing_runs": 1,
                "excluded_timing_runs": 1,
                "collection_totals": {"timing_eligible_runs": {"feature_rows": 10}},
                "coverage": [{"day": "2026-10-10", "complete": False}],
                "complete_days": 0,
                "all_detected_signals": 3,
                "incomplete_day_signals_excluded": 3,
                "phases": {"LOCKED_OOS": {"eligible_shadow_orders": 0}},
                "checks": [{"name": "complete_collection_days", "passed": False}],
                "data_integrity": {
                    "status": "PASS_WITH_TIMING_EXCLUSIONS",
                    "verified_runs": [
                        {"run_id": "one", "large": "x" * 10_000},
                        {"run_id": "two", "large": "y" * 10_000},
                    ],
                    "fingerprinted_protocol_variants": 1,
                },
            }
            report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

            compact = compact_accumulation(report)
            self.assertNotIn("verified_runs", compact["data_integrity"])
            self.assertEqual(compact["data_integrity"]["verified_run_count"], 2)
            self.assertEqual(compact["checks"], report["checks"])
            self.assertLess(len(json.dumps(compact)), len(report_path.read_text()) // 10)

            source_hash, archive_path = archive_report(report_path, root / "evidence")
            self.assertEqual(source_hash, sha256(report_path.read_bytes()).hexdigest())
            self.assertEqual(gzip.decompress(archive_path.read_bytes()), report_path.read_bytes())
            second_hash, second_path = archive_report(report_path, root / "evidence")
            self.assertEqual((second_hash, second_path), (source_hash, archive_path))

            legacy_event = {"payload": {"source_sha256": source_hash, "result": {"accumulation": report}}}
            self.assertFalse(event_has_compact_archive(legacy_event, source_hash))
            compact_event = {
                "payload": {
                    "source_sha256": source_hash,
                    "result": {
                        "source_sha256": source_hash,
                        "evidence_archive": str(archive_path),
                        "evidence_encoding": "gzip",
                        "accumulation": compact,
                    },
                }
            }
            self.assertTrue(event_has_compact_archive(compact_event, source_hash))

    def test_alpha007_shadow_pass_is_review_only_and_never_promotion_eligible(self) -> None:
        report = {
            "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
            "status": "PASS",
            "basis": "shadow only",
            "configuration": {},
            "runs": 400,
            "eligible_timing_runs": 400,
            "excluded_timing_runs": 0,
            "collection_totals": {},
            "coverage": [],
            "complete_days": 17,
            "all_detected_signals": 1500,
            "incomplete_day_signals_excluded": 0,
            "phases": {"LOCKED_OOS": {"fully_filled_orders": 220}},
            "checks": [{"name": "locked_queue_fills", "passed": True}],
            "warning": "shadow",
            "data_integrity": {"status": "PASS", "verified_runs": []},
        }
        result = build_alpha007_registry_result(
            report,
            source_hash="a" * 64,
            evidence_archive="research/evidence/alpha007/result.json.gz",
        )
        self.assertEqual(alpha007_registry_status("PASS"), "REVIEW")
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["gate"]["status"], "REVIEW")
        self.assertEqual(result["gate"]["stage"], "locked_oos")
        self.assertFalse(result["promotion_eligible"])
        self.assertTrue(result["locked_validation_opened"])
        self.assertNotIn("verified_runs", result["accumulation"]["data_integrity"])
        self.assertEqual(alpha007_registry_status("FAIL"), "FAIL")

    def test_alpha007_terminal_health_requires_valid_sealed_receipt(self) -> None:
        self.assertEqual(
            resolve_alpha007_terminal_health(
                "RUNNING_BETWEEN_CHUNKS", {"status": "ACCUMULATING"}, {"valid": False}
            ),
            "RUNNING_BETWEEN_CHUNKS",
        )
        self.assertEqual(
            resolve_alpha007_terminal_health(
                "COMPLETE", {"status": "PASS"}, {"valid": False}
            ),
            "TERMINAL_RECEIPT_INVALID",
        )
        self.assertEqual(
            resolve_alpha007_terminal_health(
                "COMPLETE", {"status": "PASS"}, {"valid": True}
            ),
            "SHADOW_REVIEW_SEALED",
        )
        self.assertEqual(
            resolve_alpha007_terminal_health(
                "COMPLETE",
                {"status": "PASS"},
                {"valid": True, "partial_fill_veto_status": "FAIL"},
            ),
            "SHADOW_REVIEW_AUXILIARY_VETOED",
        )
        self.assertEqual(
            resolve_alpha007_terminal_health(
                "COMPLETE", {"status": "FAIL"}, {"valid": True}
            ),
            "SHADOW_FAIL_SEALED",
        )

    def test_alpha007_nonterminal_warning_does_not_close_or_retune_cohort(self) -> None:
        report = {
            "status": "ACCUMULATING",
            "complete_days": 7,
            "checks": [
                {"name": "locked_net_60s_markout", "passed": False, "actual": -0.001},
                {"name": "hour_block_bootstrap_95pct_lower_bound", "passed": False, "actual": -0.002},
                {"name": "plus_100ms_latency_net_60s_markout", "passed": False, "actual": -0.0015},
                {"name": "single_hour_profit_concentration", "passed": False, "actual": 0.21},
            ],
        }
        warning = alpha007_nonterminal_performance_warning(report)
        self.assertEqual(warning["status"], "WARNING")
        self.assertTrue(warning["nonterminal"])
        self.assertIn("Continue the fixed collection unchanged", warning["action"])

        immature = dict(report, complete_days=6)
        self.assertEqual(
            alpha007_nonterminal_performance_warning(immature)["status"],
            "NO_EARLY_WARNING",
        )

    def test_alpha007_terminal_receipt_binds_exact_manifest_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "reports" / "alpha007-accumulation.json"
            registry_path = root / "research" / "registry.json"
            raw_root = root / "research" / "microstructure"
            archive_path = root / "research" / "evidence" / "alpha007" / "sealed.json.gz"
            receipt_path = root / "reports" / "alpha007-terminal-receipt.json"
            analyzer_path = root / "scripts" / "analyze_microstructure.py"
            synchronizer_path = root / "scripts" / "sync_alpha007_registry.py"
            partial_report_path = root / "reports" / "alpha007-partial-fill-stress.json"
            partial_policy_path = root / "research" / "alpha007-partial-fill-stress-policy.json"
            partial_evaluator_path = root / "quant" / "research" / "partial_fill_stress.py"
            partial_builder_path = root / "scripts" / "alpha007_partial_fill_stress.py"
            frozen_evaluator_path = root / "quant" / "research" / "microstructure.py"
            manifest_path = raw_root / "run-one" / "manifest.json"
            for path in (
                report_path,
                registry_path,
                archive_path,
                receipt_path,
                analyzer_path,
                synchronizer_path,
                partial_report_path,
                partial_policy_path,
                partial_evaluator_path,
                partial_builder_path,
                frozen_evaluator_path,
                manifest_path,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
            report = {
                "candidate": "ALPHA_007_IMBALANCE_GUIDED_MAKER",
                "status": "PASS",
                "complete_days": 17,
                "complete_day_protocol_audit": {"status": "PASS"},
            }
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
            analyzer_path.write_text("# analyzer\n", encoding="utf-8")
            synchronizer_path.write_text("# synchronizer\n", encoding="utf-8")
            partial_policy_path.write_text('{"policy":"partial"}\n', encoding="utf-8")
            partial_evaluator_path.write_text("# partial evaluator\n", encoding="utf-8")
            partial_builder_path.write_text("# partial builder\n", encoding="utf-8")
            frozen_evaluator_path.write_text("# frozen evaluator\n", encoding="utf-8")
            manifest_path.write_text('{"run_id":"run-one"}\n', encoding="utf-8")
            with gzip.open(archive_path, "wb") as compressed:
                compressed.write(report_path.read_bytes())
            report_hash = sha256(report_path.read_bytes()).hexdigest()
            partial = {
                "status": "PASS",
                "promotion_eligible": False,
                "primary_report_sha256": report_hash,
                "policy_sha256": sha256(partial_policy_path.read_bytes()).hexdigest(),
                "registry_binding": {"before_locked_oos": True},
                "evaluator_sha256": {
                    "partial_fill_stress": sha256(partial_evaluator_path.read_bytes()).hexdigest(),
                    "report_builder": sha256(partial_builder_path.read_bytes()).hexdigest(),
                    "frozen_primary_evaluator": sha256(frozen_evaluator_path.read_bytes()).hexdigest(),
                },
            }
            partial_report_path.write_text(json.dumps(partial) + "\n", encoding="utf-8")
            registry = {
                "events": [
                    {
                        "event_id": 7,
                        "event_type": "result_update",
                        "payload": {
                            "result": {
                                "status": "REVIEW",
                                "shadow_status": "PASS",
                                "promotion_eligible": False,
                                "source_sha256": report_hash,
                                "accumulation": {"candidate": report["candidate"]},
                            }
                        },
                    }
                ]
            }
            registry_path.write_text(json.dumps(registry) + "\n", encoding="utf-8")
            receipt = {
                "schema_version": 1,
                "status": "PASS",
                "candidate": report["candidate"],
                "report_status": "PASS",
                "report_sha256": report_hash,
                "analyzer_sha256": sha256(analyzer_path.read_bytes()).hexdigest(),
                "synchronizer_sha256": sha256(synchronizer_path.read_bytes()).hexdigest(),
                "source_replay": "PASS",
                "verified_run_manifests": [
                    {
                        "path": str(manifest_path.relative_to(root)),
                        "sha256": sha256(manifest_path.read_bytes()).hexdigest(),
                    }
                ],
                "evidence_archive": str(archive_path.relative_to(root)),
                "registry_event_id": 7,
                "promotion_eligible": False,
                "partial_fill_veto_status": "PASS",
                "partial_fill_report_sha256": sha256(
                    partial_report_path.read_bytes()
                ).hexdigest(),
                "partial_fill_policy_sha256": partial["policy_sha256"],
            }
            receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            arguments = {
                "receipt_path": receipt_path,
                "report_path": report_path,
                "registry_path": registry_path,
                "raw_root": raw_root,
                "project_root": root,
                "analyzer_path": analyzer_path,
                "synchronizer_path": synchronizer_path,
            }
            self.assertTrue(alpha007_receipt_health(**arguments)["valid"])

            extra_manifest = raw_root / "run-two" / "manifest.json"
            extra_manifest.parent.mkdir(parents=True)
            extra_manifest.write_text('{"run_id":"run-two"}\n', encoding="utf-8")
            health = alpha007_receipt_health(**arguments)
            self.assertFalse(health["valid"])
            self.assertIn("run_manifest_inventory_not_exact", health["failures"])

            malformed = dict(receipt)
            malformed["registry_event_id"] = {"not": "an integer"}
            receipt_path.write_text(json.dumps(malformed) + "\n", encoding="utf-8")
            malformed_health = alpha007_receipt_health(**arguments)
            self.assertFalse(malformed_health["valid"])
            self.assertIn("registry_event_id_invalid", malformed_health["failures"])

    def test_alpha007_terminal_receipt_is_write_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            original = {"status": "PASS", "report_sha256": "a" * 64}
            self.assertTrue(write_alpha007_receipt_once(path, original))
            self.assertFalse(write_alpha007_receipt_once(path, dict(original)))
            with self.assertRaisesRegex(ValueError, "already sealed"):
                write_alpha007_receipt_once(
                    path, {"status": "PASS", "report_sha256": "b" * 64}
                )
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_scoreboard_applies_latest_append_only_result_event(self) -> None:
        registry = {
            "experiments": [
                {
                    "experiment_id": 1,
                    "strategy": "forward",
                    "result": {"gate": {"status": "ACCUMULATING"}, "development": {}},
                }
            ],
            "events": [
                {
                    "event_id": 1,
                    "experiment_id": 1,
                    "event_type": "result_update",
                    "payload": {
                        "result": {
                            "gate": {"status": "FAIL"},
                            "development": {"organic": {"trades": 100, "net_profit": -1, "net_return": -0.01}},
                        }
                    },
                }
            ],
        }
        scoreboard = build_scoreboard(registry)
        self.assertEqual(scoreboard["events"], 1)
        self.assertEqual(scoreboard["experiments_detail"][0]["status"], "FAIL")

    def test_edge_budget_and_scoreboard_classify_cost_failure(self) -> None:
        self.assertFalse(EdgeBudget(7, 10, 0, 2, 0).viable)
        registry = {
            "experiments": [
                {
                    "experiment_id": 1,
                    "strategy": "A",
                    "result": {
                        "gate": {"status": "FAIL", "policy": {"min_oos_trades": 30}},
                        "development": {"organic": {"net_return": -0.01}},
                        "validation": {
                            "organic": {"trades": 40, "net_return": -0.01, "net_profit": -10, "profit_factor": 0.9},
                            "estimated_fee_cost": 15,
                            "funding_net": 0,
                            "pnl_components": {"price_pnl_estimate": 5, "trading_fees": -15, "funding_pnl": 0, "net_pnl": -10},
                        },
                    },
                }
            ]
        }
        scoreboard = build_scoreboard(registry)
        self.assertEqual(scoreboard["failure_reasons"], {"COST_DOMINATED": 1})

    def test_scoreboard_classifies_concentrated_pnl(self) -> None:
        registry = {
            "experiments": [
                {
                    "experiment_id": 1,
                    "strategy": "A",
                    "result": {
                        "gate": {
                            "status": "FAIL",
                            "policy": {"min_oos_trades": 30},
                            "checks": [{"name": "single_trade_profit_concentration", "passed": False}],
                        },
                        "development": {"organic": {"net_return": 0.01}},
                        "validation": {"organic": {"trades": 40, "net_return": 0.01, "net_profit": 10, "profit_factor": 1.3}},
                    },
                }
            ]
        }
        scoreboard = build_scoreboard(registry)
        self.assertEqual(scoreboard["failure_reasons"], {"CONCENTRATED_PNL": 1})

    def test_scoreboard_uses_development_when_locked_validation_was_not_opened(self) -> None:
        registry = {
            "experiments": [
                {
                    "experiment_id": 9,
                    "strategy": "carry",
                    "result": {
                        "gate": {
                            "status": "FAIL",
                            "checks": [{"name": "funding_exceeds_fees_and_slippage", "passed": False}],
                        },
                        "development": {
                            "organic": {"trades": 212, "net_return": 0.63, "net_profit": 635, "profit_factor": 1.26},
                            "pnl_components": {
                                "price_pnl_estimate": 637,
                                "trading_fees": -58,
                                "funding_pnl": 80,
                                "modeled_slippage": -23,
                                "net_pnl": 635,
                            },
                        },
                    },
                }
            ]
        }
        scoreboard = build_scoreboard(registry)
        row = scoreboard["experiments_detail"][0]
        self.assertEqual(row["evaluation_stage"], "development")
        self.assertEqual(row["trade_count"], 212)
        self.assertEqual(row["primary_failure"], "CARRY_NOT_SELF_FINANCING")
        self.assertIsNone(scoreboard["best_oos"])
        self.assertEqual(scoreboard["best_development_diagnostic"]["strategy"], "carry")

    def test_scoreboard_preserves_explicit_preregistered_failure_attribution(self) -> None:
        registry = {
            "experiments": [
                {
                    "experiment_id": 12,
                    "strategy": "carry",
                    "result": {
                        "status": "FAIL",
                        "primary_failure": "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION",
                        "failure_reasons": [
                            "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION",
                            "COST_STRESS_FAILURE",
                        ],
                        "gate": {"status": "FAIL"},
                        "development": {
                            "organic": {
                                "trades": 2,
                                "net_return": 0.001,
                                "net_profit": 1.0,
                                "profit_factor": 2.0,
                            }
                        },
                    },
                }
            ]
        }
        row = build_scoreboard(registry)["experiments_detail"][0]
        self.assertEqual(row["primary_failure"], "INSUFFICIENT_OPPORTUNITY_AFTER_FRICTION")
        self.assertIn("COST_STRESS_FAILURE", row["failure_reasons"])

    def test_gate_cannot_pass_with_too_few_oos_trades(self) -> None:
        outcome = evaluate_candidate(
            development=sample_metrics(),
            validation=sample_metrics(trades=7),
            cost_stress=sample_metrics(),
            lookahead_passed=True,
            recursive_passed=True,
            policy=GatePolicy(min_oos_trades=20),
        )
        self.assertEqual(outcome["status"], "REVIEW")

    def test_artifact_reference_binds_path_to_content_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backtest.zip"
            path.write_bytes(b"sealed backtest bytes")
            self.assertEqual(
                artifact_reference(path),
                {
                    "path": str(path),
                    "sha256": sha256(b"sealed backtest bytes").hexdigest(),
                },
            )
        self.assertIsNone(artifact_reference(None))

    def test_monthly_target_requires_return_risk_cost_and_sample(self) -> None:
        base = {
            "monthly": {
                "complete_months": 12,
                "geometric_monthly_return": 0.11,
                "positive_month_ratio": 0.75,
                "worst_month": -0.08,
            },
            "organic": {"trades": 120, "profit_factor": 1.3},
            "max_drawdown": 0.15,
            "equity_basis": "mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "maximum_leverage": 2.0,
                "maximum_leverage_adjusted_mae": 0.20,
                "liquidation_exits": 0,
            },
        }
        stress = {"organic": {"net_return": 0.01}}
        risk = passing_bootstrap_risk()
        self.assertEqual(
            evaluate_monthly_target(
                base, double_cost=stress, risk=risk, evidence=passing_forward_evidence()
            )["status"],
            "PASS",
        )
        base["monthly"]["geometric_monthly_return"] = 0.09
        near_target = evaluate_monthly_target(
            base, double_cost=stress, risk=risk, evidence=passing_forward_evidence()
        )
        self.assertEqual(near_target["status"], "FAIL")

    def test_monthly_target_rejects_historical_or_weak_bootstrap_evidence(self) -> None:
        base = {
            "monthly": {
                "complete_months": 12,
                "geometric_monthly_return": 0.11,
                "positive_month_ratio": 0.75,
                "worst_month": -0.08,
            },
            "organic": {"trades": 120, "profit_factor": 1.3},
            "max_drawdown": 0.15,
            "equity_basis": "mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "maximum_leverage": 2.0,
                "maximum_leverage_adjusted_mae": 0.20,
                "liquidation_exits": 0,
            },
        }
        stress = {"organic": {"net_return": 0.01}}
        historical = evaluate_monthly_target(
            base,
            double_cost=stress,
            risk=passing_bootstrap_risk(),
            evidence={
                "sample_role": "historical_development_backtest",
                "pre_registered": False,
                "parameter_changes_after_start": None,
                "registered_at_utc": None,
                "sample_started_at_utc": "2025-01-02T00:00:00Z",
            },
        )
        self.assertEqual(historical["status"], "REVIEW")
        historical_failures = {item["name"] for item in historical["checks"] if not item["passed"]}
        self.assertIn("locked_forward_sample", historical_failures)
        self.assertIn("sample_preregistered", historical_failures)

        late_registration = passing_forward_evidence()
        late_registration["registered_at_utc"] = "2025-01-03T00:00:00Z"
        mistimed = evaluate_monthly_target(
            base,
            double_cost=stress,
            risk=passing_bootstrap_risk(),
            evidence=late_registration,
        )
        mistimed_failures = {item["name"] for item in mistimed["checks"] if not item["passed"]}
        self.assertEqual(mistimed["status"], "REVIEW")
        self.assertIn("preregistration_precedes_sample", mistimed_failures)

        weak_risk = passing_bootstrap_risk()
        weak_risk["probability_terminal_loss"] = 0.11
        weak_risk["probability_reaching_annual_target"] = 0.49
        weak = evaluate_monthly_target(
            base,
            double_cost=stress,
            risk=weak_risk,
            evidence=passing_forward_evidence(),
            policy=MonthlyTargetPolicy(),
        )
        self.assertEqual(weak["status"], "FAIL")
        weak_failures = {item["name"] for item in weak["checks"] if not item["passed"]}
        self.assertIn("bootstrap_probability_terminal_loss", weak_failures)
        self.assertIn("bootstrap_probability_reaching_annual_target", weak_failures)

    def test_monthly_target_requires_complete_positive_slippage_stress_when_enabled(self) -> None:
        base = {
            "monthly": {
                "complete_months": 12,
                "geometric_monthly_return": 0.11,
                "positive_month_ratio": 0.75,
                "worst_month": -0.08,
            },
            "organic": {"trades": 120, "profit_factor": 1.3},
            "max_drawdown": 0.15,
            "equity_basis": "mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "slippage_coverage_ratio": 1.0,
                "maximum_leverage": 2.0,
                "maximum_leverage_adjusted_mae": 0.20,
                "liquidation_exits": 0,
            },
        }
        risk = passing_bootstrap_risk()
        policy = MonthlyTargetPolicy(require_slippage_stress_positive=True)
        positive = evaluate_monthly_target(
            base,
            double_cost={"organic": {"net_return": 0.01}},
            slippage_stress={"organic": {"net_return": 0.005}},
            risk=risk,
            evidence=passing_forward_evidence(),
            policy=policy,
        )
        self.assertEqual(positive["status"], "PASS")
        negative = evaluate_monthly_target(
            base,
            double_cost={"organic": {"net_return": 0.01}},
            slippage_stress={"organic": {"net_return": -0.001}},
            risk=risk,
            evidence=passing_forward_evidence(),
            policy=policy,
        )
        self.assertEqual(negative["status"], "FAIL")

    def test_monthly_target_requires_positive_adverse_funding_stress_when_enabled(self) -> None:
        base = {
            "monthly": {
                "complete_months": 12,
                "geometric_monthly_return": 0.11,
                "positive_month_ratio": 0.75,
                "worst_month": -0.08,
            },
            "organic": {"trades": 120, "profit_factor": 1.3},
            "max_drawdown": 0.15,
            "equity_basis": "mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "funding_coverage_ratio": 1.0,
                "maximum_leverage": 2.0,
                "maximum_leverage_adjusted_mae": 0.20,
                "liquidation_exits": 0,
            },
        }
        risk = passing_bootstrap_risk()
        policy = MonthlyTargetPolicy(require_funding_stress_positive=True)
        positive = evaluate_monthly_target(
            base,
            double_cost={"organic": {"net_return": 0.01}},
            funding_stress={"organic": {"net_return": 0.002}},
            risk=risk,
            evidence=passing_forward_evidence(),
            policy=policy,
        )
        self.assertEqual(positive["status"], "PASS")
        negative = evaluate_monthly_target(
            base,
            double_cost={"organic": {"net_return": 0.01}},
            funding_stress={"organic": {"net_return": -0.001}},
            risk=risk,
            evidence=passing_forward_evidence(),
            policy=policy,
        )
        self.assertEqual(negative["status"], "FAIL")

    def test_monthly_target_stays_review_until_sample_exists(self) -> None:
        base = {
            "monthly": {"complete_months": 2, "geometric_monthly_return": 0.2, "positive_month_ratio": 1.0, "worst_month": 0.1},
            "organic": {"trades": 10, "profit_factor": 2.0},
            "max_drawdown": 0.01,
        }
        self.assertEqual(evaluate_monthly_target(base, double_cost={"organic": {"net_return": 0.1}})["status"], "REVIEW")

    def test_monthly_target_does_not_treat_missing_risk_values_as_zero(self) -> None:
        result = evaluate_monthly_target(
            {
                "monthly": {
                    "complete_months": 0,
                    "geometric_monthly_return": None,
                    "positive_month_ratio": None,
                    "worst_month": None,
                },
                "organic": {"trades": 0, "profit_factor": None},
                "max_drawdown": None,
                "equity_basis": "mark_to_market",
                "trade_risk": {},
            },
            double_cost=None,
            risk=None,
        )
        checks = {item["name"]: item for item in result["checks"]}
        self.assertFalse(checks["worst_month_loss"]["passed"])
        self.assertFalse(checks["max_drawdown"]["passed"])

    def test_block_bootstrap_risk_is_deterministic_and_detects_safe_path(self) -> None:
        returns = [0.001] * 60
        first = block_bootstrap_risk(returns, simulations=500)
        second = block_bootstrap_risk(returns, simulations=500)
        self.assertEqual(first, second)
        self.assertEqual(first["probability_of_ruin"], 0.0)
        self.assertEqual(first["maximum_drawdown_p95"], 0.0)

    def test_monthly_target_cannot_pass_without_mark_to_market_equity(self) -> None:
        base = {
            "monthly": {"complete_months": 12, "geometric_monthly_return": 0.11, "positive_month_ratio": 0.8, "worst_month": -0.05},
            "organic": {"trades": 120, "profit_factor": 1.4},
            "max_drawdown": 0.1,
            "equity_basis": "closed_trade_daily_profit_not_mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "maximum_leverage": 1.0,
                "maximum_leverage_adjusted_mae": 0.10,
                "liquidation_exits": 0,
            },
        }
        stress = {"organic": {"net_return": 0.01}}
        risk = passing_bootstrap_risk(maximum_drawdown_p95=0.1)
        self.assertEqual(
            evaluate_monthly_target(
                base, double_cost=stress, risk=risk, evidence=passing_forward_evidence()
            )["status"],
            "REVIEW",
        )

    def test_mark_to_market_curve_requires_unbroken_utc_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "equity.csv"
            start = datetime(2025, 1, 1, tzinfo=timezone.utc)
            rows = ["timestamp,equity,equity_type"]
            for offset in range(60):
                observed = start + timedelta(days=offset)
                rows.append(f"{observed.isoformat()},{1000 * (1.001 ** offset)},mark_to_market")
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            result = load_mark_to_market_equity(
                path,
                expected_start="2025-01-01 00:00:00",
                expected_end="2025-03-01 00:00:00",
            )
            self.assertEqual(result["equity_basis"], "mark_to_market")
            self.assertEqual(result["monthly"]["complete_months"], 2)
            self.assertEqual(result["max_drawdown"], 0.0)

            path.write_text("\n".join(rows[:20] + rows[21:]) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "every consecutive UTC day"):
                load_mark_to_market_equity(path)

    def test_monthly_target_rejects_liquidation_or_excess_leverage(self) -> None:
        base = {
            "monthly": {"complete_months": 12, "geometric_monthly_return": 0.11, "positive_month_ratio": 0.8, "worst_month": -0.05},
            "organic": {"trades": 120, "profit_factor": 1.4},
            "max_drawdown": 0.1,
            "equity_basis": "mark_to_market",
            "trade_risk": {
                "excursion_coverage_ratio": 1.0,
                "maximum_leverage": 5.0,
                "maximum_leverage_adjusted_mae": 0.2,
                "liquidation_exits": 1,
            },
        }
        risk = passing_bootstrap_risk(maximum_drawdown_p95=0.1)
        result = evaluate_monthly_target(
            base,
            double_cost={"organic": {"net_return": 0.1}},
            risk=risk,
            evidence=passing_forward_evidence(),
        )
        failed = {item["name"] for item in result["checks"] if not item["passed"]}
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("maximum_leverage", failed)
        self.assertIn("liquidation_exits", failed)

    def test_monthly_distribution_uses_backtest_coverage_for_quiet_tail(self) -> None:
        result = _monthly_distribution(
            [["2025-01-15", 10.0], ["2025-02-03", -5.0]],
            1000.0,
            coverage_start=datetime(2025, 1, 10).date(),
            coverage_end=datetime(2025, 4, 1).date(),
        )
        self.assertEqual(result["complete_months"], 2)
        self.assertEqual([item["month"] for item in result["series"]], ["2025-01", "2025-02", "2025-03"])
        self.assertFalse(result["series"][0]["complete"])
        self.assertTrue(result["series"][1]["complete"])
        self.assertTrue(result["series"][2]["complete"])
        self.assertEqual(result["series"][2]["return"], 0.0)

    def test_short_gate_requires_bear_expectancy_and_diversified_winners(self) -> None:
        validation = sample_metrics(trades=30)
        validation["short_metrics"] = {"expectancy": 1.0, "largest_winner_profit_concentration": 0.7}
        outcome = evaluate_candidate(
            development=sample_metrics(),
            validation=validation,
            cost_stress=sample_metrics(),
            lookahead_passed=True,
            recursive_passed=True,
            role="short",
        )
        self.assertEqual(outcome["status"], "FAIL")
        self.assertFalse(next(item for item in outcome["checks"] if item["name"] == "single_trade_profit_concentration")["passed"])

    def test_breakout_gate_requires_expectancy_and_slippage_resilience(self) -> None:
        validation = sample_metrics(trades=30)
        validation["breakout_metrics"] = {"expectancy": 1.0, "largest_winner_profit_concentration": 0.3}
        outcome = evaluate_candidate(
            development=sample_metrics(),
            validation=validation,
            cost_stress=sample_metrics(),
            slippage_stress=sample_metrics(net_return=-0.001),
            lookahead_passed=True,
            recursive_passed=True,
            role="breakout",
        )
        self.assertEqual(outcome["status"], "FAIL")
        self.assertFalse(next(item for item in outcome["checks"] if item["name"] == "slippage_stress_net_positive")["passed"])

    def test_result_loader_excludes_force_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.zip"
            payload = {
                "strategy": {
                    "Demo": {
                        "starting_balance": 1000,
                        "backtest_days": 10,
                        "max_drawdown_account": 0.02,
                        "daily_profit": [["2025-01-01", 10.0], ["2025-01-02", -5.0]],
                        "trades": [
                            {"profit_abs": 10, "exit_reason": "signal", "fee_open": 0.001, "fee_close": 0.001, "funding_fees": -1, "is_short": True, "open_rate": 100, "min_rate": 90, "max_rate": 110, "trade_duration": 60, "orders": [{"cost": 100, "ft_is_entry": True}, {"cost": 110, "ft_is_entry": False}]},
                            {"profit_abs": 50, "exit_reason": "force_exit", "orders": []},
                        ],
                    }
                }
            }
            with ZipFile(result_path, "w") as archive:
                archive.writestr("result.json", json.dumps(payload))
            result = load_freqtrade_result(result_path, "Demo")
            self.assertEqual(result["organic"]["trades"], 1)
            self.assertAlmostEqual(result["organic"]["net_return"], 0.01)
            self.assertAlmostEqual(result["estimated_fee_cost"], 0.21)
            self.assertAlmostEqual(result["daily"]["mean_daily_return"], 0.002524752475247525)
            self.assertEqual(result["short_metrics"]["trades"], 1)
            self.assertAlmostEqual(result["short_metrics"]["mae_max"], 0.1)
            self.assertAlmostEqual(result["pnl_components"]["net_pnl"], 10)
            self.assertIn("breakout_success_rate", result["breakout_metrics"])

    def test_result_loader_reconciles_daily_profit_to_organic_cost_adjusted_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.zip"
            payload = {
                "strategy": {
                    "Demo": {
                        "starting_balance": 1000,
                        "backtest_days": 31,
                        "backtest_start": "2025-01-01 00:00:00+00:00",
                        "backtest_end": "2025-02-01 00:00:00+00:00",
                        "daily_profit": [["2025-01-02", 60.0]],
                        "trades": [
                            {
                                "profit_abs": 10.0,
                                "close_date": "2025-01-02 12:00:00+00:00",
                                "exit_reason": "signal",
                                "orders": [
                                    {"cost": 100.0, "ft_is_entry": True},
                                    {"cost": 110.0, "ft_is_entry": False},
                                ],
                            },
                            {
                                "profit_abs": 50.0,
                                "close_date": "2025-01-02 12:01:00+00:00",
                                "exit_reason": "force_exit",
                                "orders": [],
                            },
                        ],
                    }
                }
            }
            with ZipFile(result_path, "w") as archive:
                archive.writestr("result.json", json.dumps(payload))
            result = load_freqtrade_result(result_path, "Demo", modeled_slippage_bps=10.0)
            self.assertEqual(result["daily_accounting"]["status"], "RECONCILED")
            self.assertAlmostEqual(result["organic"]["net_profit"], 9.79)
            self.assertAlmostEqual(result["daily"]["series"][0]["profit"], 9.79)
            self.assertAlmostEqual(result["monthly"]["series"][0]["return"], 0.00979)

    def test_historical_report_requires_exact_sealed_universe_binding(self) -> None:
        expected = "a" * 64
        self.assertTrue(report_is_bound_to_universe({"universe_sha256": expected}, expected))
        self.assertFalse(report_is_bound_to_universe({"universe_sha256": "b" * 64}, expected))
        self.assertFalse(report_is_bound_to_universe({"universe_sha256": expected}, None))
        self.assertFalse(report_is_bound_to_universe({}, expected))

    def test_portfolio_activation_requires_independent_passes_in_distinct_families(self) -> None:
        policy = {
            "candidate": "PORTFOLIO",
            "status": "DEFERRED_NOT_OPENED",
            "live_order_authorization": "CLOSED",
            "activation_requirements": {
                "minimum_independent_sleeves": 2,
                "require_distinct_families": True,
                "minimum_complete_locked_forward_months": 12,
            },
        }
        registry = {
            "schema_version": 1,
            "experiments": [
                {
                    "experiment_id": 1,
                    "strategy": "FIRST",
                    "trial_accounting": {"family": "trend"},
                    "result": {
                        "gate": {"status": "PASS"},
                        "portfolio_activation_evidence": {
                            "sample_role": "locked_forward",
                            "pre_registered": True,
                            "parameter_changes_after_start": 0,
                            "locked_validation_status": "PASS",
                        },
                    },
                },
                {
                    "experiment_id": 2,
                    "strategy": "SECOND",
                    "trial_accounting": {"family": "flow"},
                    "result": {
                        "gate": {"status": "PASS"},
                        "portfolio_activation_evidence": {
                            "sample_role": "locked_forward",
                            "pre_registered": True,
                            "parameter_changes_after_start": 0,
                            "locked_validation_status": "PASS",
                        },
                    },
                },
            ],
        }
        ready = evaluate_portfolio_activation(policy, registry)
        self.assertEqual(ready["status"], "READY_FOR_PORTFOLIO_PREREGISTRATION_ONLY")
        self.assertTrue(ready["preregistration_authorized"])
        self.assertFalse(ready["portfolio_return_data_authorized"])
        self.assertEqual(ready["live_order_authorization"], "CLOSED")

        registry["experiments"][1]["trial_accounting"]["family"] = "trend"
        same_family = evaluate_portfolio_activation(policy, registry)
        self.assertEqual(same_family["status"], "WAITING_INDEPENDENT_SLEEVES")

        registry["experiments"][1]["trial_accounting"]["family"] = "flow"
        registry["experiments"][1]["trial_accounting"]["independent_alpha"] = False
        related_trial = evaluate_portfolio_activation(policy, registry)
        self.assertEqual(related_trial["status"], "WAITING_INDEPENDENT_SLEEVES")

        registry["experiments"][1]["trial_accounting"]["independent_alpha"] = None
        registry["experiments"][1]["trial_accounting"]["family"] = "flow"
        registry["experiments"][1]["result"]["portfolio_activation_evidence"]["sample_role"] = "historical_diagnostic"
        historical = evaluate_portfolio_activation(policy, registry)
        self.assertEqual(historical["status"], "WAITING_INDEPENDENT_SLEEVES")

    def test_historical_admission_caps_survivor_diagnostics(self) -> None:
        policy = {
            "schema_version": 1,
            "status": "ACTIVE",
            "live_order_authorization": "CLOSED",
            "feature_availability_contract": {
                "decision_feature_rule": "A historical decision feature requires both an event timestamp and evidence that it was available no later than the decision timestamp.",
                "historical_open_interest": {
                    "availability_status": "UNVERIFIED_PUBLICATION_TIME",
                    "allowed_as_historical_decision_feature": False,
                },
                "forward_open_interest": {
                    "allowed_as_forward_decision_feature": True,
                    "required_evidence": [
                        "source endpoint",
                        "local UTC receive timestamp",
                        "append-only raw capture",
                        "preregistered decision delay",
                    ],
                },
            },
            "lanes": [
                {
                    "id": "ARCHIVAL_DIAGNOSTIC",
                    "promotion_capability": "DIAGNOSTIC_ONLY",
                    "publisher_checksum_required": True,
                    "costs_preregistered": True,
                },
                {
                    "id": "HISTORICAL_POINT_IN_TIME_REPLICATION",
                    "promotion_capability": "RESEARCH_ONLY_UNTIL_SEPARATE_FORWARD_CONFIRMATION",
                    "membership_source_required": True,
                    "current_symbol_fallback": "FORBIDDEN",
                    "availability": "BLOCKED_BY_MISSING_HISTORICAL_MEMBERSHIP_SOURCE",
                },
                {
                    "id": "FORWARD_CONFIRMATION",
                    "minimum_consecutive_days": 180,
                    "promotion_capability": "REVIEW_ONLY",
                },
            ],
        }
        result = evaluate_historical_admission(policy)
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["historical_point_in_time_available_now"])
        self.assertEqual(result["live_order_authorization"], "CLOSED")

        policy["feature_availability_contract"]["historical_open_interest"][
            "allowed_as_historical_decision_feature"
        ] = True
        unsafe_feature = evaluate_historical_admission(policy)
        self.assertEqual(unsafe_feature["status"], "FAIL")
        self.assertFalse(
            next(
                item
                for item in unsafe_feature["checks"]
                if item["name"] == "historical_open_interest_blocked_without_publication_time"
            )["passed"]
        )
        policy["feature_availability_contract"]["historical_open_interest"][
            "allowed_as_historical_decision_feature"
        ] = False

        policy["lanes"][0]["promotion_capability"] = "LIVE_ELIGIBLE"
        unsafe = evaluate_historical_admission(policy)
        self.assertEqual(unsafe["status"], "FAIL")
        self.assertFalse(
            next(item for item in unsafe["checks"] if item["name"] == "archival_is_non_promotional")["passed"]
        )

    def test_historical_admission_requires_all_evidence_lanes(self) -> None:
        with self.assertRaises(HistoricalAdmissionError):
            evaluate_historical_admission(
                {
                    "schema_version": 1,
                    "status": "ACTIVE",
                    "live_order_authorization": "CLOSED",
                    "lanes": [],
                }
            )

    def test_historical_release_train_keeps_later_windows_withheld(self) -> None:
        policy = {
            "schema_version": 1,
            "status": "PREREGISTERED",
            "promotion_capability": "DIAGNOSTIC_ONLY",
            "live_order_authorization": "CLOSED",
            "partitions": [
                {"id": "BASE", "start": "2024-01-01", "end_exclusive": "2024-08-01", "state": "SEALED_BASELINE", "universe_sha256": "a" * 64},
                {"id": "WITHHELD", "start": "2024-08-01", "end_exclusive": "2025-01-01", "state": "WITHHELD_UNTOUCHED"},
            ],
            "release_rules": {
                "candidate_preregistration": "REQUIRED_BEFORE_WITHHELD_PARTITION_ACCESS",
                "evaluation_scope": "EXACTLY_ONE_RELEASED_PARTITION",
                "post_release_retuning": "FORBIDDEN",
            },
        }
        result = evaluate_historical_release_train(
            policy, baseline_universe_sha256="a" * 64, baseline_audit_status="PASS"
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["next_withheld_partition"]["id"], "WITHHELD")
        self.assertEqual(result["promotion_capability"], "DIAGNOSTIC_ONLY")
        policy["partitions"][1]["state"] = "RELEASED"
        self.assertEqual(
            evaluate_historical_release_train(
                policy, baseline_universe_sha256="a" * 64, baseline_audit_status="PASS"
            )["status"],
            "FAIL",
        )
        policy["partitions"].append({"id": "OVERLAP", "start": "2024-12-01", "end_exclusive": "2025-02-01", "state": "WITHHELD_UNTOUCHED"})
        with self.assertRaises(HistoricalReleaseTrainError):
            evaluate_historical_release_train(
                policy, baseline_universe_sha256="a" * 64, baseline_audit_status="PASS"
            )

    def test_historical_symbol_catalog_keeps_delisted_archive_prefixes_without_claiming_membership(self) -> None:
        page_one = parse_s3_symbol_page(
            b"""<?xml version='1.0' encoding='UTF-8'?>
            <ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>
              <Prefix>data/futures/um/daily/klines/</Prefix>
              <IsTruncated>true</IsTruncated>
              <NextContinuationToken>next-page</NextContinuationToken>
              <CommonPrefixes><Prefix>data/futures/um/daily/klines/BTCUSDT/</Prefix></CommonPrefixes>
              <CommonPrefixes><Prefix>data/futures/um/daily/klines/OLDUSDT/</Prefix></CommonPrefixes>
            </ListBucketResult>"""
        )
        page_two = parse_s3_symbol_page(
            b"""<?xml version='1.0' encoding='UTF-8'?>
            <ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>
              <Prefix>data/futures/um/daily/klines/</Prefix>
              <IsTruncated>false</IsTruncated>
              <CommonPrefixes><Prefix>data/futures/um/daily/klines/ETHUSDT/</Prefix></CommonPrefixes>
            </ListBucketResult>"""
        )
        catalogue = build_historical_symbol_catalog([page_one, page_two])
        self.assertEqual(catalogue["symbol_count"], 3)
        self.assertEqual(catalogue["symbols"], ["BTCUSDT", "ETHUSDT", "OLDUSDT"])
        self.assertEqual(catalogue["status"], "SOURCE_CATALOG_SEALED_NOT_DAY_MEMBERSHIP")
        self.assertEqual(catalogue["live_order_authorization"], "CLOSED")

    def test_historical_symbol_catalog_rejects_incomplete_listing_sequence(self) -> None:
        page = parse_s3_symbol_page(
            b"""<ListBucketResult><Prefix>data/futures/um/daily/klines/</Prefix>
            <IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken>
            <CommonPrefixes><Prefix>data/futures/um/daily/klines/BTCUSDT/</Prefix></CommonPrefixes>
            </ListBucketResult>"""
        )
        with self.assertRaises(HistoricalSymbolCatalogError):
            build_historical_symbol_catalog([page])

    def test_historical_daily_universe_uses_only_current_and_prior_bars(self) -> None:
        bars = [
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 1), 0, 86_399_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 2), 86_400_000, 172_799_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 3), 172_800_000, 259_199_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("NEWUSDT", date(2024, 1, 3), 172_800_000, 259_199_999, 90_000_000, 10, 11, 9, 10, 9_000_000, 100),
        ]
        result = build_historical_daily_universe(
            bars,
            catalog_sha256="a" * 64,
            min_listing_age_days=1,
            min_continuous_days=2,
            min_quote_volume=10_000_000,
        )
        self.assertEqual(result["status"], "HISTORICAL_DAY_MEMBERSHIP_RECONSTRUCTED_DIAGNOSTIC")
        self.assertEqual(result["membership"]["2024-01-01"]["selected"], [])
        self.assertEqual(result["membership"]["2024-01-02"]["selected"], ["BTCUSDT"])
        self.assertEqual(result["membership"]["2024-01-03"]["selected"], ["BTCUSDT"])
        self.assertEqual(result["promotion_capability"], "DIAGNOSTIC_ONLY")

    def test_historical_monthly_archive_rejects_mismatched_checksum_and_partial_day(self) -> None:
        spec = monthly_1d_archive_spec("BTCUSDT", date(2024, 1, 1))
        self.assertEqual(spec["filename"], "BTCUSDT-1d-2024-01.zip")
        unicode_spec = monthly_1d_archive_spec("哈基米USDT", date(2024, 1, 1))
        self.assertIn("%E5%93%88", unicode_spec["archive_url"])
        self.assertNotIn("哈基米", unicode_spec["archive_url"])
        with self.assertRaises(HistoricalUniverseError):
            parse_historical_universe_checksum(b"a" * 64 + b" wrong.zip\n", expected_filename=spec["filename"])
        payload = BytesIO()
        with ZipFile(payload, "w") as archive:
            archive.writestr(
                spec["csv_filename"],
                ",".join((
                    "open_time", "open", "high", "low", "close", "volume", "close_time",
                    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
                )) + "\n1704067200000,1,1,1,1,1,1704153599998,20000000,1,1,1,0\n",
            )
        with self.assertRaises(HistoricalUniverseError):
            validate_monthly_1d_archive(payload.getvalue(), symbol="BTCUSDT", month="2024-01")

    def test_historical_daily_panel_requires_sealed_membership_and_source_bound_archive(self) -> None:
        bars = [
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 1), 0, 86_399_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 2), 86_400_000, 172_799_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
        ]
        artifact = build_historical_daily_universe(
            bars, catalog_sha256="a" * 64, min_listing_age_days=1,
            min_continuous_days=2, min_quote_volume=10_000_000,
        )
        self.assertEqual(verify_historical_daily_universe(artifact)["selected_symbol_days"], 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = monthly_1d_archive_spec("BTCUSDT", date(2024, 1, 1))
            payload = BytesIO()
            with ZipFile(payload, "w") as archive:
                archive.writestr(
                    spec["csv_filename"],
                    ",".join((
                        "open_time", "open", "high", "low", "close", "volume", "close_time",
                        "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
                    )) + "\n"
                    "1704067200000,10,11,9,10,2000000,1704153599999,20000000,100,1,1,0\n"
                    "1704153600000,10,12,9,11,2000000,1704239999999,20000000,100,1,1,0\n",
                )
            path = root / "BTCUSDT" / "2024-01" / spec["filename"]
            path.parent.mkdir(parents=True)
            path.write_bytes(payload.getvalue())
            path.with_suffix(".zip.CHECKSUM").write_text(
                f"{sha256(payload.getvalue()).hexdigest()} {spec['filename']}\n", encoding="utf-8"
            )
            panel = load_historical_daily_price_panel(artifact, archive_root=root)
            self.assertEqual(panel[["decision_day", "symbol"]].to_dict("records"), [{"decision_day": "2024-01-02", "symbol": "BTCUSDT"}])
            self.assertEqual(float(panel.iloc[0]["close"]), 11.0)
            audit = audit_historical_universe(
                artifact,
                {"calendar": {"archive_start": "2024-01-01", "archive_end_exclusive": "2024-01-03"}},
                archive_root=root,
            )
            self.assertEqual(audit["status"], "PASS")
        artifact["membership"]["2024-01-02"]["selected"] = ["ETHUSDT"]
        with self.assertRaises(HistoricalUniverseError):
            verify_historical_daily_universe(artifact)

    def test_historical_scheduler_requires_a_fresh_heartbeat_not_just_a_pid(self) -> None:
        heartbeat = datetime(2024, 1, 1, tzinfo=timezone.utc)
        payload = {
            "state": "WAITING_NEXT_BATCH",
            "heartbeat_at": heartbeat.isoformat(),
            "batch_timeout_seconds": 60,
            "interval_seconds": 30,
        }
        healthy = historical_scheduler_health(
            payload,
            running=True,
            now=heartbeat + timedelta(seconds=179),
        )
        stale = historical_scheduler_health(
            payload,
            running=True,
            now=heartbeat + timedelta(seconds=181),
        )
        self.assertEqual(healthy["health"], "RUNNING")
        self.assertEqual(stale["health"], "STALE")

    def test_historical_cost_policy_forbids_same_close_and_missing_funding(self) -> None:
        policy = {
            "schema_version": 1,
            "status": "PREREGISTERED",
            "live_order_authorization": "CLOSED",
            "execution": {
                "entry_price": "next available UTC daily open",
                "exit_price": "next available UTC daily open",
                "fills_per_round_trip": 2,
                "base_fee_per_fill": 0.0005,
                "stress_fee_per_fill": 0.001,
                "base_slippage_per_fill": 0.0005,
                "stress_slippage_per_fill": 0.001,
            },
            "funding": {
                "source": "publisher CHECKSUM archive",
                "missing_data": "REJECT_EVALUATION_DAY",
                "zero_funding_assumption": "FORBIDDEN",
            },
            "risk": {
                "maximum_gross_leverage": 1.0,
                "liquidation_claim": "FORBIDDEN_FROM_DAILY_OHLC_ONLY",
                "promotion_capability": "DIAGNOSTIC_ONLY",
            },
        }
        self.assertEqual(evaluate_historical_cost_policy(policy)["status"], "PASS")
        policy["funding"]["zero_funding_assumption"] = "ALLOWED"
        self.assertEqual(evaluate_historical_cost_policy(policy)["status"], "FAIL")
        with self.assertRaises(HistoricalCostPolicyError):
            evaluate_historical_cost_policy({})

    def test_historical_daily_execution_charges_both_fills_and_uses_conservative_stop_gap(self) -> None:
        long = simulate_daily_leg(
            side="long", entry_open=100, session_high=102, session_low=85,
            scheduled_exit_open=101, fee_per_fill=0.0005,
            slippage_per_fill=0.001, stop_loss_fraction=0.10,
        )
        self.assertTrue(long["stopped"])
        self.assertEqual(long["exit_reference_price"], 90.0)
        self.assertAlmostEqual(long["fee_return"], 0.001)
        self.assertLess(long["net_return_before_funding"], long["gross_price_return"])
        short = simulate_daily_leg(
            side="short", entry_open=100, session_high=115, session_low=98,
            scheduled_exit_open=99, fee_per_fill=0.0005,
            slippage_per_fill=0.001, stop_loss_fraction=0.10,
        )
        self.assertTrue(short["stopped"])
        self.assertAlmostEqual(short["exit_reference_price"], 110.0)
        funded = attach_verified_funding(long, funding_return=-0.0002, funding_source="checksum-bound fixture")
        self.assertAlmostEqual(funded["net_return"], long["net_return_before_funding"] - 0.0002)
        self.assertAlmostEqual(funded["pnl_components"]["funding_return"], -0.0002)
        with self.assertRaises(HistoricalDailyExecutionError):
            attach_verified_funding(funded, funding_return=0.0, funding_source="duplicate")
        with self.assertRaises(HistoricalDailyExecutionError):
            simulate_daily_leg(
                side="long", entry_open=100, session_high=99, session_low=90,
                scheduled_exit_open=95, fee_per_fill=0.0005,
                slippage_per_fill=0.001, stop_loss_fraction=0.1,
            )

    def test_historical_execution_coverage_requires_a_source_bound_next_open(self) -> None:
        bars = [
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 1), 0, 86_399_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 2), 86_400_000, 172_799_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
        ]
        artifact = build_historical_daily_universe(
            bars, catalog_sha256="a" * 64, min_listing_age_days=1,
            min_continuous_days=2, min_quote_volume=10_000_000,
        )
        policy = {"calendar": {
            "development_start": "2024-01-02", "development_end": "2024-01-02",
            "locked_historical_validation_start": "2024-01-03", "locked_historical_validation_end": "2024-01-03",
        }}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = monthly_1d_archive_spec("BTCUSDT", date(2024, 1, 1))
            payload = BytesIO()
            with ZipFile(payload, "w") as archive:
                archive.writestr(
                    spec["csv_filename"],
                    ",".join((
                        "open_time", "open", "high", "low", "close", "volume", "close_time",
                        "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
                    )) + "\n"
                    "1704067200000,10,11,9,10,2000000,1704153599999,20000000,100,1,1,0\n"
                    "1704153600000,10,12,9,11,2000000,1704239999999,20000000,100,1,1,0\n",
                )
            path = root / "BTCUSDT" / "2024-01" / spec["filename"]
            path.parent.mkdir(parents=True)
            path.write_bytes(payload.getvalue())
            path.with_suffix(".zip.CHECKSUM").write_text(
                f"{sha256(payload.getvalue()).hexdigest()} {spec['filename']}\n", encoding="utf-8"
            )
            result = audit_historical_next_open_coverage(artifact, policy, archive_root=root)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["periods"][0]["missing_next_open_symbol_days"], 1)

    def test_historical_funding_calendar_requires_complete_month_boundaries(self) -> None:
        self.assertEqual(
            historical_funding_month_starts(date(2024, 1, 1), date(2024, 4, 1)),
            [date(2024, 1, 1), date(2024, 2, 1), date(2024, 3, 1)],
        )
        with self.assertRaises(ValueError):
            historical_funding_month_starts(date(2024, 1, 2), date(2024, 2, 1))

    def test_historical_funding_scheduler_requires_fresh_heartbeat(self) -> None:
        heartbeat = datetime(2024, 1, 1, tzinfo=timezone.utc)
        payload = {"state": "ACQUIRING", "heartbeat_at": heartbeat.isoformat(), "batch_timeout_seconds": 60, "interval_seconds": 15}
        self.assertEqual(historical_funding_scheduler_health(payload, running=True, now=heartbeat + timedelta(seconds=164))["health"], "RUNNING")
        self.assertEqual(historical_funding_scheduler_health(payload, running=True, now=heartbeat + timedelta(seconds=166))["health"], "STALE")

    def test_historical_funding_ledger_rejects_unsealed_or_missing_sources(self) -> None:
        manifest = {
            "schema_version": 1, "status": "COMPLETE", "universe_sha256": "a" * 64,
            "records": [], "not_found": [], "promotion_capability": "DIAGNOSTIC_ONLY",
            "live_order_authorization": "CLOSED",
        }
        manifest["manifest_sha256"] = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(HistoricalFundingLedgerError):
                funding_return_for_interval(
                    manifest, archive_root=Path(temporary), symbol="BTCUSDT",
                    entry_time_ms=1704067200000, exit_time_ms=1704153600000, side="long",
                )
        manifest["status"] = "INCOMPLETE"
        with self.assertRaises(HistoricalFundingLedgerError):
            funding_return_for_interval(
                manifest, archive_root=Path("/missing"), symbol="BTCUSDT",
                entry_time_ms=1704067200000, exit_time_ms=1704153600000, side="long",
            )

    def test_historical_funding_coverage_uses_dynamic_selection_dates(self) -> None:
        bars = [
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 1), 0, 86_399_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
            HistoricalDailyBar("BTCUSDT", date(2024, 1, 2), 86_400_000, 172_799_999, 20_000_000, 10, 11, 9, 10, 2_000_000, 100),
        ]
        artifact = build_historical_daily_universe(bars, catalog_sha256="a" * 64, min_listing_age_days=1, min_continuous_days=2, min_quote_volume=10_000_000)
        manifest = {"schema_version": 1, "status": "COMPLETE", "universe_sha256": artifact["universe_sha256"], "records": [{"symbol": "BTCUSDT", "month": "2024-01-01", "sha256": "b" * 64, "events": 1}], "not_found": [], "promotion_capability": "DIAGNOSTIC_ONLY", "live_order_authorization": "CLOSED"}
        manifest["manifest_sha256"] = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        policy = {"calendar": {"development_start": "2024-01-02", "development_end": "2024-01-02", "locked_historical_validation_start": "2024-01-02", "locked_historical_validation_end": "2024-01-02"}}
        self.assertEqual(verify_historical_funding_manifest(manifest)["records"], 1)
        self.assertEqual(audit_historical_funding_coverage(artifact, policy, manifest)["status"], "PASS")

    def test_capital_preservation_gate_uses_organic_returns_and_stays_non_promotional(self) -> None:
        objective = {
            "id": "CAPITAL_PRESERVATION_RESEARCH_OBJECTIVE_V2",
            "status": "ACTIVE_FOR_NEW_HISTORICAL_DIAGNOSTICS",
            "live_order_authorization": "CLOSED",
            "return_objective": {
                "monthly_return_target": "NOT_A_GATE",
                "promotion_requirement": "positive_long_horizon_cost_after_expectancy",
            },
            "capital_preservation_requirements": {
                "minimum_organic_trades": 100,
                "minimum_profit_factor": 1.20,
                "maximum_historical_drawdown": 0.10,
                "maximum_worst_complete_month_loss": 0.05,
                "maximum_planned_leverage": 1.0,
                "maximum_planned_account_risk_per_trade": 0.0025,
                "maximum_simultaneous_positions": 2,
                "modeled_slippage_bps": 5.0,
                "maximum_combined_cost_stress_drawdown": 0.10,
                "liquidation_exits": 0,
                "rule_violations": 0,
                "adverse_position_adds": "FORBIDDEN",
                "double_fee_plus_modeled_slippage_stress_net_return": "> 0",
                "data_integrity": "PASS",
                "lookahead_analysis": "PASS",
                "recursive_analysis": "PASS",
            },
        }
        risk_policy = json.loads(
            (Path(__file__).resolve().parents[1] / "research" / "risk-policy.json").read_text(
                encoding="utf-8"
            )
        )
        result = {
            "organic": {"trades": 50, "net_return": 0.02, "profit_factor": 1.30},
            "starting_balance": 1_000.0,
            "funding_net": 0.0,
            "modeled_slippage_bps": 5.0,
            "cost_drag": {"slippage_drag_pct": 0.0},
            "max_drawdown": 0.04,
            "monthly": {"worst_month": -0.02},
            "trade_risk": {"liquidation_exits": 0},
        }
        contract = {
            "leverage": 1.0,
            "planned_account_risk_per_trade": 0.0025,
            "maximum_simultaneous_positions": 2,
            "adverse_position_adds": False,
        }
        passed = evaluate_capital_preservation(
            result, result, result, result,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "PASS", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 0},
        )
        self.assertEqual(passed["status"], "DIAGNOSTIC_PASS_NOT_PROMOTABLE")
        self.assertFalse(passed["promotion_eligible"])

        funding_subsidized = dict(result)
        funding_subsidized["funding_net"] = 30.0
        funding_failed = evaluate_capital_preservation(
            result, funding_subsidized, result, funding_subsidized,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "PASS", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 0},
        )
        self.assertEqual(funding_failed["status"], "FAIL")
        self.assertIn("development_adverse_funding_net_positive", funding_failed["failures"])

        missing_slippage = dict(result)
        missing_slippage["modeled_slippage_bps"] = 0.0
        slippage_failed = evaluate_capital_preservation(
            result, missing_slippage, result, missing_slippage,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "PASS", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 0},
        )
        self.assertEqual(slippage_failed["status"], "FAIL")
        self.assertIn("development_combined_fee_slippage_model", slippage_failed["failures"])

        stress_drawdown = dict(result)
        stress_drawdown["cost_drag"] = {"slippage_drag_pct": 0.08}
        stress_drawdown["max_drawdown"] = 0.04
        drawdown_failed = evaluate_capital_preservation(
            result, stress_drawdown, result, stress_drawdown,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "PASS", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 0},
        )
        self.assertEqual(drawdown_failed["status"], "FAIL")
        self.assertIn("development_combined_cost_stress_drawdown", drawdown_failed["failures"])

        integrity_failed = evaluate_capital_preservation(
            result, result, result, result,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "FAIL", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 1},
        )
        self.assertEqual(integrity_failed["status"], "FAIL")
        self.assertIn("data_integrity", integrity_failed["failures"])
        self.assertIn("rule_violations", integrity_failed["failures"])

        failed_result = dict(result)
        failed_result["organic"] = {"trades": 50, "net_return": -0.01, "profit_factor": 0.8}
        failed = evaluate_capital_preservation(
            failed_result, failed_result, result, result,
            objective=objective,
            risk_policy=risk_policy,
            execution_contract=contract,
            integrity={"data_integrity": "PASS", "lookahead": "PASS", "recursive": "PASS", "rule_violations": 0},
        )
        self.assertEqual(failed["status"], "FAIL")
        self.assertIn("development_organic_net_positive", failed["failures"])
