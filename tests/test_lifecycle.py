"""Serialized engine lifecycle and startup resource cleanup (offline)."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db import Database
from app.engine import TradingEngine
from tests._util import isolated_config


class LifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = isolated_config()
        self.db = Database(self.cfg.data_dir / 'lifecycle.db')
        self.engine = TradingEngine(self.cfg, self.db, MagicMock())

    async def asyncTearDown(self):
        self.db.close()

    async def test_start_requests_do_not_overlap(self):
        active = peak = 0

        async def start():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1

        with patch.object(self.engine, '_start', side_effect=start):
            await asyncio.gather(*(self.engine.start() for _ in range(5)))
        self.assertEqual(peak, 1)

    async def test_shutdown_waits_until_restart_has_completed(self):
        events = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def start():
            events.append('start')
            entered.set()
            await release.wait()
            events.append('started')

        async def stop():
            events.append('stop')

        with patch.object(self.engine, '_start', side_effect=start), \
                patch.object(self.engine, '_stop', side_effect=stop):
            restart = asyncio.create_task(self.engine.restart())
            await entered.wait()
            shutdown = asyncio.create_task(self.engine.stop())
            await asyncio.sleep(0)
            self.assertEqual(events, ['stop', 'start'])
            self.assertFalse(shutdown.done())
            release.set()
            await asyncio.gather(restart, shutdown)
        self.assertEqual(events, ['stop', 'start', 'started', 'stop'])

    async def test_failed_start_closes_partially_initialized_broker(self):
        broker = MagicMock(stop=AsyncMock())
        self.engine.broker = broker
        with patch.object(self.engine, '_start', side_effect=OSError('restore failed')):
            with self.assertRaises(OSError):
                await self.engine.start()
        broker.stop.assert_awaited_once()
        self.assertFalse(self.engine.running)
        self.assertEqual(self.engine.status_message, 'start failed')

    async def test_cancelled_start_closes_broker(self):
        broker = MagicMock(stop=AsyncMock())
        self.engine.broker = broker
        entered = asyncio.Event()

        async def start():
            entered.set()
            await asyncio.Future()

        with patch.object(self.engine, '_start', side_effect=start):
            task = asyncio.create_task(self.engine.start())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        broker.stop.assert_awaited_once()
        self.assertFalse(self.engine._lifecycle_lock.locked())

    async def test_live_probe_failure_closes_client_before_broker_exists(self):
        self.cfg.set('venues.mexc.mode', 'live')
        self.engine.keystore.load = AsyncMock(return_value=SimpleNamespace(
            complete=lambda needs: True, api_key='dummy', api_secret='dummy', passphrase=''))
        client = MagicMock(start=AsyncMock(), ping=AsyncMock(side_effect=OSError('offline')),
                           close=AsyncMock())
        with patch.object(self.engine, '_assert_live_safety'), \
                patch.object(self.engine, '_make_client', return_value=client):
            with self.assertRaises(RuntimeError):
                await self.engine.start()
        client.close.assert_awaited_once()
        self.assertIsNone(self.engine.broker)

    async def test_synthetic_mode_does_not_construct_unused_http_client(self):
        self.cfg.set('exchange.paper_data_source', 'synthetic')
        self.engine.keystore.load = AsyncMock(return_value=None)
        self.engine.keystore.snapshot.return_value = None
        broker = MagicMock(start=AsyncMock())
        with patch.object(self.engine, '_make_client') as factory, \
                patch('app.engine.PaperBroker', return_value=broker), \
                patch('app.engine.SyntheticFeed'):
            await self.engine._build_broker()
        factory.assert_not_called()


class RestartSupervisorTest(unittest.IsolatedAsyncioTestCase):
    def manager(self, engine):
        from app.manager import VenueManager
        manager = VenueManager.__new__(VenueManager)
        manager.ctx = {'mexc': SimpleNamespace(id='mexc', engine=engine, start_error='')}
        manager._restart_tasks = {}
        manager._shutting_down = False
        manager._shutdown_task = None
        return manager

    async def test_duplicate_restart_requests_share_one_task(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def restart():
            entered.set()
            await release.wait()

        engine = MagicMock(restart=AsyncMock(side_effect=restart), stop=AsyncMock())
        manager = self.manager(engine)
        self.assertTrue(manager.request_restart('mexc'))
        await entered.wait()
        self.assertTrue(manager.request_restart('mexc'))
        self.assertEqual(len(manager._restart_tasks), 1)
        task = manager._restart_tasks['mexc']
        release.set()
        self.assertTrue(await task)
        engine.restart.assert_awaited_once()
        self.assertEqual(manager._restart_tasks, {})

    async def test_shutdown_drains_restart_and_rejects_new_requests(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def restart():
            entered.set()
            await release.wait()

        engine = MagicMock(restart=AsyncMock(side_effect=restart), stop=AsyncMock())
        manager = self.manager(engine)
        manager.request_restart('mexc')
        await entered.wait()
        stopping = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(0)
        engine.stop.assert_not_awaited()
        self.assertFalse(manager.request_restart('mexc'))
        release.set()
        await stopping
        engine.stop.assert_awaited_once()
        self.assertFalse(manager.request_restart('mexc'))

    async def test_restart_failure_is_recorded_and_task_reaped(self):
        engine = MagicMock(restart=AsyncMock(side_effect=OSError('offline')))
        manager = self.manager(engine)
        manager.request_restart('mexc')
        task = manager._restart_tasks['mexc']
        self.assertFalse(await task)
        self.assertEqual(manager.ctx['mexc'].start_error, 'offline')
        self.assertEqual(manager._restart_tasks, {})

    async def test_canceled_shutdown_waiter_does_not_cancel_restart_or_cleanup(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def restart():
            entered.set()
            await release.wait()

        engine = MagicMock(restart=AsyncMock(side_effect=restart), stop=AsyncMock())
        manager = self.manager(engine)
        manager.request_restart('mexc')
        await entered.wait()
        waiter = asyncio.create_task(manager.stop_all())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(manager._restart_tasks['mexc'].cancelled())
        release.set()
        await manager.stop_all()
        await manager.stop_all()  # server finally may request shutdown again
        engine.stop.assert_awaited_once()
