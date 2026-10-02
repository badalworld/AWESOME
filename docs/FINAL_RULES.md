# Final TP / trail / SL rules — stated against reality

**Status: rule defaults retained; live readiness NOT certified.** See the
[code-hygiene audit](CODE_HYGIENE_AUDIT_2026-10-02.md) for unresolved order-lifecycle
blockers. Simulator figures below describe synthetic paths, not measured live
strategy performance.

Read this together with [`EDGE_AND_EXPECTANCY.md`](EDGE_AND_EXPECTANCY.md)
(win-probability model).

---

## 1. The rule set (as shipped, as tested)

| Rule | Final value | Config key (`config.toml` → `[risk]`/`[takeprofit]`/`[trailing]`) | Implementation |
|---|---|---|---|
| Fixed take-profit | **+200 % ROI on margin**, closed by the bot with a reduce-only **market** order | `TP_ROI = 200` → `takeprofit.tp_roi_pct` | `app/trade/executor.py` (market close, no resting limit order) |
| Trailing activation | **+30 % ROI** peak | `TRAIL_START_ROI = 30` → `trailing.trail_start_roi` | `app/risk/manager.py:evaluate_trailing` (line 232) |
| Trailing initial stop | **+20 % ROI** | `TRAIL_INITIAL_STOP_ROI = 20` → `trailing.trail_initial_stop_roi` | idem |
| Ratchet step | every **+10 %** of peak ROI, stop moves **+10 %**; monotonic, never loosened | `TRAIL_STEP_ROI = 10`, `TRAIL_STOP_STEP_ROI = 10` | idem — `stop_ROI = 20 + floor((peak_ROI − 30)/10)·10`, peak tracked on mark price, state persisted in the per-venue DB |
| Stop-loss | **3 × ATR** of the 5-minute chart, placed as the venue's trigger-**at-market** reduce-only stop | `ATR_MULTIPLIER = 3.0` → `stoploss.atr_multiplier` | entry order carries `stopLossPrice` (MEXC `priceProtect:1`) / plan stop / `STOP_MARKET` |
| Sizing | **8 % of *running* equity** per trade, **10×** leverage, **max 10** concurrent, total margin ≤ **80 %** of current equity | `risk.equity_per_trade_pct`, `risk.leverage`, `risk.max_open_positions`, `risk.max_total_margin_pct` | `app/risk/manager.py:size_position`, `RiskGuard.can_open` |

At the calibration used everywhere in the docs (ATR = 1.2 % of price on the 5 m
chart) the stop is 3.6 % of price = **36 % ROI on margin**, i.e. a **−2.88 % of
equity** loss per losing trade at 8 % sizing (0.36 × 8 %); the +200 % ROI target
is a **+20 % price move = 16.7 × ATR** and pays **+16 % of equity** (2.0 × 8 %). That is the whole reason the rules are what they are:
a 200 % ROI target is reachable only by a large sustained move, whereas the
trail is what books ordinary winners.

## 2. What the rules actually produce (measured, not assumed)

`tools/rule_sim.py` runs the **real** exit code (`evaluate_trailing`,
`stop_price_from_roi`) over martingale GBM paths calibrated to 5 m alt
volatility. Two families were run; a difference smaller than ±1 % ROI is noise.

8 000 paths per cell, 10 s ticks, 4 h max hold, ATR 1.2 %/5 m, SL 36 % ROI, 10×
(mean ROI on margin ± standard error):

| Rule set | 0 bps drift | 30 bps favourable drift |
|---|---|---|
| **shipped: TP 200 %, trail 30/20/10/10** | **+0.11 % ± 0.40** (P(win) 53.8 %) | **+1.13 % ± 0.40** (P(win) 55.5 %) |
| TP 200 %, trail 40/25/10/10 | +0.44 % ± 0.45 (47.9 %) | +1.86 % ± 0.46 (49.6 %) |
| TP 200 %, trail 45/30 + partial 50 % at +50 | +0.32 % ± 0.45 (45.5 %) | +1.78 % ± 0.45 (47.4 %) |
| TP 200 %, **no trail** (stop 36 % only) | −0.03 % ± 0.55 (39.2 %) | +2.12 % ± 0.56 (41.0 %) |

