"""Layered configuration: config.toml defaults + data/settings.json overrides.

Every runtime-tunable knob is addressable with a dotted key (e.g. ``risk.leverage``)
so the dashboard settings API can patch individual values without rewriting files.

Design goals
------------
* Zero heavy dependencies (``tomllib`` is stdlib in 3.11+).
* Strict validation: a bad value from the UI can never reach the order router.
* Atomic persistence: settings survive restarts and bot crashes.
"""
from __future__ import annotations

import json
import os
import threading
import tomllib
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.toml"


# --------------------------------------------------------------------------- #
#  Validation table: dotted key -> (type, min, max) or ("enum", [choices])
#  Anything not listed here is not settable at runtime.
# --------------------------------------------------------------------------- #
def _num(lo: float, hi: float) -> Tuple[str, float, float]:
    return ("number", lo, hi)


VALIDATORS: Dict[str, Any] = {
    # app / exchange
    "app.mode": ("enum", ["paper", "live"]),
    "app.log_level": ("enum", ["DEBUG", "INFO", "WARNING", "ERROR"]),
    "exchange.recv_window_ms": _num(1000, 60000),
    "exchange.request_timeout_s": _num(0.5, 30),
    "exchange.entry_order_type": ("enum", ["market", "ioc_limit"]),
    "exchange.paper_data_source": ("enum", ["auto", "live", "synthetic"]),
    "exchange.ioc_limit_buffer_bps": _num(0, 100),
    "exchange.exit_order_type": ("enum", ["market", "ioc_limit"]),
    "exchange.set_leverage_on_entry": ("bool",),
    "exchange.position_mode": ("enum", [1, 2]),
    # account
    "account.paper_starting_equity": _num(10, 100_000_000),
    # risk
    "risk.equity_per_trade_pct": _num(0.5, 100),
    "risk.leverage": _num(1, 200),
    "risk.max_open_positions": _num(1, 50),
    "risk.max_positions_per_symbol": _num(1, 5),
    "risk.max_total_margin_pct": _num(5, 100),
    "risk.max_daily_loss_pct": _num(1, 100),
    "risk.max_drawdown_halt_pct": _num(1, 100),
    "risk.cooldown_after_loss_min": _num(0, 1440),
    "risk.cooldown_after_win_min": _num(0, 1440),
    "risk.min_notional_usd": _num(0.1, 1_000_000),
    "risk.risk_recalc_interval_s": _num(1, 300),
    # stoploss
    "stoploss.mode": ("enum", ["auto", "attached", "separate"]),
    "stoploss.atr_multiplier": _num(0.2, 20),
    "stoploss.atr_period": _num(2, 100),
    "stoploss.use_mark_price_trigger": ("bool",),
    "stoploss.local_watchdog": ("bool",),
    "stoploss.watchdog_grace_bps": _num(0, 100),
    "stoploss.min_sl_roi_pct": _num(0.5, 1000),
    "stoploss.max_sl_roi_pct": _num(1, 5000),
    # takeprofit
    "takeprofit.tp_roi_pct": _num(5, 100000),
    "takeprofit.exchange_side": ("bool",),
    "takeprofit.close_remainder_on_tp": ("bool",),
    # trailing
    "trailing.enabled": ("bool",),
    "trailing.trail_start_roi": _num(0, 100000),
    "trailing.trail_initial_stop_roi": _num(-1000, 100000),
    "trailing.trail_step_roi": _num(0.1, 10000),
    "trailing.trail_stop_step_roi": _num(0.1, 10000),
    "trailing.ratchet_only": ("bool",),
    "trailing.step_only_updates": ("bool",),
    "trailing.use_mark_price_for_peak": ("bool",),
    "trailing.persist_state": ("bool",),
    "trailing.replace_stop_on_step": ("bool",),
    "trailing.min_move_bps": _num(0, 200),
    # strategy
    "strategy.timeframe": ("enum", ["Min1", "Min5", "Min15", "Min30", "Min60"]),
    "strategy.candle_history": _num(60, 2000),
    "strategy.ao_fast": _num(2, 100),
    "strategy.ao_slow": _num(5, 200),
    "strategy.pivot_k": _num(1, 10),
    "strategy.lookback_bars": _num(10, 400),
    "strategy.min_pivot_gap": _num(2, 200),
    "strategy.max_pivot_gap": _num(3, 400),
    "strategy.min_ao_delta_atr": _num(0, 10),
    "strategy.require_ao_extreme": ("bool",),
    "strategy.require_trigger_break": ("bool",),
    "strategy.trigger_lookback": _num(1, 20),
    "strategy.allow_hidden_divergence": ("bool",),
    "strategy.signal_cooldown_bars": _num(0, 100),
    "strategy.max_signals_per_cycle": _num(1, 20),
    "strategy.signal_expiry_bars": _num(1, 20),
    "strategy.min_signal_score": _num(0, 100),
    # universe
    "universe.enabled": ("bool",),
    "universe.max_symbols": _num(1, 100),
    "universe.refresh_sec": _num(30, 3600),
    "universe.min_turnover_24h_usd": _num(0, 10_000_000_000),
    "universe.min_atr_pct": _num(0, 100),
    "universe.max_atr_pct": _num(0.01, 500),
    "universe.min_open_interest_usd": _num(0, 10_000_000_000),
    "universe.require_api_allowed": ("bool",),
    "universe.exclude_new_listings": ("bool",),
    "universe.exclude_stable_pairs": ("bool",),
    "universe.min_max_leverage": _num(1, 200),
    "universe.blacklist": ("list",),
    "universe.whitelist": ("list",),
    # target
    "target.equity_target": _num(10, 1_000_000_000),
    "target.days": _num(1, 365),
    "target.compounding": ("bool",),
    "target.monte_carlo_runs": _num(1000, 200000),
    # web
    "web.port": _num(1, 65535),
    "web.api_token": ("str",),
    "persistence.equity_snapshot_sec": _num(5, 3600),
}

