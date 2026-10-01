"""Multi-venue tests: signing, order mapping, isolation, symbol styles.

These are the tests that protect real money:

* signing strings are pinned against hand-computed vectors (a silent change in
  the canonical string = every order rejected / unauthorised),
* order *parameters* are asserted per venue (side, reduceOnly, positionSide,
  stop triggers) with a stubbed transport — no network is touched,
* each venue's engine, database and credentials are proven to be isolated.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import unittest
from typing import Any, Dict, List

from tests._util import isolated_config  # noqa: E402

from app.config import VenueConfig  # noqa: E402
from app.exchange.base import LONG, SHORT  # noqa: E402
from app.exchange.binance import BinanceClient  # noqa: E402
from app.exchange.kucoin import KuCoinClient, _parse_kline_row  # noqa: E402
from app.exchange.mexc import MeXCClient, MexcVenueClient  # noqa: E402
from app.exchange.synthetic import SyntheticFeed  # noqa: E402
from app.exchange.venue import VENUES, VenueClient, symbol_style_name  # noqa: E402
from app.manager import VenueManager  # noqa: E402
from app.utils import Clock  # noqa: E402


class _FrozenClock(Clock):
    """Deterministic clock so signatures are reproducible."""

    def __init__(self, ms: int = 1_700_000_000_000) -> None:
        super().__init__()
        self._ms = ms

    def now_ms(self) -> int:  # type: ignore[override]
        return self._ms

    @property
    def offset_ms(self) -> float:  # type: ignore[override]
        return 0.0

    @property
    def rtt_ms(self) -> float:  # type: ignore[override]
        return 0.0

    @property
    def age_s(self) -> float:  # type: ignore[override]
        return 0.0

    def update(self, server_ms: float, rtt_ms: float) -> None:  # type: ignore[override]
        return None


class SigningTest(unittest.TestCase):
    def test_binance_signature_is_hmac_over_the_exact_query(self):
        client = BinanceClient(_FrozenClock(), api_key="KEY123", api_secret="SECRET456")
        headers, query, content = client._sign_request(
            "GET", "/fapi/v1/account", params={"recvWindow": 5000}, body=None, signed=True
        )
        expected_param_string = "recvWindow=5000&timestamp=1700000000000"
        expected_sig = hmac.new(b"SECRET456", expected_param_string.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-MBX-APIKEY"], "KEY123")
        self.assertEqual(
            query,
            f"recvWindow=5000&timestamp=1700000000000&signature={expected_sig}",
        )
        self.assertIsNone(content)

    def test_binance_post_signs_form_body(self):
        client = BinanceClient(_FrozenClock(), api_key="K", api_secret="S")
        _headers, query, content = client._sign_request(
            "POST", "/fapi/v1/order", params=None,
            body={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 0.01}, signed=True,
        )
        self.assertIsNone(query)
        self.assertIn("symbol=BTCUSDT", content)
        self.assertIn("timestamp=1700000000000", content)
        param_string = content.split("&signature=")[0]
        self.assertEqual(
            content.split("&signature=")[1],
            hmac.new(b"S", param_string.encode(), hashlib.sha256).hexdigest(),
        )

    def test_kucoin_signature_and_passphrase(self):
        client = KuCoinClient(_FrozenClock(), api_key="KEY", api_secret="SEC",
                              passphrase="PASS")
        headers, query, content = client._sign_request(
            "GET", "/api/v1/orders", params={"status": "active"}, body=None, signed=True
        )
        message = "1700000000000GET/api/v1/orders?status=active"
        expected = base64.b64encode(
            hmac.new(b"SEC", message.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(headers["KC-API-SIGN"], expected)
        self.assertEqual(
            headers["KC-API-PASSPHRASE"],
            base64.b64encode(hmac.new(b"SEC", b"PASS", hashlib.sha256).digest()).decode(),
        )
        self.assertEqual(headers["KC-API-KEY-VERSION"], "2")
        self.assertEqual(query, "status=active")

    def test_kucoin_post_body_is_part_of_the_signature(self):
        client = KuCoinClient(_FrozenClock(), api_key="KEY", api_secret="SEC", passphrase="PASS")
        body = {"clientOid": "abc", "symbol": "XBTUSDTM", "side": "buy", "type": "market", "size": 1}
        headers, _query, content = client._sign_request(
            "POST", "/api/v1/orders", params=None, body=body, signed=True
        )
        compact = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        self.assertEqual(content, compact)
        message = "1700000000000POST/api/v1/orders" + compact
        self.assertEqual(
            headers["KC-API-SIGN"],
            base64.b64encode(hmac.new(b"SEC", message.encode(), hashlib.sha256).digest()).decode(),
        )

    def test_mexc_signature_matches_documented_formula(self):
        raw = MeXCClient("https://api.mexc.com", _FrozenClock(), api_key="MK", api_secret="MS")
        body_str = json.dumps({"symbol": "BTC_USDT", "vol": 1, "side": 1}, separators=(",", ":"))
        signature = raw._sign(1_700_000_000_000, body_str)
        self.assertEqual(
            signature,
            hmac.new(b"MS", f"MK1700000000000{body_str}".encode(), hashlib.sha256).hexdigest(),
        )

    def test_credentials_required_for_signed_calls(self):
        for client in (
            BinanceClient(_FrozenClock()),
            KuCoinClient(_FrozenClock(), api_key="k", api_secret="s"),      # passphrase missing
        ):
            self.assertFalse(client.has_credentials)
            with self.assertRaises(PermissionError):
                client._sign_request("GET", "/x", params=None, body=None, signed=True)
        client = KuCoinClient(_FrozenClock(), api_key="k", api_secret="s", passphrase="p")
        self.assertTrue(client.has_credentials)


class _CaptureClient(BinanceClient):
    """Records the request body instead of hitting the network."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(_FrozenClock(), api_key="K", api_secret="S", **kw)
        self.calls: List[Dict[str, Any]] = []
        self.responses: List[Any] = []

    async def _request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        self.calls.append({"method": method, "path": path, **kw})
        return self.responses.pop(0) if self.responses else {}


class BinanceOrderMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _CaptureClient()
        self.c._contracts = {"BTCUSDT": __import__("app.exchange.base", fromlist=["ContractSpec"]).ContractSpec(
            symbol="BTCUSDT", price_unit=0.1, price_scale=1, vol_unit=0.001, vol_scale=3,
            min_vol=0.001, contract_size=1.0)}
        self.c._dual_side = False

    def test_one_way_entry_and_exit(self):
        asyncio.run(self.c.market_order("BTCUSDT", side=LONG, qty=0.0123, reduce_only=False, leverage=10))
        body = self.c.calls[-1]["body"]
        self.assertEqual(body["side"], "BUY")
        self.assertEqual(body["quantity"], 0.012)
        self.assertNotIn("reduceOnly", body)       # opening the position
        self.assertNotIn("positionSide", body)     # one-way mode

        asyncio.run(self.c.market_order("BTCUSDT", side=LONG, qty=0.0123, reduce_only=True))
        body = self.c.calls[-1]["body"]
        self.assertEqual(body["side"], "SELL")
        self.assertEqual(body["reduceOnly"], "true")

    def test_hedge_mode_uses_position_side_not_reduce_only(self):
        self.c._dual_side = True
        asyncio.run(self.c.market_order("BTCUSDT", side=SHORT, qty=1, reduce_only=False))
        body = self.c.calls[-1]["body"]
        self.assertEqual((body["side"], body["positionSide"]), ("SELL", "SHORT"))
        self.assertNotIn("reduceOnly", body)       # Binance rejects it in hedge mode

        asyncio.run(self.c.market_order("BTCUSDT", side=SHORT, qty=1, reduce_only=True))
        body = self.c.calls[-1]["body"]
        self.assertEqual((body["side"], body["positionSide"]), ("BUY", "SHORT"))

    def test_stop_is_close_position_mark_price(self):
        asyncio.run(self.c.stop_order("BTCUSDT", side=LONG, qty=1, trigger_price=61234.56))
        body = self.c.calls[-1]["body"]
        self.assertEqual(body["type"], "STOP_MARKET")
        self.assertEqual(body["side"], "SELL")
        self.assertEqual(body["workingType"], "MARK_PRICE")
        self.assertEqual(body["closePosition"], "true")
        self.assertNotIn("quantity", body)
        self.assertEqual(body["stopPrice"], 61234.6)     # rounded to tickSize

    def test_stop_replace_places_new_before_cancelling_old(self):
        self.c.responses = [{"orderId": 999}]
        handle = {"kind": "plan", "stop_order_id": "111"}
        ok = asyncio.run(self.c.modify_stop("BTCUSDT", order_id="111", kind="plan",
                                            new_price=60000.0, qty=1, side=LONG, handle=handle))
        self.assertTrue(ok)
        self.assertEqual(self.c.calls[0]["path"], "/fapi/v1/order")
        self.assertEqual(self.c.calls[0]["body"]["type"], "STOP_MARKET")     # new stop first
        self.assertEqual(self.c.calls[1]["method"], "DELETE")               # old cancelled second
        self.assertEqual(self.c.calls[1]["params"]["orderId"], "111")
        self.assertEqual(handle["stop_order_id"], "999")                    # handle follows the replace


