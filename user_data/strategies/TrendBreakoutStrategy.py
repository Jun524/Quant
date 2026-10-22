"""Conservative, explainable trend-following baseline for Binance spot.

This is a research baseline, not an assertion of future profitability.  Every
rolling channel is shifted by one candle so the current candle never helps set
the level it is attempting to break.
"""

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.strategy import IStrategy


class TrendBreakoutStrategy(IStrategy):
    INTERFACE_VERSION = 3

    can_short = False
    timeframe = "1h"
    # EMA200 still varied by ~1% with only 250 warm-up candles in recursive
    # analysis. 800 reduces the observed initialization error to near zero.
    startup_candle_count = 800
    process_only_new_candles = True

    # Let the trend/channel exit do most of the work while keeping hard limits.
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
        "stoploss": "limit",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_on_exchange_limit_ratio": 0.99,
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
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 168,
                "trade_limit": 10,
                "stop_duration_candles": 48,
                "max_allowed_drawdown": 0.12,
            },
        ]

    plot_config = {
        "main_plot": {
            "ema_200": {"color": "orange"},
            "donchian_high_50": {"color": "green"},
            "donchian_low_20": {"color": "red"},
        },
        "subplots": {
            "ADX": {"adx": {"color": "blue"}},
            "ATR %": {"atr_pct": {"color": "purple"}},
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["volume_mean_20"] = dataframe["volume"].rolling(20).mean()

        # shift(1) is essential: channels contain only fully closed prior bars.
        dataframe["donchian_high_50"] = (
            dataframe["high"].rolling(50).max().shift(1)
        )
        dataframe["donchian_low_20"] = (
            dataframe["low"].rolling(20).min().shift(1)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        breakout = (
            (dataframe["close"] > dataframe["donchian_high_50"])
            & (dataframe["close"].shift(1) <= dataframe["donchian_high_50"].shift(1))
        )

        dataframe.loc[
            breakout
            & (dataframe["close"] > dataframe["ema_200"])
            & (dataframe["ema_200"] > dataframe["ema_200"].shift(12))
            & (dataframe["adx"] > 20)
            & dataframe["rsi"].between(50, 78)
            & dataframe["atr_pct"].between(0.005, 0.08)
            & (dataframe["volume"] > dataframe["volume_mean_20"])
            & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "donchian50_trend")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        channel_break = (
            (dataframe["close"] < dataframe["donchian_low_20"])
            & (dataframe["close"].shift(1) >= dataframe["donchian_low_20"].shift(1))
        )
        trend_failure = (
            (dataframe["close"] < dataframe["ema_200"])
            & (dataframe["close"].shift(1) >= dataframe["ema_200"].shift(1))
        )

        dataframe.loc[
            (channel_break | trend_failure) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "channel_or_trend_failure")
        return dataframe
