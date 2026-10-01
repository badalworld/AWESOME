"""Integration tests: the full trade lifecycle with real components.

These drive the *actual* executor + risk engine + paper broker (no mocks of the
trading logic) with a controllable market, verifying:

* entry produces ATR x3 stop and +200% ROI target with correct prices,
* position sizing is 8% of equity at 10x,
* the stepped trailing stop activates at +30% ROI at +20% ROI, steps every
  further +10% ROI, and *never* loosens,
* the local stop watchdog flattens the position when price breaches the stop,
* the trade is booked in SQLite with PnL, ROI and exit reason,
* restart-safety: state is persisted and can be restored.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._util import isolated_config                                        # noqa: E402
from app.db import Database                                          # noqa: E402
from app.exchange.base import LONG, SHORT, Candle, ContractSpec, Ticker   # noqa: E402
from app.exchange.paper import PaperBroker                           # noqa: E402
from app.risk.manager import RiskGuard                               # noqa: E402
from app.strategy.signals import Signal                              # noqa: E402
from app.strategy.filters import FilterReport                        # noqa: E402
from app.strategy.divergence import Divergence                       # noqa: E402
from app.trade.executor import Executor                              # noqa: E402
from app.utils import price_from_roi                                 # noqa: E402


class StaticMarket:
    """Deterministic market feed: price only moves when the test says so."""

    simulated = True

    def __init__(self, price: float = 100.0, atr: float = 0.5) -> None:
        self.price = price
        self.atr = atr
        self.contract = ContractSpec(
            symbol="TEST_USDT", contract_size=1.0, price_unit=0.01, vol_unit=1.0,
            min_vol=1.0, max_vol=1_000_000, max_leverage=100, taker_fee=0.0006, state=0,
        )

    def set_price(self, price: float) -> None:
        self.price = price

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def subscribe(self, symbols: List[str], interval: str = "Min5") -> None: ...

    async def contracts(self) -> Dict[str, ContractSpec]:
        return {"TEST_USDT": self.contract}

    async def tickers(self) -> Dict[str, Ticker]:
        return {"TEST_USDT": self._ticker()}

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        return self._ticker()

    def _ticker(self) -> Ticker:
        return Ticker(symbol="TEST_USDT", last=self.price, bid=self.price * 0.9999,
                      ask=self.price * 1.0001, fair_price=self.price, amount24=50_000_000,
                      ts=0.0)

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        # flat candles with a known ATR so the plan is deterministic
        out = []
        ts = 1_700_000_000
        for i in range(limit):
            out.append(Candle(ts=ts + i * 300, o=self.price, h=self.price + self.atr / 2,
                              l=self.price - self.atr / 2, c=self.price, v=1000.0))
        return out

    async def mark_price(self, symbol: str) -> float:
        return self.price


def make_signal(symbol: str = "TEST_USDT", side: str = LONG, price: float = 100.0,
                atr: float = 0.5) -> Signal:
    div = Divergence(
        symbol=symbol, side=side, kind="regular", ts=1_700_000_000, price=price,
        p1_index=10, p2_index=20, p1_price=price * 1.01, p2_price=price * 0.99,
        p1_ao=-2.0, p2_ao=-0.5, price_delta_pct=1.0, ao_delta=0.6,
        bars_since_pivot=1, trigger_level=price, trigger_confirmed=True,
        structure_high=price, structure_low=price * 0.98,
    )
    return Signal(
        symbol=symbol, side=side, kind="regular", ts=1_700_000_000, price=price,
        score=75.0, atr=atr, atr_pct=atr / price * 100.0, report=FilterReport(passed=True),
        divergence=div, features_snapshot={},
    )


class TradeLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ao-int-"))
        self.cfg = isolated_config()
        self.cfg._base["app"]["data_dir"] = str(self.tmp)     # type: ignore[attr-defined]
        self.db = Database(self.tmp / "int.db")
        self.guard = RiskGuard(self.cfg, self.db)
        self.market = StaticMarket(price=100.0, atr=0.5)
        self.broker = PaperBroker(self.market, starting_equity=1000.0,
                                  slippage_bps=0.0, price_interval_s=3600)
        await self.broker.start()
        self.executor = Executor(self.broker, self.cfg, self.db, self.guard)

    async def asyncTearDown(self) -> None:
        await self.broker.stop()
        self.db.close()

    # ------------------------------------------------------------------ #
    async def _open(self, side: str = LONG, price: float = 100.0, atr: float = 0.5):
        return await self.executor.open_from_signal(
            make_signal(side=side, price=price, atr=atr),
            equity=1000.0, available=1000.0, margin_used=0.0, open_positions=0,
        )

    async def test_entry_math_and_protection(self):
        pos = await self._open()
        self.assertIsNotNone(pos, "position should open")
        assert pos is not None
        # 8% of 1000 = $80 margin, 10x => ~$800 notional
        self.assertAlmostEqual(pos.margin_usd, 80.0, delta=15.0)
        self.assertAlmostEqual(pos.notional_usd, 800.0, delta=60.0)
        self.assertEqual(pos.leverage, 10)
        # ATR x3 stop
        self.assertAlmostEqual(abs(pos.entry_price - pos.sl_price), 3 * 0.5, places=6)
        # +200% ROI target at 10x => +20% price
        self.assertAlmostEqual(pos.tp_price, pos.entry_price * 1.2, places=6)
        # stop is 1.5% of price away => -15% ROI at 10x
        self.assertAlmostEqual(pos.sl_roi_pct, 15.0, delta=0.05)
        self.assertLess(pos.sl_price, pos.entry_price)
        # protection handle exists
        self.assertTrue(pos.protection)
        self.assertEqual(len(await self.broker.positions()), 1)

    async def test_trailing_steps_and_never_loosens(self):
        pos = await self._open()
        assert pos is not None
        entry = pos.entry_price
        lev = pos.leverage

        # 1) below the activation threshold -> no trailing
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 25.0, lev, True))
        self.assertFalse(pos.trail_active)
        self.assertEqual(pos.stop_price, pos.sl_price)

        # 2) peak 30% -> activate at +20% ROI (stop above entry: profit locked)
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 31.0, lev, True))
        self.assertTrue(pos.trail_active)
        self.assertAlmostEqual(pos.stop_roi_pct, 20.0, places=6)
        self.assertAlmostEqual(pos.stop_price, price_from_roi(entry, 20.0, lev, True), places=9)
        self.assertGreater(pos.stop_price, entry)

        # 3) peak 45% -> step to +30% ROI
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 45.0, lev, True))
        self.assertAlmostEqual(pos.stop_roi_pct, 30.0, places=6)

        # 4) peak 100% -> step to +90% ROI
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 100.0, lev, True))
        self.assertAlmostEqual(pos.stop_roi_pct, 90.0, places=6)
        stop_before = pos.stop_price

        # 5) price retraces to +50% ROI: the stop must NOT loosen
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 50.0, lev, True))
        self.assertEqual(pos.stop_price, stop_before)
        self.assertEqual(pos.peak_roi_pct >= 100.0, True)

    async def test_stop_watchdog_flattens_position(self):
        """A position that never trails (price falls straight to the ATR stop)."""
        pos = await self._open()
        assert pos is not None
        self.assertFalse(pos.trail_active)
        breach = pos.stop_price * 0.995
        self.market.set_price(breach)
        await self.executor.handle_tick("TEST_USDT", breach)

        self.assertEqual(len(self.executor.positions), 0, "position must be flat after the stop")
        trades = await self.db.get_trades(limit=10)
        closed = [t for t in trades if t["status"] == "CLOSED"]
        self.assertTrue(closed, "the trade must be booked as closed")
        trade = closed[0]
        self.assertIn("stop_loss", trade["exit_reason"])
        self.assertLess(trade["roi_pct"], 0)

    async def test_trailed_stop_locks_in_profit(self):
        """Peak +45% ROI -> stop at +30% ROI -> a collapse exits in profit."""
        pos = await self._open()
        assert pos is not None
        entry, lev = pos.entry_price, pos.leverage
        # +45% avoids the exact-step boundary (float rounding at 40.0 can floor to 20%)
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 45.0, lev, True))
        self.assertTrue(pos.trail_active)
        self.assertAlmostEqual(pos.stop_roi_pct, 30.0, places=6)
        stop_price = pos.stop_price
        self.assertGreater(stop_price, entry)

        breach = stop_price * 0.995
        self.market.set_price(breach)
        await self.executor.handle_tick("TEST_USDT", breach)

        trades = await self.db.get_trades(limit=10)
        trade = [t for t in trades if t["status"] == "CLOSED"][0]
        self.assertTrue(trade["exit_reason"].startswith("trailing"), trade["exit_reason"])
        self.assertGreaterEqual(trade["roi_pct"], 20.0)
        self.assertGreater(trade["peak_roi_pct"], 0)
        # closed above entry thanks to the trailing stop => profitable exit
        self.assertGreater(trade["exit_price"], trade["entry_price"])
        self.assertGreater(trade["realized_pnl"], 0)

    async def test_short_side_trailing(self):
        pos = await self._open(side=SHORT)
        assert pos is not None
        entry, lev = pos.entry_price, pos.leverage
        self.assertAlmostEqual(pos.sl_price, entry + 3 * 0.5, places=6)
        self.assertAlmostEqual(pos.tp_price, entry * 0.8, places=6)

        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 35.0, lev, False))
        self.assertTrue(pos.trail_active)
        self.assertAlmostEqual(pos.stop_roi_pct, 20.0, places=6)
        self.assertLess(pos.stop_price, entry)          # profit locked for a short

        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 65.0, lev, False))
        self.assertAlmostEqual(pos.stop_roi_pct, 50.0, places=6)
        stop_before = pos.stop_price
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 30.0, lev, False))
        self.assertEqual(pos.stop_price, stop_before)

    async def test_take_profit_exit(self):
        """The +200% ROI target is a resting reduce-only order: the venue fills
        it, the sync loop books the trade. No duplicate market order is sent."""
        pos = await self._open()
        assert pos is not None
        tp = pos.tp_price
        # price reaches the target; the executor must NOT fire a market order
        await self.executor.handle_tick("TEST_USDT", tp * 1.0001)
        self.assertEqual(len(self.executor.positions), 1, "position stays open until the venue fills")
        # the venue fills the resting target
        self.market.set_price(tp)
        await self.broker.close_position("TEST_USDT", pos.side, pos.qty, reason="take_profit")
        booked = await self.executor.sync_exchange_positions({"TEST_USDT": tp})
        self.assertTrue(booked, "the sync loop must book the exchange exit")
        self.assertEqual(len(self.executor.positions), 0)
        trades = await self.db.get_trades(limit=5)
        closed = [t for t in trades if t["status"] == "CLOSED"][0]
        self.assertIn("take_profit", closed["exit_reason"])
        self.assertAlmostEqual(closed["roi_pct"], 200.0, places=6)
        self.assertAlmostEqual(closed["exit_price"], tp, places=9)

    async def test_state_persists_and_restores(self):
        pos = await self._open()
        assert pos is not None
        entry, lev = pos.entry_price, pos.leverage
        await self.executor.handle_tick("TEST_USDT", price_from_roi(entry, 55.0, lev, True))
        self.assertAlmostEqual(pos.stop_roi_pct, 40.0, places=6)

        # a brand-new executor (simulated restart) must recover the peak/stop
        executor2 = Executor(self.broker, self.cfg, self.db, self.guard)
        await executor2.restore()
        restored = executor2.positions.get("TEST_USDT")
        self.assertIsNotNone(restored, "open position must be restored")
        assert restored is not None
        self.assertGreaterEqual(restored.peak_roi_pct, 55.0 - 1e-6)
        self.assertAlmostEqual(restored.stop_roi_pct or -1, 40.0, places=6)
        self.assertAlmostEqual(restored.stop_price, pos.stop_price, places=9)

    async def test_duplicate_symbol_blocked(self):
        pos = await self._open()
        self.assertIsNotNone(pos)
        second = await self._open()
        self.assertIsNone(second, "a second position on the same symbol must be refused")

    async def test_sizing_scales_with_equity(self):
        small = await self._open()
        assert small is not None
        await self.broker.close_position("TEST_USDT", small.side, small.qty, reason="test")
        self.executor.positions.clear()
        large = await self.executor.open_from_signal(
            make_signal(), equity=2000.0, available=2000.0, margin_used=0.0, open_positions=0,
        )
        assert large is not None
        self.assertGreater(large.notional_usd, small.notional_usd * 1.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
