"""Live broker: real orders on any supported futures venue.

This class is the *only* execution path for real money and it is
venue-agnostic — it talks to the normalized :class:`VenueClient` /
:class:`VenueStream` interfaces, so MEXC, Binance and KuCoin share one
execution state machine (same rules by construction, not by copy-paste).

Execution strategy (low latency + always protected):

1. ``client.market_order`` with a client id (idempotent retries). When the venue
   supports *attached* protection (MEXC), the stop-loss leg rides in the same
   round trip, so the position is never unprotected — not even for one tick.
2. Otherwise (or if the venue rejects the attached legs) the executor arms
   standalone protection immediately: a reduce-only stop order on the exchange
   plus a reduce-only limit order for the fixed +200 % ROI target.
3. Trailing steps move the stop in a *single* modify call — never
   cancel/replace, so there is no unprotected window.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from ..utils import Clock, LatencyTracker
from .base import (
    LONG,
    AccountSnapshot,
    Broker,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)
from .venue import HEDGE, ONEWAY, VenueClient, VenueSpec, VenueStream

log = logging.getLogger("live-broker")


class LiveBroker(Broker):
    """Real-money broker for one venue (defined by the ``client.spec``)."""

    mode = "live"

    def __init__(
        self,
        client: VenueClient,
        ws: Optional[VenueStream] = None,
        *,
        position_mode: int = ONEWAY,
        entry_order_type: str = "market",
        exit_order_type: str = "market",
        ioc_buffer_bps: float = 4.0,
        stop_mode: str = "auto",          # auto | separate (attached is venue-gated)
        telemetry: Optional[LatencyTracker] = None,
    ) -> None:
        self.client = client
        self.ws = ws
        self.spec: VenueSpec = client.spec
        self.name = f"{self.spec.id}-futures"
        self.clock: Clock = client.clock
        self.position_mode = position_mode
        self.entry_order_type = entry_order_type.lower()
        self.exit_order_type = exit_order_type.lower()
        self.ioc_buffer_bps = ioc_buffer_bps
        self.stop_mode = stop_mode
        self.telemetry = telemetry

        self._tickers: Dict[str, Ticker] = {}
        self._leverage_set: Dict[str, int] = {}
        self._cb_kline = None
        self._cb_tick = None
        self._cb_order = None
        # only venues that actually support attached SL/TP may use that path
        self.attached_supported = bool(self.spec.supports_attached_protection) and stop_mode != "separate"

    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        await self.client.start()
        if self.ws:
            self.ws.on_kline = self._on_kline
            self.ws.on_tick = self._on_tick
            self.ws.on_order = self._on_order_push
            await self.ws.start()
        try:
            self.position_mode = int(await self.client.position_mode())
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] could not read position mode (assuming %s): %s",
                        self.spec.id, self.position_mode, exc)

    async def stop(self) -> None:
        if self.ws:
            await self.ws.stop()
        await self.client.close()

    async def set_callbacks(self, on_kline=None, on_tick=None, on_order=None) -> None:
        self._cb_kline = on_kline
        self._cb_tick = on_tick
        self._cb_order = on_order
        # NOTE: the stream must always keep pointing at the broker's normalizing
        # handler — wiring the raw consumer callback straight into the socket
        # would hand venue-specific payloads to the executor.
        if self.ws:
            self.ws.on_order = self._on_order_push

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
        """Normalize the venue's private order push before it reaches the executor."""
        normalized = None
        try:
            normalizer = getattr(self.client, "normalize_order_push", None)
            if normalizer is not None:
                normalized = normalizer(data)
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] order push normalize failed: %s", self.spec.id, exc)
        if self._cb_order:
            self._cb_order(normalized or data)

    # ------------------------------------------------------------------ #
    #  market data
    # ------------------------------------------------------------------ #
    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]:
        return await self.client.contracts(force)

    async def tickers(self) -> Dict[str, Ticker]:
        rows = await self.client.tickers()
        for symbol, tk in rows.items():
            old = self._tickers.get(symbol)
            # keep fresher WS values if we have them
            if old and old.ts > tk.ts - 1.0 and old.fair_price:
                tk.fair_price = old.fair_price
                tk.last = old.last or tk.last
                if old.bid:
                    tk.bid = old.bid
                if old.ask:
                    tk.ask = old.ask
            self._tickers[symbol] = tk
        return dict(self._tickers)

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        tk = self._tickers.get(symbol)
        if tk and time.time() - tk.ts < 2.0:
            return tk
        try:
            rows = await self.client.tickers(symbol)
            if rows:
                for sym, fresh in rows.items():
                    self._tickers[sym] = fresh
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
        return await self.client.account()

    async def positions(self) -> List[Position]:
        return await self.client.positions()

    # ------------------------------------------------------------------ #
    #  trading
    # ------------------------------------------------------------------ #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        key = f"{symbol}:{position_type}"
        if self._leverage_set.get(key) == leverage:
            return True
        ok = await self.client.set_leverage(symbol, leverage, position_type)
        if ok:
            self._leverage_set[key] = leverage
        else:
            log.warning("[%s] could not set %sx leverage on %s", self.spec.id, leverage, symbol)
        return ok

    async def open_position(
        self, symbol: str, side: str, qty: float, leverage: int,
        price_hint: float = 0.0, client_id: str = "",
        sl_price: Optional[float] = None, tp_price: Optional[float] = None,
    ) -> OrderResult:
        attach = self.attached_supported
        # Attached protection is a MEXC capability: the SL/TP legs ride on the
        # entry order. Other venues use the executor's arm_protection step.
        if attach:
            result = await self.client.entry_order_with_protection(
                symbol=symbol, side=side, qty=qty, leverage=leverage,
                price_hint=price_hint, client_id=client_id,
                sl_price=sl_price, tp_price=tp_price,
                entry_order_type=self.entry_order_type, ioc_buffer_bps=self.ioc_buffer_bps,
            )
            if not result.ok and self._looks_like_attachment_error(result.error):
                log.warning("[%s] attached SL/TP rejected for %s (%s) -> separate protection",
                            self.spec.id, symbol, result.error)
                self.attached_supported = False
                result = await self._plain_entry(symbol, side, qty, leverage, price_hint, client_id)
            result.raw = dict(result.raw or {})
            result.raw["protection"] = "attached" if result.ok and attach else "separate"
            return result
        return await self._plain_entry(symbol, side, qty, leverage, price_hint, client_id)

    @staticmethod
    def _looks_like_attachment_error(error: str) -> bool:
        err = (error or "").lower()
        return any(tok in err for tok in (
            "stoploss", "takeprofit", "stop_loss", "profit", "3001", "priceprotect", "param",
        ))

    async def _plain_entry(
        self, symbol: str, side: str, qty: float, leverage: int,
        price_hint: float, client_id: str,
    ) -> OrderResult:
        if self.entry_order_type == "ioc_limit" and price_hint:
            buf = self.ioc_buffer_bps / 10_000.0
            price = price_hint * (1 + buf) if side == LONG else price_hint * (1 - buf)
            res = await self.client.limit_order(
                symbol, side=side, qty=qty, price=price, reduce_only=False, client_id=client_id,
            )
            if not res.ok:      # IOC limit that missed -> fall back to market (never leave a naked signal)
                log.warning("[%s] ioc limit entry rejected on %s (%s) -> market", self.spec.id, symbol, res.error)
                res = await self.client.market_order(
                    symbol, side=side, qty=qty, reduce_only=False, leverage=leverage, client_id=client_id,
                )
            return res
        return await self.client.market_order(
            symbol, side=side, qty=qty, reduce_only=False, leverage=leverage, client_id=client_id,
        )

    async def close_position(
        self, symbol: str, side: str, qty: float, reason: str = "", client_id: str = "",
    ) -> OrderResult:
        if self.exit_order_type == "ioc_limit":
            tk = await self.ticker(symbol)
            if tk and (tk.bid or tk.ask):
                buf = self.ioc_buffer_bps / 10_000.0
                ref = tk.bid if side == LONG else tk.ask
                price = ref * (1 - buf) if side == LONG else ref * (1 + buf)
                res = await self.client.limit_order(
                    symbol, side=side, qty=qty, price=price, reduce_only=True, client_id=client_id,
                )
                if res.ok:
                    return res
                log.warning("[%s] ioc limit exit rejected on %s (%s) -> market", self.spec.id, symbol, res.error)
        return await self.client.market_order(
            symbol, side=side, qty=qty, reduce_only=True, client_id=client_id,
        )

    # -- protection management ------------------------------------------ #
    async def arm_protection(
        self, *, symbol: str, side: str, qty: float, sl_price: Optional[float],
        tp_price: Optional[float], entry_order_id: str = "",
        adopt: bool = False,
    ) -> Dict[str, Any]:
        """Create exchange-side protection when it was not attached at entry.

        With ``adopt=True`` (restart / repair path) the exchange is queried
        first: existing resting legs are *adopted* instead of duplicated, which
        is what keeps a restart from stacking up stops and targets.
        """
        handle: Dict[str, Any] = {
            "kind": "none", "entry_order_id": entry_order_id, "symbol": symbol, "side": side,
        }
        if entry_order_id and self.attached_supported:
            found = await self.client.attached_protection(symbol, entry_order_id)
            if found:
                handle.update(found)
                handle["side"] = side
                return handle
        if adopt:
            try:
                existing = await self.client.open_protection(symbol, side)
            except Exception as exc:  # noqa: BLE001
                existing = None
                log.warning("[%s] protection lookup failed for %s: %s", self.spec.id, symbol, exc)
            if existing:
                adopted = dict(existing)
                adopted.setdefault("kind", "plan")
                adopted["side"] = side
                adopted["adopted"] = True
                if not adopted.get("stop_price") and sl_price:
                    adopted["stop_price"] = sl_price
                if not adopted.get("tp_price") and tp_price:
                    adopted["tp_price"] = tp_price
                log.info("[%s] adopted existing protection on %s (%s)",
                         self.spec.id, symbol, adopted.get("kind"))
                return adopted

        if sl_price:
            res = await self.client.stop_order(
                symbol, side=side, qty=qty, trigger_price=sl_price, reduce_only=True,
            )
            if res.ok:
                handle.update(kind="plan", stop_order_id=res.order_id, stop_price=sl_price, side=side)
                log.info("[%s] protection armed on %s (%s): stop @ %s", self.spec.id, symbol, side, sl_price)
            else:
                log.error("[%s] failed to place stop for %s: %s", self.spec.id, symbol, res.error)
        if tp_price:
            res = await self.client.limit_order(
                symbol, side=side, qty=qty, price=tp_price, reduce_only=True,
            )
            if res.ok:
                handle["tp_order_id"] = res.order_id
                handle["tp_price"] = tp_price
            else:
                log.error("[%s] failed to place fixed TP for %s: %s", self.spec.id, symbol, res.error)
        return handle

    async def move_stop(self, *, symbol: str, handle: Dict[str, Any], new_stop_price: float, qty: float) -> bool:
        kind = (handle or {}).get("kind")
        order_id = (handle or {}).get("entry_order_id") if kind == "attached" else (handle or {}).get("stop_order_id")
        if not order_id or kind == "none":
            return False
        try:
            ok = await self.client.modify_stop(
                symbol, order_id=str(order_id), kind=kind, new_price=new_stop_price, qty=qty,
                side=str((handle or {}).get("side") or LONG), handle=handle,
            )
            if ok:
                handle["stop_price"] = new_stop_price
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] move_stop failed on %s (%s): %s", self.spec.id, symbol, kind, exc)
            return False

    async def release_stop(self, *, symbol: str, handle: Dict[str, Any]) -> bool:
        """Remove *all* exchange-side protection after the position is flat.

        Every leg is cancelled independently and none of them short-circuits the
        others: a resting reduce-only TP left behind would otherwise fire into the
        *next* position opened on the same symbol.
        """
        handle = handle or {}
        kind = handle.get("kind")
        ok = False
        if kind == "attached" and handle.get("entry_order_id"):
            try:
                ok = await self.client.modify_stop(
                    symbol, order_id=str(handle["entry_order_id"]), kind="attached",
                    new_price=0.0, qty=0.0, side=str(handle.get("side") or LONG), handle=handle,
                ) or ok
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not clear attached stop on %s: %s", self.spec.id, symbol, exc)
        elif handle.get("stop_order_id"):
            stop_id = str(handle.get("stop_order_id"))
            try:
                if kind == "attached":
                    ok = await self.client.modify_stop(
                        symbol, order_id=stop_id, kind="attached", new_price=0.0, qty=0.0,
                        side=str(handle.get("side") or LONG), handle=handle,
                    ) or ok
                else:
                    ok = await self.client.cancel_stop(symbol, order_id=stop_id, kind="plan") or ok
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not cancel stop %s on %s: %s", self.spec.id, stop_id, symbol, exc)
        elif handle.get("tpsl_id"):
            stop_id = str(handle.get("tpsl_id"))
            try:
                ok = await self.client.cancel_stop(symbol, order_id=stop_id, kind="attached") or ok
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not cancel tpsl %s on %s: %s", self.spec.id, stop_id, symbol, exc)
        if handle.get("tp_order_id"):
            try:
                ok = await self.client.cancel_order_ids(symbol, [str(handle["tp_order_id"])]) or ok
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] could not cancel resting TP on %s: %s", self.spec.id, symbol, exc)
        return ok

    async def sync_time(self) -> None:
        await self.client.sync_time()

    def diagnostics(self) -> Dict[str, Any]:
        data = dict(self.client.diagnostics())
        data.update({
            "mode": self.mode,
            "name": self.name,
            "venue": self.spec.id,
            "venue_label": self.spec.label,
            "position_mode": self.position_mode,
            "position_mode_label": "hedge" if self.position_mode == HEDGE else "one-way",
            "attached_protection": self.attached_supported,
            "leverage_cached": len(self._leverage_set),
        })
        if self.ws:
            data["ws"] = self.ws.diagnostics()
        if self.telemetry:
            data["order_latency_ms"] = self.telemetry.snapshot()
        return data


__all__ = ["LiveBroker"]
