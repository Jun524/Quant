"""Credential-free order-command state machine for Alpha #7.

The engine creates Binance-shaped commands but never performs network I/O.
An adapter may submit them only after independent promotion and live preflight.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
from typing import Literal


Side = Literal["BUY", "SELL"]


class OrderPathError(ValueError):
    """Raised when an event would violate the frozen execution policy."""


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise OrderPathError(f"{field} is not decimal-compatible") from error
    if not result.is_finite():
        raise OrderPathError(f"{field} must be finite")
    return result


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _wire(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class SymbolFilters:
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_values(
        cls,
        *,
        tick_size: object,
        step_size: object,
        min_qty: object,
        max_qty: object,
        min_notional: object,
    ) -> "SymbolFilters":
        result = cls(
            tick_size=_decimal(tick_size, "tick_size"),
            step_size=_decimal(step_size, "step_size"),
            min_qty=_decimal(min_qty, "min_qty"),
            max_qty=_decimal(max_qty, "max_qty"),
            min_notional=_decimal(min_notional, "min_notional"),
        )
        if (
            result.tick_size <= 0
            or result.step_size <= 0
            or result.min_qty <= 0
            or result.max_qty < result.min_qty
            or result.min_notional <= 0
        ):
            raise OrderPathError("symbol filters must be positive and internally ordered")
        return result


@dataclass(frozen=True)
class OrderCommand:
    action: Literal["PLACE", "CANCEL"]
    role: Literal["ENTRY", "EXIT"]
    symbol: str
    client_order_id: str
    side: Side | None
    order_type: Literal["LIMIT", "MARKET"] | None
    quantity: str | None
    price: str | None
    time_in_force: Literal["GTX"] | None
    reduce_only: bool | None
    position_side: Literal["BOTH"] | None
    emitted_at_ms: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


class Alpha007OrderPath:
    NOTIONAL = Decimal("50")
    MAX_SIGNAL_AGE_MS = 250
    ENTRY_TIMEOUT_MS = 3_000
    EXIT_DELAY_MS = 60_000
    MAX_EXIT_ATTEMPTS = 3
    EPSILON = Decimal("0.000000000001")

    def __init__(self, *, symbol: str = "BTCUSDT") -> None:
        if symbol != "BTCUSDT":
            raise OrderPathError("Alpha #7 order path is locked to BTCUSDT")
        self.symbol = symbol
        self._clear()

    def _clear(self) -> None:
        self.signal_id: str | None = None
        self.entry_client_order_id: str | None = None
        self.exit_client_order_id: str | None = None
        self.entry_side: Side | None = None
        self.decision_ms: int | None = None
        self.entry_deadline_ms: int | None = None
        self.exit_due_ms: int | None = None
        self.entry_qty = Decimal("0")
        self.entry_filled_qty = Decimal("0")
        self.exit_filled_qty = Decimal("0")
        self.current_exit_order_qty = Decimal("0")
        self.current_exit_order_filled = Decimal("0")
        self.exit_attempt = 0
        self.entry_terminal = False
        self.cancel_requested = False
        self.exit_placed = False
        self.force_exit = False
        self.fault: str | None = None

    @property
    def state(self) -> str:
        if self.fault:
            return "FAULT"
        if self.signal_id is None:
            return "FLAT"
        if self.exit_placed:
            return "EXIT_WORKING"
        if not self.entry_terminal:
            return "ENTRY_CANCEL_PENDING" if self.cancel_requested else "ENTRY_WORKING"
        return "POSITION_OPEN"

    @property
    def open_quantity(self) -> Decimal:
        return self.entry_filled_qty - self.exit_filled_qty

    def _client_id(self, role: str, signal_id: str, decision_ms: int) -> str:
        digest = sha256(f"{signal_id}|{decision_ms}|{role}".encode()).hexdigest()[:24]
        result = f"a7{role}-{digest}"
        if len(result) > 36:
            raise AssertionError("client order id exceeds Binance limit")
        return result

    def on_signal(
        self,
        *,
        signal_id: str,
        side: str,
        decision_ms: int,
        observed_ms: int,
        best_bid: object,
        best_ask: object,
        filters: SymbolFilters,
    ) -> OrderCommand:
        if self.state != "FLAT":
            raise OrderPathError("new signal rejected while an order cycle is active")
        if not signal_id:
            raise OrderPathError("signal_id is required")
        normalized_side = side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise OrderPathError("side must be BUY or SELL")
        if not isinstance(decision_ms, int) or not isinstance(observed_ms, int):
            raise OrderPathError("decision and observation clocks must be integer milliseconds")
        age = observed_ms - decision_ms
        if age < 0 or age > self.MAX_SIGNAL_AGE_MS:
            raise OrderPathError("signal is future-dated or stale")
        bid = _decimal(best_bid, "best_bid")
        ask = _decimal(best_ask, "best_ask")
        if bid <= 0 or ask <= bid:
            raise OrderPathError("book must have a positive, non-crossed spread")
        if normalized_side == "BUY":
            price = _floor_to_step(bid, filters.tick_size)
            if price > bid:
                raise AssertionError("buy entry became marketable")
        else:
            price = _ceil_to_step(ask, filters.tick_size)
            if price < ask:
                raise AssertionError("sell entry became marketable")
        quantity = _floor_to_step(self.NOTIONAL / price, filters.step_size)
        if (
            price <= 0
            or quantity < filters.min_qty
            or quantity > filters.max_qty
            or quantity * price < filters.min_notional
        ):
            raise OrderPathError("fixed order does not satisfy current symbol filters")
        self.signal_id = signal_id
        self.entry_side = normalized_side  # type: ignore[assignment]
        self.decision_ms = decision_ms
        self.entry_deadline_ms = decision_ms + self.ENTRY_TIMEOUT_MS
        self.exit_due_ms = decision_ms + self.EXIT_DELAY_MS
        self.entry_qty = quantity
        self.entry_client_order_id = self._client_id("e", signal_id, decision_ms)
        return OrderCommand(
            action="PLACE",
            role="ENTRY",
            symbol=self.symbol,
            client_order_id=self.entry_client_order_id,
            side=self.entry_side,
            order_type="LIMIT",
            quantity=_wire(quantity),
            price=_wire(price),
            time_in_force="GTX",
            reduce_only=False,
            position_side="BOTH",
            emitted_at_ms=observed_ms,
            reason="fresh_alpha007_signal",
        )

    def on_entry_update(
        self,
        *,
        client_order_id: str,
        event_ms: int,
        cumulative_filled_qty: object,
        terminal: bool,
    ) -> None:
        if self.signal_id is None or client_order_id != self.entry_client_order_id:
            raise OrderPathError("entry update does not match the active cycle")
        if self.decision_ms is None or event_ms < self.decision_ms:
            raise OrderPathError("entry update precedes the signal")
        cumulative = _decimal(cumulative_filled_qty, "cumulative_filled_qty")
        if cumulative < self.entry_filled_qty or cumulative > self.entry_qty + self.EPSILON:
            raise OrderPathError("entry cumulative fill is decreasing or exceeds order quantity")
        self.entry_filled_qty = min(cumulative, self.entry_qty)
        self.entry_terminal = bool(terminal or self.entry_filled_qty >= self.entry_qty - self.EPSILON)
        if self.entry_terminal and self.entry_filled_qty <= self.EPSILON:
            self._clear()

    def on_cancel_ack(
        self,
        *,
        client_order_id: str,
        event_ms: int,
        cumulative_filled_qty: object,
    ) -> None:
        self.on_entry_update(
            client_order_id=client_order_id,
            event_ms=event_ms,
            cumulative_filled_qty=cumulative_filled_qty,
            terminal=True,
        )

    def _cancel_command(self, now_ms: int, reason: str) -> OrderCommand:
        assert self.entry_client_order_id is not None
        self.cancel_requested = True
        return OrderCommand(
            action="CANCEL",
            role="ENTRY",
            symbol=self.symbol,
            client_order_id=self.entry_client_order_id,
            side=None,
            order_type=None,
            quantity=None,
            price=None,
            time_in_force=None,
            reduce_only=None,
            position_side=None,
            emitted_at_ms=now_ms,
            reason=reason,
        )

    def _exit_command(self, now_ms: int, reason: str) -> OrderCommand:
        if self.entry_side is None or self.signal_id is None or self.decision_ms is None:
            raise OrderPathError("active cycle has no exit identity")
        quantity = self.open_quantity
        if quantity <= self.EPSILON:
            raise OrderPathError("cannot submit an exit without positive exposure")
        self.exit_attempt += 1
        self.exit_client_order_id = self._client_id(
            f"x{self.exit_attempt}", self.signal_id, self.decision_ms
        )
        self.current_exit_order_qty = quantity
        self.current_exit_order_filled = Decimal("0")
        self.exit_placed = True
        exit_side: Side = "SELL" if self.entry_side == "BUY" else "BUY"
        return OrderCommand(
            action="PLACE",
            role="EXIT",
            symbol=self.symbol,
            client_order_id=self.exit_client_order_id,
            side=exit_side,
            order_type="MARKET",
            quantity=_wire(quantity),
            price=None,
            time_in_force=None,
            reduce_only=True,
            position_side="BOTH",
            emitted_at_ms=now_ms,
            reason=reason,
        )

    def on_timer(self, now_ms: int) -> list[OrderCommand]:
        if self.signal_id is None:
            return []
        if self.fault:
            raise OrderPathError(f"order path is faulted: {self.fault}")
        if self.decision_ms is None or now_ms < self.decision_ms:
            raise OrderPathError("timer precedes the active signal")
        commands: list[OrderCommand] = []
        assert self.entry_deadline_ms is not None and self.exit_due_ms is not None
        exit_due = self.force_exit or now_ms >= self.exit_due_ms
        if not self.entry_terminal and (
            now_ms >= self.entry_deadline_ms or exit_due
        ) and not self.cancel_requested:
            commands.append(
                self._cancel_command(
                    now_ms,
                    "emergency_cancel" if self.force_exit else "entry_timeout_cancel",
                )
            )
        if self.entry_terminal and self.open_quantity > self.EPSILON and exit_due and not self.exit_placed:
            commands.append(
                self._exit_command(
                    now_ms,
                    "emergency_reduce_only_exit" if self.force_exit else "scheduled_60s_exit",
                )
            )
        return commands

    def emergency(self, now_ms: int) -> list[OrderCommand]:
        if self.signal_id is None:
            return []
        self.force_exit = True
        return self.on_timer(now_ms)

    def on_exit_update(
        self,
        *,
        client_order_id: str,
        event_ms: int,
        cumulative_filled_qty: object,
        terminal: bool,
    ) -> list[OrderCommand]:
        if not self.exit_placed or client_order_id != self.exit_client_order_id:
            raise OrderPathError("exit update does not match the active cycle")
        if self.decision_ms is None or event_ms < self.decision_ms:
            raise OrderPathError("exit update precedes the signal")
        cumulative = _decimal(cumulative_filled_qty, "exit_cumulative_filled_qty")
        if cumulative < self.current_exit_order_filled or cumulative > self.current_exit_order_qty + self.EPSILON:
            raise OrderPathError("exit order cumulative fill is decreasing or exceeds its quantity")
        delta = cumulative - self.current_exit_order_filled
        self.current_exit_order_filled = min(cumulative, self.current_exit_order_qty)
        self.exit_filled_qty = min(
            self.entry_filled_qty, self.exit_filled_qty + max(Decimal("0"), delta)
        )
        if self.open_quantity <= self.EPSILON:
            self._clear()
            return []
        elif terminal:
            if self.exit_attempt >= self.MAX_EXIT_ATTEMPTS:
                self.fault = "terminal_exit_left_residual_after_maximum_retries"
                raise OrderPathError(self.fault)
            self.exit_placed = False
            self.force_exit = True
            return [self._exit_command(event_ms, "residual_reduce_only_retry")]
        return []
