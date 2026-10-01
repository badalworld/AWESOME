#!/usr/bin/env python3
"""Rule simulator: what do the shipped TP/SL/trail rules actually produce?

``python3 tools/rule_sim.py [--runs 4000] [--sweep]``

Every path is a **martingale** price walk (GBM with the Itô term subtracted, so
E[price] is flat) calibrated to 5-minute alt volatility, ticked at 5 s. The exit logic is the *real* code from ``app/risk/manager.py``
(``evaluate_trailing`` / ``stop_price_from_roi``), so the numbers below are a
property of the shipped rules, not of a re-implementation.

Because the walk has no drift by design, the result answers one question: how
often must the *signal* be right for these rules to make money? That is reported
as the **break-even win rate** built from the rule-induced mean win/loss:

    BE = E|loss| / (E[win] + E|loss|)

Compounding is then applied with the live sizing rules (8% of *running* equity
per trade, 10x leverage, fees per venue) so the growth column is comparable to
the dashboard's numbers.
"""
from __future__ import annotations

import argparse
import math
import random
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.risk.manager import evaluate_trailing, stop_price_from_roi  # noqa: E402

# A tiny stand-in for the Config object the rule code reads.
class _Cfg:
    def __init__(self, values: Dict[str, float]) -> None:
        self._v = values

    def get(self, key: str, default=None):
        return self._v.get(key, default)


def _cfg(tp: float, trail: Tuple[float, float, float, float], partial_roi: Optional[float],
         partial_fraction: float, use_mark_peak: bool = True) -> _Cfg:
    start, initial, step, stop_step = trail
    return _Cfg({
        "partial.roi": partial_roi,
        "partial.fraction": partial_fraction,
        "trailing.enabled": True,
        "trailing.trail_start_roi": start,
        "trailing.trail_initial_stop_roi": initial,
        "trailing.trail_step_roi": step,
        "trailing.trail_stop_step_roi": stop_step,
        "trailing.min_move_bps": 2.0,
        "trailing.use_mark_price_for_peak": use_mark_peak,
        "trailing.persist_state": True,
        "trailing.replace_stop_on_step": False,
        "takeprofit.tp_roi_pct": tp,
        "stoploss.min_sl_roi_pct": 5.0,
        "stoploss.max_sl_roi_pct": 150.0,
    })


def simulate_path(rng: random.Random, *, cfg: _Cfg, leverage: int, atr_pct: float,
                  sl_roi: float, ticks: int = 2880, bar_ticks: int = 60,
                  drift_pct: float = 0.0) -> Tuple[float, str]:
    """Return (ROI on margin, exit reason) for one trade.

    5-second ticks; ``bar_ticks`` = 60 makes one 5-minute bar. Per-tick sigma is
    the bar sigma divided by ``sqrt(bar_ticks)``, and the bar sigma is derived
    from the ATR (``ATR ≈ 1.41 σ`` for a driftless walk).
    """
    entry = 100.0
    price = entry
    peak = 0.0
    stop_roi: Optional[float] = None
    stop_price: Optional[float] = None
    sl_price = stop_price_from_roi(entry, sl_roi, leverage, True)
    tp_roi = float(cfg.get("takeprofit.tp_roi_pct", 200.0))
    partial_roi = cfg.get("partial.roi")
    partial_fraction = float(cfg.get("partial.fraction", 0.0) or 0.0)
    banked = 0.0
    remaining = 1.0
    partial_done = False
    bar_sigma = max(atr_pct, 1e-6) / 100.0 / 1.41
    step_sigma = bar_sigma / math.sqrt(max(1, bar_ticks))
    # Itô correction: a zero log-drift GBM still drifts *up* in price terms by
    # exp(sigma^2/2) per step. Without this term the simulator manufactures a
    # +1.5-2% ROI "edge" out of nothing, so the walk is made an exact martingale.
    drift = -0.5 * step_sigma * step_sigma
    if drift_pct:
        drift += math.log(1.0 + drift_pct / 100.0) / max(1, ticks)
    for _ in range(ticks):
        price *= math.exp(drift + rng.gauss(0.0, step_sigma))
        roi = (price - entry) / entry * 100.0 * leverage
        if roi > peak:
            peak = roi
        if roi >= tp_roi:
            return banked + remaining * tp_roi, "tp"
        if price <= sl_price:
            return banked + remaining * roi, "sl"
        if partial_roi is not None and not partial_done and roi >= partial_roi:
            banked += partial_fraction * roi
            remaining -= partial_fraction
            partial_done = True
        decision = evaluate_trailing(
            entry_price=entry, mark_price=price, peak_roi=peak,
            current_stop_roi=stop_roi, current_stop_price=stop_price,
            initial_sl_price=sl_price, initial_sl_roi=sl_roi, leverage=leverage,
            is_long=True, cfg=cfg, last_step_index=-1,
        )
        if decision.action in ("activate", "step") and decision.stop_price:
            stop_roi, stop_price = decision.stop_roi, decision.stop_price
        if stop_price is not None and price <= stop_price:
            return banked + remaining * roi, "trail"
    return banked + remaining * ((price - entry) / entry * 100.0 * leverage), "timeout"


