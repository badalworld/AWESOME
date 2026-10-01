"""Feature extraction: everything the signal engine and the filter pipeline need,
computed once per closed candle per symbol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..exchange.base import Candle, Ticker
from ..utils import safe_div
from . import indicators as ind


@dataclass
class HTFContext:
    timeframe: str
    close: float = 0.0
    ema: Optional[float] = None
    ao: float = 0.0
    ao_prev: float = 0.0
    ao_rising: bool = False
    ao_falling: bool = False
    trend_up: bool = False
    trend_down: bool = False


@dataclass
class FeatureSet:
    symbol: str
    ts: int
    close: float
    open: float
    high: float
    low: float
    volume: float

    atr: float = 0.0
    atr_pct: float = 0.0
    atr_percentile: float = 0.0
    atr_series: List[float] = field(default_factory=list)

    ao: float = 0.0
    ao_prev: float = 0.0
    ao_prev2: float = 0.0
    ao_series: List[float] = field(default_factory=list)
    ao_signal: float = 0.0          # SMA9 of AO (classic signal line)

    ema200: Optional[float] = None
    ema50: Optional[float] = None
    ema200_slope_pct: float = 0.0

    rsi: float = 50.0
    macd_hist: float = 0.0
    macd_hist_prev: float = 0.0
    adx: float = 0.0
    bb_width: float = 0.0
    bb_width_prev: float = 0.0

    vol_sma: float = 0.0
    vol_ratio: float = 1.0
    candle_return_atr: float = 0.0   # |last candle move| / ATR

    turnover24: float = 0.0
    spread_bps: float = 0.0
    depth_usd: float = 0.0
    funding_rate: float = 0.0

    htf: Dict[str, HTFContext] = field(default_factory=dict)
    candles: List[Candle] = field(default_factory=list)

    # -- convenience ---------------------------------------------------- #
    def trend_state(self) -> str:
        if self.ema200 is None:
            return "unknown"
        if self.close > self.ema200 and (self.ema50 or 0) >= (self.ema200 or 0):
            return "up"
        if self.close < self.ema200 and (self.ema50 or 1e18) <= (self.ema200 or 0):
            return "down"
        return "mixed"


def build_features(
    symbol: str,
    candles: Sequence[Candle],
    ticker: Optional[Ticker] = None,
    *,
    ao_fast: int = 5,
    ao_slow: int = 34,
    atr_period: int = 14,
    ema_period: int = 200,
    vol_period: int = 20,
    rsi_period: int = 14,
    adx_period: int = 14,
    depth_usd: float = 0.0,
) -> Optional[FeatureSet]:
    """Compute the full feature set from closed candles (last candle = latest closed)."""
    n = len(candles)
    if n < max(ao_slow + 5, atr_period + 5, 30):
        return None

    highs = [c.h for c in candles]
    lows = [c.l for c in candles]
    closes = [c.c for c in candles]
    volumes = [c.v for c in candles]
    last = candles[-1]

    atr_series_raw = ind.atr(highs, lows, closes, atr_period)
    atr_vals = [v for v in atr_series_raw if v is not None]
    atr_now = atr_series_raw[-1] or 0.0
    if atr_now <= 0:
        return None

    ao_raw = ind.awesome_oscillator(highs, lows, ao_fast, ao_slow)
    ao_vals = [v for v in ao_raw if v is not None]
    if len(ao_vals) < 3:
        return None
    ao_signal_series = ind.sma(ao_vals, 9)
    ao_signal = ao_signal_series[-1] if ao_signal_series and ao_signal_series[-1] is not None else ao_vals[-1]

    ema_long = ind.ema(closes, ema_period)
    ema_fast = ind.ema(closes, 50)
    ema200_now = ema_long[-1]
    ema200_prev = ema_long[-6] if len(ema_long) >= 6 else None
    slope = 0.0
    if ema200_now and ema200_prev:
        slope = (ema200_now - ema200_prev) / ema200_prev * 100.0

    rsi_series = ind.rsi(closes, rsi_period)
    _, _, macd_hist = ind.macd(closes)
    adx_series = ind.adx(highs, lows, closes, adx_period)
    bb = ind.bollinger_width(closes, 20)

    vol_sma_series = ind.sma(volumes, vol_period)
    vol_sma = vol_sma_series[-1] or 0.0
    recent_atr = atr_vals[-200:] if len(atr_vals) >= 20 else atr_vals
    atr_pct = atr_now / last.c * 100.0

    features = FeatureSet(
        symbol=symbol,
        ts=last.ts,
        close=last.c,
        open=last.o,
        high=last.h,
        low=last.l,
        volume=last.v,
        atr=atr_now,
        atr_pct=atr_pct,
        atr_percentile=percentile_rank_value(recent_atr, atr_now),
        atr_series=atr_vals,
        ao=ao_vals[-1],
        ao_prev=ao_vals[-2],
        ao_prev2=ao_vals[-3] if len(ao_vals) >= 3 else 0.0,
        ao_series=ao_vals,
        ao_signal=float(ao_signal),
        ema200=ema200_now,
        ema50=ema_fast[-1],
        ema200_slope_pct=slope,
        rsi=rsi_series[-1] if rsi_series[-1] is not None else 50.0,
        macd_hist=macd_hist[-1] or 0.0,
        macd_hist_prev=macd_hist[-2] or 0.0,
        adx=adx_series[-1] or 0.0,
        bb_width=bb[-1] or 0.0,
        bb_width_prev=bb[-2] if len(bb) > 1 and bb[-2] is not None else 0.0,
        vol_sma=vol_sma,
        vol_ratio=safe_div(last.v, vol_sma, 1.0),
        candle_return_atr=safe_div(abs(last.c - last.o), atr_now, 0.0),
        turnover24=(ticker.amount24 if ticker else 0.0),
        spread_bps=(ticker.spread_bps if ticker else 0.0),
        depth_usd=depth_usd,
        funding_rate=(ticker.funding_rate if ticker else 0.0),
        candles=list(candles),
    )
    return features


def percentile_rank_value(values: Sequence[float], value: float) -> float:
    return ind.percentile_rank(values, value)


def build_htf_context(
    timeframe: str,
    candles: Sequence[Candle],
    *,
    ao_fast: int = 5,
    ao_slow: int = 34,
    ema_period: int = 50,
) -> Optional[HTFContext]:
    n = len(candles)
    if n < ao_slow + 3:
        return None
    highs = [c.h for c in candles]
    lows = [c.l for c in candles]
    closes = [c.c for c in candles]
    ao = [v for v in ind.awesome_oscillator(highs, lows, ao_fast, ao_slow) if v is not None]
    if len(ao) < 2:
        return None
    ema_series = ind.ema(closes, ema_period)
    ema_now = ema_series[-1]
    close = closes[-1]
    return HTFContext(
        timeframe=timeframe,
        close=close,
        ema=ema_now,
        ao=ao[-1],
        ao_prev=ao[-2],
        ao_rising=ao[-1] > ao[-2],
        ao_falling=ao[-1] < ao[-2],
        trend_up=bool(ema_now is not None and close > ema_now),
        trend_down=bool(ema_now is not None and close < ema_now),
    )