class _CaptureKuCoin(KuCoinClient):
    def __init__(self, **kw: Any) -> None:
        super().__init__(_FrozenClock(), api_key="K", api_secret="S", passphrase="P", **kw)
        self.calls: List[Dict[str, Any]] = []
        self.responses: List[Any] = []

    async def _request(self, method: str, path: str, **kw: Any) -> Any:  # type: ignore[override]
        self.calls.append({"method": method, "path": path, **kw})
        if self.responses:
            return self.responses.pop(0)
        if method == "GET" and path.startswith("/api/v1/orders/"):
            # fill lookup performed straight after a placement
            return {"dealSize": 42, "size": 42, "isActive": False, "avgDealPrice": "60000.5"}
        return {"orderId": "oid-1"}

    def last_body(self, path: str = "/api/v1/orders") -> Dict[str, Any]:
        for call in reversed(self.calls):
            if call["method"] == "POST" and call["path"] == path:
                return call["body"]
        raise AssertionError(f"no POST {path} recorded")


class KuCoinOrderMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.c = _CaptureKuCoin()
        from app.exchange.base import ContractSpec

        self.c._contracts = {"XBTUSDTM": ContractSpec(
            symbol="XBTUSDTM", contract_size=0.001, price_unit=0.1, price_scale=1,
            vol_unit=1, vol_scale=0, min_vol=1)}

    def test_long_entry_leverage_and_size(self):
        asyncio.run(self.c.market_order("XBTUSDTM", side=LONG, qty=42, reduce_only=False, leverage=10))
        body = self.c.last_body()
        self.assertEqual(body["side"], "buy")
        self.assertEqual(body["size"], 42)
        self.assertEqual(body["leverage"], "10")
        self.assertEqual(body["marginMode"], "ISOLATED")
        self.assertFalse(body["reduceOnly"])

    def test_long_stop_is_reduce_only_down_trigger(self):
        asyncio.run(self.c.stop_order("XBTUSDTM", side=LONG, qty=42, trigger_price=59000.55))
        body = self.c.last_body()
        self.assertEqual(body["side"], "sell")
        self.assertEqual(body["stop"], "down")          # long stop fires when price falls
        self.assertEqual(body["stopPriceType"], "MP")   # mark price trigger
        self.assertTrue(body["reduceOnly"])
        self.assertEqual(body["stopPrice"], "59000.6")  # rounded to tickSize

    def test_short_stop_is_up_trigger(self):
        asyncio.run(self.c.stop_order("XBTUSDTM", side=SHORT, qty=5, trigger_price=70000.0))
        body = self.c.last_body()
        self.assertEqual((body["side"], body["stop"]), ("buy", "up"))

    def test_stop_replace_cancels_old_after_new(self):
        handle = {"kind": "plan", "stop_order_id": "old-1"}
        self.c.responses = [{"orderId": "new-1"}, {}]
        ok = asyncio.run(self.c.modify_stop("XBTUSDTM", order_id="old-1", kind="plan",
                                            new_price=60000.0, qty=10, side=LONG, handle=handle))
        self.assertTrue(ok)
        self.assertEqual(self.c.calls[0]["path"], "/api/v1/orders")
        self.assertEqual(self.c.calls[1]["method"], "DELETE")
        self.assertTrue(self.c.calls[1]["path"].endswith("/old-1"))
        self.assertEqual(handle["stop_order_id"], "new-1")

    def test_kline_parser_handles_every_documented_order(self):
        import itertools

        for perm in itertools.permutations([105.0, 95.0, 100.0]):     # high, low, close
            row = [1_700_000_000_000, 100.0, *perm, 12.0]
            candle = _parse_kline_row(row)
            self.assertEqual((candle.h, candle.l, candle.c), (105.0, 95.0, 100.0))
            self.assertEqual(candle.ts, 1_700_000_000)                # ms -> s


