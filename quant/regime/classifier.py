"""Lookahead-safe rule-based BTC market regime classifier."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Regime(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"
    RANGE = "RANGE"
    PANIC = "PANIC"
    TRANSITION = "TRANSITION"


@dataclass(frozen=True)
class RegimeConfig:
    ema_fast: int = 50
    ema_slow: int = 200
    slope_lookback: int = 20
    adx_period: int = 14
    # Pre-registered v1 RANGE definition: low trend strength and no more than
    # 0.5% cumulative 20-day EMA200 movement. It is deliberately not tuned
    # against mean-reversion returns; it supplies a usable regime sample.
    range_adx_max: float = 25.0
    range_abs_slope_max: float = 0.005
    panic_vol_window: int = 20
    panic_reference_window: int = 180
    panic_vol_quantile: float = 0.90
    panic_daily_drop: float = -0.06
    bull_requires_positive_slope: bool = True

    @classmethod
    def alpha1_compatible(cls) -> "RegimeConfig":
        """Match Alpha #1's historic BTC price/50/200 filter exactly."""
        return cls(bull_requires_positive_slope=False)


def _ema(close: pd.Series, period: int) -> pd.Series:
    """Use TA-Lib when available so Freqtrade strategies retain their history."""
    try:
        import talib.abstract as ta

        return ta.EMA(close, timeperiod=period)
    except ImportError:
        return close.ewm(span=period, adjust=False, min_periods=period).mean()


def _adx(dataframe: pd.DataFrame, period: int) -> pd.Series:
    try:
        import talib.abstract as ta

        return ta.ADX(dataframe, timeperiod=period)
    except ImportError:
        high, low, close = dataframe["high"], dataframe["low"], dataframe["close"]
        up_move, down_move = high.diff(), -low.diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        true_range = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = true_range.rolling(period, min_periods=period).mean()
        plus_di = 100 * plus_dm.rolling(period, min_periods=period).mean() / atr
        minus_di = 100 * minus_dm.rolling(period, min_periods=period).mean() / atr
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        return dx.rolling(period, min_periods=period).mean()


class RegimeClassifier:
    def __init__(self, config: RegimeConfig = RegimeConfig()) -> None:
        self.config = config

    def annotate(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Add indicators and one regime label for each completed candle.

        Every rolling statistic is trailing. The panic volatility threshold is
        explicitly shifted one candle, so it cannot use the current or future
        distribution when making a current-candle classification.
        """
        required = {"close", "high", "low"}
        if missing := required - set(dataframe.columns):
            raise ValueError(f"regime data is missing columns: {sorted(missing)}")
        result = dataframe.copy()
        cfg = self.config
        result["ema_50"] = _ema(result["close"], cfg.ema_fast)
        result["ema_200"] = _ema(result["close"], cfg.ema_slow)
        result["ema_200_slope"] = result["ema_200"] / result["ema_200"].shift(cfg.slope_lookback) - 1
        result["adx"] = _adx(result, cfg.adx_period)
        returns = result["close"].pct_change()
        result["realized_volatility"] = returns.rolling(cfg.panic_vol_window, min_periods=cfg.panic_vol_window).std()
        result["panic_vol_threshold"] = (
            result["realized_volatility"]
            .rolling(cfg.panic_reference_window, min_periods=cfg.panic_reference_window)
            .quantile(cfg.panic_vol_quantile)
            .shift(1)
        )
        result["realized_volatility_percentile"] = result["realized_volatility"].rolling(
            cfg.panic_reference_window, min_periods=cfg.panic_reference_window
        ).apply(lambda values: float((values <= values.iloc[-1]).mean()), raw=False)

        bull = (result["close"] > result["ema_200"]) & (result["ema_50"] > result["ema_200"])
        if cfg.bull_requires_positive_slope:
            bull &= result["ema_200_slope"] > 0
        bear = (result["close"] < result["ema_200"]) & (result["ema_50"] < result["ema_200"])
        panic = (result["realized_volatility"] > result["panic_vol_threshold"]) & (returns < cfg.panic_daily_drop)
        ranging = (result["adx"] < cfg.range_adx_max) & (result["ema_200_slope"].abs() <= cfg.range_abs_slope_max)

        result["regime"] = Regime.TRANSITION.value
        result.loc[ranging, "regime"] = Regime.RANGE.value
        result.loc[bear, "regime"] = Regime.BEAR.value
        result.loc[bull, "regime"] = Regime.BULL.value
        result.loc[panic, "regime"] = Regime.PANIC.value
        return result

    def evidence_at(self, annotated: pd.DataFrame, index: int = -1) -> dict[str, object]:
        """Return the explicit facts behind an already-computed classification."""
        row = annotated.iloc[index]
        return {
            "regime": row["regime"],
            "btc_above_ema200": bool(row["close"] > row["ema_200"]),
            "ema50_above_ema200": bool(row["ema_50"] > row["ema_200"]),
            "ema200_slope": None if pd.isna(row["ema_200_slope"]) else float(row["ema_200_slope"]),
            "adx": None if pd.isna(row["adx"]) else float(row["adx"]),
            "realized_volatility_percentile": (
                None if pd.isna(row["realized_volatility_percentile"])
                else float(row["realized_volatility_percentile"])
            ),
        }
