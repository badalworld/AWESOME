/* Offline dashboard lifecycle regression tests. Run: node --test tests/test_dashboard.js */
'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function dashboard() {
  const sockets = [], timers = new Map(), intervals = [], elements = new Map();
  let seq = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      id, textContent: '', className: '', style: {},
      classList: {
        values: new Set(),
        add(...names) { names.forEach(name => this.values.add(name)); },
        remove(...names) { names.forEach(name => this.values.delete(name)); },
      },
    });
    return elements.get(id);
  };
  class Socket {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; if (this.onclose) this.onclose(); }
  }
  const ctx = vm.createContext({
    console, WebSocket: Socket,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { protocol: 'https:', host: 'dashboard.example' },
    window: { addEventListener() {} },
    document: { querySelector: () => null, querySelectorAll: () => [], getElementById: element },
    setInterval: fn => intervals.push(fn),
    setTimeout: fn => { const id = ++seq; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
  });
  const source = fs.readFileSync(path.join(__dirname, '../app/web/static/app.js'), 'utf8');
  // Evaluate actual client functions without starting the browser-only boot sequence.
  vm.runInContext(source.slice(0, source.indexOf('(async function boot()')), ctx);
  vm.runInContext('renderAll = payload => { state.lastState = payload; };', ctx);
  return { ctx, sockets, timers, intervals, elements, run: code => vm.runInContext(code, ctx) };
}

test('dashboard sections map to deep-linkable page routes', () => {
  const d = dashboard();
  assert.equal(d.run("dashboardPageUrl('settings')"), '/dashboard/settings');
  assert.equal(d.run("dashboardPageFromPath('/dashboard/markets')"), 'universe');
  assert.equal(d.run("dashboardPageFromPath('/dashboard/strategy')"), 'plan');
  assert.equal(d.run("dashboardPageFromPath('/unknown')"), 'overview');
});

test('repeated connections replace sockets without accumulating polling intervals', () => {
  const d = dashboard(), before = d.intervals.length;
  for (let i = 0; i < 10; i++) d.run('connectWS()');
  assert.equal(d.intervals.length, before);
  assert.equal(d.sockets.filter(s => !s.closed).length, 1);
  assert.equal(d.timers.size, 0);
  assert.equal(d.sockets[0].onclose, null);
});

test('late messages and close callbacks from old sockets are ignored', () => {
  const d = dashboard();
  d.run('connectWS()');
  const oldMessage = d.sockets[0].onmessage, oldClose = d.sockets[0].onclose;
  d.run("state.venue = 'binance'; state.venueEpoch++; connectWS()");
  oldMessage({ data: '{"venue":"mexc"}' });
  oldClose();
  assert.equal(d.run('state.lastState'), null);
  assert.equal(d.timers.size, 0);
  d.sockets[1].onmessage({ data: '{"venue":"binance"}' });
  assert.equal(d.run('state.lastState.venue'), 'binance');
});

test('tab changes cancel pending reconnects', () => {
  const d = dashboard();
  d.run('connectWS()');
  d.sockets[0].close();
  assert.equal(d.timers.size, 1);
  d.run("state.venue = 'kucoin'; state.venueEpoch++; connectWS()");
  assert.equal(d.timers.size, 0);
  assert.equal(d.sockets.length, 2);
});

for (const [fn, response, value] of [
  ['pollState', { venue: 'mexc' }, 'state.lastState'],
  ['pollExtras', { trades: ['old'], curve: ['old'] }, 'state.trades.length'],
  ['pollLogs', { logs: [{ seq: 50 }] }, 'state.logSeq'],
  ['loadSettings', { config: { old: true } }, 'state.config'],
]) {
  test(`${fn} discards responses after A to B to A tab switches`, async () => {
    const d = dashboard();
    const pending = [];
    d.ctx.api = () => new Promise(resolve => pending.push(resolve));
    const before = d.run(value);
    const request = d.run(`${fn}()`);
    d.run("state.venue = 'binance'; state.venueEpoch++; state.venue = 'mexc'; state.venueEpoch++;");
    pending.forEach(resolve => resolve(response));
    await request;
    assert.equal(d.run(value), before);
  });
}

test('operations pulse distinguishes real public market data from a private exchange socket', () => {
  const d = dashboard();
  d.run("state.effective = { 'market_data.max_ws_stale_sec': 10 }");
  d.ctx.renderOps({
    state: {
      engine: { running: true, mode: 'paper', trading_enabled: true, market_data: 'public', market_data_age_s: 0.5, market_tick_count: 12 },
      broker_diagnostics: {
        ws: { connected: true },
        api_usage: { requests_last_minute: 5, rate_limit_hits_last_minute: 0, quota: { utilization_pct: 95, remaining: 5, limit: 100, age_s: 0 } },
      },
      universe_scan: { started_at: Date.now() / 1000 - 5, duration_ms: 420, scan_count: 1, selected_count: 10, candidate_count: 40 },
      risk: { halted: false },
    },
    metrics: { order_latency: { count: 0 } },
  });
  assert.equal(d.elements.get('apiQuotaValue').textContent, '95.0%');
  assert.equal(d.elements.get('apiQuotaState').textContent, 'NEAR LIMIT');
  assert.equal(d.elements.get('feedState').textContent, 'PUBLIC');
  assert.match(d.elements.get('feedDetail').textContent, /public market polling/);
  assert.doesNotMatch(d.elements.get('feedDetail').textContent, /socket connected/);
  assert.equal(d.elements.get('execState').textContent, 'PAPER');
  assert.match(d.elements.get('execDetail').textContent, /live order routing disabled/);
});

test('operations pulse leaves usage-only quotas percentage-unreported', () => {
  const d = dashboard();
  d.ctx.renderOps({
    state: {
      engine: { running: false, mode: 'paper', trading_enabled: false },
      broker_diagnostics: {
        api_usage: { requests_last_minute: 3, rate_limit_hits_last_minute: 0, quota: { used: 340, limit: null, utilization_pct: null, source: 'x-mbx-used-weight-1m' } },
      },
      risk: { halted: false },
    },
    metrics: { order_latency: { count: 0 } },
  });
  assert.equal(d.elements.get('apiQuotaValue').textContent, '340 units');
  assert.equal(d.elements.get('apiQuotaState').textContent, 'USAGE ONLY');
  assert.match(d.elements.get('apiQuotaDetail').textContent, /limit not reported/);
});

test('reconnect interrupted during REST fallback cannot reopen the old venue socket', async () => {
  const d = dashboard();
  let resolve;
  d.ctx.api = () => new Promise(r => { resolve = r; });
  d.run('connectWS()');
  d.sockets[0].close();
  const reconnect = [...d.timers.values()][0]();
  d.run("state.venue = 'binance'; state.venueEpoch++; connectWS()");
  resolve({ venue: 'mexc' });
  await reconnect;
  assert.equal(d.sockets.length, 2);
  assert.equal(d.run('state.lastState'), null);
});
