"""Pre-registered Alpha #4: one volatility-compression breakout hypothesis.

This research strategy does not inherit Alpha #1/#3 and does not use a BULL or
BEAR entry filter.  It tests whether a quiet, compressed one-hour range that
breaks with volume can continue in either direction.  PANIC is the sole regime
entry exclusion; the completed BTC regime is retained in each entry tag.
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


class ALPHA_004_VOL_BREAKOUT(IStrategy):
    """Trade first 24-hour range exits following a 20th-percentile squeeze.

    Fixed Experiment 0004 specification:
    - 1h signal candles and a separately invoked 5m detail backtest;
    - ATR(14)/price and 24h range/price are each below their trailing 120h
      20th percentile (calculated only from completed prior candles);
    - prior 24h volume is non-zero and breakout volume is >= 1.5x its prior
      24h average; first 24h Donchian high/low break enters long/short;
    - PANIC entries are rejected, while BULL/BEAR/RANGE/TRANSITION are tagged;
    - a return inside the current pre-break range, PANIC, 48h age, or -4% stop
      exits.  No parameter in this module is optimized during Experiment 0004.
    """

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "1h"
    # The local 120h squeeze window is shorter, but the common informative BTC
    # regime carries a 200-day EMA.  1,500 one-hour startup candles keeps the
    # classifier recursively stable; this is data warmup, not an Alpha input.
    startup_candle_count = 1500
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.04
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    atr_window = 14
    compression_window = 24
    reference_window = 120
    compression_quantile = 0.20
    volume_window = 24
    volume_multiplier = 1.5
    max_hold_hours = 48
    account_risk_fraction = 0.003

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
                "lookback_period_candles": 72,
                "trade_limit": 3,
                "stop_duration_candles": 24,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 720,
                "trade_limit": 10,
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

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return RegimeClassifier().annotate(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=self.atr_window)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"]
        dataframe["range_high"] = dataframe["high"].rolling(self.compression_window).max().shift(1)
        dataframe["range_low"] = dataframe["low"].rolling(self.compression_window).min().shift(1)
        dataframe["range_pct"] = (dataframe["range_high"] - dataframe["range_low"]) / dataframe["close"]
        # Thresholds are shifted so current volatility/range cannot qualify its
        # own breakout candle as a past compression observation.
        dataframe["atr_squeeze_threshold"] = (
            dataframe["atr_pct"].rolling(self.reference_window).quantile(self.compression_quantile).shift(1)
        )
        dataframe["range_squeeze_threshold"] = (
            dataframe["range_pct"].rolling(self.reference_window).quantile(self.compression_quantile).shift(1)
        )
        dataframe["prior_volume_mean"] = dataframe["volume"].rolling(self.volume_window).mean().shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        squeezed = (
            (dataframe["atr_pct"] < dataframe["atr_squeeze_threshold"])
            & (dataframe["range_pct"] < dataframe["range_squeeze_threshold"])
        )
        liquid = (dataframe["prior_volume_mean"] > 0) & (dataframe["volume"] >= dataframe["prior_volume_mean"] * self.volume_multiplier)
        permitted_regime = dataframe["btc_regime_1d"].isin(
            [Regime.BULL.value, Regime.BEAR.value, Regime.RANGE.value, Regime.TRANSITION.value]
        )
        long_break = (
            (dataframe["close"] > dataframe["range_high"])
            & (dataframe["close"].shift(1) <= dataframe["range_high"].shift(1))
        )
        short_break = (
            (dataframe["close"] < dataframe["range_low"])
            & (dataframe["close"].shift(1) >= dataframe["range_low"].shift(1))
        )
        base = squeezed & liquid & permitted_regime & (dataframe["volume"] > 0)
        for regime in (Regime.BULL, Regime.BEAR, Regime.RANGE, Regime.TRANSITION):
            label = dataframe["btc_regime_1d"] == regime.value
            dataframe.loc[base & long_break & label, ["enter_long", "enter_tag"]] = (1, f"volbo_long_{regime.value.lower()}")
            dataframe.loc[base & short_break & label, ["enter_short", "enter_tag"]] = (1, f"volbo_short_{regime.value.lower()}")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        panic = dataframe["btc_regime_1d"] == Regime.PANIC.value
        long_failure = dataframe["close"] < dataframe["range_high"]
        short_failure = dataframe["close"] > dataframe["range_low"]
        dataframe.loc[long_failure & (dataframe["volume"] > 0), ["exit_long", "exit_tag"]] = (1, "volbo_return_to_range")
        dataframe.loc[short_failure & (dataframe["volume"] > 0), ["exit_short", "exit_tag"]] = (1, "volbo_return_to_range")
        dataframe.loc[panic & (dataframe["volume"] > 0), ["exit_long", "exit_tag"]] = (1, "volbo_panic_exit")
        dataframe.loc[panic & (dataframe["volume"] > 0), ["exit_short", "exit_tag"]] = (1, "volbo_panic_exit")
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
            return "volbo_max_hold"
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
