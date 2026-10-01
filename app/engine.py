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
import math
import time
from typing import Any, Dict, List, Optional

from .analytics import compound as compound_mod
from .analytics import metrics as metrics_mod
from .config import Config, VenueConfig
from .db import Database
from .exchange.base import Ticker
from .exchange.binance import BinanceClient, BinanceStream
from .exchange.kucoin import KuCoinClient, KuCoinStream
from .exchange.live import LiveBroker
from .exchange.mexc import MeXCClient, MeXCWebSocket, MexcVenueClient
from .exchange.paper import PaperBroker, SyntheticMarketAdapter, VenuePublicMarketAdapter
from .exchange.synthetic import SyntheticFeed
from .exchange.venue import VenueSpec, get_venue
from .keystore import CredentialStore
from .risk.manager import RiskGuard
from .strategy.signals import Signal, SignalEngine
from .strategy.universe import UniverseScanner
from .trade.executor import Executor
from .utils import Clock, LatencyTracker, RingLogHandler

log = logging.getLogger("engine")


class TradingEngine:
    """One engine == one venue == one account == one database.

    Three engines (MEXC / Binance / KuCoin) run side by side in the same
    process: separate broker, positions, P&L, credentials and SQLite file. The
    *strategy, filter and risk code is the same object graph* for all three,
    which is what guarantees identical rules on every venue.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        keystore: CredentialStore,
        venue_id: str = "mexc",
        *,
        base_cfg: Optional[Config] = None,
    ) -> None:
        self.venue_id = str(venue_id).lower()
        self.spec: VenueSpec = get_venue(self.venue_id)
        self.base_cfg: Config = base_cfg or cfg
        self.cfg = cfg if isinstance(cfg, VenueConfig) else VenueConfig(cfg, self.venue_id)
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
        # Fixed anchor: the balance this book started with. Set once and then
        # never moved by the bot (restarts / deposits / config edits leave it
        # alone); only an explicit paper reset re-anchors it.
        self.starting_balance: Optional[float] = None
        self.recent_signals: List[Dict[str, Any]] = []
        self.event_log: List[Dict[str, Any]] = []
        self._tasks: List[asyncio.Task] = []
        self._tick_pending: Dict[str, float] = {}
        self._tick_tasks: Dict[str, asyncio.Task] = {}
        self._entry_tasks: set[asyncio.Task] = set()
        self._analysis_queue: asyncio.Queue = asyncio.Queue()
        self._last_analysis: Dict[str, float] = {}
        self._candle_ts: Dict[str, int] = {}
        self._compound_cache: Dict[str, Any] = {}
        self._compound_ts = 0.0
        self._lifecycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    #  lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        async with self._lifecycle_lock:
            await self._start_safely()

    async def restart(self) -> None:
        # One lock spans stop AND start: two dashboard requests cannot build
        # overlapping clients or let shutdown race a half-finished restart.
        async with self._lifecycle_lock:
            await self._stop()
            await self._start_safely()

    async def _start_safely(self) -> None:
        try:
            await self._start()
        except (Exception, asyncio.CancelledError):
            self.running = False
            self.status_message = "start failed"
            if self.broker is not None:
                try:
                    await self.broker.stop()
                except Exception:
                    log.exception("broker cleanup after failed startup failed")
            raise

    async def _start(self) -> None:
        if self.running:
            return
        cfg = self.cfg
        log.info("engine starting in %s mode", cfg.mode)
        self._tick_pending.clear()
        self._tick_tasks.clear()
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
        self.starting_balance = await self.db.kv_get_json("account.starting_balance", None)
        if self.starting_balance is not None:
            self.starting_balance = float(self.starting_balance)
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
        async with self._lifecycle_lock:
            await self._stop()

    async def _stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.status_message = "stopping"
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        # An entry may already have reached the venue. Finish protection and
        # persistence instead of cancelling it between fill and stop placement.
        await asyncio.gather(*self._entry_tasks, return_exceptions=True)
        await asyncio.gather(*self._tick_tasks.values(), return_exceptions=True)
        self._tick_pending.clear()
        if self.broker:
            await self.broker.stop()
        self.status_message = "stopped"
        log.info("engine stopped")

    # ------------------------------------------------------------------ #
    #  broker construction
    # ------------------------------------------------------------------ #
    async def _build_broker(self) -> None:
        """Build the live or paper broker for *this* venue."""
        cfg = self.cfg
        spec = self.spec
        mode = cfg.mode
        rest_base = str(cfg.get("exchange.rest_base", spec.rest_base))
        ws_url = str(cfg.get("exchange.ws_url", spec.ws_url))
        creds = await self.keystore.load() or self.keystore.snapshot()
        have_creds = bool(creds and creds.complete(spec.needs_passphrase))

        if mode == "live":
            self._assert_live_safety()
            if not have_creds:
                raise RuntimeError(
                    f"Live mode on {spec.label} requires API credentials. Open the dashboard -> "
                    f"{spec.label} tab -> Settings and save the API key"
                    + ("/secret/passphrase" if spec.needs_passphrase else "/secret")
                    + " (futures trading permission enabled)."
                )
            client = self._make_client(rest_base, creds.api_key, creds.api_secret,
                                       getattr(creds, "passphrase", ""))
            try:
                await client.start()
                await client.ping()
            except asyncio.CancelledError:
                await client.close()
                raise
            except Exception as exc:  # noqa: BLE001
                await client.close()
                raise RuntimeError(
                    f"Cannot reach {spec.label} at {rest_base} ({exc}). Live trading requires "
                    "network access to the exchange (run the bot from a VPS in the same region)."
                ) from exc
            broker = LiveBroker(
                client,
                self._make_stream(client, ws_url),
                position_mode=int(cfg.get("exchange.position_mode", 1)),
                stop_mode=str(cfg.get("stoploss.mode", "auto")),
                telemetry=self.telemetry,
            )
            self.broker = broker
            await broker.start()
            self.market_data_source = "live"
            log.info("[%s] live broker ready (position mode %s)", spec.id, broker.position_mode)
            return

        # ---- paper mode ------------------------------------------------ #
        source = str(cfg.get("exchange.paper_data_source", "auto")).lower()
        adapter = None
        if source in ("live", "auto"):
            public_client = self._make_client(rest_base, None, None, "")
            try:
                await public_client.start()
                await asyncio.wait_for(public_client.ping(), timeout=6.0)
                adapter = VenuePublicMarketAdapter(public_client, self.clock)
                self.market_data_source = "public"
                log.info("[%s] paper mode using LIVE public market data (orders are simulated)", spec.id)
            except asyncio.CancelledError:
                await public_client.close()
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] public data unavailable (%s) -> falling back to the synthetic feed",
                            spec.id, exc)
                try:
                    await public_client.close()
                except Exception:  # noqa: BLE001
                    pass
                adapter = None
        if adapter is None:
            if source == "live":
                raise RuntimeError(
                    f"paper_data_source=live but {spec.label} public endpoints are unreachable"
                )
            feed = SyntheticFeed(tick_seconds=0.5, symbol_style=spec.symbol_style)
            adapter = SyntheticMarketAdapter(feed)
            self.market_data_source = "synthetic"
            log.warning("[%s] paper mode using SIMULATED market data (dashboard is clearly labelled)", spec.id)

        self.broker = PaperBroker(
            adapter,
            name=f"{spec.id}-paper",
            starting_equity=float(cfg.get("account.paper_starting_equity", 1000.0)),
            slippage_bps=1.5,
            price_interval_s=0.2,
            clock=self.clock,
            telemetry=self.telemetry,
        )
        await self.broker.start()
        self.broker.set_close_callback(self._on_paper_close)
        stored_equity = await self.db.kv_get_json("paper.equity", None)
        if stored_equity:
            self.broker.starting_equity = float(stored_equity)
            log.info("[%s] paper equity restored: $%.2f", spec.id, self.broker.starting_equity)

    def _assert_live_safety(self) -> None:
        """Refuse to arm real money behind an unauthenticated dashboard.

        Live trading with an API token-less control panel that listens on a
        non-loopback address means anyone who can reach the port can flatten the
        account. That combination is blocked unless the operator explicitly opts
        out with ``web.allow_insecure_live = true``.
        """
        token = str(self.cfg.get("web.api_token", "") or "")
        host = str(self.cfg.get("web.host", "0.0.0.0") or "0.0.0.0").strip().lower()
        allow = bool(self.cfg.get("web.allow_insecure_live", False))
        local_only = host in ("127.0.0.1", "localhost", "::1")
        if not token and not local_only and not allow:
            raise RuntimeError(
                f"Refusing to start {self.spec.label} in LIVE mode: the dashboard has no "
                f"web.api_token and is bound to {host}, so anyone who can reach the port could "
                "trade your account. Set web.api_token (recommended), or bind the dashboard to "
                "127.0.0.1 behind an authenticated reverse proxy, or set "
                "web.allow_insecure_live = true to override."
            )

    # -- starting balance anchor ----------------------------------------- #
    async def ensure_starting_balance(self, equity: Optional[float] = None,
                                      *, force: Optional[float] = None) -> float:
        """Return the *fixed* starting balance for this venue's book.

        The dashboard shows this as "starting balance"; it is deliberately
        sticky, because a starting balance that silently follows the account
        makes every return/PnL figure meaningless.  It is written once, on the
        first account snapshot of a fresh book (paper: the configured opening
        equity; live: the first observed equity), then survives restarts,
        deposits/withdrawals and config edits.

        ``force`` re-anchors it -- used by the explicit paper reset, which by
        definition starts a new book.
        """
        if force is not None:
            self.starting_balance = float(force)
            await self.db.kv_set_json("account.starting_balance", self.starting_balance)
            return self.starting_balance
        if self.starting_balance is None:
            anchor: Optional[float] = None
            if self.cfg.mode == "paper":
                # the paper book's own opening equity (restored from kv, else config)
                from_broker = getattr(self.broker, "starting_equity", None)
                anchor = float(from_broker) if from_broker else float(
                    self.cfg.get("account.paper_starting_equity", 1000.0)
                )
            elif equity and equity > 0:
                anchor = float(equity)
            if anchor is not None:
                self.starting_balance = anchor
                await self.db.kv_set_json("account.starting_balance", anchor)
                log.info("[%s] starting balance anchored at $%.2f", self.spec.id, anchor)
        return float(self.starting_balance or 0.0)

    # -- venue factories ------------------------------------------------- #
    def _make_client(self, rest_base: str, api_key, api_secret, passphrase):
        """Build the REST client for this venue with the shared transport options."""
        cfg = self.cfg
        common = dict(
            rest_base=rest_base,
            api_key=api_key,
            api_secret=api_secret,
            timeout_s=float(cfg.get("exchange.request_timeout_s", 5.0)),
            http2=bool(cfg.get("exchange.http2", True)),
            max_connections=int(cfg.get("exchange.max_connections", 20)),
            keepalive_expiry=float(cfg.get("exchange.keepalive_expiry", 300)),
            retry_attempts=int(cfg.get("exchange.retry_attempts", 3)),
            retry_backoff_ms=int(cfg.get("exchange.retry_backoff_ms", 120)),
            telemetry=self.telemetry,
        )
        if self.venue_id == "mexc":
            raw = MeXCClient(
                rest_base, self.clock, api_key=api_key, api_secret=api_secret,
                recv_window_ms=int(cfg.get("exchange.recv_window_ms", 5000)),
                timeout_s=common["timeout_s"], http2=common["http2"],
                max_connections=common["max_connections"],
                keepalive_expiry=common["keepalive_expiry"],
                retry_attempts=common["retry_attempts"],
                retry_backoff_ms=common["retry_backoff_ms"],
                telemetry=self.telemetry,
            )
            return MexcVenueClient(raw, position_mode=int(cfg.get("exchange.position_mode", 1)))
        if self.venue_id == "binance":
            return BinanceClient(self.clock, recv_window_ms=int(cfg.get("exchange.recv_window_ms", 5000)), **common)
        if self.venue_id == "kucoin":
            return KuCoinClient(self.clock, passphrase=passphrase, **common)
        raise KeyError(f"no client implementation for venue {self.venue_id}")

    def _make_stream(self, client, ws_url: str):
        if self.venue_id == "mexc":
            raw = getattr(client, "raw", None)
            return MeXCWebSocket(ws_url, raw) if raw is not None else None
        if self.venue_id == "binance":
            return BinanceStream(client, ws_base=ws_url)
        if self.venue_id == "kucoin":
            return KuCoinStream(client, ws_base=ws_url)
        return None

    # ------------------------------------------------------------------ #
    #  callbacks from the broker
    # ------------------------------------------------------------------ #
    def _on_tick(self, symbol: str, mark: float, bid: float, ask: float) -> None:
        if not self.running or not math.isfinite(mark) or mark <= 0:
            return
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
            # Coalescing may discard intermediate prices while REST I/O is in
            # flight; never discard their peak/trough for the trailing ratchet.
            pos = self.executor.positions[symbol]
            roi = pos.roi_at(mark)
            pos.peak_roi_pct = max(pos.peak_roi_pct, roi)
            pos.trough_roi_pct = min(pos.trough_roi_pct, roi)
            self._tick_pending[symbol] = mark
            if symbol not in self._tick_tasks:
                self._tick_tasks[symbol] = asyncio.create_task(self._drain_tick(symbol))

    async def _drain_tick(self, symbol: str) -> None:
        """Coalesced, per-symbol serialised tick handling (exits first)."""
        try:
            while True:
                mark = self._tick_pending.pop(symbol, None)
                if mark is None:
                    break
                if self.executor:
                    await self.executor.handle_tick(symbol, mark)
        finally:
            self._tick_tasks.pop(symbol, None)

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

    async def _try_open(self, signal: Signal):
        if not self.executor or not self.guard:
            return
        task = asyncio.create_task(self.executor.open_from_signal(signal))
        self._entry_tasks.add(task)
        task.add_done_callback(self._entry_tasks.discard)
        return await asyncio.shield(task)

    # ------------------------------------------------------------------ #
    #  loops
    # ------------------------------------------------------------------ #
    async def _equity_loop(self) -> None:
        interval = float(self.cfg.get("persistence.equity_snapshot_sec", 30))
        while self.running:
            try:
                account = await self.broker.account()
                self.last_account = account
                await self.ensure_starting_balance(account.equity)
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
        if self.executor and await self.executor.pending_entry() is not None:
            if self.guard:
                await self.guard.halt("unresolved entry journal; operator reconciliation required")
            return False
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
        starting = await self.ensure_starting_balance(equity if equity > 0 else None)
        if not starting:
            starting = (
                float(self.cfg.get("account.paper_starting_equity", 1000.0))
                if self.cfg.mode == "paper" else equity
            )
        # NOTE: ``db.trade_stats()`` sums into ``total_pnl``; the analytics module's
        # ``compute_trade_stats()`` uses ``pnl``. Mixing them up silently reported
        # a $0 realised P&L on the dashboard header.
        released_pnl = float(trade_stats.get("total_pnl") or 0.0)
        open_pnl = float(account.unrealized) if account else 0.0
        win_rate = float(trade_stats.get("win_rate") or 0.0)
        target = float(self.cfg.get("target.equity_target", 10000.0))
        days = float(self.cfg.get("target.days", 7))
        elapsed_days = max(1e-6, (time.time() - self.started_at) / 86400.0) if self.started_at else 0.0
        target_start = starting or (self.guard.day_start_equity if self.guard else equity)
        progress = 0.0
        if target_start and target > target_start:
            progress = max(0.0, min(100.0, (equity - target_start) / (target - target_start) * 100.0))

        return {
            "venue": {
                "id": self.spec.id,
                "label": self.spec.label,
                "rest_base": str(self.cfg.get("exchange.rest_base", self.spec.rest_base)),
                "quote": self.spec.quote,
                "taker_fee": float(self.cfg.get(f"venues.{self.spec.id}.taker_fee", self.spec.taker_fee)),
                "needs_passphrase": self.spec.needs_passphrase,
                "docs": self.spec.docs,
            },
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
                # fixed anchor (never moves on its own) + the two PnL halves
                "starting_balance": round(starting, 4),
                "starting_equity": round(starting, 4),
                "released_pnl": round(released_pnl, 4),      # closed trades ("released")
                "realized_pnl": round(released_pnl, 4),      # alias, kept for the API
                "open_pnl": round(open_pnl, 4),              # still in the market
                "total_pnl": round(released_pnl + open_pnl, 4),
                "return_pct": round((equity - starting) / starting * 100.0, 3) if starting else 0.0,
                "win_rate": round(win_rate, 2),
                "trades": int(trade_stats.get("trades") or 0),
                "wins": int(trade_stats.get("wins") or 0),
                "losses": int(trade_stats.get("losses") or 0),
            },
            "positions": positions,
            "pending_entry": await self.executor.pending_entry() if self.executor else None,
            "risk": self.guard.snapshot(equity) if self.guard else {},
            "stats": trade_stats,
            "target": {
                "equity_target": target,
                "days": days,
                "progress_pct": round(progress, 2),
                "elapsed_days": round(elapsed_days, 3),
                "remaining_days": round(max(0.0, days - elapsed_days), 3),
                "starting_equity": round(starting, 4),
                "starting_balance": round(starting, 4),
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
        equity = account.equity if account else 0.0
        starting = await self.ensure_starting_balance(equity if equity > 0 else None)
        if not starting:
            starting = (
                float(self.cfg.get("account.paper_starting_equity", 0.0))
                if self.cfg.mode == "paper" else equity
            )
        curve_stats = metrics_mod.compute_curve_stats(curve, starting_equity=starting)
        return {
            "trades": stats,
            "curve": curve_stats,
            "daily": metrics_mod.daily_returns(curve)[-30:],
            "open_positions": len(self.executor.positions) if self.executor else 0,
            "equity": round(equity, 4) if account else round(starting, 4),
            "starting_balance": round(starting, 4),
            "released_pnl": round(float(stats.get("pnl") or 0.0), 4),
            "open_pnl": round(float(account.unrealized), 4) if account else 0.0,
            "win_rate": round(float(stats.get("win_rate") or 0.0), 2),
            "latency": self.telemetry.snapshot(),
            "order_latency": await self.db.latency_stats(),
        }

    async def apply_credentials(self, api_key: str, api_secret: str,
                                passphrase: str = "") -> Dict[str, Any]:
        """Store credentials, verify them against the venue, hot-swap the client."""
        spec = self.spec
        await self.keystore.save(api_key, api_secret, passphrase)
        result: Dict[str, Any] = {"saved": True, "verified": False, "venue": spec.id}
        probe = self._make_client(str(self.cfg.get("exchange.rest_base", spec.rest_base)),
                                  api_key, api_secret, passphrase)
        try:
            await probe.start()
            await probe.ping()
            account = await probe.account()
            result.update({
                "verified": True,
                "equity": round(account.equity, 4),
                "available": round(account.available, 4),
                "position_mode": "hedge" if await probe.position_mode() == 1 else "one-way",
            })
            if self.broker is not None and hasattr(self.broker, "client"):
                self.broker.client.update_credentials(api_key, api_secret, passphrase)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        finally:
            try:
                await probe.close()
            except Exception:  # noqa: BLE001
                pass
        return result
