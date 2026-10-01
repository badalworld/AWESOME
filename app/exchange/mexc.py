"""MEXC USDT-M futures client (REST + WebSocket).

Verified against the official docs (https://www.mexc.com/api-docs/futures):

* Base URL ................ https://api.mexc.com
* Auth headers ............ ApiKey / Request-Time / Signature / Recv-Window
* Signature ............... HMAC-SHA256(secret, accessKey + timestamp + paramString)
    - GET/DELETE: paramString = business params sorted by key, joined with '&'
    - POST:       paramString = the exact JSON body string (no sorting)
* WS endpoint ............. wss://contract.mexc.com/edge
    - login: {"method":"login","param":{apiKey,reqTime,signature}}
    - channels: sub.kline / sub.ticker / sub.deal + private push.personal.*

Low-latency design
------------------
* One long-lived HTTP/2 connection pool (keep-alive, pre-warmed) — no TLS
  handshake or DNS lookup on the order hot path.
* Independent token buckets: order ops vs queries vs public data, so an exit
  order is never queued behind a universe scan.
* ``externalOid`` idempotency keys make retries safe (no duplicate orders).
* Wall-clock offset is measured against the exchange and reused for signing.
* Per-call latency is recorded for p50/p95/p99 telemetry in the dashboard.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from ..utils import Clock, LatencyTracker
from .base import (
    DEFAULT_TAKER_FEE,
    LONG,
    OPEN_ISOLATED,
    ORDER_MARKET,
    SHORT,
    SIDE_CLOSE_LONG,
    SIDE_CLOSE_SHORT,
    SIDE_OPEN_LONG,
    SIDE_OPEN_SHORT,
    AccountSnapshot,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)
from .venue import HEDGE, VENUES, VenueClient, normalize_order_status

log = logging.getLogger("mexc")

# MEXC kline `interval` values
# Error codes worth an automatic retry (transient / rate limits)
RETRYABLE_CODES = {429, 500, 502, 503, 504, 1001, 1002, 700001}


class RateLimiter:
    """Async token bucket (separate bucket per lane)."""

    def __init__(self, rate_per_sec: float, burst: int) -> None:
        self.rate = rate_per_sec
        self.capacity = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(min(wait, 0.25))


class MeXCClient:
    """REST client. Works without credentials for public data."""

    def __init__(
        self,
        rest_base: str,
        clock: Clock,
        *,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        recv_window_ms: int = 5000,
        timeout_s: float = 5.0,
        http2: bool = True,
        max_connections: int = 20,
        keepalive_expiry: float = 300.0,
        retry_attempts: int = 3,
        retry_backoff_ms: int = 120,
        telemetry: Optional[LatencyTracker] = None,
    ) -> None:
        self.rest_base = rest_base.rstrip("/")
        self.clock = clock
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = int(recv_window_ms)
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_ms = retry_backoff_ms
        self.telemetry = telemetry
        self._client: Optional[httpx.AsyncClient] = None
        self._timeout = timeout_s
        self._max_connections = max_connections
        self._keepalive_expiry = keepalive_expiry
        self._http2 = http2
        # lanes: order ops are serialised lightly but never starved by scans
        self._order_lane = RateLimiter(rate_per_sec=9.0, burst=6)     # MEXC: ~20 req/2s
        self._query_lane = RateLimiter(rate_per_sec=9.0, burst=6)
        self._public_lane = RateLimiter(rate_per_sec=25.0, burst=20)
        self.stats: Dict[str, Any] = {"requests": 0, "errors": 0, "retries": 0, "last_error": ""}

    # ------------------------------------------------------------------ #
    #  lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._client is not None:
            return
        limits = httpx.Limits(
            max_connections=self._max_connections,
            max_keepalive_connections=self._max_connections,
            keepalive_expiry=self._keepalive_expiry,
        )
        self._client = httpx.AsyncClient(
            base_url=self.rest_base,
            http2=self._http2,
            timeout=httpx.Timeout(self._timeout, connect=min(3.0, self._timeout)),
            limits=limits,
            headers={"User-Agent": "awesome-ao-bot/1.0", "Language": "en_US"},
            follow_redirects=False,
        )
        try:
            await self.sync_time()
        except Exception as exc:  # noqa: BLE001
            log.warning("time sync failed at startup: %s", exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def update_credentials(self, api_key: Optional[str], api_secret: Optional[str]) -> None:
        self.api_key = api_key
        self.api_secret = api_secret

    # ------------------------------------------------------------------ #
    #  signing
    # ------------------------------------------------------------------ #
    def _headers(self, signature: str, ts: int) -> Dict[str, str]:
        return {
            "ApiKey": self.api_key or "",
            "Request-Time": str(ts),
            "Signature": signature,
            "Recv-Window": str(self.recv_window),
            "Content-Type": "application/json",
        }

    def _sign(self, ts: int, param_string: str) -> str:
        msg = f"{self.api_key}{ts}{param_string}".encode("utf-8")
        return hmac.new(self.api_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    def ws_login_params(self) -> Dict[str, str]:
        ts = self.clock.now_ms()
        return {
            "apiKey": self.api_key or "",
            "reqTime": str(ts),
            "signature": self._sign(ts, ""),
        }

    # ------------------------------------------------------------------ #
    #  transport
    # ------------------------------------------------------------------ #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        lane: str = "public",
        signed: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if self._client is None:
            raise RuntimeError("MeXCClient.start() was not awaited")
        limiter = {"order": self._order_lane, "query": self._query_lane, "public": self._public_lane}[lane]

        body_str: Optional[str] = None
        headers: Dict[str, str] = {}
        query: Optional[Dict[str, Any]] = None

        if signed:
            if not self.has_credentials:
                raise PermissionError("API credentials are not configured")
            ts = self.clock.now_ms()
            if method.upper() == "POST":
                body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
                signature = self._sign(ts, body_str)
            else:
                clean = {k: v for k, v in (params or {}).items() if v is not None}
                param_string = "&".join(f"{k}={clean[k]}" for k in sorted(clean))
                signature = self._sign(ts, param_string)
                query = clean
            headers = self._headers(signature, ts)
        else:
            query = {k: v for k, v in (params or {}).items() if v is not None}
            headers = {"Content-Type": "application/json"}

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt < self.retry_attempts:
            attempt += 1
            await limiter.acquire()
            started = time.perf_counter()
            try:
                self.stats["requests"] += 1
                resp = await self._client.request(
                    method.upper(),
                    path,
                    params=query,
                    content=body_str,
                    headers=headers,
                    timeout=timeout or self._timeout,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if self.telemetry is not None:
                    await self.telemetry.record(elapsed_ms)

                if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                    raise _RetryableError(f"HTTP {resp.status_code}")
                try:
                    payload = resp.json()
                except ValueError:
                    raise _FatalError(f"non-JSON response (HTTP {resp.status_code}): {resp.text[:180]}")

                if resp.status_code >= 400:
                    raise _FatalError(f"HTTP {resp.status_code}: {str(payload)[:200]}")

                if isinstance(payload, dict) and payload.get("success") is False:
                    code = payload.get("code")
                    msg = payload.get("message") or payload.get("msg") or ""
                    if code in RETRYABLE_CODES:
                        raise _RetryableError(f"code={code} {msg}")
                    raise _FatalError(f"code={code} {msg}")
                return payload if isinstance(payload, dict) else {"success": True, "data": payload}
            except (_RetryableError, httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = exc
                self.stats["retries"] += 1
                if attempt >= self.retry_attempts:
                    break
                # jittered backoff keeps retries from synchronising across symbols
                backoff = self.retry_backoff_ms * attempt * (1.0 + 0.25 * (time.perf_counter() % 1.0))
                await asyncio.sleep(backoff / 1000.0)
            except _FatalError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.retry_attempts:
                    break
                await asyncio.sleep(self.retry_backoff_ms * attempt / 1000.0)

        self.stats["errors"] += 1
        self.stats["last_error"] = str(last_err)
        raise RuntimeError(f"{method} {path} failed after {attempt} attempts: {last_err}")

    # ------------------------------------------------------------------ #
    #  public market data
    # ------------------------------------------------------------------ #
    async def ping(self) -> Optional[int]:
        """Server time in ms (also used for clock sync)."""
        started = time.perf_counter()
        payload = await self._request("GET", "/api/v1/contract/ping", lane="public", timeout=2.0)
        rtt = (time.perf_counter() - started) * 1000.0
        data = payload.get("data")
        server_ms = int(data) if isinstance(data, (int, float)) else None
        if server_ms is None:
            server_ms = payload.get("timestamp") if isinstance(payload.get("timestamp"), (int, float)) else None
        if server_ms:
            self.clock.update(float(server_ms), rtt)
            return int(server_ms)
        return None

    async def sync_time(self) -> None:
        ts = await self.ping()
        if ts:
            log.debug("clock synced: offset=%.1fms rtt=%.1fms", self.clock.offset_ms, self.clock.rtt_ms)

    async def contract_details(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._request("GET", "/api/v1/contract/detail", params=params, lane="public")
        data = payload.get("data") or []
        if isinstance(data, dict):
            return [data]
        return list(data)

    async def contract_detail_country(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._request("GET", "/api/v1/contract/detail/country", params=params, lane="public")
        data = payload.get("data") or []
        if isinstance(data, dict):
            return [data]
        return list(data)

    async def tickers(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._request("GET", "/api/v1/contract/ticker", params=params, lane="public")
        data = payload.get("data") or []
        if isinstance(data, dict):
            return [data]
        return list(data)

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        # MEXC caps at 2000 points per call; ask for exactly what we need
        payload = await self._request(
            "GET",
            f"/api/v1/contract/kline/{symbol}",
            params={"interval": interval},
            lane="public",
        )
        data = payload.get("data") or {}
        times = data.get("time") or []
        opens = data.get("open") or []
        highs = data.get("high") or []
        lows = data.get("low") or []
        closes = data.get("close") or []
        vols = data.get("vol") or []
        n = min(len(times), len(opens), len(highs), len(lows), len(closes), len(vols))
        candles = [
            Candle(
                ts=int(times[i]),
                o=float(opens[i]),
                h=float(highs[i]),
                l=float(lows[i]),
                c=float(closes[i]),
                v=float(vols[i]),
            )
            for i in range(n)
        ]
        return candles[-limit:] if limit else candles

    async def mark_price(self, symbol: str) -> float:
        payload = await self._request("GET", f"/api/v1/contract/fair_price/{symbol}", lane="public")
        data = payload.get("data") or {}
        return float(data.get("fairPrice") or 0.0)

    async def depth_usd(self, symbol: str, levels: int = 5, contract_size: float = 1.0) -> float:
        """Notional resting within ``levels`` of the touch (both sides)."""
        payload = await self._request("GET", f"/api/v1/contract/depth/{symbol}",
                                      params={"limit": levels}, lane="public")
        data = payload.get("data") or {}
        total = 0.0
        for side in ("bids", "asks"):
            for level in (data.get(side) or [])[:levels]:
                try:
                    price, vol = float(level[0]), float(level[1])
                    total += price * vol * contract_size
                except (TypeError, ValueError, IndexError):
                    continue
        return total / 2.0        # one-sided depth

    # ------------------------------------------------------------------ #
    #  private: account & positions
    # ------------------------------------------------------------------ #
    async def assets(self) -> List[Dict[str, Any]]:
        payload = await self._request("GET", "/api/v1/private/account/assets", signed=True, lane="query")
        data = payload.get("data") or []
        return list(data) if isinstance(data, list) else [data]

    async def open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._request(
            "GET", "/api/v1/private/position/open_positions", params=params, signed=True, lane="query"
        )
        data = payload.get("data") or []
        return list(data)

    async def position_mode(self) -> int:
        payload = await self._request(
            "GET", "/api/v1/private/position/position_mode", signed=True, lane="query"
        )
        data = payload.get("data")
        # The endpoint is documented with a risk-limit style payload on some
        # accounts and a plain {"positionMode": n} on others — accept both.
        if isinstance(data, dict) and "positionMode" in data:
            return int(data["positionMode"])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "positionMode" in item:
                    return int(item["positionMode"])
        return 1


    # ------------------------------------------------------------------ #
    #  private: orders
    # ------------------------------------------------------------------ #
    async def create_order(
        self,
        *,
        symbol: str,
        vol: float,
        side: int,
        order_type: int = ORDER_MARKET,
        price: Optional[float] = None,
        leverage: Optional[int] = None,
        open_type: int = OPEN_ISOLATED,
        reduce_only: Optional[bool] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        loss_trend: int = 2,
        profit_trend: int = 2,
        price_protect: int = 1,
        external_oid: Optional[str] = None,
        position_mode: Optional[int] = None,
    ) -> OrderResult:
        """POST /api/v1/private/order/create — the order hot path."""
        body: Dict[str, Any] = {
            "symbol": symbol,
            "vol": vol,
            "side": side,
            "type": order_type,
            "openType": open_type,
        }
        if price is not None:
            body["price"] = price
        if leverage is not None:
            body["leverage"] = int(leverage)
        if reduce_only is not None:
            body["reduceOnly"] = bool(reduce_only)
        if stop_loss_price:
            body["stopLossPrice"] = stop_loss_price
            body["lossTrend"] = loss_trend
            body["priceProtect"] = price_protect
        if take_profit_price:
            body["takeProfitPrice"] = take_profit_price
            body["profitTrend"] = profit_trend
            body["priceProtect"] = price_protect
        if external_oid:
            body["externalOid"] = external_oid
        if position_mode:
            body["positionMode"] = int(position_mode)

        started = time.perf_counter()
        try:
            payload = await self._request(
                "POST", "/api/v1/private/order/create", body=body, signed=True, lane="order"
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, error=str(exc), latency_ms=(time.perf_counter() - started) * 1000.0,
                               raw={"request": body})
        latency = (time.perf_counter() - started) * 1000.0
        data = payload.get("data") or {}
        return OrderResult(
            ok=True,
            order_id=str(data.get("orderId")) if isinstance(data, dict) and data.get("orderId") else None,
            latency_ms=latency,
            status="submitted",
            raw={"request": body, "response": payload},
        )

    async def order_by_id(self, order_id: str) -> Optional[Dict[str, Any]]:
        payload = await self._request(
            "GET", f"/api/v1/private/order/get/{order_id}", signed=True, lane="query"
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def order_by_external_id(self, symbol: str, external_oid: str) -> Optional[Dict[str, Any]]:
        payload = await self._request(
            "GET", f"/api/v1/private/order/external/{symbol}/{external_oid}", signed=True, lane="query"
        )
        data = payload.get("data")
        return data if isinstance(data, dict) else None


    async def open_orders(self, symbol: str = "", page_size: int = 100) -> List[Dict[str, Any]]:
        """GET /api/v1/private/order/list/open_orders — regular resting orders.

        Plan/trigger orders live on a different endpoint (``plan_orders``); this
        one is used by the repair path to find *resting* orders that would
        otherwise survive as pending orders after the market-only audit. The
        endpoint is paginated and has no symbol filter, so the caller filters.
        """
        payload = await self._request(
            "GET", "/api/v1/private/order/list/open_orders",
            params={"page_num": 1, "page_size": max(1, min(100, page_size))},
            signed=True, lane="query",
        )
        data = payload.get("data")
        if isinstance(data, dict):
            data = data.get("resultList") or data.get("list") or data.get("items") or []
        rows = list(data or [])
        if symbol:
            rows = [r for r in rows if str(r.get("symbol") or "") == symbol]
        return rows

    async def cancel_orders(self, order_ids: List[str]) -> List[Dict[str, Any]]:
        if not order_ids:
            return []
        payload = await self._request(
            "POST", "/api/v1/private/order/cancel",
            body={"orderIds": [int(o) if str(o).isdigit() else o for o in order_ids]},
            signed=True, lane="order",
        )
        data = payload.get("data") or []
        return list(data)

    # ------------------------------------------------------------------ #
    #  private: leverage
    # ------------------------------------------------------------------ #
    async def change_leverage(
        self, symbol: str, leverage: int, position_type: int = 1,
        open_type: int = OPEN_ISOLATED, position_id: Optional[int] = None,
    ) -> bool:
        body: Dict[str, Any] = {"leverage": int(leverage), "openType": open_type}
        if position_id:
            body["positionId"] = int(position_id)
        else:
            body["symbol"] = symbol
            body["positionType"] = int(position_type)
        try:
            payload = await self._request(
                "POST", "/api/v1/private/position/change_leverage", body=body, signed=True, lane="order"
            )
            return bool(payload.get("success", True))
        except Exception as exc:  # noqa: BLE001
            log.warning("change_leverage(%s, %sx) failed: %s", symbol, leverage, exc)
            return False

    # ------------------------------------------------------------------ #
    #  private: TP/SL (attached) + plan orders
    # ------------------------------------------------------------------ #
    async def tpsl_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        payload = await self._request(
            "GET", "/api/v1/private/stoporder/open_orders", params=params, signed=True, lane="query"
        )
        data = payload.get("data") or []
        return list(data)

    async def plan_orders(self, symbol: str) -> List[Dict[str, Any]]:
        """Open *standalone* trigger orders (our separate SL legs) for a symbol."""
        payload = await self._request(
            "GET", "/api/v1/private/planorder/list/orders",
            params={"symbol": symbol}, signed=True, lane="query",
        )
        data = payload.get("data") or []
        return list(data)

    async def change_attached_stop(
        self,
        *,
        symbol: str,
        order_id: str,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        loss_trend: int = 2,
        profit_trend: int = 2,
    ) -> bool:
        """POST /api/v1/private/planorder/change_stop_order.

        Updates the TP/SL attached to our entry order in a *single* call, so the
        position is never left unprotected while a trailing stop steps up.
        Sending stopLossPrice=0 cancels the stop-loss leg.
        """
        body: Dict[str, Any] = {"symbol": symbol, "orderId": int(order_id) if str(order_id).isdigit() else order_id}
        if stop_loss_price is not None:
            body["stopLossPrice"] = stop_loss_price
            body["lossTrend"] = loss_trend
            body["priceProtect"] = 1        # never let a wick "trigger" the stop
        if take_profit_price is not None:
            body["takeProfitPrice"] = take_profit_price
            body["profitTrend"] = profit_trend
        payload = await self._request(
            "POST", "/api/v1/private/planorder/change_stop_order", body=body, signed=True, lane="order"
        )
        return bool(payload.get("success", True))

    async def place_plan_order(
        self,
        *,
        symbol: str,
        vol: float,
        side: int,
        trigger_price: float,
        execute_price: Optional[float] = None,
        trigger_type: int = 2,
        order_type: int = ORDER_MARKET,
        trend: int = 2,
        reduce_only: bool = True,
        open_type: int = OPEN_ISOLATED,
        leverage: Optional[int] = None,
        position_mode: Optional[int] = None,
    ) -> OrderResult:
        """POST /api/v1/private/planorder/place/v2 — standalone stop/trigger order."""
        body: Dict[str, Any] = {
            "symbol": symbol,
            "vol": vol,
            "side": side,
            "triggerPrice": trigger_price,
            "triggerType": trigger_type,
            "executeCycle": 1,
            "orderType": order_type,
            "trend": trend,
            "openType": open_type,
            "reduceOnly": reduce_only,
        }
        if execute_price is not None:
            body["price"] = execute_price
        if leverage is not None:
            body["leverage"] = int(leverage)
        if position_mode:
            body["positionMode"] = int(position_mode)
        started = time.perf_counter()
        try:
            payload = await self._request(
                "POST", "/api/v1/private/planorder/place/v2", body=body, signed=True, lane="order"
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, error=str(exc), latency_ms=(time.perf_counter() - started) * 1000.0)
        data = payload.get("data")
        return OrderResult(
            ok=True,
            order_id=str(data) if data else None,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            status="placed",
            raw={"request": body, "response": payload},
        )

    async def modify_plan_order(
        self,
        *,
        symbol: str,
        order_id: str,
        trigger_price: float,
        execute_price: Optional[float] = None,
        order_type: int = ORDER_MARKET,
        trigger_type: int = 2,
        trend: int = 2,
    ) -> bool:
        """POST /api/v1/private/planorder/change_price — move a resting stop.

        ``orderType=5`` (market) executes at market when triggered, so the
        ``price`` field must **not** carry the trigger price: sending it could be
        read as a limit execution price and turn the protective stop into a
        resting limit order (2026-10 live audit). Market moves therefore send
        ``price: 0``. A few MEXC deployments insist on ``price > 0``; that case
        retries once with the trigger price so the ratchet still happens.
        """
        def _body(price: float) -> Dict[str, Any]:
            return {
                "symbol": symbol,
                "orderId": int(order_id) if str(order_id).isdigit() else order_id,
                "triggerPrice": trigger_price,
                "price": price,
                "orderType": order_type,
                "triggerType": trigger_type,
                "trend": trend,
                "from": 2,
            }

        prices = [0.0, trigger_price] if order_type == ORDER_MARKET else [
            float(execute_price if execute_price is not None else trigger_price)
        ]
        last_exc: Optional[Exception] = None
        for idx, price in enumerate(prices):
            try:
                payload = await self._request(
                    "POST", "/api/v1/private/planorder/change_price",
                    body=_body(price), signed=True, lane="order",
                )
                if idx and order_type == ORDER_MARKET:
                    log.warning(
                        "[mexc] plan-order move for %s required price=%s (market price=0 was refused)",
                        symbol, price,
                    )
                return bool(payload.get("success", True))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        raise RuntimeError(f"plan-order move failed on {symbol}: {last_exc}")

    async def cancel_plan_orders(self, symbol: str, order_ids: List[str]) -> bool:
        if not order_ids:
            return True
        body = {"orders": [{"symbol": symbol, "orderId": o} for o in order_ids]}
        payload = await self._request(
            "POST", "/api/v1/private/planorder/cancel", body=body, signed=True, lane="order"
        )
        return bool(payload.get("success", True))

    # ------------------------------------------------------------------ #
    def diagnostics(self) -> Dict[str, Any]:
        return {
            "base": self.rest_base,
            "clock_offset_ms": round(self.clock.offset_ms, 2),
            "clock_rtt_ms": round(self.clock.rtt_ms, 2),
            "clock_age_s": round(self.clock.age_s, 1) if self.clock.age_s != float("inf") else None,
            "credentials": bool(self.api_key and self.api_secret),
            "stats": dict(self.stats),
        }


class _RetryableError(Exception):
    pass


class _FatalError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  WebSocket streaming
# --------------------------------------------------------------------------- #
class MeXCWebSocket:
    """Resilient WS client: auto-reconnect, auto-resubscribe, private login.

    Feeds the engine with real-time candles, mark price ticks and private
    order/position pushes (used for fill confirmation with minimal latency).
    """

    def __init__(
        self,
        url: str,
        client: MeXCClient,
        *,
        on_kline: Optional[Callable[[str, str, Candle, bool], None]] = None,
        on_tick: Optional[Callable[[str, float, float, float], None]] = None,
        on_order: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_position: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_asset: Optional[Callable[[Dict[str, Any]], None]] = None,
        reconnect_max_s: float = 30.0,
    ) -> None:
        self.url = url
        self.client = client
        self.on_kline = on_kline
        self.on_tick = on_tick
        self.on_order = on_order
        self.on_position = on_position
        self.on_asset = on_asset
        self.reconnect_max_s = reconnect_max_s

        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._kline_subs: set = set()       # (symbol, interval)
        self._tick_subs: set = set()        # symbol
        self._lock = asyncio.Lock()
        self.connected = False
        self.logged_in = False
        self.last_message_ts = 0.0
        self.reconnects = 0
        self.errors = 0

    # -- subscription management ---------------------------------------- #
    async def subscribe_klines(self, symbols: List[str], interval: str = "Min5") -> None:
        async with self._lock:
            new = [(s, interval) for s in symbols if (s, interval) not in self._kline_subs]
            self._kline_subs.update(new)
        for symbol, iv in new:
            await self._send({"method": "sub.kline", "param": {"symbol": symbol, "interval": iv}})

    async def subscribe_ticks(self, symbols: List[str]) -> None:
        async with self._lock:
            new = [s for s in symbols if s not in self._tick_subs]
            self._tick_subs.update(new)
        for symbol in new:
            await self._send({"method": "sub.ticker", "param": {"symbol": symbol}})

    # -- lifecycle ------------------------------------------------------- #
    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self._ws is None or not self.connected:
            return
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            log.debug("ws send failed: %s", exc)

    async def _resubscribe(self) -> None:
        async with self._lock:
            klines = list(self._kline_subs)
            ticks = list(self._tick_subs)
        for symbol, iv in klines:
            await self._send({"method": "sub.kline", "param": {"symbol": symbol, "interval": iv}})
        for symbol in ticks:
            await self._send({"method": "sub.ticker", "param": {"symbol": symbol}})
        if klines or ticks:
            log.info("ws resubscribed: %d kline, %d ticker streams", len(klines), len(ticks))

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                import websockets  # local import: optional dependency at import time

                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=5,
                    max_queue=4096,
                    open_timeout=10,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_message_ts = time.time()
                    backoff = 1.0
                    log.info("ws connected: %s", self.url)
                    if self.client.has_credentials:
                        await self._send({"method": "login", "param": self.client.ws_login_params()})
                    await self._resubscribe()
                    pinger = asyncio.create_task(self._ping_loop())
                    try:
                        async for raw in ws:
                            self.last_message_ts = time.time()
                            await self._handle(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.warning("ws error: %s (reconnecting in %.1fs)", exc, backoff)
            self.connected = False
            self.logged_in = False
            self._ws = None
            if self._stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            await self._send({"method": "ping"})

    async def _handle(self, raw: Any) -> None:
        try:
            if isinstance(raw, bytes):
                import gzip

                raw = gzip.decompress(raw).decode("utf-8")
            msg = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:  # noqa: BLE001
            return
        if not isinstance(msg, dict):
            return

        channel = msg.get("channel", "")
        data = msg.get("data")

        if channel == "rs.login":
            self.logged_in = True
            log.info("ws private login ok")
            return
        if channel == "rs.error":
            log.warning("ws error response: %s", msg)
            return
        if channel in ("pong", "rs.pong"):
            return

        if channel == "push.kline" and isinstance(data, dict):
            try:
                symbol = data.get("symbol") or msg.get("symbol")
                interval = data.get("interval", "Min5")
                candle = Candle(
                    ts=int(data.get("t", 0)),
                    o=float(data.get("o", 0)),
                    h=float(data.get("h", 0)),
                    l=float(data.get("l", 0)),
                    c=float(data.get("c", 0)),
                    v=float(data.get("q", data.get("v", 0)) or 0),
                )
                now = time.time()
                bucket = self._bucket(interval)
                is_closed = now >= bucket + self._interval_seconds(interval)
                if self.on_kline:
                    self.on_kline(symbol, interval, candle, is_closed)
            except Exception as exc:  # noqa: BLE001
                log.debug("kline parse error: %s", exc)
        elif channel in ("push.ticker", "push.tickers") and data is not None:
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                symbol = item.get("symbol")
                if not symbol:
                    continue
                try:
                    last = float(item.get("lastPrice") or 0)
                    fair = float(item.get("fairPrice") or 0)
                    bid = float(item.get("bid1") or 0)
                    ask = float(item.get("ask1") or 0)
                    if self.on_tick:
                        self.on_tick(symbol, fair or last, bid, ask)
                except Exception:  # noqa: BLE001
                    continue
        elif channel == "push.personal.order" and isinstance(data, dict):
            if self.on_order:
                self.on_order(data)
        elif channel == "push.personal.position" and isinstance(data, dict):
            if self.on_position:
                self.on_position(data)
        elif channel == "push.personal.asset" and isinstance(data, dict):
            if self.on_asset:
                self.on_asset(data)

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        table = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800, "Min60": 3600, "Hour4": 14400}
        return table.get(interval, 300)

    def _bucket(self, interval: str) -> int:
        secs = self._interval_seconds(interval)
        return int(time.time() // secs) * secs

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "logged_in": self.logged_in,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "kline_streams": len(self._kline_subs),
            "tick_streams": len(self._tick_subs),
            "last_message_age_s": round(time.time() - self.last_message_ts, 2) if self.last_message_ts else None,
        }


# --------------------------------------------------------------------------- #
#  Normalized venue adapter (multi-venue layer)
# --------------------------------------------------------------------------- #
class MexcVenueClient(VenueClient):
    """Adapts the raw :class:`MeXCClient` to the normalized :class:`VenueClient`.

    Thin by design: MEXC's numbers already match the canonical model
    (``vol`` is contracts, ``volUnit``/``contractSize`` are reported in
    ``ContractSpec``), so this only translates side enums and envelopes.
    """

    def __init__(self, raw: MeXCClient, *, position_mode: int = HEDGE) -> None:
        self.raw = raw
        self.spec = VENUES["mexc"]
        self.clock = raw.clock
        self.rest_base = raw.rest_base
        self.position_mode = position_mode
        self._contracts: Dict[str, ContractSpec] = {}
        self._contracts_ts = 0.0

    # -- lifecycle / credentials ---------------------------------------- #
    @property
    def has_credentials(self) -> bool:
        return self.raw.has_credentials

    def update_credentials(self, api_key, api_secret, passphrase=None) -> None:
        self.raw.update_credentials(api_key, api_secret)

    async def start(self) -> None:
        await self.raw.start()
        try:
            self.position_mode = int(await self.raw.position_mode())
        except Exception as exc:  # noqa: BLE001
            log.debug("position mode unavailable (%s); keeping %s", exc, self.position_mode)

    async def close(self) -> None:
        await self.raw.close()

    async def ping(self) -> Optional[int]:
        return await self.raw.ping()

    async def sync_time(self) -> None:
        await self.raw.sync_time()

    async def position_mode(self) -> int:
        try:
            self.position_mode = int(await self.raw.position_mode())
        except Exception:  # noqa: BLE001
            pass
        return self.position_mode

    # -- market data ----------------------------------------------------- #
    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]:
        if self._contracts and not force and time.time() - self._contracts_ts < 600:
            return self._contracts
        rows: List[Dict[str, Any]] = []
        for fetch in (self.raw.contract_details, self.raw.contract_detail_country):
            try:
                rows = await fetch()
                if rows:
                    break
            except Exception as exc:  # noqa: BLE001
                log.debug("contract fetch failed via %s: %s", fetch.__name__, exc)
        out: Dict[str, ContractSpec] = {}
        for item in rows:
            try:
                symbol = item["symbol"]
                out[symbol] = ContractSpec(
                    symbol=symbol,
                    contract_size=float(item.get("contractSize", 1) or 1),
                    price_unit=float(item.get("priceUnit", 0.0001) or 0.0001),
                    price_scale=int(item.get("priceScale", 4) or 4),
                    vol_unit=float(item.get("volUnit", 1) or 1),
                    vol_scale=int(item.get("volScale", 0) or 0),
                    min_vol=float(item.get("minVol", 1) or 1),
                    max_vol=float(item.get("maxVol", 1e9) or 1e9),
                    max_leverage=int(item.get("maxLeverage", 100) or 100),
                    min_leverage=int(item.get("minLeverage", 1) or 1),
                    taker_fee=float(item.get("takerFeeRate", DEFAULT_TAKER_FEE) or DEFAULT_TAKER_FEE),
                    maker_fee=float(item.get("makerFeeRate", 0.0002) or 0.0002),
                    api_allowed=bool(item.get("apiAllowed", True)),
                    state=int(item.get("state", 0) or 0),
                    is_new=bool(item.get("isNew", False)),
                    base=item.get("baseCoin", ""),
                    quote=item.get("quoteCoin", "USDT"),
                    position_open_type=int(item.get("positionOpenType", 3) or 3),
                    trigger_protect=float(item.get("triggerProtect", 0) or 0),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("contract parse error: %s", exc)
        if out:
            self._contracts = out
            self._contracts_ts = time.time()
        return self._contracts

    async def tickers(self, symbol: Optional[str] = None) -> Dict[str, Ticker]:
        rows = await self.raw.tickers(symbol)
        out: Dict[str, Ticker] = {}
        for item in rows:
            try:
                sym = item["symbol"]
                out[sym] = Ticker(
                    symbol=sym,
                    last=float(item.get("lastPrice") or 0),
                    bid=float(item.get("bid1") or 0),
                    ask=float(item.get("ask1") or 0),
                    volume24=float(item.get("volume24") or 0),
                    amount24=float(item.get("amount24") or 0),
                    hold_vol=float(item.get("holdVol") or 0),
                    high24=float(item.get("high24Price") or 0),
                    low24=float(item.get("lower24Price") or 0),
                    rise_fall_rate=float(item.get("riseFallRate") or 0),
                    index_price=float(item.get("indexPrice") or 0),
                    fair_price=float(item.get("fairPrice") or 0),
                    funding_rate=float(item.get("fundingRate") or 0),
                    ts=time.time(),
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        return await self.raw.klines(symbol, self.spec.native_interval(interval), limit)

    async def mark_price(self, symbol: str) -> float:
        return await self.raw.mark_price(symbol)

    async def depth_usd(self, symbol: str, levels: int = 5, contract_size: float = 1.0) -> float:
        return await self.raw.depth_usd(symbol, levels, contract_size)

    # -- account ---------------------------------------------------------- #
    async def account(self) -> AccountSnapshot:
        for row in await self.raw.assets():
            if str(row.get("currency", "")).upper() == "USDT":
                return AccountSnapshot(
                    equity=float(row.get("equity") or 0.0),
                    available=float(row.get("availableBalance") or row.get("availableOpen") or 0.0),
                    unrealized=float(row.get("unrealized") or 0.0),
                    position_margin=float(row.get("positionMargin") or 0.0),
                    currency="USDT",
                    ts=time.time(),
                )
        return AccountSnapshot(ts=time.time())

    async def positions(self) -> List[Position]:
        out: List[Position] = []
        for row in await self.raw.open_positions():
            try:
                if int(row.get("state", 1)) != 1 or float(row.get("holdVol") or 0) <= 0:
                    continue
                ptype = int(row.get("positionType", 1))
                out.append(
                    Position(
                        symbol=row["symbol"],
                        side=LONG if ptype == 1 else SHORT,
                        hold_vol=float(row.get("holdVol") or 0),
                        open_avg_price=float(row.get("openAvgPrice") or row.get("holdAvgPrice") or 0),
                        leverage=int(row.get("leverage") or 1),
                        unrealized=float(row.get("unRealizedPnl") or 0.0),
                        im=float(row.get("im") or 0.0),
                        liquidate_price=float(row.get("liquidatePrice") or 0),
                        position_id=int(row.get("positionId") or 0),
                        state=int(row.get("state") or 1),
                    )
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    # -- trading ---------------------------------------------------------- #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        return await self.raw.change_leverage(
            symbol, leverage, position_type=position_type, open_type=OPEN_ISOLATED
        )

    @staticmethod
    def _open_side(side: str) -> int:
        return SIDE_OPEN_LONG if side == LONG else SIDE_OPEN_SHORT

    @staticmethod
    def _close_side(side: str) -> int:
        return SIDE_CLOSE_LONG if side == LONG else SIDE_CLOSE_SHORT

    async def market_order(
        self, symbol: str, *, side: str, qty: float, reduce_only: bool,
        leverage: int = 0, client_id: str = "",
    ) -> OrderResult:
        return await self.raw.create_order(
            symbol=symbol, vol=qty,
            side=self._close_side(side) if reduce_only else self._open_side(side),
            order_type=ORDER_MARKET,
            leverage=leverage or None,
            open_type=OPEN_ISOLATED,
            reduce_only=reduce_only,
            external_oid=client_id or None,
            position_mode=self.position_mode,
        )

    async def entry_order_with_protection(
        self, *, symbol: str, side: str, qty: float, leverage: int = 0,
        price_hint: float = 0.0, client_id: str = "", sl_price: Optional[float] = None,
    ) -> OrderResult:
        """MEXC can carry the stop-loss leg on the entry order itself.

        Always a market entry, and **never** a take-profit leg: the target is
        enforced locally so no resting limit order ever exists (2026-10 audit).
        """
        return await self.raw.create_order(
            symbol=symbol,
            vol=qty,
            side=self._open_side(side),
            order_type=ORDER_MARKET,
            price=None,
            leverage=leverage or None,
            open_type=OPEN_ISOLATED,
            reduce_only=False,
            stop_loss_price=sl_price,
            take_profit_price=None,
            loss_trend=2,
            profit_trend=2,
            price_protect=1,
            external_oid=client_id or None,
            position_mode=self.position_mode,
        )

    async def stop_order(
        self, symbol: str, *, side: str, qty: float, trigger_price: float,
        reduce_only: bool = True, client_id: str = "",
    ) -> OrderResult:
        return await self.raw.place_plan_order(
            symbol=symbol,
            vol=qty,
            side=self._close_side(side) if reduce_only else self._open_side(side),
            trigger_price=trigger_price,
            trigger_type=2 if side == LONG else 1,   # long stop fires below, short above
            order_type=ORDER_MARKET,
            reduce_only=reduce_only,
            open_type=OPEN_ISOLATED,
            position_mode=self.position_mode,
        )

    async def modify_stop(
        self, symbol: str, *, order_id: str, kind: str, new_price: float, qty: float,
        side: str = LONG, handle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if kind == "attached":
            return await self.raw.change_attached_stop(
                symbol=symbol, order_id=str(order_id), stop_loss_price=new_price,
            )
        return await self.raw.modify_plan_order(
            symbol=symbol, order_id=str(order_id), trigger_price=new_price,
            execute_price=None, order_type=ORDER_MARKET,
            trigger_type=2 if side == LONG else 1,
        )

    async def cancel_stop(self, symbol: str, *, order_id: str, kind: str) -> bool:
        if kind == "attached":
            return await self.raw.change_attached_stop(symbol=symbol, order_id=str(order_id), stop_loss_price=0.0)
        return await self.raw.cancel_plan_orders(symbol, [str(order_id)])

    async def cancel_order_ids(self, symbol: str, order_ids: List[str]) -> bool:
        await self.raw.cancel_orders(order_ids)
        return True

    async def attached_protection(self, symbol: str, entry_order_id: str) -> Optional[Dict[str, Any]]:
        try:
            rows = await self.raw.tpsl_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("tpsl lookup failed for %s: %s", symbol, exc)
            return None
        for row in rows:
            if str(row.get("orderId")) == str(entry_order_id) and int(row.get("state", 1)) == 1:
                return {
                    "kind": "attached",
                    "tpsl_id": row.get("id"),
                    "stop_price": float(row.get("stopLossPrice") or 0),
                    "tp_price": float(row.get("takeProfitPrice") or 0),
                    # an attached take-profit leg is edited through the entry
                    # order id, and its kind differs from a standalone TP order
                    "tp_order_id": str(row.get("orderId") or ""),
                    "tp_kind": "attached",
                }
        return None

    async def cancel_take_profit(self, symbol: str, *, order_id: str, kind: str = "plan") -> bool:
        """Drop a take-profit leg: attached TPSL groups are edited in place."""
        if kind == "attached":
            return await self.raw.change_attached_stop(
                symbol=symbol, order_id=str(order_id), take_profit_price=0.0,
            )
        return await self.cancel_order_ids(symbol, [str(order_id)])

    async def order_status(self, symbol: str, *, order_id: str = "",
                           client_id: str = "") -> Optional[Dict[str, Any]]:
        """MEXC dialect: ``state`` 1..5, ``dealVol``/``dealAvgPrice``."""
        row: Optional[Dict[str, Any]] = None
        try:
            if client_id:
                row = await self.raw.order_by_external_id(symbol, client_id)
            if row is None and order_id:
                row = await self.raw.order_by_id(str(order_id))
        except Exception as exc:  # noqa: BLE001
            log.debug("[mexc] order_status(%s) failed: %s", symbol, exc)
            return None
        if not isinstance(row, dict):
            return None
        state = int(row.get("state", 0) or 0)
        filled = float(row.get("dealVol") or 0.0)
        total = float(row.get("vol") or 0.0)
        # MEXC: 1 unplaced/trigger, 2 uncompleted, 3 completed, 4 canceled, 5 invalid
        raw_status = {1: "OPEN", 2: "PARTIAL", 3: "FILLED", 4: "CANCELED", 5: "REJECTED"}.get(state, "")
        return {
            "status": normalize_order_status(filled_qty=filled, total_qty=total, raw_status=raw_status),
            "filled_qty": filled,
            "avg_price": float(row.get("dealAvgPrice") or row.get("price") or 0.0),
            "raw": row,
        }

    async def open_protection(self, symbol: str, side: str = "") -> Optional[Dict[str, Any]]:
        """Adopt existing protective legs instead of placing duplicates."""
        out: Dict[str, Any] = {}
        known = True
        try:
            rows = await self.raw.plan_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("[mexc] open plan-order query failed for %s: %s", symbol, exc)
            known = False
            rows = []
        for row in rows:
            if int(row.get("state", 1) or 1) in (1, 2) and float(row.get("triggerPrice") or 0):
                out["kind"] = "plan"
                out["stop_order_id"] = str(row.get("id") or row.get("orderId") or "")
                out["stop_price"] = float(row.get("triggerPrice") or 0)
                break
        if "tp_order_id" not in out:
            try:
                resting = await self.raw.open_orders(symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("[mexc] open-order query failed for %s: %s", symbol, exc)
                known = False
                resting = []
            for row in resting:
                # only *reduce-only* orders are take-profit legs left behind by
                # the previous version; entry orders are never touched
                if row.get("reduceOnly") and int(row.get("state", 1) or 1) in (1, 2, 3):
                    out.setdefault("kind", "plan")
                    out["tp_order_id"] = str(row.get("orderId") or row.get("id") or "")
                    out["tp_kind"] = "regular"
                    out["tp_price"] = float(row.get("price") or 0)
                    break
        try:
            tpsl = await self.raw.tpsl_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("[mexc] open tpsl query failed for %s: %s", symbol, exc)
            known = False
            tpsl = []
        for row in tpsl:
            if int(row.get("state", 1) or 1) != 1:
                continue
            stop = float(row.get("stopLossPrice") or 0)
            tp = float(row.get("takeProfitPrice") or 0)
            if stop or tp:
                out.setdefault("kind", "attached")
                out["tpsl_id"] = row.get("id")
                out["entry_order_id"] = str(row.get("orderId") or "")
                if stop:
                    out["stop_price"] = stop
                if tp:
                    out["tp_price"] = tp
                    out["tp_order_id"] = str(row.get("orderId") or "")
                    out["tp_kind"] = "attached"
                break
        if not known and not out:
            return None
        return out

    def normalize_order_push(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """MEXC order push -> {"client_id", "status", "filled_qty", "avg_price"}."""
        if not isinstance(data, dict):
            return None
        client_id = str(data.get("externalOid") or "")
        if not client_id:
            return None
        state = int(data.get("state", 0) or 0)
        filled = float(data.get("dealVol") or 0.0)
        total = float(data.get("vol") or 0.0)
        raw_status = {1: "OPEN", 2: "PARTIAL", 3: "FILLED", 4: "CANCELED", 5: "REJECTED"}.get(state, "")
        return {
            "client_id": client_id,
            "status": normalize_order_status(filled_qty=filled, total_qty=total, raw_status=raw_status),
            "filled_qty": filled,
            "avg_price": float(data.get("dealAvgPrice") or 0.0),
            "order_id": str(data.get("orderId") or ""),
            "raw": data,
        }

    def diagnostics(self) -> Dict[str, Any]:
        data = dict(self.raw.diagnostics())
        data.update({"venue": "mexc", "venue_label": self.spec.label, "position_mode": self.position_mode})
        return data
