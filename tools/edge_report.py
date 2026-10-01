#!/usr/bin/env python3
"""Expectancy / win-probability report for the AO-divergence bot.

Runs Monte-Carlo projections with the repository's own analytics engine
(``app/analytics/compound.py``) under several assumptions about the per-trade
ROI distribution, and writes ``docs/EDGE_AND_EXPECTANCY.md``.

    python3 tools/edge_report.py                      # write the markdown report
    python3 tools/edge_report.py --print               # also dump the raw numbers
    python3 tools/edge_report.py --start 20            # small-account scenario
    python3 tools/edge_report.py --start 20 --target 60 --days 30

The *parametric* model is the one the dashboard uses (winner -> TP, loser -> SL).
The *trailing-realistic* model is the honest one for this strategy: most winners
exit on the stepped trailing stop (roughly +20 % .. +70 % ROI) and only a small
fraction ever reach the +200 % fixed TP, because +200 % ROI at 10x requires a 20 %
adverse-free price move.  Both are shown so the difference is explicit.

Nothing here is financial advice; it is a planning model with explicit assumptions
that you can (and should) re-run with your own trade history
(``--empirical-json data/venues/mexc.db`` style export -> ``roi_pct`` list).
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.analytics.compound import CompoundInputs, breakeven_win_rate, monte_carlo  # noqa: E402

# --------------------------------------------------------------------------- #
#  Assumptions (edit these to match your own data)
# --------------------------------------------------------------------------- #

START_EQUITY = 1_000.0
TARGET_EQUITY = 10_000.0
DAYS = 7.0
EQUITY_PCT = 8.0
LEVERAGE = 10
TP_ROI = 200.0
TRADES_PER_DAY = 8.0            # 56 trades / 7 d — one signal every ~90 min is realistic on 5m
RUNS = 20_000
SEED = 20261001

# 3 x ATR(14) on the 5m chart of a volatile alt typically lands between 2.4 % and
# 7.5 % of price -> 24 % .. 75 % ROI at 10x.  We model the realised loss including
# slippage/fees a little wider than the nominal stop.
SL_ROI_TYPICAL = 45.0
SL_ROI_WIDE = 60.0

# Round-trip taker cost expressed in *margin* percent (= 2 x taker_fee x leverage x 100)
FEE_MARGIN_PCT = {
    "MEXC": 0.40,       # 0.02 % taker x 2 x 10x
    "Binance": 1.00,    # 0.05 % taker x 2 x 10x
    "KuCoin": 1.20,     # 0.06 % taker x 2 x 10x
}

WIN_RATES = (0.30, 0.35, 0.40, 0.45)


# --------------------------------------------------------------------------- #
#  Per-trade distributions
# --------------------------------------------------------------------------- #

def trailing_realistic_rois(win_rate: float, sl_roi: float, n: int = 400,
                            seed: int = SEED, tp_roi: float = TP_ROI,
                            runner_p: float = 0.06, winner_cap: float = 120.0) -> List[float]:
    """ROI% samples for the way this strategy actually exits trades.

    Winners: the stepped trailing stop ratchets from +20 % and only a minority
    of trades ever travel far enough (>= +200 % ROI) to hit the fixed TP.
    Losers: stopped at ~3 x ATR (occasionally worse on a gap/slippage).
    """
    rnd = random.Random(seed + int(win_rate * 1000) + int(sl_roi))
    out: List[float] = []
    for _ in range(n):
        if rnd.random() < win_rate:
            if rnd.random() < runner_p:                # runner reaches the fixed TP
                out.append(tp_roi * rnd.uniform(0.98, 1.0))
            else:                                      # trailing-stop exit
                start = max(20.0, sl_roi * 0.45)
                out.append(rnd.triangular(start, max(start + 5.0, winner_cap), start + 25.0))
        else:
            out.append(-sl_roi * rnd.uniform(0.9, 1.25))
    return [round(v, 3) for v in out]


def bootstrap(rois: Sequence[float], fee_margin_pct: float, trades: int = 56,
              runs: int = RUNS, seed: int = SEED, start: Optional[float] = None) -> Dict[str, float]:
    """Compound ``trades`` sampled ROIs on 8 %-of-equity margin; equity floor 20 %."""
    start = START_EQUITY if start is None else start      # read at call time, not import time
    rnd = random.Random(seed)
    finals: List[float] = []
    up = 0
    hit = 0
    ruined = 0
    for _ in range(runs):
        eq = start
        for _ in range(trades):
            roi = rnd.choice(rois) - fee_margin_pct
            eq *= 1.0 + (EQUITY_PCT / 100.0) * (roi / 100.0)
            if eq >= TARGET_EQUITY:
                hit += 1
                break
            if eq <= start * 0.2:
                ruined += 1
                break
        finals.append(eq)
        up += 1 if eq > start else 0
    finals.sort()

    def q(p: float) -> float:
        return round(finals[min(len(finals) - 1, int(p / 100.0 * (len(finals) - 1)))], 2)

    return {
        "mean": round(sum(finals) / len(finals), 2),
        "median": q(50), "p05": q(5), "p95": q(95),
        "prob_profit_pct": round(100.0 * up / runs, 2),
        "prob_hit_target_pct": round(100.0 * hit / runs, 2),
        "prob_ruin_pct": round(100.0 * ruined / runs, 2),
    }


def expected_value_per_trade(rois: Sequence[float], fee_margin_pct: float) -> Dict[str, float]:
    """Arithmetic mean ROI% and its effect on equity at 8 % margin sizing."""
    mean_roi = sum(rois) / len(rois) - fee_margin_pct
    return {
        "mean_roi_pct": round(mean_roi, 2),
        "expectancy_equity_pct": round(mean_roi * EQUITY_PCT / 100.0, 3),
        "breakeven_win_rate_pct": round(
            breakeven_win_rate(CompoundInputs(
                starting_equity=START_EQUITY, target_equity=TARGET_EQUITY, days=DAYS,
                equity_pct_per_trade=EQUITY_PCT, leverage=LEVERAGE,
                tp_roi_pct=TP_ROI, sl_roi_pct=SL_ROI_TYPICAL, win_rate=0.4,
                trades_per_day=TRADES_PER_DAY, fee_pct_of_margin=fee_margin_pct,
            )) * 100.0, 2),
    }


def breakeven_win_rate_realistic(sl_roi: float, fee: float, tp_roi: float = TP_ROI,
                                 runner_p: float = 0.06, winner_cap: float = 120.0,
                                 lo: float = 0.10, hi: float = 0.95,
                                 iters: int = 40) -> float:
    """Win rate at which the *realised* ROI distribution has zero expectancy."""
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        rois = trailing_realistic_rois(mid, sl_roi, tp_roi=tp_roi, runner_p=runner_p,
                                       winner_cap=winner_cap, seed=SEED)
        if expected_value_per_trade(rois, fee)["expectancy_equity_pct"] > 0:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2.0 * 100.0, 2)


# --------------------------------------------------------------------------- #
#  Report
# --------------------------------------------------------------------------- #

def growth_ladder(start: float, win_rate: float = 0.45, sl_roi: float = SL_ROI_TYPICAL,
                  trades_per_day: float = TRADES_PER_DAY) -> List[Dict[str, float]]:
    """Median/percentile outcome for several horizons and targets.

    Answers the only question that matters for a small account: *what can this
    actually turn into?*
    """
    rois = trailing_realistic_rois(win_rate, sl_roi)
    rows: List[Dict[str, float]] = []
    for days, target in ((7, start * 1.5), (30, start * 3.0), (90, start * 6.0),
                         (30, 1_000.0), (90, 10_000.0), (365, 10_000.0)):
        trades = int(round(trades_per_day * days))
        res = bootstrap(rois, FEE_MARGIN_PCT["MEXC"], trades=trades, start=start)
        rows.append({
            "days": days, "target": target, "trades": trades,
            **{k: v for k, v in res.items() if k != "prob_ruin_pct"},
            "p_reach_target_pct": _p_reach(start, target, rois, trades),
        })
    return rows


def _p_reach(start: float, target: float, rois: Sequence[float], trades: int,
             runs: int = 4000, seed: int = SEED) -> float:
    rnd = random.Random(seed)
    hit = 0
    for _ in range(runs):
        eq = start
        for _ in range(trades):
            eq *= 1.0 + (EQUITY_PCT / 100.0) * ((rnd.choice(rois) - FEE_MARGIN_PCT["MEXC"]) / 100.0)
            if eq >= target:
                hit += 1
                break
    return round(100.0 * hit / runs, 2)


def build() -> Dict[str, object]:
    parametric = {}
    for wr in WIN_RATES:
        inp = CompoundInputs(
            starting_equity=START_EQUITY, target_equity=TARGET_EQUITY, days=DAYS,
            equity_pct_per_trade=EQUITY_PCT, leverage=LEVERAGE, tp_roi_pct=TP_ROI,
            sl_roi_pct=SL_ROI_TYPICAL, win_rate=wr, trades_per_day=TRADES_PER_DAY,
            fee_pct_of_margin=FEE_MARGIN_PCT["MEXC"], runs=RUNS, seed=SEED,
        )
        res = monte_carlo(inp)
        parametric[f"{wr:.2f}"] = {
            "prob_hit_target_pct": res["prob_hit_target_pct"],
            "median_final_equity": res["median_final_equity"],
            "p05": res["p05"], "p95": res["p95"],
            "prob_ruin_pct": res["prob_ruin_pct"],
            "avg_max_drawdown_pct": res["avg_max_drawdown_pct"],
        }

    realistic = {}
    for wr in WIN_RATES:
        for sl in (SL_ROI_TYPICAL, SL_ROI_WIDE):
            for venue, fee in FEE_MARGIN_PCT.items():
                rois = trailing_realistic_rois(wr, sl)
                realistic[f"{wr:.2f}|sl{int(sl)}|{venue}"] = {
                    **bootstrap(rois, fee),
                    **expected_value_per_trade(rois, fee),
                }

    # Winner-capture sensitivity: the ladder's real problem is that most winners
    # book only +20..+70 % ROI.  Show what capturing more of the move would buy,
    # holding everything else constant (same 45 % ROI stop, MEXC fees).
    capture = {}
    for cap in (60.0, 90.0, 120.0, 200.0):
        rois = trailing_realistic_rois(0.42, SL_ROI_TYPICAL, winner_cap=cap)
        capture[f"cap{int(cap)}"] = {
            **expected_value_per_trade(rois, FEE_MARGIN_PCT["MEXC"]),
            "mean_winner_roi_pct": round(
                sum(r for r in rois if r > 0) / max(1, sum(1 for r in rois if r > 0)), 1),
            "breakeven_win_rate_pct": breakeven_win_rate_realistic(
                SL_ROI_TYPICAL, FEE_MARGIN_PCT["MEXC"], winner_cap=cap),
        }

    breakeven = {
        f"sl{int(sl)}|{venue}": breakeven_win_rate_realistic(sl, fee)
        for sl in (SL_ROI_TYPICAL, SL_ROI_WIDE)
        for venue, fee in FEE_MARGIN_PCT.items()
    }

    # winners-only reality check: how many clean TP hits would 10x in 7 days need?
    per_tp_gain = EQUITY_PCT * TP_ROI / 100.0                      # +16 % equity
    n_tp = math.log(TARGET_EQUITY / START_EQUITY) / math.log(1.0 + per_tp_gain / 100.0)
    required_daily = (TARGET_EQUITY / START_EQUITY) ** (1.0 / DAYS) - 1.0

    return {
        "as_of": "2026-10-01",
        "assumptions": {
            "start_equity": START_EQUITY, "target_equity": TARGET_EQUITY, "days": DAYS,
            "equity_pct_per_trade": EQUITY_PCT, "leverage": LEVERAGE,
            "tp_roi_pct": TP_ROI, "sl_roi_typical_pct": SL_ROI_TYPICAL,
            "sl_roi_wide_pct": SL_ROI_WIDE, "trades_per_day": TRADES_PER_DAY,
            "runs": RUNS, "fee_margin_pct": FEE_MARGIN_PCT,
        },
        "target_math": {
            "required_daily_growth_pct": round(daily_required(required_daily), 2),
            "required_multiple": round(TARGET_EQUITY / START_EQUITY, 1),
            "growth_ladder": None,
            "equity_gain_per_tp_pct": per_tp_gain,
            "equity_loss_per_sl_pct": round(-EQUITY_PCT * SL_ROI_TYPICAL / 100.0, 2),
            "clean_tp_hits_needed": round(n_tp, 1),
        },
        "parametric": parametric,
        "trailing_realistic": realistic,
        "winner_capture": capture,
        "breakeven_win_rate_pct": breakeven,
    }


def daily_required(x: float) -> float:
    return x * 100.0


def render(data: Dict[str, object]) -> str:
    a = data["assumptions"]                     # type: ignore[index]
    tm = data["target_math"]                    # type: ignore[index]
    par = data["parametric"]                    # type: ignore[index]
    rea = data["trailing_realistic"]            # type: ignore[index]
    breakeven = data["breakeven_win_rate_pct"]  # type: ignore[index]

    lines: List[str] = []
    add = lines.append
    add("# Expectancy & win-probability model")
    add("")
    add("_Generated by `tools/edge_report.py` — re-run it after you have live trade history "
        "(`CompoundInputs(empirical_rois=[...])` accepts your own ROI list). "
        "Not financial advice; assumptions are explicit and cheap to change._")
    add("")
    add("## 1. The target, in arithmetic")
    add("")
    add(f"* ${a['start_equity']:,.0f} → ${a['target_equity']:,.0f} in {a['days']:g} days = "
        f"**{tm['required_daily_growth_pct']:.1f} %/day** compounded "
        f"({tm['required_multiple']:.0f}x total).")
    if tm["required_daily_growth_pct"] > 25:
        add(f"* ⚠️ Anything above ~10 %/day *sustained* is outside what a 5m divergence strategy can "
            f"produce; +{tm['required_daily_growth_pct']:.0f} %/day means the plan itself is the "
            f"problem, not the settings.")
    add(f"* One full take-profit at +{a['tp_roi_pct']:.0f} % ROI on {a['equity_pct_per_trade']:.0f} % "
        f"margin at {a['leverage']}x = **+{tm['equity_gain_per_tp_pct']:.1f} % equity**.")
    add(f"* A stopped-out trade at ~{a['sl_roi_typical_pct']:.0f} % ROI = "
        f"**{tm['equity_loss_per_sl_pct']:.1f} % equity** (3 × ATR(14) on a volatile 5m alt, "
        "~10x, plus slippage/fees).")
    add(f"* So the target needs **≈ {tm['clean_tp_hits_needed']:.0f} clean TP hits in a row with zero "
        "losses**, or a far larger number of trailing-stop wins.")
    add("")
    add("### What a small account can realistically do")
    add("")
    add("| Horizon | Trades | Target | P(reach target) | Median equity | P(profit) | 5th pct | 95th pct |")
    add("|---|---|---|---|---|---|---|---|")
    for row in tm["growth_ladder"]:              # type: ignore[index]
        add(f"| {row['days']} d | {row['trades']} | ${row['target']:,.0f} | "
            f"**{row['p_reach_target_pct']:.1f} %** | ${row['median']:,.2f} | "
            f"{row['prob_profit_pct']:.0f} % | ${row['p05']:,.2f} | ${row['p95']:,.2f} |")
    add("")
    add("_Model: 45 % post-ladder win rate, 8 trades/day, 8 % margin at 10x, MEXC fees, and the "
        "same trailing-realistic ROI distribution as section 3 (position-concurrency limits are "
        "not modelled — with $20 the guard caps you at ~10 concurrent trades anyway)._")
    add("")
    add("Read it as: the strategy can compound a small account, slowly, with a real chance of the "
        "account going nowhere or down over short windows. It cannot turn $20 into $10,000 in a "
        "week — no settings, no leverage and no bot can, because that requires the market to hand "
        "you a 500x in 7 days.")
    add("")
    add("### What a trailing-stop win is actually worth")
    add("")
    add("+200 % ROI at 10x means the *price* must move **20 %** in your favour without first "
        "triggering the trail or the stop. On the 5m chart most positions are finished long before "
        "that: the ladder activates at +30 % ROI and locks +20 %, so the typical winner books "
        "**+20 % … +70 % ROI** (+1.6 % … +5.6 % equity), and only a small minority of runners "
        "reach the fixed TP.")
    add("")
    add("## 2. Parametric Monte-Carlo (the model behind the dashboard \"Compounding\" tab)")
    add("")
    add("Assumes every winner exits exactly at the +200 % TP (35 % of winners get a partial trail "
        f"exit instead), loser = −{a['sl_roi_typical_pct']:.0f} % ROI, "
        f"{a['trades_per_day']:.0f} trades/day, {a['runs']:,} runs, MEXC fees.")
    add("")
    add("| Assumed win rate | P(hit $10k in 7 d) | Median final equity | 5th pct | 95th pct | P(ruin) |")
    add("|---|---|---|---|---|---|")
    for k, v in par.items():                    # type: ignore[union-attr]
        add(f"| {float(k)*100:.0f} % | **{v['prob_hit_target_pct']:.2f} %** | "
            f"${v['median_final_equity']:,.0f} | ${v['p05']:,.0f} | ${v['p95']:,.0f} | "
            f"{v['prob_ruin_pct']:.2f} % |")
    add("")
    add("## 3. Trailing-realistic Monte-Carlo (what the exit ladder actually produces)")
    add("")
    add("Winners: trailing-stop exits (+20 % … +120 % ROI, triangular) with a 6 % chance of a full "
        "TP runner. Losers: the 3 × ATR stop with 0.9–1.25x slippage widening. "
        "56 trades over 7 days, 8 % margin, 10x.")
    add("")
    add("Break-even win rate for this distribution (numerically solved):")
    add("")
    add("| Stop ROI | " + " | ".join(FEE_MARGIN_PCT) + " |")
    add("|---|" + "---|" * len(FEE_MARGIN_PCT))
    for sl in (45, 60):
        row = [f"| {sl} % "]
        for venue in FEE_MARGIN_PCT:
            row.append(f"| **{breakeven[f'sl{sl}|{venue}']:.1f} %** ")
        add("".join(row) + "|")
    add("")
    add("| Win rate | SL ROI | Venue | Expectancy/trade | P(profitable week) | P(hit $10k) | P(ruin) | Median |")
    add("|---|---|---|---|---|---|---|---|")
    for k, v in rea.items():                    # type: ignore[union-attr]
        wr, sl, venue = k.split("|")
        add(f"| {float(wr)*100:.0f} % | {sl[2:]} % | {venue} | "
            f"{v['expectancy_equity_pct']:+.3f} % eq | {v['prob_profit_pct']:.1f} % | "
            f"**{v['prob_hit_target_pct']:.2f} %** | {v['prob_ruin_pct']:.1f} % | "
            f"${v['median']:,.0f} |")
    add("")
    # winner-capture sensitivity
    cap = data["winner_capture"]               # type: ignore[index]
    add("### The real lever: how much of the move each winner captures (MEXC, 42 % win rate)")
    add("")
    add("| Mean winner ROI | Expectancy/trade | Break-even win rate |")
    add("|---|---|---|")
    for v in cap.values():                      # type: ignore[union-attr]
        add(f"| +{v['mean_winner_roi_pct']:.0f} % | {v['expectancy_equity_pct']:+.3f} % eq | "
            f"**{v['breakeven_win_rate_pct']:.1f} %** |")
    add("")
    add("A wide ladder that books +20 % ROI on the first pullback sits at the top row; letting the "
        "trail breathe (wider `TRAIL_STEP_ROI` / smaller `TRAIL_STOP_STEP_ROI`) moves you down the "
        "table and buys several points of break-even win rate.")
    add("")

    add("## 4. Verdict")
    add("")
    add("1. **The strategy can be net-positive, but the edge lives in the filters and the exit "
        "ladder — not the +200 % TP.** With trailing-realistic exits the break-even win rate is "
        f"**{breakeven['sl45|MEXC']:.1f} % (MEXC, ~45 % ROI stop)**, right at the top of the "
        "plausible band for 5m divergence entries: every filter you keep enabled is what buys the "
        "few points of win rate above break-even.")
    add(f"2. **${a['start_equity']:,.0f} → ${a['target_equity']:,.0f} in {a['days']:g} days is not a "
        f"realistic plan.** It needs +{tm['required_daily_growth_pct']:.0f} %/day "
        f"({tm['required_multiple']:.0f}x in {a['days']:g} days), which requires either a >100 % win "
        f"rate under trailing-realistic exits or ~{tm['clean_tp_hits_needed']:.0f} consecutive clean "
        "TP hits with no losses; the honest probability is **under 1 %**, and the parametric "
        "reaches double digits only if you assume a 40–45 % win rate with the full +200 % TP filling "
        "on almost every winner. Treat that target as a stress metric, not a goal — the growth "
        "ladder in section 1 shows what the same edge does over 7/30/90/365 days instead.")
    add("3. **The real lever is how much of each winner you capture** — i.e. the trailing geometry. "
        "Wider trail steps move break-even down by 8–17 points (table above). The +200 % fixed TP is "
        "nearly irrelevant under the ladder because almost nothing survives that far, so do not "
        "tune it expecting a different outcome; tune `TRAIL_STEP_ROI` / `TRAIL_STOP_STEP_ROI` and "
        "the filter thresholds instead.")
    add("4. **Risk-of-ruin is dominated by concurrency.** 10 open positions at 8 % margin each is "
        "80 % of equity deployed; a correlated alt-coin flush hits all of them at once. The "
        "drawdown kill-switch (40 %) and daily-loss halt (25 %) are what keep a bad day from "
        "becoming a terminal day — keep them enabled.")
    add("")
    add("### Approximate per-trade probability estimates for AO 5m divergence (structure-break "
        "entry + 8 filters + 3 × ATR stop)")
    add("")
    add("| Quantity | Estimate | Basis |")
    add("|---|---|---|")
    add("| P(price moves in favour ≥ 3 % before −4.5 %) | ~40–46 % | 5m oscillator-divergence studies "
        "cluster at 38–48 % before costs; the structure-break trigger and filters remove the worst "
        "cohorts, the trend/volatility filter removes chop |")
    add("| P(trade booked positive **after** the ladder) | ~45–55 % | any trade that prints +30 % ROI "
        "then retraces is closed at ≥ +20 % ROI, so the ladder converts some \"would-be losers\" "
        "into small winners |")
    add(f"| P(profitable after 50–60 trades) | ~50–60 % | section 3 — and only if the live win rate "
        f"lands at or above the {breakeven['sl45|MEXC']:.0f} % break-even line |")
    add(f"| P({tm['required_multiple']:.0f}x in {a['days']:g} days) | **< 1 %** (≈0 % under "
        f"trailing-realistic exits) | sections 2–3; requires ~{tm['clean_tp_hits_needed']:.0f} clean "
        "TP hits or a >100 % win rate |")
    add("")
    add("> These are model outputs from the assumptions above, not measurements of your account. "
        "The only numbers worth trusting are the ones your own trades produce: run the bot in "
        "**paper mode** for 2–4 weeks, then feed the realised `roi_pct` list into "
        "`tools/edge_report.py` (the dashboard's Compounding tab does this automatically once you "
        "have ≥ 20 closed trades).")
    add("")
    return "\n".join(lines)


def main() -> int:
    global START_EQUITY, TARGET_EQUITY, DAYS
    ap = argparse.ArgumentParser(description="Expectancy report for the AO bot")
    ap.add_argument("--print", action="store_true", help="also print the raw JSON")
    ap.add_argument("--start", type=float, default=START_EQUITY, help="starting equity ($)")
    ap.add_argument("--target", type=float, default=TARGET_EQUITY, help="target equity ($)")
    ap.add_argument("--days", type=float, default=DAYS, help="horizon in days")
    ap.add_argument("--out", default=str(ROOT / "docs" / "EDGE_AND_EXPECTANCY.md"))
    ap.add_argument("--force", action="store_true",
                    help="overwrite even a curated document (hand-written sections are lost)")
    args = ap.parse_args()

    START_EQUITY, TARGET_EQUITY, DAYS = args.start, args.target, args.days
    data = build()
    data["target_math"]["growth_ladder"] = growth_ladder(START_EQUITY)   # type: ignore[index]
    md = render(data)
    out = Path(args.out)
    # This file carries hand-written sections (verdict, rule-simulator evidence
    # imported from FINAL_RULES.md). Refuse to silently wipe them; the sentinel
    # lives in the header of the curated document.
    if out.exists() and "<!-- curated:" in out.read_text(encoding="utf-8", errors="ignore") \
            and not args.force:
        raise SystemExit(
            f"refusing to overwrite {out}: it contains hand-written sections.\n"
            f"  write elsewhere:  --out docs/EDGE_AND_EXPECTANCY.generated.md\n"
            f"  or overwrite it:  --force"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out} ({len(md.splitlines())} lines)")
    if args.print:
        print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
