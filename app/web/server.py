"""FastAPI application: REST control plane + WebSocket live feed + dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger("web")
STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine, cfg, db, keystore) -> FastAPI:
    app = FastAPI(title="AO Divergence Futures Bot", version="1.0.0", docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(o) for o in (cfg.get("web.allow_origins", "*"),)],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    #  auth for control endpoints
    # ------------------------------------------------------------------ #
    def _check_auth(request: Request) -> None:
        token = str(cfg.get("web.api_token", "") or "")
        if not token:
            return
        provided = request.headers.get("X-API-Token") or request.query_params.get("token")
        if provided != token:
            raise HTTPException(status_code=401, detail="invalid or missing API token")

    # ------------------------------------------------------------------ #
    #  health & dashboard
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "engine_running": engine.running,
            "mode": cfg.mode,
            "market_data": engine.market_data_source,
            "ts": time.time(),
        }

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # ------------------------------------------------------------------ #
    #  read endpoints
    # ------------------------------------------------------------------ #
    @app.get("/api/state")
    async def state() -> Dict[str, Any]:
        return await engine.state()

    @app.get("/api/positions")
    async def positions() -> Dict[str, Any]:
        return {"positions": engine.executor.live_positions(engine.marks) if engine.executor else []}

    @app.get("/api/trades")
    async def trades(limit: int = 200, offset: int = 0, status: Optional[str] = None,
                     symbol: Optional[str] = None) -> Dict[str, Any]:
        rows = await db.get_trades(limit=min(limit, 1000), offset=offset, status=status, symbol=symbol)
        return {"trades": rows, "count": len(rows), "total": await db.count_trades()}

    @app.get("/api/trades.csv")
    async def trades_csv() -> PlainTextResponse:
        rows = await db.get_trades(limit=5000)
        cols = ["id", "symbol", "side", "status", "qty", "entry_price", "exit_price", "leverage",
                "margin_usd", "notional_usd", "sl_price", "tp_price", "peak_roi_pct", "stop_price",
                "trail_stop_roi", "realized_pnl", "roi_pct", "exit_reason", "opened_at", "closed_at"]
        lines = [",".join(cols)]
        for r in rows:
            lines.append(",".join("" if r.get(c) is None else str(r.get(c)) for c in cols))
        return PlainTextResponse("\n".join(lines), headers={"Content-Disposition": "attachment; filename=trades.csv"})

    @app.get("/api/signals")
    async def signals(limit: int = 100, status: Optional[str] = None) -> Dict[str, Any]:
        rows = await db.get_signals(limit=min(limit, 500), status=status)
        for row in rows:
            for field in ("filters", "features"):
                if isinstance(row.get(field), str):
                    try:
                        row[field] = json.loads(row[field])
                    except json.JSONDecodeError:
                        row[field] = {}
        return {"signals": rows}

    @app.get("/api/metrics")
    async def metrics() -> Dict[str, Any]:
        return await engine.metrics()

    @app.get("/api/compound")
    async def compound(force: bool = False) -> Dict[str, Any]:
        return await engine.compound_report(force=force)

    @app.get("/api/equity")
    async def equity(limit: int = 600) -> Dict[str, Any]:
        return {"curve": await engine.equity_curve(limit)}

    @app.get("/api/universe")
    async def universe() -> Dict[str, Any]:
        scanner = engine.universe
        return {
            "selected": scanner.to_dict_list() if scanner else [],
            "rejected_sample": (scanner.rejected_sample if scanner else [])[:40],
            "last_scan_ts": scanner.last_scan_ts if scanner else 0,
            "watchlist": engine.watchlist,
        }

    @app.get("/api/logs")
    async def logs(after: int = 0, limit: int = 200) -> Dict[str, Any]:
        return {"logs": engine.ring_log.tail(limit=limit, after_seq=after)}

    @app.get("/api/orders")
    async def orders(limit: int = 100) -> Dict[str, Any]:
        return {"orders": await db.recent_orders(limit=limit), "latency": await db.latency_stats()}

    # ------------------------------------------------------------------ #
    #  settings & credentials
    # ------------------------------------------------------------------ #
    @app.get("/api/settings")
    async def get_settings() -> Dict[str, Any]:
        return {
            "config": cfg.public_dict(),
            "credentials": keystore.masked(),
            "mode": cfg.mode,
            "overrides": cfg.as_dict(),
        }

    @app.put("/api/settings")
    async def put_settings(request: Request, patch: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check_auth(request)
        try:
            applied = cfg.set_many(patch)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        log.info("settings updated: %s", ", ".join(f"{k}={v}" for k, v in applied.items()))
        restart_required = any(k.startswith(("exchange.", "app.mode", "account.")) for k in applied)
        return {"applied": applied, "restart_required": restart_required}

    @app.post("/api/settings/reset")
    async def reset_settings(request: Request, payload: Any = Body(default=None)) -> Dict[str, Any]:
        _check_auth(request)
        keys: Optional[List[str]] = None
        if isinstance(payload, list):
            keys = [str(k) for k in payload]
        elif isinstance(payload, dict):                 # {"keys": [...]} also accepted
            raw = payload.get("keys")
            keys = [str(k) for k in raw] if isinstance(raw, list) else None
        cfg.reset(keys)
        return {"reset": True, "keys": keys or "all"}

    @app.post("/api/credentials")
    async def save_credentials(request: Request, payload: Dict[str, str] = Body(...)) -> Dict[str, Any]:
        _check_auth(request)
        api_key = (payload.get("api_key") or "").strip()
        api_secret = (payload.get("api_secret") or "").strip()
        if not api_key or not api_secret:
            raise HTTPException(status_code=400, detail="api_key and api_secret are required")
        result = await engine.apply_credentials(api_key, api_secret)
        return result

    @app.delete("/api/credentials")
    async def delete_credentials(request: Request) -> Dict[str, Any]:
        _check_auth(request)
        await keystore.clear()
        return {"cleared": True}

    @app.post("/api/credentials/test")
    async def test_credentials(request: Request) -> Dict[str, Any]:
        _check_auth(request)
        creds = keystore.snapshot()
        if not creds:
            raise HTTPException(status_code=400, detail="no credentials stored")
        result = await engine.apply_credentials(creds.api_key, creds.api_secret)
        return result

    # ------------------------------------------------------------------ #
    #  control
    # ------------------------------------------------------------------ #
    @app.post("/api/control/trading")
    async def control_trading(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check_auth(request)
        enabled = bool(payload.get("enabled", True))
        return {"trading_enabled": await engine.set_trading_enabled(enabled)}

    @app.post("/api/control/flatten")
    async def control_flatten(request: Request) -> Dict[str, Any]:
        _check_auth(request)
        return {"closed": await engine.flatten_all("manual_flatten")}

    @app.post("/api/control/close")
    async def control_close(request: Request, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        _check_auth(request)
        symbol = str(payload.get("symbol") or "")
        if not symbol:
            raise HTTPException(status_code=400, detail="symbol is required")
        return {"result": await engine.close_symbol(symbol, "manual_close")}

    @app.post("/api/control/resume-halt")
    async def control_resume(request: Request) -> Dict[str, Any]:
        _check_auth(request)
        return {"resumed": await engine.resume_risk_halt()}

    @app.post("/api/control/restart")
    async def control_restart(request: Request) -> Dict[str, Any]:
        _check_auth(request)
        asyncio.create_task(_restart_engine())
        return {"restarting": True}

    async def _restart_engine() -> None:
        try:
            await engine.stop()
            await asyncio.sleep(0.5)
            await engine.start()
        except Exception as exc:  # noqa: BLE001
            log.error("engine restart failed: %s", exc)

    @app.post("/api/control/paper-reset")
    async def control_paper_reset(request: Request, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        _check_auth(request)
        if cfg.mode != "paper":
            raise HTTPException(status_code=400, detail="only available in paper mode")
        equity = float(payload.get("equity") or cfg.get("account.paper_starting_equity", 1000.0))
        broker = engine.broker
        if broker is None:
            raise HTTPException(status_code=400, detail="broker not initialised")
        broker.starting_equity = equity
        broker.realized = 0.0
        broker.fees_paid = 0.0
        broker._positions.clear()
        broker._protection.clear()
        await db.kv_set_json("paper.equity", equity)
        await db.exec("DELETE FROM trades WHERE status='OPEN'")
        return {"reset": True, "equity": equity}

    # ------------------------------------------------------------------ #
    #  live websocket feed
    # ------------------------------------------------------------------ #
    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                payload = {
                    "type": "state",
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
            log.debug("ws closed: %s", exc)
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def build_app(engine, cfg, db, keystore) -> FastAPI:
    return create_app(engine, cfg, db, keystore)