# Filter knobs are pattern-validated (filters.<name>.<param>).
_FILTER_VALIDATORS: Dict[str, Any] = {
    "enabled": ("bool",),
    "min_atr_pct": _num(0, 100),
    "max_atr_pct": _num(0.01, 500),
    "min_atr_percentile": _num(0, 100),
    "ema_period": _num(5, 1000),
    "mode": ("enum", ["ema", "ema_stack", "off"]),
    "ema_stack_fast": _num(5, 1000),
    "slope_lookback": _num(1, 200),
    "require_slope": ("bool",),
    "htf_timeframe": ("enum", ["Min5", "Min15", "Min30", "Min60", "Hour4"]),
    "htf_ema_period": _num(5, 1000),
    "require_htf_ao_rising": ("bool",),
    "volume_sma_period": _num(2, 200),
    "min_volume_mult": _num(0, 100),
    "min_turnover_24h_usd": _num(0, 10_000_000_000),
    "require_volume_climax": ("bool",),
    "rsi_period": _num(2, 100),
    "rsi_long_max": _num(1, 100),
    "rsi_short_min": _num(0, 99),
    "require_rsi_divergence": ("bool",),
    "macd_confirm": ("bool",),
    "adx_period": _num(2, 100),
    "min_adx": _num(0, 100),
    "require_bb_expansion": ("bool",),
    "max_spread_bps": _num(0.1, 1000),
    "min_depth_mult": _num(0, 1000),
    "max_candle_atr": _num(0.1, 100),
    "block_before_funding_sec": _num(0, 3600),
}


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce_and_validate(key: str, value: Any) -> Any:
    """Validate a dotted key/value pair, returning the coerced value."""
    spec = VALIDATORS.get(key)
    if spec is None and key.startswith("filters."):
        spec = _FILTER_VALIDATORS.get(key.rsplit(".", 1)[-1])
    if spec is None:
        raise KeyError(f"unknown or read-only setting: {key}")
    return _coerce(key, value, spec)


def _coerce(key: str, value: Any, spec: Any) -> Any:
    kind = spec[0]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if kind == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: expected a number, got {value!r}")
        lo, hi = spec[1], spec[2]
        if not (lo <= num <= hi):
            raise ValueError(f"{key}: {num} out of range [{lo}, {hi}]")
        return int(num) if num.is_integer() and isinstance(spec, tuple) and _is_int_like(key) else num
    if kind == "enum":
        for allowed in spec[1]:
            if value == allowed or str(value).lower() == str(allowed).lower():
                return allowed
        raise ValueError(f"{key}: must be one of {spec[1]}")
    if kind == "list":
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        if isinstance(value, (list, tuple)):
            return [str(s).strip() for s in value]
        raise ValueError(f"{key}: expected a list or comma-separated string")
    if kind == "str":
        return "" if value is None else str(value)
    raise ValueError(f"{key}: unsupported spec {spec}")


_INT_KEYS = {
    "risk.max_open_positions", "risk.max_positions_per_symbol", "risk.leverage",
    "exchange.recv_window_ms", "stoploss.atr_period", "strategy.ao_fast",
    "strategy.ao_slow", "strategy.pivot_k", "strategy.lookback_bars",
    "strategy.min_pivot_gap", "strategy.max_pivot_gap", "strategy.trigger_lookback",
    "strategy.signal_cooldown_bars", "strategy.max_signals_per_cycle",
    "strategy.signal_expiry_bars", "universe.max_symbols", "universe.refresh_sec",
    "target.monte_carlo_runs", "web.port", "persistence.equity_snapshot_sec",
    "risk.risk_recalc_interval_s",
}


