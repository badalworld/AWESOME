# Risk model and ROI maths

All thresholds are configurable in the dashboard (Settings → Risk / Take profit / Trailing) and
stored in `data/settings.json`.

## 1. ROI is on margin, not price

With leverage `L`:

```
ROI%   = price_move% × L                       (long:  (price-entry)/entry × 100 × L)
price  = entry × (1 + ROI / (100 × L))         (long target)
price  = entry × (1 - ROI / (100 × L))         (short target)
```

At 10x, a 1% price move is a 10% ROI; the +200% ROI target sits **20% away from entry** in price.
That is intentionally ambitious: only a strong trend runner reaches it, which is exactly why the
stepped trailing stop exists.

## 2. Position sizing (compounding)

```
margin   = equity × equity_per_trade_pct        default 8%
notional = margin × leverage                    default 10x  -> 80% of equity
qty      = floor(notional / (price × contract_size) / vol_unit) × vol_unit
```

* `equity` is the **current** equity, so the position size compounds with the account
  ($1,000 → 8% = $80 margin → $800 notional; after a win → $1,160 → $92.80 margin).
* `available` (free collateral) caps the margin, and `max_total_margin_pct` (80%) caps the sum of
  open position margins.
* Positions whose notional would be below the contract minimum are rejected with a reason
  (`min_notional`), not silently rounded up.
* **Max 10 concurrent positions**, one per symbol, enforced in `RiskGuard.can_open()`.

## 3. Stop-loss = 3 × ATR

```
SL_price = entry ∓ 3 × ATR(14)          (long: minus, short: plus)
SL_ROI%  = (3 × ATR / entry) × 100 × L
```

The ROI is then clamped to `[min_sl_roi_pct, max_sl_roi_pct]` (default 5%..150%) to avoid two
degenerate cases: an ATR so small that normal noise stops you out instantly, and an ATR so large
that one loss destroys the account. The stop is placed **on the exchange** as a fair-price stop
with `priceProtect` (prevents wick-triggered fills), and duplicated as a local watchdog: the bot
market-closes on a stop breach without waiting for the venue's trigger.

## 4. Take profit = +200% ROI

Placed as a **reduce-only limit** order at `entry × (1 ± 200/(100×L))`.

Expected-value arithmetic at 8% margin / 10x:

```
win  (TP hit) : +200% ROI × 8% margin = +16.0% of equity
loss (SL 3ATR): -SL_ROI% × 8% margin  = -0.08 × SL_ROI%  (e.g. -30% ROI -> -2.4%)
```

So the break-even win rate for a 30% ROI stop is `30 / (200 + 30) ≈ 13%`. The dashboard's
**Compounding plan** tab shows the required win rate for the selected equity target and horizon,
plus a Monte-Carlo distribution of outcomes.

## 5. Stepped trailing stop

```
if peak_ROI >= trail_start_roi (30%):
    stop_ROI = floor((peak_ROI - 30) / 10) * 10 + 20
```

Rules:

1. **Monotonic ratchet.** The stop is compared in *price space*: for longs the new stop price must
   be strictly above the old one, for shorts strictly below. A "wider" value can never be applied.
2. **Step-quantised.** An order modification is sent only when `trailing_step_index()` increases —
   a 10% ROI step. Price ticks between steps update local state only (no API spam).
3. **Peak from mark price ticks** (last price / mark), not candle closes.
4. **Guarded against proximity.** If the calculated stop would sit within
   `min_move_bps` of the current mark, it is not applied (avoids instant self-triggering).
5. **Restart-safe.** `peak_roi_pct`, `stop_roi_pct`, `trail_active`, `step_index` and the
   exchange handle are persisted per symbol in SQLite and restored on boot; the exchange-side stop
   is reconciled after a restart.
6. **Never unprotected.** A stop move is a single API call
   (`planorder/change_stop_order` for attached legs). If the modification fails, the previous stop
   order remains live (it is never cancelled first).
7. **Initial stop at activation.** When the trail activates at +30% ROI, the stop jumps to
   +20% ROI (locking a profit) and only then follows the peak upwards.

## 6. Portfolio-level guards

| Guard | Default | Behaviour |
|---|---|---|
| Max concurrent positions | 10 | new signals are queued/rejected with a reason |
| One position per symbol | on | prevents doubling into a losing symbol |
| Max total margin | 80% of equity | sizing is reduced, else the signal is skipped |
| Loss cooldown per symbol | 30 min | stops immediate re-entry after a stop-out |
| Daily loss halt | -25% of the day's start equity | no new entries until the next UTC day or a manual resume |
| Drawdown kill-switch | -40% from peak equity | no new entries; open positions still managed; manual resume |
| Graceful flatten | manual | "Flatten all" closes everything at market with one click |

Halts are never silent — they are shown as badges in the dashboard header and written to the log,
and every rejection reason is stored with the signal.
