"""Long/short Binance USDT-M futures research baseline.

The strategy intentionally caps leverage at 2x, uses isolated margin and avoids
position averaging. It is a validation baseline, not a profitable live system.
"""

from datetime import datetime

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy


class FuturesTrendStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "1h"
    startup_candle_count = 800
    process_only_new_candles = True

    # Freqtrade expresses ROI and stoploss on leveraged PnL. At 2x, an 8% PnL
    # stop is approximately a 4% adverse move in the underlying before costs.
    minimal_roi = {"1440": 0.08, "720": 0.15, "0": 0.30}
    stoploss = -0.08
    trailing_stop = True
    trailing_stop_positive = 0.025
    trailing_stop_positive_offset = 0.04
    trailing_only_offset_is_reached = True

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

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
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 48,
                "trade_limit": 2,
                "stop_duration_candles": 12,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 168,
                "trade_limit": 10,
                "stop_duration_candles": 48,
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
        return min(2.0, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_20"] = dataframe["volume"].rolling(20).mean()

        # All channels use completed prior candles only.
        dataframe["high_50"] = dataframe["high"].rolling(50).max().shift(1)
        dataframe["low_50"] = dataframe["low"].rolling(50).min().shift(1)
        dataframe["high_20"] = dataframe["high"].rolling(20).max().shift(1)
        dataframe["low_20"] = dataframe["low"].rolling(20).min().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        liquid_and_moving = (
            dataframe["atr_pct"].between(0.005, 0.08)
            & (dataframe["volume"] > dataframe["volume_mean_20"])
            & (dataframe["volume"] > 0)
            & (dataframe["adx"] > 20)
        )

        long_breakout = (
            (dataframe["close"] > dataframe["high_50"])
            & (dataframe["close"].shift(1) <= dataframe["high_50"].shift(1))
            & (dataframe["close"] > dataframe["ema_200"])
            & (dataframe["ema_200"] > dataframe["ema_200"].shift(12))
            & dataframe["rsi"].between(52, 78)
        )
        short_breakout = (
            (dataframe["close"] < dataframe["low_50"])
            & (dataframe["close"].shift(1) >= dataframe["low_50"].shift(1))
            & (dataframe["close"] < dataframe["ema_200"])
            & (dataframe["ema_200"] < dataframe["ema_200"].shift(12))
            & dataframe["rsi"].between(22, 48)
        )

        dataframe.loc[
            long_breakout & liquid_and_moving,
            ["enter_long", "enter_tag"],
        ] = (1, "long_donchian50")
        dataframe.loc[
            short_breakout & liquid_and_moving,
            ["enter_short", "enter_tag"],
        ] = (1, "short_donchian50")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_channel_break = (
            (dataframe["close"] < dataframe["low_20"])
            & (dataframe["close"].shift(1) >= dataframe["low_20"].shift(1))
        )
        long_trend_failure = (
            (dataframe["close"] < dataframe["ema_200"])
            & (dataframe["close"].shift(1) >= dataframe["ema_200"].shift(1))
        )
        short_channel_break = (
            (dataframe["close"] > dataframe["high_20"])
            & (dataframe["close"].shift(1) <= dataframe["high_20"].shift(1))
        )
        short_trend_failure = (
            (dataframe["close"] > dataframe["ema_200"])
            & (dataframe["close"].shift(1) <= dataframe["ema_200"].shift(1))
        )

        dataframe.loc[
            (long_channel_break | long_trend_failure) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "long_channel_or_trend_failure")
        dataframe.loc[
            (short_channel_break | short_trend_failure) & (dataframe["volume"] > 0),
            ["exit_short", "exit_tag"],
        ] = (1, "short_channel_or_trend_failure")
        return dataframe