def _is_int_like(key: str) -> bool:
    return key in _INT_KEYS


class Config:
    """Thread-safe layered configuration store."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or DEFAULT_CONFIG_PATH)
        # Relative paths in the config (data_dir, db_path) resolve against the
        # *config file's* directory, so an alternative config is fully isolated.
        self.base_dir = self.path.parent
        self._lock = threading.RLock()
        self._base: Dict[str, Any] = {}
        self._overrides: Dict[str, Any] = {}
        self._data: Dict[str, Any] = {}
        self.reload()

    # -- loading ---------------------------------------------------------- #
    def resolve(self, path_like: str) -> Path:
        """Resolve a config path against the config file's directory."""
        candidate = Path(path_like)
        return candidate if candidate.is_absolute() else (self.base_dir / candidate)

    @property
    def overrides_path(self) -> Path:
        data_dir = Path(self._base.get("app", {}).get("data_dir", "data"))
        return self.resolve(str(data_dir)) / "settings.json"

    def reload(self) -> None:
        with self._lock:
            with open(self.path, "rb") as fh:
                self._base = tomllib.load(fh)
            self._overrides = {}
            op = self.overrides_path
            if op.exists():
                try:
                    self._overrides = json.loads(op.read_text() or "{}")
                except (json.JSONDecodeError, OSError):
                    self._overrides = {}
            self._data = _deep_merge(self._base, self._overrides)

    def save_overrides(self) -> None:
        with self._lock:
            op = self.overrides_path
            op.parent.mkdir(parents=True, exist_ok=True)
            tmp = op.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._overrides, indent=2, sort_keys=True))
            os.replace(tmp, op)

    # -- access ------------------------------------------------------------ #
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._data
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def section(self, name: str) -> Dict[str, Any]:
        with self._lock:
            node = self._data.get(name, {})
            return dict(node) if isinstance(node, dict) else {}

    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def set(self, dotted: str, value: Any) -> Any:
        """Validate + persist a single override. Returns the coerced value."""
        coerced = _coerce_and_validate(dotted, value)
        with self._lock:
            # semantic cross-checks
            if dotted == "strategy.ao_fast" and coerced >= self.get("strategy.ao_slow", 34):
                raise ValueError("strategy.ao_fast must be < strategy.ao_slow")
            if dotted == "strategy.ao_slow" and coerced <= self.get("strategy.ao_fast", 5):
                raise ValueError("strategy.ao_slow must be > strategy.ao_fast")
            if dotted == "universe.min_atr_pct" and coerced > self.get("universe.max_atr_pct", 6.0):
                raise ValueError("universe.min_atr_pct must be <= universe.max_atr_pct")
            if dotted.startswith("filters.volatility.min_atr_pct") and coerced > self.get("filters.volatility.max_atr_pct", 4.0):
                raise ValueError("min_atr_pct must be <= max_atr_pct")
            node = self._overrides
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = coerced
            self._data = _deep_merge(self._base, self._overrides)
        self.save_overrides()
        return coerced

    def set_many(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically apply many dotted keys; all-or-nothing."""
        coerced: Dict[str, Any] = {}
        for k, v in patch.items():
            coerced[k] = _coerce_and_validate(k, v)   # raises before anything is written
        with self._lock:
            for k, v in coerced.items():
                parts = k.split(".")
                node = self._overrides
                for part in parts[:-1]:
                    node = node.setdefault(part, {})
                node[parts[-1]] = v
            self._data = _deep_merge(self._base, self._overrides)
        self.save_overrides()
        return coerced

    def reset(self, keys: Optional[Iterable[str]] = None) -> None:
        with self._lock:
            if keys is None:
                self._overrides = {}
            else:
                for k in keys:
                    parts = k.split(".")
                    node: Any = self._overrides
                    stack = []
                    ok = True
                    for part in parts[:-1]:
                        stack.append((node, part))
                        node = node.get(part) if isinstance(node, dict) else None
                        if node is None:
                            ok = False
                            break
                    if ok and isinstance(node, dict):
                        node.pop(parts[-1], None)
                        # prune empties
                        for parent, part in reversed(stack):
                            if isinstance(parent.get(part), dict) and not parent[part]:
                                parent.pop(part, None)
            self._data = _deep_merge(self._base, self._overrides)
        self.save_overrides()

    # -- convenience ------------------------------------------------------- #
    @property
    def data_dir(self) -> Path:
        return self.resolve(str(self.get("app.data_dir", "data")))

    @property
    def mode(self) -> str:
        return str(self.get("app.mode", "paper"))

    def public_dict(self) -> Dict[str, Any]:
        """Config view for the dashboard (secrets stripped)."""
        data = self.as_dict()
        data.setdefault("web", {})["api_token"] = "***" if self.get("web.api_token") else ""
        return data


def load(path: Optional[Path] = None) -> Config:
    return Config(path)
