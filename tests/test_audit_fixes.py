"""Regression tests for the pre-live audit (see docs/AUDIT_2026-10.md).

Each test here pins a defect that was found by auditing the money path, so the
fix can never silently regress:

* ``GuardDecision`` used to be constructed with three positional arguments
  (``TypeError`` the moment the margin-utilisation gate tripped),
* entry/exit fill confirmation used MEXC-only client methods and MEXC-only
  field names (wrong entry price on Binance/KuCoin),
* ``release_stop`` early-returned and could leave a resting reduce-only TP
  behind that would fire into the *next* position,
* ``arm_protection`` duplicated stops/targets on every restart,
* ``size_position`` rounded **up** to the venue minimum without checking the risk
  budget (a $20 account could be handed a $100 position),
* live mode could be armed behind an unauthenticated dashboard.
"""
from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List

from tests._util import isolated_config  # noqa: E402

from app.db import Database  # noqa: E402
from app.engine import TradingEngine  # noqa: E402
from app.exchange.base import LONG, ContractSpec  # noqa: E402
from app.exchange.binance import BinanceClient  # noqa: E402
from app.exchange.kucoin import KuCoinClient  # noqa: E402
from app.exchange.live import LiveBroker  # noqa: E402
from app.exchange.mexc import MeXCClient, MexcVenueClient  # noqa: E402
from app.exchange.paper import PaperBroker, SyntheticMarketAdapter  # noqa: E402
from app.exchange.synthetic import SyntheticFeed  # noqa: E402
from app.exchange.venue import (  # noqa: E402
    ORDER_CANCELED,
    ORDER_FILLED,
    ORDER_OPEN,
    ORDER_PARTIAL,
    normalize_order_status,
)
from app.keystore import CredentialStore  # noqa: E402
from app.risk.manager import RiskGuard, size_position  # noqa: E402
from app.utils import Clock  # noqa: E402


class _FrozenClock(Clock):
    def __init__(self, ms: int = 1_700_000_000_000) -> None:
        super().__init__()
        self._ms = ms

    def now_ms(self) -> int:  # type: ignore[override]
        return self._ms


# --------------------------------------------------------------------------- #
#  order-state vocabulary
# --------------------------------------------------------------------------- #
class NormalizeOrderStatusTest(unittest.TestCase):
    def test_venue_dialects_collapse_onto_one_vocabulary(self):
        self.assertEqual(normalize_order_status(filled_qty=0, total_qty=0, raw_status="FILLED"), ORDER_FILLED)
        self.assertEqual(normalize_order_status(filled_qty=0, total_qty=0, raw_status="FILLED"), ORDER_FILLED)
        self.assertEqual(normalize_order_status(filled_qty=0, total_qty=0, raw_status="done"), ORDER_FILLED)
        self.assertEqual(normalize_order_status(filled_qty=0, total_qty=0, raw_status="CANCELED"), ORDER_CANCELED)
        self.assertEqual(normalize_order_status(filled_qty=1, total_qty=10, raw_status="PARTIALLY_FILLED"),
                         ORDER_PARTIAL)
        self.assertEqual(normalize_order_status(filled_qty=10, total_qty=10, raw_status="PARTIALLY_FILLED"),
                         ORDER_FILLED)
        self.assertEqual(normalize_order_status(filled_qty=0, total_qty=5, is_active=True), ORDER_OPEN)
        self.assertEqual(normalize_order_status(filled_qty=3, total_qty=5, is_active=False), ORDER_PARTIAL)


