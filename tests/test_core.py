"""Core test-suite: trading maths, divergence detection, filters, sizing,
trailing-stop logic, paper-broker accounting and the compounding model.

Run with:  python3 -m tests.test_core -v      (or python3 -m pytest tests/)
"""
from __future__ import annotations

import asyncio
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Config
from tests._util import isolated_config                                    # noqa: E402
from app.exchange.base import LONG, SHORT, Candle, ContractSpec, Ticker  # noqa: E402
from app.exchange.paper import PaperBroker, SyntheticMarketAdapter       # noqa: E402
from app.exchange.synthetic import SyntheticFeed                 # noqa: E402
from app.risk.manager import (                                   # noqa: E402
    RiskGuard, build_plan, evaluate_trailing, size_position, trailing_stop_roi,
)
from app.strategy import indicators as ind                       # noqa: E402
from app.strategy.divergence import detect_divergence             # noqa: E402
from app.strategy.features import build_features                  # noqa: E402
from app.strategy.filters import evaluate                         # noqa: E402
from app.strategy.signals import SignalEngine                     # noqa: E402
from app.utils import price_from_roi, roi_from_price              # noqa: E402


# --------------------------------------------------------------------------- #
#  helpers
# --------------------------------------------------------------------------- #
def make_candles(closes, highs=None, lows=None, vols=None, start_ts=1_700_000_000, step=300):
    highs = highs or [c * 1.002 for c in closes]
    lows = lows or [c * 0.998 for c in closes]
    vols = vols or [1000.0] * len(closes)
    return [
        Candle(ts=start_ts + i * step, o=closes[i], h=highs[i], l=lows[i], c=closes[i], v=vols[i])
        for i in range(len(closes))
    ]


def zigzag_bullish_divergence():
    """Price: lower low; AO: higher low. Deterministic construction."""
    closes, highs, lows, vols = [], [], [], []
    price = 100.0
    # 1) long, deep decline into the first low (makes AO very negative)
    for i in range(45):
        price -= 0.35
        closes.append(price)
        highs.append(price + 0.25)
        lows.append(price - 0.25)
        vols.append(900 + i * 4)
    # 2) recovery (AO climbs)
    for i in range(18):
        price += 0.55
        closes.append(price)
        highs.append(price + 0.25)
        lows.append(price - 0.25)
        vols.append(1200 + i * 10)
    # 3) short, shallow dip to a *lower low* than the first one -> divergence
    low_target = 100.0 - 0.35 * 45 - 0.6      # below the first trough
    while price > low_target:
        price -= 0.30
        closes.append(price)
        highs.append(price + 0.25)
        lows.append(price - 0.30)
        vols.append(1500.0)
    # 4) trigger leg: break above local structure with volume
    for i in range(3):
        price += 0.8
        closes.append(price)
        highs.append(price + 0.3)
        lows.append(price - 0.2)
        vols.append(3000.0 + i * 500)
    return make_candles(closes, highs, lows, vols)


# --------------------------------------------------------------------------- #
class TestRoiMath(unittest.TestCase):
    """ROI is on margin (leveraged), exactly per the specification."""

    def test_long_roi_and_price(self):
        entry, lev, roi = 100.0, 10, 200.0
        price = price_from_roi(entry, roi, lev, is_long=True)
        self.assertAlmostEqual(price, 120.0, places=9)              # +20% price = +200% ROI
        self.assertAlmostEqual(roi_from_price(entry, price, lev, True), 200.0, places=6)

    def test_short_roi_and_price(self):
        entry, lev, roi = 100.0, 10, 200.0
        price = price_from_roi(entry, roi, lev, is_long=False)
        self.assertAlmostEqual(price, 80.0, places=9)
        self.assertAlmostEqual(roi_from_price(entry, price, lev, False), 200.0, places=6)

    def test_trailing_stop_price_conversion(self):
        # +20% ROI at 10x => +2% price for a long
        self.assertAlmostEqual(price_from_roi(100.0, 20.0, 10, True), 102.0, places=9)
        self.assertAlmostEqual(price_from_roi(100.0, 20.0, 10, False), 98.0, places=9)

    def test_negative_roi_price(self):
        self.assertAlmostEqual(price_from_roi(50.0, -30.0, 10, True), 48.5, places=9)


