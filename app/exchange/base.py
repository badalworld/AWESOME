"""Broker abstraction: shared types + the interface both live and paper prove out.

Keeping one interface means the strategy, risk and execution layers are
*identical* in paper and live mode — the only swapped component is the broker.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

LONG = "LONG"
SHORT = "SHORT"

SIDE_OPEN_LONG = 1
SIDE_CLOSE_SHORT = 2
SIDE_OPEN_SHORT = 3
SIDE_CLOSE_LONG = 4

ORDER_LIMIT = 1
ORDER_IOC = 3
ORDER_MARKET = 5

OPEN_ISOLATED = 1


@dataclass(slots=True)
class Candle:
    ts: int          # window start, seconds
    o: float
    h: float
    l: float
    c: float
    v: float

    @property
    def mid(self) -> float:
        return (self.h + self.l) / 2.0

    @property
    def range(self) -> float:
        return self.h - self.l


# Fallback taker fee used when a venue does not report one. Kept in exactly one
# place: P&L accounting in live and paper must never disagree about fees.
DEFAULT_TAKER_FEE = 0.0006


@dataclass(slots=True)
class ContractSpec:
    symbol: str
    contract_size: float = 1.0
    price_unit: float = 0.0001
    price_scale: int = 4
    vol_unit: float = 1.0
    vol_scale: int = 0
    min_vol: float = 1.0
    max_vol: float = 1_000_000.0
    max_leverage: int = 100
    min_leverage: int = 1
    taker_fee: float = DEFAULT_TAKER_FEE
    maker_fee: float = 0.0002
    api_allowed: bool = True
    state: int = 0
    is_new: bool = False
    min_notional: float = 0.0      # venue MIN_NOTIONAL (0 = unknown / not enforced)
    base: str = ""
    quote: str = "USDT"
    position_open_type: int = 3
    trigger_protect: float = 0.0


@dataclass(slots=True)
class Ticker:
    symbol: str
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume24: float = 0.0        # contracts
    amount24: float = 0.0        # quote turnover (USDT)
    hold_vol: float = 0.0        # open interest (contracts)
    high24: float = 0.0
    low24: float = 0.0
    rise_fall_rate: float = 0.0
    index_price: float = 0.0
    fair_price: float = 0.0      # mark price -> used for ROI / SL triggers
    funding_rate: float = 0.0
    ts: float = 0.0

    @property
    def spread_bps(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 0.0
        return (self.ask - self.bid) / ((self.ask + self.bid) / 2.0) * 10_000.0

    def mark(self) -> float:
        return self.fair_price or self.last


@dataclass(slots=True)
class AccountSnapshot:
    equity: float = 0.0
    available: float = 0.0
    unrealized: float = 0.0
    position_margin: float = 0.0
    currency: str = "USDT"
    ts: float = 0.0


@dataclass(slots=True)
class Position:
    symbol: str
    side: str
    hold_vol: float
    open_avg_price: float
    leverage: int
    unrealized: float = 0.0
    im: float = 0.0
    liquidate_price: float = 0.0
    position_id: int = 0
    state: int = 1
    mark_price: float = 0.0


@dataclass
class OrderResult:
    ok: bool
    order_id: Optional[str] = None
    price: float = 0.0
    vol: float = 0.0
    filled_vol: float = 0.0
    status: str = ""
    error: str = ""
    latency_ms: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


KlineCallback = Callable[[str, str, Candle, bool], None]   # symbol, interval, candle, is_closed
TickCallback = Callable[[str, float, float, float], None]   # symbol, mark, bid, ask


class Broker(ABC):
    """Trading + market-data interface used by the engine."""

    name: str = "abstract"
    mode: str = "paper"

    # lifecycle -------------------------------------------------------- #
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # market data ------------------------------------------------------ #
    @abstractmethod
    async def contracts(self) -> Dict[str, ContractSpec]: ...

    @abstractmethod
    async def tickers(self) -> Dict[str, Ticker]: ...

    @abstractmethod
    async def ticker(self, symbol: str) -> Optional[Ticker]: ...

    @abstractmethod
    async def klines(self, symbol: str, interval: str, limit: int = 300) -> List[Candle]: ...

    @abstractmethod
    async def mark_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None: ...

    async def set_callbacks(
        self,
        on_kline: Optional[KlineCallback] = None,
        on_tick: Optional[TickCallback] = None,
        on_order: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._cb_kline = on_kline
        self._cb_tick = on_tick
        self._cb_order = on_order

    # account ---------------------------------------------------------- #
    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def positions(self) -> List[Position]: ...

    # trading ---------------------------------------------------------- #
    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool: ...

    async def open_position(
        self, symbol: str, side: str, qty: float, leverage: int,
        price_hint: float = 0.0, client_id: str = "",
        sl_price: Optional[float] = None, tp_price: Optional[float] = None,
    ) -> OrderResult:
        raise NotImplementedError

    # protection management (exchange-side stop / target lifecycle) ------- #
    async def arm_protection(
        self, *, symbol: str, side: str, qty: float, sl_price: Optional[float],
        tp_price: Optional[float], entry_order_id: str = "",
    ) -> Dict[str, Any]:
        """Ensure a stop and target exist for the position; returns a handle."""
        return {"kind": "none", "symbol": symbol}

    async def move_stop(
        self, *, symbol: str, handle: Dict[str, Any], new_stop_price: float, qty: float,
    ) -> bool:
        """Ratchet the stop to a new price (single call, never unprotected)."""
        return False

    async def release_stop(self, *, symbol: str, handle: Dict[str, Any]) -> bool:
        return False

    @abstractmethod
    async def close_position(
        self, symbol: str, side: str, qty: float, reason: str = "", client_id: str = "",
    ) -> OrderResult: ...

    # diagnostics ------------------------------------------------------ #
    def diagnostics(self) -> Dict[str, Any]:
        return {"mode": self.mode, "name": self.name}
