"""Dynamic universe scanner — "the most volatile, tradable contracts".

Two-stage scan (cheap -> expensive) so a 300+ contract exchange costs a handful
of REST calls:

1. **Ticker stage** — from ``/contract/ticker`` (one call for the whole
   exchange) keep contracts that pass the hard gates (USDT-margined, live,
   API-tradable, leverage >= 10x, turnover floor, non-stable pair) and rank by
   ``turnover x 24h range`` — a very good cheap volatility proxy.
2. **Candle stage** — fetch 5m candles for the top ~40 and compute the real
   5m ATR%, volume profile and chop statistics.

Final score = weighted rank blend of turnover, realised 5m volatility and
momentum, all configurable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..exchange.base import ContractSpec
from ..utils import safe_div
from . import indicators as ind

log = logging.getLogger("universe")

STABLE_BASES = {
    "USDC", "USDE", "USDT", "DAI", "TUSD", "FDUSD", "USDD", "USD1", "PYUSD",
    "USTC", "BUSD", "USDP", "GUSD", "sUSDe",
}


@dataclass
class UniverseEntry:
    symbol: str
    price: float
    turnover24: float
    range24_pct: float
    atr_pct: float = 0.0
    vol_ratio: float = 1.0
    adx: float = 0.0
    trend_pct: float = 0.0
    oi_usd: float = 0.0
    spread_bps: float = 0.0
    score: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    max_leverage: int = 0
    contract: Optional[ContractSpec] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "turnover24": round(self.turnover24, 2),
            "range24_pct": round(self.range24_pct, 3),
            "atr_pct_5m": round(self.atr_pct, 4),
            "vol_ratio": round(self.vol_ratio, 3),
            "adx": round(self.adx, 2),
            "trend_pct": round(self.trend_pct, 3),
            "oi_usd": round(self.oi_usd, 2),
            "spread_bps": round(self.spread_bps, 2),
            "score": round(self.score, 2),
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "max_leverage": self.max_leverage,
            "selected": True,
        }


class UniverseScanner:
    def __init__(self, broker, cfg) -> None:
        self.broker = broker
        self.cfg = cfg
        self.last_scan_ts = 0.0
        self.last_scan_started_at = 0.0
        self.last_scan_duration_ms = 0.0
        self.last_scan_error = ""
        self.scan_count = 0
        self.last_candidate_count = 0
        self.last_eligible_count = 0
        self.last_tickers = {}
        self.last_entries: List[UniverseEntry] = []
        self.rejected_sample: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    def _universe_cfg(self) -> Dict[str, Any]:
        return self.cfg.section("universe")

    async def scan(self) -> List[UniverseEntry]:
        self.last_scan_started_at = time.time()
        started = time.perf_counter()
        try:
            entries = await self._scan_once()
            self.last_scan_error = ""
            return entries
        except Exception as exc:
            self.last_scan_error = str(exc)
            raise
        finally:
            self.scan_count += 1
            self.last_scan_duration_ms = round((time.perf_counter() - started) * 1000.0, 1)

    async def _scan_once(self) -> List[UniverseEntry]:
        ucfg = self._universe_cfg()
        if not ucfg.get("enabled", True):
            return self.last_entries

        contracts = await self.broker.contracts()
        tickers = await self.broker.tickers()
        self.last_tickers = tickers
        if not contracts or not tickers:
            self.last_candidate_count = 0
            self.last_eligible_count = 0
            return self.last_entries

        whitelist = set(ucfg.get("whitelist") or [])
        blacklist = set(ucfg.get("blacklist") or [])
        min_turnover = float(ucfg.get("min_turnover_24h_usd", 5_000_000))
        min_lev = int(ucfg.get("min_max_leverage", 10))
        exclude_new = bool(ucfg.get("exclude_new_listings", True))
        exclude_stable = bool(ucfg.get("exclude_stable_pairs", True))
        require_api = bool(ucfg.get("require_api_allowed", True))

        candidates: List[UniverseEntry] = []
        rejected: List[Dict[str, Any]] = []

        # Small accounts: a symbol whose *smallest possible order* is bigger than
        # the per-trade budget can never be traded — drop it from the watchlist
        # instead of letting the executor reject every signal for it.
        affordable_notional = 0.0
        if bool(ucfg.get("only_affordable_orders", True)):
            try:
                account = await self.broker.account()
                equity = float(getattr(account, "equity", 0.0) or 0.0)
                pct = float(self.cfg.get("risk.equity_per_trade_pct", 8.0))
                lev = float(self.cfg.get("risk.leverage", 10))
                affordable_notional = equity * pct / 100.0 * lev * 1.30
            except Exception:  # noqa: BLE001
                affordable_notional = 0.0

        for symbol, spec in contracts.items():
            tk = tickers.get(symbol)
            if tk is None or tk.last <= 0:
                continue
            if spec.quote.upper() != "USDT":
                continue
            if symbol in blacklist:
                continue
            if whitelist and symbol not in whitelist:
                continue
            if spec.state != 0:
                rejected.append({"symbol": symbol, "reason": "not live"})
                continue
            if require_api and not spec.api_allowed:
                rejected.append({"symbol": symbol, "reason": "api disabled"})
                continue
            if exclude_new and spec.is_new:
                rejected.append({"symbol": symbol, "reason": "new listing"})
                continue
            if spec.max_leverage < min_lev:
                rejected.append({"symbol": symbol, "reason": f"max leverage {spec.max_leverage}x < {min_lev}x"})
                continue
            if exclude_stable and spec.base.upper() in STABLE_BASES:
                continue
            if tk.amount24 < min_turnover:
                rejected.append({"symbol": symbol, "reason": f"turnover ${tk.amount24:,.0f} < ${min_turnover:,.0f}"})
                continue
            if tk.spread_bps > 60:      # absolute sanity cap, real gate later
                rejected.append({"symbol": symbol, "reason": f"spread {tk.spread_bps:.1f}bps"})
                continue
            if affordable_notional > 0:
                min_order_notional = float(spec.min_vol or 1.0) * spec.contract_size * tk.last
                if min_order_notional > affordable_notional:
                    rejected.append({
                        "symbol": symbol,
                        "reason": (f"minimum order {spec.min_vol:g} contracts ≈ "
                                   f"${min_order_notional:,.0f} notional > per-trade budget "
                                   f"${affordable_notional:,.0f}"),
                    })
                    continue

            range24 = safe_div(tk.high24 - tk.low24, tk.last, 0.0) * 100.0
            oi_usd = tk.hold_vol * spec.contract_size * tk.last
            candidates.append(UniverseEntry(
                symbol=symbol, price=tk.last, turnover24=tk.amount24, range24_pct=range24,
                oi_usd=oi_usd, spread_bps=tk.spread_bps, max_leverage=spec.max_leverage,
                contract=spec,
            ))

        self.last_candidate_count = len(candidates)
        self.last_eligible_count = 0
        if not candidates:
            self.rejected_sample = rejected[:60]
            return self.last_entries

        # ---- stage 2: real candle statistics for the most promising ones ---
        candidates.sort(key=lambda e: e.turnover24 * max(e.range24_pct, 0.1), reverse=True)
        stage2 = candidates[: int(min(45, len(candidates)))]
        await self._enrich(stage2, concurrency=int(ucfg.get("scan_concurrency", 10)))

        min_atr = float(ucfg.get("min_atr_pct", 0.15))
        max_atr = float(ucfg.get("max_atr_pct", 6.0))
        min_oi = float(ucfg.get("min_open_interest_usd", 0))
        eligible: List[UniverseEntry] = []
        for e in stage2:
            if not (min_atr <= e.atr_pct <= max_atr):
                rejected.append({"symbol": e.symbol, "reason": f"5m ATR {e.atr_pct:.3f}% outside {min_atr}-{max_atr}%"})
                continue
            if e.oi_usd < min_oi:
                rejected.append({"symbol": e.symbol, "reason": f"OI ${e.oi_usd:,.0f} < ${min_oi:,.0f}"})
                continue
            eligible.append(e)

        self.last_eligible_count = len(eligible)
        if not eligible:
            self.rejected_sample = rejected[:60]
            return self.last_entries

        # ---- scoring: rank blend ---------------------------------------- #
        weights = ucfg.get("weights") or {}
        w_turn = float(weights.get("turnover", 0.4))
        w_vol = float(weights.get("volatility", 0.4))
        w_mom = float(weights.get("momentum", 0.2))
        tot_w = max(1e-9, w_turn + w_vol + w_mom)
        w_turn, w_vol, w_mom = w_turn / tot_w, w_vol / tot_w, w_mom / tot_w

        turnovers = [e.turnover24 for e in eligible]
        atrs = [e.atr_pct for e in eligible]
        # momentum: prefer names that actually move (trend efficiency * range)
        momentum = [abs(e.trend_pct) * max(e.range24_pct, 0.05) for e in eligible]

        def rank_pct(values: List[float], value: float) -> float:
            """Percentile rank of ``value`` inside ``values`` (0-100)."""
            if not values:
                return 0.0
            return 100.0 * sum(1 for x in values if x <= value) / len(values)

        for e, mom in zip(eligible, momentum):
            c_turn = rank_pct(turnovers, e.turnover24)
            c_vol = rank_pct(atrs, e.atr_pct)
            c_mom = rank_pct(momentum, mom)
            e.components = {"turnover": round(c_turn, 2), "volatility": round(c_vol, 2),
                            "momentum": round(c_mom, 2)}
            # blend the three ranks into a 0-100 composite score
            e.score = w_turn * c_turn + w_vol * c_vol + w_mom * c_mom

        eligible.sort(key=lambda e: e.score, reverse=True)
        max_symbols = int(ucfg.get("max_symbols", 20))
        selected = eligible[:max_symbols]
        self.last_entries = selected
        self.last_scan_ts = time.time()
        self.rejected_sample = rejected[:60]
        log.info(
            "universe scan: %d contracts -> %d eligible -> %d selected (top: %s)",
            len(contracts), len(eligible), len(selected),
            ", ".join(f"{e.symbol}({e.score:.0f})" for e in selected[:5]),
        )
        return selected

    async def _enrich(self, entries: List[UniverseEntry], concurrency: int = 6) -> None:
        sem = asyncio.Semaphore(concurrency)

        async def enrich_one(e: UniverseEntry) -> None:
            async with sem:
                try:
                    candles = await self.broker.klines(e.symbol, "Min5", 120)
                except Exception as exc:  # noqa: BLE001
                    log.debug("kline fetch failed for %s: %s", e.symbol, exc)
                    return
                if len(candles) < 40:
                    return
                # the venue returns the *in-progress* candle last: its volume and
                # range are partial, so drop it (same rule the signal engine uses)
                candles = candles[:-1]
                highs = [c.h for c in candles]
                lows = [c.l for c in candles]
                closes = [c.c for c in candles]
                atr_series = [v for v in ind.atr(highs, lows, closes, 14) if v is not None]
                if not atr_series:
                    return
                atr = atr_series[-1]
                e.atr_pct = atr / closes[-1] * 100.0
                vol_sma = ind.sma([c.v for c in candles], 20)[-1] or 0.0
                e.vol_ratio = safe_div(candles[-1].v, vol_sma, 1.0)
                adx_series = ind.adx(highs, lows, closes, 14)
                e.adx = adx_series[-1] or 0.0
                lookback = min(48, len(closes) - 1)
                e.trend_pct = safe_div(closes[-1] - closes[-lookback], closes[-lookback], 0.0) * 100.0 if lookback > 0 else 0.0

        await asyncio.gather(*(enrich_one(e) for e in entries))

    def to_dict_list(self) -> List[Dict[str, Any]]:
        return [e.to_dict() for e in self.last_entries]
