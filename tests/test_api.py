"""HTTP dashboard API tests (FastAPI TestClient, no network)."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.keystore import CredentialStore  # noqa: E402
from app.utils import RingLogHandler  # noqa: E402
from app.web.server import build_app  # noqa: E402


class ApiTest(unittest.TestCase):
    """The API is exercised against a *fake* engine: the routes, validation
    and masking logic are what matter here — the trading logic has its own
    deterministic tests."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="ao-api-"))
        cfg_path = cls.tmp / "config.toml"
        shutil.copy(ROOT / "config.toml", cfg_path)
        text = cfg_path.read_text().replace('data_dir = "data"', f'data_dir = "{cls.tmp / "data"}"')
        cfg_path.write_text(text)

        cls.db = Database(cls.tmp / "data" / "api.db")   # schema is created on open

        cls.cfg = Config(cfg_path)
        cls.keystore = CredentialStore(cls.db, cls.cfg.resolve("data") / ".secrets")

        class FakeSignal:
            def to_dict(self):
                return {"symbol": "BTC_USDT", "side": "LONG", "score": 71.0, "status": "executed"}

        class FakeEngine:
            name = "ao-divergence-bot-test"
            running = False
            mode = "paper"
            market_data = "synthetic"
            started_at = 0.0
            last_error = ""
            last_signal = FakeSignal()
            watchlist = ["BTC_USDT"]
            marks: dict = {}
            market_data_source = "synthetic"
            broker = None
            executor = None
            universe = None
            ring_log = RingLogHandler(capacity=50)

            async def state(self):
                return {
                    "engine": {"running": self.running, "name": self.name, "mode": self.mode,
                               "market_data": self.market_data, "uptime_sec": 12.0,
                               "last_error": self.last_error, "trading_enabled": True},
                    "account": {"equity": 1000.0, "available": 900.0, "realized_today": 0.0},
                    "risk": {"halted": False, "reason": "", "max_open_positions": 10},
                    "positions": [], "metrics": {}, "watchlist": self.watchlist,
                }

            async def metrics(self):
                return {"trades": 0, "win_rate_pct": 0.0}

            async def equity_curve(self, limit=500):
                return [{"ts": 1, "equity": 1000.0}]

            async def compound_report(self, force: bool = False):
                return {"simulation": {"prob_hit_target_pct": 3.0}, "requirements": {}}

            async def set_trading_enabled(self, value: bool):
                return value

            async def log_tail(self, limit=200, after_seq=0):
                return [{"seq": 1, "level": "INFO", "msg": "hello", "logger": "test"}]

            async def positions_snapshot(self, marks=None):
                return []

            async def flatten_all(self, reason: str = "manual_flatten"):
                return []

            async def close_symbol(self, symbol: str, reason: str = "manual_close"):
                return {"ok": True}

            async def resume_risk_halt(self):
                return True

            async def apply_credentials(self, api_key: str, api_secret: str):
                return {"ok": True, "configured": True}

            async def restart(self):
                return True

            async def paper_reset(self, **kw):
                return {"ok": True}

        cls.engine = FakeEngine()
        cls.app = build_app(cls.engine, cls.cfg, cls.db, cls.keystore)
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ #
    def test_health(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_state_shape(self):
        d = self.client.get("/api/state").json()
        for section in ("engine", "account", "risk"):
            self.assertIn(section, d)
        self.assertEqual(d["account"]["equity"], 1000.0)

    def test_read_endpoints_ok(self):
        for path in ("/api/positions", "/api/trades", "/api/signals", "/api/metrics",
                     "/api/compound", "/api/equity", "/api/universe", "/api/logs",
                     "/api/orders", "/api/settings"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_dashboard_static(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("AO Divergence", r.text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/styles.css").status_code, 200)

    def test_settings_are_masked_and_typed(self):
        data = self.client.get("/api/settings").json()
        self.assertNotIn("api_secret", json.dumps(data).lower())
        payload = data.get("values") or data.get("config") or {}
        self.assertIsInstance(payload, dict)
        self.assertGreater(len(payload), 5)

    def test_settings_roundtrip_and_validation(self):
        r = self.client.put("/api/settings", json={"risk.equity_per_trade_pct": 6.5})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertAlmostEqual(self.cfg.get("risk.equity_per_trade_pct"), 6.5)
        # out-of-range / unknown keys must be rejected, never silently applied
        bad = self.client.put("/api/settings", json={"risk.leverage": 9999})
        self.assertGreaterEqual(bad.status_code, 400)
        unknown = self.client.put("/api/settings", json={"nope.nope": 1})
        self.assertGreaterEqual(unknown.status_code, 400)
        reset = self.client.post("/api/settings/reset", json=["risk.equity_per_trade_pct"])
        self.assertEqual(reset.status_code, 200, reset.text)
        self.assertEqual(self.cfg.get("risk.equity_per_trade_pct"), 8.0)

    def test_credentials_never_return_secrets(self):
        r = self.client.post("/api/credentials", json={"api_key": "key-1234567890", "api_secret": "s3cret"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.text
        self.assertNotIn("s3cret", body)
        state = self.client.get("/api/settings").json()
        self.assertNotIn("s3cret", json.dumps(state))
        masked = json.dumps(state).lower()
        self.assertIn("api", masked)
        self.assertEqual(self.client.delete("/api/credentials").status_code, 200)

    def test_control_endpoints(self):
        self.assertEqual(self.client.post("/api/control/trading", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.client.post("/api/control/flatten").status_code, 200)
        self.assertEqual(self.client.post("/api/control/resume-halt").status_code, 200)
        self.assertEqual(self.client.post("/api/control/close", json={"symbol": "BTC_USDT"}).status_code, 200)

    def test_trades_csv(self):
        r = self.client.get("/api/trades.csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("symbol", r.text.splitlines()[0])   # header row even with no trades

    def test_websocket_pushes_state(self):
        with self.client.websocket_connect("/ws") as ws:
            payload = json.loads(ws.receive_text())
            self.assertEqual(payload.get("type"), "state")
            self.assertIn("account", payload["state"])


if __name__ == "__main__":
    unittest.main()
