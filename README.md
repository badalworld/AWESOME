# AWESOME — Awesome-Oscillator Divergence Futures Bot (MEXC)

An event-driven, low-latency trading system for **MEXC USDT-M perpetual futures** that trades
**Awesome-Oscillator (AO) divergences on the 5-minute chart** on the market's **most volatile
liquid contracts**, with a full **anti-fake-signal filter stack**, **ATR×3 stop-loss**,
**+200% ROI take profit**, a **stepped trailing stop**, and **8%-of-equity compounding sizing**
towards a configurable equity target.

The dashboard (FastAPI + vanilla JS, no build step) provides real-time balance tracking,
trade history, live performance metrics, an equity curve, universe scanner output, signal
explanations (why a signal was taken or skipped), latency telemetry, and a settings panel
where you enter your MEXC API key.

> ⚠️ **Risk warning.** Leveraged futures trading can lose your entire balance. A $1,000 → $10,000
> target in 7 days is an extremely aggressive objective: the built-in Monte-Carlo model tells you
> the *probability* of reaching it with your actual parameters (see the **Compounding plan** tab)
> — it is a planning tool, not a promise. Always start in **paper mode** and validate with your
> own data before risking capital. Nothing here is financial advice.

---

## Table of contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [The strategy](#the-strategy)
- [Anti-fake-signal filters](#anti-fake-signal-filters)
- [Risk model: sizing, SL, TP, trailing](#risk-model)
- [MEXC integration & low-latency design](#mexc-integration--low-latency-design)
- [The dashboard](#the-dashboard)
- [Configuration](#configuration)
- [Going live safely](#going-live-safely)
- [Testing](#testing)
- [Project layout](#project-layout)
- [FAQ / troubleshooting](#faq--troubleshooting)

---

## What it does

| Requirement | Implementation |
|---|---|
| AO divergence on 5m | `app/strategy/divergence.py` — fractal pivots + AO displacement, entry only on confirmed structure break |
| Most volatile assets | `app/strategy/universe.py` — two-stage scan (ticker → candles) scoring turnover × realised 5m volatility × momentum |
| Avoid fake signals | `app/strategy/filters.py` — 8 independent filters + composite quality score, all persisted and shown |
| Real MEXC execution | `app/exchange/mexc.py` + `app/exchange/live.py` — signed REST + WSS, attached SL/TP, reduce-only exits |
| Low-latency routing | HTTP/2 keep-alive pool, time-sync offset, idempotent `externalOid`, priority lanes for exits, coalesced tick dispatch, p50/p95/p99 telemetry |
| ATR×3 stop-loss per transaction | `app/risk/manager.py:build_plan()` — `entry ∓ 3 × ATR(14)`, clamped to sane ROI bounds, exchange-side + local watchdog |
| TP = +200% ROI on margin | `ROI = price_move% × leverage`; price = `entry × (1 ± 200/(100×10))`, placed as a reduce-only limit |
| Stepped trailing stop | `trailing_stop_roi(peak) = floor((peak−30)/10)×10 + 20` — activate at +30%, lock +20%, +10% per +10%, ratchet-only, step-quantised, restart-safe |
| 8% of equity, 10x, max 10 positions | `size_position()` + `RiskGuard` (margin cap, daily-loss halt, drawdown kill-switch, per-symbol cooldown) |
| Compounding to a target | `app/analytics/compound.py` — Monte-Carlo + required-win-rate maths, live on the dashboard |
| Dashboard: balance, history, metrics | `app/web/` — WebSocket live feed, equity curve, ROI distribution, trade table, latency panel |
| API key input in dashboard | Settings tab → encrypted (AES-256-GCM) storage, live verification against MEXC |

---

## Quick start

```bash
# 1. install (Python 3.11+)
pip install -r requirements.txt

# 2. run — starts the engine + dashboard
python3 run.py                 # http://localhost:8080

# optional flags
python3 run.py --port 9000
python3 run.py --no-engine     # dashboard only, start the engine from the UI
AO_CONFIG=/path/to/config.toml python3 run.py
```

Out of the box the bot starts in **paper mode**. If `api.mexc.com` is reachable it paper-trades on
**live MEXC market data**; otherwise (offline/CI) it uses the built-in **synthetic volatile-market
simulator** so every part of the system is still exercisable — the dashboard clearly labels which
source is active (`MEXC public data` vs `SIMULATED DATA`).

To trade live:

1. Create a MEXC API key with **futures order** permission enabled (IP-restrict it!).
2. Open the dashboard → **Settings** → paste **API key** + **secret** → *Save & verify*.
3. Set **Mode = live** → *Apply* (this restarts the engine in live mode).
4. Watch the first trade end-to-end; start with a small balance while you build confidence.

---

## Architecture

```
                 ┌──────────────────────────── dashboard (FastAPI + WS) ───────────────────────────┐
                 │  Overview · Positions · Trades · Signals · Universe · Compounding · Settings   │
                 └───────────────▲──────────────────────────────▲─────────────────────────────────┘
                                 │ REST /ws                     │ control
   ┌─────────────────────────────┴──────────────────────────────┴───────────────────────────────┐
   │                                   TradingEngine (asyncio)                                  │
   │  universe loop → SignalEngine → RiskGuard → Executor → position/trailing manager           │
   │  equity loop · position-sync loop · time-sync loop · maintenance                           │
   └───────▲───────────────────────────────▲──────────────────────────────▲─────────────────────┘
           │ klines / ticks                │ orders                       │ fills, positions
   ┌───────┴───────────────┐   ┌───────────┴──────────────┐   ┌───────────┴───────────────────┐
   │  Market data          │   │  Broker abstraction      │   │  Persistence                  │
   │  MeXC REST + WSS      │   │  LiveBroker (real)       │   │  SQLite (WAL): trades, signals │
   │  or SyntheticFeed     │   │  PaperBroker (simulated) │   │  equity curve, orders, kv      │
   └───────────────────────┘   └──────────────────────────┘   └───────────────────────────────┘
```

Everything trading-related is broker-agnostic: **paper and live share the identical strategy,
risk, trailing and execution code paths**, so paper results are meaningful for the code you will
run live.

---

## The strategy

**Awesome Oscillator** (5m): `AO = SMA5((H+L)/2) − SMA34((H+L)/2)`

**Regular bullish divergence** — price prints a **lower low**, AO prints a **higher low**
(AO must be below zero at the second pivot by default).
**Regular bearish divergence** — mirror image. Hidden (continuation) divergences exist as an
option but are **off by default** because they are far noisier intraday.

Details that matter:

- **Confirmed pivots only.** A pivot needs `pivot_k` (default 2) bars on each side, so a signal is
  never repainted retroactively.
- **Distance and displacement requirements.** The two pivots must be 3–45 bars apart, the AO
  displacement must exceed `min_ao_delta_atr × ATR` (default 0.12) — this rejects "flat" divergences.
- **Trigger, not anticipation.** A divergence alone does *not* open a position. Entry requires a
  **closed candle breaking the local structure** (the high between the two lows for longs), i.e.
  the market must prove the reversal started. This single rule removes most fake divergences.
- **Freshness window.** The second pivot must be within `max_bars_since_pivot` (default 8 bars);
  stale patterns are ignored.
- **Higher-timeframe context.** 15m (and 60m) AO/EMA are computed to confirm direction.
- Scores 0–100, ranking candidates when more signals exist than free slots.

## Anti-fake-signal filters

Each filter is independently configurable and its value/threshold is stored with every signal, so
the dashboard can explain *exactly* why a divergence was traded or skipped.

| Filter | Default | What it kills |
|---|---|---|
| `volatility_band` | ATR% 5m ∈ [0.12, 4.0] | dead chop (no follow-through) **and** news chaos |
| `volatility_percentile` | ATR ≥ p25 of last 200 bars | entering during a volatility contraction |
| `trend_alignment` | close vs EMA200 (`ema`/`ema_stack`) | counter-trend divergences in a strong trend |
| `trend_slope` | optional | fading a steeply trending market |
| `htf_ema`, `htf_ao` | 15m EMA + AO turning | 5m noise fighting the higher timeframe |
| `volume_confirmation` | trigger volume ≥ 1.15× SMA20 | divergences nobody traded |
| `liquidity_turnover` | 24h turnover ≥ $5M | illiquid symbols with random prints |
| `volume_climax` | optional ≥1.5× | (adds) capitulation-style reversal evidence |
| `rsi_extreme` | longs RSI ≤ 62 / shorts ≥ 38 | buying an already-exhausted move |
| `macd_flip` | histogram must turn | momentum not actually shifting |
| `adx_trend` | ADX ≥ 15 | range-bound chop producing endless fake pivots |
| `spread` | ≤ 12 bps | wide/stale books where fills are terrible |
| `book_depth` | top-5 depth ≥ 5× order notional | guaranteed slippage on exit |
| `shock_candle` | last candle ≤ 4× ATR | entering right after an impulse/news candle |
| `quality_score` | ≥ 60 | weak composites get skipped even if every gate passed |

Tune any of them live in **Settings → Strategy & anti-fake-signal filters**. Loosening increases
trade frequency and fake-signal rate; tightening reduces both. The **Signals** tab shows the
rejection reason for every candidate, so you can tune with evidence.

## Risk model

**Sizing (compounding)** — every trade is sized from *current* equity:

```
margin   = equity × equity_per_trade_pct                (default 8%)
notional = margin × leverage                            (default 10x → 80% of equity)
qty      = floor(notional / (price × contract_size) / vol_unit) × vol_unit
```

**ROI is on margin, not price** (exactly as specified):

```
ROI%   = (price − entry)/entry × 100 × leverage      (long)
ROI%   = (entry − price)/entry × 100 × leverage      (short)
price  = entry × (1 + ROI/(100×leverage))            (long target)
price  = entry × (1 − ROI/(100×leverage))            (short target)
```

**Stop-loss (B0)** — `entry ∓ 3 × ATR(14)`, i.e. `SL_ROI% = (3×ATR/entry)×100×leverage`,
clamped to `[min_sl_roi_pct, max_sl_roi_pct]`. Placed **on the exchange** (fair-price trigger,
`priceProtect` enabled to avoid wick-triggered stops) *and* watched locally: if price breaches the
stop before the exchange triggers, the bot market-closes immediately (fast lane).

**Take profit (B1)** — fixed `+200% ROI` as a **reduce-only limit order** on the exchange.

**Stepped trailing stop (B2)**

```
if peak_ROI <  30:  initial ATR stop stands
else:               stop_ROI = floor((peak_ROI − 30)/10) × 10 + 20
```

| peak ROI | stop ROI |
|---|---|
| 30% | 20% |
| 40% | 30% |
| 50% | 40% |
| 100% | 90% |
| 200% | TP hit |

- **Ratchet-only**: judged in price space, the stop can only move in the profit direction.
- **Step-quantised updates**: an order modification happens only when a new 10% step is reached
  (no API spam).
- **Peak from mark price ticks**, not candle closes (so wicks count).
- **Single-call modification** (`planorder/change_stop_order` for attached legs,
  `planorder/change_price` for standalone plan orders) — the position is never left unprotected
  during a stop move.
- **Restart-safe**: peak ROI, active stop level and step index are persisted in SQLite and
  restored (and repaired against the exchange) on boot.

**Portfolio guard** — max 10 concurrent positions, one per symbol, ≤80% equity as margin,
per-symbol cooldown after a loss (default 30 min), daily-loss halt (`-25%`), drawdown kill-switch
(`-40%` from peak equity). Halts are visible on the dashboard and one click to clear.

## MEXC integration & low-latency design

Verified against the official MEXC futures docs:

| Purpose | Endpoint |
|---|---|
| Contracts / tickers / candles | `GET /api/v1/contract/detail`, `/contract/ticker`, `/contract/kline/{symbol}` |
| Mark price | `GET /api/v1/contract/fair_price/{symbol}` |
| Account / positions | `GET /api/v1/private/account/assets`, `/private/position/open_positions` |
| Entry (with attached SL/TP) | `POST /api/v1/private/order/create` (`stopLossPrice`, `takeProfitPrice`, `lossTrend`, `profitTrend`, `priceProtect`, `externalOid`) |
| Stop modification (trailing) | `POST /api/v1/private/planorder/change_stop_order` |
| Standalone stop | `POST /api/v1/private/planorder/place/v2` → modify `planorder/change_price`, cancel `planorder/cancel` |
| Leverage | `POST /api/v1/private/position/change_leverage` |
| Fill/order state | `GET /api/v1/private/order/get/{orderId}`, `/order/external/{symbol}/{oid}`, `stoporder/open_orders` |

Auth: `ApiKey` + `Request-Time` + `Signature` headers where
`Signature = HMAC-SHA256(secret, accessKey + timestamp + paramString)`
(GET: sorted `k=v` pairs joined by `&`; POST: the exact JSON body string).

**Latency engineering**

- One long-lived **HTTP/2** connection pool with keep-alive — no DNS/TLS work on the order path.
- **Priority lanes**: order operations, private queries and public scans have separate token
  buckets, so an exit is never queued behind a universe scan.
- **Clock offset** measured against the exchange and reused for signing (removes a whole class of
  timestamp rejections).
- **Idempotency** via `externalOid` — a retried order can never double-fill.
- **Attached protection**: the stop/target ride along with the entry order, so the position is
  protected from the instant it fills (with an automatic fallback to standalone orders if the
  venue rejects attachments for that order type).
- **Tick coalescing**: mark-price ticks are collapsed per symbol and dispatched to a dedicated
  risk loop; the newest price always wins, older ones are dropped instead of queued.
- **Telemetry**: every request's latency is recorded → p50/p95/p99 shown live on the dashboard.

## The dashboard

- **Overview** — equity, realized PnL, win rate, expectancy, open positions, latency percentiles,
  equity curve, ROI distribution, risk/guard state, broker & connectivity diagnostics, activity feed.
- **Positions** — live ROI, peak ROI, current stop, trailing status, distance to TP, margin, PnL,
  one-click close.
- **Trade history** — entry/exit, peak ROI, PnL, fees, exit reason (`take_profit`, `stop_loss`,
  `trail`, `manual`, `exchange-sync`), CSV export.
- **Signals** — every candidate with score, ATR%, status and the *reason* it was rejected.
- **Universe** — the live volatility ranking (score, turnover, 24h range, 5m ATR%, ADX, spread)
  plus a sample of rejected symbols and why.
- **Compounding plan** — required daily growth, Monte-Carlo outcome distribution
  (P(hit target), P(ruin), percentiles), simulated equity paths and a win-rate sensitivity table.
- **Settings** — API keys (encrypted, never returned), mode switching, and every risk/strategy/
  filter/universe parameter, plus paper-account reset and engine restart.
- **Logs** — live log tail with levels.

## Configuration

`config.toml` holds the defaults; anything changed in the dashboard is written to
`data/settings.json` and survives restarts (`Config.reset()` restores defaults). Every value is
validated on write (type + range + cross-field rules), so a typo in the UI cannot reach the order
router. Key knobs:

```toml
[risk]        equity_per_trade_pct = 8.0   # per-trade margin, % of current equity
              leverage = 10                # ROI = price_move% x leverage
              max_open_positions = 10
[stoploss]    atr_multiplier = 3.0          # SL = entry -/+ 3 x ATR(14)
[takeprofit]  tp_roi_pct = 200              # +200% ROI on margin
[trailing]    trail_start_roi = 30          # activate trailing here
              trail_initial_stop_roi = 20   # lock this in at activation
              trail_step_roi = 10           # per +10% ROI of peak...
              trail_stop_step_roi = 10      # ...raise the stop by +10% ROI
[target]      equity_target = 10000.0       # planning target
              days = 7
```

## Going live safely

1. Run paper mode for at least a few days and read the **Signals** tab: are the filters rejecting
   things you would also reject? Tune until the accepted signals look right to you.
2. Check the **Compounding plan** tab: what does the Monte-Carlo say about *your* parameters?
   If P(hit target) is tiny, the honest answer is that the target is unrealistic at this risk
   level — increase the horizon, lower the target, or accept the variance.
3. Add API keys with **IP whitelist** + futures-order permission only (no withdrawals).
4. Set `web.api_token` in `config.toml` if the dashboard is reachable from anywhere but localhost.
5. Start live with a small balance; verify one full trade (entry, SL/TP placement, a trailing step,
   exit) before scaling up.
6. Run the bot on a VPS geographically close to MEXC's matching engine for the best latency, and
   keep `data/` on persistent storage (it contains your trade history and trailing state).

## Testing

```bash
python3 tests/run_all.py -v          # 70 tests, ~16 s, no network needed
python3 -m unittest tests.test_core         # maths, indicators, strategy, filters, analytics
python3 -m unittest tests.test_integration  # trade lifecycle on a deterministic market
python3 -m unittest tests.test_engine       # the orchestrator on the offline synthetic feed
python3 -m unittest tests.test_api          # every dashboard endpoint + websocket
```

Coverage highlights: ROI↔price conversions (long/short, TP *and* stop sides), the exact trailing
formula (30→20, 40→30, 50→40, 100→90) with its ratchet/step rules, ATR×3 stop construction
(including that a stop is always an adverse move — a regression test for the short-stop sign bug),
TP at +200% ROI, 8%-of-equity sizing with contract rounding and equity compounding, divergence
invariants (lower low + higher AO low, confirmed pivots), every anti-fake filter's rejection path,
the full trade lifecycle (entry → trailing steps → stop-out → booking), restart/state restore,
paper-broker accounting (fees, PnL), config validation and isolation, the risk guard (cooldowns,
drawdown halt, position caps) and the whole HTTP/WebSocket surface.

Tests are fully isolated: each one builds a throwaway config + data directory, so the suite can
never pick up (or corrupt) whatever you have configured in the live dashboard.

## Project layout

```
config.toml               all tunables (defaults; dashboard overrides go to data/settings.json)
run.py                    entry point: engine + dashboard
app/
  config.py               layered config + validation
  crypto.py keystore.py   AES-256-GCM credential storage (machine-bound key, 0600)
  db.py                   SQLite (WAL): trades, signals, equity, orders, kv state
  engine.py               orchestrator: loops, subscriptions, state for the UI
  exchange/
    base.py               broker interface + shared types
    mexc.py               REST + WSS client: signing, rate lanes, retries, latency telemetry
    live.py               LiveBroker: real orders, attached protection, single-call stop moves
    paper.py              PaperBroker: simulated fills/fees/stops on real or synthetic prices
    synthetic.py          volatile-market simulator (offline development)
  strategy/
    indicators.py         AO, ATR, EMA, RSI, MACD, ADX, Bollinger, fractal pivots
    features.py           per-candle feature extraction (+ higher timeframe context)
    divergence.py         AO divergence detection + trigger confirmation
    filters.py            the anti-fake-signal pipeline + composite score
    universe.py           two-stage volatility/liquidity scanner
    signals.py            signal engine (analysis → Signal)
  risk/manager.py         sizing, SL/TP plan, stepped trailing, portfolio guard
  trade/executor.py       order routing, fill confirmation, trailing, watchdog, reconciliation
  analytics/              metrics (win rate, expectancy, DD) + compounding Monte-Carlo
  web/                    FastAPI app + static dashboard (index.html, app.js, styles.css)
tests/                    unit + integration suite
docs/                     strategy, risk, API and deployment notes
```

## FAQ / troubleshooting

**"MEXC public data unavailable → falling back to the synthetic feed"**
The sandbox/host cannot reach `api.mexc.com`. Paper mode still runs fully (simulated prices).
Live mode requires real connectivity — run it from a VPS.

**No trades for hours.** By design: the AO divergence + trigger + 8 filters combination is
selective. Check the **Signals** tab — if candidates are being rejected, the reasons are listed.
Loosen the filters that reject for reasons you disagree with, and watch the paper results.

**A trade opened but the stop looks further away than 3×ATR.** The stop is clamped to
`[min_sl_roi_pct, max_sl_roi_pct]` (default 5–150% ROI) so a tiny ATR can't produce a stop that
gets wicked out instantly. Adjust in Settings → Risk.

**Do I need to keep the bot running for the trailing stop to work?** The initial SL and the
+200% TP are live on the exchange, so you are protected even if the bot dies. The *trailing*
stop and peak tracking resume automatically on restart from persisted state.

**Where are my keys stored?** Encrypted with AES-256-GCM in `data/bot.db`; the machine key lives in
`data/.secrets/machine.key` (mode 0600). Keys are never returned by the API (only a masked preview)
and never logged.
