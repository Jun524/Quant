"""Pre-registered research strategy for extreme perpetual-funding reversal."""

from datetime import datetime, timedelta

from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative


class ALPHA_005_FUNDING_EXTREME_REVERSAL(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "1h"
    startup_candle_count = 200
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.02
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    max_hold_hours = 8
    account_risk_fraction = 0.0025
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

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
                "lookback_period_candles": 168,
                "trade_limit": 3,
                "stop_duration_candles": 24,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 720,
                "trade_limit": 20,
                "stop_duration_candles": 72,
                "max_allowed_drawdown": 0.10,
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
        return min(1.0, max_leverage)

    @informative("1h", candle_type="funding_rate", ffill=False)
    def populate_indicators_funding_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rate = dataframe["funding_rate"]
        dataframe["funding_p05"] = rate.rolling(90, min_periods=60).quantile(0.05).shift(1)
        dataframe["funding_p95"] = rate.rolling(90, min_periods=60).quantile(0.95).shift(1)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rate = dataframe["funding_rate_1h"]
        event = rate.notna() & dataframe["funding_p05_1h"].notna() & dataframe["funding_p95_1h"].notna()
        liquid = dataframe["volume"] > 0
        long_signal = event & liquid & (rate < 0) & (rate <= dataframe["funding_p05_1h"])
        short_signal = event & liquid & (rate > 0) & (rate >= dataframe["funding_p95_1h"])
        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, "funding_negative_extreme_long")
        dataframe.loc[short_signal, ["enter_short", "enter_tag"]] = (1, "funding_positive_extreme_short")
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
        if current_time - trade.open_date_utc >= timedelta(hours=self.max_hold_hours):
            return "funding_reversal_time_exit"
        return None

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
        equity = self.wallets.get_total_stake_amount()
        if equity <= 0:
            return 0.0
        stake = equity * self.account_risk_fraction / abs(self.stoploss)
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)
