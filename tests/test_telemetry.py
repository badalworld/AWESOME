"""Telemetry regressions: quota percentages must come from venue headers, not guesses."""
from __future__ import annotations

import unittest

from app.utils import ExchangeRequestTracker


class ExchangeRequestTrackerTest(unittest.TestCase):
    def test_quota_fraction_uses_exchange_limit_and_remaining_headers(self):
        tracker = ExchangeRequestTracker()
        tracker.record_request()
        tracker.record_response(
            200,
            {
                "GW-RateLimit-Limit": "100",
                "GW-RateLimit-Remaining": "5",
            },
            12.345,
        )

        usage = tracker.snapshot()
        self.assertEqual(usage["requests_total"], 1)
        self.assertEqual(usage["requests_last_minute"], 1)
        self.assertEqual(usage["last_status"], 200)
        self.assertEqual(usage["last_latency_ms"], 12.35)
        self.assertEqual(usage["quota"]["limit"], 100)
        self.assertEqual(usage["quota"]["remaining"], 5)
        self.assertEqual(usage["quota"]["utilization_pct"], 95.0)

    def test_used_only_header_does_not_invent_a_quota_denominator(self):
        tracker = ExchangeRequestTracker()
        tracker.record_request()
        tracker.record_response(200, {"X-MBX-USED-WEIGHT-1M": "340"}, 3.0)

        quota = tracker.snapshot()["quota"]
        self.assertEqual(quota["used"], 340)
        self.assertIsNone(quota["limit"])
        self.assertIsNone(quota["utilization_pct"])

    def test_rate_limit_and_retry_counters_are_visible(self):
        tracker = ExchangeRequestTracker()
        tracker.record_request()
        tracker.record_response(429, {}, 4.0)
        tracker.record_retry()
        tracker.record_request()
        tracker.record_response(200, {}, 2.0)

        usage = tracker.snapshot()
        self.assertEqual(usage["rate_limit_hits_total"], 1)
        self.assertEqual(usage["rate_limit_hits_last_minute"], 1)
        self.assertEqual(usage["retries_total"], 1)
        self.assertEqual(usage["errors_total"], 1)
        self.assertEqual(usage["requests_last_minute"], 2)

    def test_venue_error_envelope_can_be_counted_as_throttling(self):
        for code in ("429", "429000", "-1003", "-1015"):
            with self.subTest(code=code):
                self.assertTrue(ExchangeRequestTracker.is_rate_limit_code(code))
        self.assertFalse(ExchangeRequestTracker.is_rate_limit_code("500000"))

        tracker = ExchangeRequestTracker()
        tracker.record_rate_limit()
        usage = tracker.snapshot()
        self.assertEqual(usage["rate_limit_hits_total"], 1)
        self.assertEqual(usage["rate_limit_hits_last_minute"], 1)
        self.assertEqual(usage["errors_total"], 1)


if __name__ == "__main__":
    unittest.main()
