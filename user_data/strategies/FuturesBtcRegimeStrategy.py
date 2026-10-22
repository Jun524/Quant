"""BTC-regime variants of the low-turnover long futures candidate.

These classes test a single, pre-defined hypothesis: when Bitcoin is below its
own long-term trend, altcoin breakouts have less favourable long-side odds.
They are deliberately separate from the default dry-run strategy until they
pass the locked validation protocol.
"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import informative

# Freqtrade imports strategies from user_data/strategies, whereas the shared
# research package lives at the repository root. Keep this explicit so the
# same code works from the local virtualenv and the mounted Docker workspace.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from quant.regime.classifier import Regime, RegimeClassifier, RegimeConfig

from FuturesSlowTrendStrategy import FuturesSlowTrend240Long


class FuturesBtcRegimeBase(FuturesSlowTrend240Long):
    startup_candle_count = 1500
    # Alpha #1 retains its historic price/50/200 definition while receiving
    # the label from the shared engine. Other Alphas use the stricter default.
    regime_config = RegimeConfig.alpha1_compatible()

    @informative("1d", "BTC/{stake}:{stake}", fmt="{base}_{column}_{timeframe}")
    def populate_indicators_btc_1d(
        self, dataframe: DataFrame, metadata: dict
    ) -> DataFrame:
        return RegimeClassifier(self.regime_config).annotate(dataframe)

    def btc_regime(self, dataframe: DataFrame) -> DataFrame:
        """Return a boolean series in subclasses; all values are completed daily data."""
        raise NotImplementedError

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = super().populate_entry_trend(dataframe, metadata)
        regime = self.btc_regime(dataframe).fillna(False)
        dataframe.loc[~regime, "enter_long"] = 0
        dataframe.loc[~regime, "enter_tag"] = ""
        return dataframe


class FuturesBtcRegime200Long(FuturesBtcRegimeBase):
    """Allow longs only above a rising BTC daily 200-day EMA."""

    def btc_regime(self, dataframe: DataFrame) -> DataFrame:
        return (
            (dataframe["btc_close_1d"] > dataframe["btc_ema_200_1d"])
            & (dataframe["btc_ema_200_slope_1d"] > 0)
        )


class FuturesBtcRegime50x200Long(FuturesBtcRegimeBase):
    """Stricter BTC regime: price and 50-day EMA must both exceed the 200-day EMA."""

    def btc_regime(self, dataframe: DataFrame) -> DataFrame:
        return dataframe["btc_regime_1d"] == Regime.BULL.value


class FuturesBtcRegimeRisingLong(FuturesBtcRegime50x200Long):
    """Research-only: require the BTC long-term trend itself to be rising.

    This is an anti-whipsaw hypothesis, not a return guarantee.  A 200-day
    EMA must also be higher than it was 20 completed daily candles ago before
    a long breakout can enter.
    """

    def btc_regime(self, dataframe: DataFrame) -> DataFrame:
        return super().btc_regime(dataframe) & (dataframe["btc_ema_200_slope_1d"] > 0)


class FuturesBtcRegimeCircuitBreakerLong(FuturesBtcRegime50x200Long):
    """Research-only: pause new entries after a small portfolio loss cluster.

    The base 12% MaxDrawdown protection is much larger than this deliberately
    small dry-run portfolio.  This candidate pauses for 14 days when the last
    ten closed trades lose 3%, limiting repeated entries in a failed regime.
    """

    @property
    def protections(self):
        protections = super().protections
        protections[-1] = {
            "method": "MaxDrawdown",
            "lookback_period_candles": 252,
            "trade_limit": 10,
            "stop_duration_candles": 84,
            "max_allowed_drawdown": 0.03,
        }
        return protections


class FuturesBtcRegimeTightStopLong(FuturesBtcRegime50x200Long):
    """Research-only: cap each 1x position's planned loss at 8% instead of 12%."""

    stoploss = -0.08


class FuturesBtcRegimeRiskBudgetLong(FuturesBtcRegimeTightStopLong):
    """Dry-run candidate with a fixed fraction of account equity at risk.

    `stake_amount` is only a ceiling.  At each entry, stake is calculated so
    an -8% stop represents at most 0.4% of the bot's configured capital.
    Returning zero when the exchange minimum would exceed that budget fails
    closed rather than silently increasing the risk.
    """

    account_risk_fraction = 0.004

    def custom_stake_amount(
        self,
        pair: str,
        current_time,
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
        planned_risk = abs(self.stoploss)
        if equity <= 0 or planned_risk <= 0:
            return 0.0

        stake = equity * self.account_risk_fraction / planned_risk
        if min_stake is not None and stake < min_stake:
            return 0.0
        return min(stake, max_stake)


class FuturesBtcRegimeDailyGuardLong(FuturesBtcRegimeRiskBudgetLong):
    """Research-only daily guard; it never chases a daily profit target.

    Once closed trades reach +1% net of the bot's usable equity in a Korea
    Standard Time session, no further entries are permitted that day.  The
    same fail-closed rule applies after -0.8% realised net loss.  Open trades
    continue to use their configured exchange stoploss; this callback only
    prevents *new* entries.  It deliberately does not turn a missed target
    into a reason to trade more often, increase leverage, or enlarge stakes.
    """

    daily_profit_target = 0.01
    daily_loss_limit = -0.008
    session_timezone = ZoneInfo("Asia/Seoul")

    def _session_realized_pnl_ratio(self, current_time: datetime) -> float | None:
        """Return KST-session closed PnL divided by usable account equity.

        `close_profit_abs` includes Freqtrade's recorded trading fees and
        futures funding.  Unrealised PnL is intentionally excluded so a brief
        price spike cannot lock the bot and then reverse before an exit.
        """
        reference = current_time
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        session_start = reference.astimezone(self.session_timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Trade.close_date is UTC-aware in both Freqtrade's live and backtest
        # persistence layers, so retain the timezone information for filtering.
        session_start_utc = session_start.astimezone(timezone.utc)
        closed_trades = Trade.get_trades_proxy(is_open=False, close_date=session_start_utc)
        realised_pnl = sum(trade.close_profit_abs or 0.0 for trade in closed_trades)
        usable_equity = self.wallets.get_total_stake_amount()
        if usable_equity <= 0:
            return None
        return realised_pnl / usable_equity

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
        session_pnl = self._session_realized_pnl_ratio(current_time)
        if session_pnl is not None and (
            session_pnl >= self.daily_profit_target or session_pnl <= self.daily_loss_limit
        ):
            return 0.0
        return super().custom_stake_amount(
            pair,
            current_time,
            current_rate,
            proposed_stake,
            min_stake,
            max_stake,
            leverage,
            entry_tag,
            side,
            **kwargs,
        )


class FuturesBtcRegimeFastRiskBudgetLong(FuturesBtcRegimeRiskBudgetLong):
    """Research-only medium-turnover variant of the accepted 4-hour hypothesis.

    This is intentionally a single horizon change: a 20-day entry channel and
    10-day exit channel replace the 40/20-day channels.  BTC regime filtering,
    1x leverage, the -8% stop and 0.4% per-position risk budget remain
    unchanged.  It must pass the pre-committed development and locked periods
    before it can replace the low-turnover default.
    """

    entry_window = 120
    exit_window = 60


class ALPHA_001_TREND_LONG(FuturesBtcRegimeRiskBudgetLong):
    """Official research alias for the existing long-trend Alpha #1.

    The production dry-run config retains its historic class name until the
    Alpha has enough OOS observations to clear the research gate.
    """