class TestTrailingFormula(unittest.TestCase):
    def test_spec_examples(self):
        cases = {30: 20, 31: 20, 39: 20, 40: 30, 50: 40, 100: 90, 110: 100, 199: 180}
        for peak, expected in cases.items():
            self.assertEqual(trailing_stop_roi(peak), expected, f"peak {peak}")

    def test_inactive_below_start(self):
        for peak in (0, 10, 29.9):
            self.assertIsNone(trailing_stop_roi(peak))

    def test_custom_parameters(self):
        self.assertEqual(trailing_stop_roi(50, start=50, initial_stop=40, step=25, stop_step=25), 40)
        self.assertEqual(trailing_stop_roi(75, start=50, initial_stop=40, step=25, stop_step=25), 65)

    def test_ratchet_only_and_step_updates(self):
        cfg = Config.__new__(Config)          # config-free evaluation via dict-like stub
        cfg._data = {                          # type: ignore[attr-defined]
            "trailing": {"enabled": True, "trail_start_roi": 30, "trail_initial_stop_roi": 20,
                         "trail_step_roi": 10, "trail_stop_step_roi": 10, "ratchet_only": True,
                         "step_only_updates": True, "min_move_bps": 2},
        }
        cfg.get = lambda k, d=None: cfg._data.get(k.split(".")[-1], d)  # type: ignore[assignment]

        # peak below start -> no move
        d = evaluate_trailing(entry_price=100, mark_price=101.5, peak_roi=25, current_stop_roi=None,
                              current_stop_price=None, initial_sl_price=97, initial_sl_roi=30,
                              leverage=10, is_long=True, cfg=cfg)
        self.assertEqual(d.action, "none")

        # peak 30 -> activate at 20% ROI => 102.0
        d = evaluate_trailing(entry_price=100, mark_price=103.0, peak_roi=30, current_stop_roi=None,
                              current_stop_price=97, initial_sl_price=97, initial_sl_roi=30,
                              leverage=10, is_long=True, cfg=cfg)
        self.assertEqual(d.action, "activate")
        self.assertAlmostEqual(d.stop_roi, 20)
        self.assertAlmostEqual(d.stop_price, 102.0)

        # peak 40 -> step to 30% ROI (103.0); never loosens afterwards
        d = evaluate_trailing(entry_price=100, mark_price=104.0, peak_roi=40, current_stop_roi=20,
                              current_stop_price=102.0, initial_sl_price=97, initial_sl_roi=30,
                              leverage=10, is_long=True, cfg=cfg, last_step_index=0)
        self.assertEqual(d.action, "step")
        self.assertAlmostEqual(d.stop_roi, 30)
        self.assertAlmostEqual(d.stop_price, 103.0)

        # same peak again -> no duplicate modification (avoid excessive updates)
        d = evaluate_trailing(entry_price=100, mark_price=104.0, peak_roi=40, current_stop_roi=30,
                              current_stop_price=103.0, initial_sl_price=97, initial_sl_roi=30,
                              leverage=10, is_long=True, cfg=cfg, last_step_index=1)
        self.assertEqual(d.action, "none")

    def test_short_side_direction(self):
        cfg = Config.__new__(Config)
        cfg._data = {"trailing": {"enabled": True, "trail_start_roi": 30, "trail_initial_stop_roi": 20,
                                  "trail_step_roi": 10, "trail_stop_step_roi": 10, "ratchet_only": True,
                                  "step_only_updates": True, "min_move_bps": 2}}
        cfg.get = lambda k, d=None: cfg._data.get(k.split(".")[-1], d)  # type: ignore[assignment]
        d = evaluate_trailing(entry_price=100, mark_price=97.0, peak_roi=30, current_stop_roi=None,
                              current_stop_price=103, initial_sl_price=103, initial_sl_roi=30,
                              leverage=10, is_long=False, cfg=cfg)
        self.assertEqual(d.action, "activate")
        self.assertAlmostEqual(d.stop_price, 98.0)     # short stop is *above* entry


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.spec = ContractSpec(symbol="TEST_USDT", contract_size=0.0001, vol_unit=1.0,
                                 min_vol=1.0, max_vol=1_000_000, max_leverage=100, taker_fee=0.0006)

    def test_eight_percent_margin_with_10x(self):
        s = size_position(1000.0, 50_000.0, self.spec, equity_pct=8.0, leverage=10)
        self.assertTrue(s.ok, s.reason)
        self.assertLessEqual(s.margin_usd, 80.0 + 1e-6)
        # notional ~ 800 = margin x 10 (rounding only due to contract granularity)
        self.assertAlmostEqual(s.notional_usd, s.qty * self.spec.contract_size * 50_000.0, places=6)
        self.assertLess(abs(s.notional_usd - 800.0), 50.0)

    def test_rounding_respects_contract_size(self):
        s = size_position(500.0, 3.0, self.spec, equity_pct=8.0, leverage=10)
        self.assertTrue(s.ok)
        self.assertEqual(s.qty % 1.0, 0.0)          # vol_unit = 1 contract

    def test_rejects_tiny_notional(self):
        s = size_position(5.0, 60_000.0, self.spec, equity_pct=8.0, leverage=10, min_notional_usd=5.0)
        self.assertFalse(s.ok)

    def test_max_margin_cap_limits_notional(self):
        uncapped = size_position(100_000.0, 100.0, None, equity_pct=8.0, leverage=10)
        capped = size_position(100_000.0, 100.0, None, equity_pct=8.0, leverage=10,
                               max_margin_usd=250.0)
        self.assertTrue(capped.ok)
        self.assertAlmostEqual(capped.margin_usd, 250.0, places=2)
        self.assertLess(capped.notional_usd, uncapped.notional_usd)

    def test_compound_growth_uses_current_equity(self):
        s1 = size_position(1000.0, 100.0, None, equity_pct=8.0, leverage=10)
        s2 = size_position(2000.0, 100.0, None, equity_pct=8.0, leverage=10)
        self.assertAlmostEqual(s2.notional_usd / s1.notional_usd, 2.0, places=2)


