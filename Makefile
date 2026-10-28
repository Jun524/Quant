COMPOSE := docker compose
BOT := $(COMPOSE) run --rm freqtrade
PYTHON ?= $(if $(wildcard .venv-research/bin/python),.venv-research/bin/python,python3)
CONFIG := --config /freqtrade/user_data/config.json
STRATEGY := --strategy TrendBreakoutStrategy
FUTURES_CONFIG := --config /freqtrade/user_data/config.futures.json
FUTURES_STRATEGY := --strategy FuturesBtcRegimeRiskBudgetLong
FUTURES_DAILY_GUARD_CONFIG := --config /freqtrade/user_data/config.futures.json --config /freqtrade/user_data/config.futures.daily-guard.example.json
FUTURES_DAILY_GUARD_STRATEGY := --strategy FuturesBtcRegimeDailyGuardLong
RESEARCH_STRATEGY ?= FuturesBtcRegimeRiskBudgetLong
HYPOTHESIS ?= manual research evaluation
LIVE_OVERLAY ?= user_data/config.futures.live.json
# The futures BTC regime filter needs 200 completed daily BTC candles before
# the first evaluated signal, so retain data from 2021 onward by default.
TIMERANGE ?= 20210101-
BIAS_AUDIT_TIMERANGE ?= 20220101-20260901
HISTORICAL_FUNDING_BATCH_OFFSET ?= 0
HISTORICAL_FUNDING_BATCH_LIMIT ?= 25

.PHONY: pull validate pairs download backtest lookahead recursive dry-run logs stop status \
	futures-validate futures-download futures-backtest futures-lookahead \
	futures-recursive alpha001-bias-audit futures-cost-stress futures-closed-metrics futures-dry-run \
	futures-logs futures-stop futures-dryrun-preflight futures-live-preflight \
	binance-permission-audit \
	binance-account-audit \
	futures-order-feasibility-audit \
	futures-daily-guard-backtest research-test research-evaluate data-validate \
	platform-scoreboard register-phase3 universe-snapshot universe-snapshot-all universe-status universe-scheduler-start universe-scheduler-status universe-scheduler-stop edge-budget aoa-analyze aoa-behavior-model \
	historical-symbol-catalog historical-universe-batch historical-universe-resume historical-universe-finalize historical-universe-audit historical-universe-scheduler-start historical-universe-scheduler-status historical-universe-scheduler-stop historical-release-train-status historical-execution-coverage historical-funding-batch historical-funding-resume historical-funding-finalize historical-funding-scheduler-start historical-funding-scheduler-status historical-funding-scheduler-stop historical-funding-coverage historical-funding-ledger-audit \
	aoa-prototype-validate aoa-distilled-validate aoa-distilled-backtest \
	aoa-distilled-lookahead aoa-distilled-recursive aoa-forward-report maker-execution-report execution-calibration-report \
	shadow-maker-probe monthly-objective microstructure-collect microstructure-report microstructure-campaign \
	shadow-entry-campaign-start shadow-entry-campaign-status shadow-entry-campaign-stop shadow-entry-calibration-scheduled \
	liquidation-telemetry-start liquidation-telemetry-status liquidation-telemetry-coverage liquidation-overlay-development liquidation-overlay-validation-open liquidation-overlay-validation liquidation-telemetry-stop \
	liquidation-overlay-scheduler-start liquidation-overlay-scheduler-status liquidation-overlay-scheduler-stop \
	microstructure-campaign-start microstructure-campaign-status microstructure-campaign-stop microstructure-preflight \
	alpha007-external-cost-stress alpha007-partial-fill-register alpha007-partial-fill-stress alpha007-exchange-filter-audit alpha007-order-path-register alpha007-order-path-status \
	alpha001-forward-start alpha001-forward-status alpha001-forward-stop \
	alpha001-forward-report research-health historical-research-status historical-cost-policy-status selection-bias-audit signal-diagnostics alpha013-signal-panel portfolio-activation-readiness portfolio-risk-audit portfolio-execution-audit portfolio-promotion goal-readiness \
	risk-policy-status \
	alpha005-development alpha005-validation alpha005-cost-stress \
	alpha006-development alpha006-validation alpha006-cost-stress \
	alpha008-development alpha009-development alpha010-register alpha010-metrics-download alpha010-development alpha010-sync alpha010-postmortem alpha011-register alpha011-amendment alpha011-download alpha011-update alpha011-funding-reconcile alpha011-scheduler-start alpha011-scheduler-status alpha011-scheduler-stop alpha011-status alpha011-development alpha011-sync alpha011-validation-open alpha011-validation alpha011-validation-sync alpha012-register alpha012-development alpha012-sync alpha013-register alpha013-download-day alpha013-update alpha013-funding-reconcile alpha013-forward-report alpha013-decision-availability-audit alpha013-causal-veto-sync alpha013-sync alpha013-validation-open alpha013-validation alpha013-validation-sync alpha013-scheduler-start alpha013-scheduler-status alpha013-scheduler-stop alpha013-status alpha014-register alpha014-amendment alpha014-development alpha014-sync alpha015-readiness alpha015-activation-lock alpha015-register alpha015-acquisition-plan alpha015-size-probe alpha015-download-batch alpha015-development alpha015-validation-open alpha015-validation-acquisition-plan alpha015-validation-size-probe alpha015-validation-download-batch alpha015-funding-reconcile alpha015-validation equity-record equity-daily

