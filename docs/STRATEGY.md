# Strategy — AO divergence on 5-minute futures

## 1. The indicator

```
MEDIAN_i = (high_i + low_i) / 2
AO_i     = SMA5(MEDIAN) - SMA34(MEDIAN)
```

The Awesome Oscillator is a zero-centred momentum histogram: positive when the recent median
price is above its 34-bar baseline, negative below. Its *shape* (peaks and troughs), not its
sign, carries the divergence information.

Warm-up requirement: **34 + pivot_k bars** before the first pivot can be confirmed.
The AO series is `len(candles) - (ao_slow - 1)` long — never index it by candle index without
the offset (the code does this correctly via `offset = len(candles) - len(ao)`).

## 2. Regular divergence

A **regular bullish divergence** exists when, within a bounded window:

1. price makes a **lower low**: `low[p2] < low[p1] - epsilon_price`
2. AO makes a **higher low**: `ao[p2] > ao[p1] + min_ao_delta_atr * ATR`
3. (default) AO at the newer pivot is **below zero** — the setup must occur in potentially
   oversold territory, not in a strong uptrend dip
4. both pivots are **confirmed fractals** (`pivot_k` bars on each side) — no repainting
5. `min_gap <= p2 - p1 <= max_gap` (default 3..45 bars)
6. `bars_since_p2 <= max_bars_since_pivot` (default 8) — the pattern must be fresh

Bearish is the mirror: higher high in price, lower high in AO (above zero), triggered by a break
of the intervening low.

**Hidden (continuation) divergences** — higher low price + lower low AO — are implemented but
**disabled by default**: on 5m crypto they fire far too often and mix badly with a mean-reversion
entry.

## 3. The trigger (this is what removes most fake signals)

A divergence is a *condition*, not a signal. The bot waits for the market to confirm:

* long: a **closed** candle whose high > the intervening swing high (or > `max(high[p2], high[p2-1])`
  when no clear swing exists) **and** whose close is above the previous close
* short: mirror with lows

Waiting costs a few percent of the move but eliminates the "divergence that keeps diverging while
the trend continues" failure mode. The trigger candle's volume must additionally exceed
`min_volume_mult` × SMA20 (volume filter).

## 4. Scoring (0–100)

`app/strategy/divergence.py:score_divergence()` combines:

| Component | Weight | Rationale |
|---|---|---|
| AO displacement vs ATR | 30% | a real divergence has real momentum displacement |
| Pivot spacing / structure quality | 20% | clean, well-separated swings are more reliable |
| Freshness | 15% | the newer the second pivot, the higher the follow-through odds |
| Depth of the price extreme | 15% | meaningful new lows (>0.2 ATR) matter more |
| Trigger strength (close beyond structure) | 20% | confirms participation |

The filter pipeline then computes a *separate* composite quality score (see below) and only
signals scoring ≥ `min_signal_score` (default 60) are routed to execution.

## 5. Which symbols

`app/strategy/universe.py` runs a two-stage scan every `universe.refresh_sec` (default 60s, with bounded parallel enrichment):

1. **Stage 1 — cheap ticker scan.** Every USDT-M contract is scored on 24h turnover
   (liquidity), 24h range and 24h move, with hard gates on minimum turnover, spread proxies and
   contract state.
2. **Stage 2 — candle scan** of the top `universe.scan_candidates` symbols: fetch 5m candles,
   compute ATR% and ATR percentile, ADX, recent momentum, and the current spread/depth.
   The final score blends `turnover_pct`, `atr_pct` percentile, `momentum` and `adx` with
   configurable weights, and the top `universe.max_symbols` (default 20, but never more than
   `max_open_positions` matter) become the active watchlist.

The watchlist is recomputed live and shown on the **Universe** tab, with the rejected symbols and
the exact reason each was dropped.

## 6. Known failure modes (and the mitigation)

| Failure mode | Mitigation |
|---|---|
| Divergence in a strong trend keeps failing | `trend_alignment` (EMA200) + `htf_ema`/`htf_ao` (15m) filters |
| Divergence forms during a news candle; entry gets wicked | `shock_candle` filter (max 4× ATR) |
| Divergence in dead chop | `adx_trend` (≥15) + volatility percentile |
| Divergence nobody traded | `volume_confirmation` (≥1.15×) |
| Entry at a terrible price | `spread` (≤12 bps) + `book_depth` (≥5× notional) |
| The pattern is already 20 bars old | `max_bars_since_pivot` + freshness scoring |
| Overfitting to one param set | all thresholds live-configurable and logged per signal for ex-post review |

## 7. Improvement ideas (not yet implemented)

* Funding-rate filter (avoid entering against crowded funding) — endpoint already mapped.
* Open-interest surge confirmation.
* Order-book imbalance at the trigger candle close.
* Walk-forward re-fitting of `min_ao_delta_atr` / `min_signal_score` per symbol class.
* Multi-timeframe divergence stacking (5m signal + 1m micro-trigger) for a tighter stop.