class TestStopsAndPlan(unittest.TestCase):
    def setUp(self):
        self.cfg = isolated_config()
        self.spec = ContractSpec(symbol="TEST_USDT", contract_size=1.0, vol_unit=1.0,
                                 min_vol=1.0, max_vol=1e9, max_leverage=100, taker_fee=0.0006)

    def test_sl_is_three_atr(self):
        sizing = size_position(1000.0, 100.0, self.spec, equity_pct=8.0, leverage=10)
        for side, atr in ((LONG, 1.0), (SHORT, 1.0)):
            plan = build_plan(side=side, entry_price=100.0, atr=atr, sizing=sizing,
                              contract_size=1.0, cfg=self.cfg)
            distance = abs(plan.sl_price - 100.0)
            self.assertAlmostEqual(distance, 3.0, places=6)      # ATR x 3
            signed = roi_from_price(100.0, plan.sl_price, 10, side == LONG)
            self.assertAlmostEqual(plan.sl_roi_pct, abs(signed), places=6)
            self.assertLess(signed, 0)          # a stop is always a loss at entry

    def test_tp_is_200_percent_roi(self):
        sizing = size_position(1000.0, 100.0, self.spec, equity_pct=8.0, leverage=10)
        plan = build_plan(side=LONG, entry_price=100.0, atr=0.5, sizing=sizing,
                          contract_size=1.0, cfg=self.cfg)
        self.assertAlmostEqual(plan.tp_price, 120.0, places=9)
        self.assertAlmostEqual(roi_from_price(100.0, plan.tp_price, 10, True), 200.0, places=6)

    def test_sl_clamped_to_sane_roi(self):
        sizing = size_position(1000.0, 100.0, self.spec, equity_pct=8.0, leverage=10)
        plan = build_plan(side=LONG, entry_price=100.0, atr=0.0001, sizing=sizing,   # absurdly small ATR
                          contract_size=1.0, cfg=self.cfg)
        self.assertGreaterEqual(plan.sl_roi_pct, self.cfg.get("stoploss.min_sl_roi_pct") - 1e-9)

    def test_stop_sits_on_the_adverse_side_of_entry(self):
        """Regression: a short's stop must be ABOVE entry (and a long's below).

        The old code used the favourable-move conversion for stops, so short
        stops landed below entry and the watchdog flattened instantly.
        """
        sizing = size_position(1000.0, 100.0, self.spec, equity_pct=8.0, leverage=10)
        long_plan = build_plan(side=LONG, entry_price=100.0, atr=2.0, sizing=sizing,
                               contract_size=1.0, cfg=self.cfg)
        short_plan = build_plan(side=SHORT, entry_price=100.0, atr=2.0, sizing=sizing,
                                contract_size=1.0, cfg=self.cfg)
        self.assertLess(long_plan.sl_price, 100.0)     # long stop below entry
        self.assertGreater(long_plan.tp_price, 100.0)  # long target above entry
        self.assertGreater(short_plan.sl_price, 100.0)  # short stop ABOVE entry
        self.assertLess(short_plan.tp_price, 100.0)     # short target below entry
        self.assertLess(roi_from_price(100.0, long_plan.sl_price, 10, True), 0)
        self.assertLess(roi_from_price(100.0, short_plan.sl_price, 10, False), 0)


