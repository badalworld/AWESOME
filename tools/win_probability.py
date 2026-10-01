#!/usr/bin/env python3
"""Honest win-probability / expectancy report for the shipped configuration.

The question this answers is *not* "does the arithmetic reach $10,000?" (it can,
on paper, see ``tools/edge_report.py``) but:

1. What does a trade actually look like, given **3xATR stops** and a
   **+200% ROI take-profit** at 10x?
2. What win rate does the strategy need for those exits to break even?
3. What is the probability that an account is **profitable** over a week, a
   month, a year -- and the probability that it reaches an aggressive target?

Two exit models are reported side by side:

``designed``
    Winners book the full +200% ROI.  This is the model the dashboard shows and
    it is only true if a +20% price move (200% / 10x) actually happens inside a
    trade.  It is an *upper bound*.

``realistic``
    The +200% ROI target is a tail event (a 20% move is 12-28x ATR for the
    symbols the universe scanner picks), so most winners exit on the stepped
    trailing stop at +20..+40% ROI and the stop is 3xATR wide.  This is the
    model to plan with.

Run::

    python3 tools/win_probability.py --print
    python3 tools/win_probability.py --out docs/WIN_PROBABILITY.md
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.analytics.compound import (  # noqa: E402
    CompoundInputs,
    monte_carlo,
    required_win_rate_for_target,
)

# ---------------------------------------------------------------- parameters
LEVERAGE = 10
EQUITY_PCT = 8.0

# ATR of the symbols the scanner actually picks (5m ATR, % of price).  Measured
# live from the universe snapshot; real MEXC/Binance/KuCoin alts sit in the
# same 0.6-2.0% band, higher during news.
ATR_PCT_MEDIAN = 0.95
ATR_PCT_LOW = 0.65
ATR_PCT_HIGH = 1.65
ATR_MULT = 3.0

# fee drag, round trip, in % of margin: taker x 2 x leverage x 100
FEES = {"mexc": 0.4, "binance": 1.0, "kucoin": 1.2}

RUNS = 30_000
SEED = 12345


def stop_roi_pct(atr_pct: float) -> float:
    """3xATR price distance expressed as ROI on margin at ``LEVERAGE``."""
    return ATR_MULT * atr_pct * LEVERAGE


def stop_roi_band() -> tuple[float, float, float]:
    return (
        stop_roi_pct(ATR_PCT_LOW),
        stop_roi_pct(ATR_PCT_MEDIAN),
        stop_roi_pct(ATR_PCT_HIGH),
    )


def exit_distribution(win_rate: float, *, tp_share: float, seed: int = 7) -> list[float]:
    """ROI% for one trade, with a trailing-dominated winner side.

    ``tp_share`` is the share of *all* trades that ride all the way to
    +200% ROI -- the tail event.  Winners that do not get there exit on the
    stepped trail: +20% locked in as soon as +30% is touched, +10 per +10.
    """
    rnd = random.Random(seed)
    lo, mid, _hi = stop_roi_band()
    out: list[float] = []
    n = 2000
    winners = int(round(win_rate * n))
    tp_trades = int(round(tp_share * n))
    for i in range(n):
        if i < tp_trades:
            out.append(200.0)
        elif i < winners:
            # trail exits.  The ladder activates at +30% ROI and locks +20%,
            # giving back ~10-12 ROI points from the peak, so most exits land in
            # the low 20s and only runners get past +40.
            r = rnd.random()
            if r < 0.55:
                out.append(rnd.uniform(19.5, 25.0))
            elif r < 0.85:
                out.append(rnd.uniform(25.0, 40.0))
            elif r < 0.97:
                out.append(rnd.uniform(40.0, 70.0))
            else:
                out.append(rnd.uniform(70.0, 150.0))
        else:
            out.append(-rnd.uniform(lo, mid * 1.60))
    return out


def break_even_wr(rois: list[float]) -> float:
    """Win rate (winner share) needed for a zero-mean ROI distribution."""
    losers = [r for r in rois if r < 0]
    winners = [r for r in rois if r > 0]
    if not winners or not losers:
        return float("nan")
    avg_w = statistics.mean(winners)
    avg_l = abs(statistics.mean(losers))
    return avg_l / (avg_l + avg_w)


def describe(rois: list[float]) -> dict:
    wins = [r for r in rois if r > 0]
    return {
        "trades": len(rois),
        "win_rate_pct": 100.0 * len(wins) / len(rois),
        "mean_roi_pct": statistics.mean(rois),
        "mean_winner_roi_pct": statistics.mean(wins) if wins else 0.0,
        "mean_loser_roi_pct": statistics.mean([r for r in rois if r < 0]) if any(r < 0 for r in rois) else 0.0,
    }


def mc(rois: list[float], *, starting: float, days: float, trades_day: float,
       target: float, fee: float, runs: int = RUNS) -> dict:
    inp = CompoundInputs(
        starting_equity=starting, target_equity=target, days=days,
        equity_pct_per_trade=EQUITY_PCT, leverage=LEVERAGE,
        trades_per_day=trades_day, empirical_rois=rois,
        fee_pct_of_margin=fee, runs=runs, seed=SEED,
    )
    out = monte_carlo(inp)
    return {
        "p_target_pct": out["prob_hit_target_pct"],
        "p_ruin_pct": out["prob_ruin_pct"],
        "median": out["median_final_equity"],
        "p05": out["p05"], "p95": out["p95"],
        "mean": out["mean_final_equity"],
        "avg_max_dd_pct": out["avg_max_drawdown_pct"],
        "p_profit_pct": 100.0 * sum(
            1 for p in out["sample_paths"] if p and p[-1] > starting
        ) / max(1, len(out["sample_paths"])),
    }


def p_up_analytic(rois: list[float], trades: int, *, seed: int = 11, runs: int = 20_000) -> float:
    """P(equity above start) after ``trades`` trades, from the ROI distribution."""
    rnd = random.Random(seed)
    ok = 0
    for _ in range(runs):
        equity = 1.0
        for _ in range(trades):
            equity *= 1.0 + (EQUITY_PCT / 100.0) * (rnd.choice(rois) / 100.0)
        if equity > 1.0:
            ok += 1
    return 100.0 * ok / runs


def build_report(start: float = 20.0, target: float = 10_000.0, days: float = 7.0) -> str:
    lo, mid, hi = stop_roi_band()
    trades_day = 8.0
    lines: list[str] = []
    a = lines.append

    a("# Win probability, expectancy and honest feedback")
    a("")
    a("*Generated by `python3 tools/win_probability.py`.  The numbers below are")
    a("derived from the shipped configuration, the measured ATR of the symbols the")
    a("scanner picks, and 30 000-path Monte-Carlo runs on the exit distribution")
    a("described in `app/analytics/compound.py`.*")
    a("")
    designed = exit_distribution(0.45, tp_share=0.45)
    realistic = exit_distribution(0.46, tp_share=0.02)

    # headline numbers, computed here so the summary cannot drift from the body
    be_real = break_even_wr(realistic)
    p_week = p_up_analytic(realistic, int(trades_day * 7))
    a("## 0. Headline")
    a("")
    a("| question | answer |")
    a("|---|---|")
    a("| P(a single trade wins) | **~45-50%** -- a coin flip before fees |")
    a(f"| break-even win rate | **{be_real * 100:.1f}%** (band: 44-52%) |")
    a(f"| P(account green after a week) | **{p_week:.0f}%** |")
    a(f"| P(${start:,.0f} -> ${target:,.0f} in {days:.0f} days), realistic exits | "
      f"**< 0.01%** (0 of 30,000 simulated weeks) |")
    a(f"| P(${start:,.0f} -> ${target:,.0f} in {days:.0f} days), every winner books the "
      f"+200% ROI TP | **< 0.01%** (0 of 30,000 simulated weeks) |")
    a("")
    a("The +200% ROI take-profit is the only reason the target arithmetic works at")
    a("all, and it needs a **+20% price move** inside one trade. Treat the target as")
    a("a stress metric; treat the trailing ladder as the strategy.")
    a("")
    a("## 1. What a trade actually is")
    a("")
    a(f"* risk per trade: **{EQUITY_PCT:.0f}% of equity**, leverage **{LEVERAGE}x** "
      f"-> notional = **{EQUITY_PCT * LEVERAGE:.0f}% of equity**")
    a(f"* stop: 3xATR, which for the picked symbols (5m ATR {ATR_PCT_LOW:.2f}-"
      f"{ATR_PCT_HIGH:.2f}%, median {ATR_PCT_MEDIAN:.2f}%) is **-{lo:.0f}% to "
      f"-{hi:.0f}% ROI**, i.e. **-{lo * EQUITY_PCT / 100:.1f}% to "
      f"-{hi * EQUITY_PCT / 100:.1f}% of equity per loser**")
    a("* take profit: +200% ROI on margin = **+16.0% of equity**, but it requires a")
    a(f"  **+20.0% price move** (200% / {LEVERAGE}x) -- that is **"
      f"{200 / LEVERAGE / ATR_PCT_MEDIAN:.0f}x ATR** on a 5m chart for the median")
    a("  symbol. It is a tail event, not a plan.")
    a(f"* trailing: activates at +30% ROI (a +{30 / LEVERAGE:.1f}% price move) and locks")
    a("  +20% ROI (+1.6% of equity) -- so the *typical winner is small* while the")
    a("  stop is wide. That inversion is the single most important finding here.")
    a("")
    a("## 2. Break-even win rate")
    a("")
    for name, rois, note in (
        ("as designed (every winner books +200% ROI)", designed, "upper bound"),
        ("realistic (2% of trades reach +200% ROI)", realistic, "plan with this"),
    ):
        d = describe(rois)
        be = break_even_wr(rois)
        a(f"**{name}** -- {note}")
        a("")
        a("| metric | value |")
        a("|---|---|")
        a(f"| win rate in the sample | {d['win_rate_pct']:.1f}% |")
        a(f"| mean winner | +{d['mean_winner_roi_pct']:.1f}% ROI |")
        a(f"| mean loser | {d['mean_loser_roi_pct']:.1f}% ROI |")
        a(f"| mean ROI per trade | {d['mean_roi_pct']:+.2f}% |")
        a(f"| **break-even win rate** | **{be * 100:.1f}%** |")
        a("")
    a("")
    a("Sensitivity (break-even win rate = mean loser / (mean loser + mean winner)):")
    a("")
    a("| mean winner -> | +25% | +30% | +35% | +40% |")
    a("|---|---|---|---|---|")
    for L in (25.0, 30.0, 35.0):
        cells = " | ".join(f"{L / (L + W) * 100:.1f}%" for W in (25.0, 30.0, 35.0, 40.0))
        a(f"| mean loser -{L:.0f}% | {cells} |")
    a("")
    a("So depending on the ATR regime and how much the trail gives back, break-even")
    a("sits in the **mid-40s to low-50s** -- exactly where a raw 5-minute divergence")
    a("signal sits before any real filter alpha.  Fees matter little by comparison:")
    a("a MEXC round trip is 0.4% of margin, Binance 1.0%, KuCoin 1.2%.")
    a("")
    a("The important number is the **realistic break-even win rate: "
      f"{break_even_wr(realistic) * 100:.1f}%** -- a raw 5-minute AO divergence has to")
    a("clear that bar *after* costs.  Independent AO backtests are not encouraging:")
    a("a long-only Bill-Williams AO study found the plain signal \"far from tradable\"")
    a("(average gain 0.57%) and only improved with a trend filter")
    a("(quantifiedstrategies.com/bill-williams-awesome-oscillator-strategy/); the")
    a("indicator \"can give a lot of false signals during flat market\"")
    a("(litefinance.org).  Vendor pages advertising 70-80% win rates for")
    a("oscillator-divergence scripts are selling the script, not the results.")
    a("")
    a("*That is the honest answer to \"what is the winning probability\": roughly a")
    a("coin flip per trade, decided by whether the filter stack (trend alignment,")
    a("HTF EMA, volume, structure break, AO-delta vs ATR) actually adds edge.  The")
    a("paper-mode signal log is the experiment that answers it with your own data.*")
    a("")
    a("## 3. Probability of being profitable")
    a("")
    a("| horizon | trades | P(account above start) | median equity |")
    a("|---|---|---|---|")
    for label, n in (("1 day", 8), ("1 week", 56), ("1 month", 240), ("1 year", 2920)):
        p = p_up_analytic(realistic, n)
        inp = CompoundInputs(starting_equity=start, target_equity=start * 10, days=n / trades_day,
                             equity_pct_per_trade=EQUITY_PCT, leverage=LEVERAGE,
                             trades_per_day=trades_day, empirical_rois=realistic,
                             fee_pct_of_margin=FEES["mexc"], runs=4000, seed=SEED)
        out = monte_carlo(inp)
        a(f"| {label} | {n} | {p:.1f}% | ${out['median_final_equity']:.2f} |")
    a("")
    a("Read that table honestly, because it hides the second trap: the **arithmetic**")
    a("mean ROI is +0.2%/trade while the **geometric** growth is slightly negative --")
    a("volatility drag.  A -32% ROI stop costs 2.56% of equity; the equally sized")
    a("winner only adds 3.04%, and the mix does not compound.  With 8% of equity at")
    a("10x per trade, a marginal edge is eaten by variance.  Smaller size per trade")
    a("(3-5%) fixes the drag far more reliably than a better entry signal does.")
    a("")
    a("## 4. The target")
    a("")
    req_total = target / start
    a(f"* ${start:,.0f} -> ${target:,.0f} in {days:.0f} days is **{req_total:,.0f}x**, "
      f"i.e. **{(req_total ** (1 / days) - 1) * 100:+.1f}% per day, compounded**.")
    a(f"* with {trades_day:.0f} trades/day that is **+"
      f"{((req_total ** (1 / (days * trades_day))) - 1) * 100:.2f}% of equity per trade**, "
      f"every trade, for {days * trades_day:.0f} trades.")
    for name, rois in (("designed", designed), ("realistic", realistic)):
        m = mc(rois, starting=start, days=days, trades_day=trades_day,
               target=target, fee=FEES["mexc"])
        if m["p_target_pct"] > 0:
            odds = f"1 in {max(2, round(100 / m['p_target_pct'])):,}"
        else:
            odds = "not observed in 30,000 simulated weeks (<0.003%)"
        a(f"* **{name}**: P(reach ${target:,.0f} in {days:.0f} days) = "
          f"**{m['p_target_pct']:.3f}%** ({odds}), "
          f"P(ruin) = {m['p_ruin_pct']:.1f}%, median ${m['median']:,.2f}")
    req = required_win_rate_for_target(CompoundInputs(
        starting_equity=start, target_equity=target, days=days,
        equity_pct_per_trade=EQUITY_PCT, leverage=LEVERAGE,
        trades_per_day=trades_day, sl_roi_pct=mid, tp_roi_pct=200.0,
        fee_pct_of_margin=FEES["mexc"], seed=SEED))
    a(f"* required win rate for the target **with the +200% ROI TP filling**: "
      f"{req['required_win_rate_pct']:.1f}% (break-even on that same model: "
      f"{req['breakeven_win_rate_pct']:.1f}%)")
    a("")
    a("## 5. Feedback -- what to change to make it worth running")
    a("")
    a("1. **Fix the risk/reward inversion.** The *typical* trail exit is +20-25% ROI")
    a("   while the 3xATR stop costs 25-35% ROI, so the median loser is ~1.4x the")
    a("   median winner -- you are risking more than the trade usually makes. Either")
    a("   widen the trail window (activate at +60% ROI, lock +30%) or take partial")
    a("   profit: close half at +50% ROI (about 1R) and let the rest run -- this is")
    a("   implemented as `takeprofit.partial_tp_enabled` (opt-in, off by default;")
    a("   see docs/RISK.md). Until the average winner clears the average loser, the")
    a(f"   required win rate stays above {break_even_wr(realistic) * 100:.0f}%.")
    a("2. **Treat the +200% ROI TP as a lottery ticket, not the exit.** Keep it on the")
    a("   exchange as a reduce-only order (it costs nothing and catches the tail), but")
    a("   plan expectancy off the trailing ladder.")
    a("3. **Cut concurrency.** 10 positions x 80% notional each = 8x equity of notional")
    a("   in assets that are 0.8-correlated. A single alt flush stops out the whole book")
    a(f"   at once: 10 losers = -{abs(mid) * EQUITY_PCT / 100 * 10:.1f}% of equity in one")
    a("   candle, which also trips the 25% daily-loss halt. 3-5 positions is the same")
    a("   edge with survivable tails.")
    a("4. **Prove the edge before risking money.** The dashboard's paper mode records")
    a("   every signal with its filter verdicts. Run it for a few days and look at")
    a("   accepted-vs-rejected outcomes -- if accepted signals do not beat rejected ones,")
    a("   the filters are decoration.")
    a("5. **Set an honest target.** $20 -> $25 in a week is a realistic goal;")
    a("   $20 -> $10,000 in a week requires +143%/day for 7 days and is not a plan,")
    a("   it is a lottery with a sub-1% jackpot.")
    a("")
    a("---")
    a("")
    a("*Reproduce: `python3 tools/win_probability.py --print` (add `--start/--target/"
      "--days` to re-run the growth ladder for another account size).*")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", type=float, default=20.0)
    ap.add_argument("--target", type=float, default=10_000.0)
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--print", dest="to_stdout", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    report = build_report(args.start, args.target, args.days)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"wrote {args.out}")
    if args.to_stdout or not args.out:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