# --------------------------------------------------------------------------- #
#  small-account sizing
# --------------------------------------------------------------------------- #
class SizingAffordabilityTest(unittest.TestCase):
    def test_venue_minimum_that_breaks_the_budget_is_rejected(self):
        """$20 account, 8% margin, 10x -> $16 notional budget."""
        btc = ContractSpec(symbol="XBTUSDTM", contract_size=0.001, price_unit=0.1,
                           price_scale=1, vol_unit=1, min_vol=1, min_notional=5.0)
        sizing = size_position(20.0, 100_000.0, btc, equity_pct=8.0, leverage=10,
                               min_notional_usd=5.0)
        self.assertFalse(sizing.ok)
        self.assertIn("risk budget", sizing.reason)
        self.assertIn("contracts", sizing.reason)

    def test_small_contract_symbol_fits_the_small_account(self):
        """A symbol whose one-lot minimum is $1.50 must still be tradable on $20."""
        alt = ContractSpec(symbol="TIAUSDTM", contract_size=0.1, price_unit=0.001,
                           price_scale=3, vol_unit=1, min_vol=1, min_notional=5.0)
        sizing = size_position(20.0, 1.5, alt, equity_pct=8.0, leverage=10, min_notional_usd=5.0)
        self.assertTrue(sizing.ok, sizing.reason)
        self.assertLessEqual(sizing.notional_usd, 20.0 * 0.08 * 10 * 1.30)

    def test_exchange_min_notional_is_enforced(self):
        spec = ContractSpec(symbol="X", contract_size=1.0, vol_unit=1, min_vol=1, min_notional=5000.0)
        sizing = size_position(1000.0, 10.0, spec, equity_pct=8.0, leverage=10, min_notional_usd=5.0)
        self.assertFalse(sizing.ok)
        self.assertIn("below exchange minimum", sizing.reason)

    def test_position_cap_below_venue_minimum_is_rejected(self):
        spec = ContractSpec(symbol="X", contract_size=1.0, vol_unit=1, min_vol=1, max_vol=10.0,
                            min_notional=100.0)
        sizing = size_position(1000.0, 2.0, spec, equity_pct=8.0, leverage=10, min_notional_usd=5.0)
        self.assertFalse(sizing.ok)
        self.assertIn("below the exchange minimum", sizing.reason)

    def test_available_balance_is_respected_after_rounding_up(self):
        spec = ContractSpec(symbol="X", contract_size=1.0, vol_unit=1, min_vol=100.0)
        sizing = size_position(1000.0, 1.0, spec, equity_pct=8.0, leverage=10, available=9.0)
        self.assertFalse(sizing.ok)
        self.assertIn("available", sizing.reason)


# --------------------------------------------------------------------------- #
#  risk guard (the 3-argument GuardDecision crash)
# --------------------------------------------------------------------------- #
class GuardDecisionTest(unittest.TestCase):
    def test_margin_utilisation_gate_returns_a_decision_instead_of_raising(self):
        cfg = isolated_config()
        db = Database(cfg.resolve("data/test.db"))
        guard = RiskGuard(cfg, db)
        decision = asyncio.run(guard.can_open(
            symbol="SOL_USDT", equity=1000.0, open_positions=0, margin_used=790.0,
            available=210.0, sizing_notional=300.0,
        ))
        self.assertFalse(decision.allowed)
        self.assertIn("margin utilisation", decision.reason)
        self.assertIsInstance(decision.detail, dict)
        db.close()


class PaperResetRebaselineTest(unittest.TestCase):
    """Resetting a $1000 paper book to $20 must not look like a 98 % drawdown."""

    def test_rebaseline_clears_the_halt_and_resets_the_peak(self):
        cfg = isolated_config()
        db = Database(cfg.resolve("data/test.db"))
        guard = RiskGuard(cfg, db)

        async def scenario() -> None:
            await guard.update_equity(1000.0)
            await guard.update_equity(20.0)                 # looks like a -98 % drawdown
            self.assertTrue(guard.halted)
            await guard.rebaseline(20.0)
            self.assertFalse(guard.halted)
            self.assertEqual(guard.equity_peak, 20.0)
            await guard.update_equity(19.0)                 # -5 %: below the halt, allowed
            self.assertFalse(guard.halted)

        asyncio.run(scenario())
        db.close()


# --------------------------------------------------------------------------- #
#  protection lifecycle on the live broker
# --------------------------------------------------------------------------- #
class _StubBinance(BinanceClient):
    """Binance client whose transport is a canned recording stub."""

    def __init__(self) -> None:
        super().__init__(_FrozenClock(), rest_base="https://fapi.binance.com")
        self.calls: List[Dict[str, Any]] = []
        self.rows: List[Any] = []

    async def _request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        self.calls.append({"method": method, "path": path, **kw})
        return self.rows.pop(0) if self.rows else {}


