"""Research-only translation of repeatable patterns in the AOA trade archive.

This is not a reconstruction of the trader's private decision process.  The
archive only exposes fills, so this prototype tests one deliberately simple
interpretation: trade a five-minute resumption after a short pullback when the
completed one-hour market context agrees over 24 and 168 hours.

The source archive is development data.  This strategy must therefore remain
outside the experiment registry and live configuration until it has accumulated
genuinely unseen forward data.
"""

from datetime import datetime, timedelta
from pathlib import Path
import sys

from pandas import DataFrame
import talib.abstract as ta

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.regime.classifier import Regime, RegimeClassifier


class AoaBehaviorPrototype(IStrategy):
    """Five-minute pullback/resumption inside a medium-horizon BTC trend.

    Frozen prototype rules:
    - BTC/USDT only, isolated futures, one-times leverage;
    - completed 1h returns over 24h and 168h must agree with price vs EMA200;
    - the last three completed 5m candles must pull against that direction;
    - the current 5m candle must close through the previous candle's extreme;
    - the last completed 1h volume must be at least its prior 24h median;
    - daily PANIC rejects entries;
    - EMA12 momentum failure, context invalidation, PANIC, 24h age, or a fixed
      3% emergency stop exits the trade.

    The stop and 0.25% account-risk budget are safety overlays.  They are not
    estimates of the source trader's leverage or risk appetite.
    """

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    startup_candle_count = 300
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.03
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    pullback_bars = 3
    exit_ema_bars = 12
    max_hold_hours = 24
    account_risk_fraction = 0.0025

    # Never average down in this prototype.  The archive contains adverse
    # additions and forced liquidations, which are observations to avoid rather
    # than behavior to reproduce.
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
            {"method": "CooldownPeriod", "stop_duration_candles": 3},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 288,
                "trade_limit": 3,
                "stop_duration_candles": 144,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 2016,
                "trade_limit": 20,
                "stop_duration_candles": 288,
                "max_allowed_drawdown": 0.08,
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

    @informative("1h", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["return_24h"] = dataframe["close"].pct_change(24)
        dataframe["return_168h"] = dataframe["close"].pct_change(168)
        dataframe["ema_200"] = ta.EMA(dataframe, timeperiod=200)
        # Shift the reference distribution so the completed hour being tested
        # cannot lower its own volume threshold.
        dataframe["prior_volume_median_24h"] = dataframe["volume"].rolling(24).median().shift(1)
        dataframe["volume_ratio_24h"] = dataframe["volume"] / dataframe["prior_volume_median_24h"]
        return dataframe

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return RegimeClassifier().annotate(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_ema"] = ta.EMA(dataframe, timeperiod=self.exit_ema_bars)
        dataframe["pullback_return"] = dataframe["close"].pct_change(self.pullback_bars)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        liquid_context = dataframe["btc_volume_ratio_24h_1h"] >= 1.0
        permitted_regime = dataframe["btc_regime_1d"].isin(
            [Regime.BULL.value, Regime.BEAR.value, Regime.RANGE.value, Regime.TRANSITION.value]
        )

        long_context = (
            (dataframe["btc_return_24h_1h"] > 0)
            & (dataframe["btc_return_168h_1h"] > 0)
            & (dataframe["btc_close_1h"] > dataframe["btc_ema_200_1h"])
        )
        short_context = (
            (dataframe["btc_return_24h_1h"] < 0)
            & (dataframe["btc_return_168h_1h"] < 0)
            & (dataframe["btc_close_1h"] < dataframe["btc_ema_200_1h"])
        )

        long_pullback = dataframe["pullback_return"].shift(1) < 0
        short_pullback = dataframe["pullback_return"].shift(1) > 0
        long_resumption = dataframe["close"] > dataframe["high"].shift(1)
        short_resumption = dataframe["close"] < dataframe["low"].shift(1)
        tradable = liquid_context & permitted_regime & (dataframe["volume"] > 0)

        for regime in (Regime.BULL, Regime.BEAR, Regime.RANGE, Regime.TRANSITION):
            regime_at_entry = dataframe["btc_regime_1d"] == regime.value
            dataframe.loc[
                tradable & regime_at_entry & long_context & long_pullback & long_resumption,
                ["enter_long", "enter_tag"],
            ] = (1, f"aoa_proto_long_pullback_{regime.value.lower()}")
            dataframe.loc[
                tradable & regime_at_entry & short_context & short_pullback & short_resumption,
                ["enter_short", "enter_tag"],
            ] = (1, f"aoa_proto_short_pullback_{regime.value.lower()}")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        panic = dataframe["btc_regime_1d"] == Regime.PANIC.value
        long_context_invalid = (
            (dataframe["btc_return_24h_1h"] <= 0)
            | (dataframe["btc_close_1h"] <= dataframe["btc_ema_200_1h"])
        )
        short_context_invalid = (
            (dataframe["btc_return_24h_1h"] >= 0)
            | (dataframe["btc_close_1h"] >= dataframe["btc_ema_200_1h"])
        )
        long_momentum_failed = (
            (dataframe["close"] < dataframe["exit_ema"])
            & (dataframe["close"].shift(1) >= dataframe["exit_ema"].shift(1))
        )
        short_momentum_failed = (
            (dataframe["close"] > dataframe["exit_ema"])
            & (dataframe["close"].shift(1) <= dataframe["exit_ema"].shift(1))
        )
        valid_candle = dataframe["volume"] > 0
        dataframe.loc[
            valid_candle & (panic | long_context_invalid | long_momentum_failed),
            ["exit_long", "exit_tag"],
        ] = (1, "aoa_proto_context_or_momentum_exit")
        dataframe.loc[
            valid_candle & (panic | short_context_invalid | short_momentum_failed),
            ["exit_short", "exit_tag"],
        ] = (1, "aoa_proto_context_or_momentum_exit")
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
            return "aoa_proto_max_hold"
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