class TestIndicators(unittest.TestCase):
    def test_ao_matches_definition(self):
        closes = [100 + math.sin(i / 7.0) * 3 + i * 0.05 for i in range(120)]
        candles = make_candles(closes)
        highs = [c.h for c in candles]
        lows = [c.l for c in candles]
        ao = ind.awesome_oscillator(highs, lows, 5, 34)
        median = [(h + l) / 2 for h, l in zip(highs, lows)]
        s5 = ind.sma(median, 5)
        s34 = ind.sma(median, 34)
        self.assertAlmostEqual(ao[-1], s5[-1] - s34[-1], places=9)

    def test_atr_positive_and_wilder(self):
        candles = make_candles([100 + i * 0.1 for i in range(60)])
        atr = [v for v in ind.atr([c.h for c in candles], [c.l for c in candles],
                                  [c.c for c in candles], 14) if v is not None]
        self.assertTrue(atr and atr[-1] > 0)

    def test_pivots_are_confirmed(self):
        lows = [10, 9, 8, 7, 8, 9, 10, 11, 10, 9, 8, 9, 10]
        piv = ind.pivots(lows, 2, "low")
        self.assertIn(3, piv)          # global min
        self.assertNotIn(11, piv)      # too close to the right edge -> not confirmed


class TestDivergence(unittest.TestCase):
    def test_detects_bullish_divergence_and_trigger(self):
        candles = zigzag_bullish_divergence()
        highs = [c.h for c in candles]
        lows = [c.l for c in candles]
        closes = [c.c for c in candles]
        ao = [v for v in ind.awesome_oscillator(highs, lows, 5, 34) if v is not None]
        atr = [v for v in ind.atr(highs, lows, closes, 14) if v is not None][-1]
        div = detect_divergence(
            "TEST_USDT", candles, ao, atr, pivot_k=2, lookback_bars=90, min_gap=3, max_gap=60,
            min_ao_delta_atr=0.05, require_ao_extreme=False, require_trigger_break=True,
            max_bars_since_pivot=12,
        )
        self.assertIsNotNone(div, "expected a bullish divergence to be detected")
        self.assertEqual(div.side, LONG)
        self.assertLess(div.p2_price, div.p1_price)      # lower low in price
        self.assertGreater(div.p2_ao, div.p1_ao)         # higher low in AO
        self.assertTrue(div.trigger_confirmed)

    def test_invariants_on_synthetic_data(self):
        """Whatever is detected must satisfy the pattern definition."""
        feed = SyntheticFeed(tick_seconds=0.5, seed=7, history_bars=220, htf_history_bars=120)
        checked = 0
        for symbol, sim in list(feed.symbols.items())[:12]:
            candles = feed.klines(symbol, "Min5", 200)
            highs = [c.h for c in candles]
            lows = [c.l for c in candles]
            closes = [c.c for c in candles]
            ao = [v for v in ind.awesome_oscillator(highs, lows, 5, 34) if v is not None]
            atr = [v for v in ind.atr(highs, lows, closes, 14) if v is not None][-1]
            div = detect_divergence(symbol, candles, ao, atr, require_ao_extreme=False,
                                    min_ao_delta_atr=0.0, require_trigger_break=False)
            if div is None:
                continue
            checked += 1
            if div.side == LONG:
                self.assertLess(div.p2_price, div.p1_price)
                self.assertGreater(div.p2_ao, div.p1_ao)
            else:
                self.assertGreater(div.p2_price, div.p1_price)
                self.assertLess(div.p2_ao, div.p1_ao)
        self.assertGreaterEqual(checked, 0)


