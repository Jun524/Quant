"""Pre-registered weekly cross-sectional momentum swing candidate."""

from datetime import datetime, timedelta

from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative


ASSETS = ("btc", "eth", "bnb", "sol", "xrp")


def _daily_features(dataframe: DataFrame) -> DataFrame:
    dataframe["return_30d"] = dataframe["close"].pct_change(30)
    dataframe["sma_100d"] = dataframe["close"].rolling(100, min_periods=100).mean()
    dataframe["above_sma_100d"] = dataframe["close"] > dataframe["sma_100d"]
    return dataframe


class ALPHA_006_CROSS_SECTIONAL_SWING(IStrategy):
    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "4h"
    startup_candle_count = 600
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.08
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    max_hold_days = 7
    account_risk_fraction = 0.004
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
            {"method": "CooldownPeriod", "stop_duration_candles": 6},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 180,
                "trade_limit": 3,
                "stop_duration_candles": 42,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 1080,
                "trade_limit": 20,
                "stop_duration_candles": 84,
                "max_allowed_drawdown": 0.12,
            },
        ]

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(1.0, max_leverage)

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return _daily_features(dataframe)

    @informative("1d", "ETH/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_eth_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return _daily_features(dataframe)

    @informative("1d", "BNB/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_bnb_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return _daily_features(dataframe)

    @informative("1d", "SOL/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_sol_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return _daily_features(dataframe)

    @informative("1d", "XRP/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_xrp_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return _daily_features(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return_columns = [f"{asset}_return_30d_1d" for asset in ASSETS]
        ranks = dataframe[return_columns].rank(axis=1, method="min")
        base = metadata["pair"].split("/")[0].lower()
        dataframe["relative_rank"] = ranks[f"{base}_return_30d_1d"]
        dataframe["asset_return_30d"] = dataframe[f"{base}_return_30d_1d"]
        asset_close = dataframe[f"{base}_close_1d"]
        asset_sma = dataframe[f"{base}_sma_100d_1d"]
        # Informative booleans can be widened to float after the as-of merge.
        # Compare the numeric series directly so missing history cannot become a
        # false short signal and boolean negation remains well-defined.
        dataframe["asset_above_sma100"] = asset_close.notna() & asset_sma.notna() & (asset_close > asset_sma)
        dataframe["asset_below_sma100"] = asset_close.notna() & asset_sma.notna() & (asset_close < asset_sma)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        weekly_rebalance = (dataframe["date"].dt.weekday == 0) & (dataframe["date"].dt.hour == 0)
        liquid = dataframe["volume"] > 0
        long_signal = weekly_rebalance & liquid & (dataframe["relative_rank"] == 5) & (dataframe["asset_return_30d"] > 0) & dataframe["asset_above_sma100"]
        short_signal = weekly_rebalance & liquid & (dataframe["relative_rank"] == 1) & (dataframe["asset_return_30d"] < 0) & dataframe["asset_below_sma100"]
        dataframe.loc[long_signal, ["enter_long", "enter_tag"]] = (1, "cross_sectional_winner_long")
        dataframe.loc[short_signal, ["enter_short", "enter_tag"]] = (1, "cross_sectional_loser_short")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, **kwargs) -> str | None:
        if current_time - trade.open_date_utc >= timedelta(days=self.max_hold_days):
            return "cross_sectional_weekly_exit"
        return None

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        equity = self.wallets.get_total_stake_amount()
        if equity <= 0:
            return 0.0
        stake = equity * self.account_risk_fraction / abs(self.stoploss)
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)