pull:
	$(COMPOSE) pull

validate:
	$(BOT) show-config $(CONFIG)
	$(BOT) list-strategies $(CONFIG)

pairs:
	$(BOT) list-pairs --exchange binance --quote USDT --print-json

download:
	$(BOT) download-data $(CONFIG) --timeframes 1h --timerange $(TIMERANGE) --prepend

backtest:
	$(BOT) backtesting $(CONFIG) $(STRATEGY) --timerange $(TIMERANGE) --fee 0.001 --enable-protections --cache none --export trades

lookahead:
	$(BOT) lookahead-analysis $(CONFIG) $(STRATEGY) --timerange $(TIMERANGE) --minimum-trade-amount 10 --targeted-trade-amount 30

recursive:
	$(BOT) recursive-analysis $(CONFIG) $(STRATEGY) --timerange $(TIMERANGE) -p BTC/USDT --startup-candle 800 1200 1600 2400

dry-run:
	$(COMPOSE) up -d

logs:
	$(COMPOSE) logs -f --tail=200

status:
	$(COMPOSE) ps

stop:
	$(COMPOSE) down

futures-validate:
	$(BOT) show-config $(FUTURES_CONFIG)
	$(BOT) list-strategies $(FUTURES_CONFIG)

futures-download:
	$(BOT) download-data $(FUTURES_CONFIG) --timeframes 4h 1h 1d --timerange $(TIMERANGE) --prepend

futures-backtest:
	$(BOT) backtesting $(FUTURES_CONFIG) $(FUTURES_STRATEGY) --timerange $(TIMERANGE) --timeframe-detail 1h --fee 0.0005 --enable-protections --cache none --export trades

futures-lookahead:
	$(BOT) lookahead-analysis $(FUTURES_CONFIG) $(FUTURES_STRATEGY) --timerange $(TIMERANGE) --fee 0.0005 --minimum-trade-amount 10 --targeted-trade-amount 30

futures-recursive:
	$(BOT) recursive-analysis $(FUTURES_CONFIG) $(FUTURES_STRATEGY) --timerange $(TIMERANGE) -p BTC/USDT:USDT --startup-candle 1500 2000 2400

alpha001-bias-audit:
	$(PYTHON) scripts/run_alpha001_bias_audit.py --timerange $(BIAS_AUDIT_TIMERANGE)

futures-cost-stress:
	$(BOT) backtesting $(FUTURES_CONFIG) $(FUTURES_STRATEGY) --timerange $(TIMERANGE) --timeframe-detail 1h --fee 0.001 --enable-protections --cache none --export trades

futures-daily-guard-backtest:
	$(BOT) backtesting $(FUTURES_DAILY_GUARD_CONFIG) $(FUTURES_DAILY_GUARD_STRATEGY) --timerange $(TIMERANGE) --timeframe-detail 1h --fee 0.001 --enable-protections --cache none --export trades

futures-closed-metrics:
	@test -n "$(RESULT)" || (echo "Usage: make futures-closed-metrics RESULT=user_data/backtest_results/<file>.zip"; exit 2)
	$(PYTHON) scripts/closed_trade_metrics.py "$(RESULT)" --strategy FuturesBtcRegimeRiskBudgetLong

futures-dryrun-preflight:
	$(PYTHON) scripts/live_preflight.py --base user_data/config.futures.json

binance-permission-audit:
	@test -f .env.live || (echo "Copy .env.live.example to .env.live first"; exit 2)
	$(PYTHON) scripts/audit_binance_api_permissions.py --env-file .env.live --output reports/binance-api-permission-audit.json

binance-account-audit:
	@test -f .env.live || (echo "Copy .env.live.example to .env.live first"; exit 2)
	$(PYTHON) scripts/audit_binance_futures_account.py --config user_data/config.futures.json --env-file .env.live --output reports/binance-futures-account-audit.json

futures-order-feasibility-audit:
	$(PYTHON) scripts/audit_futures_order_feasibility.py --config user_data/config.futures.json --maximum-notional-usdt 50 --output reports/binance-usdtm-order-feasibility.json

futures-live-preflight:
	@test -f "$(LIVE_OVERLAY)" || (echo "Copy user_data/config.futures.live.example.json to $(LIVE_OVERLAY) first"; exit 2)
	@test -f .env.live || (echo "Copy .env.live.example to .env.live first"; exit 2)
	$(PYTHON) scripts/audit_binance_api_permissions.py --env-file .env.live --output reports/binance-api-permission-audit.json
	$(PYTHON) scripts/audit_binance_futures_account.py --config user_data/config.futures.json --env-file .env.live --output reports/binance-futures-account-audit.json
	$(PYTHON) scripts/audit_futures_order_feasibility.py --config user_data/config.futures.json --maximum-notional-usdt 50 --output reports/binance-usdtm-order-feasibility.json
	@test -f "$(EXECUTION_AUDIT)" || (echo "Set EXECUTION_AUDIT=reports/maker-execution.json"; exit 2)
	$(PYTHON) scripts/live_preflight.py --base user_data/config.futures.json --overlay "$(LIVE_OVERLAY)" --env-file .env.live --research-report reports/alpha001-forward.json --bias-audit reports/alpha001-bias-audit.json --execution-audit "$(EXECUTION_AUDIT)" --permission-audit reports/binance-api-permission-audit.json --account-audit reports/binance-futures-account-audit.json --exchange-filter-audit reports/binance-usdtm-order-feasibility.json --compose-file docker-compose.yml --compose-file docker-compose.live.yml --require-live --require-env

