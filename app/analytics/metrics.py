"""Performance analytics: trade statistics and equity-curve metrics."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def max_drawdown(equity_curve: Sequence[float]) -> Dict[str, float]:
    peak = 0.0
    worst = 0.0
    worst_pct = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            dd = peak - value
            if dd > worst:
                worst = dd
                worst_pct = dd / peak * 100.0
    return {"max_drawdown_usd": round(worst, 4), "max_drawdown_pct": round(worst_pct, 3)}


def compute_trade_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    if not closed:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl": 0.0,
            "profit_factor": 0.0, "expectancy_usd": 0.0, "expectancy_roi": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "avg_roi": 0.0, "best_roi": 0.0,
            "worst_roi": 0.0, "avg_hold_min": 0.0, "max_win_streak": 0,
            "max_loss_streak": 0, "total_fees": 0.0, "gross_profit": 0.0,
            "gross_loss": 0.0, "avg_peak_roi": 0.0, "trail_exits": 0, "tp_exits": 0,
            "sl_exits": 0, "kelly_fraction": 0.0,
        }

    pnls = [float(t.get("realized_pnl") or 0.0) for t in closed]
    rois = [float(t.get("roi_pct") or 0.0) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    holds = [
        (float(t.get("closed_at") or 0) - float(t.get("opened_at") or 0)) / 60.0
        for t in closed if t.get("opened_at") and t.get("closed_at")
    ]

    win_streak = loss_streak = cur_win = cur_loss = 0
    for pnl in pnls:
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        win_streak = max(win_streak, cur_win)
        loss_streak = max(loss_streak, cur_loss)

    win_rate = len(wins) / len(pnls)
    avg_win = _mean(wins) if wins else 0.0
    avg_loss = _mean(losses) if losses else 0.0
    expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss

    # Kelly fraction on ROI outcomes (capped, informational only)
    kelly = 0.0
    if avg_win > 0 and avg_loss < 0:
        payoff = avg_win / abs(avg_loss)
        kelly = max(0.0, min(1.0, (win_rate * payoff - (1 - win_rate)) / payoff))

    reasons = [str(t.get("exit_reason") or "") for t in closed]
    return {
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate * 100.0, 2),
        "pnl": round(sum(pnls), 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0,
        "expectancy_usd": round(expectancy, 4),
        "expectancy_roi": round(_mean(rois), 2),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "avg_roi": round(_mean(rois), 2),
        "best_roi": round(max(rois), 2),
        "worst_roi": round(min(rois), 2),
        "avg_hold_min": round(_mean(holds), 1),
        "max_win_streak": win_streak,
        "max_loss_streak": loss_streak,
        "total_fees": round(sum(float(t.get("fees_usd") or 0.0) for t in closed), 4),
        "avg_peak_roi": round(_mean([float(t.get("peak_roi_pct") or 0.0) for t in closed]), 2),
        "tp_exits": sum(1 for r in reasons if "take_profit" in r),
        "sl_exits": sum(1 for r in reasons if "stop_loss" in r),
        "trail_exits": sum(1 for r in reasons if "stop_loss" in r and "trail" in r),
        "kelly_fraction": round(kelly, 4),
    }


def compute_curve_stats(curve: List[Dict[str, Any]], starting_equity: float = 0.0) -> Dict[str, Any]:
    if not curve:
        return {"points": 0, "equity": starting_equity, "return_pct": 0.0,
                "max_drawdown_pct": 0.0, "max_drawdown_usd": 0.0, "volatility_pct": 0.0}
    equities = [float(p.get("equity") or 0.0) for p in curve]
    first = starting_equity or equities[0]
    last = equities[-1]
    rets = []
    for i in range(1, len(equities)):
        if equities[i - 1] > 0:
            rets.append(equities[i] / equities[i - 1] - 1.0)
    dd = max_drawdown(equities)
    return {
        "points": len(equities),
        "equity": round(last, 4),
        "return_pct": round((last / first - 1.0) * 100.0, 3) if first else 0.0,
        "max_drawdown_pct": dd["max_drawdown_pct"],
        "max_drawdown_usd": dd["max_drawdown_usd"],
        "volatility_pct": round(_stdev(rets) * 100.0, 4),
        "period_start": curve[0].get("ts"),
        "period_end": curve[-1].get("ts"),
    }


def daily_returns(curve: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate the equity curve into daily buckets (UTC)."""
    buckets: Dict[str, Dict[str, float]] = {}
    for point in curve:
        ts = float(point.get("ts") or 0)
        day = time.strftime("%Y-%m-%d", time.gmtime(ts))
        bucket = buckets.setdefault(day, {"day": day, "open": float(point.get("equity") or 0),
                                          "close": float(point.get("equity") or 0),
                                          "high": float(point.get("equity") or 0),
                                          "low": float(point.get("equity") or 0)})
        eq = float(point.get("equity") or 0)
        bucket["close"] = eq
        bucket["high"] = max(bucket["high"], eq)
        bucket["low"] = min(bucket["low"], eq)
    out = []
    for day, b in sorted(buckets.items()):
        ret = (b["close"] / b["open"] - 1.0) * 100.0 if b["open"] else 0.0
        out.append({**{k: round(v, 4) for k, v in b.items() if k != "day"}, "day": day, "return_pct": round(ret, 3)})
    return out