Second family (2 seeds × 1 200 paths, 5 s ticks) agrees within the same error
bars: shipped −0.31 % / +0.50 % / +1.27 % at expected drift 0 / 25 / 50 bps;
"pure 200 %/36 %" −0.69 % / +0.88 % / **+2.86 %**.

**Read this correctly:**

1. **All rule geometries measure ≈ 0 % at zero drift.** The rules do not create
   an edge; they redistribute it. Whether the bot makes money is decided by the
   win rate of the AO-divergence signal, not by the exit numbers.
2. **The average shipped winner is smaller than the average loser.** Measured
   mean win **+29.6 % ROI** vs mean loss **−35.7 % ROI** (8 000-run sweep), giving
   a **break-even win rate of 54.6 %** — the bot must win *more than half* of its
   trades just to stand still. Exit mix over a 4 h horizon: **44 % stopped /
   52 % trailed / 4 % timed out**.
3. **The +200 % TP is a lottery ticket, not the plan.** It needs 16.7 ATR of
   favourable travel and fires on 0–4 % of simulated trades (21 × ATR for the median 0.95 %-ATR symbol, see `WIN_PROBABILITY.md`). It is kept because
   it costs nothing and caps an outlier; it is not what makes the equity curve.
4. **Geometries that let winners run need a lower win rate** — TP 90 % / trail
   40/20/10/10 measures mean win +38.5 % vs mean loss −35.1 %, break-even win
   rate **47.6 %**; TP 120 % / trail 50/25 + partial 50 % at +50 % → +45.0 %/−34.4 %,
   break-even **43.3 %**. Those are the sets to move to **if** live data shows a
   win rate in the 45–50 % band (see §4).
5. **The numbers exclude the venue fee drag** except where stated: 0.4 %/round
   trip on MEXC, 1.0 % on Binance, 1.2 % on KuCoin (as a share of margin). The
   shipped geometry's measured ±0.1–1 % per trade is therefore **underwater on
   Binance and KuCoin until the signal demonstrates real edge**.

### Reconciling the three break-even numbers in this repo

| Source | Assumed mean winner | Break-even WR | Why |
|---|---|---|---|
| `EDGE_AND_EXPECTANCY.md` §3 (parametric ladder) | +69 % ROI | 42.3 % | assumes the trail captures 20–120 % ROI on a winner — **optimistic** |
| `WIN_PROBABILITY.md` §2 (Monte-Carlo, 2 % of trades reach the TP) | +38.3 % ROI | 45.7 % | assumes a wider capture than the code measures |
| **`tools/rule_sim.py` (runs the real `evaluate_trailing`)** | **+29.6 % ROI** | **54.6 %** | what the shipped 30/20 ladder actually produces |

Plan with the measured 54.6 %, or switch to the wider ladder in §4 (~47.6 %). The other two
figures stay in the docs because they bracket what a *better* exit distribution would buy you —
they are the reason the recommendation is "widen the trail", not "the TP is wrong".

> Trap to remember: an earlier draft of the simulator used a zero **log**-drift
> walk, which is not a martingale — the Itô term `exp(σ²/2)` manufactured a
> **+1.5–2.5 % ROI "edge" out of nothing** at every rule set. The fix
> (`drift = −½σ²`) is in `simulate_path`; never quote a number measured before it.

## 3. Why these values and not "better" ones

* The 200 % TP was kept for exactly one reason: it is the target you specified,
  it is honest as an upper bound, and removing it would not change expectancy
  (rule 3 above). Lowering it raises the hit rate and lowers the payoff — a
  wash at zero drift, and *worse* if the signal has real drift.