futures-dry-run:
	$(COMPOSE) --profile futures up -d freqtrade-futures

futures-logs:
	$(COMPOSE) --profile futures logs -f --tail=200 freqtrade-futures

futures-stop:
	$(COMPOSE) --profile futures stop freqtrade-futures

research-test:
	$(PYTHON) -m unittest discover -s tests -v

research-evaluate:
	@test -n "$(DEV_RESULT)" || (echo "Set DEV_RESULT=<freqtrade development zip>"; exit 2)
	@test -n "$(VAL_RESULT)" || (echo "Set VAL_RESULT=<freqtrade validation zip>"; exit 2)
	@test -n "$(STRESS_RESULT)" || (echo "Set STRESS_RESULT=<freqtrade cost-stress zip>"; exit 2)
	@test -n "$(REASON)" || (echo "Set REASON='pre-committed hypothesis'"; exit 2)
	$(PYTHON) scripts/research_pipeline.py --strategy "$(RESEARCH_STRATEGY)" --hypothesis "$(HYPOTHESIS)" --reason "$(REASON)" --development "$(DEV_RESULT)" --validation "$(VAL_RESULT)" --cost-stress "$(STRESS_RESULT)" --lookahead-passed --recursive-passed

data-validate:
	@test -n "$(DATA)" || (echo "Set DATA=<OHLCV file> and INTERVAL_MS=<candle interval>"; exit 2)
	@test -n "$(INTERVAL_MS)" || (echo "Set INTERVAL_MS=<candle interval in milliseconds>"; exit 2)
	$(PYTHON) scripts/validate_data.py "$(DATA)" --interval-ms "$(INTERVAL_MS)"

platform-scoreboard: portfolio-activation-readiness
	$(PYTHON) scripts/build_platform_scoreboard.py

register-phase3:
	$(PYTHON) scripts/register_phase3_experiments.py

universe-snapshot:
	$(PYTHON) scripts/collect_universe_snapshot.py --output-dir research/universe_snapshots --data-dir user_data/data/binance

universe-snapshot-all:
	$(PYTHON) scripts/collect_universe_snapshot.py --all-usdt-perpetual --depth-pairs 50 --output-dir research/universe_snapshots --data-dir user_data/data/binance

universe-status:
	$(PYTHON) scripts/universe_status.py

universe-scheduler-start:
	$(PYTHON) scripts/run_universe_scheduler.py start

universe-scheduler-status:
	$(PYTHON) scripts/run_universe_scheduler.py status

universe-scheduler-stop:
	$(PYTHON) scripts/run_universe_scheduler.py stop

historical-symbol-catalog:
	$(PYTHON) scripts/collect_historical_symbol_catalog.py

edge-budget:
	@test -n "$(GROSS_EDGE_BPS)" || (echo "Set GROSS_EDGE_BPS=<expected gross edge per trade in bps>"; exit 2)
	@test -n "$(FEE_BPS)" || (echo "Set FEE_BPS=<round-trip fee budget in bps>"; exit 2)
	$(PYTHON) scripts/check_edge_budget.py --gross-edge-bps "$(GROSS_EDGE_BPS)" --fee-bps "$(FEE_BPS)" $(if $(SPREAD_BPS),--spread-bps "$(SPREAD_BPS)") $(if $(SLIPPAGE_BPS),--slippage-bps "$(SLIPPAGE_BPS)") $(if $(FUNDING_BPS),--funding-bps "$(FUNDING_BPS)")

aoa-analyze:
	$(PYTHON) scripts/analyze_aoa_history.py --fetch-market-context

aoa-behavior-model:
	$(PYTHON) scripts/analyze_aoa_behavior.py

aoa-prototype-validate:
	$(BOT) show-config $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-prototype.research.json
	$(BOT) list-strategies $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-prototype.research.json

aoa-distilled-validate:
	$(BOT) show-config $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-distilled.research.json
	$(BOT) list-strategies $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-distilled.research.json

aoa-distilled-backtest:
	$(BOT) backtesting $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-distilled.research.json --strategy AoaDistilledPolicyPrototype --timerange 20220601-20260901 --fee 0.0005 --enable-protections --cache none --export trades

aoa-distilled-lookahead:
	$(BOT) lookahead-analysis $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-distilled.research.json --config /freqtrade/user_data/config.futures.aoa-distilled.lookahead.json --strategy AoaDistilledPolicyPrototype --timerange 20220601-20260901 --fee 0.0005 --minimum-trade-amount 10 --targeted-trade-amount 30

aoa-distilled-recursive:
	$(BOT) recursive-analysis $(FUTURES_CONFIG) --config /freqtrade/user_data/config.futures.aoa-distilled.research.json --strategy AoaDistilledPolicyPrototype --timerange 20220601-20260901 -p BTC/USDT:USDT --startup-candle 300 600 1200 2400

