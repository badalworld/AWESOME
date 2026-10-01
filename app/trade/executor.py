"""Trade executor: turns an approved signal into a protected position and
manages that position tick-by-tick until it is closed.

Lifecycle of one trade
----------------------
1. **Size** — 8% of *current* equity as margin, 10x leverage (compounding).
2. **Gate** — portfolio guard (max 10 open, margin cap, daily loss, cooldown).
3. **Leverage** — set per-symbol leverage on the exchange (cached).
4. **Entry** — market (default) or IOC limit with a slippage cap, tagged with an
   idempotency key so retries can never double-fill.
5. **Protect** — ATR x3 stop + fixed +200% ROI target, preferably attached to the
   entry order itself so protection exists the instant the fill happens.
6. **Manage** — on every mark-price tick: peak ROI -> stepped trailing stop
   (single-call stop modification, never cancel/replace), plus a local watchdog
   that market-closes immediately if price breaches the stop.
7. **Close** — market or exchange-side trigger, PnL booked, cooldown applied,
   state persisted for restart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..exchange.base import LONG, ContractSpec, OrderResult, Position
from ..risk.manager import (
    RiskGuard,
    build_plan,
    evaluate_trailing,
    size_position,
    trailing_step_index,
)
from ..utils import Clock, roi_from_price

log = logging.getLogger("executor")


@dataclass
class ManagedPosition:
    trade_id: int
    trade_uid: str
    symbol: str
    side: str
    qty: float
    contract_size: float
    entry_price: float
    leverage: int
    margin_usd: float
    notional_usd: float
    atr: float
    sl_price: float
    sl_roi_pct: float
    tp_price: float
    tp_roi_pct: float
    opened_at: float
    entry_order_id: str = ""
    protection: Dict[str, Any] = field(default_factory=dict)
    stop_price: float = 0.0
    stop_roi_pct: Optional[float] = None
    peak_roi_pct: float = 0.0
    trough_roi_pct: float = 0.0
    trail_active: bool = False
    trail_step_index: int = -1
    last_trail_ts: float = 0.0
    last_persist_ts: float = 0.0
    signal_id: Optional[int] = None
    fees_usd: float = 0.0
    closed: bool = False
    notes: Dict[str, Any] = field(default_factory=dict)

    # -- helpers -------------------------------------------------------- #
    @property
    def is_long(self) -> bool:
        return self.side == LONG

    def roi_at(self, price: float) -> float:
        return roi_from_price(self.entry_price, price, self.leverage, self.is_long)

    def pnl_at(self, price: float) -> float:
        direction = 1.0 if self.is_long else -1.0
        return (price - self.entry_price) * self.qty * self.contract_size * direction

    def to_state(self) -> Dict[str, Any]:
        return asdict(self)


class Executor:
    def __init__(
        self,
        broker,
        cfg,
        db,
        guard: RiskGuard,
        *,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.broker = broker
        self.cfg = cfg
        self.db = db
        self.guard = guard
        self.clock = clock or Clock()
        self.positions: Dict[str, ManagedPosition] = {}
        self.pending_fills: Dict[str, asyncio.Future] = {}
        self.on_event = on_event
        self.closed_trades_cache: List[Dict[str, Any]] = []
        self.marks_cache: Dict[str, float] = {}
        self.last_error = ""
        self._closing: set = set()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  order-push / fill plumbing
    # ------------------------------------------------------------------ #
    def notify_order_push(self, data: Dict[str, Any]) -> None:
        """Called from the broker when the exchange pushes an order update."""
        try:
            ext = str(data.get("externalOid") or "")
            fut = self.pending_fills.get(ext)
            if fut and not fut.done():
                fut.set_result(data)
        except Exception:  # noqa: BLE001
            pass

    async def _await_fill(
        self, symbol: str, order_id: str, external_oid: str, timeout: float = 2.5
    ) -> Optional[Dict[str, Any]]:
        """Wait for the entry fill: WS push first (fastest), REST poll as backup."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        if external_oid:
            self.pending_fills[external_oid] = fut
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                wait_for = min(0.35, max(0.05, deadline - time.monotonic()))
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=wait_for)
                except asyncio.TimeoutError:
                    pass
                # REST backup (works even if the private WS is down)
                try:
                    if external_oid:
                        order = await self.broker.client.order_by_external_id(symbol, external_oid) \
                            if hasattr(self.broker, "client") else None
                    else:
                        order = None
                    if order is None and order_id and hasattr(self.broker, "client"):
                        order = await self.broker.client.order_by_id(order_id)
                    if order and int(order.get("state", 0)) == 3:
                        return order
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.08)
            return None
        finally:
            self.pending_fills.pop(external_oid, None)

    # ------------------------------------------------------------------ #
    #  open
    # ------------------------------------------------------------------ #
    async def open_from_signal(self, signal, *, equity: float, available: float,
                               margin_used: float, open_positions: int) -> Optional[ManagedPosition]:
        cfg = self.cfg
        async with self._lock:
            symbol = signal.symbol
            if symbol in self.positions:
                log.info("skip %s: already managing a position", symbol)
                return None

            contracts = await self.broker.contracts()
            spec: Optional[ContractSpec] = contracts.get(symbol)
            leverage = int(cfg.get("risk.leverage", 10))
            if spec and spec.max_leverage and leverage > spec.max_leverage:
                leverage = spec.max_leverage

            sizing = size_position(
                equity, signal.price, spec,
                equity_pct=float(cfg.get("risk.equity_per_trade_pct", 8.0)),
                leverage=leverage,
                available=available,
                min_notional_usd=float(cfg.get("risk.min_notional_usd", 5.0)),
            )
            if not sizing.ok:
                log.info("skip %s: %s", symbol, sizing.reason)
                await self._mark_signal(signal, "rejected", f"sizing: {sizing.reason}")
                return None

            decision = await self.guard.can_open(
                symbol=symbol, equity=equity, open_positions=open_positions,
                margin_used=margin_used, available=available,
                sizing_notional=sizing.notional_usd, symbol_open=symbol in self.positions,
            )
            if not decision.allowed:
                log.info("skip %s: %s", symbol, decision.reason)
                await self._mark_signal(signal, "rejected", decision.reason)
                return None

            contract_size = spec.contract_size if spec else 1.0
            plan = build_plan(
                side=signal.side, entry_price=signal.price, atr=signal.atr,
                sizing=sizing, contract_size=contract_size, cfg=cfg,
            )

            # 1) leverage (cached per symbol; hedge mode needs it per side)
            if bool(cfg.get("exchange.set_leverage_on_entry", True)):
                position_type = 1 if signal.side == LONG else 2
                await self.broker.set_leverage(symbol, leverage, position_type)

            # 2) entry (with attached protection when the venue supports it)
            trade_uid = f"ao-{symbol}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            tk = await self.broker.ticker(symbol)
            price_hint = (tk.ask if signal.side == LONG else tk.bid) if tk else signal.price
            if not price_hint or price_hint <= 0:
                price_hint = (tk.mark() if tk else signal.price) or signal.price

            entry_started = time.perf_counter()
            result: OrderResult = await self.broker.open_position(
                symbol=symbol, side=signal.side, qty=sizing.qty, leverage=leverage,
                price_hint=price_hint, client_id=trade_uid,
                sl_price=plan.sl_price, tp_price=plan.tp_price,
            )
            entry_latency = (time.perf_counter() - entry_started) * 1000.0
            await self._record_order(symbol, "entry", signal.side, plan.entry_price or price_hint,
                                     sizing.qty, result, entry_latency, signal.side)
            if not result.ok:
                log.warning("entry failed for %s: %s", symbol, result.error)
                await self._mark_signal(signal, "rejected", f"entry order failed: {result.error}")
                return None

            # 3) confirm fill
            fill = None
            if result.status != "filled":
                fill = await self._await_fill(symbol, result.order_id or "", trade_uid)
            entry_price = plan.entry_price
            filled_qty = sizing.qty
            if fill:
                entry_price = float(fill.get("dealAvgPrice") or fill.get("price") or entry_price)
                filled_qty = float(fill.get("dealVol") or filled_qty) or filled_qty
            elif result.price:
                entry_price = result.price
                filled_qty = result.filled_vol or filled_qty
            if entry_price <= 0:
                entry_price = price_hint
            if filled_qty <= 0:
                filled_qty = sizing.qty

            # recompute the plan on the *actual* entry price
            plan = build_plan(
                side=signal.side, entry_price=entry_price, atr=signal.atr,
                sizing=sizing, contract_size=contract_size, cfg=cfg,
            )

            # 4) protection: attach-aware
            #    "attached" -> the entry order already carries SL/TP legs; we only
            #    look up their id so trailing can modify them in one call.
            #    otherwise -> place standalone stop + reduce-only TP now.
            raw = result.raw if isinstance(result.raw, dict) else {}
            protection_kind = raw.get("protection")
            if protection_kind == "attached":
                handle = await self.broker.arm_protection(
                    symbol=symbol, side=signal.side, qty=filled_qty,
                    sl_price=plan.sl_price, tp_price=plan.tp_price,
                    entry_order_id=result.order_id or "",
                )
            else:
                handle = await self.broker.arm_protection(
                    symbol=symbol, side=signal.side, qty=filled_qty,
                    sl_price=plan.sl_price, tp_price=plan.tp_price,
                    entry_order_id="",
                )
            protection_ok = bool(handle) and (
                handle.get("kind") in ("attached", "plan", "paper")
                or handle.get("stop_price") or handle.get("tp_price")
            )
            if not protection_ok:
                # Never hold an unprotected leveraged position: flatten immediately.
                log.error("protection could not be established for %s -> closing entry", symbol)
                await self.broker.close_position(symbol, signal.side, filled_qty, reason="no_protection")
                await self._mark_signal(signal, "rejected", "could not place stop-loss; position flattened")
                return None

            now = time.time()
            managed = ManagedPosition(
                trade_id=0,
                trade_uid=trade_uid,
                symbol=symbol,
                side=signal.side,
                qty=filled_qty,
                contract_size=contract_size,
                entry_price=entry_price,
                leverage=leverage,
                margin_usd=plan.margin_usd,
                notional_usd=filled_qty * contract_size * entry_price,
                atr=signal.atr,
                sl_price=plan.sl_price,
                sl_roi_pct=plan.sl_roi_pct,
                tp_price=plan.tp_price,
                tp_roi_pct=plan.tp_roi_pct,
                opened_at=now,
                entry_order_id=result.order_id or "",
                protection=handle,
                stop_price=plan.sl_price,
                stop_roi_pct=None,          # None => initial SL still in force
                signal_id=signal.signal_id,
                notes={
                    "score": signal.score,
                    "atr_pct": signal.atr_pct,
                    "divergence": signal.divergence.to_dict(),
                    "filters": signal.report.to_dict(),
                    "entry_latency_ms": round(entry_latency, 2),
                    "protection_kind": handle.get("kind", "unknown"),
                },
            )
            trade_id = await self.db.insert_trade({
                "trade_uid": trade_uid,
                "symbol": symbol,
                "side": signal.side,
                "status": "OPEN",
                "qty": filled_qty,
                "contract_size": contract_size,
                "entry_price": entry_price,
                "leverage": leverage,
                "margin_usd": plan.margin_usd,
                "notional_usd": managed.notional_usd,
                "sl_price": plan.sl_price,
                "tp_price": plan.tp_price,
                "sl_roi_pct": plan.sl_roi_pct,
                "tp_roi_pct": plan.tp_roi_pct,
                "peak_roi_pct": 0.0,
                "trail_active": 0,
                "stop_price": plan.sl_price,
                "stop_order_id": str(handle.get("stop_order_id") or handle.get("tpsl_id") or ""),
                "tp_order_id": str(handle.get("tp_order_id") or ""),
                "entry_order_id": result.order_id or "",
                "atr": signal.atr,
                "realized_pnl": 0.0,
                "fees_usd": 0.0,
                "signal_id": signal.signal_id,
                "opened_at": now,
                "meta": json.dumps(managed.notes, default=str),
            })
            managed.trade_id = trade_id
            self.positions[symbol] = managed
            await self._persist(managed)
            await self._mark_signal(signal, "executed", "position opened", trade_id)
            log.info(
                "OPEN %s %s qty=%.8g @ %.8g | SL %.8g (%.1f%% ROI) | TP %.8g (%.0f%% ROI) | margin $%.2f | %dms",
                signal.side, symbol, filled_qty, entry_price, plan.sl_price, plan.sl_roi_pct,
                plan.tp_price, plan.tp_roi_pct, plan.margin_usd, entry_latency,
            )
            self._emit("position_opened", managed.to_state())
            return managed

    # ------------------------------------------------------------------ #
    #  manage
    # ------------------------------------------------------------------ #
    async def handle_tick(self, symbol: str, mark: float) -> None:
        self.marks_cache[symbol] = mark
        pos = self.positions.get(symbol)
        if pos is None or pos.closed or mark <= 0 or symbol in self._closing:
            return
        try:
            roi = pos.roi_at(mark)
            if roi > pos.peak_roi_pct:
                pos.peak_roi_pct = roi
            if roi < pos.trough_roi_pct:
                pos.trough_roi_pct = roi

            cfg = self.cfg
            # ---- local stop watchdog (always active, fastest possible exit) --
            if bool(cfg.get("stoploss.local_watchdog", True)) and pos.stop_price > 0:
                grace = float(cfg.get("stoploss.watchdog_grace_bps", 3.0)) / 10_000.0
                breached = (
                    mark <= pos.stop_price * (1 - grace) if pos.is_long
                    else mark >= pos.stop_price * (1 + grace)
                )
                if breached:
                    # the stop may have been trailed into profit: report it as
                    # a trailing exit so the stats distinguish "cut loss" from
                    # "gave back part of a winner"
                    reason = "trailing" if pos.trail_active else "stop_loss"
                    await self.close(pos, reason=reason, price=mark, source="watchdog")
                    return

            # ---- stepped trailing stop -------------------------------------- #
            decision = evaluate_trailing(
                entry_price=pos.entry_price,
                mark_price=mark,
                peak_roi=pos.peak_roi_pct,
                current_stop_roi=pos.stop_roi_pct,
                current_stop_price=pos.stop_price,
                initial_sl_price=pos.sl_price,
                initial_sl_roi=pos.sl_roi_pct,
                leverage=pos.leverage,
                is_long=pos.is_long,
                cfg=cfg,
                last_step_index=pos.trail_step_index,
            )
            if decision.action in ("activate", "step") and decision.stop_price:
                moved = await self.broker.move_stop(
                    symbol=symbol, handle=pos.protection,
                    new_stop_price=decision.stop_price, qty=pos.qty,
                )
                if moved:
                    pos.stop_price = decision.stop_price
                    pos.stop_roi_pct = decision.stop_roi
                    pos.trail_step_index = decision.step_index
                    pos.trail_active = True
                    pos.last_trail_ts = time.time()
                    await self.db.update_trade(pos.trade_id, {
                        "stop_price": pos.stop_price,
                        "stop_order_id": str((pos.protection or {}).get("stop_order_id")
                                             or (pos.protection or {}).get("tpsl_id") or ""),
                        "trail_active": 1,
                        "trail_stop_roi": pos.stop_roi_pct,
                        "peak_roi_pct": pos.peak_roi_pct,
                    })
                    log.info(
                        "TRAIL %s stop -> %.1f%% ROI @ %.8g (peak %.1f%%)",
                        symbol, decision.stop_roi, decision.stop_price, pos.peak_roi_pct,
                    )
                    self._emit("trail_moved", {
                        "symbol": symbol, "stop_price": pos.stop_price,
                        "stop_roi": pos.stop_roi_pct, "peak_roi": pos.peak_roi_pct,
                    })

            # ---- take profit ------------------------------------------------ #
            # The +200% ROI target rests on the exchange as a reduce-only limit.
            # When price reaches it we do NOT fire a second market order (that
            # would race the exchange fill): the position-sync loop books the
            # exit as soon as the exchange reports the position gone. We only
            # close locally if there is no resting target order at all.
            tp_hit = mark >= pos.tp_price if pos.is_long else mark <= pos.tp_price
            if tp_hit and bool(cfg.get("takeprofit.close_remainder_on_tp", True)):
                if self._has_resting_tp(pos):
                    pos.notes["tp_pending_since"] = pos.notes.get("tp_pending_since") or time.time()
                else:
                    await self.close(pos, reason="take_profit", price=pos.tp_price, source="mark")

            # ---- periodic state persistence --------------------------------- #
            now = time.time()
            if now - pos.last_persist_ts > 5.0:
                pos.last_persist_ts = now
                await self._persist(pos)
                await self.db.update_trade(pos.trade_id, {"peak_roi_pct": pos.peak_roi_pct})
        except Exception as exc:  # noqa: BLE001
            log.warning("tick handler error for %s: %s", symbol, exc)

    # ------------------------------------------------------------------ #
    #  close
    # ------------------------------------------------------------------ #
    def _has_resting_tp(self, pos: ManagedPosition) -> bool:
        handle = pos.protection or {}
        return bool(handle.get("tp_order_id") or handle.get("tp_price"))

    async def close(self, pos: ManagedPosition, *, reason: str, price: Optional[float] = None,
                    source: str = "bot") -> Dict[str, Any]:
        """Flatten a position at market and book the result.

        Handles the two races that matter in production:

        * the exchange-side stop/target may fill while our market order is in
          flight (or before it) -> the market order is rejected and we must book
          the exit at the exchange's price instead of losing track of a trade;
        * close orders can fail transiently -> one immediate retry, then a
          position query to decide whether the position is genuinely still open.
        """
        if pos.symbol in self._closing:
            return {}
        self._closing.add(pos.symbol)
        try:
            started = time.perf_counter()
            result = await self.broker.close_position(
                pos.symbol, pos.side, pos.qty, reason=reason,
                client_id=f"close-{pos.trade_uid}",
            )
            latency = (time.perf_counter() - started) * 1000.0
            fallback_price = price or self._last_known_price(pos)
            if result.ok:
                exit_price = result.price or fallback_price or pos.entry_price
                await self.broker.release_stop(symbol=pos.symbol, handle=pos.protection)
                await self._record_order(pos.symbol, "close", pos.side, exit_price, pos.qty,
                                         result, latency, reason)
                return await self._book_close(pos, exit_price, reason, source, latency)

            # ---- the order failed: was the position already closed elsewhere? --
            log.warning("close order failed for %s (%s) — checking position state", pos.symbol, result.error)
            still_open = await self._still_open(pos.symbol)
            if not still_open:
                exit_price = self._estimate_exchange_exit(pos, fallback_price)
                log.info("%s was already flat (exchange-side exit) — booking at %.8g", pos.symbol, exit_price)
                await self._record_order(pos.symbol, "close", pos.side, exit_price, pos.qty,
                                         result, latency, reason + ":already-flat")
                return await self._book_close(pos, exit_price, reason, f"{source}-exchange", latency)

            await asyncio.sleep(0.15)
            retry = await self.broker.close_position(pos.symbol, pos.side, pos.qty,
                                                     reason=reason + ":retry")
            latency += (time.perf_counter() - started) * 1000.0
            if retry.ok:
                exit_price = retry.price or fallback_price or pos.entry_price
                await self.broker.release_stop(symbol=pos.symbol, handle=pos.protection)
                await self._record_order(pos.symbol, "close", pos.side, exit_price, pos.qty,
                                         retry, latency, reason + ":retry")
                return await self._book_close(pos, exit_price, reason, source + "-retry", latency)
            self.last_error = f"close failed for {pos.symbol}: {retry.error}"
            log.error(self.last_error)
            return {}
        finally:
            self._closing.discard(pos.symbol)

    def _last_known_price(self, pos: ManagedPosition) -> Optional[float]:
        return self.marks_cache.get(pos.symbol)

    async def _still_open(self, symbol: str) -> bool:
        try:
            positions = await self.broker.positions()
            return any(p.symbol == symbol and p.hold_vol > 0 for p in positions)
        except Exception:  # noqa: BLE001
            return True     # assume open on query failure (safe: we retry the close)

    def _estimate_exchange_exit(self, pos: ManagedPosition, fallback: Optional[float]) -> float:
        """Best estimate of the price a resting stop/target filled at."""
        mark = fallback or pos.entry_price
        if pos.tp_price and ((pos.is_long and mark >= pos.tp_price) or (not pos.is_long and mark <= pos.tp_price)):
            return pos.tp_price
        if pos.stop_price and ((pos.is_long and mark <= pos.stop_price) or (not pos.is_long and mark >= pos.stop_price)):
            return pos.stop_price
        return mark

    async def _book_close(self, pos: ManagedPosition, exit_price: float, reason: str,
                          source: str, latency_ms: float = 0.0) -> Dict[str, Any]:
        """Single booking path: DB row, cooldown, caches, events."""
        if pos.closed:
            return {}
        fee_rate = await self._taker_fee(pos.symbol)
        fees = (pos.entry_price + exit_price) * pos.qty * pos.contract_size * fee_rate
        pnl = pos.pnl_at(exit_price) - fees
        roi = pos.roi_at(exit_price)
        closed_at = time.time()
        await self.db.update_trade(pos.trade_id, {
            "status": "CLOSED",
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "fees_usd": fees,
            "roi_pct": roi,
            "peak_roi_pct": pos.peak_roi_pct,
            "exit_reason": f"{reason}:{source}",
            "closed_at": closed_at,
            "trail_active": 1 if pos.trail_active else 0,
            "trail_stop_roi": pos.stop_roi_pct,
            "meta": json.dumps({**(pos.notes or {}), "close_latency_ms": round(latency_ms, 2)}, default=str),
        })
        await self.db.kv_delete(self._state_key(pos.symbol))
        pos.closed = True
        self.positions.pop(pos.symbol, None)
        self.closed_trades_cache.insert(0, {
            "symbol": pos.symbol, "side": pos.side, "pnl": round(pnl, 4),
            "roi": round(roi, 2), "reason": reason, "closed_at": closed_at,
        })
        del self.closed_trades_cache[50:]
        await self.guard.register_close(pos.symbol, pnl)
        log.info(
            "CLOSE %s %s @ %.8g | pnl $%.4f | ROI %.2f%% | peak %.2f%% | reason %s:%s | %.0fms",
            pos.side, pos.symbol, exit_price, pnl, roi, pos.peak_roi_pct, reason, source, latency_ms,
        )
        self._emit("position_closed", {
            "symbol": pos.symbol, "side": pos.side, "exit_price": exit_price,
            "pnl": pnl, "roi_pct": roi, "reason": reason, "source": source,
            "peak_roi_pct": pos.peak_roi_pct, "closed_at": closed_at,
        })
        return {"ok": True, "pnl": pnl, "roi_pct": roi, "exit_price": exit_price, "reason": reason}

    async def sync_exchange_positions(self, marks: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
        """Reconcile: book any position the exchange has already flattened.

        This catches exchange-side stop/target fills, liquidations and manual
        closes from the app — the trade must never stay "OPEN" in our books.
        """
        if not self.positions:
            return []
        marks = marks or {}
        try:
            live = {p.symbol for p in await self.broker.positions() if p.hold_vol > 0}
        except Exception as exc:  # noqa: BLE001
            log.debug("position sync failed: %s", exc)
            return []
        booked = []
        for symbol, pos in list(self.positions.items()):
            if symbol in live or pos.closed or symbol in self._closing:
                continue
            mark = marks.get(symbol) or pos.entry_price
            exit_price = self._estimate_exchange_exit(pos, mark)
            reason = "take_profit" if abs(exit_price - pos.tp_price) < 1e-12 else (
                "stop_loss" if abs(exit_price - pos.stop_price) < 1e-12 else "exchange_exit")
            log.info("position %s vanished on the exchange -> booking %s @ %.8g",
                     symbol, reason, exit_price)
            res = await self._book_close(pos, exit_price, reason, "exchange-sync")
            if res:
                booked.append(res)
        return booked

    async def book_external_exit(self, symbol: str, reason: str, exit_price: float,
                                 source: str = "exchange") -> Dict[str, Any]:
        """Book a position that the venue closed on its own (paper engine, stop,
        target, liquidation, manual app close)."""
        pos = self.positions.get(symbol)
        if pos is None or pos.closed:
            return {}
        return await self._book_close(pos, float(exit_price), reason, source)

    async def force_close_symbol(self, symbol: str, reason: str = "manual") -> Dict[str, Any]:
        pos = self.positions.get(symbol)
        if not pos:
            return {}
        return await self.close(pos, reason=reason, source="manual")

    async def close_all(self, reason: str = "flatten_all") -> List[Dict[str, Any]]:
        out = []
        for symbol in list(self.positions.keys()):
            try:
                res = await self.force_close_symbol(symbol, reason=reason)
                if res:
                    out.append(res)
            except Exception as exc:  # noqa: BLE001
                log.error("flatten %s failed: %s", symbol, exc)
        return out

    # ------------------------------------------------------------------ #
    #  persistence & restore
    # ------------------------------------------------------------------ #
    @staticmethod
    def _state_key(symbol: str) -> str:
        return f"position_state:{symbol}"

    async def _persist(self, pos: ManagedPosition) -> None:
        await self.db.kv_set_json(self._state_key(pos.symbol), pos.to_state())

    async def restore(self) -> None:
        """Rebuild in-memory state after a restart and repair any drift."""
        open_trades = await self.db.get_open_trades()
        exchange_positions: Dict[str, Position] = {p.symbol: p for p in await self.broker.positions()}
        contracts = await self.broker.contracts()

        # 1) restore known positions
        for row in open_trades:
            symbol = row["symbol"]
            state = await self.db.kv_get_json(self._state_key(symbol))
            live = exchange_positions.get(symbol)
            if live is None:
                # position closed while the bot was down (stop/target/liquidation)
                exit_price = row.get("exit_price") or 0.0
                pnl = 0.0
                if exit_price:
                    pnl = (exit_price - row["entry_price"]) * row["qty"] * row.get("contract_size", 1) \
                        * (1 if row["side"] == LONG else -1)
                await self.db.update_trade(row["id"], {
                    "status": "CLOSED", "closed_at": time.time(),
                    "exit_reason": "closed_while_offline", "realized_pnl": pnl,
                    "exit_price": exit_price or row["entry_price"],
                })
                await self.db.kv_delete(self._state_key(symbol))
                log.warning("trade %s (%s) was closed while the bot was offline", row["id"], symbol)
                continue
            managed = self._from_state(state, row, live, contracts)
            self.positions[symbol] = managed
            # repair protection if the exchange has no resting stop
            try:
                handle = await self.broker.arm_protection(
                    symbol=symbol, side=managed.side, qty=managed.qty,
                    sl_price=managed.stop_price or managed.sl_price,
                    tp_price=managed.tp_price,
                    entry_order_id=managed.entry_order_id,
                )
                managed.protection = handle or managed.protection
            except Exception as exc:  # noqa: BLE001
                log.warning("protection repair failed for %s: %s", symbol, exc)
            log.info("restored position %s %s entry=%.8g peak=%.1f%% stop=%s",
                     managed.side, symbol, managed.entry_price, managed.peak_roi_pct, managed.stop_price)

        # 2) adopt orphans (position exists on the exchange, not in our DB)
        for symbol, live in exchange_positions.items():
            if symbol in self.positions:
                continue
            spec = contracts.get(symbol)
            atr_price = 0.0
            try:
                candles = await self.broker.klines(symbol, "Min5", 60)
                from ..strategy import indicators as ind

                atr_series = [v for v in ind.atr([c.h for c in candles], [c.l for c in candles],
                                                 [c.c for c in candles], 14) if v is not None]
                atr_price = atr_series[-1] if atr_series else 0.0
            except Exception:  # noqa: BLE001
                pass
            if atr_price <= 0:
                atr_price = live.open_avg_price * 0.01
            mult = float(self.cfg.get("stoploss.atr_multiplier", 3.0))
            sl = live.open_avg_price - mult * atr_price if live.side == LONG else live.open_avg_price + mult * atr_price
            tp = live.open_avg_price * (1 + float(self.cfg.get("takeprofit.tp_roi_pct", 200))
                                        / (100 * max(1, live.leverage))) if live.side == LONG else \
                live.open_avg_price * (1 - float(self.cfg.get("takeprofit.tp_roi_pct", 200))
                                       / (100 * max(1, live.leverage)))
            uid = f"adopted-{symbol}-{int(time.time())}"
            trade_id = await self.db.insert_trade({
                "trade_uid": uid, "symbol": symbol, "side": live.side, "status": "OPEN",
                "qty": live.hold_vol, "contract_size": (spec.contract_size if spec else 1.0),
                "entry_price": live.open_avg_price, "leverage": live.leverage,
                "margin_usd": live.im, "notional_usd": live.hold_vol * (spec.contract_size if spec else 1) * live.open_avg_price,
                "sl_price": sl, "tp_price": tp, "sl_roi_pct": 0.0, "tp_roi_pct": float(self.cfg.get("takeprofit.tp_roi_pct", 200)),
                "trail_active": 0, "stop_price": sl, "atr": atr_price,
                "realized_pnl": 0.0, "fees_usd": 0.0, "opened_at": time.time(),
                "meta": json.dumps({"adopted": True}),
            })
            handle = await self.broker.arm_protection(
                symbol=symbol, side=live.side, qty=live.hold_vol, sl_price=sl, tp_price=tp,
            )
            self.positions[symbol] = ManagedPosition(
                trade_id=trade_id, trade_uid=uid, symbol=symbol, side=live.side,
                qty=live.hold_vol, contract_size=(spec.contract_size if spec else 1.0),
                entry_price=live.open_avg_price, leverage=live.leverage,
                margin_usd=live.im, notional_usd=live.hold_vol * live.open_avg_price,
                atr=atr_price, sl_price=sl, sl_roi_pct=0.0, tp_price=tp,
                tp_roi_pct=float(self.cfg.get("takeprofit.tp_roi_pct", 200)),
                opened_at=time.time(), protection=handle, stop_price=sl,
                notes={"adopted": True},
            )
            log.warning("adopted orphan position %s %s qty=%s entry=%.8g",
                        live.side, symbol, live.hold_vol, live.open_avg_price)

    def _from_state(self, state: Optional[Dict[str, Any]], row: Dict[str, Any],
                    live: Position, contracts: Dict[str, ContractSpec]) -> ManagedPosition:
        spec = contracts.get(row["symbol"])
        base = {
            "trade_id": row["id"],
            "trade_uid": row["trade_uid"],
            "symbol": row["symbol"],
            "side": row["side"],
            "qty": live.hold_vol or row["qty"],
            "contract_size": row.get("contract_size") or (spec.contract_size if spec else 1.0),
            "entry_price": live.open_avg_price or row["entry_price"],
            "leverage": live.leverage or row["leverage"],
            "margin_usd": live.im or row.get("margin_usd") or 0.0,
            "notional_usd": row.get("notional_usd") or 0.0,
            "atr": row.get("atr") or 0.0,
            "sl_price": row.get("sl_price") or 0.0,
            "sl_roi_pct": row.get("sl_roi_pct") or 0.0,
            "tp_price": row.get("tp_price") or 0.0,
            "tp_roi_pct": row.get("tp_roi_pct") or 0.0,
            "opened_at": row.get("opened_at") or time.time(),
            "entry_order_id": row.get("entry_order_id") or "",
            "stop_price": row.get("stop_price") or row.get("sl_price") or 0.0,
            "stop_roi_pct": row.get("trail_stop_roi"),
            "peak_roi_pct": row.get("peak_roi_pct") or 0.0,
            "trail_active": bool(row.get("trail_active")),
            "signal_id": row.get("signal_id"),
        }
        if state:
            state = dict(state)
            state.update({k: v for k, v in base.items() if v not in (None, "")})
            try:
                state["protection"] = state.get("protection") or {}
                return ManagedPosition(**{k: v for k, v in state.items()
                                          if k in ManagedPosition.__dataclass_fields__})
            except Exception as exc:  # noqa: BLE001
                log.warning("state restore fell back to DB row for %s: %s", row["symbol"], exc)
        managed = ManagedPosition(**base)
        managed.trail_step_index = trailing_step_index(managed.peak_roi_pct)
        return managed

    # ------------------------------------------------------------------ #
    async def _mark_signal(self, signal, status: str, reason: str = "", trade_id: Optional[int] = None) -> None:
        if signal is None or signal.signal_id is None:
            return
        patch = {"status": status, "reason": reason[:500]}
        if trade_id:
            patch["trade_id"] = trade_id
        await self.db.update_signal(signal.signal_id, patch)

    async def _record_order(self, symbol: str, kind: str, side: str, price: float, vol: float,
                            result: OrderResult, latency_ms: float, note: str = "") -> None:
        await self.db.insert_order({
            "ts": time.time(), "symbol": symbol, "kind": kind, "side": side,
            "price": price, "vol": vol,
            "exchange_order_id": result.order_id,
            "status": "ok" if result.ok else "error",
            "latency_ms": round(latency_ms, 2),
            "error": result.error or "",
            "request": json.dumps({"note": note, **(result.raw or {}).get("request", {})}, default=str)[:4000],
            "response": json.dumps((result.raw or {}).get("response", {}), default=str)[:4000],
        })

    async def _taker_fee(self, symbol: str) -> float:
        try:
            spec = (await self.broker.contracts()).get(symbol)
            return spec.taker_fee if spec else 0.0006
        except Exception:  # noqa: BLE001
            return 0.0006

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(event, payload)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def live_positions(self, marks: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
        out = []
        marks = marks or {}
        for symbol, pos in self.positions.items():
            mark = marks.get(symbol) or pos.entry_price
            roi = pos.roi_at(mark)
            out.append({
                "trade_id": pos.trade_id,
                "trade_uid": pos.trade_uid,
                "symbol": symbol,
                "side": pos.side,
                "qty": pos.qty,
                "entry_price": pos.entry_price,
                "mark_price": mark,
                "leverage": pos.leverage,
                "margin_usd": round(pos.margin_usd, 4),
                "notional_usd": round(pos.notional_usd, 4),
                "pnl": round(pos.pnl_at(mark), 4),
                "roi_pct": round(roi, 2),
                "peak_roi_pct": round(pos.peak_roi_pct, 2),
                "trough_roi_pct": round(pos.trough_roi_pct, 2),
                "sl_price": pos.sl_price,
                "sl_roi_pct": round(pos.sl_roi_pct, 2),
                "tp_price": pos.tp_price,
                "tp_roi_pct": round(pos.tp_roi_pct, 2),
                "stop_price": pos.stop_price,
                "stop_roi_pct": pos.stop_roi_pct,
                "trail_active": pos.trail_active,
                "opened_at": pos.opened_at,
                "age_s": round(time.time() - pos.opened_at, 1),
                "protection": pos.protection.get("kind") if isinstance(pos.protection, dict) else None,
                "notes": pos.notes,
                "next_trail_at_roi": self._next_trail_level(pos),
            })
        return out

    def _next_trail_level(self, pos: ManagedPosition) -> Optional[float]:
        if not bool(self.cfg.get("trailing.enabled", True)):
            return None
        start = float(self.cfg.get("trailing.trail_start_roi", 30.0))
        step = float(self.cfg.get("trailing.trail_step_roi", 10.0))
        if pos.peak_roi_pct < start:
            return start
        idx = trailing_step_index(pos.peak_roi_pct, start=start, step=step)
        return start + (idx + 1) * step
