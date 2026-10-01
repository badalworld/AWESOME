"""Shared helpers: clock sync, numeric utilities, logging ring buffer."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Mapping, Optional


# --------------------------------------------------------------------------- #
#  Clock
# --------------------------------------------------------------------------- #
class Clock:
    """Local clock with a measured offset against the exchange server.

    Every signed MEXC request must carry a millisecond timestamp within the
    accepted window, so we keep the offset fresh and use it for signing. This
    removes one class of avoidable order rejections on the latency hot path.
    """

    def __init__(self) -> None:
        self._offset_ms = 0.0
        self._synced_at = 0.0
        self._rtt_ms = 0.0

    def update(self, server_ms: float, rtt_ms: float) -> None:
        # Called at response receipt: the request midpoint is RTT/2 *before*
        # now, not after it. Assume symmetric network latency.
        self._offset_ms = server_ms - (time.time() * 1000.0 - rtt_ms / 2.0)
        self._rtt_ms = rtt_ms
        self._synced_at = time.time()

    @property
    def offset_ms(self) -> float:
        return self._offset_ms

    @property
    def rtt_ms(self) -> float:
        return self._rtt_ms

    @property
    def age_s(self) -> float:
        return time.time() - self._synced_at if self._synced_at else float("inf")

    def now_ms(self) -> int:
        return int(time.time() * 1000.0 + self._offset_ms)

# --------------------------------------------------------------------------- #
#  Numerics
# --------------------------------------------------------------------------- #
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b else default


def percentile(values: List[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = clamp(q / 100.0, 0.0, 1.0) * (len(ordered) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def price_from_roi(entry: float, roi_pct: float, leverage: float, is_long: bool) -> float:
    """ROI% = price_move% * leverage  =>  price_move% = ROI / leverage."""
    move = roi_pct / (100.0 * leverage)
    return entry * (1.0 + move) if is_long else entry * (1.0 - move)


def stop_price_from_roi(entry: float, sl_roi_pct: float, leverage: float, is_long: bool) -> float:
    """Price of a STOP at a given ROI magnitude.

    A stop is an *adverse* move: for a long it sits below entry, for a short above.
    (`price_from_roi` maps ROI to a favourable move — that is the TP side.)
    """
    move = abs(sl_roi_pct) / (100.0 * leverage)
    return entry * (1.0 - move) if is_long else entry * (1.0 + move)


def roi_from_price(entry: float, price: float, leverage: float, is_long: bool) -> float:
    if entry <= 0:
        return 0.0
    move = (price - entry) / entry if is_long else (entry - price) / entry
    return move * 100.0 * leverage


# --------------------------------------------------------------------------- #
#  Log ring buffer (dashboard "Logs" tab)
# --------------------------------------------------------------------------- #
class RingLogHandler(logging.Handler):
    """Keeps the last N log records in memory for the live dashboard feed."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.records: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - logging internals
        try:
            self._seq += 1
            self.records.append(
                {
                    "seq": self._seq,
                    "ts": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
            )
        except Exception:  # noqa: BLE001
            pass

    def tail(self, limit: int = 200, after_seq: int = 0) -> List[Dict[str, Any]]:
        out = [r for r in self.records if r["seq"] > after_seq]
        return out[-limit:]


class LatencyTracker:
    """Rolling latency stats (p50/p95/p99) for order-routing telemetry."""

    def __init__(self, capacity: int = 500) -> None:
        self.samples: Deque[float] = deque(maxlen=capacity)
        self._lock = asyncio.Lock()

    async def record(self, ms: float) -> None:
        async with self._lock:
            self.samples.append(ms)

    def snapshot(self) -> Dict[str, float]:
        if not self.samples:
            return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
        vals = list(self.samples)
        return {
            "count": len(vals),
            "p50": round(percentile(vals, 50), 2),
            "p95": round(percentile(vals, 95), 2),
            "p99": round(percentile(vals, 99), 2),
            "max": round(max(vals), 2),
            "avg": round(mean(vals), 2),
        }


class ExchangeRequestTracker:
    """Observe REST request volume and exchange-reported rate-limit pressure.

    Exchanges expose different quotas, and several expose no usable quota
    headers at all. This tracker reports the latest *actual* quota only when a
    response supplies both a limit and remaining count; it never invents a
    denominator from local token-bucket settings.
    """

    _LIMIT_PAIRS = (
        ("gw-ratelimit-limit", "gw-ratelimit-remaining"),  # KuCoin gateway
        ("x-ratelimit-limit", "x-ratelimit-remaining"),
        ("x-rate-limit-limit", "x-rate-limit-remaining"),
        ("ratelimit-limit", "ratelimit-remaining"),
    )
    _USED_HEADERS = (
        "x-mbx-used-weight-1m",
        "x-mbx-order-count-10s",
        "x-mbx-order-count-1d",
    )
    _RATE_LIMIT_CODES = {"418", "429", "429000", "-1003", "-1015"}

    @classmethod
    def is_rate_limit_code(cls, code: Any) -> bool:
        """Recognize common HTTP/envelope throttle codes across supported venues."""
        return str(code).strip() in cls._RATE_LIMIT_CODES

    def __init__(self, capacity: int = 4096) -> None:
        self.requests_total = 0
        self.responses_total = 0
        self.errors_total = 0
        self.retries_total = 0
        self.rate_limit_hits_total = 0
        self.last_status: Optional[int] = None
        self.last_response_at = 0.0
        self.last_latency_ms: Optional[float] = None
        self._recent_requests: Deque[float] = deque(maxlen=capacity)
        self._recent_rate_limits: Deque[float] = deque(maxlen=capacity)
        self._quota: Optional[Dict[str, Any]] = None

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        try:
            # Headers may contain whitespace or a comma-separated list.
            parsed = float(str(value).strip().split(",", 1)[0])
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def record_request(self) -> None:
        now = time.time()
        self.requests_total += 1
        self._recent_requests.append(now)

    def record_response(self, status: int, headers: Optional[Mapping[str, Any]] = None,
                        latency_ms: Optional[float] = None) -> None:
        now = time.time()
        status = int(status)
        self.responses_total += 1
        self.last_status = status
        self.last_response_at = now
        if latency_ms is not None and math.isfinite(float(latency_ms)):
            self.last_latency_ms = round(float(latency_ms), 2)
        if status >= 400:
            self.errors_total += 1
        if status in (418, 429):
            self.rate_limit_hits_total += 1
            self._recent_rate_limits.append(now)

        normalized = {str(k).lower(): v for k, v in (headers or {}).items()}
        for limit_key, remaining_key in self._LIMIT_PAIRS:
            limit = self._number(normalized.get(limit_key))
            remaining = self._number(normalized.get(remaining_key))
            if limit is None or remaining is None or limit <= 0:
                continue
            used = max(0.0, min(limit, limit - remaining))
            self._quota = {
                "source": f"{limit_key} / {remaining_key}",
                "limit": limit,
                "remaining": min(limit, remaining),
                "used": used,
                "utilization_pct": round(used / limit * 100.0, 2),
                "observed_at": now,
            }
            return

        # Binance exposes used request weight/order count, but not the applicable
        # per-IP limit in every response. Report the usage without a guessed %.
        for key in self._USED_HEADERS:
            used = self._number(normalized.get(key))
            if used is not None:
                self._quota = {
                    "source": key,
                    "used": used,
                    "limit": None,
                    "remaining": None,
                    "utilization_pct": None,
                    "observed_at": now,
                }
                return

    def record_error(self) -> None:
        self.errors_total += 1

    def record_network_error(self) -> None:
        self.errors_total += 1
        self.last_response_at = time.time()
        self.last_status = None

    def record_retry(self) -> None:
        self.retries_total += 1

    def record_rate_limit(self) -> None:
        """Count venue-level throttling codes returned inside a 2xx envelope."""
        now = time.time()
        self.rate_limit_hits_total += 1
        self.errors_total += 1
        self._recent_rate_limits.append(now)

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        cutoff = now - 60.0
        while self._recent_requests and self._recent_requests[0] < cutoff:
            self._recent_requests.popleft()
        while self._recent_rate_limits and self._recent_rate_limits[0] < cutoff:
            self._recent_rate_limits.popleft()
        quota = dict(self._quota) if self._quota else None
        if quota is not None:
            quota["age_s"] = round(max(0.0, now - float(quota.get("observed_at") or now)), 1)
        return {
            "requests_total": self.requests_total,
            "requests_last_minute": len(self._recent_requests),
            "responses_total": self.responses_total,
            "errors_total": self.errors_total,
            "retries_total": self.retries_total,
            "rate_limit_hits_total": self.rate_limit_hits_total,
            "rate_limit_hits_last_minute": len(self._recent_rate_limits),
            "last_status": self.last_status,
            "last_response_age_s": round(max(0.0, now - self.last_response_at), 1) if self.last_response_at else None,
            "last_latency_ms": self.last_latency_ms,
            "quota": quota,
        }
