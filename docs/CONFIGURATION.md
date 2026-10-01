# Configuration reference

Precedence (highest wins):

1. `data/settings.json` — written by the dashboard, validated, atomic
2. `config.toml` — the shipped defaults
3. code defaults

`POST /api/settings/reset` (Settings → *Reset group*) deletes the override and falls back to
`config.toml`. Updates and resets validate the combined configuration before
persisting it. Invalid runtime keys are rejected. Integer settings reject fractional
values; boolean settings reject unrecognized values rather than guessing.

Venue overrides (`venues.<id>.*`) take precedence over global settings. Each venue
has its own `mode`, credentials, balances and database; the common strategy/risk
values are inherited unless explicitly overridden. File-loaded configuration still
needs operator review: runtime validation is not a full startup schema validator.

Config paths (`data_dir`, `persistence.db_path`) are resolved **relative to the config file's
directory**, so `AO_CONFIG=/tmp/exp/config.toml python3 run.py` is fully isolated (separate DB,
separate overrides). This matters for testing and for running paper + live instances side by side.

## `[app]`

| Key | Default | Meaning |
|---|---|---|
| `mode` | `paper` | `paper` \| `live`. Switching requires an engine restart (dashboard does it for you). |
| `data_dir` | `data` | runtime directory: DB, settings, machine key |
| `log_level` | `INFO` | console/dashboard logging level (applied at process startup) |

Risk-day boundaries use UTC; the dashboard formats dates in the browser’s timezone.

## `[exchange]`

| Key | Default | Meaning |
|---|---|---|
| `rest_base` | `https://api.mexc.com` | REST base |
| `ws_url` | `wss://contract.mexc.com/edge` | market-data / user-data socket |
| `paper_data_source` | `auto` | `auto` \| `live` \| `synthetic` — `auto` uses live public market data from the venue when reachable, otherwise the simulator |
| `recv_window_ms` | `5000` | signed-request tolerance |
| `http2` | `true` | HTTP/2 keep-alive pooling |
| `max_connections` / `keepalive_expiry` | `20` / `300` | pool size / idle lifetime |
| `request_timeout_s` | `5` | per-request timeout |
| `retry_attempts` | `3` | adapter retry limit; not proof of fill finality |
| `set_leverage_on_entry` | `true` | set leverage before entry; reject when setup fails |

## `[risk]`

| Key | Default | Meaning |
|---|---|---|
| `equity_per_trade_pct` | `8.0` | margin per trade as % of current equity |
| `leverage` | `10` | ROI multiplier; also set on the exchange per symbol |
| `max_open_positions` | `10` | hard concurrency cap |
| `max_total_margin_pct` | `80` | cap on summed margin |
| `min_notional_usd` | `5` | venue minimum notional |
| `max_margin_usd` | `0` | hard cap on margin per trade (`0` = no cap) |
| `max_drawdown_halt_pct` | `40` | kill-switch from peak equity |
| `max_daily_loss_pct` | `25` | daily loss halt |
| `cooldown_after_loss_min` | `30` | per-symbol re-entry cooldown after a loss |

## `[account]`

`paper_starting_equity = 20.0` sets simulated starting equity. Venue overrides may
set a different paper balance. Live sizing uses the broker-reported running equity.

## `[stoploss]`

| Key | Default | Meaning |
|---|---|---|
| `atr_period` | `14` | ATR lookback |
| `atr_multiplier` | `3.0` | stop distance = 3 × ATR |
| `min_sl_roi_pct` | `5` | clamp (too-tight stops) |
| `max_sl_roi_pct` | `150` | clamp (too-wide stops) |
| `local_watchdog` | `true` | bot-side stop enforcement in addition to the exchange order |

## `[takeprofit]`

| Key | Default | Meaning |
|---|---|---|
| `tp_roi_pct` | `200` | target ROI on margin — enforced **locally** with a reduce-only market close, never as a resting limit order |
| `partial_tp_enabled` | `false` | bank part of the position at a nearer target (opt-in) |
| `partial_tp_roi_pct` | `50` | ROI on margin for that slice (5-300) |
| `partial_tp_fraction` | `0.5` | fraction of the position closed there (0.1-0.9) |