aoa-forward-report:
	@test -n "$(AOA_FORWARD_RESULT)" || (echo "Set AOA_FORWARD_RESULT=<locked Freqtrade result zip>"; exit 2)
	$(PYTHON) scripts/aoa_forward_report.py --result "$(AOA_FORWARD_RESULT)" \
		--data user_data/data/binance/futures/BTC_USDT_USDT-5m-futures.feather \
		--data user_data/data/binance/futures/BTC_USDT_USDT-1h-futures.feather \
		--data user_data/data/binance/futures/BTC_USDT_USDT-1d-futures.feather \
		--data user_data/data/binance/futures/BTC_USDT_USDT-1h-funding_rate.feather \
		--data user_data/data/binance/futures/BTC_USDT_USDT-1h-mark.feather

maker-execution-report:
	@test -n "$(EXECUTIONS)" || (echo "Set EXECUTIONS=<space-separated current-protocol jsonl segments>"; exit 2)
	$(PYTHON) scripts/maker_execution_report.py $(EXECUTIONS) --evidence-kind public_l2_shadow

execution-calibration-report:
	@test -n "$(CALIBRATION_MANIFEST)" || (echo "Set CALIBRATION_MANIFEST=<completed calibration manifest>"; exit 2)
	$(PYTHON) scripts/calibration_execution_report.py $(CALIBRATION_MANIFEST)

shadow-maker-probe:
	@test -n "$(SIDE)" || (echo "Set SIDE=buy|sell"; exit 2)
	@test -n "$(QTY)$(NOTIONAL_USDT)" || (echo "Set QTY=<base quantity> or NOTIONAL_USDT=<quote notional>"; exit 2)
	$(PYTHON) scripts/shadow_maker_probe.py $(if $(SYMBOL),--symbol "$(SYMBOL)") --side "$(SIDE)" $(if $(QTY),--qty "$(QTY)",--notional-usdt "$(NOTIONAL_USDT)") $(if $(SIGNAL_ID),--signal-id "$(SIGNAL_ID)")

shadow-entry-campaign-start:
	$(PYTHON) scripts/manage_shadow_entry_campaign.py start

shadow-entry-campaign-status:
	$(PYTHON) scripts/manage_shadow_entry_campaign.py status

shadow-entry-campaign-stop:
	$(PYTHON) scripts/manage_shadow_entry_campaign.py stop

shadow-entry-calibration-scheduled:
	@test -n "$(NOT_BEFORE)" || (echo "Set NOT_BEFORE=<ISO-8601 UTC timestamp>"; exit 2)
	$(PYTHON) scripts/scheduled_shadow_calibration.py --not-before "$(NOT_BEFORE)"

liquidation-telemetry-start:
	$(PYTHON) scripts/manage_liquidation_telemetry_campaign.py start

liquidation-telemetry-status:
	$(PYTHON) scripts/manage_liquidation_telemetry_campaign.py status

liquidation-telemetry-coverage:
	$(PYTHON) scripts/liquidation_telemetry_status.py

liquidation-overlay-development:
	$(PYTHON) scripts/analyze_liquidation_overlay.py

liquidation-overlay-validation-open:
	$(PYTHON) scripts/open_liquidation_overlay_validation.py

liquidation-overlay-validation:
	$(PYTHON) scripts/analyze_liquidation_overlay_validation.py

liquidation-overlay-scheduler-start:
	$(PYTHON) scripts/run_liquidation_overlay_scheduler.py start

liquidation-overlay-scheduler-status:
	$(PYTHON) scripts/run_liquidation_overlay_scheduler.py status

liquidation-overlay-scheduler-stop:
	$(PYTHON) scripts/run_liquidation_overlay_scheduler.py stop

liquidation-telemetry-stop:
	$(PYTHON) scripts/manage_liquidation_telemetry_campaign.py stop

microstructure-collect:
	$(PYTHON) scripts/collect_microstructure.py $(if $(SYMBOL),--symbol "$(SYMBOL)") $(if $(DURATION),--duration-seconds "$(DURATION)")

microstructure-report:
	$(PYTHON) scripts/analyze_microstructure.py
	$(PYTHON) scripts/alpha007_partial_fill_stress.py
	$(PYTHON) scripts/sync_alpha007_registry.py

alpha007-external-cost-stress:
	$(PYTHON) scripts/alpha007_external_cost_stress.py

alpha007-partial-fill-register:
	$(PYTHON) scripts/register_alpha007_partial_fill_policy.py

alpha007-partial-fill-stress:
	$(PYTHON) scripts/alpha007_partial_fill_stress.py

alpha007-exchange-filter-audit:
	$(PYTHON) scripts/alpha007_exchange_filter_audit.py

alpha007-order-path-register:
	$(PYTHON) scripts/register_alpha007_order_path_policy.py

alpha007-order-path-status:
	$(PYTHON) scripts/alpha007_order_path_status.py

microstructure-campaign:
	@test -n "$(HOURS)" || (echo "Set HOURS=<bounded collection hours>, for example HOURS=24"; exit 2)
	$(PYTHON) scripts/run_microstructure_campaign.py --hours "$(HOURS)" $(if $(CHUNK_SECONDS),--chunk-seconds "$(CHUNK_SECONDS)") $(if $(SYMBOL),--symbol "$(SYMBOL)") $(if $(MIN_FREE_GB),--minimum-free-gb "$(MIN_FREE_GB)")