class InterfaceContractTest(unittest.TestCase):
    def test_every_venue_implements_the_normalized_surface(self):
        from app.exchange.binance import BinanceClient
        from app.exchange.kucoin import KuCoinClient
        from app.exchange.mexc import MeXCClient

        required = sorted(VenueClient.__abstractmethods__)
        raw = MeXCClient("https://api.mexc.com", Clock())
        impls = {
            "mexc": MexcVenueClient(raw),
            "binance": BinanceClient(Clock()),
            "kucoin": KuCoinClient(Clock()),
        }
        for vid, impl in impls.items():
            self.assertIsInstance(impl, VenueClient, vid)
            for name in required:
                self.assertTrue(callable(getattr(impl, name, None)), f"{vid}.{name}")
            self.assertEqual(impl.spec.id, vid)
            self.assertTrue(str(impl.spec.rest_base).startswith("https://"))

    def test_venue_spec_table(self):
        self.assertEqual(set(VENUES), {"mexc", "binance", "kucoin"})
        self.assertTrue(VENUES["kucoin"].needs_passphrase)
        self.assertFalse(VENUES["binance"].needs_passphrase)
        self.assertTrue(VENUES["mexc"].supports_attached_protection)
        self.assertEqual(VENUES["binance"].native_interval("Min5"), "5m")
        self.assertEqual(VENUES["kucoin"].native_interval("Min5"), "5")
        self.assertEqual(VENUES["mexc"].native_interval("Min5"), "Min5")


class SymbolStyleTest(unittest.TestCase):
    def test_synthetic_feed_names_symbols_per_venue(self):
        feed = SyntheticFeed(history_bars=20, htf_history_bars=10, symbol_style="binance")
        self.assertIn("SOLUSDT", feed.symbols)
        feed = SyntheticFeed(history_bars=20, htf_history_bars=10, symbol_style="kucoin")
        self.assertIn("SOLUSDTM", feed.symbols)
        feed = SyntheticFeed(history_bars=20, htf_history_bars=10, symbol_style="mexc")
        self.assertIn("SOL_USDT", feed.symbols)
        self.assertEqual(symbol_style_name("mexc", "btc"), "BTC_USDT")
        self.assertEqual(symbol_style_name("binance", "btc"), "BTCUSDT")
        self.assertEqual(symbol_style_name("kucoin", "btc"), "BTCUSDTM")


