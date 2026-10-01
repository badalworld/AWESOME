# Code hygiene and operational safety audit — 2026-10-02

## Verdict

**Live-trading decision: NO — do not arm live order routing on this checkout yet.**
Keep all venues in paper mode until the blockers below are implemented and verified.
The cleanup/hardening pass is complete, but this report does not certify live deployment
and supersedes the earlier unconditional “go for live trading” statement.
Passing unit/integration tests does not prove exchange acceptance, fill finality,
profitability, or recovery from every failure. No real orders were placed during
this audit, no credentials were changed, and no venue was switched to live mode.

**Latest verification: 240 Python tests and 11 JavaScript tests passed, zero failures/errors/skips.** Static checks (`pyflakes`, Vulture, `compileall`, `node --check`) and `git diff --check` also pass. These checks do not certify live exchange acceptance or recovery.

## Scope and method

- Inventory of the tracked runtime, tests, tools, configuration and documentation.
- Whole-Python-tree static analysis with Pyflakes, compileall and Vulture.
- Targeted manual review of order entry/close, risk sizing, exchange adapters,
  engine lifecycle, settings persistence, authentication and dashboard data routes.
- Reference checks before deleting suspected dead code; existing tests preserved.
- Deterministic paper-broker, mocked failure/concurrency and HTTP/WebSocket tests.
- This is not a claim that every line is defect-free, a dependency vulnerability
  assessment, or a live certification of the three exchanges' current APIs.

## Removed or consolidated

| Item | Reason |
|---|---|
| `MeXCPublicMarketAdapter` | Unreferenced legacy adapter; all venues already use `VenuePublicMarketAdapter`. Removed 87 lines of obsolete MEXC-only parsing/lifecycle code. |
| `ORDER_IOC` | No call sites; obsolete after market-only execution. |
| `round_to_step` | No call sites. Actual venue rounding paths retained. |
| `VenueConfig.venue_dict` | Unused convenience wrapper. |
| `VenueManager.credentials_view` | Unused wrapper; dashboard uses the active masked-credentials path. |
| Duplicate protection-arm branches | Same call, differing only in attached entry-order identifier; collapsed without removing attached protection. |
| Per-route HTTP authentication calls | Replaced by one HTTP authentication boundary covering both legacy and venue-scoped routes. |
| Duplicate single/batch settings mutation | `set` delegates to the validated, atomic `set_many` path. |
| Caller-supplied entry account snapshots | Removed from the internal executor interface and all in-tree callers. |
| Seven no-op runtime settings | Removed validators/defaults and the two exposed UI toggles; fixed behavior remains unchanged. Details in `CONFIGURATION.md`. |
| Inert metadata/telemetry settings | Removed unused name/timezone/quote-asset defaults and the nonfunctional telemetry switch. |
| Misplaced leverage setting | Moved the shipped default from `account.set_leverage_on_entry` to the actual `exchange.set_leverage_on_entry` reader. |
| Per-connection dashboard polling timers | Removed unbounded timer accumulation on every WebSocket reconnect. |
| Requested-size/reference-price fill fallback | Removed: a timeout is not a fill. |
| 99.9%-filled heuristic | Removed: near-complete active orders remain partial; cancellation is terminal even with a partial quantity. |
| Unused HTTP client in synthetic mode | No longer constructed. |
| Unused engine lock / restart delay | Replaced with an actual lifecycle lock; removed the arbitrary 300ms restart sleep. |
| Untracked restart tasks | Replaced by manager-owned, coalesced tasks drained on shutdown. |

Vulture's remaining low-confidence findings include decorated FastAPI handlers,
SQLite/Uvicorn framework attributes, and serialized dataclass fields. These are
not safe deletions merely because no direct Python attribute read was found.
No runtime databases, encrypted credentials, legacy migration support or active
paper/test infrastructure were deleted.

## Defects fixed

### 1. Entry sizing and portfolio limits

Previously the engine could pass a cached equity snapshot and pre-lock position
counts into the executor. Now the executor queries the broker's account and open
positions **inside its per-venue entry lock**, immediately before sizing/gating.

