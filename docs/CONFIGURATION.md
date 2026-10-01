# Configuration reference

Precedence (highest wins):

1. `data/settings.json` — written by the dashboard, validated, atomic
2. `config.toml` — the shipped defaults
3. code defaults

`POST /api/settings/reset` (Settings → *Reset group*) deletes the override and falls back to
`config.toml`. Unknown keys are rejected with `KeyError` — this is deliberate: a typo must never
silently disable a risk limit.

Config paths (`data_dir`, `persistence.db_path`) are resolved **relative to the config file's
directory**, so `AO_CONFIG=/tmp/exp/config.toml python3 run.py` is fully isolated (separate DB,
separate overrides). This matters for testing and for running paper + live instances side by side.

## `[app]`

| Key | Default | Meaning |
|---|---|---|
| `mode` | `paper` | `paper` \| `live`. Switching requires an engine restart (dashboard does it for you). |
| `data_dir` | `data` | runtime directory: DB, settings, machine key |
| `timezone_offset_hours` | `0` | only affects displayed "day" boundaries |

## `[exchange]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `mexc` | broker implementation |
| `base_url` | `https://api.mexc.com` | REST base |
| `ws_url` | `wss://contract.mexc.com/edge` | market-data / user-data socket |
| `paper_data_source` | `auto` | `auto` \| `live` \| `synthetic` — `auto` uses live public market data from the venue when reachable, otherwise the simulator |
| `recv_window_ms` | `60000` | signed-request tolerance |
| `http2` | `true` | HTTP/2 keep-alive pooling |
| `max_connections` / `keepalive_expiry` | `20` / `30` | pool size / idle lifetime |
| `timeout_seconds` | `8` | per-request timeout |
| `max_retries` | `3` | idempotent retries (exits get the fastest lane) |

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
| `loss_cooldown_minutes` | `30` | per-symbol re-entry cooldown after a loss |
| `paper_starting_equity` | `1000` | paper account starting balance |

## `[stoploss]`

| Key | Default | Meaning |
|---|---|---|
| `atr_period` | `14` | ATR lookback |
| `atr_multiplier` | `3.0` | stop distance = 3 × ATR |
| `min_sl_roi_pct` | `5` | clamp (too-tight stops) |
| `max_sl_roi_pct` | `150` | clamp (too-wide stops) |
| `use_fair_price` | `true` | trigger on fair/mark price, not last |
| `price_protect` | `true` | venue's anti-wick protection |
| `local_watchdog` | `true` | bot-side stop enforcement in addition to the exchange order |
| `breakeven_at_roi` | `0` | optional: move stop to break-even at this ROI (0 = off) |

## `[takeprofit]`

| Key | Default | Meaning |
|---|---|---|
| `tp_roi_pct` | `200` | target ROI on margin |
| `close_remainder_on_tp` | `true` | flatten any residual volume after a partial TP |
| `count_partials_as_win` | `true` | stats classification |
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
| `min_move_bps` | `8` | minimum price distance before a stop move is sent |
| `update_mode` | `step` | `step` = only on a new 10% step (no API spam) |
| `use_mark_price` | `true` | peak tracking source |

Formula: `stop_ROI = floor((peak_ROI - 30)/10)*10 + 20` for `peak_ROI ≥ 30`, ratchet-only.

## `[strategy]`

| Key | Default | Meaning |
|---|---|---|
| `timeframe` | `Min5` | entry timeframe |
| `htf_timeframe` | `Min15` | higher timeframe context |
| `ao_fast` / `ao_slow` | `5` / `34` | AO periods |
| `pivot_k` | `2` | bars on each side of a confirmed pivot |
| `lookback_bars` | `90` | divergence search window |
| `min_gap` / `max_gap` | `3` / `45` | pivot spacing bounds |
| `min_ao_delta_atr` | `0.12` | minimum AO displacement (in ATR) |
| `require_ao_extreme` | `true` | AO below/above zero at the second pivot |
| `require_trigger_break` | `true` | wait for the structure break |
| `allow_hidden` | `false` | hidden (continuation) divergences |
| `max_bars_since_pivot` | `8` | freshness window |
| `cooldown_bars` | `6` | per-symbol re-signal cooldown |
| `min_signal_score` | `60` | composite quality gate |

## `[filters.*]`

See the table in the README. Each group has an `enabled` flag, and every threshold is per-signal
recorded so you can audit why a divergence was skipped.

## `[universe]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | use the scanner (otherwise the static symbol list) |
| `max_symbols` | `20` | watchlist size |
| `rescan_seconds` | `300` | rescan cadence |
| `min_turnover_24h_usd` | `5e6` | hard liquidity floor |
| `scan_candidates` | `40` | stage-2 depth |
| weights (`weight_turnover`, `weight_atr_pct`, `weight_momentum`, `weight_adx`) | 0.35/0.30/0.20/0.15 | composite ranking |

## `[persistence]`

| Key | Default | Meaning |
|---|---|---|
| `db_path` | `data/bot.db` | SQLite database (WAL) |
| `equity_snapshot_seconds` | `60` | curve resolution |
| `housekeeping_hours` | `6` | old-candle/log pruning cadence |
| `retain_days` | `30` | history retention |

## `[web]`

| Key | Default | Meaning |
|---|---|---|
| `host` / `port` | `0.0.0.0` / `8080` | dashboard bind |
| `api_token` | empty | optional `X-API-Token` requirement (set it if the dashboard is public) |
| `log_ring_capacity` | `2000` | in-memory log lines kept for the UI |

## `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `level` | `INFO` | `DEBUG`\|`INFO`\|`WARNING`\|`ERROR` |
| `file` | `data/bot.log` | rotating file (also mirrored into the dashboard) |
| `json` | `false` | structured logs |

## `[target]`

| Key | Default | Meaning |
|---|---|---|
| `equity_target` | `10000` | planning target for the compounding tab |
| `days` | `7` | horizon |
| `mc_paths` / `mc_horizon_trades` | `2000` / `400` | Monte-Carlo resolution |
