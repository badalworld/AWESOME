"""Anti-fake-signal filter pipeline.

Raw AO divergence is a *context* signal, not an entry. Every candidate has to
survive a stack of independent filters; each one targets a specific way
divergence signals fail in practice:

======================  =====================================================
filter                  catches
======================  =====================================================
volatility              dead/illiquid chop (no follow-through) and chaos
                        (news candles, stop hunts)
trend                   counter-trend divergences in a strong trend
htf                     lower-timeframe noise fighting the higher timeframe
volume                  divergences nobody traded — no participation
momentum                exhaustion entries (RSI already overbought) and
                        missing MACD flip
chop                    ranging markets producing endless fake pivots
orderbook               wide-spread / thin books where fills are terrible
shock                   entering right after a >Nx ATR impulse candle
======================  =====================================================

The pipeline is deliberately explainable: every filter returns a value and a
threshold, all of which are persisted with the signal and shown in the
dashboard ("why did the bot skip this?").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .divergence import LONG, SHORT, Divergence
from .features import FeatureSet


@dataclass
class FilterResult:
    name: str
    passed: bool
    value: Any = None
    threshold: Any = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "value": self.value,
                "threshold": self.threshold, "note": self.note}


@dataclass
class FilterReport:
    passed: bool = True
    results: List[FilterResult] = field(default_factory=list)
    score: float = 0.0
    score_parts: Dict[str, float] = field(default_factory=dict)

    def add(self, res: FilterResult) -> None:
        self.results.append(res)
        if not res.passed:
            self.passed = False

    def rejections(self) -> List[str]:
        return [f"{r.name}: {r.note}" for r in self.results if not r.passed]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 2),
            "score_parts": {k: round(v, 2) for k, v in self.score_parts.items()},
            "results": [r.to_dict() for r in self.results],
        }


def evaluate(
    features: FeatureSet,
    div: Divergence,
    filters_cfg: Dict[str, Any],
    *,
    order_notional_usd: float = 0.0,
    min_signal_score: float = 60.0,
) -> FilterReport:
    report = FilterReport()

    _volatility(features, div, filters_cfg.get("volatility", {}), report)
    _trend(features, div, filters_cfg.get("trend", {}), report)
    _htf(features, div, filters_cfg.get("htf", {}), report)
    _volume(features, div, filters_cfg.get("volume", {}), report)
    _momentum(features, div, filters_cfg.get("momentum", {}), report)
    _chop(features, div, filters_cfg.get("chop", {}), report)
    _orderbook(features, div, filters_cfg.get("orderbook", {}), report,
               order_notional_usd=order_notional_usd)
    _shock(features, div, filters_cfg.get("shock", {}), report)

    report.score, report.score_parts = _composite_score(features, div)
    if report.score < min_signal_score:
        report.add(FilterResult("quality_score", False, report.score, min_signal_score,
                                note=f"composite score {report.score:.1f} < {min_signal_score}"))
    return report


# --------------------------------------------------------------------------- #
#  individual filters
# --------------------------------------------------------------------------- #
def _volatility(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    lo = float(cfg.get("min_atr_pct", 0.12))
    hi = float(cfg.get("max_atr_pct", 4.0))
    pct_min = float(cfg.get("min_atr_percentile", 25.0))
    rep.add(FilterResult(
        "volatility_band", lo <= f.atr_pct <= hi, round(f.atr_pct, 3), f"{lo}-{hi}%",
        note=f"5m ATR {f.atr_pct:.3f}% outside tradable band {lo}-{hi}%",
    ))
    rep.add(FilterResult(
        "volatility_percentile", f.atr_percentile >= pct_min, round(f.atr_percentile, 1), pct_min,
        note=f"ATR percentile {f.atr_percentile:.0f} below {pct_min:.0f} (market too quiet)",
    ))


def _trend(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    mode = str(cfg.get("mode", "ema")).lower()
    if mode == "off" or f.ema200 is None:
        rep.add(FilterResult("trend_alignment", True, "skipped", mode, note="trend filter disabled/warming up"))
        return
    if mode == "ema_stack":
        fast = f.ema50 or f.close
        ok = (f.close > f.ema200 and fast >= f.ema200) if d.side == LONG else (f.close < f.ema200 and fast <= f.ema200)
        desc = "close & EMA50 vs EMA200"
    else:
        ok = f.close > f.ema200 if d.side == LONG else f.close < f.ema200
        desc = "close vs EMA200"
    rep.add(FilterResult("trend_alignment", ok, f.trend_state(), f"{d.side.lower()} wants {mode}", note=f"{desc}: trend is {f.trend_state()}"))
    if cfg.get("require_slope", False):
        ok_slope = f.ema200_slope_pct >= 0 if d.side == LONG else f.ema200_slope_pct <= 0
        rep.add(FilterResult("trend_slope", ok_slope, round(f.ema200_slope_pct, 4), "no opposing slope",
                             note=f"EMA200 slope {f.ema200_slope_pct:+.3f}% opposes the trade"))


def _htf(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    tf = str(cfg.get("htf_timeframe", "Min15"))
    ctx = f.htf.get(tf)
    if ctx is None:
        rep.add(FilterResult("htf_confirmation", True, "no-data", tf, note=f"no {tf} context available"))
        return
    checks = []
    if ctx.ema is not None:
        checks.append(("htf_ema", ctx.trend_up if d.side == LONG else ctx.trend_down,
                       "above" if d.side == LONG else "below", f"{tf} EMA"))
    if cfg.get("require_htf_ao_rising", True):
        ao_ok = ctx.ao_rising if d.side == LONG else ctx.ao_falling
        checks.append(("htf_ao", ao_ok, "rising" if d.side == LONG else "falling", f"{tf} AO"))
    for name, ok, want, desc in checks:
        rep.add(FilterResult(name, ok, f"{ctx.ao:.6g}", want, note=f"{desc} not {want} ({desc} AO={ctx.ao:.6g})"))


def _volume(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    mult = float(cfg.get("min_volume_mult", 1.15))
    rep.add(FilterResult("volume_confirmation", f.vol_ratio >= mult, round(f.vol_ratio, 3), f">= {mult}",
                         note=f"trigger candle volume {f.vol_ratio:.2f}x SMA20 (needs {mult}x)"))
    min_turnover = float(cfg.get("min_turnover_24h_usd", 5_000_000))
    rep.add(FilterResult("liquidity_turnover", f.turnover24 >= min_turnover, round(f.turnover24, 0), min_turnover,
                         note=f"24h turnover ${f.turnover24:,.0f} below ${min_turnover:,.0f}"))
    if cfg.get("require_volume_climax", False):
        # capitulation volume at the divergence pivot is a classic reversal tell
        idx = min(d.p2_index, len(f.candles) - 1)
        pivot_vol = f.candles[idx].v if f.candles else 0.0
        ratio = pivot_vol / f.vol_sma if f.vol_sma else 0.0
        rep.add(FilterResult("volume_climax", ratio >= 1.5, round(ratio, 3), ">= 1.5",
                             note=f"pivot-bar volume {ratio:.2f}x SMA20 (no climax)"))


def _momentum(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    if d.side == LONG:
        ok = f.rsi <= float(cfg.get("rsi_long_max", 62))
        note = f"RSI {f.rsi:.1f} too hot for a long entry (max {cfg.get('rsi_long_max', 62)})"
        thr = float(cfg.get("rsi_long_max", 62))
    else:
        ok = f.rsi >= float(cfg.get("rsi_short_min", 38))
        note = f"RSI {f.rsi:.1f} too cold for a short entry (min {cfg.get('rsi_short_min', 38)})"
        thr = float(cfg.get("rsi_short_min", 38))
    rep.add(FilterResult("rsi_extreme", ok, round(f.rsi, 2), thr, note=note))

    if cfg.get("macd_confirm", True):
        macd_ok = (f.macd_hist > f.macd_hist_prev) if d.side == LONG else (f.macd_hist < f.macd_hist_prev)
        rep.add(FilterResult("macd_flip", macd_ok, round(f.macd_hist, 8), "turning",
                             note=f"MACD histogram not turning with the {d.side.lower()}"))

    if cfg.get("require_rsi_divergence", False):
        rsi_div_ok = f.rsi > 30 if d.side == LONG else f.rsi < 70
        rep.add(FilterResult("rsi_divergence", rsi_div_ok, round(f.rsi, 2), ">=30 / <=70",
                             note="RSI does not confirm the divergence"))


def _chop(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    min_adx = float(cfg.get("min_adx", 15.0))
    rep.add(FilterResult("adx_trend", f.adx >= min_adx, round(f.adx, 2), min_adx,
                         note=f"ADX {f.adx:.1f} < {min_adx} (range-bound, fake divergence risk)"))
    if cfg.get("require_bb_expansion", False):
        expanding = f.bb_width >= f.bb_width_prev > 0
        rep.add(FilterResult("bb_expansion", expanding, round(f.bb_width, 4), "expanding",
                             note="Bollinger bandwidth not expanding"))


def _orderbook(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport,
               *, order_notional_usd: float) -> None:
    if not cfg.get("enabled", True):
        return
    max_spread = float(cfg.get("max_spread_bps", 12))
    rep.add(FilterResult("spread", 0 < f.spread_bps <= max_spread, round(f.spread_bps, 2), f"<= {max_spread}bps",
                         note=f"spread {f.spread_bps:.1f}bps > {max_spread}bps"))
    if f.depth_usd > 0 and order_notional_usd > 0:
        mult = float(cfg.get("min_depth_mult", 5.0))
        need = order_notional_usd * mult
        rep.add(FilterResult("book_depth", f.depth_usd >= need, round(f.depth_usd, 0), round(need, 0),
                             note=f"top-5 depth ${f.depth_usd:,.0f} < {mult}x notional (${need:,.0f})"))


def _shock(f: FeatureSet, d: Divergence, cfg: Dict[str, Any], rep: FilterReport) -> None:
    if not cfg.get("enabled", True):
        return
    max_move = float(cfg.get("max_candle_atr", 4.0))
    rep.add(FilterResult("shock_candle", f.candle_return_atr <= max_move, round(f.candle_return_atr, 2), max_move,
                         note=f"last candle moved {f.candle_return_atr:.1f}x ATR (news/impulse entry blocked)"))


# --------------------------------------------------------------------------- #
#  composite quality score (0-100)
# --------------------------------------------------------------------------- #
def _composite_score(f: FeatureSet, d: Divergence) -> tuple:
    parts: Dict[str, float] = {}

    parts["divergence"] = min(1.0, d.ao_delta / 0.60) * 0.30
    parts["volume"] = min(1.0, max(0.0, f.vol_ratio - 0.8) / 1.2) * 0.20
    # volatility sweet spot: reward the upper half of the tradable band
    vol_fit = min(1.0, f.atr_pct / max(0.35, 1.0)) if f.atr_pct > 0 else 0.0
    parts["volatility"] = vol_fit * 0.15
    trend_ok = (f.trend_state() == "up") if d.side == LONG else (f.trend_state() == "down")
    parts["trend"] = (1.0 if trend_ok else 0.4) * 0.15
    htf = f.htf.get("Min15")
    htf_ok = bool(htf and ((htf.ao_rising and d.side == LONG) or (htf.ao_falling and d.side == SHORT)))
    parts["htf"] = (1.0 if htf_ok else 0.5) * 0.10
    parts["structure"] = min(1.0, f.adx / 30.0) * 0.10

    total = sum(parts.values()) * 100.0
    return round(total, 2), parts
