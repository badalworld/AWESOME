/* =========================================================================
   AO Divergence Bot — dashboard client
   Vanilla JS, no build step, no external deps (charts are hand-rolled SVG).
   ========================================================================= */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/** Set text (and optionally a class) on an optional node — never throws. */
function setText(id, value, className) {
  const el = document.getElementById(id);
  if (!el) return null;
  el.textContent = value;
  if (className !== undefined) el.className = className;
  return el;
}

const state = {
  config: null,
  credentials: null,
  lastState: null,
  logSeq: 0,
  logLines: [],
  tradeFilter: '',
  venue: localStorage.getItem('ao.venue') || 'mexc',
  venues: [],            // metadata from /api/venues/meta
  summary: [],           // live per-venue summary from /api/venues
  trades: [],            // closed/open trade history for the active venue
  equityCurve: [],       // equity points for the active venue
  credVenue: null,       // venue being edited in the credentials card
  venueScope: false,     // write config as venues.<id>.* overrides
  ws: null,
  token: localStorage.getItem('ao.token') || '',   // web.api_token (if configured)
  health: null,
};

/** Remember the dashboard token (asked once, then reused for REST + WS). */
function setToken(token) {
  state.token = token || '';
  if (state.token) localStorage.setItem('ao.token', state.token);
  else localStorage.removeItem('ao.token');
}

/* ------------------------------ venues ------------------------------ */
function venueById(id) { return state.venues.find(v => v.id === id) || { id, label: id, needs_passphrase: false }; }
function venueLabel(id) { return venueById(id).label || id; }

/** Venue-scoped API path: /api/v/<active venue><path> */
function vapi(path, opts) { return api(`/api/v/${state.venue}${path}`, opts); }

/** Config keys are written globally, or as venues.<id>.* when scoped. */
function scopedKey(key) {
  return state.venueScope && !key.startsWith('venues.') ? `venues.${state.venue}.${key}` : key;
}

function renderVenueTabs() {
  const host = $('#venueTabs');
  if (!host) return;
  const summary = new Map(state.summary.map(v => [v.id, v]));
  host.innerHTML = state.venues.map(v => {
    const s = summary.get(v.id) || {};
    const live = s.running ? (s.mode === 'live' ? 'live' : 'paper') : 'off';
    const pnl = Number(s.realized_pnl || 0);
    const cred = s.credentials ? (s.credentials.complete ? 'keys ✓' : (s.credentials.configured ? 'keys partial' : 'no keys')) : '';
    return `<button class="venue-tab ${v.id === state.venue ? 'active' : ''} ${s.running ? '' : 'offline'}" data-venue="${v.id}">
      <span class="vt-dot ${s.running ? 'on' : 'off'}"></span>
      <span class="vt-name">${esc(v.label)}<em class="vt-mode vt-${live}">${live.toUpperCase()}</em></span>
      <span class="vt-stats">
        <b class="${cls(pnl)}">${s.equity != null ? fmtMoney(s.equity) : '—'}</b>
        <i class="${cls(pnl)}">${pnl ? fmtMoney(pnl) : '$0.00'} released</i>
        <i class="vt-wr">${(s.win_rate || 0).toFixed(1)}% WR${s.trades ? ` (${s.trades})` : ''}</i>
        <i>${s.open_positions ?? 0} open</i>
        <i class="vt-cred">${cred}</i>
      </span>
    </button>`;
  }).join('');
  $$('#venueTabs .venue-tab').forEach(btn => btn.onclick = () => switchVenue(btn.dataset.venue));
}

async function pollVenues() {
  try {
    const data = await api('/api/venues');
    state.summary = data.venues || [];
    renderVenueTabs();
    renderVenueBar();
  } catch (e) { /* keep last known */ }
  try {
    state.health = await api('/api/health');
    renderSafetyBanner();
  } catch (e) { /* keep last known */ }
}

/** Loud banner when real money is armed behind an unauthenticated dashboard. */
function renderSafetyBanner() {
  const host = $('#safetyBanner');
  if (!host) return;
  const h = state.health || {};
  const live = (h.live_venues || []);
  const msgs = [];
  if (h.insecure_live) {
    msgs.push(`⚠️ LIVE trading is armed on ${live.join(', ')} while this dashboard has no API token ` +
              `— anyone who can reach this port can trade the account. Set <code>web.api_token</code>.`);
  }
  if (live.length && h.auth_required) {
    msgs.push(`LIVE on ${live.join(', ')} — orders are real money.`);
  }
  host.innerHTML = msgs.map(m => `<div class="safety-banner">${m}</div>`).join('');
  host.classList.toggle('hidden', msgs.length === 0);
}

function renderVenueBar() {
  const s = state.summary.find(v => v.id === state.venue);
  const el = $('#venueMeta');
  if (!el) return;
  if (!s) { el.textContent = ''; return; }
  const bits = [
    `<span class="vm-chip">${esc(venueLabel(state.venue))}</span>`,
    `<span>${s.running ? 'engine running' : 'engine stopped'}</span>`,
    `<span>market data: <b>${esc(s.market_data || '—')}</b></span>`,
    `<span>watchlist: <b>${s.watchlist ?? 0}</b></span>`,
    `<span>trades: <b>${s.trades ?? 0}</b> · win ${Number(s.win_rate || 0).toFixed(1)}%</span>`,
    s.start_error ? `<span class="neg">start error: ${esc(s.start_error)}</span>` : '',
  ];
  el.innerHTML = bits.filter(Boolean).join(' <i class="sep">·</i> ');
}

/** Trade history and the equity curve are paginated REST data, not part of the
 *  websocket frame — poll them for the active venue and feed them back into the
 *  renderer so the dashboard always shows *that venue's* trades. */
async function pollExtras() {
  try {
    const [t, e] = await Promise.all([vapi('/trades?limit=200'), vapi('/equity?limit=400')]);
    state.trades = t.trades || [];
    state.equityCurve = e.curve || [];
    if (state.lastState) {
      state.lastState.trades = state.trades;
      state.lastState.equity = state.equityCurve;
      renderAll(state.lastState);
    }
  } catch (e) { /* keep last known */ }
}

async function switchVenue(id) {
  if (!id || id === state.venue) return;
  state.venue = id;
  state.logLines = [];
  state.logSeq = 0;
  localStorage.setItem('ao.venue', id);
  const brand = $('#brandVenue');
  if (brand) brand.textContent = venueLabel(id);
  renderVenueTabs();
  renderVenueBar();
  if (state.ws) { try { state.ws.close(); } catch (e) {} }
  state.trades = [];              // never show the previous venue's trades
  state.equityCurve = [];
  try { await loadSettings(); } catch (e) {}
  try { renderAll(await vapi('/state')); } catch (e) {}
  connectWS();
  pollExtras();
}

