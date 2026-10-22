"""Low-turnover Binance USDT-M futures research candidates.

These candidates test one economic hypothesis: slower breakouts should reduce
1-hour whipsaws and fee drag.  They deliberately use 1x leverage while the
signal is being validated.  They are research strategies, not live approval.
"""

from datetime import datetime

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, stoploss_from_absolute


class FuturesSlowTrendBase(IStrategy):
    INTERFACE_VERSION = 3

    can_short = True
    timeframe = "4h"
    startup_candle_count = 800
    process_only_new_candles = True

    # Disable profit taking: a trend system needs rare large winners.  The hard
    # stop is only a catastrophe cap; the ATR stop and exit channel act first.
    minimal_roi = {"0": 10.0}
    stoploss = -0.20
    use_custom_stoploss = True
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    entry_window = 120
    exit_window = 60
    atr_multiple = 3.0
    allow_long = True
    allow_short = True

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
                "stop_duration_candles": 42,
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
        # Establish a cost-after-fees edge before considering leverage.
        return min(1.0, max_leverage)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        dataframe["adx"] = ta.ADX(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]

        # Shift by one candle so today's close never defines its own breakout.
        dataframe["entry_high"] = (
            dataframe["high"].rolling(self.entry_window).max().shift(1)
        )
        dataframe["entry_low"] = (
            dataframe["low"].rolling(self.entry_window).min().shift(1)
        )
        dataframe["exit_high"] = (
            dataframe["high"].rolling(self.exit_window).max().shift(1)
        )
        dataframe["exit_low"] = (
            dataframe["low"].rolling(self.exit_window).min().shift(1)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        tradeable = (
            dataframe["atr_pct"].between(0.006, 0.10)
            & (dataframe["adx"] > 18)
            & (dataframe["volume"] > 0)
        )
        long_breakout = (
            (dataframe["close"] > dataframe["entry_high"])
            & (dataframe["close"].shift(1) <= dataframe["entry_high"].shift(1))
            & (dataframe["close"] > dataframe["ema_200"])
            & (dataframe["ema_200"] > dataframe["ema_200"].shift(30))
        )
        short_breakout = (
            (dataframe["close"] < dataframe["entry_low"])
            & (dataframe["close"].shift(1) >= dataframe["entry_low"].shift(1))
            & (dataframe["close"] < dataframe["ema_200"])
            & (dataframe["ema_200"] < dataframe["ema_200"].shift(30))
        )

        if self.allow_long:
            dataframe.loc[
                long_breakout & tradeable, ["enter_long", "enter_tag"]
            ] = (1, f"long_breakout_{self.entry_window}")
        if self.allow_short:
            dataframe.loc[
                short_breakout & tradeable, ["enter_short", "enter_tag"]
            ] = (1, f"short_breakout_{self.entry_window}")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        long_exit = (
            (dataframe["close"] < dataframe["exit_low"])
            & (dataframe["close"].shift(1) >= dataframe["exit_low"].shift(1))
        )
        short_exit = (
            (dataframe["close"] > dataframe["exit_high"])
            & (dataframe["close"].shift(1) <= dataframe["exit_high"].shift(1))
        )

        dataframe.loc[
            long_exit & (dataframe["volume"] > 0), ["exit_long", "exit_tag"]
        ] = (1, f"long_channel_{self.exit_window}")
        dataframe.loc[
            short_exit & (dataframe["volume"] > 0), ["exit_short", "exit_tag"]
        ] = (1, f"short_channel_{self.exit_window}")
        return dataframe

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return None

        atr = dataframe.iloc[-1]["atr"]
        if atr is None or atr <= 0:
            return None

        stop_rate = (
            current_rate + self.atr_multiple * atr
            if trade.is_short
            else current_rate - self.atr_multiple * atr
        )
        return stoploss_from_absolute(
            stop_rate,
            current_rate=current_rate,
            is_short=trade.is_short,
            leverage=trade.leverage,
        )


class FuturesSlowTrend120(FuturesSlowTrendBase):
    """Roughly 20-day entry and 10-day exit on 4-hour candles."""

    entry_window = 120
    exit_window = 60


class FuturesSlowTrend240(FuturesSlowTrendBase):
    """Roughly 40-day entry and 20-day exit on 4-hour candles."""

    entry_window = 240
    exit_window = 120


class FuturesSlowTrend240Channel(FuturesSlowTrend240):
    """40/20-day breakout using its exit channel instead of an ATR trail."""

    use_custom_stoploss = False
    stoploss = -0.12


class FuturesSlowTrend240Long(FuturesSlowTrend240Channel):
    """Long-only variant for crypto's asymmetric long-run return distribution."""

    can_short = False
    allow_short = False