- Running equity, available funds and current drawdown checks are refreshed.
- Exchange-held positions, including unmanaged symbols, consume position slots
  and margin; the same symbol cannot be opened twice.
- Locally managed positions supplement lagging exchange snapshots, preventing
  consecutive entries from reusing the same apparent margin headroom.
- The portfolio gate uses **actual sized margin**, not notional divided by the
  configured leverage. This matters when a contract caps leverage below 10×.
- Missing contract specifications, failed leverage setup, below-minimum leverage,
  invalid numeric inputs and invalid contract geometry reject entry.
- NaN/infinite account/price data cannot silently pass comparison checks or poison
  trailing state. Non-finite equity updates halt risk without setting baselines.

The default 8% running-equity sizing, 10× leverage, 10-position cap and 80% margin
cap are unchanged. Order rounding, available balance, exchange minimums and the
existing sizing overshoot allowance mean actual margin is not always exactly 8%.
The pre-order margin gate is not a guarantee against post-fill slippage, equity
changes or other clients trading the same account.

### 2. Close acknowledgement is not proof of a flat position

After a successful close acknowledgement, the executor now verifies that the
exchange reports the position flat before cancelling the protective stop and
booking a full close. If still open or the query fails, it keeps the position and
stop. The retry path also resolves the exit price and no longer double-counts
latency. This is a conservative safeguard, not complete partial-fill accounting.

### 3. Dashboard authentication and token disclosure

Previously read routes were public even with `web.api_token` configured, and
`settings.effective` included the plaintext token despite the separately masked
configuration view. Settings updates could also echo/log the token.

- All `/api/` data/control routes now require the configured token, except the
  public health endpoint and CORS preflight. Static dashboard assets remain public.
- Settings views and update responses redact global and scoped API tokens.
- Settings logs contain changed key names, not their values.
- HTTP and WebSocket checks use constant-time token comparison.
- History pagination rejects negative and excessively large limits.
- Explicit CORS origins are whitespace-trimmed.

**If an earlier dashboard was reachable by others, rotate its dashboard token.**
This finding concerns `web.api_token`; it is not evidence that exchange API
secrets were disclosed. An empty dashboard token still means unauthenticated
paper access; production requires authentication and network restrictions.

### 4. Atomic configuration semantics

Batch changes previously bypassed the cross-field checks in single-key updates.
Both now validate the proposed combined configuration, including venue overrides:
AO fast < slow, universe minimum ATR ≤ maximum, and filter minimum ATR ≤ maximum.
A file-write failure rolls back the in-memory settings for both updates and resets.
Partial resets validate the final combined configuration before publication; an
empty reset list is correctly reported as a no-op. Malformed reset request bodies
return HTTP 400 instead of accidentally erasing all overrides. Valid paired changes
are checked together rather than rejected against an intermediate state.
Integer knobs reject fractional values, and unrecognized boolean inputs are
rejected instead of silently becoming true or false. Startup now validates every
runtime-tunable key and filter/venue override against the same schema, checks cross-field
relations, validates the connection/storage settings that are not dashboard-editable, and
fails closed on malformed JSON, non-object overrides or stale unknown override keys. This is
not a schema for arbitrary unknown TOML sections; deployment-specific extensions remain a
separate concern.

### 5. Clock synchronization

The synchronization helper is called at response receipt. The request midpoint
is therefore local receipt time **minus** half the round-trip time, not plus.
Corrected the sign and added deterministic zero-skew/positive-skew tests.

### 6. Shutdown and tick-task lifecycle

- `asyncio.CancelledError` is explicitly handled while awaiting the cancelled
  server watcher, allowing engine/database cleanup to run on normal server exit.
- In-flight engine entries are shielded from worker cancellation; engine shutdown
  waits for entry completion/protection before closing the broker.
- Tick bursts use one tracked worker per symbol instead of spawning one task per
  tick just to discover another worker is busy.
- Shutdown drains tracked tick workers before closing exchange clients.
- Peak/trough ROI is captured before tick coalescing, so intermediate highs/lows
  are not lost while a stop update is awaiting REST I/O.

Forced process termination still requires exchange-side stops and durable recovery.
Graceful shutdown is not protection against machine failure or SIGKILL.

