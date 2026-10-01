"""Synthetic market simulator.

Purpose: keep the whole system (strategy, risk, trailing logic, dashboard,
paper trading) fully exercisable on machines that cannot reach MEXC — e.g. CI,
a laptop on a plane, or the hosted demo preview. The price process is a
volatility-clustered geometric random walk (EWMA/GARCH-lite) that produces
realistic OHLCV, wicks and awesome-oscillator divergences.

It is clearly labelled SIMULATED everywhere it surfaces in the UI.
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from typing import Dict, List, Optional, Tuple

from .base import DEFAULT_TAKER_FEE, Candle, ContractSpec, Ticker
from .venue import symbol_style_name

INTERVAL_SECONDS = {
    "Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800,
    "Min60": 3600, "Hour4": 14400, "Hour8": 28800,
    "Day1": 86400, "Week1": 604800, "Month1": 2592000,
}


class SyntheticSymbol:
    """One simulated perpetual contract."""

    TREND_BAR_SECONDS = 300.0      # drift is calibrated against a 5m bar

    def __init__(
        self,
        symbol: str,
        start_price: float,
        annual_vol: float,
        tick_seconds: float = 0.5,
        seed: int = 0,
        contract_size: float = 1.0,
        price_step: float = 0.0001,
        max_leverage: int = 125,
    ) -> None:
        self.symbol = symbol
        self.price = start_price
        self.start_price = start_price
        self.annual_vol = annual_vol
        self.tick_seconds = tick_seconds
        self.rnd = random.Random(seed)
        self.contract_size = contract_size
        self.price_step = price_step
        self.max_leverage = max_leverage
        self.taker_fee = DEFAULT_TAKER_FEE

        # GARCH-lite state (EWMA of squared returns -> volatility clustering)
        self._base_sigma = annual_vol / math.sqrt(365 * 24 * 3600 / tick_seconds)
        self._sigma = self._base_sigma
        self._trend = 0.0            # slow-moving drift, regime changes
        self._regime_left = 0
        self._spread_bps = max(0.8, self.rnd.uniform(1.0, 6.0))

        self.bid, self.ask = self._touch()

        self.candles: Dict[str, List[Candle]] = {}
        self._partial: Dict[str, Candle] = {}

    # ------------------------------------------------------------------ #
    def _step_price(self, dt_seconds: float) -> float:
        """One GBM step with EWMA vol clustering + regime drift."""
        if self._regime_left <= 0:
            # occasional regime shifts: trend + vol multiplier change
            self._regime_left = self.rnd.randint(120, 900)
            # drift is expressed per SECOND and sized against a 5-minute bar so
            # it is independent of the tick length: over one bar the drift is
            # ~0.25x the bar's diffusion, never multiples of it.
            self._trend = self.rnd.gauss(0.0, 0.25) * self._base_sigma / math.sqrt(SyntheticSymbol.TREND_BAR_SECONDS)
            vol_mult = self.rnd.choice([0.6, 0.85, 1.0, 1.25, 1.6, 2.0])
            self._sigma = self._base_sigma * vol_mult
        self._regime_left -= 1

        shock = self.rnd.gauss(0.0, 1.0)
        sigma = self._sigma * math.sqrt(dt_seconds)
        # jump component (~2% of ticks) -> fat tails, realistic wicks
        jump = 0.0
        if self.rnd.random() < 0.015:
            jump = self.rnd.gauss(0, 1) * sigma * self.rnd.uniform(1.8, 3.5)
        ret = self._trend * dt_seconds + sigma * shock + jump

        # volatility clustering feedback (EWMA of squared returns)
        self._sigma = math.sqrt(0.94 * self._sigma ** 2 + 0.06 * (abs(ret) / max(math.sqrt(dt_seconds), 1e-9)) ** 2)
        self._sigma = min(max(self._sigma, self._base_sigma * 0.4), self._base_sigma * 4.5)

        self.price = max(self.price * (1.0 + ret), self.start_price * 0.05)
        self.bid, self.ask = self._touch()
        return self.price

    def _bucket(self, interval: str, now: float) -> int:
        secs = INTERVAL_SECONDS.get(interval, 300)
        return int(now // secs) * secs

    def tick(self, now: float, intervals: Tuple[str, ...] = ("Min5",)) -> None:
        self._step_price(min(self.tick_seconds, 5.0))
        for interval in intervals:
            bucket = self._bucket(interval, now)
            partial = self._partial.get(interval)
            if partial is None or partial.ts != bucket:
                if partial is not None:
                    self._store(interval, partial)
                partial = Candle(ts=bucket, o=self.price, h=self.price, l=self.price, c=self.price, v=0.0)
                self._partial[interval] = partial
            partial.h = max(partial.h, self.price)
            partial.l = min(partial.l, self.price)
            partial.c = self.price
            partial.v += abs(self.rnd.gauss(1.0, 0.4)) * 1000.0

    def _store(self, interval: str, candle: Candle) -> None:
        series = self.candles.setdefault(interval, [])
        if series and series[-1].ts == candle.ts:
            series[-1] = candle
        else:
            series.append(candle)
        if len(series) > 1500:
            del series[: len(series) - 1500]

    def seed_history(self, interval: str, bars: int, ticks_per_bar: Optional[int] = None) -> None:
        """Pre-generate history so indicators are warm on boot."""
        secs = INTERVAL_SECONDS.get(interval, 300)
        now = time.time()
        start = int(now // secs) * secs - bars * secs
        # A handful of sub-steps per bar is enough for a realistic OHLC shape;
        # simulating every tick would make seeding quadratic (and slow boots).
        ticks_per_bar = ticks_per_bar or min(48, max(6, int(secs / max(self.tick_seconds, 1e-6))))
        for i in range(bars):
            ts = start + i * secs
            o = self.price
            h = l = c = self.price
            vol = 0.0
            for _ in range(ticks_per_bar):
                c = self._step_price(secs / ticks_per_bar)
                h = max(h, c)
                l = min(l, c)
                vol += abs(self.rnd.gauss(1.0, 0.4)) * 800.0
            series = self.candles.setdefault(interval, [])
            series.append(Candle(ts=ts, o=o, h=h, l=l, c=c, v=vol))
        # leave the simulator exactly at the last close
        self.price = self.candles[interval][-1].c if self.candles.get(interval) else self.price

    def aggregate(self, source: str, target: str) -> None:
        """Build a higher timeframe series from a base series (no re-simulation)."""
        base = self.candles.get(source) or []
        if not base:
            return
        buckets: Dict[int, Candle] = {}
        secs = INTERVAL_SECONDS.get(target, 900)
        for c in base:
            key = int(c.ts // secs) * secs
            cur = buckets.get(key)
            if cur is None:
                buckets[key] = Candle(ts=key, o=c.o, h=c.h, l=c.l, c=c.c, v=c.v)
            else:
                cur.h = max(cur.h, c.h)
                cur.l = min(cur.l, c.l)
                cur.c = c.c
                cur.v += c.v
        self.candles[target] = [buckets[k] for k in sorted(buckets)]

    def _touch(self) -> Tuple[float, float]:
        """Bid/ask derived from the current price (single source of truth)."""
        half = self.price * self._spread_bps / 2e4
        return self.price - half, self.price + half

    def ticker(self) -> Ticker:
        series = self.candles.get("Min5") or []
        bid, ask = self._touch()
        high24 = max((c.h for c in series[-288:]), default=self.price)
        low24 = min((c.l for c in series[-288:]), default=self.price)
        vol24 = sum(c.v for c in series[-288:])
        return Ticker(
            symbol=self.symbol,
            last=self.price,
            bid=bid,
            ask=ask,
            volume24=vol24,
            amount24=vol24 * self.price * self.contract_size,
            hold_vol=vol24 * 0.25,
            high24=high24,
            low24=low24,
            rise_fall_rate=(self.price / series[-288].o - 1.0) if len(series) >= 288 else 0.0,
            index_price=self.price * 0.9998,
            fair_price=self.price,
            funding_rate=0.0001,
            ts=time.time(),
        )


class SyntheticFeed:
    """Background simulator for a basket of symbols."""

    DEFS: List[Tuple[str, float, float]] = [
        # symbol, start price, annualised vol
        ("BTC_USDT", 68_500.0, 0.65),
        ("ETH_USDT", 3_250.0, 0.78),
        ("SOL_USDT", 158.0, 1.05),
        ("DOGE_USDT", 0.1420, 1.25),
        ("PEPE_USDT", 0.0000098, 1.85),
        ("WIF_USDT", 2.35, 2.10),
        ("SUI_USDT", 1.42, 1.40),
        ("APT_USDT", 8.90, 1.25),
        ("AVAX_USDT", 26.4, 1.15),
        ("LINK_USDT", 14.2, 1.05),
        ("ARB_USDT", 0.78, 1.35),
        ("OP_USDT", 1.65, 1.30),
        ("TIA_USDT", 5.10, 1.60),
        ("SEI_USDT", 0.41, 1.55),
        ("INJ_USDT", 21.5, 1.50),
        ("NEAR_USDT", 4.35, 1.30),
        ("FIL_USDT", 4.10, 1.25),
        ("LTC_USDT", 84.0, 0.85),
        ("XRP_USDT", 2.15, 1.10),
        ("BNB_USDT", 610.0, 0.55),
        ("AAVE_USDT", 168.0, 1.20),
        ("WLD_USDT", 1.95, 1.75),
        ("JUP_USDT", 0.85, 1.60),
        ("CRV_USDT", 0.52, 1.45),
    ]

    def __init__(self, tick_seconds: float = 0.5, seed: int = 20261001,
                 history_bars: int = 320, htf_history_bars: int = 200,
                 symbol_style: str = "mexc") -> None:
        self.tick_seconds = tick_seconds
        self.seed = seed
        self.history_bars = history_bars
        self.htf_history_bars = htf_history_bars
        self.symbol_style = symbol_style
        self.symbols: Dict[str, SyntheticSymbol] = {}
        self.contracts: Dict[str, ContractSpec] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._intervals: set = {"Min5", "Min15"}
        for i, (base, price, vol) in enumerate(self.DEFS):
            base = base.split("_")[0]
            sym = symbol_style_name(symbol_style, base)
            step = 10 ** -max(1, min(8, int(-math.floor(math.log10(price))) + 3)) if price < 1 else 0.1
            s = SyntheticSymbol(
                symbol=sym, start_price=price, annual_vol=vol,
                tick_seconds=tick_seconds, seed=seed + i * 7919,
                price_step=step, contract_size=1.0,
                max_leverage=random.Random(seed + i).choice([50, 100, 125, 200]),
            )
            s.seed_history("Min5", history_bars)
            # the higher timeframe only feeds EMA/AO context, so a coarse
            # sub-step count keeps the boot fast (12 points per bar is plenty)
            s.seed_history("Min15", htf_history_bars, ticks_per_bar=12)
            # 1h context is aggregated, not re-simulated (cheap, coherent)
            s.aggregate("Min15", "Min60")
            self.symbols[sym] = s
            self.contracts[sym] = ContractSpec(
                symbol=sym,
                contract_size=1.0,
                price_unit=step if step else 0.0001,
                price_scale=max(0, int(-math.floor(math.log10(step)))) if step else 4,
                vol_unit=1.0,
                vol_scale=0,
                min_vol=1.0,
                max_vol=5_000_000.0,
                max_leverage=s.max_leverage,
                min_leverage=1,
                taker_fee=DEFAULT_TAKER_FEE,
                maker_fee=0.0002,
                api_allowed=True,
                state=0,
                is_new=False,
                base=base,
                quote="USDT",
            )

    # -- lifecycle ------------------------------------------------------ #
    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            intervals = tuple(sorted(self._intervals))
            for sym, sim in self.symbols.items():
                sim.tick(now, intervals)
            await asyncio.sleep(self.tick_seconds)


    def add_interval(self, interval: str) -> None:
        if interval not in self.symbols[list(self.symbols)[0]].candles:
            for sim in self.symbols.values():
                sim.seed_history(interval, 150)
        self._intervals.add(interval)


    def klines(self, symbol: str, interval: str, limit: int = 300) -> List[Candle]:
        sim = self.symbols.get(symbol)
        if sim is None:
            return []
        self.add_interval(interval)
        series = sim.candles.get(interval, [])
        out = list(series[-limit:])
        partial = sim._partial.get(interval)  # noqa: SLF001 - same package
        if partial is not None:
            out = out + [partial] if not out or partial.ts != out[-1].ts else out[:-1] + [partial]
        return out

    def ticker(self, symbol: str) -> Optional[Ticker]:
        sim = self.symbols.get(symbol)
        return sim.ticker() if sim else None

    def tickers(self) -> Dict[str, Ticker]:
        return {s: sim.ticker() for s, sim in self.symbols.items()}

    def mark_price(self, symbol: str) -> float:
        sim = self.symbols.get(symbol)
        return sim.price if sim else 0.0
