"""Live order-path audit (2026-10): every order the bot places is MARKET.

The live path is driven end to end through the *real* ``MeXCClient`` →
``MexcVenueClient`` → ``LiveBroker`` stack, with a fake HTTP transport standing
in for the exchange socket. The transport records every signed request together
with its JSON body, so these tests assert on the exact payloads the venue would
receive — entries, protective stops, trailing moves, exits and the repair/adopt
path. Nothing here touches the network.

The rule under test: **no order of ours may rest on the book**. Entries, exits,
stop-loss, trail updates and the ROI target are all market orders; the only
object that may sit on the exchange is the protective *trigger* stop, which is a
market order once triggered, and any legacy resting take-profit found on adopt
must be cancelled rather than adopted.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, Dict, List, Optional

from app.exchange.live import LiveBroker
from app.exchange.mexc import MexcVenueClient, MeXCClient

SYMBOL = "BTC_USDT"
STOP = 95_000.0
ENTRY_STOP_ID = "222"


class _Clock:
    def now_ms(self) -> int:
        return 1_700_000_000_000

    def update(self, *_: Any) -> None:  # pragma: no cover - sync helper
        pass


class _Resp:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeHTTP:
    """Records every signed request; routes answer with canned JSON."""

    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        self.routes: Dict[str, Any] = routes or {}
        self.calls: List[Dict[str, Any]] = []

    @property
    def order_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if "/order/" in c["path"] or "planorder" in c["path"]]

    async def request(
        self, method: str, path: str, *,
        params: Optional[Dict[str, Any]] = None, content: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None, timeout: Optional[float] = None,
    ) -> _Resp:
        body = json.loads(content) if content else None
        self.calls.append({
            "method": method.upper(), "path": path, "params": params,
            "body": body, "headers": headers or {},
        })
        handler: Any = self.routes.get(path)
        if callable(handler):
            payload = handler(method.upper(), params, body)
        elif handler is not None:
            payload = handler
        else:
            payload = {"success": True, "data": None}
        return _Resp(payload)


class _Harness:
    def __init__(self, routes: Optional[Dict[str, Any]] = None) -> None:
        self.raw = MeXCClient(
            "https://contract.mexc.com", _Clock(),  # type: ignore[arg-type]
            api_key="key", api_secret="secret",
        )
        self.http = _FakeHTTP(routes)
        self.raw._client = self.http            # type: ignore[assignment]
        self.venue = MexcVenueClient(self.raw)
        self.broker = LiveBroker(self.venue, None)

    def body_for(self, path: str) -> Dict[str, Any]:
        hits = [c for c in self.http.calls if c["path"] == path and c["body"]]
        assert hits, f"no request body recorded for {path}"
        return hits[-1]["body"]

    def bodies_for(self, path: str) -> List[Dict[str, Any]]:
        return [c["body"] for c in self.http.calls if c["path"] == path and c["body"]]


class MarketOnlyOrderAuditTest(unittest.TestCase):
    """Every order payload the live path can produce, asserted field by field."""

    def test_entry_is_market_with_attached_stop_and_no_take_profit(self) -> None:
        h = _Harness({"/api/v1/private/order/create": {"success": True, "data": {"orderId": 111}}})
        res = asyncio.run(h.broker.open_position(
            SYMBOL, side="LONG", qty=3, leverage=10, sl_price=STOP, client_id="ao-1",
        ))
        self.assertTrue(res.ok, res.error)
        body = h.body_for("/api/v1/private/order/create")
        self.assertEqual(body["type"], 5, "entry must be orderType=5 (market)")
        self.assertNotIn("price", body, "a market entry must not carry an execution price")
        self.assertEqual(body["stopLossPrice"], STOP)
        self.assertNotIn("takeProfitPrice", body, "the ROI target is local, never an order")
        self.assertNotIn("profitTrend", body)
        self.assertEqual(body["priceProtect"], 1)
        self.assertEqual(body["reduceOnly"], False)
        self.assertEqual(res.raw.get("protection"), "attached")

    def test_standalone_stop_is_a_market_trigger_order(self) -> None:
        h = _Harness({
            "/api/v1/private/planorder/list/orders": {"success": True, "data": []},
            "/api/v1/private/order/list/open_orders": {"success": True, "data": []},
            "/api/v1/private/stoporder/open_orders": {"success": True, "data": []},
            "/api/v1/private/planorder/place/v2": {"success": True, "data": ENTRY_STOP_ID},
        })
        handle = asyncio.run(h.broker.arm_protection(
            symbol=SYMBOL, side="LONG", qty=3, sl_price=STOP, adopt=True,
        ))
        body = h.body_for("/api/v1/private/planorder/place/v2")
        self.assertEqual(body["orderType"], 5, "stop must execute at market when triggered")
        self.assertNotIn("price", body, "no execution price => no resting limit leg")
        self.assertEqual(body["triggerPrice"], STOP)
        self.assertEqual(body["triggerType"], 2, "long stop triggers when price <= trigger")
        self.assertTrue(body["reduceOnly"])
        self.assertEqual(handle["stop_order_id"], ENTRY_STOP_ID)
        self.assertEqual(handle["kind"], "plan")
        self.assertNotIn("tp_order_id", handle)

    def test_trailing_move_is_a_market_move_never_a_limit_price(self) -> None:
        h = _Harness({"/api/v1/private/planorder/change_price": {"success": True}})
        handle = {"kind": "plan", "stop_order_id": ENTRY_STOP_ID, "side": "LONG", "stop_price": STOP}
        ok = asyncio.run(h.broker.move_stop(
            symbol=SYMBOL, handle=handle, new_stop_price=97_000.0, qty=3,
        ))
        self.assertTrue(ok)
        body = h.body_for("/api/v1/private/planorder/change_price")
        self.assertEqual(body["orderType"], 5)
        self.assertEqual(body["price"], 0, "the trigger price must never become a limit price")
        self.assertEqual(body["triggerPrice"], 97_000.0)
        self.assertEqual(handle["stop_price"], 97_000.0)

    def test_trailing_move_falls_back_when_the_venue_refuses_price_zero(self) -> None:
        def refuse_zero(method: str, params: Any, body: Dict[str, Any]) -> Dict[str, Any]:
            if body.get("price") in (0, 0.0, None):
                return {"success": False, "code": 3001, "message": "price is required"}
            return {"success": True}

        h = _Harness({"/api/v1/private/planorder/change_price": refuse_zero})
        handle = {"kind": "plan", "stop_order_id": ENTRY_STOP_ID, "side": "LONG", "stop_price": STOP}
        ok = asyncio.run(h.broker.move_stop(
            symbol=SYMBOL, handle=handle, new_stop_price=97_000.0, qty=3,
        ))
        self.assertTrue(ok, "the ratchet must still happen after the fallback")
        bodies = h.bodies_for("/api/v1/private/planorder/change_price")
        self.assertEqual([b["price"] for b in bodies], [0, 97_000.0])
        self.assertTrue(all(b["orderType"] == 5 for b in bodies), "both attempts stay market")

    def test_exit_is_a_reduce_only_market_order(self) -> None:
        h = _Harness({"/api/v1/private/order/create": {"success": True, "data": {"orderId": 999}}})
        res = asyncio.run(h.broker.close_position(SYMBOL, "LONG", 3, reason="trailing_stop"))
        self.assertTrue(res.ok, res.error)
        body = h.body_for("/api/v1/private/order/create")
        self.assertEqual(body["type"], 5)
        self.assertTrue(body["reduceOnly"])
        self.assertNotIn("price", body)


class AdoptRepairAuditTest(unittest.TestCase):
    """A restart must never re-create, and never adopt, a resting take-profit."""

    def test_legacy_reduce_only_limit_tp_is_cancelled_not_adopted(self) -> None:
        h = _Harness({
            "/api/v1/private/planorder/list/orders": {"success": True, "data": [
                {"id": "55", "state": 1, "triggerPrice": STOP, "orderType": 5},
            ]},
            "/api/v1/private/order/list/open_orders": {"success": True, "data": [
                {"orderId": "66", "symbol": SYMBOL, "reduceOnly": True, "state": 1, "price": 120_000.0},
                {"orderId": "77", "symbol": SYMBOL, "reduceOnly": False, "state": 1, "price": 90_000.0},
            ]},
            "/api/v1/private/stoporder/open_orders": {"success": True, "data": []},
            "/api/v1/private/order/cancel": {"success": True, "data": []},
        })
        handle = asyncio.run(h.broker.arm_protection(
            symbol=SYMBOL, side="LONG", qty=3, sl_price=STOP, adopt=True,
        ))
        # the stop is adopted...
        self.assertEqual(handle["stop_order_id"], "55")
        self.assertNotIn("tp_order_id", handle)
        self.assertNotIn("tp_price", handle)
        # ...the resting reduce-only limit (a legacy take-profit) is cancelled...
        cancel = h.body_for("/api/v1/private/order/cancel")
        self.assertEqual(cancel["orderIds"], [66])
        # ...and nothing is placed while a valid stop already rests
        self.assertEqual(h.bodies_for("/api/v1/private/planorder/place/v2"), [])

    def test_attached_legacy_tp_is_cancelled_through_the_entry_order(self) -> None:
        entry_id = 111
        h = _Harness({
            "/api/v1/private/stoporder/open_orders": {"success": True, "data": [
                {
                    "id": 9001, "orderId": entry_id, "state": 1,
                    "stopLossPrice": STOP, "takeProfitPrice": 120_000.0,
                },
            ]},
            "/api/v1/private/planorder/change_stop_order": {"success": True},
        })
        handle = asyncio.run(h.broker.arm_protection(
            symbol=SYMBOL, side="LONG", qty=3, sl_price=STOP,
            entry_order_id=str(entry_id), adopt=False,
        ))
        self.assertEqual(handle["kind"], "attached")
        self.assertEqual(handle["stop_price"], STOP)
        self.assertNotIn("tp_price", handle)
        body = h.body_for("/api/v1/private/planorder/change_stop_order")
        self.assertEqual(body["orderId"], entry_id)
        self.assertEqual(body["takeProfitPrice"], 0, "the take-profit leg must be dropped")
        self.assertNotIn("stopLossPrice", body, "the stop-loss leg must be left intact")
        # the stop was never re-placed as a second order
        self.assertEqual(h.bodies_for("/api/v1/private/planorder/place/v2"), [])


class FullSessionInvariantTest(unittest.TestCase):
    """One complete trade: entry -> arm -> trail -> exit. No positive price anywhere."""

    def test_no_order_in_a_whole_session_can_rest_on_the_book(self) -> None:
        h = _Harness({
            "/api/v1/private/order/create": {"success": True, "data": {"orderId": 111}},
            "/api/v1/private/planorder/list/orders": {"success": True, "data": []},
            "/api/v1/private/order/list/open_orders": {"success": True, "data": []},
            "/api/v1/private/stoporder/open_orders": {"success": True, "data": []},
            "/api/v1/private/planorder/place/v2": {"success": True, "data": ENTRY_STOP_ID},
            "/api/v1/private/planorder/change_price": {"success": True},
        })

        async def session() -> Dict[str, Any]:
            entry = await h.broker.open_position(
                SYMBOL, side="LONG", qty=3, leverage=10, sl_price=STOP, client_id="ao-1",
            )
            self.assertTrue(entry.ok, entry.error)
            handle = await h.broker.arm_protection(
                symbol=SYMBOL, side="LONG", qty=3, sl_price=STOP, adopt=True,
            )
            for step in (96_000.0, 97_000.0, 98_000.0):
                ok = await h.broker.move_stop(symbol=SYMBOL, handle=handle, new_stop_price=step, qty=3)
                self.assertTrue(ok)
            return handle

        handle = asyncio.run(session())
        self.assertTrue(handle["stop_order_id"])

        # invariant 1: nothing we send ever carries a positive execution price
        for call in h.http.order_calls:
            price = (call["body"] or {}).get("price")
            self.assertIn(price, (None, 0, 0.0), f"{call['path']} sent a limit price: {price}")
        # invariant 2: every order-creating call is explicitly market
        for call in h.http.order_calls:
            if call["path"].endswith("/order/create"):
                self.assertEqual(call["body"]["type"], 5)
            if call["path"].endswith("planorder/place/v2"):
                self.assertEqual(call["body"]["orderType"], 5)
            if call["path"].endswith("planorder/change_price"):
                self.assertEqual(call["body"]["orderType"], 5)
        # invariant 3: the entry order carried the stop, so there is exactly one
        # protective leg and it is the plan stop we manage
        creates = h.bodies_for("/api/v1/private/order/create")
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["stopLossPrice"], STOP)
        self.assertNotIn("takeProfitPrice", creates[0])


class ProtectionFailureAuditTest(unittest.TestCase):
    """If protection cannot be placed, the caller must be able to see it."""

    def test_failed_stop_placement_is_reported_not_silently_swallowed(self) -> None:
        h = _Harness({
            "/api/v1/private/planorder/list/orders": {"success": True, "data": []},
            "/api/v1/private/order/list/open_orders": {"success": True, "data": []},
            "/api/v1/private/stoporder/open_orders": {"success": True, "data": []},
            "/api/v1/private/planorder/place/v2": {"success": False, "code": 3001, "message": "nope"},
        })
        handle = asyncio.run(h.broker.arm_protection(
            symbol=SYMBOL, side="LONG", qty=3, sl_price=STOP, adopt=True,
        ))
        self.assertEqual(handle["kind"], "none")
        self.assertFalse(handle.get("stop_price"))
        # the executor treats this handle as "not protected" and flattens:
        self.assertFalse(bool(handle.get("stop_price")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