class ProtectionLifecycleTest(unittest.TestCase):
    def _broker(self) -> tuple[LiveBroker, _StubBinance]:
        client = _StubBinance()
        broker = LiveBroker(client, None, entry_order_type="market")
        return broker, client

    def test_adopt_existing_protection_instead_of_duplicating(self):
        broker, client = self._broker()
        client.rows = [[
            {"orderId": 11, "type": "STOP_MARKET", "stopPrice": "95.0", "reduceOnly": True},
            {"orderId": 12, "type": "LIMIT", "price": "200.0", "reduceOnly": True},
        ]]
        handle = asyncio.run(broker.arm_protection(
            symbol="SOLUSDT", side=LONG, qty=1, sl_price=95.0, tp_price=200.0, adopt=True,
        ))
        self.assertEqual(handle["kind"], "plan")
        self.assertTrue(handle.get("adopted"))
        self.assertEqual(handle["stop_order_id"], "11")
        self.assertEqual(handle["tp_order_id"], "12")
        # exactly one query, zero order placements
        self.assertEqual([c["method"] for c in client.calls], ["GET"])
        self.assertNotIn("/fapi/v1/order", [c["path"] for c in client.calls if c["method"] == "POST"])

    def test_adopt_falls_back_to_placing_when_nothing_rests(self):
        broker, client = self._broker()
        client.rows = [[]]                     # open orders query: empty
        placed: List[str] = []
        client.rows = [[]]

        async def fake_stop(symbol, *, side, qty, trigger_price, reduce_only=True, client_id=""):
            placed.append("stop")
            from app.exchange.base import OrderResult

            return OrderResult(ok=True, order_id="s1", price=trigger_price)

        client.stop_order = fake_stop  # type: ignore[assignment]
        handle = asyncio.run(broker.arm_protection(
            symbol="SOLUSDT", side=LONG, qty=1, sl_price=95.0, tp_price=None, adopt=True,
        ))
        self.assertEqual(placed, ["stop"])
        self.assertEqual(handle["kind"], "plan")

    def test_release_stop_cancels_every_leg(self):
        broker, client = self._broker()
        handle = {"kind": "plan", "stop_order_id": "55", "tp_order_id": "66", "side": LONG}
        ok = asyncio.run(broker.release_stop(symbol="SOLUSDT", handle=handle))
        self.assertTrue(ok)
        deleted = [c for c in client.calls if c["method"] == "DELETE"]
        paths = {c["path"] for c in deleted}
        self.assertIn("/fapi/v1/order", paths)                     # the stop
        params = [c.get("params", {}) for c in deleted]
        self.assertIn({"symbol": "SOLUSDT", "orderId": "55"}, params)
        self.assertIn({"symbol": "SOLUSDT", "orderId": "66"}, params)   # the resting TP too

    def test_order_push_is_normalized_before_the_executor_sees_it(self):
        broker, _ = self._broker()
        seen: List[Dict[str, Any]] = []
        asyncio.run(broker.set_callbacks(on_order=seen.append))
        broker._on_order_push({"o": {"c": "ao-SOL-1", "X": "FILLED", "z": "3", "q": "3", "ap": "101.5"}})
        self.assertEqual(seen[0]["client_id"], "ao-SOL-1")
        self.assertEqual(seen[0]["status"], ORDER_FILLED)
        self.assertEqual(seen[0]["filled_qty"], 3.0)
        self.assertEqual(seen[0]["avg_price"], 101.5)

    def test_set_callbacks_keeps_the_normalizer_in_the_socket_path(self):
        """Regression: wiring the raw consumer into the WS bypassed normalization."""
        broker, _ = self._broker()

        class _FakeWS:
            on_order = None

        broker.ws = _FakeWS()                      # type: ignore[assignment]
        seen: List[Dict[str, Any]] = []
        asyncio.run(broker.set_callbacks(on_order=seen.append))
        self.assertEqual(broker.ws.on_order.__func__, broker._on_order_push.__func__)
        broker.ws.on_order({"o": {"c": "ao-BTC-9", "X": "FILLED", "z": "1", "q": "1", "ap": "7.5"}})
        self.assertEqual(seen[0]["client_id"], "ao-BTC-9")
        self.assertEqual(seen[0]["status"], ORDER_FILLED)

    def test_attached_venues_still_use_attached_protection(self):
        raw = MeXCClient("https://api.mexc.com", _FrozenClock())
        mexc = MexcVenueClient(raw)
        broker = LiveBroker(mexc, None)
        self.assertTrue(broker.attached_supported)