class VenueIsolationTest(unittest.TestCase):
    """The manager must keep three completely separate trading stacks."""

    def setUp(self) -> None:
        self.cfg = isolated_config()
        self.mgr = VenueManager(self.cfg)
        self.addCleanup(self.mgr.close)

    def test_three_contexts_with_separate_databases(self):
        ids = [c.id for c in self.mgr.all()]
        self.assertEqual(ids, ["mexc", "binance", "kucoin"])
        paths = {c.db.path for c in self.mgr.all()}
        self.assertEqual(len(paths), 3)
        for ctx in self.mgr.all():
            self.assertEqual(ctx.engine.venue_id, ctx.id)
            self.assertEqual(ctx.engine.spec.id, ctx.id)
            self.assertIsInstance(ctx.cfg, VenueConfig)
            self.assertEqual(ctx.cfg.mode, "paper")

    def test_credentials_and_trades_do_not_leak_between_venues(self):
        async def scenario():
            binance = self.mgr.get("binance")
            kucoin = self.mgr.get("kucoin")
            await binance.keystore.save("binance-key-123456", "binance-secret-123456")
            await kucoin.keystore.save("kucoin-key-123456", "kucoin-secret-123456", "pass-12345")
            self.assertTrue(binance.keystore.snapshot().complete(False))
            self.assertTrue(kucoin.keystore.snapshot().complete(True))
            self.assertTrue(kucoin.keystore.snapshot().passphrase)
            self.assertIsNone(self.mgr.get("mexc").keystore.snapshot())
            self.assertFalse(binance.keystore.masked()["needs_passphrase"])
            self.assertTrue(kucoin.keystore.masked()["needs_passphrase"])

            await kucoin.db.insert_trade({
                "trade_uid": "u1", "symbol": "SOLUSDTM", "side": "LONG", "status": "CLOSED",
                "qty": 1, "entry_price": 100.0, "exit_price": 110.0, "leverage": 10,
                "margin_usd": 8, "notional_usd": 80, "realized_pnl": 5.0, "roi_pct": 62.5,
                "opened_at": 1.0, "closed_at": 2.0,
            })
            self.assertEqual(await kucoin.db.count_trades(), 1)
            self.assertEqual(await binance.db.count_trades(), 0)
            self.assertEqual(await self.mgr.get("mexc").db.count_trades(), 0)

        asyncio.run(scenario())

    def test_summary_reports_every_venue(self):
        data = asyncio.run(self.mgr.summary())
        self.assertEqual(len(data["venues"]), 3)
        self.assertEqual(data["primary"], "mexc")
        for row in data["venues"]:
            self.assertIn("credentials", row)
            self.assertEqual(row["mode"], "paper")
            # the dashboard tabs read these per venue: fixed opening balance,
            # released (closed) P/L, still-open P/L and the win rate
            for key in ("starting_balance", "released_pnl", "realized_pnl", "open_pnl",
                        "total_pnl", "return_pct", "win_rate", "trades", "wins", "losses"):
                self.assertIn(key, row)
            self.assertEqual(
                row["starting_balance"],
                float(self.cfg.get("account.paper_starting_equity")),
            )

    def test_released_pnl_is_reported_per_venue(self):
        async def scenario():
            kucoin = self.mgr.get("kucoin")
            await kucoin.db.insert_trade({
                "trade_uid": "dash-1", "symbol": "ETHUSDTM", "side": "SHORT", "status": "CLOSED",
                "qty": 2, "entry_price": 3000.0, "exit_price": 2940.0, "leverage": 10,
                "margin_usd": 8, "notional_usd": 80, "realized_pnl": 1.6, "roi_pct": 20.0,
                "opened_at": 10.0, "closed_at": 20.0, "exit_reason": "trailing_stop",
            })
            rows = {r["id"]: r for r in (await self.mgr.summary())["venues"]}
            self.assertEqual(rows["kucoin"]["released_pnl"], 1.6)
            self.assertEqual(rows["kucoin"]["trades"], 1)
            self.assertEqual(rows["kucoin"]["wins"], 1)
            self.assertEqual(rows["kucoin"]["win_rate"], 100.0)
            self.assertEqual(rows["mexc"]["released_pnl"], 0.0)
            self.assertEqual(rows["mexc"]["trades"], 0)

        asyncio.run(scenario())

    def test_per_venue_config_override_only_affects_that_venue(self):
        self.cfg.set_many({"venues.binance.risk.leverage": 7, "risk.max_open_positions": 9})
        self.assertEqual(self.mgr.get("binance").cfg.get("risk.leverage"), 7)
        self.assertEqual(self.mgr.get("mexc").cfg.get("risk.leverage"), 10)
        self.assertEqual(self.mgr.get("kucoin").cfg.get("risk.max_open_positions"), 9)
        self.cfg.reset(["venues.binance.risk.leverage", "risk.max_open_positions"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
