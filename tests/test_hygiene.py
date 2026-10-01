"""Regression coverage for the October 2 code-hygiene audit (no network/orders)."""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Config
from app.db import Database
from app.exchange.base import AccountSnapshot, ContractSpec, LONG, OrderResult, Position
from app.exchange.paper import PaperBroker
from app.risk.manager import RiskGuard, size_position
from app.trade.executor import Executor
from app.utils import Clock
from tests._util import isolated_config
from tests.test_integration import StaticMarket, make_signal


class ClockRegressionTest(unittest.TestCase):
    def test_sync_uses_request_midpoint_before_response(self):
        clock = Clock()
        with patch('app.utils.time.time', return_value=1000.0):
            # Request sent at 999.8s, response received at 1000s, no clock skew.
            clock.update(999900.0, 200.0)
            self.assertEqual(clock.offset_ms, 0.0)
            self.assertEqual(clock.now_ms(), 1000000)
            clock.update(1000400.0, 200.0)  # exchange is 500ms ahead
            self.assertEqual(clock.offset_ms, 500.0)


class SizingValidationTest(unittest.TestCase):
    def test_invalid_numeric_inputs_fail_closed(self):
        for field in ('equity', 'price', 'equity_pct', 'leverage', 'available'):
            for value in (float('nan'), float('inf'), float('-inf')):
                with self.subTest(field=field, value=value):
                    kwargs = dict(equity=1000., price=100., spec=None, available=1000.)
                    kwargs[field] = value
                    self.assertFalse(size_position(**kwargs).ok)

    def test_invalid_contract_geometry_fails_closed(self):
        for field, value in (('contract_size', 0), ('vol_unit', 0),
                             ('contract_size', float('nan')), ('min_vol', -1)):
            spec = ContractSpec(symbol='TEST')
            setattr(spec, field, value)
            with self.subTest(field=field):
                self.assertFalse(size_position(1000., 100., spec).ok)

    def test_no_available_balance_cannot_size(self):
        self.assertFalse(size_position(1000., 100., None, available=0).ok)


class ConfigurationRegressionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = isolated_config()

    def test_multi_key_semantic_failure_is_atomic(self):
        before = self.cfg.as_dict()
        with self.assertRaises(ValueError):
            self.cfg.set_many({'risk.leverage': 20, 'strategy.ao_fast': 40})
        self.assertEqual(self.cfg.as_dict(), before)

    def test_multi_key_valid_pair_is_checked_together(self):
        self.cfg.set_many({'strategy.ao_fast': 40, 'strategy.ao_slow': 50})
        self.assertEqual(self.cfg.get('strategy.ao_fast'), 40)

    def test_scoped_relation_cannot_bypass_validation(self):
        with self.assertRaises(ValueError):
            self.cfg.set_many({'venues.binance.strategy.ao_slow': 3})
        self.assertIsNone(self.cfg.get('venues.binance.strategy.ao_slow'))

    def test_global_update_checks_existing_venue_override(self):
        self.cfg.set('venues.binance.strategy.ao_fast', 20)
        with self.assertRaises(ValueError):
            self.cfg.set('strategy.ao_slow', 15)
        self.assertEqual(self.cfg.get('strategy.ao_slow'), 34)

    def test_failed_persistence_does_not_change_running_settings(self):
        before = self.cfg.as_dict()
        with patch.object(self.cfg, 'save_overrides', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.cfg.set_many({'risk.leverage': 20})
        self.assertEqual(self.cfg.as_dict(), before)

    def test_all_token_locations_are_redacted(self):
        self.cfg.set_many({'web.api_token': 'global-secret',
                           'venues.binance.web.api_token': 'scoped-secret'})
        self.assertNotIn('global-secret', str(self.cfg.public_dict()))
        self.assertNotIn('scoped-secret', str(self.cfg.public_dict()))

    def test_reset_relation_failure_is_atomic(self):
        self.cfg.set_many({'strategy.ao_fast': 40, 'strategy.ao_slow': 50})
        before, disk = self.cfg.as_dict(), self.cfg.overrides_path.read_text()
        with self.assertRaises(ValueError):
            self.cfg.reset(['strategy.ao_slow'])
        self.assertEqual(self.cfg.as_dict(), before)
        self.assertEqual(self.cfg.overrides_path.read_text(), disk)
        self.cfg.reset(['strategy.ao_fast', 'strategy.ao_slow'])
        self.assertEqual(self.cfg.get('strategy.ao_slow'), 34)

    def test_reset_failed_write_restores_in_memory_overrides(self):
        self.cfg.set('risk.leverage', 20)
        before = self.cfg.as_dict()
        with patch.object(self.cfg, 'save_overrides', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.cfg.reset()
        self.assertEqual(self.cfg.as_dict(), before)
        self.cfg.set('risk.max_open_positions', 2)
        self.assertEqual(self.cfg.get('risk.leverage'), 20)

    def test_fractional_integer_knobs_are_rejected(self):
        for key in ('risk.leverage', 'risk.max_open_positions', 'venues.binance.retry_attempts',
                    'universe.scan_concurrency'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.cfg.set(key, 2.5)

    def test_live_feed_staleness_guard_has_bounded_configuration(self):
        for value in (0, 0.5, 121):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cfg.set('market_data.max_ws_stale_sec', value)
        self.cfg.set('market_data.max_ws_stale_sec', 5)
        self.assertEqual(self.cfg.get('market_data.max_ws_stale_sec'), 5)

    def test_invalid_boolean_values_are_rejected(self):
        for value in ('maybe', [], {}, 2, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.cfg.set('trailing.enabled', value)
        for value in ('false', 'off', '0', 0, False):
            self.assertFalse(self.cfg.set('trailing.enabled', value))
        self.assertTrue(self.cfg.set('trailing.enabled', 'true'))

    def test_removed_noop_knobs_cannot_be_advertised_as_tunable(self):
        for key in ('risk.max_positions_per_symbol', 'risk.risk_recalc_interval_s',
                    'stoploss.use_mark_price_trigger', 'trailing.use_mark_price_for_peak',
                    'trailing.persist_state', 'trailing.replace_stop_on_step', 'target.compounding'):
            with self.subTest(key=key), self.assertRaises(KeyError):
                self.cfg.set(key, True)
        self.assertTrue(self.cfg.get('exchange.set_leverage_on_entry'))
        self.assertIsNone(self.cfg.get('account.set_leverage_on_entry'))


class StartupConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.cfg = isolated_config()

    def test_out_of_range_toml_knob_fails_before_engine_start(self):
        text = self.cfg.path.read_text()
        self.assertIn("leverage = 10                   # 10x", text)
        self.cfg.path.write_text(text.replace(
            "leverage = 10                   # 10x",
            "leverage = 201                  # 10x",
            1,
        ))
        with self.assertRaisesRegex(ValueError, "risk.leverage"):
            Config(self.cfg.path)

    def test_out_of_range_saved_override_fails_closed(self):
        self.cfg.overrides_path.write_text(json.dumps({"risk": {"leverage": 201}}))
        with self.assertRaisesRegex(ValueError, "risk.leverage"):
            Config(self.cfg.path)

    def test_corrupt_or_non_object_override_file_is_not_silently_ignored(self):
        for contents in ("{broken", "[]"):
            with self.subTest(contents=contents):
                self.cfg.overrides_path.write_text(contents)
                with self.assertRaises(ValueError):
                    Config(self.cfg.path)

    def test_stale_or_unknown_override_key_fails_startup(self):
        self.cfg.overrides_path.write_text(json.dumps({"risk": {"not_a_setting": 1}}))
        with self.assertRaisesRegex(ValueError, "unknown or read-only"):
            Config(self.cfg.path)

    def test_invalid_base_cross_field_rules_fail_at_startup(self):
        text = self.cfg.path.read_text()
        text = text.replace("ao_fast = 5                     #", "ao_fast = 40                    #", 1)
        self.cfg.path.write_text(text)
        with self.assertRaisesRegex(ValueError, "strategy.ao_fast"):
            Config(self.cfg.path)

    def test_invalid_universe_weight_shape_fails_at_startup(self):
        text = self.cfg.path.read_text()
        text = text.replace(
            "weights = { turnover = 0.40, volatility = 0.40, momentum = 0.20 }",
            'weights = "not-a-table"', 1,
        )
        self.cfg.path.write_text(text)
        with self.assertRaisesRegex(ValueError, "universe.weights"):
            Config(self.cfg.path)


class MultiMarket(StaticMarket):
    async def contracts(self):
        return {s: replace(self.contract, symbol=s, vol_unit=.01, min_vol=.01)
                for s in ['TEST_USDT'] + [f'TEST{i}_USDT' for i in range(15)]}


class ExecutionRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = isolated_config()
        self.db = Database(Path(self.tmp.name) / 'audit.db')
        self.guard = RiskGuard(self.cfg, self.db)
        self.market = MultiMarket()
        self.broker = PaperBroker(self.market, starting_equity=1000., slippage_bps=0,
                                  price_interval_s=3600)
        await self.broker.start()
        self.executor = Executor(self.broker, self.cfg, self.db, self.guard)

    async def asyncTearDown(self):
        await self.broker.stop()
        self.db.close()
        self.tmp.cleanup()

    async def test_entry_fetches_account_inside_lock(self):
        original = self.broker.account

        async def account():
            self.assertTrue(self.executor._lock.locked())
            return await original()

        with patch.object(self.broker, 'account', side_effect=account) as fetch:
            self.assertIsNotNone(await self.executor.open_from_signal(make_signal()))
            self.assertEqual(fetch.call_count, 1)

    async def test_concurrent_entries_respect_position_limit(self):
        self.cfg.set('risk.max_open_positions', 3)
        results = await asyncio.gather(*[
            self.executor.open_from_signal(make_signal(symbol=f'TEST{i}_USDT'))
            for i in range(12)
        ])
        self.assertEqual(sum(r is not None for r in results), 3)
        self.assertEqual(len(await self.broker.positions()), 3)

    async def test_lagging_exchange_snapshot_still_reserves_local_margin(self):
        self.cfg.set('risk.max_total_margin_pct', 20)
        with patch.object(self.broker, 'positions', new=AsyncMock(return_value=[])), \
                patch.object(self.broker, 'account', new=AsyncMock(return_value=AccountSnapshot(1000., 1000.))):
            results = await asyncio.gather(*[
                self.executor.open_from_signal(make_signal(symbol=f'TEST{i}_USDT'))
                for i in range(4)
            ])
        self.assertEqual(sum(r is not None for r in results), 2)
        self.assertLessEqual(sum(p.margin_usd for p in self.executor.positions.values()), 200.)

    async def test_margin_cap_uses_actual_capped_leverage(self):
        self.cfg.set('risk.max_total_margin_pct', 75)
        self.market.contract.max_leverage = 5
        external = Position(symbol='EXTERNAL', side=LONG, hold_vol=1., open_avg_price=100., leverage=10, im=700.)
        with patch.object(self.broker, 'positions', new=AsyncMock(return_value=[external])):
            self.assertIsNone(await self.executor.open_from_signal(make_signal()))
        self.assertEqual(self.executor.positions, {})

    async def test_unmanaged_exchange_symbol_blocks_duplicate_entry(self):
        external = Position(symbol='TEST_USDT', side=LONG, hold_vol=1., open_avg_price=100., leverage=10, im=10.)
        with patch.object(self.broker, 'positions', new=AsyncMock(return_value=[external])):
            self.assertIsNone(await self.executor.open_from_signal(make_signal()))

    async def test_leverage_failure_prevents_order(self):
        with patch.object(self.broker, 'set_leverage', new=AsyncMock(return_value=False)), \
                patch.object(self.broker, 'open_position', new=AsyncMock()) as order:
            self.assertIsNone(await self.executor.open_from_signal(make_signal()))
            order.assert_not_awaited()

    async def test_missing_spec_prevents_order(self):
        with patch.object(self.broker, 'contracts', new=AsyncMock(return_value={})), \
                patch.object(self.broker, 'open_position', new=AsyncMock()) as order:
            self.assertIsNone(await self.executor.open_from_signal(make_signal()))
            order.assert_not_awaited()

    async def test_account_query_failure_never_uses_cached_equity(self):
        with patch.object(self.broker, 'account', new=AsyncMock(side_effect=OSError('offline'))), \
                patch.object(self.broker, 'open_position', new=AsyncMock()) as order:
            with self.assertRaises(OSError):
                await self.executor.open_from_signal(make_signal())
            order.assert_not_awaited()

    async def test_non_finite_account_prevents_order(self):
        with patch.object(self.broker, 'account', new=AsyncMock(return_value=AccountSnapshot(float('nan'), 1000.))):
            self.assertIsNone(await self.executor.open_from_signal(make_signal()))
        self.assertIsNone(self.guard.equity_peak)

    async def test_non_finite_mark_does_not_poison_state(self):
        pos = await self.executor.open_from_signal(make_signal())
        for mark in (float('nan'), float('inf'), -1., 0.):
            await self.executor.handle_tick(pos.symbol, mark)
        self.assertEqual(pos.peak_roi_pct, 0.)
        self.assertNotIn(pos.symbol, self.executor.marks_cache)

    async def test_accepted_close_keeps_stop_until_flat(self):
        pos = await self.executor.open_from_signal(make_signal())
        with patch.object(self.broker, 'close_position', new=AsyncMock(return_value=OrderResult(ok=True, price=100.))), \
                patch.object(self.broker, 'release_stop', new=AsyncMock()) as release:
            result = await self.executor.close(pos, reason='test')
            self.assertEqual(result, {})
            release.assert_not_awaited()
        self.assertIn(pos.symbol, self.executor.positions)
        self.assertFalse(pos.closed)
        rows = await self.db.get_trades()
        self.assertEqual(rows[0]['status'], 'OPEN')

    async def test_close_query_failure_keeps_stop(self):
        pos = await self.executor.open_from_signal(make_signal())
        with patch.object(self.broker, 'close_position', new=AsyncMock(return_value=OrderResult(ok=True, price=100.))), \
                patch.object(self.broker, 'positions', new=AsyncMock(side_effect=OSError('offline'))), \
                patch.object(self.broker, 'release_stop', new=AsyncMock()) as release:
            self.assertEqual(await self.executor.close(pos, reason='test'), {})
            release.assert_not_awaited()

    async def test_non_finite_equity_halts_without_poisoning_baseline(self):
        await self.guard.update_equity(float('nan'))
        self.assertTrue(self.guard.halted)
        self.assertIsNone(self.guard.equity_peak)

    async def test_tick_burst_uses_one_tracked_worker(self):
        from app.engine import TradingEngine
        engine = TradingEngine(self.cfg, self.db, None)
        engine.running = True
        engine.executor = self.executor
        await self.executor.open_from_signal(make_signal())
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def handle(symbol, mark):
            seen.append(mark)
            entered.set()
            await release.wait()

        with patch.object(self.executor, "handle_tick", side_effect=handle):
            engine._on_tick("TEST_USDT", 100., 0., 0.)
            await entered.wait()
            for mark in range(101, 201):
                engine._on_tick("TEST_USDT", float(mark), 0., 0.)
            engine._on_tick("TEST_USDT", 101., 0., 0.)  # latest tick loses the transient high
            pos = self.executor.positions["TEST_USDT"]
            self.assertAlmostEqual(pos.peak_roi_pct, pos.roi_at(200.))
            self.assertEqual(len(engine._tick_tasks), 1)
            worker = engine._tick_tasks["TEST_USDT"]
            release.set()
            await worker
        self.assertEqual(seen, [100., 101.])
        self.assertEqual(engine._tick_tasks, {})

    async def test_shutdown_waits_for_entry_protection(self):
        from app.engine import TradingEngine
        engine = TradingEngine(self.cfg, self.db, None)
        engine.running = True
        engine.executor = self.executor
        engine.guard = self.guard
        engine.broker = MagicMock(stop=AsyncMock())
        entered, release = asyncio.Event(), asyncio.Event()

        async def open_signal(signal):
            entered.set()
            await release.wait()
            return "protected"

        with patch.object(self.executor, "open_from_signal", side_effect=open_signal):
            caller = asyncio.create_task(engine._try_open(make_signal()))
            engine._tasks.append(caller)
            await entered.wait()
            shutdown = asyncio.create_task(engine.stop())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            engine.broker.stop.assert_not_awaited()
            release.set()
            await shutdown
        self.assertTrue(caller.cancelled())
        self.assertFalse(engine._entry_tasks)
        engine.broker.stop.assert_awaited_once()


class ShutdownRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_server_exit_cancels_watcher_but_runs_cleanup(self):
        import run
        cfg = isolated_config()
        manager = MagicMock()
        manager.stop_all = AsyncMock()
        server = MagicMock()
        server.serve = AsyncMock()
        with patch.object(run, 'load_config', return_value=cfg), \
                patch.object(run, 'VenueManager', return_value=manager), \
                patch.object(run, 'setup_logging'), \
                patch.object(run, 'build_app'), \
                patch('uvicorn.Server', return_value=server), \
                patch.object(asyncio.get_running_loop(), 'add_signal_handler'):
            await run.main_async(argparse.Namespace(port=8089, no_engine=True))
        manager.stop_all.assert_awaited_once()
        manager.close.assert_called_once()