* The trail is the money path; the 30/20 geometry was kept because every
  measured alternative is inside the noise band, and 30/20 is the one covered by
  the 151-test suite. **Do not re-tune these numbers on the simulator** — it
  cannot resolve differences below ~1 % ROI. Only realised trades can.
* The 3 × ATR stop is the only rule that is load-bearing for survival: its nominal loss at the 1.2%-ATR calibration is ≈ 2.88% of equity before costs.
  A trigger-market stop does not guarantee that loss cap.

## 4. If you want the geometry with the lowest bar (optional, one line)

Reality says the shipped trail has a sub-1 payoff ratio (needs > 54.6 % wins).
The geometry that needs the *least* skill from the signal — and the one I would
switch to if the live win rate prints in the 45–50 % band — is:

```toml
[takeprofit]
tp_roi_pct = 90          # was 200
[trailing]
trail_start_roi = 40     # was 30
trail_initial_stop_roi = 25
```

Break-even win rate **47.6 %** instead of 54.6 %, mean win ≈ +38 % vs mean loss
≈ −35 %. Trade-off: fewer winning trades (P(win) ≈ 48 % vs 54 %), longer holds,
and the 47.6 % bar is measured on paths, not on your signal. Do not change it
mid-flight — decide before the first live trade and let the realised `roi_pct`
list from `tools/edge_report.py` adjudicate after ≥ 30 closed trades.

## 5. Risk you are accepting by switching this on

* **Concurrency dominates the tail.** 10 positions × 8 % margin at 10× = 80 % of
  equity deployed as margin, i.e. **8× equity of notional**. A correlated
  alt-coin flush of −8 % against the book is ≈ **−64 % of equity** before the
  protective exits fill. The 40 % drawdown halt and 25 % daily-loss halt block
  further entries; they do not guarantee an existing book is liquidated at those
  thresholds. Keep them on, but do not treat them as portfolio-loss caps.
* **The stop is trigger-at-market, not a guaranteed price.** It is the only
  resting order the bot is allowed to leave (market-only policy). A gap through
  the trigger fills at the next available market price — the −36 % ROI figure is
  the *design* loss, not a floor.
* With 8× notional at 10× leverage, an adverse move of roughly −10 % against a
  single position reaches that position's liquidation before the stop could
  help. The 3 × ATR stop is designed to be far inside that, but it is not a
  guarantee against a venue-side liquidation in a fast market.

## 6. Compounding is verified (8 % of *running* equity, 10 trades, 10×)

The sizing path reads the broker's current account under the per-venue entry lock:

```
TradingEngine._try_open(signal)
  → Executor.open_from_signal(signal)
    → under entry lock: broker.account(), broker.positions()
    → combine exchange exposure with locally managed exposure
    → RiskGuard.update_equity(current equity)
    → size_position(current equity, current available funds, contract leverage)
    → RiskGuard.can_open(actual sizing_margin, exposure count, used margin)
```

The equity loop still supplies dashboard snapshots, but those cached values are
no longer the entry sizing source. Profits increase the next size; losses shrink
it. Contract rounding, exchange minimums, available funds and existing sizing
overshoot rules can change actual margin from the nominal 8%. The gate projects
current used margin plus the actual proposed margin against the 80% cap and
counts unmanaged exchange positions too. Fees and unrealized P&L affect the
broker-reported running equity. Concurrent entries are serialized per venue;
independent venues do not share balances or margin headroom.

## 7. Verdict

**Keep paper mode pending the order-lifecycle fixes in the
[latest audit](CODE_HYGIENE_AUDIT_2026-10-02.md).** This supersedes the earlier
unconditional go-live recommendation. Tests validate specified code behavior, not
real-exchange acceptance or profitability. No measured live win rate is available;
the Monte Carlo figures in this document cannot establish an AO-divergence edge.