microstructure-campaign-start:
	@test -n "$(HOURS)" || (echo "Set HOURS=<bounded collection hours>, for example HOURS=456"; exit 2)
	$(PYTHON) scripts/manage_microstructure_campaign.py start --hours "$(HOURS)" $(if $(CHUNK_SECONDS),--chunk-seconds "$(CHUNK_SECONDS)") $(if $(SYMBOL),--symbol "$(SYMBOL)") $(if $(MIN_FREE_GB),--minimum-free-gb "$(MIN_FREE_GB)")

microstructure-campaign-status:
	$(PYTHON) scripts/manage_microstructure_campaign.py status

microstructure-campaign-stop:
	$(PYTHON) scripts/manage_microstructure_campaign.py stop

microstructure-preflight:
	$(PYTHON) scripts/microstructure_preflight.py $(if $(HOURS),--hours "$(HOURS)") $(if $(MIN_FREE_GB),--minimum-free-gb "$(MIN_FREE_GB)")

alpha001-forward-start:
	$(PYTHON) scripts/manage_alpha001_forward_dryrun.py start

alpha001-forward-status:
	$(PYTHON) scripts/manage_alpha001_forward_dryrun.py status

alpha001-forward-stop:
	$(PYTHON) scripts/manage_alpha001_forward_dryrun.py stop

alpha001-forward-report:
	$(PYTHON) scripts/alpha001_forward_report.py

research-health:
	$(PYTHON) scripts/research_health.py

historical-research-status:
	$(PYTHON) scripts/historical_research_status.py

historical-release-train-status:
	$(PYTHON) scripts/historical_release_train_status.py

historical-execution-coverage:
	$(PYTHON) scripts/audit_historical_execution_coverage.py

historical-funding-batch:
	$(PYTHON) scripts/collect_historical_monthly_funding.py --task-offset $(HISTORICAL_FUNDING_BATCH_OFFSET) --archive-limit $(HISTORICAL_FUNDING_BATCH_LIMIT)

historical-funding-resume:
	$(PYTHON) scripts/collect_historical_monthly_funding.py --resume --archive-limit $(HISTORICAL_FUNDING_BATCH_LIMIT)

historical-funding-finalize:
	$(PYTHON) scripts/collect_historical_monthly_funding.py --finalize

historical-funding-scheduler-start:
	$(PYTHON) scripts/run_historical_funding_scheduler.py start

historical-funding-scheduler-status:
	$(PYTHON) scripts/run_historical_funding_scheduler.py status

historical-funding-scheduler-stop:
	$(PYTHON) scripts/run_historical_funding_scheduler.py stop

historical-funding-coverage:
	$(PYTHON) scripts/audit_historical_funding_coverage.py

historical-funding-ledger-audit:
	$(PYTHON) scripts/audit_historical_funding_ledger.py

goal-readiness: platform-scoreboard
	$(PYTHON) scripts/goal_readiness_report.py

risk-policy-status:
	$(PYTHON) scripts/risk_policy_status.py

portfolio-activation-readiness:
	$(PYTHON) scripts/portfolio_activation_readiness.py

selection-bias-audit:
	@test -n "$(TRIAL_RETURNS)" || (echo "Set TRIAL_RETURNS=<daily trial matrix CSV>"; exit 2)
	@test -n "$(TRIAL_INVENTORY)" || (echo "Set TRIAL_INVENTORY=<sealed trial inventory JSON>"; exit 2)
	@test -n "$(SELECTION_BIAS_OUTPUT)" || (echo "Set SELECTION_BIAS_OUTPUT=<audit JSON>"; exit 2)
	$(PYTHON) scripts/selection_bias_audit.py --trial-returns "$(TRIAL_RETURNS)" --trial-inventory "$(TRIAL_INVENTORY)" --output "$(SELECTION_BIAS_OUTPUT)"

signal-diagnostics:
	@test -n "$(SIGNAL_PANEL)" || (echo "Set SIGNAL_PANEL=<causal signal/forward-return CSV>"; exit 2)
	@test -n "$(SIGNAL_DIAGNOSTIC_OUTPUT)" || (echo "Set SIGNAL_DIAGNOSTIC_OUTPUT=<diagnostic JSON>"; exit 2)
	$(PYTHON) scripts/signal_diagnostics_report.py --input "$(SIGNAL_PANEL)" --output "$(SIGNAL_DIAGNOSTIC_OUTPUT)" $(if $(MIN_CROSS_SECTION),--minimum-cross-section "$(MIN_CROSS_SECTION)") $(if $(QUANTILE_FRACTION),--quantile-fraction "$(QUANTILE_FRACTION)")

alpha013-signal-panel:
	$(PYTHON) scripts/build_alpha013_signal_panel.py --panel-output research/alpha013/signal-panel.csv --receipt-output reports/alpha013-signal-panel.json

portfolio-risk-audit:
	@test -n "$(PORTFOLIO_EQUITY)" || (echo "Set PORTFOLIO_EQUITY=<daily MTM equity CSV>"; exit 2)
	@test -n "$(PORTFOLIO_RISK_OUTPUT)" || (echo "Set PORTFOLIO_RISK_OUTPUT=<risk audit JSON>"; exit 2)
	$(PYTHON) scripts/portfolio_risk_audit.py --equity "$(PORTFOLIO_EQUITY)" --output "$(PORTFOLIO_RISK_OUTPUT)"

