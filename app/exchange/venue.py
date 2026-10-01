"""Multi-venue layer: one normalized contract, three exchanges.

Design
------
Everything above this module (strategy, risk, executor, dashboard) speaks a
*single* dialect:

* sides are the canonical ``LONG``/``SHORT`` of the position (never the raw
  exchange buy/sell enum),
* quantities are **contracts** (``ContractSpec.contract_size`` maps them to the
  exchange's native size unit),
* intervals are canonical (``Min5``, ``Min15`` …); each venue translates them,
* position mode is canonical: ``HEDGE = 1`` / ``ONEWAY = 2``,
* protection is a small handle dict with ``kind`` ∈ ``attached``/``plan``/``none``.

:class:`VenueClient` is the normalized REST surface a venue must implement;
:class:`VenueStream` is the normalized WebSocket surface. ``LiveBroker`` is
written against those two only — that is what makes "same strategy, same rules,
three exchanges" true by construction instead of by copy-paste.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from ..utils import Clock, LatencyTracker
from .base import (
    DEFAULT_TAKER_FEE,
    AccountSnapshot,
    Candle,
    ContractSpec,
    OrderResult,
    Position,
    Ticker,
)

log = logging.getLogger("venue")

# canonical position modes (exchange dialects are mapped in each client)
HEDGE = 1
ONEWAY = 2

# canonical interval -> seconds (kept here so venues share one source)
INTERVAL_SECONDS: Dict[str, int] = {
    "Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
    "Min60": 3600, "Hour4": 14400, "Hour8": 28800, "Day1": 86400,
}


@dataclass(frozen=True)
class VenueSpec:
    """Static description of one futures venue."""

    id: str
    label: str
    rest_base: str
    ws_url: str
    taker_fee: float = DEFAULT_TAKER_FEE
    maker_fee: float = 0.0002
    max_leverage: int = 100
    quote: str = "USDT"
    needs_passphrase: bool = False
    supports_attached_protection: bool = False
    hedge_mode_default: bool = False
    symbol_style: str = "mexc"          # how synthetic symbols are named
    intervals: Dict[str, str] = field(default_factory=dict)
    docs: str = ""

    def native_interval(self, canonical: str) -> str:
        return self.intervals.get(canonical, self.intervals.get("Min5", "Min5"))


VENUES: Dict[str, VenueSpec] = {
    "mexc": VenueSpec(
        id="mexc",
        label="MEXC Futures",
        rest_base="https://api.mexc.com",
        ws_url="wss://contract.mexc.com/edge",
        taker_fee=0.0002,               # MEXC USDT-M taker (0.02%)
        maker_fee=0.0000,
        max_leverage=200,
        needs_passphrase=False,
        supports_attached_protection=True,
        hedge_mode_default=True,
        symbol_style="mexc",
        intervals={
            "Min1": "Min1", "Min5": "Min5", "Min15": "Min15", "Min30": "Min30",
            "Min60": "Min60", "Hour4": "Hour4",
        },
        docs="https://www.mexc.com/api-docs/futures",
    ),
    "binance": VenueSpec(
        id="binance",
        label="Binance USDⓈ-M Futures",
        rest_base="https://fapi.binance.com",
        ws_url="wss://fstream.binance.com",
        taker_fee=0.0005,               # Binance USDⓈ-M taker (0.05%) w/o BNB discount
        maker_fee=0.0002,
        max_leverage=125,
        needs_passphrase=False,
        supports_attached_protection=False,
        hedge_mode_default=False,       # default is one-way (BOTH)
        symbol_style="binance",
        intervals={
            "Min1": "1m", "Min5": "5m", "Min15": "15m", "Min30": "30m",
            "Min60": "1h", "Hour4": "4h",
        },
        docs="https://developers.binance.com/docs/derivatives/usds-margined-futures",
    ),
    "kucoin": VenueSpec(
        id="kucoin",
        label="KuCoin Futures",
        rest_base="https://api-futures.kucoin.com",
        ws_url="wss://ws-api-futures.kucoin.com",
        taker_fee=0.0006,               # KuCoin futures taker (0.06%)
        maker_fee=0.0002,
        max_leverage=100,
        needs_passphrase=True,          # KuCoin API keys always ship a passphrase
        supports_attached_protection=False,
        hedge_mode_default=False,       # KuCoin futures is one-way only
        symbol_style="kucoin",
        intervals={
            "Min1": "1", "Min5": "5", "Min15": "15", "Min30": "30",
            "Min60": "60", "Hour4": "240",
        },
        docs="https://www.kucoin.com/docs/beginners/introduction",
    ),
}


def get_venue(venue_id: str) -> VenueSpec:
    try:
        return VENUES[str(venue_id).strip().lower()]
    except KeyError:
        raise KeyError(f"unknown venue {venue_id!r}; known: {', '.join(VENUES)}") from None


def venue_ids() -> List[str]:
    return list(VENUES)


def symbol_style_name(style: str, base: str) -> str:
    """Canonical *base* coin -> a venue-shaped ticker (used by the simulator)."""
    base = base.upper()
    if style == "binance":
        return f"{base}USDT"
    if style == "kucoin":
        return f"{base}USDTM"
    return f"{base}_USDT"


def style_base(style: str, symbol: str) -> str:
    """Inverse of :func:`symbol_style_name` (best effort, for display)."""
    s = symbol.upper()
    if style == "binance" and s.endswith("USDT"):
        return s[:-4]
    if style == "kucoin" and s.endswith("USDTM"):
        return s[:-5]
    if s.endswith("_USDT"):
        return s[:-5]
    return s.split("USDT")[0]


# Canonical order states.  Venues map their own dialect onto these.
ORDER_FILLED = "filled"
ORDER_PARTIAL = "partial"
ORDER_OPEN = "open"
ORDER_CANCELED = "canceled"
ORDER_REJECTED = "rejected"
ORDER_UNKNOWN = "unknown"


def normalize_order_status(*, filled_qty: float, total_qty: float = 0.0,
                           raw_status: str = "", is_active: Optional[bool] = None,
                           canceled: bool = False) -> str:
    """Collapse a venue-specific order state into the canonical vocabulary."""
    state = (raw_status or "").strip().upper()
    if state in ("FILLED", "FULLY_FILLED", "DEAL", "SETTLED", "COMPLETED", "DONE", "SUCCESS"):
        return ORDER_FILLED
    if state in ("CANCELED", "CANCELLED", "PARTIALLY_FILLED_CANCELED", "EXPIRED"):
        return ORDER_CANCELED
    if state in ("REJECTED", "FAILED", "ERROR"):
        return ORDER_REJECTED
    if state in ("OPEN", "NEW", "PENDING", "ACTIVE", "LIVE", "UNTRIGGERED", "SENT", "PARTIAL"):
        # MEXC reports state 1/2 as OPEN/PARTIAL, Binance as NEW/PARTIALLY_FILLED;
        # both are resting orders whose fill is tracked by filled_qty below.
        if total_qty and filled_qty:
            return ORDER_FILLED if filled_qty >= total_qty * 0.999 else ORDER_PARTIAL
        return ORDER_OPEN
    if total_qty and filled_qty:
        if filled_qty >= total_qty * 0.999:
            return ORDER_FILLED
        return ORDER_PARTIAL
    if canceled:
        return ORDER_CANCELED
    if is_active is False and filled_qty:
        return ORDER_FILLED
    if is_active is True:
        return ORDER_OPEN
    return ORDER_UNKNOWN


class CredentialsRequired(PermissionError):
    """Raised when a signed endpoint is called without usable credentials."""


class _Retryable(Exception):
    pass


class _Fatal(Exception):
    pass


# --------------------------------------------------------------------------- #
#  Normalized REST surface
# --------------------------------------------------------------------------- #
class VenueClient(ABC):
    """What every venue must expose to the trading stack (no venue enums leak)."""

    spec: VenueSpec

    @property
    def has_credentials(self) -> bool:
        raise NotImplementedError

    def update_credentials(self, api_key, api_secret, passphrase=None) -> None:  # pragma: no cover
        raise NotImplementedError

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def ping(self) -> Optional[int]: return None
    async def sync_time(self) -> None: ...

    @abstractmethod
    async def contracts(self, force: bool = False) -> Dict[str, ContractSpec]: ...

    @abstractmethod
    async def tickers(self, symbol: Optional[str] = None) -> Dict[str, Ticker]: ...

    @abstractmethod
    async def klines(self, symbol: str, interval: str = "Min5", limit: int = 300) -> List[Candle]: ...

    @abstractmethod
    async def mark_price(self, symbol: str) -> float: ...

    async def depth_usd(self, symbol: str, levels: int = 5, contract_size: float = 1.0) -> float:
        return 0.0

    @abstractmethod
    async def account(self) -> AccountSnapshot: ...

    @abstractmethod
    async def positions(self) -> List[Position]: ...

    async def position_mode(self) -> int:
        return ONEWAY

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int, position_type: int = 1) -> bool: ...

    @abstractmethod
    async def market_order(
        self, symbol: str, *, side: str, qty: float, reduce_only: bool,
        leverage: int = 0, client_id: str = "",
    ) -> OrderResult: ...

    async def entry_order_with_protection(
        self, *, symbol: str, side: str, qty: float, leverage: int = 0,
        price_hint: float = 0.0, client_id: str = "", sl_price: Optional[float] = None,
    ) -> OrderResult:
        """Market entry with the stop-loss attached in the same round trip.

        Only venues that can do this (MEXC) override it. The default is a plain
        market entry; the executor then arms standalone protection.
        """
        return await self.market_order(
            symbol, side=side, qty=qty, reduce_only=False, leverage=leverage, client_id=client_id,
        )

    @abstractmethod
    async def stop_order(
        self, symbol: str, *, side: str, qty: float, trigger_price: float,
        reduce_only: bool = True, client_id: str = "",
    ) -> OrderResult: ...

    @abstractmethod
    async def modify_stop(
        self, symbol: str, *, order_id: str, kind: str, new_price: float, qty: float,
        side: str = "LONG", handle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Move a protective stop. Venues that must cancel/replace (Binance,
        KuCoin) place the new stop *before* cancelling the old one and update
        ``handle["stop_order_id"]`` in place."""

    @abstractmethod
    async def cancel_stop(self, symbol: str, *, order_id: str, kind: str) -> bool: ...

    @abstractmethod
    async def order_status(self, symbol: str, *, order_id: str = "",
                           client_id: str = "") -> Optional[Dict[str, Any]]:
        """Look an order up and return a *normalized* fill report::

            {"status": "filled"|"partial"|"open"|"canceled"|"rejected"|"unknown",
             "filled_qty": float, "avg_price": float, "raw": {...}}

        Every venue speaks a different dialect here (MEXC ``state`` 1..5,
        Binance ``status``/``executedQty``/``avgPrice``, KuCoin ``isActive``/
        ``dealSize``/``avgDealPrice``) — the executor only ever sees this shape,
        which is what keeps the money path identical on all three exchanges.
        ``None`` means "could not be determined".
        """

    @abstractmethod
    async def open_protection(self, symbol: str, side: str = "") -> Optional[Dict[str, Any]]:
        """Existing resting protective orders for ``symbol``, or an empty dict.

        Returns ``{"kind", "stop_order_id", "stop_price", "tp_order_id",
        "tp_price"}`` when the venue reports them, ``{}`` when it answered and
        there are none, and ``None`` when the query failed (unknown state).
        Used to *adopt* protection after a restart instead of duplicating it.
        """

    async def cancel_order_ids(self, symbol: str, order_ids: List[str]) -> bool:
        return False

    async def cancel_take_profit(self, symbol: str, *, order_id: str, kind: str = "plan") -> bool:
        """Cancel a *legacy* resting take-profit order. Never places anything.

        Only ever used by the repair/adopt path: the live bot enforces the ROI
        target locally with a market close, so a take-profit order that already
        rests on the exchange belongs to an older version (or was placed by
        hand) and must not survive the "no pending orders" audit rule.
        """
        return await self.cancel_order_ids(symbol, [str(order_id)])

    async def attached_protection(self, symbol: str, entry_order_id: str) -> Optional[Dict[str, Any]]:
        """Look up exchange-side SL/TP attached to the entry order, if supported."""
        return None

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "venue": self.spec.id,
            "venue_label": self.spec.label,
            "base": getattr(self, "rest_base", self.spec.rest_base),
            "credentials": False,
        }


