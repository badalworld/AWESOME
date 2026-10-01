"""Signal engine: candle -> features -> divergence -> filters -> Signal."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence

from ..exchange.base import Candle, Ticker
from . import divergence as divmod
from .features import FeatureSet, build_features, build_htf_context
from .filters import FilterReport, evaluate

log = logging.getLogger("signals")


@dataclass
class Signal:
    symbol: str
    side: str
    kind: str
    ts: int
    price: float
    score: float
    atr: float
    atr_pct: float
    report: FilterReport
    divergence: divmod.Divergence
    features_snapshot: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    signal_id: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side,
            "kind": self.kind,
            "ts": self.ts,
            "price": self.price,
            "score": round(self.score, 2),
            "atr": self.atr,
            "atr_pct": round(self.atr_pct, 4),
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "divergence": self.divergence.to_dict(),
            "filters": self.report.to_dict(),
            "features": self.features_snapshot,
        }


class SignalEngine:
    """Stateless analysis + per-symbol cooldown bookkeeping."""

    def __init__(self, cfg, db=None) -> None:
        self.cfg = cfg
        self.db = db
        self._last_signal_bar: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    def _params(self) -> Dict[str, Any]:
        return self.cfg.section("strategy")

    async def analyze(
        self,
        symbol: str,
        candles_5m: Sequence[Candle],
        ticker: Optional[Ticker],
        *,
        htf_candles: Optional[Dict[str, Sequence[Candle]]] = None,
        depth_usd: float = 0.0,
        order_notional_usd: float = 0.0,
    ) -> Optional[Signal]:
        p = self._params()
        if len(candles_5m) < max(60, p.get("ao_slow", 34) + 10):
            return None

        features = build_features(
            symbol,
            candles_5m,
            ticker,
            ao_fast=int(p.get("ao_fast", 5)),
            ao_slow=int(p.get("ao_slow", 34)),
            atr_period=int(self.cfg.get("stoploss.atr_period", 14)),
            ema_period=int(self.cfg.get("filters.trend.ema_period", 200)),
            vol_period=int(self.cfg.get("filters.volume.volume_sma_period", 20)),
            rsi_period=int(self.cfg.get("filters.momentum.rsi_period", 14)),
            adx_period=int(self.cfg.get("filters.chop.adx_period", 14)),
            depth_usd=depth_usd,
        )
        if features is None:
            return None

        # higher-timeframe context (15m by default, plus 1h for context)
        for tf in {str(self.cfg.get("filters.htf.htf_timeframe", "Min15")), "Min15", "Min60"}:
            series = (htf_candles or {}).get(tf)
            if not series:
                continue
            ctx = build_htf_context(
                tf, series,
                ao_fast=int(p.get("ao_fast", 5)), ao_slow=int(p.get("ao_slow", 34)),
                ema_period=int(self.cfg.get("filters.htf.htf_ema_period", 50)),
            )
            if ctx:
                features.htf[tf] = ctx

        # cooldown: one signal per N bars per symbol
        cooldown_bars = int(p.get("signal_cooldown_bars", 6))
        tf_seconds = self._timeframe_seconds(str(p.get("timeframe", "Min5")))
        last_ts = self._last_signal_bar.get(symbol, 0)
        bar_id = int(features.ts)
        if last_ts and (bar_id - last_ts) < cooldown_bars * tf_seconds:
            return None

        div = divmod.detect_divergence(
            symbol,
            candles_5m,
            features.ao_series,
            features.atr,
            pivot_k=int(p.get("pivot_k", 2)),
            lookback_bars=int(p.get("lookback_bars", 90)),
            min_gap=int(p.get("min_pivot_gap", 3)),
            max_gap=int(p.get("max_pivot_gap", 45)),
            min_ao_delta_atr=float(p.get("min_ao_delta_atr", 0.12)),
            require_ao_extreme=bool(p.get("require_ao_extreme", True)),
            require_trigger_break=bool(p.get("require_trigger_break", True)),
            allow_hidden=bool(p.get("allow_hidden_divergence", False)),
        )
        if div is None:
            return None
        if not div.trigger_confirmed:
            return None

        report = evaluate(
            features, div, self._filters_cfg(),
            order_notional_usd=order_notional_usd,
            min_signal_score=float(p.get("min_signal_score", 60)),
        )
        div.score = divmod.score_divergence(div)
        signal = Signal(
            symbol=symbol,
            side=div.side,
            kind=div.kind,
            ts=features.ts,
            price=features.close,
            score=report.score,
            atr=features.atr,
            atr_pct=features.atr_pct,
            report=report,
            divergence=div,
            features_snapshot=self._snapshot(features),
            status="pending" if report.passed else "filtered",
            reason="" if report.passed else "; ".join(report.rejections()[:3]),
        )
        if report.passed:
            self._last_signal_bar[symbol] = bar_id
        return signal

    # ------------------------------------------------------------------ #
    def _filters_cfg(self) -> Dict[str, Any]:
        sections = self.cfg.section("filters")
        return dict(sections) if isinstance(sections, dict) else {}

    @staticmethod
    def _timeframe_seconds(tf: str) -> int:
        return {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800, "Min60": 3600}.get(tf, 300)

    @staticmethod
    def _snapshot(f: FeatureSet) -> Dict[str, Any]:
        return {
            "close": f.close,
            "atr": round(f.atr, 8),
            "atr_pct": round(f.atr_pct, 4),
            "atr_percentile": round(f.atr_percentile, 1),
            "ao": round(f.ao, 8),
            "ao_prev": round(f.ao_prev, 8),
            "ao_signal": round(f.ao_signal, 8),
            "rsi": round(f.rsi, 2),
            "adx": round(f.adx, 2),
            "macd_hist": round(f.macd_hist, 10),
            "vol_ratio": round(f.vol_ratio, 3),
            "ema200": f.ema200,
            "ema50": f.ema50,
            "ema200_slope_pct": round(f.ema200_slope_pct, 4),
            "trend_state": f.trend_state(),
            "turnover24": round(f.turnover24, 2),
            "spread_bps": round(f.spread_bps, 3),
            "depth_usd": round(f.depth_usd, 2),
            "htf": {
                tf: {"ao": round(ctx.ao, 8), "ao_rising": ctx.ao_rising,
                     "trend_up": ctx.trend_up, "ema": ctx.ema}
                for tf, ctx in f.htf.items()
            },
        }
