"""Multi-venue manager: three exchanges, three isolated engines.

Isolation model (this is what makes "no code conflict" true in practice):

============================  ==================================================
shared                        per venue
============================  ==================================================
strategy code                 broker + websocket client
filter/risk/trailing code     SQLite database (data/venues/<id>.db)
config defaults               API credentials (encrypted, in that database)
dashboard + REST/WS API       positions, orders, P&L, equity curve, logs view
============================  ==================================================

Because every venue owns its own database, a corruption, a halt, a kill-switch
or a credential problem on one exchange cannot touch another. The engines are
started independently: if Binance cannot be reached, MEXC keeps trading.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, VenueConfig
from .db import Database
from .engine import TradingEngine
from .exchange.venue import VENUES, get_venue, venue_ids
from .keystore import CredentialStore

log = logging.getLogger("manager")


@dataclass
class VenueContext:
    id: str
    spec: Any
    cfg: VenueConfig
    db: Database
    keystore: CredentialStore
    engine: TradingEngine
    enabled: bool = True
    start_error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class VenueManager:
    """Owns one :class:`TradingEngine` per configured exchange."""

    def __init__(self, cfg: Config, *, db_dir: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.data_dir = cfg.data_dir
        self.venues_dir = Path(db_dir) if db_dir else (self.data_dir / "venues")
        self.venues_dir.mkdir(parents=True, exist_ok=True)
        self._legacy_db = cfg.resolve(str(cfg.get("persistence.db_path", "data/bot.db")))
        self.ctx: Dict[str, VenueContext] = {}
        self._build()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        for vid in venue_ids():
            spec = get_venue(vid)
            enabled = bool(self.cfg.get(f"venues.{vid}.enabled", True))
            vcfg = VenueConfig(self.cfg, vid)
            db_path = self.venues_dir / f"{vid}.db"
            self._migrate_legacy(vid, db_path)
            db = Database(db_path)
            keystore = CredentialStore(
                db, self.data_dir / ".secrets",
                venue_label=spec.label, needs_passphrase=spec.needs_passphrase,
            )
            engine = TradingEngine(vcfg, db, keystore, venue_id=vid, base_cfg=self.cfg)
            self.ctx[vid] = VenueContext(
                id=vid, spec=spec, cfg=vcfg, db=db, keystore=keystore,
                engine=engine, enabled=enabled,
            )
            if not enabled:
                log.info("[%s] disabled in config — engine not started", vid)

    def _migrate_legacy(self, venue_id: str, target: Path) -> None:
        """Adopt the pre-multi-venue database (history + MEXC credentials)."""
        if venue_id != "mexc" or target.exists() or not self._legacy_db.exists():
            return
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self._legacy_db) + suffix)
            if src.exists():
                try:
                    shutil.copy2(src, Path(str(target) + suffix))
                except OSError as exc:  # noqa: BLE001
                    log.warning("legacy db migration (%s) failed: %s", src, exc)
        log.info("migrated legacy database %s -> %s", self._legacy_db, target)

    # ------------------------------------------------------------------ #
    @property
    def primary(self) -> VenueContext:
        """Venue used by the legacy (un-prefixed) API routes."""
        preferred = str(self.cfg.get("app.primary_venue", "mexc") or "mexc").lower()
        if preferred in self.ctx:
            return self.ctx[preferred]
        return next(iter(self.ctx.values()))

    def all(self) -> List[VenueContext]:
        return list(self.ctx.values())

    def get(self, venue_id: str) -> VenueContext:
        vid = str(venue_id or "").strip().lower()
        if vid not in self.ctx:
            raise KeyError(f"unknown venue {venue_id!r}; known: {', '.join(self.ctx)}")
        return self.ctx[vid]

    # ------------------------------------------------------------------ #
    async def start_all(self) -> Dict[str, str]:
        """Start every enabled venue. One failure never blocks the others."""
        results: Dict[str, str] = {}
        for ctx in self.all():
            if not ctx.enabled:
                results[ctx.id] = "disabled"
                continue
            await ctx.keystore.load()
            try:
                await ctx.engine.start()
                ctx.start_error = ""
                results[ctx.id] = "running"
            except Exception as exc:  # noqa: BLE001
                ctx.start_error = str(exc)
                results[ctx.id] = f"error: {exc}"
                log.error("[%s] engine failed to start: %s", ctx.id, exc)
        return results

    async def stop_all(self) -> None:
        for ctx in self.all():
            try:
                await ctx.engine.stop()
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] engine stop failed: %s", ctx.id, exc)

    def close(self) -> None:
        for ctx in self.all():
            try:
                ctx.db.close()
            except Exception:  # noqa: BLE001
                pass

    async def restart(self, venue_id: str) -> bool:
        ctx = self.get(venue_id)
        await ctx.engine.stop()
        await asyncio.sleep(0.3)
        try:
            await ctx.engine.start()
            ctx.start_error = ""
            return True
        except Exception as exc:  # noqa: BLE001
            ctx.start_error = str(exc)
            log.error("[%s] engine restart failed: %s", venue_id, exc)
            return False

    # ------------------------------------------------------------------ #
    async def apply_credentials(self, venue_id: str, api_key: str, api_secret: str,
                                passphrase: str = "") -> Dict[str, Any]:
        ctx = self.get(venue_id)
        return await ctx.engine.apply_credentials(api_key, api_secret, passphrase)

    async def clear_credentials(self, venue_id: str) -> None:
        ctx = self.get(venue_id)
        await ctx.keystore.clear()
        client = getattr(ctx.engine.broker, "client", None)
        if client is not None:
            client.update_credentials(None, None, None)

    def credentials_view(self, venue_id: str) -> Dict[str, Any]:
        ctx = self.get(venue_id)
        data = ctx.keystore.masked()
        data["venue"] = ctx.id
        data["label"] = ctx.spec.label
        return data

    # ------------------------------------------------------------------ #
    async def summary(self) -> Dict[str, Any]:
        """Compact per-venue snapshot for the dashboard tab strip."""
        venues: List[Dict[str, Any]] = []
        for ctx in self.all():
            engine = ctx.engine
            account = engine.last_account
            if account is None and engine.broker is not None and engine.running:
                try:
                    account = await engine.broker.account()
                    engine.last_account = account
                except Exception:  # noqa: BLE001
                    account = None
            stats = await ctx.db.trade_stats()
            venues.append({
                "id": ctx.id,
                "label": ctx.spec.label,
                "enabled": ctx.enabled,
                "mode": ctx.cfg.mode,
                "running": bool(engine.running),
                "status": engine.status_message,
                "start_error": ctx.start_error,
                "market_data": engine.market_data_source,
                "equity": round(account.equity, 4) if account else 0.0,
                "realized_pnl": round(float(stats.get("pnl") or 0.0), 4),
                "open_positions": len(engine.executor.positions) if engine.executor else 0,
                "trades": int(stats.get("trades") or 0),
                "win_rate": float(stats.get("win_rate") or 0.0),
                "watchlist": len(engine.watchlist),
                "credentials": ctx.keystore.masked(),
                "needs_passphrase": ctx.spec.needs_passphrase,
                "docs": ctx.spec.docs,
                "rest_base": str(ctx.cfg.get("exchange.rest_base", ctx.spec.rest_base)),
            })
        return {
            "venues": venues,
            "primary": self.primary.id,
            "updated_at": asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0,
        }


__all__ = ["VenueManager", "VenueContext", "VENUES"]
