"""Pre-registered RANGE-only mean-reversion research candidates.

These strategies are intentionally separate from the dry-run default. They
use the common Regime Engine and have exactly two fixed hypotheses; no search
over RSI or deviation thresholds is performed here.
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


class MeanReversionBase(IStrategy):
    INTERFACE_VERSION = 3
    can_short = False
    timeframe = "1h"
    # Binance limits the number of startup 1h candles Freqtrade may request.
    # The informative daily BTC feed independently warms its 200-day EMA; early
    # rows without a completed regime are rejected rather than backfilled.
    startup_candle_count = 1200
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.04
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    deviation_threshold = 1.5
    return_threshold = -0.03
    rsi_threshold = 32
    max_hold_hours = 24
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
            {"method": "CooldownPeriod", "stop_duration_candles": 4},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 72,
                "trade_limit": 2,
                "stop_duration_candles": 24,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 720,
                "trade_limit": 10,
                "stop_duration_candles": 72,
                "max_allowed_drawdown": 0.08,
            },
        ]

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float, max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(1.0, max_leverage)

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return RegimeClassifier().annotate(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["center"] = ta.EMA(dataframe, timeperiod=24)
        dataframe["deviation_std"] = dataframe["close"].rolling(48).std()
        dataframe["deviation_z"] = (dataframe["close"] - dataframe["center"]) / dataframe["deviation_std"]
        dataframe["return_6h"] = dataframe["close"].pct_change(6)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = (
            (dataframe["btc_regime_1d"] == Regime.RANGE.value)
            & (dataframe["deviation_z"] <= -self.deviation_threshold)
            & (dataframe["return_6h"] <= self.return_threshold)
            & (dataframe["rsi"] <= self.rsi_threshold)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[conditions, ["enter_long", "enter_tag"]] = (1, self.entry_tag)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        center_recovered = dataframe["close"] >= dataframe["center"]
        adverse_regime = dataframe["btc_regime_1d"].isin([Regime.BEAR.value, Regime.PANIC.value])
        dataframe.loc[(center_recovered | adverse_regime) & (dataframe["volume"] > 0), ["exit_long", "exit_tag"]] = (
            1,
            "mean_reversion_exit",
        )
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float, current_profit: float, **kwargs) -> str | None:
        if current_time - trade.open_date_utc >= timedelta(hours=self.max_hold_hours):
            return "mean_reversion_time_exit"
        return None

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float, proposed_stake: float, min_stake: float | None, max_stake: float, leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        equity = self.wallets.get_total_stake_amount()
        if equity <= 0:
            return 0.0
        stake = equity * self.account_risk_fraction / abs(self.stoploss)
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)


class ALPHA_002_MEAN_REVERSION_A(MeanReversionBase):
    """MR-A: 1.5σ deviation, -3% six-hour return, 24-hour maximum hold."""

    entry_tag = "mr_a_range_oversold"


class ALPHA_002_MEAN_REVERSION_B(MeanReversionBase):
    """MR-B: pre-registered stricter alternative, not yet evaluated."""

    deviation_threshold = 2.0
    return_threshold = -0.05
    rsi_threshold = 27
    max_hold_hours = 48
    entry_tag = "mr_b_range_deep_oversold"
