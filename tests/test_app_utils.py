"""Unit coverage for app_utils helper functions."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app_utils


class SafeGetTests(unittest.TestCase):
    @mock.patch("app_utils.requests.get")
    def test_safe_get_success(self, mock_get):
        resp = mock.Mock()
        resp.ok = True
        resp.json.return_value = {"ok": True}
        mock_get.return_value = resp

        data = app_utils.safe_get("https://example.com", {"q": "xrp"}, timeout=1)

        self.assertEqual(data, {"ok": True})
        mock_get.assert_called_once()

    @mock.patch("app_utils.requests.get")
    def test_safe_get_handles_status_error(self, mock_get):
        resp = mock.Mock()
        resp.ok = False
        resp.status_code = 500
        mock_get.return_value = resp

        data = app_utils.safe_get("https://example.com/bad")

        self.assertIsNone(data)
        mock_get.assert_called_once()

    @mock.patch("app_utils.requests.get", side_effect=Exception("boom"))
    def test_safe_get_handles_exception(self, mock_get):
        data = app_utils.safe_get("https://example.com/fail")
        self.assertIsNone(data)
        mock_get.assert_called_once()


class SentimentComponentTests(unittest.TestCase):
    def test_compute_sentiment_components_filters_weights(self):
        articles = [
            {"scalar": 0.6, "pos": 0.9, "neg": 0.1, "weight": 0.8},
            {"scalar": -0.2, "pos": 0.1, "neg": 0.9, "weight": 0.4},
        ]

        inst, bull, bear = app_utils.compute_sentiment_components(articles, "Institutional Only")

        self.assertAlmostEqual(inst, 0.6)
        self.assertAlmostEqual(bull, 0.9)
        self.assertAlmostEqual(bear, 0.1)

    def test_compute_sentiment_components_handles_empty(self):
        inst, bull, bear = app_utils.compute_sentiment_components([], "Weighted (All Sources)")
        self.assertEqual((inst, bull, bear), (0.0, 0.0, 0.0))


class DataHealthTests(unittest.TestCase):
    def test_describe_data_health_notes_redis(self):
        live = {"price": None, "oi_usd": None, "funding_hist_pct": [], "xrpl_weighted_inflow": 0}
        news_payload = {"count": 0}

        issues, redis_notes = app_utils.describe_data_health(live, news_payload)

        self.assertIn("XRP price feed unavailable (CoinGecko)", issues)
        self.assertTrue(any("news:sentiment" in note for note in redis_notes))
        self.assertTrue(any("xrpl:latest_inflows" in note for note in redis_notes))
        self.assertTrue(any("news:sentiment_ema" in note for note in redis_notes))
        self.assertTrue(any("xrpl:inflow_history" in note for note in redis_notes))
        self.assertTrue(any("ratio_ema:xrp_btc" in note for note in redis_notes))

    @mock.patch("app_utils.cache_get_json")
    def test_describe_data_health_skips_fresh_xrpl_note(self, mock_cache):
        def cache_side_effect(key):
            if key == "xrpl:latest_inflows_meta":
                return {
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "run_seconds": 600,
                }
            if key == "xrpl:latest_inflows":
                return []
            if key == "xrpl:inflow_history":
                return []
            return None

        mock_cache.side_effect = cache_side_effect

        live = {"price": 0.5, "oi_usd": 1.0, "funding_hist_pct": [0.01], "xrpl_weighted_inflow": 0}
        news_payload = {"count": 1}

        _, redis_notes = app_utils.describe_data_health(live, news_payload)

        self.assertFalse(any("xrpl:latest_inflows" in note for note in redis_notes))

    @mock.patch("app_utils.cache_get_json")
    def test_describe_data_health_uses_inflow_timestamp(self, mock_cache):
        recent_ts = datetime.now(timezone.utc) - timedelta(seconds=300)

        def cache_side_effect(key):
            if key == "xrpl:latest_inflows":
                return [{"timestamp": recent_ts.timestamp()}]
            return None

        mock_cache.side_effect = cache_side_effect

        live = {"price": 0.5, "oi_usd": 1.0, "funding_hist_pct": [0.01], "xrpl_weighted_inflow": 0}
        news_payload = {"count": 1}

        _, redis_notes = app_utils.describe_data_health(live, news_payload)

        self.assertFalse(any("xrpl:latest_inflows" in note for note in redis_notes))


if __name__ == "__main__":
    unittest.main()
