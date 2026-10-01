#!/usr/bin/env python3
"""Crypto Hunter entry point: boots the trading engines and dashboard.

    python3 run.py                # start engines + dashboard on :8080
    python3 run.py --port 9000    # custom port
    python3 run.py --no-engine    # dashboard only (engine can be started from the UI)

Environment:
    AO_CONFIG   path to an alternative config.toml
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config import load as load_config                     # noqa: E402
from app.manager import VenueManager                           # noqa: E402
from app.utils import RingLogHandler                           # noqa: E402
from app.web.server import build_app                           # noqa: E402

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"


def setup_logging(level: str, ring: RingLogHandler) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(stream)
    ring.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(ring)
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main_async(args: argparse.Namespace) -> None:
    config_path = Path(os.environ.get("AO_CONFIG", ROOT / "config.toml"))
    cfg = load_config(config_path)

    data_dir = cfg.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    manager = VenueManager(cfg)
    setup_logging(str(cfg.get("app.log_level", "INFO")), manager.primary.engine.ring_log)

    log = logging.getLogger("main")
    log.info("=" * 78)
    log.info("Crypto Hunter — %d venues | data_dir=%s",
             len(manager.all()), data_dir)
    for ctx in manager.all():
        log.info("  %-8s %-24s mode=%-5s enabled=%s db=%s",
                 ctx.id, ctx.spec.label, ctx.cfg.mode, ctx.enabled, ctx.db.path.name)
    log.info("=" * 78)

    if not args.no_engine:
        results = await manager.start_all()
        for vid, state in results.items():
            log.info("[%s] %s", vid, state)
        if all(str(s).startswith(("error",)) for s in results.values()):
            log.error("No venue could start — the dashboard is still available; "
                      "fix settings there and press 'Restart'.")

    app = build_app(manager, cfg)

    import uvicorn

    host = str(cfg.get("web.host", "0.0.0.0"))
    port = int(args.port or cfg.get("web.port", 8080))
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_level="warning", access_log=False,
        loop="auto", timeout_keep_alive=30,
    ))

    log.info("dashboard: http://%s:%d", "localhost" if host in ("0.0.0.0", "::") else host, port)

    stop_event = asyncio.Event()

    def _signal(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal)

    async def _watch() -> None:
        await stop_event.wait()
        log.info("shutdown requested — stopping all engines and the server")
        await manager.stop_all()
        server.should_exit = True

    watcher = asyncio.create_task(_watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await manager.stop_all()
        manager.close()
        log.info("bye")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Crypto Hunter — automated futures trading across MEXC, Binance and KuCoin")
    parser.add_argument("--port", type=int, default=None, help="dashboard port")
    parser.add_argument("--no-engine", action="store_true", help="start the dashboard only")
    args = parser.parse_args()

    if sys.version_info < (3, 11):
        print("Python 3.11+ is required (tomllib).", file=sys.stderr)
        sys.exit(2)

    try:
        import uvloop  # type: ignore

        uvloop.install()
    except Exception:  # noqa: BLE001 - optional performance boost
        pass

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