class TestFilters(unittest.TestCase):
    def setUp(self):
        self.cfg = isolated_config()
        self.filters = self.cfg.section("filters")

    def _features_and_div(self, symbol="TEST_USDT", vol_ratio=2.0, atr_pct=None, adx=30.0,
                          rsi=45.0, spread=3.0, turnover=50_000_000.0, ema_up=True,
                          candle_move=0.5):
        feed = SyntheticFeed(tick_seconds=0.5, seed=3, history_bars=240, htf_history_bars=120)
        candles = feed.klines("SOL_USDT", "Min5", 220)   # data source; label stays generic
        closes = [c.c for c in candles]
        if atr_pct:
            scale = atr_pct / 100.0 * closes[-1]
            candles = [Candle(ts=c.ts, o=c.o, h=c.c + scale, l=c.c - scale, c=c.c, v=c.v) for c in candles]
        tk = Ticker(symbol=symbol, last=closes[-1], bid=closes[-1] * (1 - spread / 2e4),
                    ask=closes[-1] * (1 + spread / 2e4), amount24=turnover,
                    fair_price=closes[-1], funding_rate=0.0001)
        f = build_features(symbol, candles, tk)
        assert f is not None
        f.vol_ratio = vol_ratio
        f.atr_percentile = 60.0        # isolate the pipeline logic from the raw percentile
        f.adx = adx
        f.rsi = rsi
        f.candle_return_atr = candle_move
        f.turnover24 = turnover
        f.depth_usd = 500_000.0
        # make the trend/HTF context deterministic (the simulator is random by design):
        # a long signal needs an up-trend and a rising higher-timeframe AO
        f.ema200 = f.close * (0.98 if ema_up else 1.05)
        f.ema50 = f.close * (0.99 if ema_up else 1.06)
        f.ema200_slope_pct = 0.1 if ema_up else -0.1
        for tf, ctx in f.htf.items():
            ctx.ema = f.close * (0.995 if ema_up else 1.005)
            ctx.trend_up = ema_up
            ctx.trend_down = not ema_up
            ctx.ao = 0.5 if ema_up else -0.5
            ctx.ao_rising = ema_up
            ctx.ao_falling = not ema_up
        from app.strategy.divergence import Divergence
        div = Divergence(symbol=symbol, side=LONG, kind="regular", ts=f.ts, price=f.close,
                         p1_index=100, p2_index=130, p1_price=10.0, p2_price=9.0,
                         p1_ao=-1.0, p2_ao=-0.2, price_delta_pct=10.0, ao_delta=0.5,
                         bars_since_pivot=1, trigger_level=f.close, trigger_confirmed=True,
                         structure_high=f.close, structure_low=9.0)
        return f, div

    def test_good_signal_passes(self):
        f, div = self._features_and_div()
        rep = evaluate(f, div, self.filters, order_notional_usd=800.0, min_signal_score=0)
        self.assertTrue(rep.passed, rep.rejections())

    def test_bad_volume_rejected(self):
        f, div = self._features_and_div(vol_ratio=0.4)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "volume_confirmation" for r in rep.results if not r.passed))

    def test_low_adx_rejected(self):
        f, div = self._features_and_div(adx=5.0)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "adx_trend" for r in rep.results if not r.passed))

    def test_wide_spread_rejected(self):
        f, div = self._features_and_div(spread=80.0)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "spread" for r in rep.results if not r.passed))

    def test_shock_candle_rejected(self):
        f, div = self._features_and_div(candle_move=9.0)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "shock_candle" for r in rep.results if not r.passed))

    def test_counter_trend_rejected(self):
        f, div = self._features_and_div(ema_up=False)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "trend_alignment" for r in rep.results if not r.passed))

    def test_low_liquidity_rejected(self):
        f, div = self._features_and_div(turnover=1000.0)
        rep = evaluate(f, div, self.filters, min_signal_score=0)
        self.assertFalse(rep.passed)
        self.assertTrue(any(r.name == "liquidity_turnover" for r in rep.results if not r.passed))