portfolio-execution-audit:
	@test -n "$(PORTFOLIO_MANIFEST)" || (echo "Set PORTFOLIO_MANIFEST=<preregistered portfolio JSON>"; exit 2)
	@test -n "$(PORTFOLIO_EXECUTION_OBSERVATION_MANIFEST)" || (echo "Set PORTFOLIO_EXECUTION_OBSERVATION_MANIFEST=<sealed execution observation manifest JSON>"; exit 2)
	@test -n "$(PORTFOLIO_EQUITY)" || (echo "Set PORTFOLIO_EQUITY=<daily MTM equity CSV>"; exit 2)
	@test -n "$(PORTFOLIO_EXECUTION_AUDIT)" || (echo "Set PORTFOLIO_EXECUTION_AUDIT=<execution audit JSON output>"; exit 2)
	$(PYTHON) scripts/portfolio_execution_audit.py --manifest "$(PORTFOLIO_MANIFEST)" --observation-manifest "$(PORTFOLIO_EXECUTION_OBSERVATION_MANIFEST)" --portfolio-equity "$(PORTFOLIO_EQUITY)" --output "$(PORTFOLIO_EXECUTION_AUDIT)"

portfolio-promotion:
	@test -n "$(PORTFOLIO_MANIFEST)" || (echo "Set PORTFOLIO_MANIFEST=<preregistered portfolio JSON>"; exit 2)
	@test -n "$(PORTFOLIO_REPORT)" || (echo "Set PORTFOLIO_REPORT=<combined locked-forward report JSON>"; exit 2)
	@test -n "$(PORTFOLIO_SELECTION_BIAS_AUDIT)" || (echo "Set PORTFOLIO_SELECTION_BIAS_AUDIT=<combined DSR/PBO audit JSON>"; exit 2)
	@test -n "$(PORTFOLIO_RISK_AUDIT)" || (echo "Set PORTFOLIO_RISK_AUDIT=<combined multi-block risk audit JSON>"; exit 2)
	@test -n "$(PORTFOLIO_CONTRIBUTIONS)" || (echo "Set PORTFOLIO_CONTRIBUTIONS=<daily sleeve PnL contribution CSV>"; exit 2)
	@test -n "$(PORTFOLIO_SLEEVE_PNL)" || (echo "Set PORTFOLIO_SLEEVE_PNL=<source-bound daily sleeve net PnL fraction CSV>"; exit 2)
	@test -n "$(PORTFOLIO_TRADES)" || (echo "Set PORTFOLIO_TRADES=<closed portfolio trade ledger CSV>"; exit 2)
	@test -n "$(PORTFOLIO_EXPOSURE)" || (echo "Set PORTFOLIO_EXPOSURE=<daily maximum gross/net leverage CSV>"; exit 2)
	@test -n "$(PORTFOLIO_ACCOUNTING)" || (echo "Set PORTFOLIO_ACCOUNTING=<daily MTM PnL component CSV>"; exit 2)
	@test -n "$(SLEEVE_REPORTS)" || (echo "Set SLEEVE_REPORTS='CANDIDATE=report.json ...'"; exit 2)
	@test -n "$(SLEEVE_SELECTION_BIAS_AUDITS)" || (echo "Set SLEEVE_SELECTION_BIAS_AUDITS='CANDIDATE=audit.json ...'"; exit 2)
	@test -n "$(PORTFOLIO_EXECUTION_AUDIT)" || (echo "Set PORTFOLIO_EXECUTION_AUDIT=<execution audit JSON>"; exit 2)
	$(PYTHON) scripts/portfolio_promotion_report.py --manifest "$(PORTFOLIO_MANIFEST)" --portfolio-report "$(PORTFOLIO_REPORT)" --portfolio-selection-bias-audit "$(PORTFOLIO_SELECTION_BIAS_AUDIT)" --portfolio-risk-audit "$(PORTFOLIO_RISK_AUDIT)" --portfolio-contributions "$(PORTFOLIO_CONTRIBUTIONS)" --portfolio-sleeve-pnl "$(PORTFOLIO_SLEEVE_PNL)" --portfolio-trades "$(PORTFOLIO_TRADES)" --portfolio-exposure "$(PORTFOLIO_EXPOSURE)" --portfolio-accounting "$(PORTFOLIO_ACCOUNTING)" $(foreach report,$(SLEEVE_REPORTS),--sleeve-report "$(report)") $(foreach audit,$(SLEEVE_SELECTION_BIAS_AUDITS),--sleeve-selection-bias-audit "$(audit)") --execution-audit "$(PORTFOLIO_EXECUTION_AUDIT)"

monthly-objective:
	@test -n "$(RESULT)" || (echo "Set RESULT=<Freqtrade result zip>"; exit 2)
	@test -n "$(MONTHLY_STRATEGY)" || (echo "Set MONTHLY_STRATEGY=<strategy class>"; exit 2)
	$(PYTHON) scripts/monthly_objective_report.py "$(RESULT)" --strategy "$(MONTHLY_STRATEGY)" $(if $(STRESS_RESULT),--double-cost-result "$(STRESS_RESULT)") $(if $(SLIPPAGE_STRESS_RESULT),--slippage-stress-result "$(SLIPPAGE_STRESS_RESULT)") $(if $(FUNDING_STRESS_RESULT),--funding-stress-result "$(FUNDING_STRESS_RESULT)") $(if $(EQUITY_CURVE),--equity-curve "$(EQUITY_CURVE)")

