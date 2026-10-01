"""HTTP dashboard API tests (FastAPI TestClient, no network)."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Config, VenueConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.exchange.venue import get_venue, venue_ids  # noqa: E402
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

        cls.cfg = Config(cfg_path)

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

            async def apply_credentials(self, api_key: str, api_secret: str, passphrase: str = ""):
                return {"ok": True, "configured": True, "verified": True, "venue": "mexc"}

            async def restart(self):
                return True

            async def paper_reset(self, **kw):
                return {"ok": True}

        cls.engine = FakeEngine()

        # One isolated database + keystore per venue — exactly like production.
        class Ctx:
            def __init__(self, vid):
                self.id = vid
                self.spec = get_venue(vid)
                self.cfg = VenueConfig(cls.cfg, vid)
                self.db = Database(cls.tmp / "data" / "venues" / f"{vid}.db")
                self.keystore = CredentialStore(
                    self.db, cls.tmp / "data" / ".secrets",
                    venue_label=self.spec.label, needs_passphrase=self.spec.needs_passphrase,
                )
                self.engine = FakeEngine()
                self.enabled = True
                self.start_error = ""

        class FakeManager:
            def __init__(self):
                self.ctx = {vid: Ctx(vid) for vid in venue_ids()}

            @property
            def primary(self):
                return self.ctx["mexc"]

            def all(self):
                return list(self.ctx.values())

            def get(self, vid):
                vid = str(vid or "").strip().lower()
                if vid not in self.ctx:
                    raise KeyError(f"unknown venue {vid!r}")
                return self.ctx[vid]

            async def summary(self):
                return {"primary": "mexc", "venues": [
                    {"id": c.id, "label": c.spec.label, "mode": c.cfg.mode, "running": False,
                     "equity": 1000.0, "open_positions": 0, "credentials": c.keystore.masked()}
                    for c in self.all()]}

            async def apply_credentials(self, vid, api_key, api_secret, passphrase=""):
                ctx = self.get(vid)
                await ctx.keystore.save(api_key, api_secret, passphrase)
                return {"saved": True, "verified": True, "venue": vid}

            async def clear_credentials(self, vid):
                await self.get(vid).keystore.clear()

            async def restart(self, vid):
                return True

        cls.manager = FakeManager()
        cls.db = cls.manager.primary.db
        cls.keystore = cls.manager.primary.keystore
        cls.app = build_app(cls.manager, cls.cfg)
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

    def test_venue_tabs(self):
        d = self.client.get("/api/venues").json()
        ids = [v["id"] for v in d["venues"]]
        self.assertEqual(ids, ["mexc", "binance", "kucoin"])
        self.assertEqual(d["primary"], "mexc")

    def test_scoped_state_matches_venue(self):
        for vid in ("mexc", "binance", "kucoin"):
            r = self.client.get(f"/api/v/{vid}/state")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["engine"]["name"], self.engine.name)
        self.assertEqual(self.client.get("/api/v/nope/state").status_code, 404)

    def test_legacy_alias_targets_primary(self):
        legacy = self.client.get("/api/state").json()
        primary = self.client.get("/api/v/mexc/state").json()
        self.assertEqual(legacy, primary)
        via_query = self.client.get("/api/state", params={"venue": "binance"}).json()
        self.assertEqual(via_query, primary)

    def test_credentials_are_venue_scoped(self):
        r = self.client.post("/api/v/kucoin/credentials",
                             json={"api_key": "k" * 12, "api_secret": "s" * 12, "passphrase": "p" * 8})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["verified"])
        self.assertTrue(self.client.get("/api/v/kucoin/settings").json()["credentials"]["configured"])
        self.assertFalse(self.client.get("/api/v/binance/settings").json()["credentials"]["configured"])
        # KuCoin keys without a passphrase must be rejected
        r = self.client.post("/api/v/kucoin/credentials",
                             json={"api_key": "k" * 12, "api_secret": "s" * 12})
        self.assertEqual(r.status_code, 400)
        r = self.client.delete("/api/v/kucoin/credentials")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.client.get("/api/v/kucoin/settings").json()["credentials"]["configured"])

    def test_settings_scope(self):
        r = self.client.put("/api/v/binance/settings", json={"venues.binance.risk.leverage": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["applied"]["venues.binance.risk.leverage"], 7)
        self.assertEqual(self.client.get("/api/v/binance/settings").json()["effective"]["risk.leverage"], 7)
        self.assertEqual(self.client.get("/api/v/mexc/settings").json()["effective"]["risk.leverage"], 10)
        self.client.post("/api/v/binance/settings/reset", json=["venues.binance.risk.leverage"])
        self.assertEqual(self.client.get("/api/v/binance/settings").json()["effective"]["risk.leverage"], 10)

    def test_ws_venue_scoped(self):
        with self.client.websocket_connect("/ws/kucoin") as ws:
            frame = json.loads(ws.receive_text())
        self.assertEqual(frame["type"], "state")
        self.assertEqual(frame["venue"], "kucoin")

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
        r = self.client.post("/api/credentials", json={"api_key": "key-1234567890", "api_secret": "s3cret-value-1234567890"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.text
        self.assertNotIn("s3cret-value", body)
        state = self.client.get("/api/settings").json()
        self.assertNotIn("s3cret-value", json.dumps(state))
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
