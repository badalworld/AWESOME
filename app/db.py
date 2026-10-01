"""SQLite persistence layer (WAL) — trades, signals, equity curve, bot state.

All public methods are async and execute the blocking sqlite work in a worker
thread, so the trading event loop is never blocked by disk I/O. A single
connection + ``check_same_thread=False`` + an internal lock is used: writes are
serialised (cheap at this scale) and WAL keeps readers lock-free.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_uid     TEXT UNIQUE,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,            -- LONG | SHORT
    status        TEXT NOT NULL,            -- OPEN | CLOSED
    qty           REAL NOT NULL,            -- contracts
    contract_size REAL NOT NULL DEFAULT 1,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    leverage      INTEGER NOT NULL,
    margin_usd    REAL NOT NULL,
    notional_usd  REAL NOT NULL,
    sl_price      REAL,
    tp_price      REAL,
    sl_roi_pct    REAL,
    tp_roi_pct    REAL,
    peak_roi_pct  REAL DEFAULT 0,
    trail_active  INTEGER DEFAULT 0,
    trail_stop_roi REAL,
    stop_price    REAL,
    stop_order_id TEXT,
    tp_order_id   TEXT,
    entry_order_id TEXT,
    atr           REAL,
    realized_pnl  REAL DEFAULT 0,
    fees_usd      REAL DEFAULT 0,
    roi_pct       REAL,
    exit_reason   TEXT,
    signal_id     INTEGER,
    opened_at     REAL,
    closed_at     REAL,
    closed_ts     REAL,
    meta          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_at);

CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    symbol     TEXT NOT NULL,
    side       TEXT NOT NULL,
    kind       TEXT,                        -- regular | hidden
    score      REAL,
    price      REAL,
    status     TEXT,                        -- executed | rejected | expired | filtered
    reason     TEXT,
    filters    TEXT,                        -- JSON: per-filter results
    features   TEXT,                        -- JSON: indicator context
    trade_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);

CREATE TABLE IF NOT EXISTS equity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    equity         REAL NOT NULL,
    available      REAL,
    unrealized     REAL,
    realized_today REAL,
    open_positions INTEGER,
    mode           TEXT
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    symbol          TEXT,
    kind            TEXT,                   -- entry | sl | tp | trail | close | leverage
    side            TEXT,
    price           REAL,
    vol             REAL,
    exchange_order_id TEXT,
    status          TEXT,
    latency_ms      REAL,
    error           TEXT,
    request         TEXT,
    response        TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS candles (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL DEFAULT 'Min5',
    ts       INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (symbol, interval, ts)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._migrate()
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        `candles` is a pure cache, so an outdated layout is simply dropped and
        rebuilt (the old table keyed on (symbol, ts) would collide Min5 and
        Min15 bars for the same symbol).
        """
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(candles)").fetchall()]
        except sqlite3.Error:
            return
        if cols and "interval" not in cols:
            self._conn.execute("DROP TABLE candles")
            self._conn.commit()

    # ------------------------------------------------------------------ #
    #  low level
    # ------------------------------------------------------------------ #
    def _exec(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            self._conn.commit()
            return rows

    async def exec(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        return await asyncio.to_thread(self._exec, sql, params)

    def _query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self._exec(sql, params)
        return rows[0] if rows else None

    async def _query_one_async(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = await self.exec(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    #  key/value state (restart-safe bot state, credentials, counters)
    # ------------------------------------------------------------------ #
    async def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = await self.exec("SELECT value FROM kv WHERE key=?", (key,))
        return row[0]["value"] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.exec(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def kv_delete(self, key: str) -> None:
        await self.exec("DELETE FROM kv WHERE key=?", (key,))

    async def kv_get_json(self, key: str, default: Any = None) -> Any:
        raw = await self.kv_get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    async def kv_set_json(self, key: str, value: Any) -> None:
        await self.kv_set(key, json.dumps(value, separators=(",", ":"), default=str))

    # ------------------------------------------------------------------ #
    #  trades
    # ------------------------------------------------------------------ #
    async def insert_trade(self, trade: Dict[str, Any]) -> int:
        cols = list(trade.keys())
        sql = (
            f"INSERT INTO trades ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        )
        with self._lock:
            cur = self._conn.execute(sql, [trade[c] for c in cols])
            self._conn.commit()
            return int(cur.lastrowid)

    async def update_trade(self, trade_id: int, patch: Dict[str, Any]) -> None:
        if not patch:
            return
        cols = list(patch.keys())
        sql = f"UPDATE trades SET {','.join(f'{c}=?' for c in cols)} WHERE id=?"
        await self.exec(sql, [patch[c] for c in cols] + [trade_id])

    async def update_trade_by_uid(self, uid: str, patch: Dict[str, Any]) -> None:
        if not patch:
            return
        cols = list(patch.keys())
        sql = f"UPDATE trades SET {','.join(f'{c}=?' for c in cols)} WHERE trade_uid=?"
        await self.exec(sql, [patch[c] for c in cols] + [uid])

    async def get_open_trades(self) -> List[Dict[str, Any]]:
        rows = await self.exec("SELECT * FROM trades WHERE status='OPEN' ORDER BY opened_at")
        return [dict(r) for r in rows]

    async def get_trades(
        self, limit: int = 100, offset: int = 0, symbol: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where, params = [], []
        if symbol:
            where.append("symbol=?")
            params.append(symbol)
        if status:
            where.append("status=?")
            params.append(status)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = await self.exec(
            f"SELECT * FROM trades {clause} ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        return [dict(r) for r in rows]

    async def count_trades(self, status: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM trades" + (" WHERE status=?" if status else "")
        rows = await self.exec(sql, (status,) if status else ())
        return int(rows[0]["n"]) if rows else 0

    async def closed_trades_since(self, since_ts: float) -> List[Dict[str, Any]]:
        rows = await self.exec(
            "SELECT * FROM trades WHERE status='CLOSED' AND closed_at >= ? ORDER BY closed_at",
            (since_ts,),
        )
        return [dict(r) for r in rows]

    async def last_closed_trade(self, symbol: str) -> Optional[Dict[str, Any]]:
        row = await self._query_one_async(
            "SELECT * FROM trades WHERE symbol=? AND status='CLOSED' ORDER BY closed_at DESC LIMIT 1",
            (symbol,),
        )
        return dict(row) if row else None

    async def trade_stats(self) -> Dict[str, Any]:
        rows = await self.exec(
            """SELECT COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(realized_pnl) pnl, SUM(fees_usd) fees,
                      AVG(CASE WHEN realized_pnl>0 THEN realized_pnl END) avg_win,
                      AVG(CASE WHEN realized_pnl<=0 THEN realized_pnl END) avg_loss,
                      AVG(roi_pct) avg_roi, MAX(roi_pct) best_roi, MIN(roi_pct) worst_roi
               FROM trades WHERE status='CLOSED'"""
        )
        base = dict(rows[0]) if rows else {}
        n = base.get("n") or 0
        wins = base.get("wins") or 0
        return {
            "trades": int(n),
            "wins": int(wins),
            "losses": int(n - wins),
            "win_rate": round(100.0 * wins / n, 2) if n else 0.0,
            "total_pnl": round(base.get("pnl") or 0.0, 4),
            "total_fees": round(base.get("fees") or 0.0, 4),
            "avg_win": round(base.get("avg_win") or 0.0, 4),
            "avg_loss": round(base.get("avg_loss") or 0.0, 4),
            "avg_roi": round(base.get("avg_roi") or 0.0, 2),
            "best_roi": round(base.get("best_roi") or 0.0, 2),
            "worst_roi": round(base.get("worst_roi") or 0.0, 2),
        }

    async def trade_stats_map(self) -> Dict[str, Dict[str, Any]]:
        """Per-symbol closed-trade statistics (used by the quality scorer)."""
        rows = await self.exec(
            """SELECT symbol, COUNT(*) n, SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                      SUM(realized_pnl) pnl
               FROM trades WHERE status='CLOSED' GROUP BY symbol"""
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            n = r["n"] or 0
            out[r["symbol"]] = {
                "trades": n,
                "win_rate": round(100.0 * (r["wins"] or 0) / n, 2) if n else 0.0,
                "pnl": round(r["pnl"] or 0.0, 4),
            }
        return out

    # ------------------------------------------------------------------ #
    #  signals
    # ------------------------------------------------------------------ #
    async def insert_signal(self, sig: Dict[str, Any]) -> int:
        cols = list(sig.keys())
        sql = f"INSERT INTO signals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self._lock:
            cur = self._conn.execute(sql, [sig[c] for c in cols])
            self._conn.commit()
            return int(cur.lastrowid)

    async def update_signal(self, signal_id: int, patch: Dict[str, Any]) -> None:
        if not patch:
            return
        cols = list(patch.keys())
        await self.exec(
            f"UPDATE signals SET {','.join(f'{c}=?' for c in cols)} WHERE id=?",
            [patch[c] for c in cols] + [signal_id],
        )

    async def get_signals(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM signals"
        params: List[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = await self.exec(sql, params)
        return [dict(r) for r in rows]

    async def last_signal_ts(self, symbol: str) -> float:
        row = await self._query_one_async("SELECT MAX(ts) AS t FROM signals WHERE symbol=?", (symbol,))
        return float(row["t"]) if row and row["t"] else 0.0

    # ------------------------------------------------------------------ #
    #  equity curve
    # ------------------------------------------------------------------ #
    async def insert_equity(self, snap: Dict[str, Any]) -> None:
        cols = ["ts", "equity", "available", "unrealized", "realized_today", "open_positions", "mode"]
        await self.exec(
            f"INSERT INTO equity ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [snap.get(c) for c in cols],
        )

    async def get_equity_curve(self, limit: int = 2880, since: Optional[float] = None) -> List[Dict[str, Any]]:
        if since:
            rows = await self.exec(
                "SELECT * FROM equity WHERE ts >= ? ORDER BY ts LIMIT ?", (since, limit)
            )
        else:
            rows = await self.exec(
                "SELECT * FROM equity ORDER BY ts DESC LIMIT ?", (limit,)
            )
            rows = list(reversed(rows))
        return [dict(r) for r in rows]

    async def downsample_equity(self, max_points: int = 400) -> List[Dict[str, Any]]:
        rows = await self.exec("SELECT ts, equity, open_positions FROM equity ORDER BY ts")
        data = [dict(r) for r in rows]
        if len(data) <= max_points:
            return data
        stride = max(1, len(data) // max_points)
        sampled = data[::stride]
        if sampled[-1] is not data[-1]:
            sampled.append(data[-1])
        return sampled

    # ------------------------------------------------------------------ #
    #  orders (latency + audit trail)
    # ------------------------------------------------------------------ #
    async def insert_order(self, order: Dict[str, Any]) -> None:
        cols = list(order.keys())
        await self.exec(
            f"INSERT INTO orders ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [order[c] for c in cols],
        )

    async def recent_orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = await self.exec("SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def latency_stats(self) -> Dict[str, Any]:
        rows = await self.exec("SELECT latency_ms FROM orders WHERE latency_ms IS NOT NULL ORDER BY id DESC LIMIT 500")
        vals = [float(r["latency_ms"]) for r in rows]
        if not vals:
            return {"count": 0}
        vals_sorted = sorted(vals)

        def pct(q: float) -> float:
            idx = int(q / 100.0 * (len(vals_sorted) - 1))
            return round(vals_sorted[idx], 2)

        return {
            "count": len(vals),
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
            "max": round(max(vals), 2),
            "avg": round(sum(vals) / len(vals), 2),
        }

    # ------------------------------------------------------------------ #
    #  candles (local cache, used by the paper engine & backtests)
    # ------------------------------------------------------------------ #
    async def upsert_candles(self, symbol: str, interval: str, rows: List[Sequence[Any]]) -> None:
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO candles(symbol,interval,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(symbol,interval,ts) DO UPDATE SET "
                "o=excluded.o,h=excluded.h,l=excluded.l,c=excluded.c,v=excluded.v",
                [(symbol, interval, *r[:6]) for r in rows],
            )
            self._conn.commit()

    async def get_candles(self, symbol: str, interval: str = "Min5", limit: int = 500) -> List[sqlite3.Row]:
        rows = await self.exec(
            "SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND interval=? ORDER BY ts DESC LIMIT ?",
            (symbol, interval, limit),
        )
        return list(reversed(rows))

    # ------------------------------------------------------------------ #
    async def housekeeping(self, keep_days: int = 30) -> None:
        cutoff = time.time() - keep_days * 86400
        await self.exec("DELETE FROM equity WHERE ts < ?", (cutoff,))
        await self.exec("DELETE FROM orders WHERE ts < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
