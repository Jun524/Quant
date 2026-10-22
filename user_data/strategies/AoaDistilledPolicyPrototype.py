"""Research-only chronological distillation of the AOA execution archive.

The linear policy was fitted on 2018-2019, its action threshold was fixed on
2020, and its timing/direction metrics were evaluated once on 2021.  The model
file deliberately excludes the failed outcome-quality filter.
"""

from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sys

import numpy as np
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.regime.classifier import Regime, RegimeClassifier

MODEL_PATH = PROJECT_ROOT / "research" / "aoa-behavior-model-spec.json"


def _load_model_spec() -> dict:
    if not MODEL_PATH.exists():
        raise FileNotFoundError(
            f"missing {MODEL_PATH}; run scripts/analyze_aoa_behavior.py before loading this strategy"
        )
    return json.loads(MODEL_PATH.read_text(encoding="utf-8"))


MODEL_SPEC = _load_model_spec()


def _linear_probability(dataframe: DataFrame, spec: dict) -> np.ndarray:
    matrix = dataframe[spec["features"]].to_numpy(dtype=float)
    mean = np.asarray(spec["mean"], dtype=float)
    scale = np.asarray(spec["scale"], dtype=float)
    coefficient = np.asarray(spec["coefficient"], dtype=float)
    score = ((matrix - mean) / scale) @ coefficient + float(spec["intercept"])
    score = np.clip(score, -40.0, 40.0)
    probability = 1.0 / (1.0 + np.exp(-score))
    # A truncated replay can differ at ~1e-16 because BLAS reductions are not
    # bitwise stable, and NaN != NaN makes lookahead-analysis report an
    # indicator-only false positive.  Missing features are explicitly
    # non-tradable and model output is stored at more precision than any fixed
    # threshold needs.
    return np.where(np.isfinite(probability), np.round(probability, 12), -1.0)


class AoaDistilledPolicyPrototype(IStrategy):
    """Use the locked teacher timing/direction models as an hourly policy.

    This tests transfer to Binance; it does not claim the source trader's
    profitability.  Entry is evaluated once per hour on completed data.  Exit
    occurs when the learned action state disappears, direction flips, PANIC is
    detected, the 24-hour safety horizon expires, or the 3% emergency stop is
    reached.  There is no averaging down or position adjustment.
    """

    INTERFACE_VERSION = 3
    can_short = True
    timeframe = "5m"
    # The 200-period EWM features converge only around the 1200-2400 candle
    # range.  2400 is the largest tested value within Freqtrade's recommended
    # live startup limit and produced zero displayed recursive variance.
    startup_candle_count = 2400
    process_only_new_candles = True

    minimal_roi = {"0": 10.0}
    stoploss = -0.03
    use_exit_signal = True
    exit_profit_only = False
    trailing_stop = False

    max_hold_hours = 24
    account_risk_fraction = 0.0025
    position_adjustment_enable = False
    max_entry_position_adjustment = 0

    action_threshold = float(MODEL_SPEC["action"]["threshold"])
    direction_threshold = float(MODEL_SPEC["direction"]["threshold"])
    minimum_direction_confidence = float(MODEL_SPEC["direction"]["minimum_confidence"])

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
            {"method": "CooldownPeriod", "stop_duration_candles": 12},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 576,
                "trade_limit": 3,
                "stop_duration_candles": 288,
                "only_per_pair": False,
                "only_per_side": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 4032,
                "trade_limit": 20,
                "stop_duration_candles": 576,
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
        hourly_return = dataframe["close"].pct_change()
        for hours in (1, 4, 12, 24, 72, 168):
            dataframe[f"return_{hours}h"] = dataframe["close"].pct_change(hours)
        for hours in (6, 24, 72):
            dataframe[f"volatility_{hours}h"] = hourly_return.rolling(hours, min_periods=hours).std()
        for hours in (24, 72, 200):
            ema = dataframe["close"].ewm(span=hours, adjust=False, min_periods=hours).mean()
            dataframe[f"ema_{hours}"] = ema
            dataframe[f"distance_ema{hours}"] = dataframe["close"] / ema - 1.0
        dataframe["ema24_slope_24h"] = dataframe["ema_24"].pct_change(24)
        dataframe["ema200_slope_24h"] = dataframe["ema_200"].pct_change(24)
        range_high = dataframe["high"].rolling(24, min_periods=24).max()
        range_low = dataframe["low"].rolling(24, min_periods=24).min()
        width = (range_high - range_low).replace(0, np.nan)
        dataframe["range_position_24h"] = (dataframe["close"] - range_low) / width
        dataframe["range_width_24h"] = width / dataframe["close"]
        prior_volume_median = dataframe["volume"].rolling(24, min_periods=24).median().shift(1)
        dataframe["volume_ratio_24h"] = dataframe["volume"] / prior_volume_median.replace(0, np.nan)
        dataframe["candle_body"] = (dataframe["close"] - dataframe["open"]) / dataframe["open"]
        dataframe["candle_range"] = (dataframe["high"] - dataframe["low"]) / dataframe["open"]
        angle = 2.0 * math.pi * dataframe["date"].dt.hour / 24.0
        dataframe["hour_sin"] = np.sin(angle)
        dataframe["hour_cos"] = np.cos(angle)
        dataframe["action_probability"] = _linear_probability(dataframe, MODEL_SPEC["action"])
        dataframe["direction_probability"] = _linear_probability(dataframe, MODEL_SPEC["direction"])
        return dataframe

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return RegimeClassifier().annotate(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        first_decision_candle = dataframe["date"].dt.minute == 0
        action = dataframe["btc_action_probability_1h"] >= self.action_threshold
        permitted = dataframe["btc_regime_1d"].isin(
            [Regime.BULL.value, Regime.BEAR.value, Regime.RANGE.value, Regime.TRANSITION.value]
        )
        valid = first_decision_candle & action & permitted & (dataframe["volume"] > 0)
        direction_probability = dataframe["btc_direction_probability_1h"]
        direction_confident = (direction_probability - self.direction_threshold).abs() >= self.minimum_direction_confidence
        predicted_long = direction_probability >= self.direction_threshold
        valid &= direction_confident

        for regime in (Regime.BULL, Regime.BEAR, Regime.RANGE, Regime.TRANSITION):
            regime_at_entry = dataframe["btc_regime_1d"] == regime.value
            dataframe.loc[
                valid & regime_at_entry & predicted_long,
                ["enter_long", "enter_tag"],
            ] = (1, f"aoa_distilled_long_{regime.value.lower()}")
            dataframe.loc[
                valid & regime_at_entry & ~predicted_long,
                ["enter_short", "enter_tag"],
            ] = (1, f"aoa_distilled_short_{regime.value.lower()}")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        action_gone = dataframe["btc_action_probability_1h"] < self.action_threshold
        direction_probability = dataframe["btc_direction_probability_1h"]
        direction_confident = (direction_probability - self.direction_threshold).abs() >= self.minimum_direction_confidence
        predicted_long = direction_probability >= self.direction_threshold
        panic = dataframe["btc_regime_1d"] == Regime.PANIC.value
        valid = (dataframe["volume"] > 0) & (action_gone | panic)
        dataframe.loc[
            (valid | (direction_confident & ~predicted_long)) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "aoa_distilled_state_exit")
        dataframe.loc[
            (valid | (direction_confident & predicted_long)) & (dataframe["volume"] > 0),
            ["exit_short", "exit_tag"],
        ] = (1, "aoa_distilled_state_exit")
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
            return "aoa_distilled_max_hold"
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
