"""Risk engine: position sizing, ATR stop-loss, fixed ROI target, stepped
trailing stop, and portfolio-level guards.

Everything the user asked for is implemented verbatim here:

* margin per trade  = equity x ``risk.equity_per_trade_pct`` (8% default)
* leverage          = ``risk.leverage`` (10x default) -> ROI = price_move% x lev
* stop-loss         = entry -/+ ``stoploss.atr_multiplier`` (3x) x ATR
* take-profit       = +``takeprofit.tp_roi_pct`` (200% ROI) on margin
* trailing          = stepped ratchet, activates at +30% ROI, initial stop at
                      +20% ROI, then +10% ROI stop per +10% ROI of peak

ROI <-> price conversions follow the specification exactly::

    ROI%   = (price - entry) / entry * 100 * leverage     (long)
    price  = entry * (1 + ROI / (100 * leverage))         (long)

    ROI%   = (entry - price) / entry * 100 * leverage     (short)
    price  = entry * (1 - ROI / (100 * leverage))         (short)
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from ..exchange.base import LONG, ContractSpec
from ..utils import price_from_roi, roi_from_price, stop_price_from_roi


# --------------------------------------------------------------------------- #
#  sizing
# --------------------------------------------------------------------------- #
@dataclass
class Sizing:
    qty: float = 0.0            # contracts
    margin_usd: float = 0.0
    notional_usd: float = 0.0
    leverage: int = 10
    ok: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"qty": self.qty, "margin_usd": round(self.margin_usd, 4),
                "notional_usd": round(self.notional_usd, 4), "leverage": self.leverage,
                "ok": self.ok, "reason": self.reason}


def size_position(
    equity: float,
    price: float,
    spec: Optional[ContractSpec],
    *,
    equity_pct: float = 8.0,
    leverage: int = 10,
    available: Optional[float] = None,
    min_notional_usd: float = 5.0,
    max_margin_usd: Optional[float] = None,
    max_margin_overshoot: float = 1.30,
) -> Sizing:
    """Margin = equity x pct, notional = margin x leverage, qty in contracts.

    The returned size is checked against the venue's *real* minimum order
    (``spec.min_vol`` contracts / ``spec.min_notional`` quote) and the venue's
    own contract multiplier.  That matters most on small accounts: rounding up
    to a one-lot minimum can otherwise silently multiply the intended risk (a
    $20 account must not accidentally take a $100 position).
    """
    inputs = (equity, price, equity_pct, leverage, min_notional_usd, max_margin_overshoot)
    if (not all(math.isfinite(v) for v in inputs)
            or (available is not None and not math.isfinite(available))
            or (max_margin_usd is not None and not math.isfinite(max_margin_usd))):
        return Sizing(ok=False, reason="non-finite sizing input")
    if equity <= 0 or price <= 0 or equity_pct <= 0 or leverage < 1:
        return Sizing(ok=False, reason="invalid equity/price/risk/leverage")
    if available is not None and available <= 0:
        return Sizing(ok=False, reason="no available balance")
    if spec and (not all(math.isfinite(v) for v in (
            spec.contract_size, spec.vol_unit, spec.min_vol, spec.max_vol, spec.min_notional))
            or spec.contract_size <= 0 or spec.vol_unit <= 0
            or spec.min_vol < 0 or spec.max_vol < 0 or spec.min_notional < 0):
        return Sizing(ok=False, reason="invalid contract specification")
    margin = equity * equity_pct / 100.0
    if available is not None and available > 0:
        margin = min(margin, available * 0.95)     # keep a buffer for fees
    if max_margin_usd:
        margin = min(margin, max_margin_usd)
    notional = margin * leverage
    venue_min_notional = max(float(min_notional_usd or 0.0),
                             float(getattr(spec, "min_notional", 0.0) or 0.0))
    if notional < venue_min_notional:
        return Sizing(margin_usd=margin, notional_usd=notional, leverage=leverage,
                      ok=False, reason=f"notional ${notional:.2f} below exchange minimum ${venue_min_notional:.2f}")

    contract_size = spec.contract_size if spec else 1.0
    vol_unit = spec.vol_unit if spec else 1.0
    raw_qty = notional / (price * contract_size)
    qty = math.floor(raw_qty / vol_unit) * vol_unit if vol_unit > 0 else raw_qty
    rounded_up = False
    if spec and qty < spec.min_vol:
        qty = spec.min_vol
        rounded_up = True
    if spec and spec.max_vol and qty > spec.max_vol:
        qty = spec.max_vol
    if qty <= 0:
        return Sizing(margin_usd=margin, notional_usd=notional, leverage=leverage,
                      ok=False, reason="computed size rounds to zero contracts")

    effective_notional = qty * contract_size * price
    effective_margin = effective_notional / max(1, leverage)
    # Small-account guard: the venue minimum must still fit the risk budget.
    if effective_notional < venue_min_notional:
        return Sizing(margin_usd=margin, notional_usd=effective_notional, leverage=leverage, ok=False,
                      reason=(f"smallest order is ${effective_notional:.2f} notional, below the "
                              f"exchange minimum ${venue_min_notional:.2f}"))
    if rounded_up and effective_margin > margin * max(1.0, max_margin_overshoot):
        return Sizing(margin_usd=effective_margin, notional_usd=effective_notional, leverage=leverage,
                      ok=False,
                      reason=(f"exchange minimum order ({qty:g} contracts ≈ ${effective_notional:,.2f} "
                              f"notional, ${effective_margin:,.2f} margin at {leverage}x) exceeds the "
                              f"${margin:,.2f} risk budget for this trade — raise equity_per_trade_pct, "
                              f"use a smaller-priced symbol, or add funds"))
    if available is not None and effective_margin > available:
        return Sizing(margin_usd=effective_margin, notional_usd=effective_notional, leverage=leverage,
                      ok=False,
                      reason=(f"exchange minimum order needs ${effective_margin:,.2f} margin but only "
                              f"${available:,.2f} is available"))
    return Sizing(
        qty=qty,
        margin_usd=effective_margin,
        notional_usd=effective_notional,
        leverage=leverage,
        ok=True,
        reason="ok",
    )


# --------------------------------------------------------------------------- #
#  stop-loss / take-profit
# --------------------------------------------------------------------------- #
@dataclass
class TradePlan:
    side: str
    entry_price: float
    qty: float
    contract_size: float
    leverage: int
    atr: float
    sl_price: float
    tp_price: float
    sl_roi_pct: float
    tp_roi_pct: float
    margin_usd: float
    notional_usd: float
    notes: Dict[str, Any] = field(default_factory=dict)


def build_plan(
    *,
    side: str,
    entry_price: float,
    atr: float,
    sizing: Sizing,
    contract_size: float,
    cfg,
) -> TradePlan:
    """ATR-based stop + fixed ROI target, with the safety clamps applied."""
    leverage = int(sizing.leverage)
    is_long = side == LONG
    mult = float(cfg.get("stoploss.atr_multiplier", 3.0))
    min_sl_roi = float(cfg.get("stoploss.min_sl_roi_pct", 5.0))
    max_sl_roi = float(cfg.get("stoploss.max_sl_roi_pct", 150.0))
    tp_roi = float(cfg.get("takeprofit.tp_roi_pct", 200.0))

    atr = max(atr, entry_price * 1e-5)
    sl_price = entry_price - mult * atr if is_long else entry_price + mult * atr
    sl_roi = abs(roi_from_price(entry_price, sl_price, leverage, is_long))
    # clamp pathological stops (very small ATR -> tiny stop gets wicked out;
    # very large ATR -> stop so wide the trade has no expectancy)
    if sl_roi < min_sl_roi:
        sl_roi = min_sl_roi
        sl_price = stop_price_from_roi(entry_price, sl_roi, leverage, is_long)
    elif sl_roi > max_sl_roi:
        sl_roi = max_sl_roi
        sl_price = stop_price_from_roi(entry_price, sl_roi, leverage, is_long)

    tp_price = price_from_roi(entry_price, tp_roi, leverage, is_long)
    return TradePlan(
        side=side,
        entry_price=entry_price,
        qty=sizing.qty,
        contract_size=contract_size,
        leverage=leverage,
        atr=atr,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_roi_pct=sl_roi,
        tp_roi_pct=tp_roi,
        margin_usd=sizing.margin_usd,
        notional_usd=sizing.notional_usd,
        notes={"atr_multiplier": mult, "sl_atr_distance": mult * atr},
    )


# --------------------------------------------------------------------------- #
#  stepped trailing stop
# --------------------------------------------------------------------------- #
def trailing_stop_roi(
    peak_roi: float,
    *,
    start: float = 30.0,
    initial_stop: float = 20.0,
    step: float = 10.0,
    stop_step: float = 10.0,
) -> Optional[float]:
    """``floor((peak - start) / step) * stop_step + initial_stop``, or None.

    Examples (defaults):  peak 30 -> 20, 40 -> 30, 50 -> 40, 100 -> 90.
    """
    if peak_roi < start or step <= 0:
        return None
    steps = math.floor((peak_roi - start) / step)
    return initial_stop + steps * stop_step


def trailing_step_index(
    peak_roi: float, *, start: float = 30.0, step: float = 10.0
) -> int:
    if peak_roi < start or step <= 0:
        return -1
    return int(math.floor((peak_roi - start) / step))


@dataclass
class TrailingDecision:
    action: str = "none"       # none | activate | step | chase
    stop_roi: Optional[float] = None
    stop_price: Optional[float] = None
    step_index: int = -1
    reason: str = ""


def evaluate_trailing(
    *,
    entry_price: float,
    mark_price: float,
    peak_roi: float,
    current_stop_roi: Optional[float],
    current_stop_price: Optional[float],
    initial_sl_price: float,
    initial_sl_roi: float,
    leverage: int,
    is_long: bool,
    cfg,
    last_step_index: int = -1,
) -> TrailingDecision:
    """Decide whether the stop should move (ratchet-only, step-quantised)."""
    if not bool(cfg.get("trailing.enabled", True)):
        return TrailingDecision(action="none", reason="trailing disabled")

    start = float(cfg.get("trailing.trail_start_roi", 30.0))
    initial_stop = float(cfg.get("trailing.trail_initial_stop_roi", 20.0))
    step = float(cfg.get("trailing.trail_step_roi", 10.0))
    stop_step = float(cfg.get("trailing.trail_stop_step_roi", 10.0))
    min_move_bps = float(cfg.get("trailing.min_move_bps", 2.0))

    target_roi = trailing_stop_roi(peak_roi, start=start, initial_stop=initial_stop,
                                   step=step, stop_step=stop_step)
    if target_roi is None:
        return TrailingDecision(action="none", reason=f"peak {peak_roi:.1f}% < start {start:.0f}%")

    step_idx = trailing_step_index(peak_roi, start=start, step=step)

    # Ratcheting is judged in *price* space, which is unambiguous for both sides:
    # a long stop may only move up, a short stop only down. (Comparing ROI
    # magnitudes would be wrong, because the initial ATR stop lives below entry
    # for a long while the trailing stop lives above it.)
    current_price = current_stop_price if current_stop_price is not None else initial_sl_price
    baseline_roi = current_stop_roi if current_stop_roi is not None else initial_sl_roi
    target_price = price_from_roi(entry_price, target_roi, leverage, is_long)

    tol = min_move_bps / 10_000.0
    if bool(cfg.get("trailing.ratchet_only", True)):
        if is_long and target_price <= current_price * (1 + tol):
            return TrailingDecision(action="none", stop_roi=baseline_roi, step_index=step_idx,
                                    reason="ratchet: new stop is not higher")
        if not is_long and target_price >= current_price * (1 - tol):
            return TrailingDecision(action="none", stop_roi=baseline_roi, step_index=step_idx,
                                    reason="ratchet: new stop is not lower")
    if bool(cfg.get("trailing.step_only_updates", True)) and step_idx <= last_step_index:
        return TrailingDecision(action="none", stop_roi=baseline_roi, step_index=step_idx,
                                reason="no new trail step")

    # never place a stop through the current market (would fire instantly)
    if is_long and target_price >= mark_price * (1 - tol):
        return TrailingDecision(action="none", stop_roi=baseline_roi, step_index=step_idx,
                                reason="stop would sit at/above market")
    if not is_long and target_price <= mark_price * (1 + tol):
        return TrailingDecision(action="none", stop_roi=baseline_roi, step_index=step_idx,
                                reason="stop would sit at/below market")

    action = "activate" if current_stop_roi is None else "step"
    return TrailingDecision(
        action=action, stop_roi=target_roi, stop_price=target_price, step_index=step_idx,
        reason=f"peak {peak_roi:.1f}% ROI -> stop {target_roi:.0f}% ROI @ {target_price:.8g}",
    )


# --------------------------------------------------------------------------- #
#  portfolio guard
# --------------------------------------------------------------------------- #
@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""
    detail: Optional[Dict[str, Any]] = None


class RiskGuard:
    """Portfolio-level gates evaluated before every new entry."""

    def __init__(self, cfg, db) -> None:
        self.cfg = cfg
        self.db = db
        self.halted = False
        self.halt_reason = ""
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        self.day_start_equity: Optional[float] = None
        self.equity_peak: Optional[float] = None
        self.cooldowns: Dict[str, float] = {}      # symbol -> unix ts until cooldown ends

    # -- persistence ----------------------------------------------------- #
    async def load(self) -> None:
        self.day = await self.db.kv_get("risk.day", self.day) or self.day
        self.day_start_equity = await self.db.kv_get_json("risk.day_start_equity", None)
        self.equity_peak = await self.db.kv_get_json("risk.equity_peak", None)
        self.halted = bool(await self.db.kv_get_json("risk.halted", False))
        self.halt_reason = await self.db.kv_get("risk.halt_reason", "") or ""
        self.cooldowns = await self.db.kv_get_json("risk.cooldowns", {}) or {}

    async def _persist(self) -> None:
        await self.db.kv_set_json("risk.day", self.day)
        await self.db.kv_set_json("risk.day_start_equity", self.day_start_equity)
        await self.db.kv_set_json("risk.equity_peak", self.equity_peak)
        await self.db.kv_set_json("risk.halted", self.halted)
        await self.db.kv_set("risk.halt_reason", self.halt_reason)
        await self.db.kv_set_json("risk.cooldowns", self.cooldowns)

    # -- equity tracking ------------------------------------------------- #
    async def update_equity(self, equity: float) -> None:
        if not math.isfinite(equity):
            await self.halt("non-finite account equity")
            return
        today = time.strftime("%Y-%m-%d", time.gmtime())
        changed = False
        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            changed = True
        if self.day_start_equity is None:
            self.day_start_equity = equity
            changed = True
        if self.equity_peak is None or equity > self.equity_peak:
            self.equity_peak = equity
            changed = True
        if changed:
            await self._persist()

        # circuit breakers
        dd_limit = float(self.cfg.get("risk.max_drawdown_halt_pct", 40.0))
        day_limit = float(self.cfg.get("risk.max_daily_loss_pct", 25.0))
        if self.equity_peak and self.equity_peak > 0:
            dd = (self.equity_peak - equity) / self.equity_peak * 100.0
            if dd >= dd_limit and not self.halted:
                await self.halt(f"max drawdown {dd:.1f}% >= {dd_limit:.0f}% from peak equity")
        if self.day_start_equity and self.day_start_equity > 0:
            day_pnl = (equity - self.day_start_equity) / self.day_start_equity * 100.0
            if day_pnl <= -day_limit and not self.halted:
                await self.halt(f"daily loss {day_pnl:.1f}% <= -{day_limit:.0f}%")

    async def rebaseline(self, equity: float, *, clear_halt: bool = True) -> None:
        """Re-anchor the risk baselines after a deliberate balance change.

        Used by the paper-account reset (and by any manual re-baseline): without
        it, shrinking the book from $1000 to $20 reads as a 98 % drawdown and the
        kill-switch halts every venue.
        """
        self.day_start_equity = float(equity)
        self.equity_peak = float(equity)
        self.day = time.strftime("%Y-%m-%d", time.gmtime())
        if clear_halt:
            self.halted = False
            self.halt_reason = ""
        await self._persist()

    async def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        await self._persist()

    async def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""
        if self.equity_peak is not None:
            self.equity_peak = None      # reset peak so the DD breaker re-baselines
        await self._persist()

    # -- cooldowns ------------------------------------------------------- #
    def cooldown_remaining(self, symbol: str) -> float:
        until = self.cooldowns.get(symbol, 0.0)
        return max(0.0, until - time.time())

    async def register_close(self, symbol: str, pnl: float) -> None:
        minutes = float(self.cfg.get("risk.cooldown_after_loss_min", 30) if pnl <= 0
                        else self.cfg.get("risk.cooldown_after_win_min", 0))
        if minutes > 0:
            self.cooldowns[symbol] = time.time() + minutes * 60.0
            await self._persist()

    # -- entry gate ------------------------------------------------------ #
    async def can_open(
        self, *, symbol: str, equity: float, open_positions: int,
        margin_used: float, available: float, sizing_margin: float,
        symbol_open: bool = False,
    ) -> GuardDecision:
        if not all(math.isfinite(v) for v in (equity, margin_used, available, sizing_margin)):
            return GuardDecision(False, "non-finite risk snapshot")
        if equity <= 0 or margin_used < 0 or sizing_margin <= 0:
            return GuardDecision(False, "invalid equity/margin")
        if sizing_margin > available:
            return GuardDecision(False, "insufficient available balance")
        if self.halted:
            return GuardDecision(False, f"trading halted: {self.halt_reason}")
        max_open = int(self.cfg.get("risk.max_open_positions", 10))
        if open_positions >= max_open:
            return GuardDecision(False, f"max open positions reached ({open_positions}/{max_open})")
        if symbol_open:
            return GuardDecision(False, "position already open for this symbol")
        cooldown = self.cooldown_remaining(symbol)
        if cooldown > 0:
            return GuardDecision(False, f"cooldown active for {symbol} ({cooldown/60:.1f} min left)")
        max_margin_pct = float(self.cfg.get("risk.max_total_margin_pct", 80.0))
        if equity > 0:
            projected = (margin_used + sizing_margin) / equity * 100.0
            if projected > max_margin_pct:
                return GuardDecision(
                    False,
                    f"margin utilisation would reach {projected:.1f}% > {max_margin_pct:.0f}%",
                    detail={"margin_used": margin_used, "equity": equity},
                )
        if available <= 0:
            return GuardDecision(False, "no available balance")
        return GuardDecision(True, "ok")

    def snapshot(self, equity: float) -> Dict[str, Any]:
        day_pct = 0.0
        if self.day_start_equity:
            day_pct = (equity - self.day_start_equity) / self.day_start_equity * 100.0
        dd_pct = 0.0
        if self.equity_peak:
            dd_pct = (self.equity_peak - equity) / self.equity_peak * 100.0
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "day_pnl_pct": round(day_pct, 3),
            "equity_peak": self.equity_peak,
            "drawdown_pct": round(dd_pct, 3),
            "cooldowns": {k: round(max(0.0, v - time.time()) / 60.0, 1) for k, v in self.cooldowns.items()
                          if v > time.time()},
        }
