"""Awesome-Oscillator divergence detection on closed candles.

Definitions
-----------
*Bullish (regular)* — price prints a **lower low** while AO prints a **higher
low** (momentum stops confirming the down-move). Trade trigger: a closed candle
breaks the local structure high that formed between the two pivots, *and* AO
turns up.

*Bearish (regular)* — mirror image.

*Hidden* (continuation) variants are supported but disabled by default because
they are far noisier on the 5m timeframe.

Only fully-formed fractal pivots are used (needs ``pivot_k`` bars to the right),
so nothing is detected retroactively — the signal is always actionable in real
time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..exchange.base import Candle
from ..utils import safe_div
from . import indicators as ind

LONG = "LONG"
SHORT = "SHORT"


@dataclass
class Divergence:
    symbol: str
    side: str
    kind: str                     # regular | hidden
    ts: int
    price: float
    p1_index: int
    p2_index: int
    p1_price: float
    p2_price: float
    p1_ao: float
    p2_ao: float
    price_delta_pct: float
    ao_delta: float               # normalised by ATR
    bars_since_pivot: int
    trigger_level: float          # price that must break to confirm entry
    trigger_confirmed: bool = False
    trigger_note: str = ""
    structure_high: float = 0.0
    structure_low: float = 0.0
    ao_slope_up: bool = False
    score: float = 0.0
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "kind": self.kind,
            "ts": self.ts,
            "price": self.price,
            "p1_price": self.p1_price,
            "p2_price": self.p2_price,
            "p1_ao": self.p1_ao,
            "p2_ao": self.p2_ao,
            "price_delta_pct": round(self.price_delta_pct, 4),
            "ao_delta_atr": round(self.ao_delta, 4),
            "bars_since_pivot": self.bars_since_pivot,
            "trigger_level": self.trigger_level,
            "trigger_confirmed": self.trigger_confirmed,
            "trigger_note": self.trigger_note,
            "score": round(self.score, 2),
            **self.meta,
        }


def detect_divergence(
    symbol: str,
    candles: Sequence[Candle],
    ao_series: Sequence[float],
    atr: float,
    *,
    pivot_k: int = 2,
    lookback_bars: int = 90,
    min_gap: int = 3,
    max_gap: int = 45,
    min_ao_delta_atr: float = 0.12,
    require_ao_extreme: bool = True,
    require_trigger_break: bool = True,
    allow_hidden: bool = False,
    max_bars_since_pivot: int = 8,
) -> Optional[Divergence]:
    """Return the freshest actionable divergence, or ``None``."""
    n = len(candles)
    if n < 40 or len(ao_series) < 20 or atr <= 0:
        return None

    # AO is only defined after the slow SMA warms up (ao_slow-1 leading bars),
    # so the series is shorter than the candle array; align by offset.
    offset = n - len(ao_series)
    if offset < 0:
        return None
    lows = [c.l for c in candles]
    highs = [c.h for c in candles]

    best: Optional[Divergence] = None

    # ---------------- bullish (lower low in price, higher low in AO) ------ #
    low_idx = [i for i in ind.pivots(lows, pivot_k, "low") if i >= max(pivot_k, n - lookback_bars)]
    if len(low_idx) >= 2:
        for a, b in zip(low_idx, low_idx[1:]):
            if not (min_gap <= b - a <= max_gap):
                continue
            if n - 1 - b > max_bars_since_pivot:
                continue
            p1_ao = _ao_at(ao_series, offset, a)
            p2_ao = _ao_at(ao_series, offset, b)
            if p1_ao is None or p2_ao is None:
                continue
            price_lower_low = lows[b] < lows[a]
            ao_higher_low = (p2_ao - p1_ao) > min_ao_delta_atr * atr
            if not (price_lower_low and ao_higher_low):
                # hidden bullish: higher low in price + lower AO low
                if allow_hidden and lows[b] > lows[a] and (p2_ao - p1_ao) < -min_ao_delta_atr * atr:
                    pass
                else:
                    continue
                kind = "hidden"
            else:
                kind = "regular"
            if require_ao_extreme and kind == "regular" and p2_ao > 0:
                continue  # classic bullish divergence lives below the zero line
            structure_high = max(highs[b:n - 1]) if n - 1 > b else highs[b]
            div = _build(
                symbol, LONG, kind, candles, a, b, lows[a], lows[b], p1_ao, p2_ao,
                atr, structure_high, structure_low=min(lows[a:b + 1]),
                require_trigger_break=require_trigger_break,
            )
            if div and (best is None or div.ts >= best.ts):
                best = div

    # ---------------- bearish (higher high, lower AO high) --------------- #
    high_idx = [i for i in ind.pivots(highs, pivot_k, "high") if i >= max(pivot_k, n - lookback_bars)]
    if len(high_idx) >= 2:
        for a, b in zip(high_idx, high_idx[1:]):
            if not (min_gap <= b - a <= max_gap):
                continue
            if n - 1 - b > max_bars_since_pivot:
                continue
            p1_ao = _ao_at(ao_series, offset, a)
            p2_ao = _ao_at(ao_series, offset, b)
            if p1_ao is None or p2_ao is None:
                continue
            price_higher_high = highs[b] > highs[a]
            ao_lower_high = (p1_ao - p2_ao) > min_ao_delta_atr * atr
            if not (price_higher_high and ao_lower_high):
                if allow_hidden and highs[b] < highs[a] and (p1_ao - p2_ao) < -min_ao_delta_atr * atr:
                    kind = "hidden"
                else:
                    continue
            else:
                kind = "regular"
            if require_ao_extreme and kind == "regular" and p2_ao < 0:
                continue
            structure_low = min(lows[b:n - 1]) if n - 1 > b else lows[b]
            div = _build(
                symbol, SHORT, kind, candles, a, b, highs[a], highs[b], p1_ao, p2_ao,
                atr, structure_high=max(highs[a:b + 1]), structure_low=structure_low,
                require_trigger_break=require_trigger_break,
            )
            if div and (best is None or div.ts >= best.ts):
                best = div

    return best


def _ao_at(ao_series: Sequence[float], offset: int, index: int) -> Optional[float]:
    j = index - offset
    if 0 <= j < len(ao_series):
        return ao_series[j]
    return None


def _build(
    symbol: str, side: str, kind: str, candles: Sequence[Candle],
    p1: int, p2: int, price1: float, price2: float, ao1: float, ao2: float,
    atr: float, structure_high: float, structure_low: float,
    require_trigger_break: bool,
) -> Optional[Divergence]:
    n = len(candles)
    last = candles[-1]
    ao_vals = None
    slope_up = False
    # AO slope from the last two closed candles
    if n >= 2:
        slope_up = candles[-1].c > candles[-2].c
    trigger_level = structure_high if side == LONG else structure_low

    confirmed = False
    note = ""
    if require_trigger_break:
        if side == LONG and last.c > structure_high:
            confirmed = True
            note = f"close {last.c:.6g} broke structure high {structure_high:.6g}"
        elif side == SHORT and last.c < structure_low:
            confirmed = True
            note = f"close {last.c:.6g} broke structure low {structure_low:.6g}"
    else:
        confirmed = True
        note = "trigger break not required"

    price_delta_pct = abs(price2 - price1) / price1 * 100.0 if price1 else 0.0
    ao_delta = abs(ao2 - ao1) / atr if atr else 0.0

    return Divergence(
        symbol=symbol,
        side=side,
        kind=kind,
        ts=last.ts,
        price=last.c,
        p1_index=p1,
        p2_index=p2,
        p1_price=price1,
        p2_price=price2,
        p1_ao=ao1,
        p2_ao=ao2,
        price_delta_pct=price_delta_pct,
        ao_delta=ao_delta,
        bars_since_pivot=n - 1 - p2,
        trigger_level=trigger_level,
        trigger_confirmed=confirmed,
        trigger_note=note,
        structure_high=structure_high,
        structure_low=structure_low,
        ao_slope_up=slope_up,
        meta={
            "p1_bar": int(p1),
            "p2_bar": int(p2),
            "price1": price1,
            "price2": price2,
            "ao1": ao1,
            "ao2": ao2,
        },
    )


def score_divergence(div: Divergence) -> float:
    """0-100 quality of the raw pattern (before filters)."""
    magnitude = min(1.0, div.ao_delta / 0.60)              # AO displacement vs ATR
    price_leg = min(1.0, div.price_delta_pct / 1.20)       # size of the price leg
    freshness = max(0.0, 1.0 - div.bars_since_pivot / 10.0)
    kind_bonus = 1.0 if div.kind == "regular" else 0.75
    return round(100.0 * (0.45 * magnitude + 0.20 * price_leg + 0.20 * freshness + 0.15 * kind_bonus), 2)


def divergence_distance_score(div: Divergence, atr: float, price: float) -> float:
    """How far price still is from the invalidation level (used by ranking)."""
    if atr <= 0:
        return 0.0
    if div.side == LONG:
        return safe_div(price - div.p2_price, atr, 0.0)
    return safe_div(div.p2_price - price, atr, 0.0)
