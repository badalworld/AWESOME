"""Compounding model for the "$10,000 in 7 days" objective.

The maths of the requested configuration
----------------------------------------
With ``equity_per_trade_pct = 8`` and ``leverage = 10``:

* notional per trade   = 8% x equity x 10           = 80% of equity
* take profit         = +200% ROI on margin        -> equity +16.0% per TP
* stop loss           = -(3 x ATR x leverage) ROI  -> equity -(0.08 x SL_ROI%)%

so every trade moves equity multiplicatively::

    equity_next = equity * (1 + (equity_pct/100) * (ROI/100))

This module answers, quantitatively:

* what daily growth is required to reach the target,
* what a given win rate/ATR regime actually produces (Monte Carlo, bootstrap
  from live trades when available),
* the probability of reaching the target, of drawdown, and of ruin,
* which win rate would be needed for the target to be realistic.

The output is deliberately honest about variance: a strategy with a positive
expectation can still have a low probability of hitting an aggressive target
inside a short window, and the dashboard shows that distribution.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass
class CompoundInputs:
    starting_equity: float = 1000.0
    target_equity: float = 10_000.0
    days: float = 7.0
    equity_pct_per_trade: float = 8.0
    leverage: int = 10
    tp_roi_pct: float = 200.0
    sl_roi_pct: float = 30.0
    win_rate: float = 0.45
    trades_per_day: float = 8.0
    fee_pct_of_margin: float = 0.0        # round-trip fee drag expressed in margin %
    max_concurrent: int = 10
    runs: int = 20_000
    seed: int = 12345
    empirical_rois: Optional[List[float]] = None   # closed-trade ROI% history

    @property
    def required_daily_growth_pct(self) -> float:
        if self.starting_equity <= 0 or self.days <= 0:
            return 0.0
        return ((self.target_equity / self.starting_equity) ** (1.0 / self.days) - 1.0) * 100.0

    @property
    def required_total_growth_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.target_equity / self.starting_equity - 1.0) * 100.0

    @property
    def trades_total(self) -> float:
        return self.trades_per_day * self.days

    def equity_multiplier(self, roi_pct: float) -> float:
        return 1.0 + (self.equity_pct_per_trade / 100.0) * ((roi_pct - self.fee_pct_of_margin) / 100.0)


def _sample_roi(inp: CompoundInputs, rnd: random.Random, empirical: bool) -> float:
    if empirical and inp.empirical_rois:
        return rnd.choice(inp.empirical_rois)
    # parametric: win -> TP, loss -> SL (with a small chance of a partial trail exit)
    if rnd.random() < inp.win_rate:
        if inp.tp_roi_pct and rnd.random() < 0.35:
            # some winners exit on the trailing stop, booking between start and TP
            return rnd.uniform(max(20.0, inp.tp_roi_pct * 0.25), inp.tp_roi_pct * 0.9)
        return inp.tp_roi_pct
    return -inp.sl_roi_pct * rnd.uniform(0.85, 1.15)


def monte_carlo(inp: CompoundInputs) -> Dict[str, Any]:
    rnd = random.Random(inp.seed)
    empirical = bool(inp.empirical_rois and len(inp.empirical_rois) >= 20)
    runs = max(200, int(inp.runs))
    trades_per_run = max(1, int(round(inp.trades_total)))

    finals: List[float] = []
    hit_target = 0
    ruined = 0
    max_dd_sum = 0.0
    paths_sample: List[List[float]] = []

    for run in range(runs):
        equity = inp.starting_equity
        peak = equity
        max_dd = 0.0
        hit = False
        path: List[float] = [] if run < 40 else []  # keep a few paths for charting
        for i in range(trades_per_run):
            roi = _sample_roi(inp, rnd, empirical)
            equity *= inp.equity_multiplier(roi)
            peak = max(peak, equity)
            dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
            if run < 40:
                path.append(equity)
            if equity >= inp.target_equity:
                hit = True
                if run < 40:
                    path.extend([equity] * (trades_per_run - len(path)))
                break
            if equity <= inp.starting_equity * 0.2:
                break
        finals.append(equity)
        hit_target += 1 if hit else 0
        ruined += 1 if equity <= inp.starting_equity * 0.2 else 0
        max_dd_sum += max_dd
        if path and len(path) < trades_per_run:
            path.extend([equity] * (trades_per_run - len(path)))
        if run < 40:
            paths_sample.append(path)

    finals_sorted = sorted(finals)

    def pct(q: float) -> float:
        idx = min(len(finals_sorted) - 1, max(0, int(q / 100.0 * (len(finals_sorted) - 1))))
        return round(finals_sorted[idx], 2)

    mean_final = sum(finals) / len(finals)
    return {
        "runs": runs,
        "trades_per_run": trades_per_run,
        "empirical": empirical,
        "mean_final_equity": round(mean_final, 2),
        "median_final_equity": pct(50),
        "p05": pct(5), "p25": pct(25), "p75": pct(75), "p95": pct(95),
        "prob_hit_target_pct": round(100.0 * hit_target / runs, 2),
        "prob_ruin_pct": round(100.0 * ruined / runs, 2),
        "avg_max_drawdown_pct": round(max_dd_sum / runs, 2),
        "expected_growth_multiple": round(mean_final / inp.starting_equity, 3) if inp.starting_equity else 0.0,
        "sample_paths": [[round(v, 2) for v in path] for path in paths_sample[:12]],
        "inputs": {
            "starting_equity": inp.starting_equity,
            "target_equity": inp.target_equity,
            "days": inp.days,
            "equity_pct_per_trade": inp.equity_pct_per_trade,
            "leverage": inp.leverage,
            "tp_roi_pct": inp.tp_roi_pct,
            "sl_roi_pct": inp.sl_roi_pct,
            "win_rate": inp.win_rate,
            "trades_per_day": inp.trades_per_day,
            "required_daily_growth_pct": round(inp.required_daily_growth_pct, 3),
            "required_total_growth_pct": round(inp.required_total_growth_pct, 2),
        },
    }


def breakeven_win_rate(inp: CompoundInputs) -> float:
    """Win rate at which expected log-growth per trade is exactly zero."""
    win_mult = inp.equity_multiplier(inp.tp_roi_pct)
    loss_mult = inp.equity_multiplier(-inp.sl_roi_pct)
    if win_mult <= 0:
        return 1.0
    a = math.log(win_mult)
    b = math.log(loss_mult) if loss_mult > 0 else -10.0
    if a - b == 0:
        return 0.0
    p = -b / (a - b)
    return max(0.0, min(1.0, p))


def required_win_rate_for_target(inp: CompoundInputs) -> Dict[str, Any]:
    """Win rate needed so that the *expected* log-growth reaches the target.

    Uses the expected log-growth per trade: with ``N`` trades the equity
    multiplier is ``exp(N * E[ln(m)])``, so we need
    ``E[ln(m)] = ln(target/start) / N``.
    """
    win_mult = inp.equity_multiplier(inp.tp_roi_pct)
    loss_mult = inp.equity_multiplier(-inp.sl_roi_pct)
    n = max(1.0, inp.trades_total)
    if inp.starting_equity <= 0 or inp.target_equity <= 0:
        return {"feasible": False, "reason": "invalid equity inputs"}
    need = math.log(inp.target_equity / inp.starting_equity) / n
    a = math.log(win_mult) if win_mult > 0 else float("-inf")
    b = math.log(loss_mult) if loss_mult > 0 else float("-inf")
    if not (a > b) or not math.isfinite(a) or not math.isfinite(b):
        return {"feasible": False, "reason": "loss size too large relative to win size"}
    p = (need - b) / (a - b)
    return {
        "feasible": 0.0 <= p <= 1.0,
        "required_win_rate_pct": round(max(0.0, min(1.0, p)) * 100.0, 2),
        "breakeven_win_rate_pct": round(breakeven_win_rate(inp) * 100.0, 2),
        "required_log_growth_per_trade": round(need, 6),
        "trades_total": round(n, 1),
    }


def sensitivity(inp: CompoundInputs, win_rates: Sequence[float] = (0.30, 0.35, 0.40, 0.45, 0.50, 0.60)) -> List[Dict[str, Any]]:
    out = []
    for wr in win_rates:
        scenario = CompoundInputs(**{**inp.__dict__, "win_rate": float(wr), "runs": max(500, inp.runs // 8)})
        res = monte_carlo(scenario)
        out.append({
            "win_rate": round(float(wr) * 100.0, 1),
            "prob_hit_target_pct": res["prob_hit_target_pct"],
            "median_final_equity": res["median_final_equity"],
            "mean_final_equity": res["mean_final_equity"],
            "prob_ruin_pct": res["prob_ruin_pct"],
            "avg_max_drawdown_pct": res["avg_max_drawdown_pct"],
        })
    return out


def build_report(
    *,
    starting_equity: float,
    target_equity: float,
    days: float,
    equity_pct_per_trade: float,
    leverage: int,
    tp_roi_pct: float,
    sl_roi_pct: float,
    trades_per_day: float,
    win_rate: float,
    runs: int = 20_000,
    empirical_rois: Optional[List[float]] = None,
) -> Dict[str, Any]:
    inp = CompoundInputs(
        starting_equity=starting_equity,
        target_equity=target_equity,
        days=days,
        equity_pct_per_trade=equity_pct_per_trade,
        leverage=leverage,
        tp_roi_pct=tp_roi_pct,
        sl_roi_pct=sl_roi_pct,
        trades_per_day=trades_per_day,
        win_rate=win_rate,
        runs=runs,
        empirical_rois=empirical_rois,
    )
    sim = monte_carlo(inp)
    return {
        "simulation": sim,
        "requirements": required_win_rate_for_target(inp),
        "breakeven_win_rate_pct": round(breakeven_win_rate(inp) * 100.0, 2),
        "required_daily_growth_pct": round(inp.required_daily_growth_pct, 3),
        "per_trade_math": {
            "notional_pct_of_equity": round(equity_pct_per_trade * leverage, 2),
            "equity_gain_per_tp_pct": round(equity_pct_per_trade * tp_roi_pct / 100.0, 3),
            "equity_loss_per_sl_pct": round(-equity_pct_per_trade * sl_roi_pct / 100.0, 3),
        },
        "sensitivity": sensitivity(inp),
        "disclaimer": (
            "Monte-Carlo projection from the configured parameters. Past/paper performance does not "
            "guarantee future results; the target is a planning input, not a promise."
        ),
    }
