"""Live broker: real orders on MEXC USDT-M futures.

Execution strategy (low latency + always protected):

1. ``POST /order/create`` with ``externalOid`` (idempotent retries) and, when
   supported, an *attached* stop-loss + take-profit leg — this puts the stop on
   the exchange in the same round trip as the entry, so the position is never
   unprotected, not even for one tick.
2. If the exchange rejects the attached legs (order-type dependent), the
   executor immediately falls back to standalone protection:
   ``POST /planorder/place/v2`` (stop-market, reduce-only, fair-price trigger)
   and a reduce-only limit order for the fixed +200% ROI target.
3. Trailing steps move the stop in a single call —
   ``POST /planorder/change_stop_order`` for attached legs,
   ``POST /planorder/change_price`` for standalone plan orders — never
   cancel/replace, so there is no unprotected window.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..utils import Clock, LatencyTracker, round_to_step
from .base import (
    DEFAULT_TAKER_FEE,
    LONG,
    OPEN_ISOLATED,
    ORDER_IOC,
    ORDER_LIMIT,
    ORDER_MARKET,
    SHORT,
    SIDE_CLOSE_LONG,
    SIDE_CLOSE_SHORT,
    SIDE_OPEN_LONG,
    SIDE_OPEN_SHORT,
    AccountSnapshot,
    Broker,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)
from .mexc import MeXCClient, MeXCWebSocket

log = logging.getLogger("live-broker")


class LiveBroker(Broker):
    name = "mexc-futures"
    mode = "live"

    def __init__(
        self,
        client: MeXCClient,
        ws: Optional[MeXCWebSocket],
        *,
        position_mode: int = 1,
        entry_order_type: str = "market",
        exit_order_type: str = "market",
        ioc_buffer_bps: float = 4.0,
        open_type: int = OPEN_ISOLATED,
        stop_mode: str = "auto",          # auto | attached | separate
        trigger_trend: int = 2,           # 2 = fair (mark) price
        price_protect: int = 1,
        telemetry: Optional[LatencyTracker] = None,
    ) -> None:
        self.client = client
        self.ws = ws
        self.clock = client.clock
        self.position_mode = position_mode
        self.entry_order_type = entry_order_type.lower()
        self.exit_order_type = exit_order_type.lower()
        self.ioc_buffer_bps = ioc_buffer_bps
        self.open_type = open_type
        self.stop_mode = stop_mode
        self.trigger_trend = trigger_trend
        self.price_protect = price_protect
        self.telemetry = telemetry

        self._contracts: Dict[str, ContractSpec] = {}
        self._tickers: Dict[str, Ticker] = {}
        self._contracts_ts = 0.0
        self._leverage_set: Dict[str, int] = {}
        self._cb_kline = None
        self._cb_tick = None
        self.attached_supported = True   # learned at runtime, self-healing

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        await self.client.start()
        if self.ws:
            self.ws.on_kline = self._on_kline
            self.ws.on_tick = self._on_tick
            self.ws.on_order = self._on_order_push
            await self.ws.start()
        try:
            self.position_mode = await self.client.position_mode()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read position mode (assuming %s): %s", self.position_mode, exc)

    async def stop(self) -> None:
        if self.ws:
            await self.ws.stop()
        await self.client.close()

    async def set_callbacks(self, on_kline=None, on_tick=None, on_order=None) -> None:
        self._cb_kline = on_kline
        self._cb_tick = on_tick
        self._cb_order = on_order
        if self.ws:
            self.ws.on_order = on_order

    def _on_kline(self, symbol: str, interval: str, candle: Candle, is_closed: bool) -> None:
        if self._cb_kline:
            self._cb_kline(symbol, interval, candle, is_closed)

    def _on_tick(self, symbol: str, mark: float, bid: float, ask: float) -> None:
        tk = self._tickers.get(symbol)
        if tk is None:
            tk = Ticker(symbol=symbol)
            self._tickers[symbol] = tk
        tk.fair_price = mark
        tk.last = mark
        if bid:
            tk.bid = bid
        if ask:
            tk.ask = ask
        tk.ts = time.time()
        if self._cb_tick:
            self._cb_tick(symbol, mark, bid, ask)

    def _on_order_push(self, data: Dict[str, Any]) -> None:
        if self._cb_order:
            self._cb_order(data)

    # ------------------------------------------------------------------ #
    #  market data
    # ------------------------------------------------------------------ #
    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]:
        if self._contracts and not force and time.time() - self._contracts_ts < 600:
            return self._contracts
        raw: List[Dict[str, Any]] = []
        for fetch in (self.client.contract_details, self.client.contract_detail_country):
            try:
                raw = await fetch()
                if raw:
                    break
            except Exception as exc:  # noqa: BLE001
                log.debug("contract fetch failed via %s: %s", fetch.__name__, exc)
        out: Dict[str, ContractSpec] = {}
        for item in raw:
            try:
                symbol = item["symbol"]
                out[symbol] = ContractSpec(
                    symbol=symbol,
                    contract_size=float(item.get("contractSize", 1) or 1),
                    price_unit=float(item.get("priceUnit", 0.0001) or 0.0001),
                    price_scale=int(item.get("priceScale", 4) or 4),
                    vol_unit=float(item.get("volUnit", 1) or 1),
                    vol_scale=int(item.get("volScale", 0) or 0),
                    min_vol=float(item.get("minVol", 1) or 1),
                    max_vol=float(item.get("maxVol", 1e9) or 1e9),
                    max_leverage=int(item.get("maxLeverage", 100) or 100),
                    min_leverage=int(item.get("minLeverage", 1) or 1),
                    taker_fee=float(item.get("takerFeeRate", DEFAULT_TAKER_FEE) or DEFAULT_TAKER_FEE),
                    maker_fee=float(item.get("makerFeeRate", 0.0002) or 0.0002),
                    api_allowed=bool(item.get("apiAllowed", True)),
                    state=int(item.get("state", 0) or 0),
                    is_new=bool(item.get("isNew", False)),
                    base=item.get("baseCoin", ""),
                    quote=item.get("quoteCoin", "USDT"),
                    position_open_type=int(item.get("positionOpenType", 3) or 3),
                    trigger_protect=float(item.get("triggerProtect", 0) or 0),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("contract parse error: %s", exc)
        if out:
            self._contracts = out
            self._contracts_ts = time.time()
        return self._contracts

    async def tickers(self) -> Dict[str, Ticker]:
        rows = await self.client.tickers()
        out: Dict[str, Ticker] = {}
        for item in rows:
            try:
                symbol = item["symbol"]
                tk = Ticker(
                    symbol=symbol,
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
                    ts=time.time(),
                )
                # keep fresher WS values if we have them
                old = self._tickers.get(symbol)
                if old and old.ts > tk.ts - 1.0 and old.fair_price:
                    tk.fair_price = old.fair_price
                    tk.last = old.last or tk.last
                    if old.bid:
                        tk.bid = old.bid
                    if old.ask:
                        tk.ask = old.ask
                self._tickers[symbol] = tk
                out[symbol] = tk
            except Exception:  # noqa: BLE001
                continue
        return out

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        tk = self._tickers.get(symbol)
        if tk and time.time() - tk.ts < 2.0:
            return tk
        try:
            rows = await self.client.tickers(symbol)
            if rows:
                await self.tickers()  # refresh cache once
                return self._tickers.get(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("ticker(%s) failed: %s", symbol, exc)
        return tk

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        return await self.client.klines(symbol, interval, limit)

    async def mark_price(self, symbol: str) -> float:
        tk = self._tickers.get(symbol)
        if tk and tk.fair_price and time.time() - tk.ts < 2.0:
            return tk.fair_price
        try:
            price = await self.client.mark_price(symbol)
            if price:
                if tk:
                    tk.fair_price = price
                    tk.ts = time.time()
                return price
        except Exception:  # noqa: BLE001
            pass
        return tk.fair_price if tk else 0.0

    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None:
        if not self.ws:
            return
        await self.ws.subscribe_klines(symbols, interval)
        await self.ws.subscribe_ticks(symbols)

    # ------------------------------------------------------------------ #
    #  account
    # ------------------------------------------------------------------ #
    async def account(self) -> AccountSnapshot:
        rows = await self.client.assets()
        for row in rows:
            if str(row.get("currency", "")).upper() == "USDT":
                return AccountSnapshot(
                    equity=float(row.get("equity") or 0.0),
                    available=float(row.get("availableBalance") or row.get("availableOpen") or 0.0),
                    unrealized=float(row.get("unrealized") or 0.0),
                    position_margin=float(row.get("positionMargin") or 0.0),
                    currency="USDT",
                    ts=time.time(),
                )
        return AccountSnapshot(ts=time.time())

    async def positions(self) -> List[Position]:
        rows = await self.client.open_positions()
        out: List[Position] = []
        for row in rows:
            try:
                if int(row.get("state", 1)) != 1 or float(row.get("holdVol") or 0) <= 0:
                    continue
                ptype = int(row.get("positionType", 1))
                out.append(
                    Position(
                        symbol=row["symbol"],
                        side=LONG if ptype == 1 else SHORT,
                        hold_vol=float(row.get("holdVol") or 0),
                        open_avg_price=float(row.get("openAvgPrice") or row.get("holdAvgPrice") or 0),
                        leverage=int(row.get("leverage") or 1),
                        unrealized=float(row.get("unRealizedPnl") or 0.0),
                        im=float(row.get("im") or 0.0),
                        liquidate_price=float(row.get("liquidatePrice") or 0),
                        position_id=int(row.get("positionId") or 0),
                        state=int(row.get("state") or 1),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    # ------------------------------------------------------------------ #
    #  trading
    # ------------------------------------------------------------------ #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        key = f"{symbol}:{position_type}"
        if self._leverage_set.get(key) == leverage:
            return True
        ok = await self.client.change_leverage(
            symbol, leverage, position_type=position_type, open_type=self.open_type
        )
        if ok:
            self._leverage_set[key] = leverage
        return ok

    async def open_position(
        self, symbol: str, side: str, qty: float, leverage: int,
        price_hint: float = 0.0, client_id: str = "",
        sl_price: Optional[float] = None, tp_price: Optional[float] = None,
    ) -> OrderResult:
        is_long = side == LONG
        order_side = SIDE_OPEN_LONG if is_long else SIDE_OPEN_SHORT

        # decide attached vs separate protection
        attach = self.stop_mode == "attached" or (self.stop_mode == "auto" and self.attached_supported)
        sl = sl_price if attach else None
        tp = tp_price if attach else None

        async def _send(with_attach: bool) -> OrderResult:
            price = None
            order_type = ORDER_MARKET
            if self.entry_order_type == "ioc_limit" and price_hint:
                buf = self.ioc_buffer_bps / 10_000.0
                price = price_hint * (1 + buf) if is_long else price_hint * (1 - buf)
                order_type = ORDER_IOC
            return await self.client.create_order(
                symbol=symbol,
                vol=qty,
                side=order_side,
                order_type=order_type,
                price=price,
                leverage=leverage,
                open_type=self.open_type,
                reduce_only=False,
                stop_loss_price=sl if with_attach else None,
                take_profit_price=tp if with_attach else None,
                loss_trend=self.trigger_trend,
                profit_trend=self.trigger_trend,
                price_protect=self.price_protect,
                external_oid=client_id or None,
                position_mode=self.position_mode,
            )

        result = await _send(attach)
        if not result.ok and attach:
            err = (result.error or "").lower()
            if any(tok in err for tok in ("stoploss", "takeprofit", "stop_loss", "profit", "3001", "priceprotect", "param")):
                log.warning("attached SL/TP rejected for %s (%s) -> falling back to separate protection", symbol, result.error)
                self.attached_supported = False
                result = await _send(False)
        if result.ok:
            result.raw = dict(result.raw or {})
            result.raw["protection"] = "attached" if (attach and result.ok) else "separate"
        return result

    async def close_position(
        self, symbol: str, side: str, qty: float, reason: str = "", client_id: str = "",
    ) -> OrderResult:
        order_side = SIDE_CLOSE_LONG if side == LONG else SIDE_CLOSE_SHORT
        price = None
        order_type = ORDER_MARKET
        if self.exit_order_type == "ioc_limit":
            tk = await self.ticker(symbol)
            if tk and (tk.bid or tk.ask):
                buf = self.ioc_buffer_bps / 10_000.0
                ref = tk.bid if side == LONG else tk.ask
                price = ref * (1 - buf) if side == LONG else ref * (1 + buf)
                order_type = ORDER_IOC
        return await self.client.create_order(
            symbol=symbol,
            vol=qty,
            side=order_side,
            order_type=order_type,
            price=price,
            open_type=self.open_type,
            reduce_only=True,
            external_oid=client_id or None,
            position_mode=self.position_mode,
        )

    # -- protection management ------------------------------------------ #
    async def arm_protection(
        self, *, symbol: str, side: str, qty: float, sl_price: Optional[float],
        tp_price: Optional[float], entry_order_id: str = "",
    ) -> Dict[str, Any]:
        """Create exchange-side protection when it was not attached at entry."""
        handle: Dict[str, Any] = {"kind": "none", "entry_order_id": entry_order_id, "symbol": symbol}
        is_long = side == LONG
        if entry_order_id:
            try:
                rows = await self.client.tpsl_orders(symbol)
                for row in rows:
                    if str(row.get("orderId")) == str(entry_order_id) and int(row.get("state", 1)) == 1:
                        handle.update(
                            kind="attached", tpsl_id=row.get("id"),
                            stop_price=float(row.get("stopLossPrice") or 0),
                            tp_price=float(row.get("takeProfitPrice") or 0),
                        )
                        break
            except Exception as exc:  # noqa: BLE001
                log.debug("tpsl lookup failed for %s: %s", symbol, exc)
        if handle["kind"] == "attached":
            return handle

        if sl_price:
            res = await self.client.place_plan_order(
                symbol=symbol,
                vol=qty,
                side=SIDE_CLOSE_LONG if is_long else SIDE_CLOSE_SHORT,
                trigger_price=sl_price,
                trigger_type=2 if is_long else 1,        # long: fire when price <= trigger
                order_type=ORDER_MARKET,
                trend=self.trigger_trend,
                reduce_only=True,
                open_type=self.open_type,
                position_mode=self.position_mode,
            )
            if res.ok:
                handle.update(kind="plan", stop_order_id=res.order_id, stop_price=sl_price)
            else:
                log.error("failed to place standalone stop for %s: %s", symbol, res.error)
        if tp_price:
            side_close = SIDE_CLOSE_LONG if is_long else SIDE_CLOSE_SHORT
            res = await self.client.create_order(
                symbol=symbol,
                vol=qty,
                side=side_close,
                order_type=ORDER_LIMIT,
                price=tp_price,
                open_type=self.open_type,
                reduce_only=True,
                external_oid=None,
                position_mode=self.position_mode,
            )
            if res.ok:
                handle["tp_order_id"] = res.order_id
                handle["tp_price"] = tp_price
            else:
                log.error("failed to place fixed TP for %s: %s", symbol, res.error)
        return handle

    async def move_stop(self, *, symbol: str, handle: Dict[str, Any], new_stop_price: float, qty: float) -> bool:
        kind = (handle or {}).get("kind")
        order_id = (handle or {}).get("entry_order_id") if kind == "attached" else (handle or {}).get("stop_order_id")
        if not order_id:
            return False
        try:
            if kind == "attached":
                ok = await self.client.change_attached_stop(
                    symbol=symbol, order_id=str(order_id),
                    stop_loss_price=new_stop_price, loss_trend=self.trigger_trend,
                )
            elif kind == "plan":
                ok = await self.client.modify_plan_order(
                    symbol=symbol, order_id=str(order_id), trigger_price=new_stop_price,
                    execute_price=new_stop_price, order_type=ORDER_MARKET,
                    trigger_type=2, trend=self.trigger_trend,
                )
            else:
                return False
            if ok:
                handle["stop_price"] = new_stop_price
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("move_stop failed on %s (%s): %s", symbol, kind, exc)
            return False

    async def release_stop(self, *, symbol: str, handle: Dict[str, Any]) -> bool:
        """Remove exchange-side protection (used when the local watchdog already flattened)."""
        kind = (handle or {}).get("kind")
        try:
            if kind == "attached" and handle.get("entry_order_id"):
                return await self.client.change_attached_stop(
                    symbol=symbol, order_id=str(handle["entry_order_id"]), stop_loss_price=0.0
                )
            if kind == "plan" and handle.get("stop_order_id"):
                return await self.client.cancel_plan_orders(symbol, [str(handle["stop_order_id"])])
            if handle.get("tp_order_id"):
                return await self.client.cancel_orders([str(handle["tp_order_id"])])
        except Exception as exc:  # noqa: BLE001
            log.debug("release_stop(%s) failed: %s", symbol, exc)
        return False

    async def sync_time(self) -> None:
        await self.client.sync_time()

    def diagnostics(self) -> Dict[str, Any]:
        data = self.client.diagnostics()
        data.update({
            "mode": self.mode,
            "position_mode": self.position_mode,
            "attached_protection": self.attached_supported,
            "leverage_cached": len(self._leverage_set),
        })
        if self.ws:
            data["ws"] = self.ws.diagnostics()
        if self.telemetry:
            data["order_latency_ms"] = self.telemetry.snapshot()
        return data


__all__ = ["LiveBroker", "round_to_step", "Clock", "Callable"]