### 7. Dashboard connection and venue-switch isolation

A socket reconnect previously created another permanent polling interval; closing
an old venue socket also scheduled a fresh connection. There is now one active
socket and a tracked reconnect timer. Deliberate closes detach callbacks and
cancel reconnects. REST settings/state/history/log responses carry a venue epoch:
late responses are discarded even when the user switches A → B → A. Old socket
messages cannot render into the active page. Eleven offline Node tests exercise the
actual client functions, route mapping, delayed responses and controlled socket/timer mocks;
these are not full browser end-to-end tests.

### 8. Paper-only controls and documentation drift

Paper reset now checks the **actual broker mode**, not only the editable config
mode. Changing settings from live to paper without a restart cannot use this
endpoint to mutate a still-live broker's bookkeeping. Nonpositive/non-finite or
invalid reset balances are rejected before mutation. Full synchronization of paper
reset with open positions, in-flight entries and executor state remains follow-up
work; don't reset an active book.

The configuration reference had obsolete/nonexistent key names, defaults and
features. It now matches the reviewed configuration surface, documents removed
no-op switches, and distinguishes hard-coded behaviors from tunable settings.
Existing runtime databases/settings were not rewritten. Execution comments no
longer promise that an acknowledgement establishes a fill or active protection.

### 9. Second pass: persisted entry intent and uncertainty handling

`execution.pending_entry` in each venue's SQLite KV store records the client ID,
symbol, side, requested size, reference price, leverage, intended stop and lifecycle
phase **before** submission. A failed initial write prevents the order. After
submission, uncertain outcomes retain this record and halt that venue.

- No requested quantity/reference price is substituted after fill timeout.
- Active partial fills no longer complete the fill future; terminal canceled
  partials with a valid positive quantity/price are managed at their actual size.
- Fill futures are registered before submission, so a private-stream fill arriving
  before the REST acknowledgement is not lost. Pending futures are cleaned up.
- Terminal fills require positive finite price and quantity. Protection handles
  require a valid trigger price and the relevant exchange identifier (paper handles
  are only accepted by the paper broker).
- Order telemetry writes occur after protection, not before it; failure to record
  telemetry cannot prevent an attempt to place the stop.
- Protection exceptions/invalid handles halt trading and attempt an emergency
  market close **only using an observed terminal filled quantity**, never a guessed
  requested quantity. The incident records `flat_observed`, `not_confirmed_flat`,
  or `unknown` after querying positions. A close acknowledgement is not called flat.
- Once protection is established, a later bookkeeping failure retains that stop
  and the journal instead of initiating another blind close.
- The marker is removed only after normal trade/state/signal persistence succeeds.
  A crash between writes may conservatively leave a marker even for a valid trade.
- Restart and new-entry paths inspect the persisted marker. Dashboard risk resume
  refuses to clear it; direct risk-guard resume still cannot bypass the entry gate.
  Corrupt marker contents also fail closed. The authenticated state API includes
  `pending_entry` for diagnosis. Normal risk monitoring of managed positions continues.
- Restored and orphan positions now require a verified active stop to be treated as
  protected. A missing/failed stop repair writes a durable `execution.recovery_incident`
  barrier; both dashboard resume and direct guard resume are unable to re-enable entries.
  Orphan recovery no longer substitutes 1% of entry price for missing candle ATR: it
  adopts a real exchange stop if available, otherwise records the position with unknown
  ATR/stop and latches the incident.

This is a **persisted safety barrier**, not a complete automatic reconciler. In
particular, an uncertain or still-active partial entry can leave exposure whose
final size is not known. It is NOT automatically retried or declared protected.
No blanket emergency close is sent using a requested quantity. Manual venue-side
reconciliation is still necessary; keep paper mode pending full automated recovery.
SQLite commits are subject to the configured database/storage durability; they are
not a guarantee against storage corruption or all machine-failure scenarios.

### 10. Second pass: startup, restart and shutdown lifecycle

One per-engine lifecycle lock now serializes start, stop and the full restart
sequence. Failed/canceled startup attempts close an assigned broker. A live-client
probe failure/cancellation closes the client even before a broker exists; canceled
public-data probes also close their client. Synthetic mode no longer constructs an
unused public HTTP client.