def run(rng: random.Random, *, runs: int, **kw) -> Dict[str, float]:
    cfg: _Cfg = kw.pop("cfg")
    rois: List[float] = []
    reasons: Dict[str, int] = {}
    for _ in range(runs):
        roi, why = simulate_path(rng, cfg=cfg, **kw)
        rois.append(roi)
        reasons[why] = reasons.get(why, 0) + 1
    wins = [r for r in rois if r > 0]
    losses = [r for r in rois if r <= 0]
    mean_win = statistics.fmean(wins) if wins else 0.0
    mean_loss = statistics.fmean(losses) if losses else 0.0
    be = abs(mean_loss) / (mean_win + abs(mean_loss)) if (mean_win + abs(mean_loss)) else 1.0
    return {
        "mean_roi": statistics.fmean(rois),
        "se": (statistics.stdev(rois) / math.sqrt(runs)) if runs > 1 else 0.0,
        "median_roi": statistics.median(rois),
        "mean_win": mean_win,
        "mean_loss": mean_loss,
        "p_win": len(wins) / runs,
        "be_wr": be,
        **{f"pct_{k}": v / runs for k, v in reasons.items()},
    }


def compound(mean_roi: float, *, equity_pct: float = 8.0, trades: int = 100,
             fee_pct: float = 0.4, start: float = 1000.0) -> float:
    """Equity multiplier for ``trades`` trades at a constant mean ROI per trade."""
    per_trade = equity_pct / 100.0 * (mean_roi - fee_pct) / 100.0
    return start * (1.0 + per_trade) ** trades


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--atr-pct", type=float, default=1.2, help="5m ATR as %% of price")
    ap.add_argument("--leverage", type=int, default=10)
    ap.add_argument("--sl-roi", type=float, default=36.0, help="3xATR expressed as ROI on margin")
    ap.add_argument("--sweep", action="store_true", help="compare candidate rule sets")
    ap.add_argument("--drift-pct", type=float, default=0.0,
                    help="expected favourable price move over the whole trade (%%) — the signal's edge")
    args = ap.parse_args()

    candidates = {
        "shipped 2026-10 (tp200, trail 30/20/10/10)": (_cfg(200, (30, 20, 10, 10), None, 0.0)),
        "A  realistic tp90, trail 40/20/10/10": (_cfg(90, (40, 20, 10, 10), None, 0.0)),
        "B  A + partial 50% of size at +50% ROI": (_cfg(90, (40, 20, 10, 10), 50.0, 0.5)),
        "C  tp60, trail 35/15/10/10 + partial 50% at +40": (_cfg(60, (35, 15, 10, 10), 40.0, 0.5)),
        "D  tp120, trail 50/25/10/10 + partial 50% at +50": (_cfg(120, (50, 25, 10, 10), 50.0, 0.5)),
    }
    if not args.sweep:
        candidates = {k: v for k, v in candidates.items() if k.startswith("shipped")}

    print(f"zero-drift GBM paths | {args.runs} runs | ATR {args.atr_pct:.2f}%/5m "
          f"| SL {args.sl_roi:.0f}% ROI | {args.leverage}x | seed {args.seed}")
    print(f"{'rule set':46} {'P(win)':>7} {'mean win':>9} {'mean loss':>10} "
          f"{'mean ROI':>16} {'BE WR':>7} {'$1k/100tr':>10}")
    print("-" * 118)
    for name, cfg in candidates.items():
        # two independent seeds -> report the mean and the standard error, so a
        # sub-1%% difference is never mistaken for an edge
        rng = random.Random(args.seed)
        a = run(rng, runs=args.runs // 2, cfg=cfg, leverage=args.leverage,
                atr_pct=args.atr_pct, sl_roi=args.sl_roi, drift_pct=args.drift_pct)
        rng = random.Random(args.seed + 1)
        b = run(rng, runs=args.runs - args.runs // 2, cfg=cfg, leverage=args.leverage,
                atr_pct=args.atr_pct, sl_roi=args.sl_roi, drift_pct=args.drift_pct)
        mean_roi = (a["mean_roi"] + b["mean_roi"]) / 2.0
        se = (a["se"] / math.sqrt(2) if args.runs > 1 else 0.0)
        gap = abs(a["mean_roi"] - b["mean_roi"])
        if gap > 2.0 * math.sqrt(a["se"] ** 2 + b["se"] ** 2) and args.runs >= 400:
            print(f"  (note: the two seeds disagree by {gap:.2f}% ROI — "
                  f"treat this row as noise)")
        final = compound(mean_roi, trades=100)
        print(f"{name:46} {a['p_win']*100:6.1f}% {a['mean_win']:+8.1f}% "
              f"{a['mean_loss']:+9.1f}% {mean_roi:+8.2f}% +-{se:5.2f}% {a['be_wr']*100:6.1f}% "
              f"${final:9,.0f}")
    print()
    print("mean win / mean loss / mean ROI are percentages of margin (100% = 1x the trade margin).")
    print("BE WR = break-even directional win rate for that rule geometry "
          "(E|loss| / (E[win] + E|loss|)).")
    print("$1k/100tr = $1,000 after 100 trades at the mean ROI, 8% of running equity per")
    print("trade, 10x leverage, MEXC taker fees (0.4% of margin per round trip).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