/* ------------------------------ helpers ------------------------------ */
function fmtMoney(v, digits = 2) {
  const n = Number(v || 0);
  const sign = n < 0 ? '-' : '';
  return sign + '$' + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtNum(v, digits = 4) {
  const n = Number(v || 0);
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
  if (Math.abs(n) >= 1) return n.toFixed(Math.min(4, digits));
  return n.toPrecision(4);
}
function fmtPct(v, digits = 2) { return (Number(v || 0) >= 0 ? '+' : '') + Number(v || 0).toFixed(digits) + '%'; }
function fmtTime(ts, withDate = true) {
  if (!ts) return '—';
  const d = new Date(Number(ts) * 1000);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  return withDate ? d.toLocaleDateString([], { month: 'short', day: '2-digit' }) + ' ' + time : time;
}
function fmtAge(sec) {
  if (sec == null) return '—';
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm ' + (s % 60) + 's';
  return Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
}
function cls(v) { return Number(v) > 0 ? 'pos' : (Number(v) < 0 ? 'neg' : ''); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, kind = '', ms = 4200) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast ' + kind;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

async function api(path, opts = {}, retry = true) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['X-API-Token'] = state.token;
  const res = await fetch(path, { headers, ...opts });
  if (res.status === 401 && retry) {
    const entered = prompt('This dashboard requires an API token (web.api_token):', state.token || '');
    if (entered !== null) {
      setToken(entered.trim());
      return api(path, opts, false);
    }
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ------------------------------ charts ------------------------------ */
const NEON = ['#22d3ee', '#8b5cf6', '#e879f9', '#34d399', '#fbbf24', '#38bdf8'];

/* One shared <defs> block per chart: neon gradients + a soft outer glow. */
function svgDefs(uid, colors) {
  const grads = colors.map((c, i) => `
    <linearGradient id="${uid}-g${i}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${c}" stop-opacity="0.34"/>
      <stop offset="55%" stop-color="${c}" stop-opacity="0.10"/>
      <stop offset="100%" stop-color="${c}" stop-opacity="0"/>
    </linearGradient>
    <linearGradient id="${uid}-s${i}" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="${c}" stop-opacity="0.55"/>
      <stop offset="50%" stop-color="${c}" stop-opacity="1"/>
      <stop offset="100%" stop-color="${c}" stop-opacity="0.75"/>
    </linearGradient>`).join('');
  return `<defs>
    ${grads}
    <filter id="${uid}-glow" x="-30%" y="-60%" width="160%" height="240%">
      <feGaussianBlur stdDeviation="3.4" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>`;
}

function lineChart(el, series, opts = {}) {
  const w = el.clientWidth || 600, h = el.clientHeight || 250;
  const pad = { l: 56, r: 16, t: 14, b: 24 };
  const all = series.flatMap(s => s.data);
  if (!all.length) { el.innerHTML = '<div class="muted" style="padding:24px">no data yet</div>'; return; }
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08; max += span * 0.08;
  const n = Math.max(2, series[0].data.length);
  const X = i => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
  const Y = v => pad.t + (1 - (v - min) / (max - min)) * (h - pad.t - pad.b);

  const uid = 'c' + Math.random().toString(36).slice(2, 8);
  let g = '';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (i / 4) * (h - pad.t - pad.b);
    const val = max - (i / 4) * (max - min);
    g += `<line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${y}" y2="${y}"/>`;
    g += `<text class="axis-label" x="8" y="${y + 3}">${fmtNum(val, 2)}</text>`;
  }

  let paths = '';
  series.forEach((s, si) => {
    const d = s.data.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    const color = s.color || NEON[si % NEON.length];
    const dash = s.dash ? 'stroke-dasharray="4 4"' : '';
    if (s.fill !== false && series.length <= 2) {
      paths += `<path d="${d} L${X(s.data.length - 1).toFixed(1)},${(h - pad.b).toFixed(1)} L${X(0).toFixed(1)},${(h - pad.b).toFixed(1)} Z"
        fill="url(#${uid}-g${si})" stroke="none"/>`;
    }
    paths += `<path d="${d}" fill="none" stroke="url(#${uid}-s${si})" stroke-width="${s.width || 2}"
      stroke-linejoin="round" stroke-linecap="round" ${dash} ${s.glow === false ? '' : `filter="url(#${uid}-glow)" opacity="0.95"`}/>`;
    if (s.marker !== false && series.length === 1 && s.data.length > 1) {
      const lx = X(s.data.length - 1), ly = Y(s.data[s.data.length - 1]);
      paths += `<circle cx="${lx}" cy="${ly}" r="4.5" fill="${color}" filter="url(#${uid}-glow)"/>
                <circle cx="${lx}" cy="${ly}" r="8" fill="${color}" opacity="0.18"/>`;
    }
  });
  const markers = (opts.markers || []).map(m => `<line x1="${X(m.i)}" x2="${X(m.i)}" y1="${pad.t}" y2="${h - pad.b}" stroke="${m.color}" stroke-width="1" stroke-dasharray="3 3"/>`);
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${svgDefs(uid, series.map((s, i) => s.color || NEON[i % NEON.length]))}${g}${paths}${markers.join('')}</svg>`;
}

function barChart(el, values, opts = {}) {
  const w = el.clientWidth || 600, h = el.clientHeight || 250;
  const pad = { l: 48, r: 14, t: 14, b: 28 };
  if (!values.length) { el.innerHTML = '<div class="muted" style="padding:24px">no closed trades yet</div>'; return; }
  const maxAbs = Math.max(1, ...values.map(v => Math.abs(v)));
  const bw = (w - pad.l - pad.r) / values.length;
  const uid = 'b' + Math.random().toString(36).slice(2, 8);
  const zero = pad.t + (h - pad.t - pad.b) * 0.5;
  let bars = '';
  values.forEach((v, i) => {
    const x = pad.l + i * bw + bw * 0.16;
    const bh = Math.max(1.5, (Math.abs(v) / maxAbs) * (h - pad.t - pad.b) * 0.5);
    const y = v >= 0 ? zero - bh : zero;
    const grad = v >= 0 ? `${uid}-up` : `${uid}-dn`;
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.68).toFixed(1)}" height="${bh.toFixed(1)}"
      fill="url(#${grad})" rx="3" filter="url(#${uid}-glow)" opacity="0.92"/>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs>
      <linearGradient id="${uid}-up" x1="0" y1="1" x2="0" y2="0">
        <stop offset="0%" stop-color="#0f766e" stop-opacity="0.55"/><stop offset="100%" stop-color="#34d399"/>
      </linearGradient>
      <linearGradient id="${uid}-dn" x1="0" y1="1" x2="0" y2="0">
        <stop offset="0%" stop-color="#fb7185"/><stop offset="100%" stop-color="#7f1d3a" stop-opacity="0.55"/>
      </linearGradient>
      <filter id="${uid}-glow" x="-40%" y="-40%" width="180%" height="180%">
        <feGaussianBlur stdDeviation="2.6" result="b"/>
        <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${zero}" y2="${zero}"/>
    <text class="axis-label" x="8" y="${zero + 3}">0%</text>
    <text class="axis-label" x="4" y="${pad.t + 12}">+${maxAbs.toFixed(0)}%</text>
    <text class="axis-label" x="4" y="${h - pad.b}">-${maxAbs.toFixed(0)}%</text>
    ${bars}</svg>`;
}

const HIST_COLORS = { green: 'var(--green)', red: 'var(--red)' };

/* ------------------------------ overview ------------------------------ */
function renderOverview(d) {
  const acct = d.state.account, stats = d.metrics.trades, engine = d.state.engine;
  const fixedStart = acct.starting_balance ?? d.state.target?.starting_balance ?? null;
  setText('kStart', fixedStart == null ? '—' : fmtMoney(fixedStart));
  setText('kStartSub', fixedStart == null ? 'fixed' : `fixed · return ${fmtPct(acct.return_pct)}`);
  $('#kEquity').textContent = fmtMoney(acct.equity);
  $('#kEquitySub').textContent = `available ${fmtMoney(acct.available)} · open P/L ${fmtMoney(acct.open_pnl ?? acct.unrealized)}`;
  const released = acct.released_pnl ?? acct.realized_pnl ?? stats.pnl;
  $('#kPnl').textContent = fmtMoney(released);
  $('#kPnl').className = 'kpi ' + cls(released);
  $('#kPnlSub').textContent = `${stats.trades} closed · fees ${fmtMoney(stats.total_fees)} · open ${fmtMoney(acct.open_pnl ?? 0)}`;
  $('#kWin').textContent = (stats.win_rate || 0).toFixed(1) + '%';
  $('#kWinSub').textContent = `${stats.wins}W / ${stats.losses}L · PF ${stats.profit_factor === Infinity ? '∞' : stats.profit_factor}`;
  $('#kOpen').textContent = d.state.positions.length;
  $('#kOpenSub').textContent = `max ${state.config?.risk?.max_open_positions ?? 10} · margin ${fmtMoney(acct.position_margin)}`;
  $('#kExp').textContent = fmtMoney(stats.expectancy_usd);
  $('#kExpSub').textContent = `avg ROI ${fmtPct(stats.expectancy_roi)} · avg win ${fmtMoney(stats.avg_win)} / loss ${fmtMoney(stats.avg_loss)}`;
  const lat = d.state.latency || {};
  $('#kLat').textContent = lat.count ? `${lat.p50 ?? 0} / ${lat.p95 ?? 0} ms` : '—';
  $('#kLatSub').textContent = lat.count ? `${lat.count} requests · max ${lat.max} ms` : 'no requests yet';

  const curve = d.equity.map(p => Number(p.equity));
  lineChart($('#equityChart'), [{ data: curve.length ? curve : [acct.equity], color: '#22d3ee', fill: true }]);
  $('#curveMeta').textContent = `${curve.length} points · ${fmtPct(d.metrics.curve.return_pct)} · max DD ${d.metrics.curve.max_drawdown_pct}%`;

  const rois = (d.trades || []).filter(t => t.status === 'CLOSED').map(t => Number(t.roi_pct || 0)).slice(0, 120).reverse();
  barChart($('#roiChart'), rois);

  const risk = d.state.risk || {};
  $('#riskTable').innerHTML = [
    ['Trading', d.state.engine.trading_enabled ? 'enabled' : 'paused'],
    ['Halted', risk.halted ? `<span class="neg">${esc(risk.halt_reason || 'yes')}</span>` : 'no'],
    ['Day', `${risk.day || '—'} · PnL ${fmtPct(risk.day_pnl_pct)}`],
    ['Day start equity', fmtMoney(risk.day_start_equity)],
    ['Equity peak', fmtMoney(risk.equity_peak)],
    ['Drawdown from peak', `${(risk.drawdown_pct ?? 0).toFixed(2)}%`],
    ['Max open positions', state.config?.risk?.max_open_positions ?? '—'],
    ['Risk per trade', `${state.config?.risk?.equity_per_trade_pct ?? '—'}% × ${state.config?.risk?.leverage ?? '—'}x`],
    ['Cooldowns', Object.keys(risk.cooldowns || {}).length ? esc(JSON.stringify(risk.cooldowns)) : 'none'],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  const bd = d.state.broker_diagnostics || {};
  const ws = bd.ws || {};
  $('#brokerTable').innerHTML = [
    ['Broker', esc(d.state.engine.broker)],
    ['Market data', esc(d.state.engine.market_data)],
    ['Clock offset', `${bd.clock_offset_ms ?? '—'} ms (rtt ${bd.clock_rtt_ms ?? '—'} ms)`],
    ['Credentials', bd.credentials ? 'configured' : 'not configured'],
    ['Attachment protection', bd.attached_protection === undefined ? '—' : (bd.attached_protection ? 'attached SL/TP' : 'separate orders')],
    ['WebSocket', ws.connected === undefined ? '—' : (ws.connected ? `<span class="pos">connected</span> (${ws.kline_streams || 0} klines, ${ws.tick_streams || 0} ticks)` : `<span class="neg">down</span>`)],
    ['WS reconnects', ws.reconnects ?? '—'],
    ['Order latency', bd.order_latency_ms ? `p50 ${bd.order_latency_ms.p50} ms · p95 ${bd.order_latency_ms.p95} ms` : '—'],
    ['Watchlist', `${(d.state.engine.watchlist || []).length} symbols`],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  $('#eventFeed').innerHTML = (d.state.events || []).slice(0, 40).map(e => {
    const summary = e.event === 'signal'
      ? `<b>${esc(e.side)} ${esc(e.symbol)}</b> score ${e.score} @ ${fmtNum(e.price)}`
      : e.event === 'position_opened'
        ? `<b>OPEN ${esc(e.side)} ${esc(e.symbol)}</b> @ ${fmtNum(e.entry_price)} · SL ${fmtNum(e.sl_price)} · TP ${fmtNum(e.tp_price)}`
        : e.event === 'position_closed'
          ? `<b>CLOSE ${esc(e.symbol)}</b> ${fmtMoney(e.pnl)} (${fmtPct(e.roi_pct)}) · ${esc(e.reason)}`
          : e.event === 'trail_moved'
            ? `<b>TRAIL ${esc(e.symbol)}</b> stop ${e.stop_roi}% ROI @ ${fmtNum(e.stop_price)} (peak ${e.peak_roi}%)`
            : e.event === 'signal_rejected'
              ? `skip <b>${esc(e.symbol)}</b> — ${esc(e.reason)}`
              : esc(JSON.stringify(e));
    return `<div class="feed-item"><time>${fmtTime(e.ts, false)}</time><span>${summary}</span></div>`;
  }).join('') || '<div class="muted">no activity yet</div>';
}

/* ------------------------------ positions ------------------------------ */
function renderPositions(d) {
  const rows = d.state.positions || [];
  $('#posCount').textContent = rows.length;
  const tbody = $('#positionsTable tbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="14" class="muted">no open positions</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(p => {
    const trail = p.trail_active
      ? `<span class="tag tag-ok">${(p.stop_roi_pct ?? 0).toFixed(0)}%</span>`
      : `<span class="tag tag-no">idle → ${p.next_trail_at_roi ? p.next_trail_at_roi.toFixed(0) + '%' : '—'}</span>`;
    return `<tr>
      <td><b>${esc(p.symbol)}</b></td>
      <td><span class="tag ${p.side === 'LONG' ? 'tag-long' : 'tag-short'}">${esc(p.side)}</span></td>
      <td>${fmtNum(p.qty, 6)}</td>
      <td>${fmtNum(p.entry_price)}</td>
      <td>${fmtNum(p.mark_price)}</td>
      <td class="${cls(p.roi_pct)}">${fmtPct(p.roi_pct, 1)}</td>
      <td class="${cls(p.peak_roi_pct)}">${fmtPct(p.peak_roi_pct, 1)}</td>
      <td>${fmtNum(p.stop_price)}</td>
      <td>${trail}</td>
      <td>${fmtNum(p.tp_price)}</td>
      <td>${fmtMoney(p.margin_usd)}</td>
      <td class="${cls(p.pnl)}">${fmtMoney(p.pnl)}</td>
      <td>${fmtAge(p.age_s)}</td>
      <td><button class="btn btn-danger-ghost btn-sm" data-close="${esc(p.symbol)}">close</button></td>
    </tr>`;
  }).join('');
  $$('[data-close]', tbody).forEach(b => b.onclick = async () => {
    try {
      await vapi('/control/close', { method: 'POST', body: JSON.stringify({ symbol: b.dataset.close }) });
      toast('Close order sent for ' + b.dataset.close, 'ok');
    } catch (e) { toast('Close failed: ' + e.message, 'error'); }
  });
}

/* ------------------------------ trades ------------------------------ */
function renderTrades(d) {
  const filter = state.tradeFilter.toUpperCase();
  const rows = (d.trades || []).filter(t => !filter || String(t.symbol).toUpperCase().includes(filter));
  $('#tradesTable tbody').innerHTML = rows.length ? rows.map(t => `<tr>
      <td>${fmtTime(t.closed_at || t.opened_at)}</td>
      <td><b>${esc(t.symbol)}</b></td>
      <td><span class="tag ${t.side === 'LONG' ? 'tag-long' : 'tag-short'}">${esc(t.side)}</span></td>
      <td>${fmtNum(t.entry_price)}</td>
      <td>${fmtNum(t.exit_price)}</td>
      <td>${fmtNum(t.qty, 6)}</td>
      <td>${t.leverage}x</td>
      <td>${fmtMoney(t.margin_usd)}</td>
      <td>${fmtPct(t.peak_roi_pct, 1)}</td>
      <td class="${cls(t.roi_pct)}">${t.status === 'OPEN' ? '—' : fmtPct(t.roi_pct, 1)}</td>
      <td class="${cls(t.realized_pnl)}">${fmtMoney(t.realized_pnl)}</td>
      <td>${fmtMoney(t.fees_usd)}</td>
      <td>${esc((t.exit_reason || (t.status === 'OPEN' ? 'open' : '')).replace(':', ' · '))}</td>
    </tr>`).join('') : '<tr><td colspan="13" class="muted">no trades yet</td></tr>';
}

/* ------------------------------ signals ------------------------------ */
function renderSignals(d) {
  const rows = d.state.signals || [];
  $('#signalsTable tbody').innerHTML = rows.length ? rows.map(s => {
    const filters = (s.filters && s.filters.results) || [];
    const failed = filters.filter(f => !f.passed).map(f => f.note).join(' · ');
    const reason = s.report_reason || (s.status === 'executed' ? 'position opened' : (failed || s.reason || ''));
    const tagCls = s.status === 'executed' ? 'tag-ok' : (s.status === 'rejected' ? 'tag-warn' : 'tag-no');
    return `<tr>
      <td>${fmtTime(s.created_at)}</td>
      <td><b>${esc(s.symbol)}</b></td>
      <td><span class="tag ${s.side === 'LONG' ? 'tag-long' : 'tag-short'}">${esc(s.side)}</span></td>
      <td>${(s.score || 0).toFixed(1)}</td>
      <td>${fmtNum(s.price)}</td>
      <td>${(s.atr_pct || 0).toFixed(3)}%</td>
      <td><span class="tag ${tagCls}">${esc(s.status)}</span></td>
      <td class="muted" style="max-width:420px;font-family:inherit">${esc(reason).slice(0, 260)}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="8" class="muted">no signals yet — waiting for a divergence on the 5m chart</td></tr>';
}

/* ------------------------------ universe ------------------------------ */
function renderUniverse(d) {
  const rows = d.state.universe || [];
  $('#universeTable tbody').innerHTML = rows.length ? rows.map((u, i) => `<tr>
      <td>${i + 1}</td>
      <td><b>${esc(u.symbol)}</b></td>
      <td>${u.score.toFixed(1)}</td>
      <td>${fmtNum(u.price)}</td>
      <td>${fmtMoney(u.turnover24, 0)}</td>
      <td>${u.range24_pct.toFixed(2)}%</td>
      <td class="warn">${u.atr_pct_5m.toFixed(3)}%</td>
      <td>${u.vol_ratio.toFixed(2)}x</td>
      <td>${u.adx.toFixed(1)}</td>
      <td>${u.spread_bps.toFixed(1)}bps</td>
      <td>${u.max_leverage}x</td>
    </tr>`).join('') : '<tr><td colspan="11" class="muted">universe scan pending…</td></tr>';
  $('#rejectedList').innerHTML = (d.rejected || []).slice(0, 40).map(r =>
    `<span class="chip">${esc(r.symbol)} — ${esc(r.reason)}</span>`).join('') || '<span class="muted">—</span>';
}

/* ------------------------------ plan ------------------------------ */
function renderPlan(d) {
  const c = d.compound || {};
  const sim = c.simulation || {}, req = c.requirements || {}, math = c.per_trade_math || {}, a = c.assumptions || {};
  $('#pDaily').textContent = ((sim.inputs && sim.inputs.required_daily_growth_pct) || 0).toFixed(2) + '%';
  $('#pHit').textContent = (sim.prob_hit_target_pct ?? 0).toFixed(2) + '%';
  $('#pMedian').textContent = fmtMoney(sim.median_final_equity, 0);
  $('#pBreakeven').textContent = (c.breakeven_win_rate_pct ?? 0).toFixed(1) + '%';

  $('#planMath').innerHTML = [
    ['Notional per trade', `${math.notional_pct_of_equity}% of equity`],
    ['Equity gain per TP (+' + (sim.inputs ? sim.inputs.tp_roi_pct : 200) + '% ROI)', `+${math.equity_gain_per_tp_pct}%`],
    ['Equity loss per SL (−' + (sim.inputs ? sim.inputs.sl_roi_pct : 30).toFixed(1) + '% ROI)', `${math.equity_loss_per_sl_pct}%`],
    ['Required total growth', `${sim.inputs ? sim.inputs.required_total_growth_pct : 0}%`],
    ['Required win rate (expectation)', req.required_win_rate_pct !== undefined ? req.required_win_rate_pct + '%' : '—'],
    ['Trades needed', req.trades_total ?? '—'],
    ['P(ruin)', (sim.prob_ruin_pct ?? 0) + '%'],
    ['Expected multiple', (sim.expected_growth_multiple ?? 0) + 'x'],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  $('#planAssumptions').innerHTML = [
    ['Equity (current)', fmtMoney(a.equity)],
    ['Win rate used', `${a.win_rate_pct}% (${esc(a.win_rate_source || '')})`],
    ['Sample size', a.sample_size ?? 0],
    ['SL used (ROI%)', a.sl_roi_pct_used ?? '—'],
    ['Avg 5m ATR%', a.avg_atr_pct_5m ?? '—'],
    ['Trades / day', a.trades_per_day ?? '—'],
    ['Monte-Carlo runs', sim.runs ?? '—'],
    ['At the target, equity lands in', `p5 ${fmtMoney(sim.p05, 0)} · p95 ${fmtMoney(sim.p95, 0)}`],
  ].map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('');

  const paths = (sim.sample_paths || []).slice(0, 12).map((p, i) => ({
    data: p, color: NEON[i % NEON.length], width: 1.5, glow: false, fill: false,
  }));
  if (paths.length) lineChart($('#mcChart'), paths); else $('#mcChart').innerHTML = '<div class="muted" style="padding:24px">no simulation yet</div>';

  $('#sensitivityTable tbody').innerHTML = (c.sensitivity || []).map(s => `<tr>
      <td>${s.win_rate}%</td>
      <td>${s.prob_hit_target_pct}%</td>
      <td>${fmtMoney(s.median_final_equity, 0)}</td>
      <td>${fmtMoney(s.mean_final_equity, 0)}</td>
      <td>${s.prob_ruin_pct}%</td>
      <td>${s.avg_max_drawdown_pct}%</td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted">—</td></tr>';
  $('#planDisclaimer').textContent = c.disclaimer || '';
}

/* ------------------------------ ticker strip ------------------------------ */
function renderTickerStrip(d) {
  const el = $('#tickerStrip');
  if (!el) return;
  const rows = (d.state.universe || []).slice(0, 12);
  if (!rows.length) {
    el.innerHTML = '<div class="ticker-empty">waiting for the first universe scan…</div>';
    return;
  }
  const watch = new Set(d.state.engine?.watchlist || []);
  el.innerHTML = rows.map(u => {
    const chg = Number(u.trend_pct || 0);
    const color = chg > 0 ? 'pos' : (chg < 0 ? 'neg' : 'muted');
    return `<div class="ticker-item" title="${esc(u.symbol)} · ATR ${u.atr_pct_5m}% · score ${u.score}${watch.has(u.symbol) ? ' · trading' : ''}">
      <div class="tk-row">
        <span class="tk-sym">${esc(u.symbol)}${watch.has(u.symbol) ? '<span class="live-dot" style="margin-left:6px"></span>' : ''}</span>
        <span class="${color}">${fmtPct(chg, 2)}</span>
      </div>
      <div class="tk-row"><span>${fmtNum(u.price)}</span><span class="tk-meta">ATR ${Number(u.atr_pct_5m || 0).toFixed(2)}%</span></div>
    </div>`;
  }).join('');
}

/* ------------------------------ heartbeat ------------------------------ */
function setHeartbeat(engine, connected) {
  const dot = $('#liveDot'), label = $('#liveText');
  if (!dot || !label) return;
  const live = !!(engine.running && connected);
  dot.className = 'live-dot' + (live ? '' : ' off');
  label.textContent = live ? (engine.trading_enabled ? 'LIVE' : 'PAUSED') : 'OFFLINE';
}
function tickClock() {
  const el = $('#topClock');
  if (el) el.textContent = new Date().toISOString().slice(11, 19);
}
setInterval(tickClock, 1000);
tickClock();

/* ------------------------------ header ------------------------------ */
function renderHeader(d) {
  const engine = d.state.engine, acct = d.state.account;
  const venue = d.state.venue || {};
  const modeBadge = $('#modeBadge');
  const isLive = engine.mode === 'live';
  modeBadge.textContent = `${(venue.id || state.venue).toUpperCase()} · ${isLive ? 'LIVE' : 'PAPER'}`;
  modeBadge.className = 'badge ' + (isLive ? 'badge-live' : 'badge-paper');
  const brand = $('#brandVenue');
  if (brand && venue.label) brand.textContent = venue.label;

  const db = $('#dataBadge');
  const md = engine.market_data || 'unknown';
  const mdLabel = { synthetic: 'SIMULATED DATA', public: 'LIVE public market data', live: 'LIVE exchange feed' }[md] || md;
  db.textContent = mdLabel;
  db.className = 'badge ' + (md === 'synthetic' ? 'badge-paper' : 'badge-dim');

  setHeartbeat(engine, d.state.broker_diagnostics?.ws?.connected !== false);

  const sb = $('#statusBadge');
  const connected = (d.state.broker_diagnostics?.ws?.connected !== false) && engine.running;
  sb.textContent = engine.running ? (engine.trading_enabled ? 'running' : 'paused') : 'stopped';
  sb.className = 'badge ' + (engine.running && engine.trading_enabled ? 'badge-ok' : 'badge-dim');

  const halt = $('#haltBadge');
  if (d.state.risk?.halted) { halt.classList.remove('hidden'); halt.textContent = 'HALTED: ' + (d.state.risk.halt_reason || ''); }
  else halt.classList.add('hidden');

  const stats = d.metrics?.trades || {};
  const released = acct.released_pnl ?? acct.realized_pnl ?? 0;
  const openPnl = acct.open_pnl ?? acct.unrealized ?? 0;
  const startBal = acct.starting_balance ?? d.state.target?.starting_balance ?? null;
  const venueTag = (venue.id || state.venue || '').toUpperCase();

  // every figure below belongs to the *active venue tab* — never mixed
  setText('topStart', startBal == null ? '—' : fmtMoney(startBal));
  setText('topStartSub', startBal == null ? 'fixed' : `${venueTag} book · fixed`);
  setText('topEquity', fmtMoney(acct.equity));
  setText('topEquitySub', `return ${fmtPct(acct.return_pct)} · avail ${fmtMoney(acct.available)}`);
  setText('topPnl', fmtMoney(released), 'stat-value ' + cls(released));
  setText('topPnlSub', `${stats.trades ?? acct.trades ?? 0} closed · ${stats.wins ?? acct.wins ?? 0}W/${stats.losses ?? acct.losses ?? 0}L`);
  const wr = acct.win_rate ?? stats.win_rate ?? 0;
  setText('topWin', (Number(wr) || 0).toFixed(1) + '%');
  setText('topWinSub', startBal == null ? '—' : `vs fixed ${fmtMoney(startBal)}`);
  setText('topOpenPnl', fmtMoney(openPnl), 'stat-value ' + cls(openPnl));
  setText('topOpenPnlSub', 'unrealized');
  setText('topOpen', d.state.positions.length);
  setText('topOpenSub', `max ${state.config?.risk?.max_open_positions ?? 10}`);
  // (day P/L lives in the risk table now, next to the drawdown and peak)
  $('#pauseBtn').textContent = engine.trading_enabled ? 'Pause' : 'Resume';
  $('#pauseBtn').className = 'btn ' + (engine.trading_enabled ? 'btn-ghost' : '');

  const t = d.state.target || {};
  const start = acct.starting_balance || t.starting_balance || t.starting_equity || acct.equity || 1;
  const pct = Math.max(0, Math.min(100, ((acct.equity - start) / (t.equity_target - start)) * 100));
  $('#targetFill').style.width = (isFinite(pct) ? pct : 0) + '%';
  $('#targetLabel').textContent = fmtMoney(t.equity_target, 0);
  $('#targetDays').textContent = t.days;
  $('#targetProgress').textContent = `${fmtMoney(acct.equity, 2)} → ${pct.toFixed(2)}% of target · ${t.remaining_days ?? 0}d left`;
}

/* ------------------------------ settings ------------------------------ */
const FIELD_GROUPS = {
  riskForm: [
    ['risk.equity_per_trade_pct', 'Equity per trade (%)', 'number', 0.5],
    ['risk.leverage', 'Leverage (x)', 'number', 1],
    ['risk.max_open_positions', 'Max open positions', 'number', 1],
    ['risk.max_total_margin_pct', 'Max total margin (%)', 'number', 5],
    ['risk.max_daily_loss_pct', 'Daily loss halt (%)', 'number', 1],
    ['risk.max_drawdown_halt_pct', 'Drawdown halt (%)', 'number', 1],
    ['risk.cooldown_after_loss_min', 'Cooldown after loss (min)', 'number', 1],
    ['risk.min_notional_usd', 'Min notional ($)', 'number', 1],
    ['risk.max_margin_usd', 'Max margin per trade ($, 0 = off)', 'number', 1],
    ['stoploss.atr_multiplier', 'ATR multiplier (SL)', 'number', 0.1],
    ['stoploss.atr_period', 'ATR period', 'number', 1],
    ['stoploss.use_mark_price_trigger', 'Trigger on mark price', 'bool'],
    ['stoploss.local_watchdog', 'Local SL watchdog', 'bool'],
    ['stoploss.watchdog_grace_bps', 'Watchdog grace (bps)', 'number', 1],
    ['takeprofit.tp_roi_pct', 'Take profit (ROI %)', 'number', 5],
    ['takeprofit.exchange_side', 'Exchange-side TP order', 'bool'],
    ['trailing.enabled', 'Trailing enabled', 'bool'],
    ['trailing.trail_start_roi', 'Trail start ROI (%)', 'number', 1],
    ['trailing.trail_initial_stop_roi', 'Trail initial stop ROI (%)', 'number', 1],
    ['trailing.trail_step_roi', 'Trail step ROI (%)', 'number', 1],
    ['trailing.trail_stop_step_roi', 'Trail stop step ROI (%)', 'number', 1],
    ['trailing.ratchet_only', 'Ratchet only (never loosen)', 'bool'],
    ['trailing.step_only_updates', 'Update only on new step', 'bool'],
    ['trailing.use_mark_price_for_peak', 'Peak ROI from mark price', 'bool'],
    ['trailing.min_move_bps', 'Min stop move (bps)', 'number', 1],
  ],
  strategyForm: [
    ['strategy.timeframe', 'Timeframe', 'select', ['Min1', 'Min5', 'Min15', 'Min30', 'Min60']],
    ['strategy.ao_fast', 'AO fast period', 'number', 1],
    ['strategy.ao_slow', 'AO slow period', 'number', 1],
    ['strategy.pivot_k', 'Pivot strength (k)', 'number', 1],
    ['strategy.lookback_bars', 'Divergence lookback (bars)', 'number', 5],
    ['strategy.min_pivot_gap', 'Min pivot gap', 'number', 1],
    ['strategy.max_pivot_gap', 'Max pivot gap', 'number', 1],
    ['strategy.min_ao_delta_atr', 'Min AO delta (× ATR)', 'number', 0.01],
    ['strategy.require_ao_extreme', 'Require AO extreme', 'bool'],
    ['strategy.require_trigger_break', 'Require structure break', 'bool'],
    ['strategy.signal_cooldown_bars', 'Signal cooldown (bars)', 'number', 1],
    ['strategy.min_signal_score', 'Min signal score', 'number', 1],
    ['filters.volatility.enabled', 'Filter: volatility band', 'bool'],
    ['filters.volatility.min_atr_pct', 'Min ATR% (5m)', 'number', 0.01],
    ['filters.volatility.max_atr_pct', 'Max ATR% (5m)', 'number', 0.1],
    ['filters.volatility.min_atr_percentile', 'Min ATR percentile', 'number', 1],
    ['filters.trend.enabled', 'Filter: trend alignment', 'bool'],
    ['filters.trend.mode', 'Trend mode', 'select', ['ema', 'ema_stack', 'off']],
    ['filters.trend.ema_period', 'Trend EMA period', 'number', 5],
    ['filters.htf.enabled', 'Filter: higher timeframe', 'bool'],
    ['filters.htf.htf_timeframe', 'HTF timeframe', 'select', ['Min5', 'Min15', 'Min30', 'Min60']],
    ['filters.htf.require_htf_ao_rising', 'HTF AO must turn', 'bool'],
    ['filters.volume.enabled', 'Filter: volume confirmation', 'bool'],
    ['filters.volume.min_volume_mult', 'Min volume multiple', 'number', 0.05],
    ['filters.volume.min_turnover_24h_usd', 'Min 24h turnover ($)', 'number', 100000],
    ['filters.volume.require_volume_climax', 'Require pivot volume climax', 'bool'],
    ['filters.momentum.enabled', 'Filter: momentum/RSI', 'bool'],
    ['filters.momentum.rsi_long_max', 'RSI max for longs', 'number', 1],
    ['filters.momentum.rsi_short_min', 'RSI min for shorts', 'number', 1],
    ['filters.momentum.macd_confirm', 'MACD must confirm', 'bool'],
    ['filters.chop.enabled', 'Filter: chop/ADX', 'bool'],
    ['filters.chop.min_adx', 'Min ADX', 'number', 1],
    ['filters.orderbook.enabled', 'Filter: order book', 'bool'],
    ['filters.orderbook.max_spread_bps', 'Max spread (bps)', 'number', 0.5],
    ['filters.orderbook.min_depth_mult', 'Min depth multiple', 'number', 0.5],
    ['filters.shock.enabled', 'Filter: shock candle', 'bool'],
    ['filters.shock.max_candle_atr', 'Max candle move (× ATR)', 'number', 0.1],
  ],
  universeForm: [
    ['universe.enabled', 'Dynamic universe', 'bool'],
    ['universe.max_symbols', 'Max symbols monitored', 'number', 1],
    ['universe.refresh_sec', 'Rescan interval (s)', 'number', 30],
    ['universe.min_turnover_24h_usd', 'Min 24h turnover ($)', 'number', 100000],
    ['universe.min_atr_pct', 'Min 5m ATR%', 'number', 0.01],
    ['universe.max_atr_pct', 'Max 5m ATR%', 'number', 0.1],
    ['universe.min_open_interest_usd', 'Min open interest ($)', 'number', 100000],
    ['universe.exclude_new_listings', 'Exclude new listings', 'bool'],
    ['universe.exclude_stable_pairs', 'Exclude stable pairs', 'bool'],
    ['universe.require_api_allowed', 'Require API-enabled', 'bool'],
    ['universe.min_max_leverage', 'Min available leverage', 'number', 1],
    ['universe.blacklist', 'Blacklist (comma separated)', 'csv'],
    ['universe.whitelist', 'Whitelist (empty = auto)', 'csv'],
    ['target.equity_target', 'Target equity ($)', 'number', 100],
    ['target.days', 'Target horizon (days)', 'number', 1],
    ['target.monte_carlo_runs', 'Monte-Carlo runs', 'number', 1000],
  ],
};

function getPath(obj, path) {
  return path.split('.').reduce((acc, k) => (acc == null ? undefined : acc[k]), obj);
}
function setPath(obj, path, value) {
  const parts = path.split('.');
  let node = obj;
  for (const p of parts.slice(0, -1)) node = node[p] = node[p] || {};
  node[parts[parts.length - 1]] = value;
}

const VENUE_FIELDS = [
  ['enabled', 'enabled', 'bool'],
  ['mode', 'mode (paper / live)', 'select', ['paper', 'live']],
  ['rest_base', 'REST base URL', 'text'],
  ['ws_url', 'WebSocket URL', 'text'],
  ['recv_window_ms', 'recv window (ms)', 'number', 500],
  ['taker_fee', 'taker fee (fraction)', 'number', 0.0001],
  ['paper_data_source', 'paper data source', 'select', ['auto', 'live', 'synthetic']],
  ['entry_order_type', 'entry order type', 'select', ['market', 'ioc_limit']],
];

function buildVenueForm(venueCfg) {
  const form = $('#venueForm');
  if (!form) return;
  const block = venueCfg || {};
  form.innerHTML = VENUE_FIELDS.map(([key, label, type, extra]) => {
    const value = block[key];
    const id = 'v_' + key;
    const attr = `id="${id}" data-key="${key}" data-type="${type}"`;
    if (type === 'bool') {
      return `<label>${esc(label)}<select ${attr} class="input">
        <option value="true"${value !== false ? ' selected' : ''}>true</option>
        <option value="false"${value === false ? ' selected' : ''}>false</option></select></label>`;
    }
    if (type === 'select') {
      const opts = extra.map(o => `<option value="${o}"${String(value) === String(o) ? ' selected' : ''}>${o}</option>`).join('');
      return `<label>${esc(label)}<select ${attr} class="input">${opts}</select></label>`;
    }
    const step = type === 'number' ? ` step="${extra || 1}"` : '';
    const itype = type === 'number' ? 'number' : 'text';
    return `<label>${esc(label)}<input ${attr} class="input" type="${itype}"${step} value="${value ?? ''}" /></label>`;
  }).join('');
}

function buildForms(overrideCfg) {
  const cfg = overrideCfg || state.effective || state.config || {};
  Object.entries(FIELD_GROUPS).forEach(([formId, fields]) => {
    const form = $('#' + formId);
    if (!form) return;
    form.innerHTML = fields.map(([key, label, type, extra]) => {
      const value = getPath(cfg, key);
      const id = 'f_' + key.replace(/\./g, '_');
      if (type === 'bool') {
        return `<label>${esc(label)}<select id="${id}" data-key="${key}" data-type="bool" class="input">
          <option value="true"${value ? ' selected' : ''}>true</option>
          <option value="false"${!value ? ' selected' : ''}>false</option></select></label>`;
      }
      if (type === 'select') {
        const opts = (extra || []).map(o => `<option value="${o}"${String(value) === String(o) ? ' selected' : ''}>${o}</option>`).join('');
        return `<label>${esc(label)}<select id="${id}" data-key="${key}" data-type="enum" class="input">${opts}</select></label>`;
      }
      if (type === 'csv') {
        const v = Array.isArray(value) ? value.join(',') : (value || '');
        return `<label>${esc(label)}<input id="${id}" data-key="${key}" data-type="list" class="input" value="${esc(v)}" /></label>`;
      }
      return `<label>${esc(label)}<input id="${id}" data-key="${key}" data-type="number" step="${extra || 1}" class="input" value="${value ?? ''}" /></label>`;
    }).join('');
  });
}

function collectPatch(formId) {
  const patch = {};
  $$('#' + formId + ' [data-key]').forEach(el => {
    const key = el.dataset.key, type = el.dataset.type;
    let value = el.value;
    if (type === 'bool') value = value === 'true';
    else if (type === 'number') value = Number(value);
    else if (type === 'list') value = value.split(',').map(s => s.trim()).filter(Boolean);
    patch[key] = value;
  });
  return patch;
}

function scopePatch(patch) {
  const out = {};
  Object.entries(patch).forEach(([k, v]) => { out[scopedKey(k)] = v; });
  return out;
}

async function saveGroup(formId, label) {
  try {
    const res = await vapi('/settings', { method: 'PUT', body: JSON.stringify(scopePatch(collectPatch(formId))) });
    toast(`${label} saved (${Object.keys(res.applied).length} settings)${res.restart_required ? ' — restart the engine to apply' : ''}`, 'ok');
    await loadSettings();
  } catch (e) { toast('Save failed: ' + e.message, 'error'); }
}

async function resetGroup(formId, keys, label) {
  try {
    const list = (keys || $$('#' + formId + ' [data-key]').map(el => el.dataset.key)).map(scopedKey);
    await vapi('/settings/reset', { method: 'POST', body: JSON.stringify(list) });
    toast(`${label} restored to defaults`, 'ok');
    await loadSettings();
  } catch (e) { toast('Reset failed: ' + e.message, 'error'); }
}

async function loadSettings() {
  const data = await vapi('/settings');
  state.config = data.config;
  state.credentials = data.credentials;
  state.effective = data.effective || {};
  state.venueMeta = data.venue || {};
  state.credVenue = state.credVenue || state.venue;
  // forms render the *effective* (venue-scoped) values so the panel shows what
  // will actually be used on this exchange
  buildForms(data.effective || data.config);
  buildVenueForm(data.venue_overrides);
  renderCredVenueChips();
  renderCredState();
  const label = $('#modeVenueLabel');
  if (label) label.textContent = venueLabel(state.venue);
  const vc = $('#venueConnLabel');
  if (vc) vc.textContent = '· ' + venueLabel(state.venue);
  $('#cfgMode').value = data.mode || 'paper';
  $('#cfgPaperSource').value = state.effective['exchange.paper_data_source'] || 'auto';
  $('#cfgPaperEquity').value = state.effective['account.paper_starting_equity'];
  $('#cfgEntryType').value = state.effective['exchange.entry_order_type'] || 'market';
  const brand = $('#brandVenue');
  if (brand) brand.textContent = venueLabel(state.venue);
}

function renderCredVenueChips() {
  const host = $('#credVenueChips');
  if (!host) return;
  host.innerHTML = state.venues.map(v => {
    const s = state.summary.find(x => x.id === v.id) || {};
    const cred = s.credentials || {};
    const state_ = cred.complete ? 'ok' : (cred.configured ? 'partial' : 'none');
    return `<button class="chip ${v.id === state.credVenue ? 'active' : ''}" data-cred-venue="${v.id}">
      ${esc(v.label)} <i class="chip-key chip-${state_}">${state_ === 'ok' ? 'key ✓' : state_ === 'partial' ? 'incomplete' : 'no key'}</i></button>`;
  }).join('');
  $$('#credVenueChips .chip').forEach(btn => btn.onclick = () => {
    state.credVenue = btn.dataset.credVenue;
    renderCredVenueChips();
    renderCredState();
  });
}

function renderCredState() {
  const cs = $('#credState');
  const vid = state.credVenue || state.venue;
  const s = state.summary.find(v => v.id === vid) || {};
  const cred = s.credentials || {};
  if (cred.complete) {
    cs.textContent = `${venueLabel(vid)} · configured · ${cred.api_key_preview || ''}`;
    cs.className = 'badge badge-ok';
  } else if (cred.configured) {
    cs.textContent = `${venueLabel(vid)} · incomplete (passphrase missing?)`;
    cs.className = 'badge badge-warn';
  } else {
    cs.textContent = `${venueLabel(vid)} · not configured`;
    cs.className = 'badge badge-dim';
  }
  const wrap = $('#passphraseWrap');
  if (wrap) wrap.classList.toggle('hidden', !venueById(vid).needs_passphrase);
}

/* ------------------------------ logs ------------------------------ */
async function pollLogs() {
  try {
    const res = await vapi('/logs?after=' + state.logSeq + '&limit=200');
    if (res.logs && res.logs.length) {
      res.logs.forEach(l => {
        state.logSeq = Math.max(state.logSeq, l.seq);
        state.logLines.push(l);
      });
      if (state.logLines.length > 600) state.logLines = state.logLines.slice(-600);
      $('#logView').innerHTML = state.logLines.map(l =>
        `<span class="log-line log-${l.level}">${fmtTime(l.ts, false)} [${l.level}] ${esc(l.msg)}</span>`).join('');
      const el = $('#logView');
      el.scrollTop = el.scrollHeight;
    }
  } catch (e) { /* ignore */ }
}

/* ------------------------------ live feed ------------------------------ */
function renderAll(payload) {
  if (!payload) return;
  if (payload.state) {
    // the WS frame carries state/metrics/curve; REST /state carries neither
    // metrics nor curve, so merge in what we have rather than blanking cards
    if (!payload.trades) payload.trades = state.trades || [];
    else state.trades = payload.trades;
    if (!payload.equity) payload.equity = state.equityCurve || [];
    else state.equityCurve = payload.equity;
    if (!payload.metrics) {
      const st = payload.state;
      payload.metrics = {
        trades: st.stats || {},
        curve: {
          return_pct: st.account?.return_pct,
          max_drawdown_pct: st.risk?.drawdown_pct,
        },
        daily: [],
        open_positions: (st.positions || []).length,
        equity: st.account?.equity,
      };
    }
  }
  state.lastState = payload;
  renderHeader(payload);
  renderTickerStrip(payload);
  renderOverview(payload);
  renderPositions(payload);
  renderTrades(payload);
  renderSignals(payload);
  renderUniverse(payload);
  renderPlan(payload);
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const venue = state.venue;
  const q = state.token ? `?token=${encodeURIComponent(state.token)}` : '';
  const ws = new WebSocket(`${proto}://${location.host}/ws/${venue}${q}`);
  state.ws = ws;
  let alive = false;
  ws.onopen = () => { alive = true; };
  ws.onmessage = ev => {
    try { renderAll(JSON.parse(ev.data)); } catch (e) { console.warn(e); }
  };
  ws.onclose = () => {
    alive = false;
    setTimeout(async () => {
      // fall back to polling while the socket is down
      try { renderAll(await vapi('/state')); } catch (e) {}
      connectWS();
    }, 2000);
  };
  ws.onerror = () => ws.close();
  setInterval(async () => {
    if (!alive) {
      try { renderAll(await vapi('/state')); } catch (e) {}
    }
  }, 5000);
}

/* ------------------------------ wiring ------------------------------ */
function initTabs() {
  $$('.tab').forEach(tab => tab.onclick = () => {
    $$('.tab').forEach(t => t.classList.remove('active'));
    $$('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    $('#tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'logs') $('#logView').scrollTop = $('#logView').scrollHeight;
  });
}

function initControls() {
  $('#pauseBtn').onclick = async () => {
    const enabled = !(state.lastState?.state?.engine?.trading_enabled);
    try {
      await vapi('/control/trading', { method: 'POST', body: JSON.stringify({ enabled }) });
      toast(enabled ? 'Trading resumed' : 'Trading paused (open positions still managed)', 'ok');
    } catch (e) { toast('Failed: ' + e.message, 'error'); }
  };
  $('#flattenBtn').onclick = async () => {
    if (!confirm('Close ALL open positions at market?')) return;
    try {
      const res = await vapi('/control/flatten', { method: 'POST' });
      toast(`Flattened ${res.closed.length} position(s)`, 'ok');
    } catch (e) { toast('Flatten failed: ' + e.message, 'error'); }
  };
  $('#tradeFilter').oninput = e => { state.tradeFilter = e.target.value; if (state.lastState) renderTrades(state.lastState); };
  $('#clearLogs').onclick = () => { state.logLines = []; $('#logView').innerHTML = ''; };

  $('#saveCreds').onclick = async () => {
    const vid = state.credVenue || state.venue;
    const api_key = $('#apiKey').value.trim(), api_secret = $('#apiSecret').value.trim();
    const passphrase = ($('#apiPassphrase')?.value || '').trim();
    if (!api_key || !api_secret) return toast('Enter both API key and secret', 'error');
    if (venueById(vid).needs_passphrase && !passphrase) return toast(venueLabel(vid) + ' also needs the API passphrase', 'error');
    const btn = $('#saveCreds'); btn.disabled = true; btn.textContent = 'Saving & verifying…';
    try {
      const res = await api(`/api/v/${vid}/credentials`, {
        method: 'POST', body: JSON.stringify({ api_key, api_secret, passphrase }),
      });
      if (res.verified) {
        $('#credResult').innerHTML = `<span class="pos">Verified ✓</span> ${esc(venueLabel(vid))} equity ${fmtMoney(res.equity)} · available ${fmtMoney(res.available)} · ${esc(res.position_mode || '')}`;
        toast(`${venueLabel(vid)} API key saved and verified`, 'ok');
      } else {
        $('#credResult').innerHTML = `<span class="neg">Saved, but verification failed:</span> ${esc(res.error || 'unknown')}`;
        toast('Key saved, but verification failed: ' + (res.error || ''), 'error');
      }
      $('#apiKey').value = ''; $('#apiSecret').value = ''; if ($('#apiPassphrase')) $('#apiPassphrase').value = '';
      await pollVenues();
      await loadSettings();
    } catch (e) { toast('Save failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Save & verify'; }
  };
  $('#testCreds').onclick = async () => {
    const vid = state.credVenue || state.venue;
    try {
      const res = await api(`/api/v/${vid}/credentials/test`, { method: 'POST' });
      $('#credResult').innerHTML = res.verified
        ? `<span class="pos">Connected ✓</span> ${esc(venueLabel(vid))} equity ${fmtMoney(res.equity)}`
        : `<span class="neg">Failed:</span> ${esc(res.error || '')}`;
    } catch (e) { toast('Test failed: ' + e.message, 'error'); }
  };
  $('#clearCreds').onclick = async () => {
    const vid = state.credVenue || state.venue;
    if (!confirm(`Delete the stored API keys for ${venueLabel(vid)}?`)) return;
    await api(`/api/v/${vid}/credentials`, { method: 'DELETE' });
    $('#credResult').textContent = venueLabel(vid) + ' credentials deleted.';
    await pollVenues();
    await loadSettings();
  };

  const scopeBox = $('#venueScope');
  if (scopeBox) scopeBox.onchange = () => {
    state.venueScope = scopeBox.checked;
    const hint = $('#scopeHint');
    if (hint) {
      hint.textContent = state.venueScope ? `writes venues.${state.venue}.*` : 'global (all venues)';
      hint.className = 'badge ' + (state.venueScope ? 'badge-warn' : 'badge-dim');
    }
  };

  $('#saveVenue').onclick = async () => {
    const patch = {};
    $$('#venueForm [data-key]').forEach(el => {
      const key = el.dataset.key, type = el.dataset.type;
      let value = el.value;
      if (type === 'bool') value = value === 'true';
      else if (type === 'number') value = Number(value);
      patch[`venues.${state.venue}.${key}`] = value;
    });
    try {
      await vapi('/settings', { method: 'PUT', body: JSON.stringify(patch) });
      toast(`${venueLabel(state.venue)} connection settings saved — restart the engine to apply`, 'ok');
      await loadSettings();
    } catch (e) { toast('Save failed: ' + e.message, 'error'); }
  };
  $('#resetVenue').onclick = async () => {
    const keys = VENUE_FIELDS.map(([k]) => `venues.${state.venue}.${k}`);
    await vapi('/settings/reset', { method: 'POST', body: JSON.stringify(keys) });
    toast(`${venueLabel(state.venue)} connection settings restored`, 'ok');
    await loadSettings();
  };

  $('#saveMode').onclick = async () => {
    const vid = state.venue;
    const patch = {
      [`venues.${vid}.mode`]: $('#cfgMode').value,
      [`venues.${vid}.paper_data_source`]: $('#cfgPaperSource').value,
      'account.paper_starting_equity': Number($('#cfgPaperEquity').value || 1000),
      [`venues.${vid}.entry_order_type`]: $('#cfgEntryType').value,
    };
    try {
      await vapi('/settings', { method: 'PUT', body: JSON.stringify(patch) });
      toast(`${venueLabel(vid)} mode saved — restarting that engine…`, 'ok');
      await api(`/api/v/${vid}/control/restart`, { method: 'POST' });
      setTimeout(() => { loadSettings(); pollVenues(); }, 2500);
    } catch (e) { toast('Failed: ' + e.message, 'error'); }
  };
  $('#restartEngine').onclick = async () => {
    await vapi('/control/restart', { method: 'POST' });
    toast('Restarting ' + venueLabel(state.venue) + '…');
  };
  $('#resetPaper').onclick = async () => {
    const eq = Number(prompt('New paper starting equity ($):', String(state.config?.account?.paper_starting_equity ?? 1000)) || 0);
    if (!eq) return;
    try {
      await vapi('/control/paper-reset', { method: 'POST', body: JSON.stringify({ equity: eq }) });
      toast('Paper account reset to ' + fmtMoney(eq), 'ok');
    } catch (e) { toast('Failed: ' + e.message, 'error'); }
  };

  $('#saveRisk').onclick = () => saveGroup('riskForm', 'Risk');
  $('#saveStrategy').onclick = () => saveGroup('strategyForm', 'Strategy');
  $('#saveUniverse').onclick = () => saveGroup('universeForm', 'Universe');
  $('#resetRisk').onclick = () => resetGroup('riskForm', null, 'Risk settings');
  $('#resetStrategy').onclick = () => resetGroup('strategyForm', null, 'Strategy settings');
  $('#resetUniverse').onclick = () => resetGroup('universeForm', null, 'Universe settings');
}

window.addEventListener('resize', () => { if (state.lastState) renderAll(state.lastState); });

(async function boot() {
  initTabs();
  initControls();
  try {
    const meta = await api('/api/venues/meta');
    state.venues = meta.venues || [];
  } catch (e) {
    state.venues = [{ id: 'mexc', label: 'MEXC Futures' }, { id: 'binance', label: 'Binance Futures' }, { id: 'kucoin', label: 'KuCoin Futures' }];
  }
  if (!state.venues.some(v => v.id === state.venue)) state.venue = state.venues[0]?.id || 'mexc';
  const brand = $('#brandVenue');
  if (brand) brand.textContent = venueLabel(state.venue);
  await pollVenues();
  try { await loadSettings(); } catch (e) { toast('Could not load settings: ' + e.message, 'error'); }
  connectWS();
  pollLogs();
  setInterval(pollLogs, 3000);
  setInterval(pollVenues, 5000);
  setInterval(pollExtras, 5000);
  try { renderAll(await vapi('/state')); } catch (e) {}
  pollExtras();
})();
