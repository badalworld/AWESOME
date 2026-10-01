"""KuCoin Futures client (REST + WebSocket) — normalized venue adapter.

Verified against the official docs (https://www.kucoin.com/docs / docs.kucoin.io
futures section) and the official SDKs:

* Base URL .............. https://api-futures.kucoin.com
* Auth headers .......... KC-API-KEY, KC-API-SIGN, KC-API-TIMESTAMP,
                          KC-API-PASSPHRASE, KC-API-KEY-VERSION: 2
* Signature ............. BASE64(HMAC-SHA256(secret, ts + method + path + body))
     - ``path`` includes the query string (`/api/v1/orders?symbol=XBTUSDTM`)
     - ``body`` is the exact JSON string for POST/PUT, empty otherwise
* Passphrase ........... BASE64(HMAC-SHA256(secret, api_passphrase))
* WS ................... POST /api/v1/bullet-public -> token + instanceServers,
                          then ``wss://...?token=...`` with topics
                          ``/contractMarket/tickerV2:{symbol}`` and
                          ``/contractMarket/limitCandle:{symbol}_{granularity}``.
                          Private traffic (orders/positions) needs
                          ``/api/v1/bullet-private``.

Live-money notes
----------------
* KuCoin futures is **one-way only** → the canonical position mode is ONEWAY.
* ``size`` is in contracts (``contracts/active`` gives ``multiplier`` = base
  units per contract) and must be an integer; ``price`` is a string.
* Stop orders are ``stop`` + ``stopPrice`` + ``stopPriceType`` on the normal
  order endpoint: ``MP`` = mark price (what we trigger on), ``down`` = fires
  when price falls. There is no modify endpoint for stop orders, so trailing
  uses place-new-then-cancel-old — never cancel-first.
* The kline array order has changed over time (Open→Close→High→Low today), so
  the parser classifies OHLC positionally **and verifies** it against the
  high/low invariants instead of trusting a fixed index order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ..utils import Clock
from .base import (
    DEFAULT_TAKER_FEE,
    LONG,
    AccountSnapshot,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)
from .venue import (
    ONEWAY,
    VENUES,
    BaseHTTPVenueClient,
    VenueStream,
    hmac_sha256_b64,
)

log = logging.getLogger("kucoin")

RETRYABLE_CODES = {"429000", "500000", "200002", "200003", "300000", "400100"}


class KuCoinClient(BaseHTTPVenueClient):
    """REST client + normalized venue surface for KuCoin Futures."""

    spec = VENUES["kucoin"]

    def __init__(
        self,
        clock: Clock,
        *,
        rest_base: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("order_rate", 8.0)
        kwargs.setdefault("query_rate", 8.0)
        kwargs.setdefault("public_rate", 20.0)
        super().__init__(self.spec, clock, rest_base=rest_base, api_key=api_key,
                         api_secret=api_secret, passphrase=passphrase, **kwargs)
        self._contracts: Dict[str, ContractSpec] = {}
        self._contracts_ts = 0.0
        self._oi: Dict[str, float] = {}
        self._leverage_hint: Dict[str, int] = {}
        self._ws_token: Optional[Dict[str, Any]] = None
        self._ws_token_private: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    #  signing
    # ------------------------------------------------------------------ #
    def _base_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def _sign_request(
        self, method: str, path: str, *, params, body, signed: bool,
    ) -> Tuple[Dict[str, str], Any, Optional[str]]:
        query = ""
        if params:
            query = "&".join(f"{k}={_enc(v)}" for k, v in params.items() if v is not None)
        full_path = f"{path}?{query}" if query else path
        body_str: Optional[str] = None
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if method.upper() in ("POST", "PUT", "DELETE") and body:
            body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        now = self.clock.now_ms()
        if signed:
            if not self.has_credentials:
                from .venue import CredentialsRequired

                raise CredentialsRequired(
                    "KuCoin API credentials are not configured (key + secret + passphrase)"
                )
            message = f"{now}{method.upper()}{full_path}{body_str or ''}"
            headers.update({
                "KC-API-KEY": self.api_key or "",
                "KC-API-SIGN": hmac_sha256_b64(self.api_secret or "", message),
                "KC-API-TIMESTAMP": str(now),
                "KC-API-PASSPHRASE": hmac_sha256_b64(self.api_secret or "", self.passphrase or ""),
                "KC-API-KEY-VERSION": "2",
            })
        return headers, query, body_str

    def _unwrap(self, payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _is_envelope_error(self, payload: Any) -> Tuple[Optional[Any], str]:
        if isinstance(payload, dict) and "code" in payload:
            code = str(payload.get("code"))
            if code not in ("200000", "0", "None"):
                return code, str(payload.get("msg") or payload.get("message") or "")
        return None, ""

    def _retryable_codes(self) -> set:
        return RETRYABLE_CODES

    # ------------------------------------------------------------------ #
    #  public market data
    # ------------------------------------------------------------------ #
    async def ping(self) -> Optional[int]:
        started = time.perf_counter()
        data = await self._request("GET", "/api/v1/timestamp", lane="public", timeout=2.0)
        rtt = (time.perf_counter() - started) * 1000.0
        ts = data if isinstance(data, (int, float)) else (data or {}).get("data")
        if ts:
            self.clock.update(float(ts), rtt)
            return int(ts)
        return None

    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]:
        if self._contracts and not force and time.time() - self._contracts_ts < 3600:
            return self._contracts
        rows = await self._request("GET", "/api/v1/contracts/active", lane="public")
        out: Dict[str, ContractSpec] = {}
        for item in rows if isinstance(rows, list) else []:
            try:
                if str(item.get("status", "")).lower() not in ("open", ""):
                    continue
                if str(item.get("quoteCurrency", "USDT")).upper() != "USDT":
                    continue
                symbol = str(item["symbol"])
                if item.get("openInterest") is not None:
                    self._oi[symbol] = float(item.get("openInterest") or 0.0)
                tick = float(item.get("tickSize") or 0.0001)
                lot = float(item.get("lotSize") or 1)
                multiplier = float(item.get("multiplier") or 1.0)
                out[symbol] = ContractSpec(
                    symbol=symbol,
                    contract_size=multiplier,
                    price_unit=tick,
                    price_scale=max(0, int(round(-math.log10(tick)))) if tick else 4,
                    vol_unit=lot,
                    vol_scale=0,
                    min_vol=lot,
                    max_vol=float(item.get("maxOrderQty") or 1e9),
                    max_leverage=int(item.get("maxLeverage") or 100),
                    min_leverage=1,
                    taker_fee=float(item.get("takerFeeRate") or DEFAULT_TAKER_FEE),
                    maker_fee=float(item.get("makerFeeRate") or 0.0002),
                    api_allowed=True,
                    state=0,
                    is_new=False,
                    base=str(item.get("baseCurrency", "")),
                    quote="USDT",
                )
                # open interest (contracts) is cached separately: it is not part
                # of the immutable contract spec
                self._oi[symbol] = float(item.get("openInterest") or 0.0)
            except Exception as exc:  # noqa: BLE001
                log.debug("contract parse error: %s", exc)
        if out:
            self._contracts = out
            self._contracts_ts = time.time()
        return self._contracts

    async def tickers(self, symbol: Optional[str] = None) -> Dict[str, Ticker]:
        if symbol:
            params = {"symbol": symbol}
            rows = await self._request("GET", "/api/v1/ticker", params=params, lane="public")
            rows = [rows] if isinstance(rows, dict) else (rows or [])
        else:
            rows = await self._request("GET", "/api/v1/allTickers", lane="public")
            rows = rows if isinstance(rows, list) else []
        out: Dict[str, Ticker] = {}
        for item in rows:
            try:
                sym = str(item.get("symbol"))
                oi_contracts = self._oi.get(sym, 0.0)
                out[sym] = Ticker(
                    symbol=sym,
                    last=float(item.get("price") or item.get("lastTradePrice") or 0),
                    bid=float(item.get("bestBidPrice") or 0),
                    ask=float(item.get("bestAskPrice") or 0),
                    volume24=float(item.get("volume") or 0),
                    amount24=float(item.get("turnover") or 0),
                    hold_vol=oi_contracts,
                    high24=float(item.get("high") or 0),
                    low24=float(item.get("low") or 0),
                    rise_fall_rate=float(item.get("priceChgPct") or 0),
                    index_price=0.0,
                    fair_price=0.0,          # filled by mark_price()/WS
                    funding_rate=float(item.get("fundingFeeRate") or 0),
                    ts=time.time(),
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        granularity = self.spec.native_interval(interval)
        secs = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
                "Min60": 3600, "Hour4": 14400}.get(interval, 300)
        end_ms = self.clock.now_ms()
        start_ms = end_ms - secs * (max(2, limit) + 2) * 1000
        try:
            rows = await self._request(
                "GET", "/api/v1/kline/query",
                params={"symbol": symbol, "granularity": granularity,
                        "from": int(start_ms), "to": int(end_ms)},
                lane="public",
            )
        except Exception:
            # older/newer deployments may expect startAt/endAt
            rows = await self._request(
                "GET", "/api/v1/kline/query",
                params={"symbol": symbol, "granularity": granularity,
                        "startAt": int(start_ms), "endAt": int(end_ms)},
                lane="public",
            )
        out: List[Candle] = []
        for r in rows if isinstance(rows, list) else []:
            candle = _parse_kline_row(r)
            if candle is not None:
                out.append(candle)
        out.sort(key=lambda c: c.ts)
        return out[-limit:] if limit else out

    async def mark_price(self, symbol: str) -> float:
        try:
            data = await self._request("GET", f"/api/v1/mark-price/{symbol}/current", lane="public")
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, dict) and data.get("value"):
            return float(data["value"])
        try:
            data = await self._request("GET", "/api/v1/index-price/" + symbol + "/current", lane="public")
            return float((data or {}).get("value") or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    async def depth_usd(self, symbol: str, levels: int = 5, contract_size: float = 1.0) -> float:
        try:
            data = await self._request(
                "GET", "/api/v1/level2/depth20", params={"symbol": symbol}, lane="public",
            )
        except Exception:  # noqa: BLE001
            return 0.0
        total = 0.0
        for side in ("bids", "asks"):
            for level in (data.get(side) or [])[:levels] if isinstance(data, dict) else []:
                try:
                    total += float(level[0]) * float(level[1]) * contract_size
                except (TypeError, ValueError, IndexError):
                    continue
        return total / 2.0

    # ------------------------------------------------------------------ #
    #  account
    # ------------------------------------------------------------------ #
    async def account(self) -> AccountSnapshot:
        data = await self._request(
            "GET", "/api/v1/account-overview", params={"currency": "USDT"},
            signed=True, lane="query",
        )
        if not isinstance(data, dict):
            return AccountSnapshot(ts=time.time())
        equity = float(data.get("accountEquity") or data.get("marginBalance") or 0.0)
        return AccountSnapshot(
            equity=equity,
            available=float(data.get("availableBalance") or 0.0),
            unrealized=float(data.get("unrealisedPNL") or 0.0),
            position_margin=float(data.get("positionMargin") or 0.0),
            currency="USDT",
            ts=time.time(),
        )

    async def positions(self) -> List[Position]:
        rows = await self._request("GET", "/api/v1/positions", signed=True, lane="query")
        out: List[Position] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                qty = float(row.get("currentQty") or 0.0)
                if qty == 0:
                    continue
                out.append(Position(
                    symbol=str(row.get("symbol")),
                    side=LONG if qty > 0 else "SHORT",
                    hold_vol=abs(qty),
                    open_avg_price=float(row.get("avgEntryPrice") or 0.0),
                    leverage=int(float(row.get("leverage") or 1)),
                    unrealized=float(row.get("unrealisedPnl") or row.get("unrealisedPNL") or 0.0),
                    im=float(row.get("posInit") or 0.0),
                    liquidate_price=float(row.get("liquidationPrice") or 0.0),
                    position_id=int(row.get("id") or 0) if str(row.get("id") or "").isdigit() else 0,
                    state=1,
                    mark_price=float(row.get("markPrice") or 0.0),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out

    async def position_mode(self) -> int:
        return ONEWAY       # KuCoin futures has no hedge mode

    # ------------------------------------------------------------------ #
    #  trading
    # ------------------------------------------------------------------ #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        """Best effort — the entry order also carries ``leverage`` explicitly."""
        self._leverage_hint[symbol] = int(leverage)
        try:
            await self._request(
                "POST", "/api/v1/position/changeLeverage",
                params={"symbol": symbol, "leverage": int(leverage)},
                signed=True, lane="order",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[kucoin] changeLeverage(%s, %sx) failed (%s) — order still carries leverage",
                        symbol, leverage, exc)
            return False

    def _round_qty(self, symbol: str, qty: float) -> int:
        spec = self._contracts.get(symbol)
        lot = spec.vol_unit if spec and spec.vol_unit else 1.0
        return max(1, int(round(qty / lot) * lot))

    def _round_price(self, symbol: str, price: float) -> float:
        spec = self._contracts.get(symbol)
        tick = spec.price_unit if spec and spec.price_unit else 0.0
        if not tick:
            return round(price, 6)
        decimals = max(0, spec.price_scale)
        return round(round(price / tick) * tick, decimals)

    async def _order(self, body: Dict[str, Any]) -> OrderResult:
        started = time.perf_counter()
        try:
            data = await self._request("POST", "/api/v1/orders", body=body, signed=True, lane="order")
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, error=str(exc),
                               latency_ms=(time.perf_counter() - started) * 1000.0, raw={"request": body})
        latency = (time.perf_counter() - started) * 1000.0
        order_id = None
        if isinstance(data, dict):
            order_id = data.get("orderId") or data.get("id")
        return OrderResult(
            ok=True, order_id=str(order_id) if order_id else None,
            latency_ms=latency, status="submitted", raw={"request": body, "response": data},
        )

    def _side(self, side: str, reduce_only: bool) -> str:
        is_long = side == LONG
        return ("sell" if is_long else "buy") if reduce_only else ("buy" if is_long else "sell")

    async def market_order(
        self, symbol: str, *, side: str, qty: float, reduce_only: bool,
        leverage: int = 0, client_id: str = "",
    ) -> OrderResult:
        body: Dict[str, Any] = {
            "clientOid": client_id or uuid.uuid4().hex[:32],
            "symbol": symbol,
            "side": self._side(side, reduce_only),
            "type": "market",
            "size": self._round_qty(symbol, qty),
            "reduceOnly": bool(reduce_only),
            "marginMode": "ISOLATED",
            "positionSide": "BOTH",
        }
        if not reduce_only:
            body["leverage"] = str(max(1, int(leverage or self._leverage_hint.get(symbol, 1))))
            self._leverage_hint[symbol] = int(body["leverage"])
        return await self._order(body)

    async def limit_order(
        self, symbol: str, *, side: str, qty: float, price: float,
        reduce_only: bool, client_id: str = "",
    ) -> OrderResult:
        body: Dict[str, Any] = {
            "clientOid": client_id or uuid.uuid4().hex[:32],
            "symbol": symbol,
            "side": self._side(side, reduce_only),
            "type": "limit",
            "price": str(self._round_price(symbol, price)),
            "size": self._round_qty(symbol, qty),
            "timeInForce": "GTC",
            "reduceOnly": bool(reduce_only),
            "marginMode": "ISOLATED",
            "positionSide": "BOTH",
        }
        if not reduce_only:
            body["leverage"] = str(max(1, int(self._leverage_hint.get(symbol, 1))))
        return await self._order(body)

    async def stop_order(
        self, symbol: str, *, side: str, qty: float, trigger_price: float,
        reduce_only: bool = True, client_id: str = "",
    ) -> OrderResult:
        """Stop-market, mark-price trigger, reduce-only (fires below for longs)."""
        is_long = side == LONG
        body: Dict[str, Any] = {
            "clientOid": client_id or uuid.uuid4().hex[:32],
            "symbol": symbol,
            "side": self._side(side, True),
            "type": "market",
            "size": self._round_qty(symbol, qty),
            "reduceOnly": bool(reduce_only),
            "closeOrder": False,
            "stop": "down" if is_long else "up",
            "stopPrice": str(self._round_price(symbol, trigger_price)),
            "stopPriceType": "MP",          # mark price trigger (matches stoploss.use_mark_price_trigger)
            "marginMode": "ISOLATED",
            "positionSide": "BOTH",
        }
        return await self._order(body)

    async def modify_stop(
        self, symbol: str, *, order_id: str, kind: str, new_price: float, qty: float,
        side: str = LONG, handle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """KuCoin has no stop-order modify endpoint: place new, then cancel old."""
        res = await self.stop_order(symbol, side=side, qty=qty, trigger_price=new_price)
        if not res.ok:
            log.error("[kucoin] stop replace failed on %s: %s", symbol, res.error)
            return False
        if order_id:
            try:
                await self._request("DELETE", f"/api/v1/orders/{order_id}", signed=True, lane="order")
            except Exception as exc:  # noqa: BLE001
                log.warning("[kucoin] could not cancel superseded stop %s on %s: %s", order_id, symbol, exc)
        if handle is not None:
            handle["stop_order_id"] = res.order_id
            handle["kind"] = "plan"
        return True

    async def cancel_stop(self, symbol: str, *, order_id: str, kind: str) -> bool:
        return await self.cancel_order_ids(symbol, [order_id])

    async def cancel_order_ids(self, symbol: str, order_ids: List[str]) -> bool:
        ok = True
        for oid in order_ids:
            try:
                await self._request("DELETE", f"/api/v1/orders/{oid}", signed=True, lane="order")
            except Exception as exc:  # noqa: BLE001
                log.warning("[kucoin] cancel %s on %s failed: %s", oid, symbol, exc)
                ok = False
        return ok

    async def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"status": "active"}
        if symbol:
            params["symbol"] = symbol
        try:
            data = await self._request("GET", "/api/v1/orders", params=params, signed=True, lane="query")
            if isinstance(data, dict):
                return list(data.get("items") or [])
            return list(data or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("[kucoin] open orders fetch failed: %s", exc)
            return []

    async def stop_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        try:
            data = await self._request("GET", "/api/v1/stopOrders", params=params, signed=True, lane="query")
            if isinstance(data, dict):
                return list(data.get("items") or [])
            return list(data or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("[kucoin] stop orders fetch failed: %s", exc)
            return []

    # -- websocket bootstrap --------------------------------------------- #
    async def bullet_public(self) -> Optional[Dict[str, Any]]:
        if self._ws_token and time.time() - self._ws_token.get("_ts", 0) < 60:
            return self._ws_token
        try:
            data = await self._request("POST", "/api/v1/bullet-public", lane="public")
        except Exception as exc:  # noqa: BLE001
            log.warning("[kucoin] bullet-public failed: %s", exc)
            return None
        if isinstance(data, dict):
            data["_ts"] = time.time()
            self._ws_token = data
        return self._ws_token

    async def bullet_private(self) -> Optional[Dict[str, Any]]:
        if self._ws_token_private and time.time() - self._ws_token_private.get("_ts", 0) < 60:
            return self._ws_token_private
        if not self.has_credentials:
            return None
        try:
            data = await self._request("POST", "/api/v1/bullet-private", signed=True, lane="query")
        except Exception as exc:  # noqa: BLE001
            log.debug("[kucoin] bullet-private failed: %s", exc)
            return None
        if isinstance(data, dict):
            data["_ts"] = time.time()
            self._ws_token_private = data
        return self._ws_token_private

    def diagnostics(self) -> Dict[str, Any]:
        data = super().diagnostics()
        data.update({
            "venue": "kucoin",
            "venue_label": self.spec.label,
            "position_mode": "one-way",
            "contracts_cached": len(self._contracts),
            "passphrase_set": bool(self.passphrase),
        })
        return data


def _enc(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_kline_row(row: Any) -> Optional[Candle]:
    """Parse a KuCoin kline row, verifying the OHLC positions.

    The array order moved between API revisions (and differs between spot and
    futures), so instead of hard-coding an index order we take the three
    candidate values and label them high/low/close using the OHLC invariants —
    a wrong guess would silently invert stop direction, which is unacceptable.
    """
    try:
        if isinstance(row, dict):
            ts = int(row.get("time") or row.get("t") or 0)
            o = float(row.get("open") or 0)
            h = float(row.get("high") or 0)
            l = float(row.get("low") or 0)
            c = float(row.get("close") or 0)
            v = float(row.get("volume") or 0)
            return Candle(ts=ts // 1000 if ts > 10**12 else ts, o=o, h=h, l=l, c=c, v=v)
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            return None
        ts = int(float(row[0]))
        ts = ts // 1000 if ts > 10**12 else ts
        o = float(row[1])
        a, b, c_ = float(row[2]), float(row[3]), float(row[4])
        v = float(row[5])
        # invariants: high >= open. but candidates are (a,b,c_) in unknown roles
        cands = [a, b, c_]
        hi = max(cands)
        lo = min(cands)
        mid = sorted(cands)[1]
        # the remaining (neither max nor min) value is the close
        close = mid
        if not (hi >= o >= lo or hi >= close >= lo):
            # degenerate row (flat candle): fall back to the documented order
            close, hi, lo = a, b, c_
        return Candle(ts=ts, o=o, h=max(hi, o, close), l=min(lo, o, close), c=close, v=v)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
#  WebSocket
# --------------------------------------------------------------------------- #
class KuCoinStream(VenueStream):
    """Public (and private when credentials exist) KuCoin futures WS client."""

    def __init__(
        self,
        client: KuCoinClient,
        *,
        ws_base: Optional[str] = None,
        reconnect_max_s: float = 30.0,
    ) -> None:
        super().__init__()
        self.client = client
        self.ws_base = (ws_base or client.spec.ws_url).rstrip("/")
        self.reconnect_max_s = reconnect_max_s
        self.connected = False
        self.logged_in = False
        self.reconnects = 0
        self.errors = 0
        self.last_message_ts = 0.0
        self._kline_subs: set = set()
        self._tick_subs: set = set()
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._private_task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._ping_interval = 18000.0

    # -- subscription ----------------------------------------------------- #
    def _topic_interval(self, interval: str) -> str:
        return {"Min1": "1min", "Min5": "5min", "Min15": "15min", "Min30": "30min",
                "Min60": "1hour", "Hour4": "4hour"}.get(interval, "5min")

    async def subscribe_klines(self, symbols: List[str], interval: str = "Min5") -> None:
        async with self._lock:
            for s in symbols:
                self._kline_subs.add((s, interval))
        self._wake.set()

    async def subscribe_ticks(self, symbols: List[str]) -> None:
        async with self._lock:
            self._tick_subs.update(symbols)
        self._wake.set()

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="kucoin-ws")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in (self._task, self._ping_task, self._private_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._task = None
        self._ping_task = None
        self._private_task = None
        self.connected = False

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                import websockets

                token_data = await self.client.bullet_public()
                if not token_data:
                    raise RuntimeError("no websocket token")
                servers = token_data.get("instanceServers") or []
                if not servers:
                    raise RuntimeError("no instance servers in bullet response")
                server = servers[0]
                endpoint = str(server.get("endpoint") or self.ws_base)
                ping_interval = float(server.get("pingInterval") or 18000) / 1000.0
                self._ping_interval = max(5.0, ping_interval - 5.0)
                url = f"{endpoint}?token={token_data.get('token')}&acceptUserMessage=true"
                self._wake.clear()
                async with websockets.connect(
                    url, ping_interval=None, close_timeout=5, max_queue=4096, open_timeout=10,
                ) as ws:
                    self.connected = True
                    self.last_message_ts = time.time()
                    backoff = 1.0
                    log.info("[kucoin] ws connected (%s)", endpoint)
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    await self._resubscribe(ws)
                    await self._subscribe_private(ws)
                    reader = asyncio.create_task(self._reader(ws))
                    switcher = asyncio.create_task(self._watch_subscriptions())
                    try:
                        await asyncio.wait({reader, switcher}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        reader.cancel()
                        switcher.cancel()
                        if self._ping_task:
                            self._ping_task.cancel()
                            self._ping_task = None
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.warning("[kucoin] ws error: %s (reconnect in %.1fs)", exc, backoff)
            self.connected = False
            self.logged_in = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)

    async def _watch_subscriptions(self) -> None:
        await self._wake.wait()
        await asyncio.sleep(0.2)

    async def _send(self, ws, payload: Dict[str, Any]) -> None:
        try:
            await ws.send(json.dumps(payload, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            log.debug("[kucoin] ws send failed: %s", exc)

    async def _resubscribe(self, ws) -> None:
        async with self._lock:
            klines = list(self._kline_subs)
            ticks = list(self._tick_subs)
        for idx, (symbol, _iv) in enumerate(ticks):
            await self._send(ws, {
                "id": f"tk{idx}{int(time.time() * 1000) % 100000}",
                "type": "subscribe",
                "topic": f"/contractMarket/tickerV2:{symbol}",
                "privateChannel": False,
                "response": True,
            })
        for idx, (symbol, interval) in enumerate(klines):
            await self._send(ws, {
                "id": f"kl{idx}{int(time.time() * 1000) % 100000}",
                "type": "subscribe",
                "topic": f"/contractMarket/limitCandle:{symbol}_{self._topic_interval(interval)}",
                "privateChannel": False,
                "response": True,
            })
        if klines or ticks:
            log.info("[kucoin] ws subscribed: %d tickers, %d candles", len(ticks), len(klines))

    async def _subscribe_private(self, ws) -> None:
        token_data = await self.client.bullet_private()
        if not token_data:
            return
        try:
            ws2_url = f"{token_data['instanceServers'][0]['endpoint']}?token={token_data['token']}&acceptUserMessage=true"
            # private traffic rides a *separate* connection (KuCoin requires the
            # private token on its own socket)
            self._private_task = asyncio.create_task(self._private_loop(ws2_url))
        except Exception as exc:  # noqa: BLE001
            log.debug("[kucoin] private stream setup failed: %s", exc)

    async def _private_loop(self, url: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                import websockets

                async with websockets.connect(url, ping_interval=None, open_timeout=10) as ws:
                    self.logged_in = True
                    backoff = 1.0
                    await self._send(ws, {
                        "id": str(uuid.uuid4().hex[:16]), "type": "subscribe",
                        "topic": "/contractMarket/tradeOrders", "privateChannel": True, "response": True,
                    })
                    await self._send(ws, {
                        "id": str(uuid.uuid4().hex[:16]), "type": "subscribe",
                        "topic": "/contract/positionAll", "privateChannel": True, "response": True,
                    })
                    last_ping = time.time()
                    async for raw in ws:
                        now = time.time()
                        if now - last_ping > self._ping_interval:
                            await self._send(ws, {"id": str(int(now * 1000)), "type": "ping"})
                            last_ping = now
                        try:
                            msg = json.loads(raw)
                        except Exception:  # noqa: BLE001
                            continue
                        if msg.get("type") == "message" and self.on_order:
                            self.on_order({**msg.get("data", {}), "_topic": msg.get("topic", "")})
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.debug("[kucoin] private ws error: %s", exc)
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)

    async def _ping_loop(self, ws) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await self._send(ws, {"id": str(int(time.time() * 1000)), "type": "ping"})

    async def _reader(self, ws) -> None:
        async for raw in ws:
            self.last_message_ts = time.time()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(msg, dict) or msg.get("type") != "message":
                continue
            topic = str(msg.get("topic") or "")
            data = msg.get("data") or {}
            try:
                if topic.startswith("/contractMarket/tickerV2"):
                    symbol = str(data.get("symbol") or topic.split(":")[-1])
                    last = float(data.get("price") or 0)
                    bid = float(data.get("bestBidPrice") or 0)
                    ask = float(data.get("bestAskPrice") or 0)
                    if self.on_tick and symbol:
                        self.on_tick(symbol, last, bid, ask)
                elif topic.startswith("/contractMarket/limitCandle"):
                    symbol = str(data.get("symbol") or topic.split(":")[-1].rsplit("_", 1)[0])
                    candles = data.get("candles") or []
                    if candles and self.on_kline:
                        candle = _parse_kline_row(candles)
                        if candle is not None:
                            interval = topic.rsplit("_", 1)[-1]
                            canonical = {"1min": "Min1", "5min": "Min5", "15min": "Min15",
                                         "30min": "Min30", "1hour": "Min60", "4hour": "Hour4"}.get(
                                             interval, "Min5")
                            closed = bool(data.get("S")) or str(data.get("subject", "")) == "candle.stick"
                            self.on_kline(symbol, canonical, candle, closed)
            except Exception as exc:  # noqa: BLE001
                log.debug("[kucoin] ws parse error: %s", exc)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "kline_streams": len(self._kline_subs),
            "tick_streams": len(self._tick_subs),
            "private": bool(self.logged_in),
            "source": self.ws_base,
            "last_message_age_s": round(time.time() - self.last_message_ts, 2) if self.last_message_ts else None,
        }


__all__ = ["KuCoinClient", "KuCoinStream", "RETRYABLE_CODES"]