equity-record:
	$(PYTHON) scripts/record_mtm_equity.py $(if $(DURATION),--duration-seconds "$(DURATION)") $(if $(OUTPUT),--output "$(OUTPUT)")

equity-daily:
	@test -n "$(RAW)" || (echo "Set RAW='<one or more raw JSONL paths>'"; exit 2)
	@test -n "$(OUTPUT)" || (echo "Set OUTPUT=<daily MTM CSV>"; exit 2)
	$(PYTHON) scripts/build_daily_equity_curve.py $(RAW) --output "$(OUTPUT)" $(if $(MAX_AGE),--maximum-age-seconds "$(MAX_AGE)")

alpha005-development:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_005_FUNDING_EXTREME_REVERSAL --timerange 20210101-20240101 --timeframe 1h --fee 0.0005 --enable-protections --cache none --export trades

alpha008-development:
	$(PYTHON) -m freqtrade backtesting --config user_data/config.futures.json --config user_data/config.futures.alpha008.research.json --strategy ALPHA_008_TIME_SERIES_MOMENTUM --timerange 20220101-20250101 --timeframe 4h --fee 0.0005 --enable-protections --cache none --export trades

alpha009-development:
	$(PYTHON) scripts/analyze_funding_carry.py

alpha010-register:
	$(PYTHON) scripts/register_alpha010.py

alpha010-metrics-download:
	$(PYTHON) scripts/download_binance_futures_metrics.py --symbol BTCUSDT --start 2021-01-01 --end-exclusive 2025-01-01 --output-root research/alpha010/raw

alpha010-development:
	$(PYTHON) scripts/analyze_leverage_crowding.py

alpha010-sync:
	$(PYTHON) scripts/sync_alpha010_registry.py

alpha010-postmortem:
	$(PYTHON) scripts/alpha010_postmortem.py

alpha011-register:
	$(PYTHON) scripts/register_alpha011.py

alpha011-amendment:
	$(PYTHON) scripts/record_alpha011_amendment.py

alpha011-download:
	@test -n "$(START)" -a -n "$(END)" || (echo "Set START=YYYY-MM-DD END=YYYY-MM-DD (end-exclusive)"; exit 2)
	$(PYTHON) scripts/download_binance_aggtrades.py --symbol BTCUSDT --start "$(START)" --end-exclusive "$(END)"

alpha011-update:
	$(PYTHON) scripts/update_alpha011_data.py

alpha011-funding-reconcile:
	@test -n "$(MANIFEST)" -a -n "$(MONTH)" || (echo "Set MANIFEST=<sealed Alpha #11 manifest> MONTH=YYYY-MM"; exit 2)
	$(PYTHON) scripts/reconcile_alpha011_funding.py --manifest "$(MANIFEST)" --month "$(MONTH)"

alpha011-scheduler-start:
	$(PYTHON) scripts/run_alpha011_scheduler.py start

alpha011-scheduler-status:
	$(PYTHON) scripts/run_alpha011_scheduler.py status

alpha011-scheduler-stop:
	$(PYTHON) scripts/run_alpha011_scheduler.py stop

alpha011-status:
	$(PYTHON) scripts/alpha011_status.py

alpha011-development:
	@test -n "$(MANIFEST)" || (echo "Set MANIFEST=<sealed Alpha #11 manifest>"; exit 2)
	$(PYTHON) scripts/analyze_quarter_hour_flow.py --manifest "$(MANIFEST)"

alpha011-sync:
	$(PYTHON) scripts/sync_alpha011_registry.py

alpha011-validation-open:
	$(PYTHON) scripts/open_alpha011_validation.py

alpha011-validation:
	@test -n "$(MANIFEST)" || (echo "Set MANIFEST=<sealed Alpha #11 manifest>"; exit 2)
	$(PYTHON) scripts/analyze_alpha011_validation.py --manifest "$(MANIFEST)"

alpha011-validation-sync:
	$(PYTHON) scripts/sync_alpha011_validation.py

alpha012-register:
	$(PYTHON) scripts/register_alpha012.py

alpha012-development:
	$(PYTHON) scripts/analyze_spot_perp_carry.py

alpha012-sync:
	$(PYTHON) scripts/sync_alpha012_registry.py

alpha013-register:
	$(PYTHON) scripts/register_alpha013.py

alpha013-download-day:
	@test -n "$(DAY)" || (echo "Set DAY=YYYY-MM-DD"; exit 2)
	$(PYTHON) scripts/download_alpha013_day.py --day "$(DAY)"

alpha013-update:
	$(PYTHON) scripts/update_alpha013_data.py

alpha013-funding-reconcile:
	@test -n "$(MONTH)" || (echo "Set MONTH=YYYY-MM"; exit 2)
	$(PYTHON) scripts/reconcile_alpha013_funding.py --month "$(MONTH)"

alpha013-forward-report:
	$(PYTHON) scripts/analyze_alpha013_forward.py

alpha013-decision-availability-audit:
	$(PYTHON) scripts/audit_alpha013_decision_availability.py

alpha013-causal-veto-sync:
	$(PYTHON) scripts/sync_alpha013_causal_veto.py

alpha013-sync:
	$(PYTHON) scripts/sync_alpha013_registry.py

alpha013-validation-open:
	$(PYTHON) scripts/open_alpha013_validation.py

