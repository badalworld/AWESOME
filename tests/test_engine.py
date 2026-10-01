"""Engine-level tests: the orchestrator wires broker, strategy, risk and executor
together. These run on the offline synthetic feed, so they need no network and
are fully deterministic in their assertions (values that are random by design —
signal counts — are only checked for internal consistency)."""
from __future__ import annotations

import asyncio
import unittest

from tests._util import isolated_config, temp_dir

from app.db import Database
from app.engine import TradingEngine
from app.keystore import CredentialStore


class _EngineHarness:
    """Shared setup for engine tests (a mixin, so it is not collected twice)."""

    async def asyncSetUp(self) -> None:
        self.tmp = temp_dir("ao-engine-")
        self.cfg = isolated_config(self.tmp / "data")
        # deterministic offline market, no MEXC reachability probing
        self.cfg.set("exchange.paper_data_source", "synthetic")
        # these tests assert against a $1000 book regardless of the shipped
        # default (the shipped config targets a small $20 account)
        self.cfg.set("account.paper_starting_equity", 1000.0)
        self.cfg.set("universe.refresh_sec", 60)
        # relax the discretionary gates so an engineered divergence is actionable;
        # the filters themselves are covered exhaustively in test_core
        for key, value in (
            ("filters.trend.enabled", False),
            ("filters.htf.enabled", False),
            ("filters.chop.min_adx", 0.0),
            ("filters.volatility.min_atr_percentile", 0.0),
            ("filters.momentum.macd_confirm", False),
            ("filters.momentum.rsi_long_max", 100.0),
            ("filters.momentum.rsi_short_min", 0.0),
            ("filters.volume.min_volume_mult", 0.0),
            ("filters.shock.max_candle_atr", 100.0),
            ("filters.orderbook.max_spread_bps", 500.0),
            ("filters.orderbook.min_depth_mult", 0.0),
            ("strategy.min_signal_score", 0.0),
            ("strategy.signal_cooldown_bars", 1),
            ("strategy.min_ao_delta_atr", 0.02),
            ("strategy.require_ao_extreme", False),
            ("strategy.max_pivot_gap", 60),
        ):
            self.cfg.set(key, value)
        self.db = Database(self.cfg.resolve("data") / "bot.db")
        self.keystore = CredentialStore(self.db, self.cfg.resolve("data") / ".secrets")
        self.engine = TradingEngine(self.cfg, self.db, self.keystore)

    async def asyncTearDown(self) -> None:
        await self.engine.stop()

    async def _start(self) -> None:
        await self.engine.start()
        # let the first universe scan + equity snapshot run
        for _ in range(100):
            if self.engine.watchlist:
                break
            await asyncio.sleep(0.1)

class EngineTest(_EngineHarness, unittest.IsolatedAsyncioTestCase):
    """Engine boot, universe scan, metrics, controls, credentials, lifecycle."""

    async def test_engine_boots_with_synthetic_market(self):
        await self._start()
        state = await self.engine.state()
        self.assertTrue(state["engine"]["running"])
        self.assertEqual(state["engine"]["mode"], "paper")
        self.assertEqual(self.engine.market_data_source, "synthetic")
        self.assertGreater(len(self.engine.watchlist), 3)
        self.assertEqual(state["account"]["equity"], 1000.0)
        self.assertIn("risk", state)
        self.assertFalse(state["risk"]["halted"])

    async def test_universe_scan_selects_tradable_symbols(self):
        await self._start()
        scanner = self.engine.universe
        self.assertIsNotNone(scanner)
        selected = scanner.to_dict_list()
        self.assertGreaterEqual(len(selected), 1)
        self.assertTrue(all("score" in row for row in selected))
        # scores are 0-100 percentiles (regression: they used to be raw dollar values)
        self.assertTrue(all(0.0 <= row["score"] <= 100.0 for row in selected))

    async def test_metrics_and_state_survive_without_trades(self):
        await self._start()
        metrics = await self.engine.metrics()
        self.assertEqual(metrics["trades"]["trades"], 0)
        self.assertEqual(metrics["equity"], 1000.0)
        curve = await self.engine.equity_curve(10)
        self.assertIsInstance(curve, list)
        report = await self.engine.compound_report(force=True)
        self.assertIn("simulation", report)
        self.assertIn("prob_hit_target_pct", report["simulation"])

    async def test_trading_toggle_and_risk_halt(self):
        await self._start()
        self.assertFalse(await self.engine.set_trading_enabled(False))
        self.assertFalse(self.engine.trading_enabled)
        self.assertTrue(await self.engine.set_trading_enabled(True))
        state = await self.engine.state()
        self.assertTrue(state["engine"]["trading_enabled"])

    async def test_credentials_are_stored_encrypted_and_masked(self):
        await self._start()
        await self.engine.apply_credentials("test-key-1234567890", "test-secret-value")
        snapshot = self.keystore.masked()
        self.assertTrue(snapshot["configured"])
        self.assertNotIn("test-secret-value", str(snapshot))
        self.assertNotIn("test-key-1234567890", str(snapshot))
        # a live-mode restart must not be attempted while credentials are fake
        self.assertEqual(self.engine.cfg.mode, "paper")
        await self.keystore.clear()
        self.assertFalse(self.keystore.masked()["configured"])

    async def test_stop_is_idempotent_and_releases_tasks(self):
        await self._start()
        await self.engine.stop()
        again = await asyncio.wait_for(self.engine.stop(), timeout=5)
        self.assertIsNone(again)
        state = await self.engine.state()
        self.assertFalse(state["engine"]["running"])

    async def test_paper_position_lifecycle_through_the_engine(self):
        """Open a position through the engine's executor and manage it to a
        full stop-out — the same code path a live signal would take."""
        await self._start()
        symbol = self.engine.watchlist[0]
        ticker = await self.engine.broker.ticker(symbol)
        mark = float(ticker.last if ticker else 0.0)
        self.assertGreater(mark, 0)
        executor = self.engine.executor
        self.assertIsNotNone(executor)

        from app.strategy.divergence import Divergence
        from app.strategy.filters import FilterReport
        from app.strategy.signals import Signal

        div = Divergence(symbol=symbol, side="LONG", kind="regular", ts=0, price=mark,
                         p1_index=1, p2_index=8, p1_price=mark * 0.99, p2_price=mark * 0.98,
                         p1_ao=-1.0, p2_ao=-0.2, price_delta_pct=1.0, ao_delta=0.5,
                         bars_since_pivot=1, trigger_level=mark, trigger_confirmed=True,
                         structure_high=mark, structure_low=mark * 0.97)
        signal = Signal(
            symbol=symbol, side="LONG", kind="regular", ts=0, price=mark, score=75.0,
            atr=mark * 0.004, atr_pct=0.4, report=FilterReport(passed=True, results=[], score=75.0),
            divergence=div,
        )
        pos = await executor.open_from_signal(
            signal, equity=1000.0, available=1000.0, margin_used=0.0, open_positions=0,
        )
        if pos is None:
            self.skipTest(f"paper broker declined the demo order: {executor.last_error}")
        self.assertEqual(len(executor.positions), 1)
        # stop must sit below entry for a long
        self.assertLess(pos.stop_price, pos.entry_price)
        self.assertGreater(pos.tp_price, pos.entry_price)
        # drive the mark through the stop and the watchdog must flatten
        await executor.handle_tick(symbol, pos.stop_price * 0.999)
        self.assertEqual(len(executor.positions), 0)
        trades = await self.db.get_trades(limit=5)
        self.assertEqual(trades[0]["status"], "CLOSED")
        self.assertIn("stop_loss", trades[0]["exit_reason"])


