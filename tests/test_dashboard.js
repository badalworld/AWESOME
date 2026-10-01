/* Offline dashboard lifecycle regression tests. Run: node --test tests/test_dashboard.js */
'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function dashboard() {
  const sockets = [], timers = new Map(), intervals = [];
  let seq = 0;
  class Socket {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; if (this.onclose) this.onclose(); }
  }
  const ctx = vm.createContext({
    console, WebSocket: Socket,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { protocol: 'https:', host: 'dashboard.example' },
    window: { addEventListener() {} },
    document: { querySelector: () => null, querySelectorAll: () => [] },
    setInterval: fn => intervals.push(fn),
    setTimeout: fn => { const id = ++seq; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
  });
  const source = fs.readFileSync(path.join(__dirname, '../app/web/static/app.js'), 'utf8');
  // Evaluate actual client functions without starting the browser-only boot sequence.
  vm.runInContext(source.slice(0, source.indexOf('(async function boot()')), ctx);
  vm.runInContext('renderAll = payload => { state.lastState = payload; };', ctx);
  return { ctx, sockets, timers, intervals, run: code => vm.runInContext(code, ctx) };
}

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
