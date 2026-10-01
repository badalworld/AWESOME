"""Entry uncertainty and failure recovery regressions; no network or real orders."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.db import Database
from app.engine import TradingEngine
from app.exchange.base import OrderResult
from app.exchange.paper import PaperBroker
from app.exchange.venue import normalize_order_status
from app.risk.manager import RiskGuard
from app.trade.executor import Executor
from tests._util import isolated_config
from tests.test_integration import StaticMarket, make_signal


class EntryJournalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = isolated_config()
        self.db = Database(Path(self.tmp.name) / 'journal.db')
        self.guard = RiskGuard(self.cfg, self.db)
        self.broker = PaperBroker(StaticMarket(), starting_equity=1000.,
                                  slippage_bps=0, price_interval_s=3600)
        await self.broker.start()
        self.ex = Executor(self.broker, self.cfg, self.db, self.guard)

    async def asyncTearDown(self):
        await self.broker.stop()
        self.db.close()
        self.tmp.cleanup()

    async def test_intent_is_durable_before_submission_and_removed_after_success(self):
        original = self.broker.open_position

        async def submit(**kwargs):
            intent = await self.ex.pending_entry()
            self.assertEqual(intent['phase'], 'prepared')
            self.assertEqual(intent['client_id'], kwargs['client_id'])
            return await original(**kwargs)

        with patch.object(self.broker, 'open_position', side_effect=submit):
            pos = await self.ex.open_from_signal(make_signal())
        self.assertIsNotNone(pos)
        self.assertIsNone(await self.ex.pending_entry())
        self.assertEqual(self.ex.pending_fills, {})

    async def test_failed_initial_journal_write_prevents_submission(self):
        with patch.object(self.ex, '_save_entry', side_effect=OSError('disk full')), \
                patch.object(self.broker, 'open_position', new=AsyncMock()) as submit:
            with self.assertRaises(OSError):
                await self.ex.open_from_signal(make_signal())
            submit.assert_not_awaited()

    async def test_timeout_does_not_invent_fill_from_request_or_reference_price(self):
        with patch.object(self.broker, 'open_position', new=AsyncMock(return_value=OrderResult(
                ok=True, status='submitted', price=100., vol=8.))), \
                patch.object(self.ex, '_await_fill', new=AsyncMock(return_value=None)), \
                patch.object(self.broker, 'arm_protection', new=AsyncMock()) as protect:
            self.assertIsNone(await self.ex.open_from_signal(make_signal()))
            protect.assert_not_awaited()
        self.assertTrue(self.guard.halted)
        self.assertEqual(self.ex.positions, {})
        self.assertEqual(await self.db.get_open_trades(), [])
        intent = await self.ex.pending_entry()
        self.assertEqual(intent['phase'], 'incident')
        self.assertNotIn('filled_qty', intent)

    async def test_submission_exception_retains_identifier_and_halts(self):
        with patch.object(self.broker, 'open_position', new=AsyncMock(side_effect=TimeoutError())):
            self.assertIsNone(await self.ex.open_from_signal(make_signal()))
        self.assertTrue((await self.ex.pending_entry())['client_id'].startswith('ao-'))
        self.assertTrue(self.guard.halted)
        self.assertEqual(self.ex.pending_fills, {})

    async def test_negative_ack_is_uncertain_not_permission_to_retry(self):
        with patch.object(self.broker, 'open_position', new=AsyncMock(return_value=OrderResult(
                ok=False, error='transport error'))) as submit:
            await self.ex.open_from_signal(make_signal())
            await self.guard.resume()  # even direct guard resume cannot bypass journal
            await self.ex.open_from_signal(make_signal())
            self.assertEqual(submit.await_count, 1)
        self.assertTrue(self.guard.halted)

    async def test_restart_and_resume_cannot_bypass_journal(self):
        await self.db.kv_set_json('execution.pending_entry', {
            'client_id': 'interrupted-order', 'symbol': 'TEST_USDT', 'phase': 'prepared'})
        fresh_guard = RiskGuard(self.cfg, self.db)
        fresh = Executor(self.broker, self.cfg, self.db, fresh_guard)
        await fresh.restore()
        self.assertTrue(fresh_guard.halted)
        engine = TradingEngine(self.cfg, self.db, None)
        engine.executor, engine.guard = fresh, fresh_guard
        self.assertFalse(await engine.resume_risk_halt())
        with patch.object(self.broker, 'open_position', new=AsyncMock()) as submit:
            self.assertIsNone(await fresh.open_from_signal(make_signal()))
            submit.assert_not_awaited()

    async def test_incident_survives_database_close_and_reopen(self):
        with patch.object(self.broker, 'open_position', new=AsyncMock(side_effect=TimeoutError())):
            await self.ex.open_from_signal(make_signal())
        before = await self.ex.pending_entry()
        self.db.close()
        self.db = Database(Path(self.tmp.name) / 'journal.db')
        guard = RiskGuard(self.cfg, self.db)
        await guard.load()
        fresh = Executor(self.broker, self.cfg, self.db, guard)
        await fresh.restore()
        self.assertEqual((await fresh.pending_entry())['client_id'], before['client_id'])
        self.assertTrue(guard.halted)

    async def test_corrupt_journal_fails_closed(self):
        for raw in ('invalid json', '[]', 'null', '{}'):
            await self.db.kv_set('execution.pending_entry', raw)
            self.assertEqual((await self.ex.pending_entry())['phase'], 'corrupt')
            with patch.object(self.broker, 'open_position', new=AsyncMock()) as submit:
                self.assertIsNone(await self.ex.open_from_signal(make_signal()))
                submit.assert_not_awaited()

    async def test_active_partial_is_not_a_final_entry(self):
        with patch.object(self.broker, 'open_position', new=AsyncMock(return_value=OrderResult(
                ok=True, status='partial', filled_vol=2., price=100.))), \
                patch.object(self.ex, '_await_fill', new=AsyncMock(return_value={
                    'status': 'partial', 'filled_qty': 2., 'avg_price': 100.})), \
                patch.object(self.broker, 'arm_protection', new=AsyncMock()) as protect:
            self.assertIsNone(await self.ex.open_from_signal(make_signal()))
            protect.assert_not_awaited()
        self.assertIsNotNone(await self.ex.pending_entry())

    async def test_terminal_canceled_partial_is_managed_at_its_actual_size(self):
        original = self.broker.open_position

        async def partial(**kwargs):
            kwargs['qty'] = 2.
            result = await original(**kwargs)
            result.status = 'canceled'  # remaining quantity is terminally canceled
            return result

        with patch.object(self.broker, 'open_position', side_effect=partial):
            pos = await self.ex.open_from_signal(make_signal())
        self.assertEqual(pos.qty, 2.)
        self.assertAlmostEqual(pos.notional_usd, pos.entry_price * pos.contract_size * 2.)
        self.assertIsNone(await self.ex.pending_entry())

    async def test_terminal_invalid_fill_is_not_guessed(self):
        for price, qty in ((100., 0.), (float('nan'), 2.), (100., float('inf'))):
            with self.subTest(price=price, qty=qty):
                await self.db.kv_delete('execution.pending_entry')
                await self.guard.resume()
                with patch.object(self.broker, 'open_position', new=AsyncMock(return_value=OrderResult(
                        ok=True, status='filled', price=price, filled_vol=qty))):
                    self.assertIsNone(await self.ex.open_from_signal(make_signal()))
                self.assertEqual(await self.db.get_open_trades(), [])

    async def test_early_fill_push_before_rest_ack_is_not_lost(self):
        original = self.broker.open_position

        async def push_first(**kwargs):
            result = await original(**kwargs)
            self.ex.notify_order_push({'client_id': kwargs['client_id'], 'status': 'filled',
                                       'filled_qty': result.filled_vol, 'avg_price': result.price})
            return OrderResult(ok=True, order_id=result.order_id, status='submitted')

        with patch.object(self.broker, 'open_position', side_effect=push_first):
            self.assertIsNotNone(await self.ex.open_from_signal(make_signal()))
        self.assertEqual(self.ex.pending_fills, {})

    async def test_poll_waits_past_active_partial_until_terminal(self):
        class Client:
            order_status = AsyncMock(side_effect=[
                {'status': 'partial', 'filled_qty': 1., 'avg_price': 100.},
                {'status': 'filled', 'filled_qty': 2., 'avg_price': 101.},
            ])
        with patch.object(self.broker, 'client', Client(), create=True):
            fill = await self.ex._await_fill('TEST_USDT', 'o1', 'client1', timeout=2.)
        self.assertEqual(fill['filled_qty'], 2.)
        self.assertEqual(self.ex.pending_fills, {})

    async def test_protection_exception_attempts_close_and_retains_incident(self):
        with patch.object(self.broker, 'arm_protection', new=AsyncMock(side_effect=OSError('offline'))):
            self.assertIsNone(await self.ex.open_from_signal(make_signal()))
        self.assertEqual(await self.broker.positions(), [])
        intent = await self.ex.pending_entry()
        self.assertEqual(intent['emergency_close'], 'flat_observed')
        self.assertTrue(self.guard.halted)  # flat is not full fill/fee reconciliation

    async def test_acknowledged_emergency_close_is_not_reported_as_flat(self):
        with patch.object(self.broker, 'arm_protection', new=AsyncMock(return_value={})), \
                patch.object(self.broker, 'close_position', new=AsyncMock(return_value=OrderResult(ok=True))):
            await self.ex.open_from_signal(make_signal())
        self.assertEqual((await self.ex.pending_entry())['emergency_close'], 'not_confirmed_flat')
        self.assertTrue(await self.broker.positions())

    async def test_failed_emergency_close_keeps_durable_incident(self):
        with patch.object(self.broker, 'arm_protection', new=AsyncMock(return_value={})), \
                patch.object(self.broker, 'close_position', new=AsyncMock(side_effect=OSError('offline'))):
            await self.ex.open_from_signal(make_signal())
        self.assertEqual((await self.ex.pending_entry())['emergency_close'], 'unknown')
        self.assertTrue(self.guard.halted)

    async def test_post_fill_disk_error_still_attempts_emergency_close(self):
        original = self.ex._save_entry

        async def save(intent, **changes):
            if changes.get('phase') == 'acknowledged':
                raise OSError('disk full')
            return await original(intent, **changes)

        with patch.object(self.ex, '_save_entry', side_effect=save):
            await self.ex.open_from_signal(make_signal())
        self.assertEqual(await self.broker.positions(), [])
        self.assertEqual((await self.ex.pending_entry())['emergency_close'], 'flat_observed')

    async def test_order_audit_failure_cannot_prevent_stop_placement(self):
        with patch.object(self.ex, '_record_order', side_effect=OSError('disk full')), \
                patch.object(self.broker, 'close_position', new=AsyncMock()) as close:
            await self.ex.open_from_signal(make_signal())
            close.assert_not_awaited()  # keep established protection
        self.assertEqual((await self.ex.pending_entry())['protection']['kind'], 'paper')
        self.assertTrue(self.broker._protection['TEST_USDT']['stop_price'] > 0)
        self.assertTrue(self.guard.halted)

    async def test_trade_insert_failure_retains_protected_entry_evidence(self):
        with patch.object(self.db, 'insert_trade', side_effect=OSError('disk full')):
            self.assertIsNone(await self.ex.open_from_signal(make_signal()))
        self.assertIn('protection', await self.ex.pending_entry())
        self.assertTrue(await self.broker.positions())

    async def test_cancellation_preserves_intent(self):
        entered = asyncio.Event()

        async def interrupted(**kwargs):
            entered.set()
            await asyncio.Future()

        with patch.object(self.broker, 'open_position', side_effect=interrupted):
            task = asyncio.create_task(self.ex.open_from_signal(make_signal()))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(await self.ex.pending_entry())
        self.assertTrue(self.guard.halted)
        self.assertEqual(self.ex.pending_fills, {})

    async def test_invalid_protection_handles_cannot_pass_as_active_stop(self):
        for handle in ({'kind': 'none', 'stop_price': 97.},
                       {'kind': 'plan', 'stop_price': 97.},
                       {'kind': 'attached', 'tpsl_id': 5, 'stop_price': 0},
                       {'kind': 'paper', 'stop_price': float('nan')}):
            self.assertFalse(self.ex._valid_protection(handle))


class CanonicalFillStateTest(unittest.TestCase):
    def test_nearly_complete_is_still_partial(self):
        self.assertEqual(normalize_order_status(filled_qty=999.5, total_qty=1000., raw_status='OPEN'), 'partial')

    def test_cancel_flag_takes_precedence_over_partial_quantity(self):
        self.assertEqual(normalize_order_status(filled_qty=2., total_qty=8., canceled=True), 'canceled')