class EngineSignalPathTest(_EngineHarness, unittest.IsolatedAsyncioTestCase):
    """End-to-end: the engine's own analysis path must turn an engineered AO
    divergence into an order, with protection on the correct side, and then
    manage the position (watchdog + trailing)."""

    async def test_analysis_path_opens_and_manages_a_position(self):
        from tests.test_core import zigzag_bullish_divergence

        await self._start()
        symbol = self.engine.watchlist[0]
        feed = self.engine.broker.market.feed
        await feed.stop()                     # freeze the market: deterministic fills

        sim = feed.symbols[symbol]
        historical = list(sim.candles["Min5"])
        engineered = zigzag_bullish_divergence()
        scale = historical[-1].c / engineered[-1].c
        tail = [
            type(engineered[0])(ts=historical[len(historical) - len(engineered) + i].ts,
                                o=c.o * scale, h=c.h * scale, l=c.l * scale, c=c.c * scale, v=c.v)
            for i, c in enumerate(engineered)
        ]
        # keep one extra bar on top: the engine drops the in-progress candle
        extra = type(engineered[0])(ts=tail[-1].ts + 300, o=tail[-1].c, h=tail[-1].c * 1.002,
                                    l=tail[-1].c * 0.999, c=tail[-1].c * 1.001, v=tail[-1].v)
        sim.candles["Min5"] = list(historical[: len(historical) - len(tail)]) + tail + [extra]
        sim.price = extra.c
        await self.engine._refresh_tickers([symbol])      # noqa: SLF001

        await self.engine._analyze_symbol(symbol)          # noqa: SLF001
        executor = self.engine.executor
        self.assertIn(symbol, executor.positions, "the engineered divergence should have opened a position")
        pos = executor.positions[symbol]

        # --- protection is on the correct side ---------------------------- #
        self.assertLess(pos.stop_price, pos.entry_price)
        self.assertGreater(pos.tp_price, pos.entry_price)
        self.assertIsNotNone(pos.protection)
        self.assertGreater(pos.qty, 0)
        # 8% of 1000 equity -> <= $80 margin (floored to whole contracts)
        self.assertLessEqual(pos.margin_usd, 80.0 + 1e-9)
        self.assertGreater(pos.margin_usd, 60.0)

        async def drive(mark: float) -> None:
            """Move the (frozen) market to `mark` and feed the tick through."""
            sim.price = mark
            await executor.handle_tick(symbol, mark)

        # --- trailing: +40% ROI activates it, the stop ratchets up -------- #
        await drive(pos.entry_price * (1 + 40.0 / (100.0 * pos.leverage)))
        self.assertTrue(pos.trail_active, "trailing should be active past +30% ROI")
        self.assertGreaterEqual(pos.stop_roi_pct, 20.0)
        stop_after_step = pos.stop_price
        self.assertGreater(stop_after_step, pos.entry_price)

        # a retrace that stays above the stopped-out level must NOT loosen it
        await drive(pos.entry_price * (1 + 35.0 / (100.0 * pos.leverage)))
        self.assertGreaterEqual(pos.stop_price, stop_after_step)

        # --- breaching the ratcheted stop books the trade at a profit ----- #
        await drive(pos.stop_price * 0.999)
        self.assertNotIn(symbol, executor.positions)
        trades = await self.db.get_trades(limit=5)
        self.assertEqual(trades[0]["status"], "CLOSED")
        self.assertIn(trades[0]["exit_reason"].split(":")[0], ("stop_loss", "trailing"))
        self.assertGreaterEqual(trades[0]["roi_pct"], 20.0)


if __name__ == "__main__":
    unittest.main()
