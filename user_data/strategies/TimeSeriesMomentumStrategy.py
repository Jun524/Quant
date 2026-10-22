"""Pre-registered low-turnover absolute time-series momentum candidate."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import sqrt

import numpy as np
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative


class ALPHA_008_TIME_SERIES_MOMENTUM(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "4h"
    startup_candle_count = 1200
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.15
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    momentum_days = 180
    volatility_days = 60
    maximum_holding_days = 28
    portfolio_volatility_target = 0.20
    asset_count = 5
    maximum_asset_weight = 0.25

    order_types = {
        "entry": "limit",
        "exit": "limit",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_price_type": "mark",
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 6},
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 1080,
                "trade_limit": 20,
                "stop_duration_candles": 84,
                "max_allowed_drawdown": 0.15,
            },
        ]

    @informative("1d")
    def populate_indicators_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        log_return = np.log(dataframe["close"] / dataframe["close"].shift(1))
        dataframe["return_180d"] = dataframe["close"] / dataframe["close"].shift(self.momentum_days) - 1.0
        dataframe["realized_vol_60d"] = log_return.rolling(self.volatility_days, min_periods=self.volatility_days).std() * sqrt(365)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rebalance = (dataframe["date"].dt.day == 1) & (dataframe["date"].dt.hour == 0)
        usable = (
            dataframe["return_180d_1d"].notna()
            & dataframe["realized_vol_60d_1d"].notna()
            & (dataframe["realized_vol_60d_1d"] > 0)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[rebalance & usable & (dataframe["return_180d_1d"] > 0), ["enter_long", "enter_tag"]] = (
            1,
            "monthly_tsmom_long",
        )
        dataframe.loc[rebalance & usable & (dataframe["return_180d_1d"] < 0), ["enter_short", "enter_tag"]] = (
            1,
            "monthly_tsmom_short",
        )
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs,
    ) -> str | None:
        if current_time - trade.open_date_utc >= timedelta(days=self.maximum_holding_days):
            return "monthly_tsmom_28d_exit"
        return None

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        return min(1.0, max_leverage)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return 0.0
        realized_volatility = float(dataframe.iloc[-1].get("realized_vol_60d_1d") or 0.0)
        equity = self.wallets.get_total_stake_amount()
        if not np.isfinite(realized_volatility) or realized_volatility <= 0 or equity <= 0:
            return 0.0
        asset_volatility_budget = self.portfolio_volatility_target / sqrt(self.asset_count)
        weight = min(self.maximum_asset_weight, asset_volatility_budget / realized_volatility)
        stake = equity * weight
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)
