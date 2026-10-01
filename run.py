#!/usr/bin/env python3
"""Entry point: boots the trading engine and the dashboard web server.

    python3 run.py                # start engine + dashboard on :8080
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
from app.db import Database                                    # noqa: E402
from app.engine import TradingEngine                           # noqa: E402
from app.keystore import CredentialStore                       # noqa: E402
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
    db = Database(cfg.resolve(str(cfg.get("persistence.db_path", "data/bot.db"))))
    keystore = CredentialStore(db, data_dir / ".secrets")
    await keystore.load()

    engine = TradingEngine(cfg, db, keystore)
    setup_logging(str(cfg.get("app.log_level", "INFO")), engine.ring_log)

    log = logging.getLogger("main")
    log.info("=" * 78)
    log.info("AO Divergence Futures Bot — mode=%s | data_dir=%s", cfg.mode, data_dir)
    log.info("=" * 78)

    if not args.no_engine:
        try:
            await engine.start()
        except Exception as exc:  # noqa: BLE001
            log.error("engine failed to start: %s", exc)
            log.error("The dashboard is still available — fix settings there and press 'Restart engine'.")

    app = build_app(engine, cfg, db, keystore)

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
        log.info("shutdown requested — stopping engine and server")
        await engine.stop()
        server.should_exit = True

    watcher = asyncio.create_task(_watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        with contextlib.suppress(Exception):
            await watcher
        await engine.stop()
        db.close()
        log.info("bye")


def main() -> None:
    parser = argparse.ArgumentParser(description="AO Divergence Futures Bot (MEXC)")
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