# --------------------------------------------------------------------------- #
#  fill confirmation is venue-neutral
# --------------------------------------------------------------------------- #
class OrderStatusNormalizationTest(unittest.TestCase):
    def test_binance_dialect(self):
        client = _StubBinance()
        client.rows = [{"orderId": 7, "status": "FILLED", "executedQty": "2", "avgPrice": "101.25",
                        "origQty": "2"}]
        info = asyncio.run(client.order_status("SOLUSDT", order_id="7"))
        self.assertEqual(info["status"], ORDER_FILLED)
        self.assertEqual(info["filled_qty"], 2.0)
        self.assertEqual(info["avg_price"], 101.25)

    def test_kucoin_dialect(self):
        class _StubKuCoin(KuCoinClient):
            def __init__(self) -> None:
                super().__init__(_FrozenClock(), api_key="K", api_secret="S", passphrase="P")
                self.rows: List[Any] = []

            async def _request(self, method, path, **kw):  # type: ignore[override]
                return self.rows.pop(0) if self.rows else {}

        client = _StubKuCoin()
        client.rows = [{"dealSize": 5, "size": 10, "isActive": True, "avgDealPrice": "50.5"}]
        info = asyncio.run(client.order_status("SOLUSDTM", order_id="abc"))
        self.assertEqual(info["status"], ORDER_PARTIAL)
        self.assertEqual(info["filled_qty"], 5.0)
        self.assertEqual(info["avg_price"], 50.5)

    def test_mexc_dialect(self):
        raw = MeXCClient("https://api.mexc.com", _FrozenClock())
        client = MexcVenueClient(raw)

        async def fake_by_ext(symbol, oid):
            return {"state": 3, "dealVol": 4, "vol": 4, "dealAvgPrice": 12.5}

        raw.order_by_external_id = fake_by_ext  # type: ignore[assignment]
        info = asyncio.run(client.order_status("SOL_USDT", client_id="ao-1"))
        self.assertEqual(info["status"], ORDER_FILLED)
        self.assertEqual(info["avg_price"], 12.5)


class ExecutorFillPlumbingTest(unittest.TestCase):
    def test_pending_fill_future_only_resolves_on_a_real_fill(self):
        from app.trade.executor import Executor

        cfg = isolated_config()
        db = Database(cfg.resolve("data/test.db"))
        guard = RiskGuard(cfg, db)

        class _Broker:
            pass

        ex = Executor(_Broker(), cfg, db, guard)

        async def scenario() -> None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            ex.pending_fills["ao-1"] = fut
            ex.notify_order_push({"client_id": "ao-1", "status": ORDER_OPEN, "filled_qty": 0})
            self.assertFalse(fut.done())                       # resting order: ignored
            ex.notify_order_push({"client_id": "ao-1", "status": ORDER_PARTIAL, "filled_qty": 2})
            self.assertTrue(fut.done())
            self.assertEqual(fut.result()["filled_qty"], 2)

        asyncio.run(scenario())
        db.close()


# --------------------------------------------------------------------------- #
#  live-mode safety gate
# --------------------------------------------------------------------------- #
class LiveSafetyGateTest(unittest.TestCase):
    def _engine(self, **overrides: Any) -> TradingEngine:
        cfg = isolated_config()
        cfg.set_many(overrides)
        db = Database(cfg.resolve("data/test.db"))
        self.addCleanup(db.close)
        keystore = CredentialStore(db, cfg.resolve("data/.secrets"), "MEXC", False)
        return TradingEngine(cfg, db, keystore, "mexc")

    def test_live_without_token_on_a_public_bind_is_refused(self):
        engine = self._engine(**{"app.mode": "live", "web.api_token": "", "web.host": "0.0.0.0"})
        with self.assertRaises(RuntimeError) as ctx:
            engine._assert_live_safety()
        self.assertIn("web.api_token", str(ctx.exception))

    def test_token_or_loopback_bind_is_allowed(self):
        engine = self._engine(**{"app.mode": "live", "web.api_token": "s3cret-token",
                                 "web.host": "0.0.0.0"})
        engine._assert_live_safety()
        engine = self._engine(**{"app.mode": "live", "web.api_token": "", "web.host": "127.0.0.1"})
        engine._assert_live_safety()

    def test_explicit_override_is_honoured(self):
        engine = self._engine(**{"app.mode": "live", "web.api_token": "", "web.host": "0.0.0.0",
                                 "web.allow_insecure_live": True})
        engine._assert_live_safety()


