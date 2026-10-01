"""Technical indicators — pure Python, dependency-free, allocation-light.

Everything works on plain lists of floats and returns lists aligned to the
input (leading ``None`` where the indicator is not yet defined). For 200-400
candle windows this is far faster than any overhead numpy/ta-lib would add.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> List[float]:
    out = [0.0] * len(closes)
    for i in range(len(closes)):
        if i == 0:
            out[i] = highs[i] - lows[i]
        else:
            out[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    """Wilder's ATR."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n == 0 or period <= 0 or n < period:
        return out
    tr = true_range(highs, lows, closes)
    seed = sum(tr[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def awesome_oscillator(
    highs: Sequence[float], lows: Sequence[float], fast: int = 5, slow: int = 34
) -> List[Optional[float]]:
    """AO = SMA(fast, median price) - SMA(slow, median price), median = (H+L)/2."""
    median = [(h + l) / 2.0 for h, l in zip(highs, lows)]
    fast_ma = sma(median, fast)
    slow_ma = sma(median, slow)
    out: List[Optional[float]] = [None] * len(median)
    for i in range(len(median)):
        f, s = fast_ma[i], slow_ma[i]
        if f is not None and s is not None:
            out[i] = f - s
    return out


def rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 - 100.0 / (1.0 + (avg_gain / avg_loss if avg_loss else float("inf")))
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss else float("inf")
        out[i] = 100.0 - 100.0 / (1.0 + rs)
    return out


def macd(
    closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    n = len(closes)
    macd_line: List[Optional[float]] = [None] * n
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    valid = [v for v in macd_line if v is not None]
    start = len(macd_line) - len(valid)
    sig_vals = ema(valid, signal) if valid else []
    signal_line: List[Optional[float]] = [None] * n
    for i, v in enumerate(sig_vals):
        signal_line[start + i] = v
    hist: List[Optional[float]] = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, hist


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14
) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period * 2:
        return out
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = true_range(highs, lows, closes)
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    def _wilder(vals: List[float]) -> List[Optional[float]]:
        res: List[Optional[float]] = [None] * n
        if n < period:
            return res
        s = sum(vals[1:period + 1])
        res[period] = s
        for i in range(period + 1, n):
            s = s - s / period + vals[i]
            res[i] = s
        return res

    tr_s = _wilder(tr)
    plus_s = _wilder(plus_dm)
    minus_s = _wilder(minus_dm)
    dx: List[Optional[float]] = [None] * n
    for i in range(n):
        if tr_s[i] and tr_s[i] > 0 and plus_s[i] is not None and minus_s[i] is not None:
            plus_di = 100.0 * plus_s[i] / tr_s[i]
            minus_di = 100.0 * minus_s[i] / tr_s[i]
            denom = plus_di + minus_di
            dx[i] = 100.0 * abs(plus_di - minus_di) / denom if denom else 0.0
    first = next((i for i, v in enumerate(dx) if v is not None), None)
    if first is None or first + period >= n:
        return out
    window = dx[first:first + period]
    prev = sum(v for v in window if v is not None) / period
    out[first + period - 1] = prev
    for i in range(first + period, n):
        if dx[i] is None:
            continue
        prev = (prev * (period - 1) + dx[i]) / period
        out[i] = prev
    return out


def bollinger_width(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> List[Optional[float]]:
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    mid = sma(closes, period)
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        if m is None or m == 0:
            continue
        var = sum((x - m) ** 2 for x in window) / period
        sd = math.sqrt(var)
        out[i] = (4.0 * mult * sd) / m * 100.0
    return out


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Where ``value`` sits inside ``values`` (0-100)."""
    if not values:
        return 0.0
    below = sum(1 for v in values if v <= value)
    return 100.0 * below / len(values)


def pivots(values: Sequence[float], k: int = 2, kind: str = "low") -> List[int]:
    """Fractal pivots: index i is a pivot if it is the extreme of a 2k+1 window.

    Only fully-formed pivots are returned (needs ``k`` bars to the right), which
    is what makes divergence detection safe to act on in real time.
    """
    out: List[int] = []
    n = len(values)
    for i in range(k, n - k):
        window = values[i - k:i + k + 1]
        if kind == "low":
            if values[i] == min(window) and window.count(values[i]) == 1:
                out.append(i)
        else:
            if values[i] == max(window) and window.count(values[i]) == 1:
                out.append(i)
    return out
