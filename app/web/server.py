"""FastAPI application: multi-venue REST control plane + WebSocket + dashboard.

Every trading route exists twice:

* ``/api/v/{venue}/…``   — the venue-scoped form (used by the three dashboard tabs)
* ``/api/…``             — a backwards-compatible alias that targets the
                           *primary* venue (``app.primary_venue``, default MEXC),
                           and accepts ``?venue=binance`` as a shortcut.

The handlers are literally the same functions, registered under both paths, so
the two surfaces can never drift apart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from ..config import VALIDATORS, Config
from ..exchange.venue import VENUES
from ..manager import VenueContext, VenueManager

log = logging.getLogger("web")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(manager: VenueManager, cfg: Config) -> FastAPI:
    app = FastAPI(title="AO Divergence Multi-Venue Futures Bot", version="2.0.0", docs_url="/api/docs")
    # The dashboard is same-origin, so CORS is off by default. A wildcard with
    # credentials is both invalid per spec and a needless attack surface for a
    # control panel that can place orders; list explicit origins to enable it.
    _origins = [o.strip() for o in (cfg.get("web.allow_origins", "") or "").split(",") if o.strip()]
    if _origins and "*" not in _origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # ------------------------------------------------------------------ #
    #  helpers
    # ------------------------------------------------------------------ #
    def _ctx(venue: Optional[str]) -> VenueContext:
        try:
            return manager.get(venue) if venue else manager.primary
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    def _check_auth(request: Request) -> None:
        token = str(cfg.get("web.api_token", "") or "")
        if not token:
            return
        provided = request.headers.get("X-API-Token") or request.query_params.get("token")
        if not secrets.compare_digest((provided or "").encode(), token.encode()):
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    @app.middleware("http")
    async def protect_api(request: Request, call_next):
        # Account data, logs, settings and CSV are as sensitive as controls.
        # Keep health public so the dashboard can discover auth requirements.
        if request.url.path.startswith("/api/") and request.url.path != "/api/health" and request.method != "OPTIONS":
            try:
                _check_auth(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    def _settings_payload(ctx: VenueContext) -> Dict[str, Any]:
        """Global config + this venue's effective (scoped) values."""
        effective: Dict[str, Any] = {}
        for key in VALIDATORS:
            value = ctx.cfg.get(key)
            effective[key] = ("***" if value else "") if key.endswith("api_token") else value
        venue_block = dict(cfg.public_dict().get("venues", {}).get(ctx.id, {}) or {})
        return {
            "venue": {
                "id": ctx.id, "label": ctx.spec.label, "docs": ctx.spec.docs,
                "needs_passphrase": ctx.spec.needs_passphrase,
                "rest_base": str(ctx.cfg.get("exchange.rest_base", ctx.spec.rest_base)),
                "ws_url": str(ctx.cfg.get("exchange.ws_url", ctx.spec.ws_url)),
                "taker_fee": float(ctx.cfg.get(f"venues.{ctx.id}.taker_fee", ctx.spec.taker_fee)),
            },
            "config": cfg.public_dict(),
            "venue_overrides": venue_block,
            "effective": effective,
            "credentials": ctx.keystore.masked(),
            "mode": ctx.cfg.mode,
            "enabled": bool(cfg.get(f"venues.{ctx.id}.enabled", True)),
            "primary": manager.primary.id,
            "venues": [{"id": v.id, "label": v.spec.label} for v in manager.all()],
        }

    # ------------------------------------------------------------------ #
    #  health, dashboard, venue list
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        live_venues = [c.id for c in manager.all() if c.cfg.mode == "live" and c.enabled]
        return {
            "ok": True,
            "venues": [
                {"id": c.id, "running": c.engine.running, "mode": c.cfg.mode, "enabled": c.enabled}
                for c in manager.all()
            ],
            "primary": manager.primary.id,
            "auth_required": bool(str(cfg.get("web.api_token", "") or "")),
            "live_venues": live_venues,
            "insecure_live": bool(live_venues) and not bool(str(cfg.get("web.api_token", "") or ""))
            and not bool(cfg.get("web.allow_insecure_live", False)),
            "ts": time.time(),
        }

    @app.get("/api/venues")
    async def venues() -> Dict[str, Any]:
        return await manager.summary()

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------------ #
    #  read endpoints (registered twice: scoped + legacy alias)
    # ------------------------------------------------------------------ #
    async def state(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        return await _ctx(venue or request.query_params.get("venue")).engine.state()

    async def positions(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        engine = _ctx(venue or request.query_params.get("venue")).engine
        return {"positions": engine.executor.live_positions(engine.marks) if engine.executor else []}

    async def trades(request: Request, venue: Optional[str] = None, limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0),
                     status: Optional[str] = None, symbol: Optional[str] = None) -> Dict[str, Any]:
        db = _ctx(venue or request.query_params.get("venue")).db
        rows = await db.get_trades(limit=limit, offset=offset, status=status, symbol=symbol)
        return {"trades": rows, "count": len(rows), "total": await db.count_trades()}

    async def trades_csv(request: Request, venue: Optional[str] = None) -> PlainTextResponse:
        ctx = _ctx(venue or request.query_params.get("venue"))
        rows = await ctx.db.get_trades(limit=5000)
        cols = ["id", "symbol", "side", "status", "qty", "entry_price", "exit_price", "leverage",
                "margin_usd", "notional_usd", "sl_price", "tp_price", "peak_roi_pct", "stop_price",
                "trail_stop_roi", "realized_pnl", "roi_pct", "exit_reason", "opened_at", "closed_at"]
        lines = [",".join(cols)]
        for r in rows:
            lines.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": f"attachment; filename={ctx.id}-trades.csv"},
        )

    async def signals(request: Request, venue: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                      status: Optional[str] = None) -> Dict[str, Any]:
        db = _ctx(venue or request.query_params.get("venue")).db
        rows = await db.get_signals(limit=limit, status=status)
        for row in rows:
            for field in ("filters", "features"):
                if isinstance(row.get(field), str):
                    try:
                        row[field] = json.loads(row[field])
                    except json.JSONDecodeError:
                        row[field] = {}
        return {"signals": rows}

    async def metrics(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        return await _ctx(venue or request.query_params.get("venue")).engine.metrics()

    async def compound(request: Request, venue: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        return await _ctx(venue or request.query_params.get("venue")).engine.compound_report(force=force)

    async def equity(request: Request, venue: Optional[str] = None, limit: int = Query(600, ge=1, le=5000)) -> Dict[str, Any]:
        return {"curve": await _ctx(venue or request.query_params.get("venue")).engine.equity_curve(limit)}

    async def universe(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        engine = _ctx(venue or request.query_params.get("venue")).engine
        scanner = engine.universe
        return {
            "selected": scanner.to_dict_list() if scanner else [],
            "rejected_sample": (scanner.rejected_sample if scanner else [])[:40],
            "last_scan_ts": scanner.last_scan_ts if scanner else 0,
            "watchlist": engine.watchlist,
        }

    async def logs(request: Request, venue: Optional[str] = None, after: int = 0,
                   limit: int = 200) -> Dict[str, Any]:
        engine = _ctx(venue or request.query_params.get("venue")).engine
        return {"logs": engine.ring_log.tail(limit=limit, after_seq=after)}

    async def orders(request: Request, venue: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)) -> Dict[str, Any]:
        db = _ctx(venue or request.query_params.get("venue")).db
        return {"orders": await db.recent_orders(limit=limit), "latency": await db.latency_stats()}

    # ------------------------------------------------------------------ #
    #  settings & credentials
    # ------------------------------------------------------------------ #
    async def get_settings(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        return _settings_payload(_ctx(venue or request.query_params.get("venue")))

    async def put_settings(request: Request, venue: Optional[str] = None,
                           patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """Write settings.

        Keys are applied exactly as sent: ``risk.leverage`` changes every venue,
        ``venues.binance.risk.leverage`` changes only Binance. The dashboard's
        scope selector is what decides which form to send.
        """
        ctx = _ctx(venue or request.query_params.get("venue"))
        try:
            applied = cfg.set_many(patch)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log.info("[%s] settings updated: %s", ctx.id, ", ".join(applied))
        restart_required = any(
            k.startswith(("exchange.", "app.mode", "account.", "venues.")) for k in applied
        )
        return {"applied": {k: ("***" if v else "") if k.endswith("api_token") else v
                            for k, v in applied.items()},
                "restart_required": restart_required, "venue": ctx.id}

    async def reset_settings(request: Request, venue: Optional[str] = None,
                             payload: Any = Body(default=None)) -> Dict[str, Any]:
        keys: Optional[List[str]] = None
        if payload is not None:
            raw = payload.get("keys") if isinstance(payload, dict) else payload
            if not isinstance(raw, list) or not all(isinstance(k, str) for k in raw):
                raise HTTPException(status_code=400, detail="expected a list of setting keys, or null to reset all")
            keys = raw
        try:
            cfg.reset(keys)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"reset": True, "keys": "all" if keys is None else keys}

    async def save_credentials(request: Request, venue: Optional[str] = None,
                               payload: Dict[str, str] = Body(...)) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        api_key = (payload.get("api_key") or "").strip()
        api_secret = (payload.get("api_secret") or "").strip()
        passphrase = (payload.get("passphrase") or "").strip()
        if not api_key or not api_secret:
            raise HTTPException(status_code=400, detail="api_key and api_secret are required")
        if ctx.spec.needs_passphrase and not passphrase:
            raise HTTPException(status_code=400, detail=f"{ctx.spec.label} also requires the API passphrase")
        try:
            return await manager.apply_credentials(ctx.id, api_key, api_secret, passphrase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    async def delete_credentials(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        await manager.clear_credentials(ctx.id)
        return {"cleared": True, "venue": ctx.id}

    async def test_credentials(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        creds = ctx.keystore.snapshot()
        if not creds:
            raise HTTPException(status_code=400, detail="no credentials stored")
        return await manager.apply_credentials(ctx.id, creds.api_key, creds.api_secret, creds.passphrase)

    # ------------------------------------------------------------------ #
    #  control
    # ------------------------------------------------------------------ #
    async def control_trading(request: Request, venue: Optional[str] = None,
                              payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        enabled = bool(payload.get("enabled", True))
        return {"venue": ctx.id, "trading_enabled": await ctx.engine.set_trading_enabled(enabled)}

    async def control_flatten(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        return {"venue": ctx.id, "closed": await ctx.engine.flatten_all("manual_flatten")}

    async def control_close(request: Request, venue: Optional[str] = None,
                            payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        symbol = str(payload.get("symbol") or "")
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        return {"venue": ctx.id, "result": await ctx.engine.close_symbol(symbol, "manual_close")}

    async def control_resume(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        return {"venue": ctx.id, "resumed": await ctx.engine.resume_risk_halt()}

    async def control_restart(request: Request, venue: Optional[str] = None) -> Dict[str, Any]:
        if venue or request.query_params.get("venue"):
            ctx = _ctx(venue or request.query_params.get("venue"))
            if not manager.request_restart(ctx.id):
                raise HTTPException(status_code=503, detail="shutdown in progress; restart not accepted")
            return {"venue": ctx.id, "restarting": True}
        requested = {ctx.id: manager.request_restart(ctx.id) for ctx in manager.all()}
        if not any(requested.values()):
            raise HTTPException(status_code=503, detail="shutdown in progress; restart not accepted")
        return {"restarting": True, "venues": list(requested),
                "requested": requested}

    async def control_paper_reset(request: Request, venue: Optional[str] = None,
                                  payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        ctx = _ctx(venue or request.query_params.get("venue"))
        if ctx.cfg.mode != "paper":
            raise HTTPException(status_code=400, detail="only available in paper mode")
        broker = ctx.engine.broker
        if broker is None:
            raise HTTPException(status_code=400, detail="broker not initialised")
        # A settings mode change does not replace a running live broker until
        # restart. Never let a paper-only control mutate that broker or its DB.
        if broker.mode != "paper":
            raise HTTPException(status_code=400, detail="running broker is not in paper mode")
        try:
            equity = float(payload.get("equity", ctx.cfg.get("account.paper_starting_equity", 20.0)))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="equity must be a positive finite number")
        if not math.isfinite(equity) or equity <= 0:
            raise HTTPException(status_code=400, detail="equity must be a positive finite number")
        broker.starting_equity = equity
        broker.realized = 0.0
        broker.fees_paid = 0.0
        broker._positions.clear()
        broker._protection.clear()
        await ctx.db.kv_set_json("paper.equity", equity)
        await ctx.db.exec("DELETE FROM trades WHERE status='OPEN'")
        # a deliberate balance change must re-anchor the risk baselines, otherwise
        # shrinking the book reads as a catastrophic drawdown and halts trading
        if ctx.engine.guard is not None:
            await ctx.engine.guard.rebaseline(equity)
        # ...and it is the one thing allowed to move the *fixed* starting balance,
        # because a reset starts a new book by definition
        await ctx.engine.ensure_starting_balance(force=equity)
        # drop the cached account snapshot so the dashboard shows the new book at
        # once instead of waiting for the next equity-loop tick
        ctx.engine.last_account = None
        return {"reset": True, "equity": equity, "venue": ctx.id}

    # ------------------------------------------------------------------ #
    #  register read/control routes for both surfaces
    # ------------------------------------------------------------------ #
    read_routes = [
        ("/state", state), ("/positions", positions), ("/trades", trades),
        ("/signals", signals), ("/metrics", metrics), ("/compound", compound),
        ("/equity", equity), ("/universe", universe), ("/logs", logs),
        ("/orders", orders), ("/settings", get_settings),
    ]
    for path, handler in read_routes:
        app.add_api_route(f"/api{path}", handler, methods=["GET"])
        app.add_api_route(f"/api/v/{{venue}}{path}", handler, methods=["GET"])
    app.add_api_route("/api/trades.csv", trades_csv, methods=["GET"])
    app.add_api_route("/api/v/{venue}/trades.csv", trades_csv, methods=["GET"])

    write_routes = [
        ("/settings", put_settings, ["PUT"]),
        ("/settings/reset", reset_settings, ["POST"]),
        ("/credentials", save_credentials, ["POST"]),
        ("/credentials", delete_credentials, ["DELETE"]),
        ("/credentials/test", test_credentials, ["POST"]),
        ("/control/trading", control_trading, ["POST"]),
        ("/control/flatten", control_flatten, ["POST"]),
        ("/control/close", control_close, ["POST"]),
        ("/control/resume-halt", control_resume, ["POST"]),
        ("/control/restart", control_restart, ["POST"]),
        ("/control/paper-reset", control_paper_reset, ["POST"]),
    ]
    for path, handler, methods in write_routes:
        app.add_api_route(f"/api{path}", handler, methods=methods)
        app.add_api_route(f"/api/v/{{venue}}{path}", handler, methods=methods)

    # ------------------------------------------------------------------ #
    #  live websocket feed (one socket per venue tab)
    # ------------------------------------------------------------------ #
    async def ws_endpoint(websocket: WebSocket, venue: Optional[str] = None) -> None:
        await websocket.accept()
        token = str(cfg.get("web.api_token", "") or "")
        if token:
            provided = (websocket.query_params.get("token")
                        or websocket.headers.get("x-api-token") or "")
            if not secrets.compare_digest(provided.encode(), token.encode()):
                await websocket.close(code=1008, reason="invalid or missing API token")
                return
        try:
            ctx = _ctx(venue or websocket.query_params.get("venue"))
        except HTTPException:
            await websocket.close(code=1008, reason="unknown venue")
            return
        engine = ctx.engine
        try:
            while True:
                payload = {
                    "type": "state",
                    "venue": ctx.id,
                    "state": await engine.state(),
                    "metrics": await engine.metrics(),
                    "compound": await engine.compound_report(),
                    "equity": (await engine.equity_curve(400))[-400:],
                }
                await websocket.send_text(json.dumps(payload, default=str))
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] ws closed: %s", ctx.id, exc)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    app.add_api_websocket_route("/ws", ws_endpoint)
    app.add_api_websocket_route("/ws/{venue}", ws_endpoint)

    @app.get("/api/venues/meta")
    async def venues_meta() -> Dict[str, Any]:
        return {
            "venues": [
                {
                    "id": spec.id, "label": spec.label, "docs": spec.docs,
                    "rest_base": spec.rest_base, "ws_url": spec.ws_url,
                    "needs_passphrase": spec.needs_passphrase,
                    "symbol_style": spec.symbol_style,
                    "max_leverage": spec.max_leverage,
                    "taker_fee": spec.taker_fee, "maker_fee": spec.maker_fee,
                }
                for spec in VENUES.values()
            ]
        }

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def build_app(manager: VenueManager, cfg: Config) -> FastAPI:
    return create_app(manager, cfg)