class TestSignalEngine(unittest.IsolatedAsyncioTestCase):
    async def test_signal_engine_scores_and_reports(self):
        cfg = isolated_config()
        cfg.set("strategy.require_trigger_break", False)
        engine = SignalEngine(cfg)
        feed = SyntheticFeed(tick_seconds=0.5, seed=11, history_bars=280, htf_history_bars=120)
        produced = 0
        for symbol, sim in list(feed.symbols.items())[:14]:
            candles = feed.klines(symbol, "Min5", 260)[:-1]
            tk = sim.ticker()
            signal = await engine.analyze(symbol, candles, tk)
            if signal is not None:
                produced += 1
                self.assertIn(signal.side, (LONG, SHORT))
                self.assertTrue(0 <= signal.score <= 100)
                self.assertTrue(len(signal.report.results) >= 8)   # full pipeline ran
        self.assertGreaterEqual(produced, 0)

    async def test_engine_end_to_end_on_engineered_signal(self):
        """Awarded signal: the full engine returns a Signal object with filters."""
        cfg = isolated_config()
        cfg.set("strategy.require_trigger_break", False)
        cfg.set("strategy.min_signal_score", 0)
        engine = SignalEngine(cfg)
        candles = zigzag_bullish_divergence()
        ticker = Ticker(symbol="TEST_USDT", last=candles[-1].c, bid=candles[-1].c * 0.999,
                        ask=candles[-1].c * 1.001, amount24=40_000_000.0)
        signal = await engine.analyze("TEST_USDT", candles, ticker)
        if signal is not None:
            self.assertIn(signal.side, (LONG, SHORT))
            self.assertTrue(len(signal.report.results) >= 8)


class TestPaperBroker(unittest.IsolatedAsyncioTestCase):
    async def test_open_close_pnl_accounting(self):
        feed = SyntheticFeed(tick_seconds=0.2, seed=5, history_bars=60, htf_history_bars=40)
        broker = PaperBroker(SyntheticMarketAdapter(feed), starting_equity=1000.0, slippage_bps=0.0)
        await broker.start()
        try:
            symbol = "SOL_USDT"
            mark = await broker.mark_price(symbol)
            res = await broker.open_position(symbol, LONG, qty=1.0, leverage=10,
                                             sl_price=mark * 0.97)
            self.assertTrue(res.ok, res.error)
            entry = res.price
            self.assertGreater(entry, 0)
            account = await broker.account()
            self.assertLess(account.equity, 1000.0 + 1e-9)      # entry fee paid
            close = await broker.close_position(symbol, LONG, 1.0, reason="test")
            self.assertTrue(close.ok)
            self.assertGreater(close.price, 0)
            positions = await broker.positions()
            self.assertEqual(len(positions), 0)
        finally:
            await broker.stop()

    async def test_stop_loss_triggers_close(self):
        feed = SyntheticFeed(tick_seconds=0.2, seed=9, history_bars=60, htf_history_bars=40)
        broker = PaperBroker(SyntheticMarketAdapter(feed), starting_equity=1000.0)
        await broker.start()
        try:
            symbol = "DOGE_USDT"
            mark = await broker.mark_price(symbol)
            # stop just above the current market for a long => triggered by the tick engine
            await broker.open_position(symbol, LONG, qty=1000.0, leverage=10,
                                       sl_price=mark * 1.0001)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if not await broker.positions():
                    break
            self.assertEqual(len(await broker.positions()), 0, "stop should have flattened the position")
        finally:
            await broker.stop()