# --------------------------------------------------------------------------- #
#  paper path still behaves after the audit changes
# --------------------------------------------------------------------------- #
class StartingBalanceAnchorTest(unittest.IsolatedAsyncioTestCase):
    """The dashboard's "starting balance" is a fixed anchor.

    It must survive restarts and config edits (only an explicit paper reset may
    move it), and the state payload must expose it next to released/open P&L and
    the win rate for the active venue.
    """

    async def asyncSetUp(self) -> None:
        from tests._util import temp_dir

        self.tmp = temp_dir("ao-anchor-")
        self.cfg = isolated_config(self.tmp / "data")
        self.cfg.set("exchange.paper_data_source", "synthetic")
        self.cfg.set("account.paper_starting_equity", 1000.0)
        self.cfg.set("universe.refresh_sec", 600)
        self.db = Database(self.tmp / "data" / "venues" / "mexc.db")
        self.addCleanup(self.db.close)
        self.engine = TradingEngine(
            self.cfg, self.db,
            CredentialStore(self.db, self.tmp / "data" / ".secrets"),
            venue_id="mexc",
        )
        await self.engine.start()
        self.addAsyncCleanup(self.engine.stop)

    async def test_anchor_is_set_once_and_never_follows_config(self):
        anchor = await self.engine.ensure_starting_balance(1000.0)
        self.assertEqual(anchor, 1000.0)
        # a later config edit must not move the anchor of an existing book
        self.cfg.set("account.paper_starting_equity", 500.0)
        self.assertEqual(await self.engine.ensure_starting_balance(1234.0), 1000.0)
        # ...and it is what the dashboard reports
        state = await self.engine.state()
        self.assertEqual(state["account"]["starting_balance"], 1000.0)
        self.assertEqual(state["account"]["return_pct"], 0.0)
        self.assertIn("released_pnl", state["account"])
        self.assertIn("open_pnl", state["account"])
        self.assertIn("win_rate", state["account"])
        self.assertEqual(state["target"]["starting_balance"], 1000.0)

    async def test_released_pnl_and_win_rate_reach_the_dashboard(self):
        """The header figures must come from the closed trades, not a stale key.

        ``db.trade_stats()`` sums into ``total_pnl`` while the analytics module
        uses ``pnl`` — reading the wrong one reported a $0.00 released P/L on a
        profitable account.
        """
        await self.engine.ensure_starting_balance(1000.0)
        for uid, pnl, roi in (("t1", 12.5, 156.0), ("t2", -3.0, -37.5), ("t3", 4.0, 50.0)):
            await self.db.insert_trade({
                "trade_uid": uid, "symbol": "SOL_USDT", "side": "LONG", "status": "CLOSED",
                "qty": 1, "entry_price": 100.0, "exit_price": 101.0, "leverage": 10,
                "margin_usd": 8, "notional_usd": 80, "realized_pnl": pnl, "roi_pct": roi,
                "opened_at": 1.0, "closed_at": 2.0,
            })
        state = await self.engine.state()
        acct = state["account"]
        self.assertEqual(acct["released_pnl"], 13.5)
        self.assertEqual(acct["total_pnl"], 13.5)
        self.assertEqual(acct["trades"], 3)
        self.assertEqual(acct["wins"], 2)
        self.assertEqual(acct["losses"], 1)
        self.assertAlmostEqual(acct["win_rate"], 66.67, places=1)

    async def test_explicit_reset_reanchors(self):
        await self.engine.ensure_starting_balance(1000.0)
        # what the paper-reset control does: shrink the book, then re-anchor
        self.engine.broker.starting_equity = 20.0
        self.engine.broker.realized = 0.0
        self.engine.last_account = None          # what the reset endpoint does
        self.assertEqual(await self.engine.ensure_starting_balance(force=20.0), 20.0)
        self.assertEqual(await self.engine.ensure_starting_balance(), 20.0)
        state = await self.engine.state()
        self.assertEqual(state["account"]["starting_balance"], 20.0)
        self.assertEqual(state["account"]["return_pct"], 0.0)


class PaperRegressionTest(unittest.TestCase):
    def test_paper_broker_arm_protection_accepts_adopt_flag(self):
        feed = SyntheticFeed(history_bars=30, tick_seconds=0.05)
        broker = PaperBroker(SyntheticMarketAdapter(feed), name="paper", starting_equity=20.0)
        handle = asyncio.run(broker.arm_protection(
            symbol=list(feed.symbols)[0], side=LONG, qty=1, sl_price=1.0, tp_price=2.0, adopt=True,
        ))
        self.assertEqual(handle["kind"], "paper")

    def test_paper_broker_name_is_venue_scoped(self):
        feed = SyntheticFeed(history_bars=20, tick_seconds=0.05)
        broker = PaperBroker(SyntheticMarketAdapter(feed), name="kucoin-paper", starting_equity=20.0)
        self.assertEqual(broker.diagnostics()["venue"], "kucoin-paper")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
