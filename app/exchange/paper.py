"""Paper broker: full simulation of the same execution flow, in-process.

Uses *real* market data when MEXC is reachable (public endpoints need no
credentials) and the built-in synthetic feed otherwise, so the dashboard,
strategy and risk engine behave identically in either case.

Fills are modelled with taker fees, a configurable slippage, and stop/target
triggers evaluated against mark-price ticks (not candle closes), mirroring how
the live broker behaves.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from ..utils import Clock, round_to_step
from .base import (
    LONG,
    OPEN_ISOLATED,
    SHORT,
    AccountSnapshot,
    Broker,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)

log = logging.getLogger("paper-broker")


class SyntheticMarketAdapter:
    """Adapts :class:`SyntheticFeed` to the async market interface."""

    def __init__(self, feed) -> None:
        self.feed = feed
        self.simulated = True

    async def start(self) -> None:
        await self.feed.start()

    async def stop(self) -> None:
        await self.feed.stop()

    async def contracts(self) -> Dict[str, ContractSpec]:
        return dict(self.feed.contracts)

    async def tickers(self) -> Dict[str, Ticker]:
        return self.feed.tickers()

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        return self.feed.ticker(symbol)

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        return self.feed.klines(symbol, interval, limit)

    async def mark_price(self, symbol: str) -> float:
        return self.feed.mark_price(symbol)

    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None:
        self.feed.add_interval(interval)


class MeXCPublicMarketAdapter:
    """Adapts the public half of :class:`MeXCClient` for paper trading."""

    def __init__(self, client, clock: Clock) -> None:
        self.client = client
        self.clock = clock
        self.simulated = False
        self._cache: Dict[str, Ticker] = {}
        self._cache_ts = 0.0

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> None:
        pass

    async def contracts(self) -> Dict[str, ContractSpec]:
        rows = await self.client.contract_details()
        out: Dict[str, ContractSpec] = {}
        for item in rows:
            try:
                out[item["symbol"]] = ContractSpec(
                    symbol=item["symbol"],
                    contract_size=float(item.get("contractSize", 1) or 1),
                    price_unit=float(item.get("priceUnit", 0.0001) or 0.0001),
                    vol_unit=float(item.get("volUnit", 1) or 1),
                    min_vol=float(item.get("minVol", 1) or 1),
                    max_vol=float(item.get("maxVol", 1e9) or 1e9),
                    max_leverage=int(item.get("maxLeverage", 100) or 100),
                    taker_fee=float(item.get("takerFeeRate", 0.0006) or 0.0006),
                    maker_fee=float(item.get("makerFeeRate", 0.0002) or 0.0002),
                    api_allowed=bool(item.get("apiAllowed", True)),
                    state=int(item.get("state", 0) or 0),
                    is_new=bool(item.get("isNew", False)),
                    base=item.get("baseCoin", ""),
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    async def _refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._cache_ts < 1.0:
            return
        rows = await self.client.tickers()
        now = time.time()
        for item in rows:
            try:
                sym = item["symbol"]
                self._cache[sym] = Ticker(
                    symbol=sym,
                    last=float(item.get("lastPrice") or 0),
                    bid=float(item.get("bid1") or 0),
                    ask=float(item.get("ask1") or 0),
                    volume24=float(item.get("volume24") or 0),
                    amount24=float(item.get("amount24") or 0),
                    hold_vol=float(item.get("holdVol") or 0),
                    high24=float(item.get("high24Price") or 0),
                    low24=float(item.get("lower24Price") or 0),
                    rise_fall_rate=float(item.get("riseFallRate") or 0),
                    index_price=float(item.get("indexPrice") or 0),
                    fair_price=float(item.get("fairPrice") or 0),
                    funding_rate=float(item.get("fundingRate") or 0),
                    ts=now,
                )
            except Exception:  # noqa: BLE001
                continue
        self._cache_ts = now

    async def tickers(self) -> Dict[str, Ticker]:
        await self._refresh()
        return dict(self._cache)

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        await self._refresh()
        return self._cache.get(symbol)

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        return await self.client.klines(symbol, interval, limit)

    async def mark_price(self, symbol: str) -> float:
        tk = await self.ticker(symbol)
        return tk.mark() if tk else 0.0

    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None:
        return None


class PaperBroker(Broker):
    """Local matching engine + portfolio simulation."""

    name = "paper"
    mode = "paper"
    supports_exchange_stops = True   # simulated, but the flow is identical

    def __init__(
        self,
        market,
        *,
        starting_equity: float = 1000.0,
        slippage_bps: float = 1.5,
        price_interval_s: float = 0.2,
        position_mode: int = 1,
        open_type: int = OPEN_ISOLATED,
        clock: Optional[Clock] = None,
    ) -> None:
        self.market = market
        self.starting_equity = float(starting_equity)
        self.slippage_bps = slippage_bps
        self.price_interval_s = price_interval_s
        self.position_mode = position_mode
        self.open_type = open_type
        self.clock = clock or Clock()

        self.realized = 0.0
        self.fees_paid = 0.0
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._contracts: Dict[str, ContractSpec] = {}
        self._tickers: Dict[str, Ticker] = {}
        self._protection: Dict[str, Dict[str, Any]] = {}
        self._leverage: Dict[str, int] = {}
        self._subscribed: set = set()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._cb_kline = None
        self._cb_tick = None
        self._cb_order = None
        self._closed_callback = None
        self._last_candle_ts: Dict[str, int] = {}
        self._triggered: set = set()
        self._seq = 0

    # -- lifecycle ------------------------------------------------------ #
    async def start(self) -> None:
        await self.market.start()
        try:
            self._contracts = await self.market.contracts()
        except Exception as exc:  # noqa: BLE001
            log.warning("paper: could not load contracts: %s", exc)
        self._stop.clear()
        self._task = asyncio.create_task(self._price_loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.market.stop()

    async def set_callbacks(self, on_kline=None, on_tick=None, on_position=None, on_order=None) -> None:
        self._cb_kline = on_kline
        self._cb_tick = on_tick
        self._cb_order = on_order

    async def _price_loop(self) -> None:
        """Drive ticks: SL/TP evaluation + engine callbacks."""
        while not self._stop.is_set():
            try:
                tickers = await self.market.tickers()
                now = time.time()
                self._tickers.update(tickers)
                for symbol, tk in tickers.items():
                    mark = tk.mark() or tk.last
                    if mark <= 0:
                        continue
                    if self._cb_tick and symbol in self._subscribed:
                        self._cb_tick(symbol, mark, tk.bid, tk.ask)
                    self._evaluate_protection(symbol, tk)
                await self._emit_candle_closes(now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("paper price loop error: %s", exc)
            await asyncio.sleep(self.price_interval_s)

    async def _emit_candle_closes(self, now: float) -> None:
        if not self._cb_kline:
            return
        for symbol in list(self._subscribed):
            try:
                candles = await self.market.klines(symbol, "Min5", 3)
                if len(candles) < 2:
                    continue
                last_closed = candles[-2]
                if self._last_candle_ts.get(symbol) == last_closed.ts:
                    continue
                self._last_candle_ts[symbol] = last_closed.ts
                self._cb_kline(symbol, "Min5", last_closed, True)
            except Exception as exc:  # noqa: BLE001
                log.debug("paper candle emit failed for %s: %s", symbol, exc)

    # -- market data ---------------------------------------------------- #
    async def contracts(self) -> Dict[str, ContractSpec]:
        if not self._contracts:
            self._contracts = await self.market.contracts()
        return self._contracts

    async def tickers(self) -> Dict[str, Ticker]:
        self._tickers.update(await self.market.tickers())
        return self._tickers

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        """The market is the source of truth; the cache only covers a miss."""
        tk = await self.market.ticker(symbol)
        if tk is None:
            tk = self._tickers.get(symbol)
        else:
            self._tickers[symbol] = tk
        return tk

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        return await self.market.klines(symbol, interval, limit)

    async def mark_price(self, symbol: str) -> float:
        tk = await self.ticker(symbol)
        return (tk.mark() or tk.last) if tk else 0.0

    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None:
        self._subscribed.update(symbols)
        await self.market.subscribe(symbols, interval)

    async def unsubscribe_all(self) -> None:
        self._subscribed.clear()

    # -- account -------------------------------------------------------- #
    def _unrealized(self) -> float:
        total = 0.0
        for symbol, pos in self._positions.items():
            tk = self._tickers.get(symbol)
            mark = (tk.mark() or tk.last) if tk else pos["entry"]
            total += self._pnl(pos, mark)
        return total

    def _margin_used(self) -> float:
        return sum(p["margin"] for p in self._positions.values())

    async def account(self) -> AccountSnapshot:
        unreal = self._unrealized()
        equity = self.starting_equity + self.realized + unreal
        return AccountSnapshot(
            equity=equity,
            available=max(0.0, equity - self._margin_used()),
            unrealized=unreal,
            position_margin=self._margin_used(),
            currency="USDT",
            ts=time.time(),
        )

    async def positions(self) -> List[Position]:
        out = []
        for symbol, pos in self._positions.items():
            tk = self._tickers.get(symbol)
            mark = (tk.mark() or tk.last) if tk else pos["entry"]
            out.append(
                Position(
                    symbol=symbol,
                    side=pos["side"],
                    hold_vol=pos["qty"],
                    open_avg_price=pos["entry"],
                    leverage=pos["leverage"],
                    unrealized=self._pnl(pos, mark),
                    im=pos["margin"],
                    position_id=pos["position_id"],
                    state=1,
                    mark_price=mark,
                )
            )
        return out

    @staticmethod
    def _pnl(pos: Dict[str, Any], price: float) -> float:
        direction = 1.0 if pos["side"] == LONG else -1.0
        return (price - pos["entry"]) * pos["qty"] * pos["contract_size"] * direction

    # -- trading -------------------------------------------------------- #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        self._leverage[f"{symbol}:{position_type}"] = leverage
        return True

    def _fill_price(self, symbol: str, is_buy: bool, mark: float) -> float:
        """Fill at the current touch.

        The live mark is authoritative — a *cached* book snapshot must never be
        used when it disagrees with it (that would let paper fills happen at a
        price that no longer exists after a move).
        """
        base = mark
        tk = self._tickers.get(symbol)
        if tk is not None and tk.last > 0 and mark > 0 and abs(tk.last - mark) / mark < 0.01:
            touch = tk.ask if is_buy else tk.bid
            if touch > 0:
                base = touch
        slip = self.slippage_bps / 10_000.0
        return base * (1 + slip) if is_buy else base * (1 - slip)

    async def open_position(
        self, symbol: str, side: str, qty: float, leverage: int,
        price_hint: float = 0.0, client_id: str = "",
        sl_price: Optional[float] = None, tp_price: Optional[float] = None,
    ) -> OrderResult:
        started = time.perf_counter()
        mark = await self.mark_price(symbol)
        if mark <= 0:
            return OrderResult(ok=False, error=f"no market data for {symbol}")
        if symbol in self._positions:
            return OrderResult(ok=False, error=f"position already open for {symbol}")
        spec = (await self.contracts()).get(symbol)
        contract_size = spec.contract_size if spec else 1.0
        fee_rate = spec.taker_fee if spec else 0.0006
        fill = self._fill_price(symbol, side == LONG, mark)
        notional = fill * qty * contract_size
        margin = notional / max(1, leverage)
        fee = notional * fee_rate

        self._seq += 1
        self._triggered.discard(symbol)
        self.realized -= fee
        self.fees_paid += fee
        self._positions[symbol] = {
            "side": side,
            "qty": qty,
            "entry": fill,
            "leverage": leverage,
            "margin": margin,
            "contract_size": contract_size,
            "position_id": 900_000 + self._seq,
            "opened_at": time.time(),
            "fees": fee,
            "notional": notional,
        }
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        self._protection[symbol] = {
            "kind": "paper",
            "symbol": symbol,
            "entry_order_id": order_id,
            "stop_price": sl_price,
            "tp_price": tp_price,
            "tpsl_id": f"paper-tpsl-{self._seq}",
        }
        if self._cb_order:
            self._cb_order({
                "channel": "push.personal.order", "ok": True, "symbol": symbol,
                "orderId": order_id, "state": 3, "dealAvgPrice": fill, "dealVol": qty,
                "side": 1 if side == LONG else 3, "reduceOnly": False, "simulated": True,
            })
        return OrderResult(
            ok=True, order_id=order_id, price=fill, vol=qty, filled_vol=qty, status="filled",
            latency_ms=(time.perf_counter() - started) * 1000.0,
            raw={"protection": "attached", "simulated": True},
        )

    async def close_position(
        self, symbol: str, side: str, qty: float, reason: str = "", client_id: str = "",
    ) -> OrderResult:
        started = time.perf_counter()
        pos = self._positions.get(symbol)
        if not pos:
            return OrderResult(ok=False, error=f"no open position for {symbol}")
        qty = min(qty, pos["qty"]) if qty else pos["qty"]
        mark = await self.mark_price(symbol)
        fill = self._fill_price(symbol, pos["side"] != LONG, mark)
        spec = (await self.contracts()).get(symbol)
        fee_rate = spec.taker_fee if spec else 0.0006
        close_fee = fill * qty * pos["contract_size"] * fee_rate
        pnl = self._pnl({**pos, "qty": qty}, fill) - close_fee
        self.realized += pnl
        self.fees_paid += close_fee
        pos["qty"] -= qty
        if pos["qty"] <= 1e-9:
            self._positions.pop(symbol, None)
            self._protection.pop(symbol, None)
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        if self._cb_order:
            self._cb_order({
                "channel": "push.personal.order", "ok": True, "symbol": symbol,
                "orderId": order_id, "state": 3, "dealAvgPrice": fill, "dealVol": qty,
                "side": 4 if pos["side"] == LONG else 2, "reduceOnly": True, "profit": pnl,
                "simulated": True, "reason": reason,
            })
        return OrderResult(
            ok=True, order_id=order_id, price=fill, vol=qty, filled_vol=qty, status="filled",
            latency_ms=(time.perf_counter() - started) * 1000.0,
            raw={"pnl": pnl, "reason": reason, "simulated": True},
        )

    # -- protection ----------------------------------------------------- #
    async def arm_protection(
        self, *, symbol: str, side: str, qty: float, sl_price: Optional[float],
        tp_price: Optional[float], entry_order_id: str = "",
    ) -> Dict[str, Any]:
        handle = self._protection.get(symbol) or {"kind": "paper", "symbol": symbol}
        if sl_price is not None:
            handle["stop_price"] = sl_price
        if tp_price is not None:
            handle["tp_price"] = tp_price
        handle["entry_order_id"] = entry_order_id or handle.get("entry_order_id", "")
        self._protection[symbol] = handle
        return handle

    async def move_stop(self, *, symbol: str, handle: Dict[str, Any], new_stop_price: float, qty: float) -> bool:
        h = self._protection.get(symbol)
        if h is None:
            return False
        h["stop_price"] = new_stop_price
        return True

    async def release_stop(self, *, symbol: str, handle: Dict[str, Any]) -> bool:
        h = self._protection.get(symbol)
        if h:
            h["stop_price"] = None
            h["tp_price"] = None
        return True

    def _evaluate_protection(self, symbol: str, tk: Ticker) -> None:
        """Simulate exchange-side trigger evaluation on every mark tick."""
        pos = self._positions.get(symbol)
        handle = self._protection.get(symbol)
        if not pos or not handle or symbol in self._triggered:
            return
        mark = tk.mark() or tk.last
        if mark <= 0:
            return
        stop = handle.get("stop_price")
        tp = handle.get("tp_price")
        is_long = pos["side"] == LONG
        hit_reason = None
        if stop:
            if (is_long and mark <= stop) or (not is_long and mark >= stop):
                hit_reason = "stop_loss"
        if hit_reason is None and tp:
            if (is_long and mark >= tp) or (not is_long and mark <= tp):
                hit_reason = "take_profit"
        if hit_reason:
            self._triggered.add(symbol)
            asyncio.create_task(self._force_close(symbol, hit_reason, mark))

    async def _force_close(self, symbol: str, reason: str, mark: float) -> None:
        pos = self._positions.get(symbol)
        if not pos:
            self._triggered.discard(symbol)
            return
        res = await self.close_position(symbol, pos["side"], pos["qty"], reason=reason)
        self._triggered.discard(symbol)
        if res.ok and self._closed_callback:
            try:
                await self._closed_callback(symbol, reason, res)
            except Exception as exc:  # noqa: BLE001
                log.warning("close callback failed for %s: %s", symbol, exc)

    def set_close_callback(self, cb) -> None:
        self._closed_callback = cb

    async def place_stop_order(
        self, symbol: str, side: str, qty: float, trigger_price: float,
        limit_price: float = 0.0, client_id: str = "",
    ) -> OrderResult:
        handle = self._protection.setdefault(symbol, {"kind": "paper", "symbol": symbol})
        handle["stop_price"] = trigger_price
        return OrderResult(ok=True, order_id=f"paper-sl-{self._seq}", price=trigger_price, vol=qty,
                           status="placed", raw={"simulated": True})

    async def place_tp_order(self, symbol: str, side: str, qty: float, price: float, client_id: str = "") -> OrderResult:
        handle = self._protection.setdefault(symbol, {"kind": "paper", "symbol": symbol})
        handle["tp_price"] = price
        return OrderResult(ok=True, order_id=f"paper-tp-{self._seq}", price=price, vol=qty,
                           status="placed", raw={"simulated": True})

    async def modify_stop_order(self, symbol: str, order_id: str, trigger_price: float, limit_price: float = 0.0) -> OrderResult:
        handle = self._protection.setdefault(symbol, {"kind": "paper", "symbol": symbol})
        handle["stop_price"] = trigger_price
        return OrderResult(ok=True, order_id=order_id, price=trigger_price, raw={"simulated": True})

    async def cancel_stop_order(self, symbol: str, order_id: str) -> bool:
        handle = self._protection.get(symbol)
        if handle:
            handle["stop_price"] = None
        return True

    async def cancel_order(self, order_id: str) -> bool:
        return True

    async def cancel_all_orders(self, symbol: str) -> bool:
        handle = self._protection.get(symbol)
        if handle:
            handle["stop_price"] = None
            handle["tp_price"] = None
        return True

    async def open_orders(self, symbol: str) -> List[Dict[str, Any]]:
        handle = self._protection.get(symbol)
        if not handle:
            return []
        out = []
        if handle.get("stop_price"):
            out.append({"orderId": handle.get("stop_order_id") or "paper-sl",
                        "symbol": symbol, "reduceOnly": True, "price": handle["stop_price"],
                        "state": 2, "simulated": True})
        if handle.get("tp_price"):
            out.append({"orderId": handle.get("tp_order_id") or "paper-tp",
                        "symbol": symbol, "reduceOnly": True, "price": handle["tp_price"],
                        "state": 2, "simulated": True})
        return out

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "name": self.name,
            "simulated_market_data": getattr(self.market, "simulated", False),
            "open_positions": len(self._positions),
            "realized_pnl": round(self.realized, 6),
            "fees_paid": round(self.fees_paid, 6),
            "subscribed": len(self._subscribed),
        }
