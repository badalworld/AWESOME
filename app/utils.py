"""Shared helpers: clock sync, numeric utilities, logging ring buffer."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, Optional


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
        # Assume symmetric latency: server time corresponds to the midpoint.
        self._offset_ms = server_ms - (time.time() * 1000.0 + rtt_ms / 2.0)
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

    @staticmethod
    def utc_now() -> float:
        return time.time()


# --------------------------------------------------------------------------- #
#  Numerics
# --------------------------------------------------------------------------- #
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(round(value / step, 9)) * step


def round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return round(round(value / step, 9)) * step


def decimals_from_step(step: float) -> int:
    if step <= 0:
        return 8
    text = f"{step:.12f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def fmt_price(value: float, step: float) -> str:
    return f"{round_to_step(value, step):.{decimals_from_step(step)}f}"


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0


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


def stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


# --------------------------------------------------------------------------- #
#  ROI <-> price conversion (leveraged ROI on margin, exactly as specified)
# --------------------------------------------------------------------------- #
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
