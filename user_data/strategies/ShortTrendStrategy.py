"""Pre-registered BEAR-only short trend-following research strategy.

Alpha #3 is deliberately independent from Alpha #1.  Its sole hypothesis is
that an already established BTC bear market can support continuation shorts in
assets that also trade below their own long-term trend.  The values below are
fixed for Experiment 0003; this module contains no optimized parameters.
"""

from datetime import datetime
from pathlib import Path
import sys

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy, informative

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.regime.classifier import Regime, RegimeClassifier


class ALPHA_003_SHORT_TREND(IStrategy):
    """Follow new 20-day lows only while the common BTC regime is BEAR.

    Entry: BTC 50 < 200 EMA, falling BTC 200 EMA, asset below/falling own
    200 EMA, and a first close through a 20-day Donchian low.  Exit: a first
    close above a 10-day Donchian high, non-BEAR BTC regime, or fixed -8%
    emergency stop.  PANIC cannot enter because it is not the BEAR label.
    """

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "4h"
    startup_candle_count = 1500
    process_only_new_candles = True

    # No profit target or trailing-stop parameter is part of this experiment.
    minimal_roi = {"0": 10.0}
    stoploss = -0.08
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    entry_window = 120  # 20 days of 4-hour candles
    exit_window = 60  # 10 days of 4-hour candles
    trend_window = 200
    trend_slope_window = 20
    account_risk_fraction = 0.004

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
            {"method": "CooldownPeriod", "stop_duration_candles": 1},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 42,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 252,
                "trade_limit": 10,
                "stop_duration_candles": 84,
                "max_allowed_drawdown": 0.12,
            },
        ]

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
        """Experiment 0003 is explicitly one-times leverage."""
        return min(1.0, max_leverage)

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return RegimeClassifier().annotate(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=self.trend_window)
        dataframe["ema_200_slope"] = dataframe["ema_200"].pct_change(self.trend_slope_window)
        # Shifted channels ensure a candle cannot use its own low/high as a
        # breakout boundary.
        dataframe["entry_donchian_low"] = dataframe["low"].rolling(self.entry_window).min().shift(1)
        dataframe["exit_donchian_high"] = dataframe["high"].rolling(self.exit_window).max().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakdown = (
            (dataframe["close"] < dataframe["entry_donchian_low"])
            & (dataframe["close"].shift(1) >= dataframe["entry_donchian_low"].shift(1))
        )
        bear_context = (
            (dataframe["btc_regime_1d"] == Regime.BEAR.value)
            & (dataframe["btc_ema_50_1d"] < dataframe["btc_ema_200_1d"])
            & (dataframe["btc_ema_200_slope_1d"] < 0)
        )
        asset_trend = (dataframe["close"] < dataframe["ema_200"]) & (dataframe["ema_200_slope"] < 0)
        conditions = bear_context & asset_trend & breakdown & (dataframe["volume"] > 0)
        dataframe.loc[conditions, ["enter_short", "enter_tag"]] = (1, "alpha003_bear_breakdown")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rebound = (
            (dataframe["close"] > dataframe["exit_donchian_high"])
            & (dataframe["close"].shift(1) <= dataframe["exit_donchian_high"].shift(1))
        )
        left_bear = dataframe["btc_regime_1d"] != Regime.BEAR.value
        conditions = (rebound | left_bear) & (dataframe["volume"] > 0)
        dataframe.loc[conditions, ["exit_short", "exit_tag"]] = (1, "alpha003_rebound_or_regime_exit")
        return dataframe

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
        """Risk no more than 0.4% of account equity at the fixed stop."""
        equity = self.wallets.get_total_stake_amount()
        if equity <= 0:
            return 0.0
        stake = equity * self.account_risk_fraction / abs(self.stoploss)
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)