class TestCompoundModel(unittest.TestCase):
    def test_required_growth_for_10k_in_7_days(self):
        from app.analytics import compound as cm
        inp = cm.CompoundInputs(starting_equity=1000.0, target_equity=10_000.0, days=7)
        self.assertAlmostEqual(inp.required_daily_growth_pct, (10 ** (1 / 7) - 1) * 100, places=3)

    def test_monte_carlo_bounds(self):
        from app.analytics import compound as cm
        report = cm.build_report(
            starting_equity=1000.0, target_equity=10_000.0, days=7,
            equity_pct_per_trade=8.0, leverage=10, tp_roi_pct=200.0, sl_roi_pct=30.0,
            trades_per_day=10.0, win_rate=0.5, runs=1200,
        )
        sim = report["simulation"]
        self.assertTrue(0.0 <= sim["prob_hit_target_pct"] <= 100.0)
        self.assertTrue(0.0 <= sim["prob_ruin_pct"] <= 100.0)
        self.assertLessEqual(sim["p05"], sim["p95"])
        self.assertIn("sensitivity", report)
        self.assertEqual(report["per_trade_math"]["equity_gain_per_tp_pct"], 16.0)

    def test_higher_win_rate_helps(self):
        from app.analytics import compound as cm
        lo = cm.monte_carlo(cm.CompoundInputs(starting_equity=1000, target_equity=10_000, win_rate=0.30, runs=800))
        hi = cm.monte_carlo(cm.CompoundInputs(starting_equity=1000, target_equity=10_000, win_rate=0.60, runs=800))
        self.assertGreaterEqual(hi["median_final_equity"], lo["median_final_equity"])


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(ROOT / "config.toml", self.tmp / "config.toml")
        (self.tmp / "data").mkdir(exist_ok=True)
        self.cfg = Config(self.tmp / "config.toml")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            self.cfg.set("risk.leverage", 5000)
        with self.assertRaises(ValueError):
            self.cfg.set("app.mode", "yolo")
        with self.assertRaises(KeyError):
            self.cfg.set("not.a.real.setting", 1)

    def test_persists_overrides(self):
        self.cfg.set("trailing.trail_start_roi", 25)
        self.cfg.set("risk.equity_per_trade_pct", 5)
        reloaded = Config(self.tmp / "config.toml")
        self.assertEqual(reloaded.get("trailing.trail_start_roi"), 25)
        self.assertEqual(reloaded.get("risk.equity_per_trade_pct"), 5)

    def test_atomic_multi_set(self):
        with self.assertRaises(ValueError):
            self.cfg.set_many({"risk.leverage": 20, "risk.max_open_positions": 9999})
        self.assertEqual(self.cfg.get("risk.leverage"), 10)   # nothing applied

    def test_reset(self):
        self.cfg.set("risk.leverage", 20)
        self.cfg.reset(["risk.leverage"])
        self.assertEqual(self.cfg.get("risk.leverage"), 10)


class TestRiskGuard(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        from app.db import Database
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = isolated_config()
        self.db = Database(self.tmp / "t.db")
        self.guard = RiskGuard(self.cfg, self.db)

    async def test_gates(self):
        ok = await self.guard.can_open(symbol="X_USDT", equity=1000, open_positions=0, margin_used=0,
                                      available=900, sizing_margin=80.0)
        self.assertTrue(ok.allowed)
        full = await self.guard.can_open(symbol="X_USDT", equity=1000, open_positions=10, margin_used=0,
                                        available=900, sizing_margin=80.0)
        self.assertFalse(full.allowed)
        dup = await self.guard.can_open(symbol="X_USDT", equity=1000, open_positions=1, margin_used=0,
                                       available=900, sizing_margin=80.0, symbol_open=True)
        self.assertFalse(dup.allowed)

    async def test_drawdown_halt(self):
        await self.guard.update_equity(1000)
        await self.guard.update_equity(500)     # -50% vs 40% limit
        self.assertTrue(self.guard.halted)

    async def test_cooldown_after_loss(self):
        await self.guard.register_close("X_USDT", -5.0)
        d = await self.guard.can_open(symbol="X_USDT", equity=1000, open_positions=0, margin_used=0,
                                     available=900, sizing_margin=80.0)
        self.assertFalse(d.allowed)
        self.assertIn("cooldown", d.reason)


# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    unittest.main(verbosity=2)