# --------------------------------------------------------------------------- #
#  REST client base
# --------------------------------------------------------------------------- #
class BaseHTTPVenueClient(VenueClient):
    """Shared transport: keep-alive pool, token buckets, retries, clock sync.

    Venues only implement signing (:meth:`_sign_request`), response shaping
    (:meth:`_unwrap`) and the endpoint mapping of the normalized methods.
    """

    spec: VenueSpec

    def __init__(
        self,
        spec: VenueSpec,
        clock: Clock,
        *,
        rest_base: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        passphrase: Optional[str] = None,
        timeout_s: float = 5.0,
        http2: bool = True,
        max_connections: int = 20,
        keepalive_expiry: float = 300.0,
        retry_attempts: int = 3,
        retry_backoff_ms: int = 120,
        order_rate: float = 9.0,
        query_rate: float = 9.0,
        public_rate: float = 25.0,
        telemetry: Optional[LatencyTracker] = None,
    ) -> None:
        self.spec = spec
        self.rest_base = (rest_base or spec.rest_base).rstrip("/")
        self.clock = clock
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.retry_attempts = max(1, retry_attempts)
        self.retry_backoff_ms = retry_backoff_ms
        self.telemetry = telemetry
        self._timeout = timeout_s
        self._max_connections = max_connections
        self._keepalive_expiry = keepalive_expiry
        self._http2 = http2
        self._client: Optional[httpx.AsyncClient] = None
        self._order_lane = RateLimiter(order_rate, burst=6)
        self._query_lane = RateLimiter(query_rate, burst=6)
        self._public_lane = RateLimiter(public_rate, burst=20)
        self.stats: Dict[str, Any] = {"requests": 0, "errors": 0, "retries": 0, "last_error": ""}

    # -- lifecycle ------------------------------------------------------ #
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
            headers={"User-Agent": "awesome-ao-bot/2.0", **self._base_headers()},
            follow_redirects=False,
        )
        try:
            await self.sync_time()
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] time sync failed at startup: %s", self.spec.id, exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def has_credentials(self) -> bool:
        if not (self.api_key and self.api_secret):
            return False
        if self.spec.needs_passphrase and not self.passphrase:
            return False
        return True

    def update_credentials(
        self, api_key: Optional[str], api_secret: Optional[str], passphrase: Optional[str] = None
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        if passphrase is not None or not self.spec.needs_passphrase:
            self.passphrase = passphrase

    def _base_headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    # -- signing hooks --------------------------------------------------- #
    @abstractmethod
    def _sign_request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]],
        body: Optional[Dict[str, Any]],
        signed: bool,
    ) -> Tuple[Dict[str, str], Any, Optional[str]]:
        """Return (headers, query params (dict or raw string), raw body)."""

    def _unwrap(self, payload: Any) -> Any:
        """Convert a successful response into the venue's data shape."""
        return payload

    def _is_envelope_error(self, payload: Any) -> Tuple[Optional[Any], str]:
        """Return (code, message) when the payload is a venue-level error."""
        return None, ""

    def _retryable_codes(self) -> set:
        return set()

    # -- transport ------------------------------------------------------- #
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
    ) -> Any:
        if self._client is None:
            raise RuntimeError(f"{type(self).__name__}.start() was not awaited")
        if signed and not self.has_credentials:
            raise CredentialsRequired(
                f"{self.spec.label} API credentials are not configured"
                + (" (key + secret + passphrase)" if self.spec.needs_passphrase else " (key + secret)")
            )
        limiter = {"order": self._order_lane, "query": self._query_lane, "public": self._public_lane}[lane]
        headers, query, content = self._sign_request(method, path, params=params, body=body, signed=signed)

        attempt = 0
        last_err: Optional[Exception] = None
        while attempt < self.retry_attempts:
            attempt += 1
            await limiter.acquire()
            started = time.perf_counter()
            try:
                self.stats["requests"] += 1
                resp = await self._client.request(
                    method.upper(), path, params=query, content=content,
                    headers=headers, timeout=timeout or self._timeout,
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                if self.telemetry is not None:
                    await self.telemetry.record(elapsed_ms)
                if resp.status_code == 429 or resp.status_code in (500, 502, 503, 504):
                    raise _Retryable(f"HTTP {resp.status_code}")
                try:
                    payload = resp.json()
                except ValueError:
                    raise _Fatal(f"non-JSON response (HTTP {resp.status_code}): {resp.text[:180]}")
                if resp.status_code >= 400:
                    code, msg = self._is_envelope_error(payload)
                    detail = f"{code} {msg}".strip() or str(payload)[:200]
                    raise _Fatal(f"HTTP {resp.status_code}: {detail}")
                code, msg = self._is_envelope_error(payload)
                if code is not None:
                    if str(code) in {str(c) for c in self._retryable_codes()}:
                        raise _Retryable(f"code={code} {msg}")
                    raise _Fatal(f"code={code} {msg}")
                return self._unwrap(payload)
            except (_Retryable, httpx.TransportError, httpx.TimeoutException) as exc:
                last_err = exc
                self.stats["retries"] += 1
                if attempt >= self.retry_attempts:
                    break
                backoff = self.retry_backoff_ms * attempt * (1.0 + 0.25 * (time.perf_counter() % 1.0))
                await asyncio.sleep(backoff / 1000.0)
            except _Fatal:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt >= self.retry_attempts:
                    break
                await asyncio.sleep(self.retry_backoff_ms * attempt / 1000.0)

        self.stats["errors"] += 1
        self.stats["last_error"] = str(last_err)
        raise RuntimeError(f"[{self.spec.id}] {method} {path} failed after {attempt} attempts: {last_err}")

class RateLimiter:
    """Async token bucket (one per lane so exits never queue behind scans)."""

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


# --------------------------------------------------------------------------- #
#  WebSocket base
# --------------------------------------------------------------------------- #
class VenueStream(ABC):
    """Normalized market-data/user-data stream used by ``LiveBroker``."""

    connected: bool = False
    reconnects: int = 0
    source: str = ""

    def __init__(self) -> None:
        self.on_kline: Optional[Callable[[str, str, Candle, bool], None]] = None
        self.on_tick: Optional[Callable[[str, float, float, float], None]] = None
        self.on_order: Optional[Callable[[Dict[str, Any]], None]] = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def subscribe_klines(self, symbols: List[str], interval: str = "Min5") -> None: ...

    @abstractmethod
    async def subscribe_ticks(self, symbols: List[str]) -> None: ...

    def diagnostics(self) -> Dict[str, Any]:
        return {"connected": self.connected, "reconnects": self.reconnects, "source": self.source}


def hmac_sha256_hex(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def hmac_sha256_b64(secret: str, message: str) -> str:
    import base64

    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()


def dumps_compact(body: Dict[str, Any]) -> str:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "normalize_order_status",
    "ORDER_FILLED",
    "HEDGE", "ONEWAY", "INTERVAL_SECONDS", "VENUES", "VenueSpec", "VenueClientAlias",
    "get_venue", "venue_ids", "symbol_style_name", "style_base",
    "CredentialsRequired", "RateLimiter", "BaseHTTPVenueClient", "VenueStream",
    "hmac_sha256_hex", "hmac_sha256_b64", "dumps_compact",
]

VenueClientAlias = BaseHTTPVenueClient   # backwards-friendly alias for typing imports
