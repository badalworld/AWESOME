"""Binance USDⓈ-M futures client (REST + WebSocket) — normalized venue adapter.

Verified against the official docs
(https://developers.binance.com/docs/derivatives/usds-margined-futures):

* Base URL .............. https://fapi.binance.com
* Auth .................. header ``X-MBX-APIKEY``; every signed request carries
                          ``timestamp`` (+ optional ``recvWindow``) and
                          ``signature`` = HMAC-SHA256(secret, queryString) hex.
* Signature payload ..... GET/DELETE → the exact query string that is sent;
                          POST → the same string, sent form-urlencoded.
* WS .................... wss://fstream.binance.com/stream?streams=a/b/c
                          (<symbol>@kline_5m, @markPrice@1s, @bookTicker) and
                          wss://fstream.binance.com/ws/<listenKey> for the user
                          data stream (ORDER_TRADE_UPDATE / ACCOUNT_UPDATE).
* Hedge mode ............ ``positionSide`` = LONG/SHORT; one-way = BOTH and
                          ``reduceOnly`` (they are mutually exclusive).

Notes that matter for live money
--------------------------------
* ``STOP_MARKET`` + ``closePosition=true`` is used for the protective stop: it
  closes whatever size the position has when it triggers, so a partial fill can
  never leave a naked remainder.
* Stop modifications are done as *place-new-then-cancel-old* (Binance supports
  ``PUT /fapi/v1/order`` only for limit orders), never cancel-first.
* Quantity is rounded to ``LOT_SIZE.stepSize`` and price to ``PRICE_FILTER.tickSize``
  from ``exchangeInfo`` — Binance rejects anything else.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from ..utils import Clock
from .base import (
    LONG,
    SHORT,
    AccountSnapshot,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)
from .venue import (
    HEDGE,
    ONEWAY,
    VENUES,
    BaseHTTPVenueClient,
    VenueSpec,
    VenueStream,
    hmac_sha256_hex,
    normalize_order_status,
)

log = logging.getLogger("binance")

RETRYABLE_CODES = {-1001, -1003, -1006, -1007, -1008, -1016, -1015, -1021, -1000}


class BinanceClient(BaseHTTPVenueClient):
    """REST client + normalized venue surface for Binance USDⓈ-M futures."""

    spec = VENUES["binance"]

    def __init__(
        self,
        clock: Clock,
        *,
        rest_base: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        recv_window_ms: int = 5000,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("order_rate", 8.0)
        kwargs.setdefault("query_rate", 8.0)
        kwargs.setdefault("public_rate", 20.0)
        super().__init__(self.spec, clock, rest_base=rest_base, api_key=api_key,
                         api_secret=api_secret, **kwargs)
        self.recv_window = int(recv_window_ms)
        self._contracts: Dict[str, ContractSpec] = {}
        self._contracts_ts = 0.0
        self._oi_cache: Dict[str, float] = {}
        self._oi_ts = 0.0
        self._dual_side = False

    # ------------------------------------------------------------------ #
    #  signing / transport hooks
    # ------------------------------------------------------------------ #
    def _base_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        return headers

    def _sign_request(
        self, method: str, path: str, *, params, body, signed: bool,
    ) -> Tuple[Dict[str, str], Optional[Dict[str, Any]], Optional[str]]:
        headers = {"Content-Type": "application/x-www-form-urlencoded" if method.upper() == "POST" else "application/json"}
        if self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        merged: Dict[str, Any] = {}
        for src in (params or {}, body or {}):
            merged.update({k: v for k, v in src.items() if v is not None})
        if signed:
            if not self.has_credentials:
                from .venue import CredentialsRequired

                raise CredentialsRequired("Binance API credentials are not configured")
            merged["timestamp"] = self.clock.now_ms()
            merged["recvWindow"] = self.recv_window
            query = "&".join(f"{k}={_enc(v)}" for k, v in merged.items())
            signature = hmac_sha256_hex(self.api_secret or "", query)
            query = f"{query}&signature={signature}"
        else:
            query = "&".join(f"{k}={_enc(v)}" for k, v in merged.items())
        if method.upper() in ("GET", "DELETE"):
            return headers, query, None        # raw string: byte-exact signature
        return headers, None, query

    def _unwrap(self, payload: Any) -> Any:
        return payload

    def _is_envelope_error(self, payload: Any) -> Tuple[Optional[Any], str]:
        if isinstance(payload, dict) and "code" in payload and payload.get("msg") is not None:
            return payload.get("code"), str(payload.get("msg"))
        return None, ""

    def _retryable_codes(self) -> set:
        return RETRYABLE_CODES

    # ------------------------------------------------------------------ #
    #  public market data
    # ------------------------------------------------------------------ #
    async def ping(self) -> Optional[int]:
        started = time.perf_counter()
        payload = await self._request("GET", "/fapi/v1/time", lane="public", timeout=2.0)
        rtt = (time.perf_counter() - started) * 1000.0
        ts = payload.get("serverTime") if isinstance(payload, dict) else None
        if ts:
            self.clock.update(float(ts), rtt)
            return int(ts)
        return None

    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]:
        if self._contracts and not force and time.time() - self._contracts_ts < 3600:
            return self._contracts
        payload = await self._request("GET", "/fapi/v1/exchangeInfo", lane="public")
        out: Dict[str, ContractSpec] = {}
        for item in payload.get("symbols", []) if isinstance(payload, dict) else []:
            try:
                if str(item.get("contractType", "")).upper() != "PERPETUAL":
                    continue
                if str(item.get("status", "")).upper() != "TRADING":
                    continue
                quote = str(item.get("quoteAsset", "USDT"))
                if quote != "USDT":
                    continue
                filters = {f.get("filterType"): f for f in item.get("filters", [])}
                tick = float((filters.get("PRICE_FILTER") or {}).get("tickSize", 0.01) or 0.01)
                lot = filters.get("LOT_SIZE") or {}
                step = float(lot.get("stepSize", 0.001) or 0.001)
                min_qty = float(lot.get("minQty", step) or step)
                max_qty = float(lot.get("maxQty", 1e9) or 1e9)
                min_notional = float((filters.get("MIN_NOTIONAL") or {}).get("notional", 5.0) or 5.0)
                price_scale = max(0, int(round(-math.log10(tick)))) if tick > 0 else 2
                vol_scale = max(0, int(round(-math.log10(step)))) if step > 0 else 3
                out[item["symbol"]] = ContractSpec(
                    symbol=item["symbol"],
                    contract_size=1.0,              # USDT-M: quantity is in base units
                    price_unit=tick,
                    price_scale=price_scale,
                    vol_unit=step,
                    vol_scale=vol_scale,
                    min_vol=min_qty,
                    max_vol=max_qty,
                    max_leverage=int(item.get("maxLeverage") or 125),
                    min_leverage=1,
                    taker_fee=self.spec.taker_fee,
                    maker_fee=self.spec.maker_fee,
                    api_allowed=True,
                    state=0,
                    is_new=False,
                    base=str(item.get("baseAsset", "")),
                    quote=quote,
                    min_notional=min_notional,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("contract parse error: %s", exc)
        if out:
            self._contracts = out
            self._contracts_ts = time.time()
        return self._contracts

    async def tickers(self, symbol: Optional[str] = None) -> Dict[str, Ticker]:
        params = {"symbol": symbol} if symbol else None
        rows = await self._request("GET", "/fapi/v1/ticker/24hr", params=params, lane="public")
        if isinstance(rows, dict):
            rows = [rows]
        # mark/funding via premiumIndex (one call for everything)
        premium: Dict[str, Dict[str, Any]] = {}
        try:
            p_params = {"symbol": symbol} if symbol else None
            p_rows = await self._request("GET", "/fapi/v1/premiumIndex", params=p_params, lane="public")
            if isinstance(p_rows, dict):
                p_rows = [p_rows]
            premium = {str(r.get("symbol")): r for r in p_rows}
        except Exception as exc:  # noqa: BLE001
            log.debug("premiumIndex failed: %s", exc)

        out: Dict[str, Ticker] = {}
        for item in rows:
            try:
                sym = str(item["symbol"])
                prem = premium.get(sym, {})
                out[sym] = Ticker(
                    symbol=sym,
                    last=float(item.get("lastPrice") or 0),
                    bid=float(item.get("bidPrice") or 0),
                    ask=float(item.get("askPrice") or 0),
                    volume24=float(item.get("volume") or 0),
                    amount24=float(item.get("quoteVolume") or 0),
                    hold_vol=0.0,
                    high24=float(item.get("highPrice") or 0),
                    low24=float(item.get("lowPrice") or 0),
                    rise_fall_rate=float(item.get("priceChangePercent") or 0) / 100.0,
                    index_price=float(prem.get("indexPrice") or 0),
                    fair_price=float(prem.get("markPrice") or 0),
                    funding_rate=float(prem.get("lastFundingRate") or 0),
                    ts=time.time(),
                )
            except Exception:  # noqa: BLE001
                continue
        if out and symbol is None:
            await self._enrich_open_interest(out)
        return out

    async def _enrich_open_interest(self, tickers: Dict[str, Ticker]) -> None:
        """Attach open interest (contracts) for the liquid symbols only.

        ``/fapi/v1/openInterest`` is one call per symbol, so we only refresh the
        top-turnover names — which is exactly the set the universe scanner keeps.
        """
        if time.time() - self._oi_ts < 240 and self._oi_cache:
            for sym, vol in self._oi_cache.items():
                if sym in tickers:
                    tickers[sym].hold_vol = vol
            return
        ranked = sorted(tickers.items(), key=lambda kv: kv[1].amount24, reverse=True)[:35]
        fresh: Dict[str, float] = {}
        for sym, _tk in ranked:
            try:
                payload = await self._request(
                    "GET", "/fapi/v1/openInterest", params={"symbol": sym}, lane="public",
                )
                fresh[sym] = float(payload.get("openInterest") or 0.0)
            except Exception:  # noqa: BLE001
                continue
        if fresh:
            self._oi_cache = fresh
            self._oi_ts = time.time()
            for sym, vol in fresh.items():
                if sym in tickers:
                    tickers[sym].hold_vol = vol

    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]:
        rows = await self._request(
            "GET", "/fapi/v1/klines",
            params={"symbol": symbol, "interval": self.spec.native_interval(interval),
                    "limit": max(1, min(int(limit), 1500))},
            lane="public",
        )
        out: List[Candle] = []
        for r in rows if isinstance(rows, list) else []:
            try:
                out.append(Candle(
                    ts=int(int(r[0]) // 1000), o=float(r[1]), h=float(r[2]),
                    l=float(r[3]), c=float(r[4]), v=float(r[5]),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out

    async def mark_price(self, symbol: str) -> float:
        payload = await self._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol}, lane="public")
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        return float(payload.get("markPrice") or 0.0)

    async def depth_usd(self, symbol: str, levels: int = 5, contract_size: float = 1.0) -> float:
        payload = await self._request(
            "GET", "/fapi/v1/depth", params={"symbol": symbol, "limit": max(5, levels)}, lane="public",
        )
        total = 0.0
        for side in ("bids", "asks"):
            for level in (payload.get(side) or [])[:levels]:
                try:
                    total += float(level[0]) * float(level[1]) * contract_size
                except (TypeError, ValueError, IndexError):
                    continue
        return total / 2.0

    # ------------------------------------------------------------------ #
    #  account
    # ------------------------------------------------------------------ #
    async def account(self) -> AccountSnapshot:
        payload = await self._request("GET", "/fapi/v2/account", signed=True, lane="query")
        for row in payload.get("assets", []) if isinstance(payload, dict) else []:
            if str(row.get("asset", "")).upper() == "USDT":
                return AccountSnapshot(
                    equity=float(row.get("marginBalance") or row.get("walletBalance") or 0.0),
                    available=float(row.get("availableBalance") or 0.0),
                    unrealized=float(row.get("unrealizedProfit") or 0.0),
                    position_margin=float(payload.get("totalPositionInitialMargin") or 0.0),
                    currency="USDT",
                    ts=time.time(),
                )
        return AccountSnapshot(ts=time.time())

    async def positions(self) -> List[Position]:
        rows = await self._request("GET", "/fapi/v2/positionRisk", signed=True, lane="query")
        out: List[Position] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                amt = float(row.get("positionAmt") or 0.0)
                if amt == 0:
                    continue
                out.append(Position(
                    symbol=str(row.get("symbol")),
                    side=LONG if amt > 0 else SHORT,
                    hold_vol=abs(amt),
                    open_avg_price=float(row.get("entryPrice") or 0.0),
                    leverage=int(float(row.get("leverage") or 1)),
                    unrealized=float(row.get("unRealizedProfit") or 0.0),
                    im=abs(amt) * float(row.get("markPrice") or 0.0) / max(1, int(float(row.get("leverage") or 1))),
                    liquidate_price=float(row.get("liquidationPrice") or 0.0),
                    position_id=0,
                    state=1,
                    mark_price=float(row.get("markPrice") or 0.0),
                ))
            except Exception:  # noqa: BLE001
                continue
        return out

    async def position_mode(self) -> int:
        try:
            payload = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True, lane="query")
            self._dual_side = bool(payload.get("dualSidePosition", False))
        except Exception as exc:  # noqa: BLE001
            log.debug("dualSidePosition read failed: %s", exc)
        return HEDGE if self._dual_side else ONEWAY

    # ------------------------------------------------------------------ #
    #  trading
    # ------------------------------------------------------------------ #
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool:
        try:
            await self._request(
                "POST", "/fapi/v1/leverage",
                body={"symbol": symbol, "leverage": int(leverage)}, signed=True, lane="order",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[binance] set_leverage(%s, %sx) failed: %s", symbol, leverage, exc)
            return False

    def _position_side(self, side: str) -> str:
        if not self._dual_side:
            return "BOTH"
        return "LONG" if side == LONG else "SHORT"

    def _order_side(self, side: str, reduce_only: bool) -> str:
        """Canonical position side + reduce flag -> Binance BUY/SELL."""
        is_long = side == LONG
        if reduce_only:
            return "SELL" if is_long else "BUY"
        return "BUY" if is_long else "SELL"

    def _qty(self, symbol: str, qty: float) -> float:
        spec = self._contracts.get(symbol)
        step = spec.vol_unit if spec and spec.vol_unit else 0.0
        if step:
            return round(round(qty / step) * step, max(0, spec.vol_scale))
        return qty

    def _price(self, symbol: str, price: float) -> float:
        spec = self._contracts.get(symbol)
        tick = spec.price_unit if spec and spec.price_unit else 0.0
        if tick:
            return round(round(price / tick) * tick, max(0, spec.price_scale))
        return price

    async def _order(self, body: Dict[str, Any]) -> OrderResult:
        started = time.perf_counter()
        try:
            payload = await self._request("POST", "/fapi/v1/order", body=body, signed=True, lane="order")
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, error=str(exc),
                               latency_ms=(time.perf_counter() - started) * 1000.0, raw={"request": body})
        latency = (time.perf_counter() - started) * 1000.0
        filled = float(payload.get("cumQty") or payload.get("executedQty") or 0.0)
        avg = float(payload.get("avgPrice") or 0.0)
        return OrderResult(
            ok=True,
            order_id=str(payload.get("orderId")) if payload.get("orderId") else None,
            price=avg, filled_vol=filled,
            status=str(payload.get("status") or "submitted").lower(),
            latency_ms=latency,
            raw={"request": body, "response": payload},
        )

    async def market_order(
        self, symbol: str, *, side: str, qty: float, reduce_only: bool,
        leverage: int = 0, client_id: str = "",
    ) -> OrderResult:
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": self._order_side(side, reduce_only),
            "type": "MARKET",
            "quantity": self._qty(symbol, qty),
            "newOrderRespType": "RESULT",
        }
        if self._dual_side:
            body["positionSide"] = self._position_side(side)
        elif reduce_only:
            body["reduceOnly"] = "true"
        if client_id:
            body["newClientOrderId"] = client_id
        return await self._order(body)

    async def stop_order(
        self, symbol: str, *, side: str, qty: float, trigger_price: float,
        reduce_only: bool = True, client_id: str = "",
    ) -> OrderResult:
        """Protective stop: STOP_MARKET, mark-price trigger, closePosition.

        ``closePosition=true`` closes the whole position when it fires, so a
        partial fill can never leave an unprotected remainder. It is mutually
        exclusive with quantity/reduceOnly, as the API requires.
        """
        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": self._order_side(side, True),
            "type": "STOP_MARKET",
            "stopPrice": self._price(symbol, trigger_price),
            "workingType": "MARK_PRICE",
            "closePosition": "true",
        }
        if self._dual_side:
            body["positionSide"] = self._position_side(side)
        if client_id:
            body["newClientOrderId"] = client_id
        return await self._order(body)

    async def modify_stop(
        self, symbol: str, *, order_id: str, kind: str, new_price: float, qty: float,
        side: str = LONG, handle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Place the new stop first, then cancel the old one (never unprotected)."""
        res = await self.stop_order(symbol, side=side, qty=qty, trigger_price=new_price)
        if not res.ok:
            log.error("[binance] stop replace failed on %s: %s", symbol, res.error)
            return False
        if order_id:
            try:
                await self._request(
                    "DELETE", "/fapi/v1/order",
                    params={"symbol": symbol, "orderId": order_id}, signed=True, lane="order",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("[binance] could not cancel superseded stop %s on %s: %s", order_id, symbol, exc)
        if handle is not None:
            handle["stop_order_id"] = res.order_id
            handle["kind"] = "plan"
        return True

    async def cancel_stop(self, symbol: str, *, order_id: str, kind: str) -> bool:
        return await self.cancel_order_ids(symbol, [order_id])

    async def cancel_order_ids(self, symbol: str, order_ids: List[str]) -> bool:
        if not order_ids:
            return True
        try:
            await self._request(
                "DELETE", "/fapi/v1/order",
                params={"symbol": symbol, "orderId": order_ids[0]}, signed=True, lane="order",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[binance] cancel %s on %s failed: %s", order_ids, symbol, exc)
            return False

    def normalize_order_push(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """ORDER_TRADE_UPDATE -> canonical order update."""
        if not isinstance(data, dict):
            return None
        order = data.get("o") if isinstance(data.get("o"), dict) else data
        client_id = str(order.get("c") or order.get("clientOrderId") or "")
        if not client_id:
            return None
        filled = float(order.get("z") or order.get("cumQty") or 0.0)
        total = float(order.get("q") or order.get("origQty") or 0.0)
        return {
            "client_id": client_id,
            "status": normalize_order_status(filled_qty=filled, total_qty=total,
                                             raw_status=str(order.get("X") or order.get("status") or "")),
            "filled_qty": filled,
            "avg_price": float(order.get("ap") or order.get("avgPrice") or 0.0),
            "order_id": str(order.get("i") or order.get("orderId") or ""),
            "raw": data,
        }

    async def order_status(self, symbol: str, *, order_id: str = "",
                           client_id: str = "") -> Optional[Dict[str, Any]]:
        """Binance dialect: ``status`` + ``executedQty`` + ``avgPrice``."""
        params: Dict[str, Any] = {"symbol": symbol}
        if order_id:
            params["orderId"] = order_id
        elif client_id:
            params["origClientOrderId"] = client_id
        else:
            return None
        try:
            row = await self._request("GET", "/fapi/v1/order", params=params, signed=True, lane="query")
        except Exception as exc:  # noqa: BLE001
            log.debug("[binance] order_status(%s) failed: %s", symbol, exc)
            return None
        if not isinstance(row, dict):
            return None
        filled = float(row.get("executedQty") or 0.0)
        total = float(row.get("origQty") or 0.0)
        return {
            "status": normalize_order_status(filled_qty=filled, total_qty=total,
                                             raw_status=str(row.get("status") or "")),
            "filled_qty": filled,
            "avg_price": float(row.get("avgPrice") or 0.0),
            "raw": row,
        }

    async def open_protection(self, symbol: str, side: str = "") -> Optional[Dict[str, Any]]:
        """Adopt resting STOP_MARKET/TAKE_PROFIT legs instead of duplicating them."""
        try:
            rows = await self.open_orders(symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("[binance] open-order query failed for %s: %s", symbol, exc)
            return None
        out: Dict[str, Any] = {}
        for row in rows or []:
            otype = str(row.get("type") or "").upper()
            price = float(row.get("stopPrice") or 0.0)
            if otype.startswith("STOP") or otype == "TRAILING_STOP_MARKET":
                if price or row.get("closePosition"):
                    out.setdefault("kind", "plan")
                    out["stop_order_id"] = str(row.get("orderId") or "")
                    out["stop_price"] = price
            elif otype.startswith("TAKE_PROFIT"):
                out.setdefault("kind", "plan")
                out["tp_order_id"] = str(row.get("orderId") or "")
                out["tp_price"] = price
            elif otype == "LIMIT" and (row.get("reduceOnly") is True):
                tp = float(row.get("price") or 0.0)
                if tp:
                    out.setdefault("kind", "plan")
                    out["tp_order_id"] = str(row.get("orderId") or "")
                    out["tp_price"] = tp
        return out

    async def open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"symbol": symbol} if symbol else None
        rows = await self._request("GET", "/fapi/v1/openOrders", params=params, signed=True, lane="query")
        return list(rows) if isinstance(rows, list) else []

    # -- user data stream ------------------------------------------------ #
    async def create_listen_key(self) -> Optional[str]:
        try:
            payload = await self._request("POST", "/fapi/v1/listenKey", signed=True, lane="query")
            return payload.get("listenKey") if isinstance(payload, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("[binance] listenKey create failed: %s", exc)
            return None

    async def keepalive_listen_key(self, listen_key: str) -> bool:
        try:
            await self._request("PUT", "/fapi/v1/listenKey", body={"listenKey": listen_key},
                                signed=True, lane="query")
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("[binance] listenKey keepalive failed: %s", exc)
            return False

    def diagnostics(self) -> Dict[str, Any]:
        data = super().diagnostics()
        data.update({
            "venue": "binance",
            "venue_label": self.spec.label,
            "position_mode": "hedge" if self._dual_side else "one-way",
            "contracts_cached": len(self._contracts),
        })
        return data


def _enc(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- #
#  WebSocket
# --------------------------------------------------------------------------- #
class BinanceStream(VenueStream):
    """Combined-stream client with auto-reconnect and listenKey keepalive."""

    def __init__(
        self,
        client: BinanceClient,
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
        self._kline_subs: set = set()        # (symbol, interval)
        self._tick_subs: set = set()         # symbol
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._listen_key: Optional[str] = None

    # -- subscriptions --------------------------------------------------- #
    def _stream_names(self) -> List[str]:
        names = [f"{s.lower()}@{self._native_iv(iv)}" for s, iv in sorted(self._kline_subs)]
        for s in sorted(self._tick_subs):
            names.append(f"{s.lower()}@markPrice@1s")
            names.append(f"{s.lower()}@bookTicker")
        return names

    def _native_iv(self, interval: str) -> str:
        iv = self.client.spec.native_interval(interval)
        return "kline_" + iv if not iv.startswith("kline") else iv

    def _url(self) -> str:
        streams = self._stream_names()
        base = f"{self.ws_base}/stream?streams=" + "/".join(streams)
        if self._listen_key:
            base += "/" + self._listen_key
        return base

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
        self._task = asyncio.create_task(self._run(), name="binance-ws")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self.connected = False

    async def _keepalive_loop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(25 * 60)
            if self._listen_key:
                ok = await self.client.keepalive_listen_key(self._listen_key)
                if not ok:
                    self._listen_key = await self.client.create_listen_key()
                    self._wake.set()

    async def _run(self) -> None:
        backoff = 1.0
        keepalive: Optional[asyncio.Task] = None
        while not self._stop.is_set():
            try:
                import websockets  # local import: optional dependency

                if self.client.has_credentials and not self._listen_key:
                    self._listen_key = await self.client.create_listen_key()
                if self.client.has_credentials and keepalive is None:
                    keepalive = asyncio.create_task(self._keepalive_loop())
                self._wake.clear()
                url = self._url()
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=15, close_timeout=5,
                    max_queue=4096, open_timeout=10,
                ) as ws:
                    self.connected = True
                    self.last_message_ts = time.time()
                    backoff = 1.0
                    log.info("[binance] ws connected (%d streams)", len(self._stream_names()))
                    reader = asyncio.create_task(self._reader(ws))
                    switcher = asyncio.create_task(self._watch_subscriptions())
                    try:
                        await asyncio.wait({reader, switcher}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        reader.cancel()
                        switcher.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                log.warning("[binance] ws error: %s (reconnect in %.1fs)", exc, backoff)
            self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self.reconnect_max_s)
        if keepalive:
            keepalive.cancel()

    async def _watch_subscriptions(self) -> None:
        """Reconnect when the stream set changes (combined streams are URL-bound)."""
        await self._wake.wait()
        await asyncio.sleep(0.15)      # coalesce a burst of subscribe calls

    async def _reader(self, ws) -> None:
        async for raw in ws:
            self.last_message_ts = time.time()
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            data = msg.get("data") if isinstance(msg, dict) else None
            if not isinstance(data, dict):
                continue
            event = data.get("e")
            if event == "kline":
                self._on_kline_msg(data)
            elif event == "markPriceUpdate":
                sym = str(data.get("s") or "")
                mark = float(data.get("p") or 0)
                if sym and mark and self.on_tick:
                    self.on_tick(sym, mark, 0.0, 0.0)
            elif event == "bookTicker":
                sym = str(data.get("s") or "")
                if sym and self.on_tick:
                    self.on_tick(sym, 0.0, float(data.get("b") or 0), float(data.get("a") or 0))
            elif event in ("ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"):
                if self.on_order:
                    self.on_order(data)

    def _on_kline_msg(self, data: Dict[str, Any]) -> None:
        try:
            k = data.get("k") or {}
            symbol = str(k.get("s") or data.get("s") or "")
            interval = str(k.get("i") or "5m")
            candle = Candle(
                ts=int(int(k.get("t", 0)) // 1000),
                o=float(k.get("o") or 0), h=float(k.get("h") or 0),
                l=float(k.get("l") or 0), c=float(k.get("c") or 0),
                v=float(k.get("v") or 0),
            )
            if self.on_kline and symbol:
                self.on_kline(symbol, _canonical_interval(self.client.spec, interval),
                              candle, bool(k.get("x")))
        except Exception as exc:  # noqa: BLE001
            log.debug("[binance] kline parse error: %s", exc)

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "errors": self.errors,
            "kline_streams": len(self._kline_subs),
            "tick_streams": len(self._tick_subs),
            "listen_key": bool(self._listen_key),
            "source": self.ws_base,
            "last_message_age_s": round(time.time() - self.last_message_ts, 2) if self.last_message_ts else None,
        }


def _canonical_interval(spec: VenueSpec, native: str) -> str:
    for canonical, native_name in spec.intervals.items():
        if native_name == native:
            return canonical
    return native or "Min5"


__all__ = ["BinanceClient", "BinanceStream", "RETRYABLE_CODES"]