The manager owns dashboard restart tasks, coalesces repeated requests per venue,
records failures and removes completed tasks. Shutdown stops accepting requests and
drains already accepted restarts before stopping engines and closing databases.
Restart requests during shutdown return HTTP 503, not a false success response.
Shutdown itself is an owned, shielded task: canceling the server watcher does not
cancel a pending restart or tear down cleanup midway, and the server's finalizer
can await the same cleanup task without stopping clients twice.
Graceful lifecycle coordination still does not protect against forced termination.

### 11. Premium dashboard and addressable pages

The dashboard now uses a lower-glare graphite/emerald workspace theme, responsive page heading,
clearer active-venue context, visible “live readiness not cleared” notice, and focus/reduced-motion
states. The compounding target moved off the global header into the strategy/planning page so
operations data stays focused. Navigation is split into direct-loadable routes for overview,
positions, trade history, signals, markets, strategy/plan, settings and logs. Browser back/forward
updates the active view; unknown dashboard page routes return 404. Venue state remains scoped by
the existing venue selector. This is a set of routes over one shared dashboard shell, not separate
services or separate authentication domains.

## Remaining live-deployment blockers

Keep paper mode until these are implemented and verified:

1. **Automatic reconciliation of uncertain entries:** the old fabricated-fill
   fallback is removed, but the new journal deliberately blocks instead of trying
   to guess a recovery. Authoritative order/fill lookup, outstanding-remainder
   cancellation where appropriate, late-fill detection and protection of unknown
   exposure still need a durable per-order recovery state machine.
2. **Incident resolution and interrupted bookkeeping:** restored/orphan protection repair
   now verifies the stop and latches a non-bypassable durable recovery incident on failure,
   but there is no authenticated resolution workflow or atomic transaction spanning the
   exchange and local persistence. A protected entry whose trade write failed still needs
   reconciliation before reactivation. Recovery/adoption needs exchange-acceptance testing
   for opposite-side/hedged exposure and stale snapshots; an operator must reconcile and
   clear the persisted incident only after comparing the venue state and local records.
3. **Partial-fill and external-exit accounting:** residual quantities, partial-close
   fees, delayed fills and external exits still need reconciliation from authoritative
   venue fills. Some paths estimate exit prices. Optional partial TP remains off.
4. **Authenticated venue acceptance/recovery testing:** tests exercise mocked
   responses and paper fills, not real venue acknowledgements, disconnects,
   stop replacement failures and restarts with outstanding orders.

### When an entry incident is reported

Pause new trading and retain the venue database/logs. Use the journal's client ID
and order ID to establish the authoritative order status, every executed fill,
remaining order quantity, actual exposure and active stop orders on that venue.
Manage any exposure from the venue as necessary. Reconcile the bot's trade/state
records, fees and P&L against that evidence before clearing an incident or resuming.
**Do not simply delete `execution.pending_entry` to make the bot trade again.**
A flat snapshot alone cannot prove that an outstanding entry will not fill later.
No dashboard "dismiss incident" button was added because that would bypass the
unimplemented reconciliation checks.

Other follow-up areas: full schema validation for arbitrary deployment-specific TOML extensions,
paper-reset synchronization, dependency auditing, and exchange-specific API compatibility.
Bounded startup validation is implemented, but it is not a complete schema for every possible
static configuration extension. No strategy win probability or financial return was established.

## Reproduction

```bash
python3 tests/run_all.py
node --test tests/test_dashboard.js
python3 -m pyflakes app tools tests run.py
python3 -m vulture app tools run.py --min-confidence 80
python3 -m compileall -q app tools tests run.py
node --check app/web/static/app.js
git diff --check
```

Regression coverage includes startup configuration validation in `tests/test_hygiene.py`
(38 tests), nine HTTP/WebSocket cases in `tests/test_api.py`,
`tests/test_entry_journal.py` (26 tests), `tests/test_lifecycle.py` (10 tests), and
`tests/test_dashboard.js` (11 tests). Exchange/order tests remain mocked or paper-only;
no production orders are submitted by the test suite.