## `[trailing]`

| Key | Default | Meaning |
|---|---|---|
| `trail_start_roi` | `30` | ROI at which trailing activates |
| `trail_initial_stop_roi` | `20` | stop locked in at activation |
| `trail_step_roi` | `10` | peak-ROI increment that unlocks a new step |
| `trail_stop_step_roi` | `10` | stop-ROI increment per step |
| `min_move_bps` | `2` | minimum price distance before a stop move is sent |
| `step_only_updates` | `true` | update only when a new ladder step is reached |
| `ratchet_only` | `true` | never loosen an established stop |

Mark-price peaks and SQLite persistence are always used; they are not toggles.

Formula: `stop_ROI = floor((peak_ROI - 30)/10)*10 + 20` for `peak_ROI ≥ 30`, ratchet-only.

## `[strategy]`

| Key | Default | Meaning |
|---|---|---|
| `timeframe` | `Min5` | entry timeframe |
| `ao_fast` / `ao_slow` | `5` / `34` | AO periods |
| `pivot_k` | `2` | bars on each side of a confirmed pivot |
| `lookback_bars` | `90` | divergence search window |
| `min_pivot_gap` / `max_pivot_gap` | `3` / `45` | pivot spacing bounds |
| `min_ao_delta_atr` | `0.12` | minimum AO displacement (in ATR) |
| `require_ao_extreme` | `true` | AO below/above zero at the second pivot |
| `require_trigger_break` | `true` | wait for the structure break |
| `allow_hidden_divergence` | `false` | hidden (continuation) divergences |
| `signal_cooldown_bars` | `6` | per-symbol re-signal cooldown |
| `min_signal_score` | `60` | composite quality gate |

## `[filters.*]`

See the table in the README. Each group has an `enabled` flag, and every threshold is per-signal
recorded so you can audit why a divergence was skipped.

## `[universe]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | run new scans (disabled retains the last selected watchlist) |
| `max_symbols` | `20` | watchlist size |
| `refresh_sec` | `300` | rescan cadence |
| `min_turnover_24h_usd` | `5e6` | hard liquidity floor |
| `[universe.weights]` (`turnover`, `volatility`, `momentum`) | 0.4/0.4/0.2 | composite ranking |

Stage two enriches up to 45 candidates; this is currently a code limit, not a setting.

## `[persistence]`

| Key | Default | Meaning |
|---|---|---|
| `db_path` | `data/bot.db` | legacy migration source; current databases are `data/venues/<id>.db` |
| `equity_snapshot_sec` | `30` | balance snapshot cadence |
| `log_ring_size` | `500` | in-memory dashboard log capacity |

## `[web]`

| Key | Default | Meaning |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8080` | dashboard bind |
| `api_token` | empty | protects REST data/control and WebSocket access; set for production |

The health endpoint and dashboard static assets remain public. Use TLS and network
restrictions; do not expose a tokenless dashboard to untrusted users.

## `[target]`

| Key | Default | Meaning |
|---|---|---|
| `equity_target` | `10000` | planning target for the compounding tab |
| `days` | `7` | horizon |
| `monte_carlo_runs` | `20000` | simulated planning paths; not a measured strategy win probability |


## Removed no-op settings (October 2026)

`risk.max_positions_per_symbol`, `risk.risk_recalc_interval_s`,
`stoploss.use_mark_price_trigger`, `trailing.use_mark_price_for_peak`,
`trailing.persist_state`, `trailing.replace_stop_on_step`, and `target.compounding`
never controlled the claimed runtime behavior and are no longer offered by the
settings API. Existing persisted values are inert; runtime files are not rewritten
by this cleanup. One position per symbol, running-equity sizing, mark triggers,
mark peaks and persistence remain fixed behaviors. Stop replacement is selected by
the venue adapter, not a toggle. `account.set_leverage_on_entry` was misplaced;
the working setting is `exchange.set_leverage_on_entry`.

Unused metadata (`app.name`, `app.timezone`, `exchange.name`, `account.quote_asset`)
and the inert `exchange.latency_telemetry` flag were removed from shipped defaults.
Venues are selected by their IDs; the scanner uses USDT contracts and latency
measurement remains enabled.
