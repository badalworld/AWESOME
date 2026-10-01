/* =========================================================================
   AO Divergence Bot — dashboard client
   Vanilla JS, no build step, no external deps (charts are hand-rolled SVG).
   ========================================================================= */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  config: null,
  credentials: null,
  lastState: null,
  logSeq: 0,
  logLines: [],
  tradeFilter: '',
};

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

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ------------------------------ charts ------------------------------ */
function lineChart(el, series, opts = {}) {
  const w = el.clientWidth || 600, h = el.clientHeight || 240;
  const pad = { l: 52, r: 14, t: 12, b: 22 };
  const all = series.flatMap(s => s.data);
  if (!all.length) { el.innerHTML = '<div class="muted" style="padding:24px">no data yet</div>'; return; }
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  min -= span * 0.08; max += span * 0.08;
  const n = Math.max(2, series[0].data.length);
  const X = i => pad.l + (i / (n - 1)) * (w - pad.l - pad.r);
  const Y = v => pad.t + (1 - (v - min) / (max - min)) * (h - pad.t - pad.b);

  let g = '';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (i / 4) * (h - pad.t - pad.b);
    const val = max - (i / 4) * (max - min);
    g += `<line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${y}" y2="${y}"/>`;
    g += `<text class="axis-label" x="6" y="${y + 3}">${fmtNum(val, 2)}</text>`;
  }
  let paths = '';
  series.forEach((s, si) => {
    const d = s.data.map((v, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    const color = s.color || '#4f8cff';
    const dash = s.dash ? 'stroke-dasharray="4 4"' : '';
    paths += `<path d="${d}" fill="none" stroke="${color}" stroke-width="${s.width || 2}" ${dash}/>`;
    if (s.fill) {
      paths += `<path d="${d} L${X(s.data.length - 1).toFixed(1)},${(h - pad.b).toFixed(1)} L${X(0).toFixed(1)},${(h - pad.b).toFixed(1)} Z" fill="${color}" opacity="0.08"/>`;
    }
  });
  const markers = (opts.markers || []).map(m => `<line x1="${X(m.i)}" x2="${X(m.i)}" y1="${pad.t}" y2="${h - pad.b}" stroke="${m.color}" stroke-width="1" stroke-dasharray="3 3"/>`);
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${g}${paths}${markers.join('')}</svg>`;
}

function barChart(el, values, opts = {}) {
  const w = el.clientWidth || 600, h = el.clientHeight || 240;
  const pad = { l: 44, r: 12, t: 12, b: 26 };
  if (!values.length) { el.innerHTML = '<div class="muted" style="padding:24px">no closed trades yet</div>'; return; }
  const maxAbs = Math.max(1, ...values.map(v => Math.abs(v)));
  const bw = (w - pad.l - pad.r) / values.length;
  let bars = '';
  values.forEach((v, i) => {
    const x = pad.l + i * bw + bw * 0.15;
    const bh = (Math.abs(v) / maxAbs) * (h - pad.t - pad.b) * 0.5;
    const zero = pad.t + (h - pad.t - pad.b) * 0.5;
    const y = v >= 0 ? zero - bh : zero;
    const color = v >= 0 ? 'var(--green)' : 'var(--red)';
    bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${(bw * 0.7).toFixed(1)}" height="${Math.max(1, bh).toFixed(1)}" fill="${color}" opacity="0.85" rx="2"/>`;
  });
  const zero = pad.t + (h - pad.t - pad.b) * 0.5;
  el.innerHTML = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <line class="grid-line" x1="${pad.l}" x2="${w - pad.r}" y1="${zero}" y2="${zero}"/>
    <text class="axis-label" x="6" y="${zero + 3}">0%</text>
    <text class="axis-label" x="2" y="${pad.t + 10}">+${maxAbs.toFixed(0)}%</text>
    <text class="axis-label" x="2" y="${h - pad.b}">-${maxAbs.toFixed(0)}%</text>
    ${bars}</svg>`;
}

const HIST_COLORS = { green: 'var(--green)', red: 'var(--red)' };

/* ------------------------------ overview ------------------------------ */
function renderOverview(d) {
  const acct = d.state.account, stats = d.metrics.trades, engine = d.state.engine;
  $('#kEquity').textContent = fmtMoney(acct.equity);
  $('#kEquitySub').textContent = `available ${fmtMoney(acct.available)} · unrealized ${fmtMoney(acct.unrealized)}`;
  $('#kPnl').textContent = fmtMoney(stats.pnl);
  $('#kPnl').className = 'kpi ' + cls(stats.pnl);
  $('#kPnlSub').textContent = `${stats.trades} closed · fees ${fmtMoney(stats.total_fees)}`;
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
  lineChart($('#equityChart'), [{ data: curve.length ? curve : [acct.equity], color: '#4f8cff', fill: true }]);
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
      await api('/api/control/close', { method: 'POST', body: JSON.stringify({ symbol: b.dataset.close }) });
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
    data: p, color: ['#4f8cff', '#7c5cff', '#22c55e', '#f59e0b', '#06b6d4', '#ec4899'][i % 6], width: 1.4,
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

/* ------------------------------ header ------------------------------ */
function renderHeader(d) {
  const engine = d.state.engine, acct = d.state.account;
  const modeBadge = $('#modeBadge');
  modeBadge.textContent = state.config?.app?.mode === 'live' ? 'LIVE' : 'PAPER';
  modeBadge.className = 'badge ' + (state.config?.app?.mode === 'live' ? 'badge-live' : 'badge-paper');

  const db = $('#dataBadge');
  const md = engine.market_data || 'unknown';
  db.textContent = md === 'synthetic' ? 'SIMULATED DATA' : (md === 'mexc-public' ? 'MEXC public data' : 'MEXC live');
  db.className = 'badge ' + (md === 'synthetic' ? 'badge-paper' : 'badge-dim');

  const sb = $('#statusBadge');
  const connected = (d.state.broker_diagnostics?.ws?.connected !== false) && engine.running;
  sb.textContent = engine.running ? (engine.trading_enabled ? 'running' : 'paused') : 'stopped';
  sb.className = 'badge ' + (engine.running && engine.trading_enabled ? 'badge-ok' : 'badge-dim');

  const halt = $('#haltBadge');
  if (d.state.risk?.halted) { halt.classList.remove('hidden'); halt.textContent = 'HALTED: ' + (d.state.risk.halt_reason || ''); }
  else halt.classList.add('hidden');

  $('#topEquity').textContent = fmtMoney(acct.equity);
  $('#topDay').textContent = fmtPct(d.state.risk?.day_pnl_pct);
  $('#topDay').className = cls(d.state.risk?.day_pnl_pct);
  $('#topOpen').textContent = d.state.positions.length;
  $('#topPnl').textContent = fmtMoney(acct.realized_pnl);
  $('#topPnl').className = cls(acct.realized_pnl);
  $('#pauseBtn').textContent = engine.trading_enabled ? 'Pause' : 'Resume';
  $('#pauseBtn').className = 'btn ' + (engine.trading_enabled ? 'btn-ghost' : '');

  const t = d.state.target || {};
  const start = t.starting_equity || acct.equity || 1;
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

function buildForms() {
  const cfg = state.config || {};
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

async function saveGroup(formId, label) {
  try {
    const res = await api('/api/settings', { method: 'PUT', body: JSON.stringify(collectPatch(formId)) });
    toast(`${label} saved (${Object.keys(res.applied).length} settings)${res.restart_required ? ' — restart the engine to apply' : ''}`, 'ok');
    await loadSettings();
  } catch (e) { toast('Save failed: ' + e.message, 'error'); }
}

async function resetGroup(formId, keys, label) {
  try {
    await api('/api/settings/reset', { method: 'POST', body: JSON.stringify(keys) });
    toast(`${label} restored to defaults`, 'ok');
    await loadSettings();
  } catch (e) { toast('Reset failed: ' + e.message, 'error'); }
}

async function loadSettings() {
  const data = await api('/api/settings');
  state.config = data.config;
  state.credentials = data.credentials;
  buildForms();
  const cfg = data.config;
  $('#cfgMode').value = cfg.app.mode;
  $('#cfgPaperSource').value = cfg.exchange.paper_data_source || 'auto';
  $('#cfgPaperEquity').value = cfg.account.paper_starting_equity;
  $('#cfgEntryType').value = cfg.exchange.entry_order_type;
  const cs = $('#credState');
  if (data.credentials.configured) {
    cs.textContent = 'configured · ' + data.credentials.api_key_preview;
    cs.className = 'badge badge-ok';
  } else {
    cs.textContent = 'not configured';
    cs.className = 'badge badge-dim';
  }
}

/* ------------------------------ logs ------------------------------ */
async function pollLogs() {
  try {
    const res = await api('/api/logs?after=' + state.logSeq + '&limit=200');
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
  state.lastState = payload;
  renderHeader(payload);
  renderOverview(payload);
  renderPositions(payload);
  renderTrades(payload);
  renderSignals(payload);
  renderUniverse(payload);
  renderPlan(payload);
}

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  let alive = false;
  ws.onopen = () => { alive = true; };
  ws.onmessage = ev => {
    try { renderAll(JSON.parse(ev.data)); } catch (e) { console.warn(e); }
  };
  ws.onclose = () => {
    alive = false;
    setTimeout(async () => {
      // fall back to polling while the socket is down
      try { renderAll(await api('/api/state')); } catch (e) {}
      connectWS();
    }, 2000);
  };
  ws.onerror = () => ws.close();
  setInterval(async () => {
    if (!alive) {
      try { renderAll(await api('/api/state')); } catch (e) {}
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
      await api('/api/control/trading', { method: 'POST', body: JSON.stringify({ enabled }) });
      toast(enabled ? 'Trading resumed' : 'Trading paused (open positions still managed)', 'ok');
    } catch (e) { toast('Failed: ' + e.message, 'error'); }
  };
  $('#flattenBtn').onclick = async () => {
    if (!confirm('Close ALL open positions at market?')) return;
    try {
      const res = await api('/api/control/flatten', { method: 'POST' });
      toast(`Flattened ${res.closed.length} position(s)`, 'ok');
    } catch (e) { toast('Flatten failed: ' + e.message, 'error'); }
  };
  $('#tradeFilter').oninput = e => { state.tradeFilter = e.target.value; if (state.lastState) renderTrades(state.lastState); };
  $('#clearLogs').onclick = () => { state.logLines = []; $('#logView').innerHTML = ''; };

  $('#saveCreds').onclick = async () => {
    const api_key = $('#apiKey').value.trim(), api_secret = $('#apiSecret').value.trim();
    if (!api_key || !api_secret) return toast('Enter both API key and secret', 'error');
    const btn = $('#saveCreds'); btn.disabled = true; btn.textContent = 'Saving & verifying…';
    try {
      const res = await api('/api/credentials', { method: 'POST', body: JSON.stringify({ api_key, api_secret }) });
      if (res.verified) {
        $('#credResult').innerHTML = `<span class="pos">Verified ✓</span> equity ${fmtMoney(res.equity)} · available ${fmtMoney(res.available)}`;
        toast('API key saved and verified against MEXC', 'ok');
      } else {
        $('#credResult').innerHTML = `<span class="neg">Saved, but verification failed:</span> ${esc(res.error || 'unknown')}`;
        toast('Key saved, but verification failed: ' + (res.error || ''), 'error');
      }
      $('#apiKey').value = ''; $('#apiSecret').value = '';
      await loadSettings();
    } catch (e) { toast('Save failed: ' + e.message, 'error'); }
    finally { btn.disabled = false; btn.textContent = 'Save & verify'; }
  };
  $('#testCreds').onclick = async () => {
    try {
      const res = await api('/api/credentials/test', { method: 'POST' });
      $('#credResult').innerHTML = res.verified
        ? `<span class="pos">Connected ✓</span> equity ${fmtMoney(res.equity)}`
        : `<span class="neg">Failed:</span> ${esc(res.error || '')}`;
    } catch (e) { toast('Test failed: ' + e.message, 'error'); }
  };
  $('#clearCreds').onclick = async () => {
    if (!confirm('Delete stored API keys?')) return;
    await api('/api/credentials', { method: 'DELETE' });
    $('#credResult').textContent = 'Credentials deleted.';
    await loadSettings();
  };

  $('#saveMode').onclick = async () => {
    const patch = {
      'app.mode': $('#cfgMode').value,
      'exchange.paper_data_source': $('#cfgPaperSource').value,
      'account.paper_starting_equity': Number($('#cfgPaperEquity').value || 1000),
      'exchange.entry_order_type': $('#cfgEntryType').value,
    };
    try {
      const res = await api('/api/settings', { method: 'PUT', body: JSON.stringify(patch) });
      toast('Mode settings saved — restarting engine…', 'ok');
      await api('/api/control/restart', { method: 'POST' });
      setTimeout(loadSettings, 2500);
    } catch (e) { toast('Failed: ' + e.message, 'error'); }
  };
  $('#restartEngine').onclick = async () => {
    await api('/api/control/restart', { method: 'POST' });
    toast('Engine restarting…');
  };
  $('#resetPaper').onclick = async () => {
    const eq = Number(prompt('New paper starting equity ($):', String(state.config?.account?.paper_starting_equity ?? 1000)) || 0);
    if (!eq) return;
    try {
      await api('/api/control/paper-reset', { method: 'POST', body: JSON.stringify({ equity: eq }) });
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
  try { await loadSettings(); } catch (e) { toast('Could not load settings: ' + e.message, 'error'); }
  connectWS();
  pollLogs();
  setInterval(pollLogs, 3000);
  try { renderAll(await api('/api/state')); } catch (e) {}
})();