alpha013-validation:
	$(PYTHON) scripts/analyze_alpha013_validation.py

alpha013-validation-sync:
	$(PYTHON) scripts/sync_alpha013_validation.py

alpha013-scheduler-start:
	$(PYTHON) scripts/run_alpha013_scheduler.py start

alpha013-scheduler-status:
	$(PYTHON) scripts/run_alpha013_scheduler.py status

alpha013-scheduler-stop:
	$(PYTHON) scripts/run_alpha013_scheduler.py stop

alpha013-status:
	$(PYTHON) scripts/alpha013_status.py

alpha014-register:
	$(PYTHON) scripts/register_alpha014.py

alpha014-amendment:
	$(PYTHON) scripts/record_alpha014_amendment.py

alpha014-development:
	$(PYTHON) scripts/analyze_alpha014_basis.py

alpha014-sync:
	$(PYTHON) scripts/sync_alpha014_registry.py

alpha015-readiness: universe-status
	$(PYTHON) scripts/alpha015_readiness.py

alpha015-activation-lock: alpha015-readiness
	$(PYTHON) scripts/alpha015_activation_lock.py
	$(PYTHON) scripts/alpha015_readiness.py

alpha015-register: alpha015-activation-lock
	$(PYTHON) scripts/register_alpha015.py

ALPHA015_SIZE_PROBE_LIMIT ?= 50
ALPHA015_DOWNLOAD_LIMIT ?= 1

alpha015-acquisition-plan:
	$(PYTHON) scripts/acquire_alpha015.py

alpha015-size-probe:
	$(PYTHON) scripts/acquire_alpha015.py --probe-sizes $(ALPHA015_SIZE_PROBE_LIMIT)

alpha015-download-batch:
	$(PYTHON) scripts/acquire_alpha015.py --download-limit $(ALPHA015_DOWNLOAD_LIMIT)

alpha015-development:
	$(PYTHON) scripts/evaluate_alpha015_development.py

alpha015-validation-open:
	$(PYTHON) scripts/open_alpha015_validation.py

alpha015-validation-acquisition-plan:
	$(PYTHON) scripts/acquire_alpha015.py --phase locked_validation

alpha015-validation-size-probe:
	$(PYTHON) scripts/acquire_alpha015.py --phase locked_validation --probe-sizes $(ALPHA015_SIZE_PROBE_LIMIT)

alpha015-validation-download-batch:
	$(PYTHON) scripts/acquire_alpha015.py --phase locked_validation --download-limit $(ALPHA015_DOWNLOAD_LIMIT)

alpha015-funding-reconcile:
	$(PYTHON) scripts/reconcile_alpha015_funding.py

alpha015-validation:
	$(PYTHON) scripts/evaluate_alpha015_validation.py

HISTORICAL_UNIVERSE_BATCH_LIMIT ?= 25
HISTORICAL_UNIVERSE_BATCH_OFFSET ?= 0

historical-universe-batch:
	$(PYTHON) scripts/collect_historical_daily_universe.py --task-offset $(HISTORICAL_UNIVERSE_BATCH_OFFSET) --archive-limit $(HISTORICAL_UNIVERSE_BATCH_LIMIT)

historical-universe-resume:
	$(PYTHON) scripts/collect_historical_daily_universe.py --resume --archive-limit $(HISTORICAL_UNIVERSE_BATCH_LIMIT)

historical-universe-finalize:
	$(PYTHON) scripts/collect_historical_daily_universe.py --finalize

historical-universe-audit:
	$(PYTHON) scripts/audit_historical_universe.py

HISTORICAL_UNIVERSE_SCHEDULER_BATCH ?= 100
HISTORICAL_UNIVERSE_SCHEDULER_INTERVAL ?= 60

historical-universe-scheduler-start:
	$(PYTHON) scripts/run_historical_universe_scheduler.py start --batch-size $(HISTORICAL_UNIVERSE_SCHEDULER_BATCH) --interval-seconds $(HISTORICAL_UNIVERSE_SCHEDULER_INTERVAL)

historical-universe-scheduler-status:
	$(PYTHON) scripts/run_historical_universe_scheduler.py status

historical-universe-scheduler-stop:
	$(PYTHON) scripts/run_historical_universe_scheduler.py stop

historical-cost-policy-status:
	$(PYTHON) scripts/historical_cost_policy_status.py

alpha005-validation:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_005_FUNDING_EXTREME_REVERSAL --timerange 20240101-20260901 --timeframe 1h --fee 0.0005 --enable-protections --cache none --export trades

alpha005-cost-stress:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_005_FUNDING_EXTREME_REVERSAL --timerange 20240101-20260901 --timeframe 1h --fee 0.001 --enable-protections --cache none --export trades

alpha006-development:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_006_CROSS_SECTIONAL_SWING --timerange 20210101-20240101 --timeframe 4h --fee 0.0005 --enable-protections --cache none --export trades

alpha006-validation:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_006_CROSS_SECTIONAL_SWING --timerange 20240101-20260901 --timeframe 4h --fee 0.0005 --enable-protections --cache none --export trades

alpha006-cost-stress:
	$(BOT) backtesting $(FUTURES_CONFIG) --strategy ALPHA_006_CROSS_SECTIONAL_SWING --timerange 20240101-20260901 --timeframe 4h --fee 0.001 --enable-protections --cache none --export trades
