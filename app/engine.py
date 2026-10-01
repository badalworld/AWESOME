"""Trading engine: the orchestrator.

Responsibilities
----------------
* build the right broker (live MEXC / paper with live or synthetic data),
* scan the universe, subscribe to the needed streams,
* analyse closed 5m candles for AO divergence, run the anti-fake filters,
* rank signals and open positions through the executor + risk guard,
* drive mark-price ticks into the trailing-stop / stop-loss watchdog,
* snapshot equity, expose state for the dashboard, persist everything.

Concurrency model
-----------------
One asyncio loop. Ticks are coalesced per symbol and dispatched to a dedicated
risk loop so an exit is never queued behind universe scans or REST I/O. All
blocking work (SQLite) runs in threads.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional

from .analytics import compound as compound_mod
from .analytics import metrics as metrics_mod
from .config import Config
from .db import Database
from .exchange.base import Ticker
from .exchange.live import LiveBroker
from .exchange.mexc import MeXCClient, MeXCWebSocket
from .exchange.paper import MeXCPublicMarketAdapter, PaperBroker, SyntheticMarketAdapter
from .exchange.synthetic import SyntheticFeed
from .keystore import CredentialStore
from .risk.manager import RiskGuard
from .strategy.signals import Signal, SignalEngine
from .strategy.universe import UniverseScanner
from .trade.executor import Executor
from .utils import Clock, LatencyTracker, RingLogHandler

log = logging.getLogger("engine")


class TradingEngine:
    def __init__(self, cfg: Config, db: Database, keystore: CredentialStore) -> None:
        self.cfg = cfg
        self.db = db
        self.keystore = keystore
        self.clock = Clock()
        self.telemetry = LatencyTracker()
        self.ring_log = RingLogHandler(capacity=int(cfg.get("persistence.log_ring_size", 500)))

        self.broker = None
        self.executor: Optional[Executor] = None
        self.guard: Optional[RiskGuard] = None
        self.signals: Optional[SignalEngine] = None
        self.universe: Optional[UniverseScanner] = None

        self.running = False
        self.trading_enabled = True
        self.started_at = 0.0
        self.status_message = "idle"
        self.last_error = ""

        self.watchlist: List[str] = []
        self.marks: Dict[str, float] = {}
        self.tickers: Dict[str, Ticker] = {}
        self.market_data_source = "unknown"
        self.last_account = None
        self.recent_signals: List[Dict[str, Any]] = []
        self.event_log: List[Dict[str, Any]] = []
        self._tasks: List[asyncio.Task] = []
        self._tick_pending: Dict[str, float] = {}
        self._tick_busy: set = set()
        self._analysis_queue: asyncio.Queue = asyncio.Queue()
        self._last_analysis: Dict[str, float] = {}
        self._candle_ts: Dict[str, int] = {}
        self._compound_cache: Dict[str, Any] = {}
        self._compound_ts = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self.running:
            return
        cfg = self.cfg
        log.info("engine starting in %s mode", cfg.mode)
        await self._build_broker()

        self.guard = RiskGuard(cfg, self.db)
        await self.guard.load()
        self.executor = Executor(self.broker, cfg, self.db, self.guard,
                                 on_event=self._on_trade_event, clock=self.clock)
        self.signals = SignalEngine(cfg, self.db)
        self.universe = UniverseScanner(self.broker, cfg)
        self.trading_enabled = bool(await self.db.kv_get_json("trading_enabled", True))

        await self.broker.set_callbacks(
            on_kline=self._on_kline, on_tick=self._on_tick, on_order=self._on_order_push,
        )
        await self.executor.restore()
        self.running = True
        self.started_at = time.time()
        self.status_message = "running"

        self._tasks = [
            asyncio.create_task(self._universe_loop(), name="universe"),
            asyncio.create_task(self._position_sync_loop(), name="position-sync"),
            asyncio.create_task(self._analysis_worker(), name="analysis"),
            asyncio.create_task(self._fallback_analysis_loop(), name="analysis-fallback"),
            asyncio.create_task(self._equity_loop(), name="equity"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
            asyncio.create_task(self._time_sync_loop(), name="time-sync"),
        ]
        log.info("engine started (%s broker, data source: %s)", self.broker.name, self.market_data_source)

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.status_message = "stopping"
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.broker:
            await self.broker.stop()
        self.status_message = "stopped"
        log.info("engine stopped")

    # ------------------------------------------------------------------ #
    #  broker construction
    # ------------------------------------------------------------------ #
    async def _build_broker(self) -> None:
        cfg = self.cfg
        mode = cfg.mode
        rest_base = str(cfg.get("exchange.rest_base", "https://api.mexc.com"))
        ws_url = str(cfg.get("exchange.ws_url", "wss://contract.mexc.com/edge"))

        creds = await self.keystore.load() or self.keystore.snapshot()

        if mode == "live":
            if not creds or not creds.complete:
                raise RuntimeError(
                    "Live mode requires API credentials. Open the dashboard → Settings and save "
                    "your MEXC API key/secret (futures order permission enabled)."
                )
            client = MeXCClient(
                rest_base, self.clock, api_key=creds.api_key, api_secret=creds.api_secret,
                recv_window_ms=int(cfg.get("exchange.recv_window_ms", 5000)),
                timeout_s=float(cfg.get("exchange.request_timeout_s", 5.0)),
                http2=bool(cfg.get("exchange.http2", True)),
                max_connections=int(cfg.get("exchange.max_connections", 20)),
                keepalive_expiry=float(cfg.get("exchange.keepalive_expiry", 300)),
                retry_attempts=int(cfg.get("exchange.retry_attempts", 3)),
                retry_backoff_ms=int(cfg.get("exchange.retry_backoff_ms", 120)),
                telemetry=self.telemetry,
            )
            await client.start()
            try:
                await client.ping()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"Cannot reach MEXC at {rest_base} ({exc}). Live trading requires network access "
                    "to api.mexc.com (run the bot from a VPS in the same region as the exchange)."
                ) from exc
            ws = MeXCWebSocket(ws_url, client)
            self.broker = LiveBroker(
                client, ws,
                position_mode=int(cfg.get("exchange.position_mode", 1)),
                entry_order_type=str(cfg.get("exchange.entry_order_type", "market")),
                exit_order_type=str(cfg.get("exchange.exit_order_type", "market")),
                ioc_buffer_bps=float(cfg.get("exchange.ioc_limit_buffer_bps", 4)),
                stop_mode=str(cfg.get("stoploss.mode", "auto")),
                trigger_trend=2 if bool(cfg.get("stoploss.use_mark_price_trigger", True)) else 1,
                price_protect=1,
                telemetry=self.telemetry,
            )
            await self.broker.start()
            self.market_data_source = "mexc-live"
            log.info("live broker ready (position mode %s)", self.broker.position_mode)
            return

        # ---- paper mode ------------------------------------------------ #
        source = str(cfg.get("exchange.paper_data_source", "auto")).lower()
        public_client = MeXCClient(
            rest_base, self.clock,
            recv_window_ms=int(cfg.get("exchange.recv_window_ms", 5000)),
            timeout_s=min(4.0, float(cfg.get("exchange.request_timeout_s", 5.0))),
            http2=bool(cfg.get("exchange.http2", True)),
            retry_attempts=2, retry_backoff_ms=150, telemetry=self.telemetry,
        )
        use_live_data = source in ("live", "auto")
        adapter = None
        if use_live_data:
            try:
                await public_client.start()
                await asyncio.wait_for(public_client.ping(), timeout=6.0)
                adapter = MeXCPublicMarketAdapter(public_client, self.clock)
                self.market_data_source = "mexc-public"
                log.info("paper mode using LIVE MEXC public market data (orders are simulated)")
            except Exception as exc:  # noqa: BLE001
                log.warning("MEXC public data unavailable (%s) -> falling back to the synthetic feed", exc)
                try:
                    await public_client.close()
                except Exception:  # noqa: BLE001
                    pass
                adapter = None
        if adapter is None:
            if source == "live":
                raise RuntimeError("paper_data_source=live but MEXC public endpoints are unreachable")
            feed = SyntheticFeed(tick_seconds=0.5)
            adapter = SyntheticMarketAdapter(feed)
            self.market_data_source = "synthetic"
            log.warning("paper mode using SIMULATED market data (dashboard is clearly labelled)")

        self.broker = PaperBroker(
            adapter,
            starting_equity=float(cfg.get("account.paper_starting_equity", 1000.0)),
            slippage_bps=1.5,
            price_interval_s=0.2,
            clock=self.clock,
            telemetry=self.telemetry,
        )
        await self.broker.start()
        self.broker.set_close_callback(self._on_paper_close)
        # adopt simulated equity if this is a fresh database
        stored_equity = await self.db.kv_get_json("paper.equity", None)
        if stored_equity:
            self.broker.starting_equity = float(stored_equity)
            log.info("paper equity restored: $%.2f", self.broker.starting_equity)

    # ------------------------------------------------------------------ #
    #  callbacks from the broker
    # ------------------------------------------------------------------ #
    def _on_tick(self, symbol: str, mark: float, bid: float, ask: float) -> None:
        self.marks[symbol] = mark
        tk = self.tickers.get(symbol)
        if tk is not None:
            tk.last = mark
            tk.fair_price = mark
            if bid:
                tk.bid = bid
            if ask:
                tk.ask = ask
            tk.ts = time.time()
        if self.executor and symbol in self.executor.positions:
            self._tick_pending[symbol] = mark
            asyncio.get_running_loop().create_task(self._drain_tick(symbol))

    async def _drain_tick(self, symbol: str) -> None:
        """Coalesced, per-symbol serialised tick handling (exits first)."""
        if symbol in self._tick_busy:
            return
        self._tick_busy.add(symbol)
        try:
            while True:
                mark = self._tick_pending.pop(symbol, None)
                if mark is None:
                    break
                if self.executor:
                    await self.executor.handle_tick(symbol, mark)
        finally:
            self._tick_busy.discard(symbol)

    def _on_kline(self, symbol: str, interval: str, candle, is_closed: bool) -> None:
        tf = str(self.cfg.get("strategy.timeframe", "Min5"))
        if interval != tf:
            return
        prev = self._candle_ts.get(symbol, 0)
        if candle.ts > prev:
            self._candle_ts[symbol] = candle.ts
            if prev:
                # the previous candle just closed -> analyse shortly after
                self._schedule_analysis(symbol, delay=1.5)

    def _on_order_push(self, data: Dict[str, Any]) -> None:
        if self.executor:
            self.executor.notify_order_push(data)

    async def _on_paper_close(self, symbol: str, reason: str, result) -> None:
        """The paper broker flattened a position on its own trigger."""
        if not self.executor:
            return
        pos = self.executor.positions.get(symbol)
        if pos is None:
            return
        exit_price = result.price or self.marks.get(symbol, pos.entry_price)
        await self.executor.book_external_exit(symbol, reason, exit_price, source="paper-engine")

    def _on_trade_event(self, event: str, payload: Dict[str, Any]) -> None:
        self._log_event(event, payload)

    def _log_event(self, event: str, payload: Dict[str, Any]) -> None:
        self.event_log.insert(0, {"ts": time.time(), "event": event, **payload})
        del self.event_log[200:]

    # ------------------------------------------------------------------ #
    #  universe & subscriptions
    # ------------------------------------------------------------------ #
    async def _universe_loop(self) -> None:
        while self.running:
            try:
                entries = await self.universe.scan()
                symbols = [e.symbol for e in entries]
                if symbols:
                    if symbols != self.watchlist:
                        log.info("watchlist updated (%d symbols): %s", len(symbols), ", ".join(symbols[:12]) + (" ..." if len(symbols) > 12 else ""))
                    self.watchlist = symbols
                    await self.broker.subscribe(symbols, str(self.cfg.get("strategy.timeframe", "Min5")))
                    htf_tf = str(self.cfg.get("filters.htf.htf_timeframe", "Min15"))
                    if htf_tf != str(self.cfg.get("strategy.timeframe", "Min5")):
                        await self.broker.subscribe(symbols, htf_tf)
                    if self.market_data_source != "synthetic":
                        await self._refresh_tickers(symbols)
                wait = int(self.cfg.get("universe.refresh_sec", 300))
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"universe scan: {exc}"
                log.warning("universe loop error: %s", exc)
                await asyncio.sleep(30)

    async def _refresh_tickers(self, symbols: List[str]) -> None:
        try:
            all_tickers = await self.broker.tickers()
            for sym in symbols:
                if sym in all_tickers:
                    self.tickers[sym] = all_tickers[sym]
        except Exception as exc:  # noqa: BLE001
            log.debug("ticker refresh failed: %s", exc)

    async def _fallback_analysis_loop(self) -> None:
        """Guarantees analysis even if a WS candle event is missed."""
        tf_seconds = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800, "Min60": 3600}.get(
            str(self.cfg.get("strategy.timeframe", "Min5")), 300)
        while self.running:
            await asyncio.sleep(30)
            now = time.time()
            for symbol in list(self.watchlist):
                last = self._last_analysis.get(symbol, 0)
                if now - last > tf_seconds + 20:
                    self._schedule_analysis(symbol, delay=0.0)

    # ------------------------------------------------------------------ #
    #  analysis
    # ------------------------------------------------------------------ #
    def _schedule_analysis(self, symbol: str, delay: float = 0.0) -> None:
        try:
            self._analysis_queue.put_nowait((symbol, delay))
        except asyncio.QueueFull:
            pass

    async def _analysis_worker(self) -> None:
        while self.running:
            try:
                symbol, delay = await self._analysis_queue.get()
                if delay:
                    await asyncio.sleep(delay)
                await self._analyze_symbol(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"analysis: {exc}"
                log.warning("analysis worker error: %s", exc)

    async def _analyze_symbol(self, symbol: str) -> None:
        if symbol not in self.watchlist:
            return
        now = time.time()
        self._last_analysis[symbol] = now
        cfg = self.cfg
        tf = str(cfg.get("strategy.timeframe", "Min5"))
        history = int(cfg.get("strategy.candle_history", 300))

        try:
            candles = await self.broker.klines(symbol, tf, history)
        except Exception as exc:  # noqa: BLE001
            log.debug("kline fetch failed for %s: %s", symbol, exc)
            return
        if len(candles) < 60:
            return
        # drop the in-progress candle: signals are only taken on closed bars
        if len(candles) >= 2:
            candles = candles[:-1]

        htf: Dict[str, List] = {}
        htf_tf = str(cfg.get("filters.htf.htf_timeframe", "Min15"))
        for tf_name in {htf_tf, "Min15", "Min60"}:
            try:
                series = await self.broker.klines(symbol, tf_name, 160)
                htf[tf_name] = series[:-1] if len(series) >= 2 else series
            except Exception:  # noqa: BLE001
                continue

        tk = self.tickers.get(symbol)
        if tk is None:
            try:
                tk = await self.broker.ticker(symbol)
                if tk:
                    self.tickers[symbol] = tk
            except Exception:  # noqa: BLE001
                tk = None

        depth_usd = await self._depth_usd(symbol, tk)
        equity = self.last_account.equity if self.last_account else 0.0
        notional = equity * float(cfg.get("risk.equity_per_trade_pct", 8.0)) / 100.0 * int(cfg.get("risk.leverage", 10))

        signal: Optional[Signal] = await self.signals.analyze(
            symbol, candles, tk, htf_candles=htf, depth_usd=depth_usd,
            order_notional_usd=notional,
        )
        if signal is None:
            return

        # persist + broadcast
        signal.signal_id = await self.db.insert_signal({
            "ts": signal.created_at, "symbol": symbol, "side": signal.side, "kind": signal.kind,
            "score": signal.score, "price": signal.price, "status": signal.status,
            "reason": signal.reason,
            "filters": json.dumps(signal.report.to_dict(), default=str),
            "features": json.dumps(signal.features_snapshot, default=str),
        })
        self.recent_signals.insert(0, signal.to_dict())
        del self.recent_signals[100:]

        if not signal.report.passed:
            log.info("signal rejected %s %s (score %.1f): %s", signal.side, symbol, signal.score,
                     signal.reason or signal.report.rejections()[:2])
            self._log_event("signal_rejected", {"symbol": symbol, "side": signal.side,
                                                "score": signal.score, "reason": signal.reason})
            return

        log.info("signal %s %s score=%.1f price=%.8g atr=%.3f%%", signal.side, symbol,
                 signal.score, signal.price, signal.atr_pct)
        self._log_event("signal", signal.to_dict())
        if not self.trading_enabled:
            await self.db.update_signal(signal.signal_id, {"status": "rejected", "reason": "trading paused"})
            return
        await self._try_open(signal)

    async def _depth_usd(self, symbol: str, tk: Optional[Ticker]) -> float:
        """Best-effort order-book depth (real when available, else estimated)."""
        if self.market_data_source == "synthetic":
            if tk is None:
                return 0.0
            # crude proxy: ~30 seconds of the symbol's typical turnover
            return max(1_000.0, tk.amount24 / 2880.0)
        try:
            contracts = await self.broker.contracts()
            spec = contracts.get(symbol)
            if hasattr(self.broker, "client"):
                return await self.broker.client.depth_usd(symbol, 5, spec.contract_size if spec else 1.0)
        except Exception as exc:  # noqa: BLE001
            log.debug("depth fetch failed for %s: %s", symbol, exc)
        return max(1_000.0, (tk.amount24 / 2880.0) if tk else 0.0)

    async def _try_open(self, signal: Signal) -> None:
        if not self.executor or not self.guard:
            return
        account = self.last_account or await self.broker.account()
        positions = await self.broker.positions()
        margin_used = sum(p.im for p in positions)
        result = await self.executor.open_from_signal(
            signal,
            equity=account.equity,
            available=account.available,
            margin_used=margin_used,
            open_positions=len(self.executor.positions),
        )
        return result

    # ------------------------------------------------------------------ #
    #  loops
    # ------------------------------------------------------------------ #
    async def _equity_loop(self) -> None:
        interval = float(self.cfg.get("persistence.equity_snapshot_sec", 30))
        while self.running:
            try:
                account = await self.broker.account()
                self.last_account = account
                positions = self.executor.live_positions(self.marks) if self.executor else []
                realized_today = sum(
                    float(t.get("realized_pnl") or 0)
                    for t in await self.db.closed_trades_since(self._start_of_day())
                )
                await self.db.insert_equity({
                    "ts": time.time(),
                    "equity": account.equity,
                    "available": account.available,
                    "unrealized": account.unrealized,
                    "realized_today": realized_today,
                    "open_positions": len(positions),
                    "mode": self.cfg.mode,
                })
                if self.guard:
                    await self.guard.update_equity(account.equity)
                if self.cfg.mode == "paper" and hasattr(self.broker, "starting_equity"):
                    await self.db.kv_set_json("paper.equity", self.broker.starting_equity + self.broker.realized)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"equity loop: {exc}"
                log.debug("equity loop error: %s", exc)
            await asyncio.sleep(interval)

    @staticmethod
    def _start_of_day() -> float:
        now = time.gmtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, 0)) - time.timezone

    async def _position_sync_loop(self) -> None:
        """Book exits performed by the venue (stop/target/liquidation/manual)."""
        while self.running:
            try:
                if self.executor is not None:
                    await self.executor.sync_exchange_positions(self.marks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.debug("position sync error: %s", exc)
            await asyncio.sleep(2.0)

    async def _maintenance_loop(self) -> None:
        while self.running:
            await asyncio.sleep(300)
            try:
                await self.db.housekeeping(keep_days=30)
            except Exception as exc:  # noqa: BLE001
                log.debug("housekeeping failed: %s", exc)

    async def _time_sync_loop(self) -> None:
        interval = float(self.cfg.get("exchange.time_sync_interval_s", 300))
        while self.running:
            await asyncio.sleep(interval)
            try:
                if hasattr(self.broker, "client"):
                    await self.broker.client.sync_time()
                elif hasattr(self.broker, "sync_time"):
                    await self.broker.sync_time()
            except Exception as exc:  # noqa: BLE001
                log.debug("time sync failed: %s", exc)

    # ------------------------------------------------------------------ #
    #  control
    # ------------------------------------------------------------------ #
    async def set_trading_enabled(self, enabled: bool) -> bool:
        self.trading_enabled = bool(enabled)
        await self.db.kv_set_json("trading_enabled", self.trading_enabled)
        log.info("trading %s", "enabled" if enabled else "paused")
        self._log_event("control", {"trading_enabled": self.trading_enabled})
        return self.trading_enabled

    async def flatten_all(self, reason: str = "manual_flatten") -> List[Dict[str, Any]]:
        if not self.executor:
            return []
        return await self.executor.close_all(reason=reason)

    async def close_symbol(self, symbol: str, reason: str = "manual_close") -> Dict[str, Any]:
        if not self.executor:
            return {}
        return await self.executor.force_close_symbol(symbol, reason=reason)

    async def resume_risk_halt(self) -> bool:
        if self.guard:
            await self.guard.resume()
            log.info("risk halt cleared")
            return True
        return False

    # ------------------------------------------------------------------ #
    #  state for the dashboard
    # ------------------------------------------------------------------ #
    async def state(self) -> Dict[str, Any]:
        account = self.last_account
        if account is None and self.broker:
            try:
                account = await self.broker.account()
                self.last_account = account
            except Exception:  # noqa: BLE001
                account = None
        positions = self.executor.live_positions(self.marks) if self.executor else []
        trade_stats = await self.db.trade_stats()
        equity = account.equity if account else 0.0
        starting = None
        if self.cfg.mode == "paper":
            starting = float(self.cfg.get("account.paper_starting_equity", 1000.0))
        target = float(self.cfg.get("target.equity_target", 10000.0))
        days = float(self.cfg.get("target.days", 7))
        elapsed_days = max(1e-6, (time.time() - self.started_at) / 86400.0) if self.started_at else 0.0
        target_start = starting or (self.guard.day_start_equity if self.guard else equity)
        progress = 0.0
        if target_start and target > target_start:
            progress = max(0.0, min(100.0, (equity - target_start) / (target - target_start) * 100.0))

        return {
            "engine": {
                "running": self.running,
                "trading_enabled": self.trading_enabled,
                "mode": self.cfg.mode,
                "broker": self.broker.name if self.broker else "none",
                "market_data": self.market_data_source,
                "status": self.status_message,
                "started_at": self.started_at,
                "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0,
                "last_error": self.last_error,
                "watchlist": self.watchlist,
                "clock_offset_ms": round(self.clock.offset_ms, 2),
            },
            "account": {
                "equity": round(equity, 4),
                "available": round(account.available, 4) if account else 0.0,
                "unrealized": round(account.unrealized, 4) if account else 0.0,
                "position_margin": round(account.position_margin, 4) if account else 0.0,
                "currency": account.currency if account else "USDT",
                "starting_equity": starting,
                "realized_pnl": trade_stats.get("pnl", 0.0),
            },
            "positions": positions,
            "risk": self.guard.snapshot(equity) if self.guard else {},
            "stats": trade_stats,
            "target": {
                "equity_target": target,
                "days": days,
                "progress_pct": round(progress, 2),
                "elapsed_days": round(elapsed_days, 3),
                "remaining_days": round(max(0.0, days - elapsed_days), 3),
                "starting_equity": starting,
            },
            "universe": self.universe.to_dict_list() if self.universe else [],
            "signals": self.recent_signals[:50],
            "events": self.event_log[:50],
            "latency": self.telemetry.snapshot(),
            "broker_diagnostics": self.broker.diagnostics() if self.broker else {},
            "ts": time.time(),
        }

    async def compound_report(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and self._compound_cache and now - self._compound_ts < 60:
            return self._compound_cache
        account = self.last_account
        equity = account.equity if account else float(self.cfg.get("account.paper_starting_equity", 1000.0))
        trades = await self.db.get_trades(limit=400, status="CLOSED")
        rois = [float(t.get("roi_pct") or 0.0) for t in trades]
        stats = metrics_mod.compute_trade_stats(trades)
        # realised per-trade pace from actual data (fallback: configured assumption)
        tpd = 8.0
        if trades:
            span_days = max(1e-4, (time.time() - min(float(t.get("opened_at") or now) for t in trades)) / 86400.0)
            tpd = max(0.5, min(120.0, len(trades) / span_days))
        # express the *configured* stop in ROI%-space using the average ATR of recent signals
        avg_atr_pct = 0.6
        if self.recent_signals:
            vals = [float(s["atr_pct"]) for s in self.recent_signals[:50] if s.get("atr_pct")]
            if vals:
                avg_atr_pct = sum(vals) / len(vals)
        sl_roi_pct = float(self.cfg.get("stoploss.atr_multiplier", 3.0)) * avg_atr_pct * int(self.cfg.get("risk.leverage", 10))
        sl_roi_pct = max(float(self.cfg.get("stoploss.min_sl_roi_pct", 5.0)),
                         min(float(self.cfg.get("stoploss.max_sl_roi_pct", 150.0)), sl_roi_pct))
        win_rate = (stats["win_rate"] / 100.0) if stats.get("trades", 0) >= 20 else 0.45

        report = compound_mod.build_report(
            starting_equity=equity,
            target_equity=float(self.cfg.get("target.equity_target", 10000.0)),
            days=float(self.cfg.get("target.days", 7)),
            equity_pct_per_trade=float(self.cfg.get("risk.equity_per_trade_pct", 8.0)),
            leverage=int(self.cfg.get("risk.leverage", 10)),
            tp_roi_pct=float(self.cfg.get("takeprofit.tp_roi_pct", 200.0)),
            sl_roi_pct=sl_roi_pct,
            trades_per_day=tpd,
            win_rate=win_rate,
            runs=int(self.cfg.get("target.monte_carlo_runs", 20000)),
            empirical_rois=rois if len(rois) >= 20 else None,
        )
        report["assumptions"] = {
            "equity": round(equity, 2),
            "win_rate_pct": round(win_rate * 100, 2),
            "win_rate_source": "observed (>=20 trades)" if stats.get("trades", 0) >= 20 else "assumed 45% (insufficient history)",
            "sl_roi_pct_used": round(sl_roi_pct, 2),
            "avg_atr_pct_5m": round(avg_atr_pct, 4),
            "trades_per_day": round(tpd, 2),
            "sample_size": len(rois),
        }
        self._compound_cache = report
        self._compound_ts = now
        return report

    async def equity_curve(self, limit: int = 600) -> List[Dict[str, Any]]:
        return await self.db.downsample_equity(limit)

    async def metrics(self) -> Dict[str, Any]:
        trades = await self.db.get_trades(limit=1000, status="CLOSED")
        stats = metrics_mod.compute_trade_stats(trades)
        curve = await self.db.downsample_equity(600)
        account = self.last_account
        starting = float(self.cfg.get("account.paper_starting_equity", 0.0)) if self.cfg.mode == "paper" else 0.0
        curve_stats = metrics_mod.compute_curve_stats(curve, starting_equity=starting)
        return {
            "trades": stats,
            "curve": curve_stats,
            "daily": metrics_mod.daily_returns(curve)[-30:],
            "open_positions": len(self.executor.positions) if self.executor else 0,
            "equity": round(account.equity, 4) if account else starting,
            "latency": self.telemetry.snapshot(),
            "order_latency": await self.db.latency_stats(),
        }

    async def apply_credentials(self, api_key: str, api_secret: str) -> Dict[str, Any]:
        """Store credentials, verify them against MEXC, and hot-swap the client."""
        await self.keystore.save(api_key, api_secret)
        result = {"saved": True, "verified": False}
        probe = MeXCClient(
            str(self.cfg.get("exchange.rest_base", "https://api.mexc.com")),
            self.clock, api_key=api_key, api_secret=api_secret,
            timeout_s=6.0, retry_attempts=1, telemetry=self.telemetry,
        )
        try:
            await probe.start()
            assets = await probe.assets()
            usdt = next((a for a in assets if str(a.get("currency", "")).upper() == "USDT"), None)
            result.update({
                "verified": True,
                "equity": float(usdt.get("equity") or 0.0) if usdt else 0.0,
                "available": float(usdt.get("availableBalance") or 0.0) if usdt else 0.0,
            })
            if self.broker is not None and hasattr(self.broker, "client"):
                self.broker.client.update_credentials(api_key, api_secret)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        finally:
            try:
                await probe.close()
            except Exception:  # noqa: BLE001
                pass
        return result
