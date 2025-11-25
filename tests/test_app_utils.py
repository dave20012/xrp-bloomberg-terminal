"""Unit coverage for app_utils helper functions."""

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import app_utils


class NormalizeEnvValueTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"BINANCE_API_KEY": "  abc123  "})
    def test_trims_whitespace(self):
        self.assertEqual(app_utils.normalize_env_value("BINANCE_API_KEY"), "abc123")

    @mock.patch.dict(os.environ, {"BINANCE_API_SECRET": "'pasted-secret'"})
    def test_strips_surrounding_quotes(self):
        self.assertEqual(app_utils.normalize_env_value("BINANCE_API_SECRET"), "pasted-secret")

    def test_handles_missing_env(self):
        self.assertEqual(app_utils.normalize_env_value("DOES_NOT_EXIST"), "")


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

    @mock.patch("app_utils.requests.get")
    def test_safe_get_handles_rate_limit(self, mock_get):
        resp = mock.Mock()
        resp.ok = False
        resp.status_code = 429
        mock_get.return_value = resp

        data = app_utils.safe_get("https://example.com/throttle")

        self.assertIsNone(data)
        mock_get.assert_called_once()

    @mock.patch("app_utils.requests.get")
    def test_safe_get_handles_bad_json(self, mock_get):
        resp = mock.Mock()
        resp.ok = True
        resp.status_code = 200
        resp.json.side_effect = ValueError("no json")
        mock_get.return_value = resp

        data = app_utils.safe_get("https://example.com/not-json")

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


class SentimentConflictResolutionTests(unittest.TestCase):
    def test_positive_wins_against_lower_weight_negative(self):
        pos = [
            {"title": "Ripple scores ETF approval", "weight": 0.65, "scalar": 0.7},
        ]
        neg = [
            {"title": "Ripple scores ETF approval", "weight": 0.35, "scalar": -0.2},
            {"title": "Other bearish headline", "weight": 0.35, "scalar": -0.5},
        ]

        kept_pos, kept_neg = app_utils.resolve_sentiment_conflicts(pos, neg)

        self.assertEqual(len(kept_pos), 1)
        self.assertEqual(kept_pos[0]["title"], "Ripple scores ETF approval")
        self.assertEqual(len(kept_neg), 1)
        self.assertEqual(kept_neg[0]["title"], "Other bearish headline")

    def test_negative_wins_when_higher_weight(self):
        pos = [
            {"title": "Market jitters ahead", "weight": 0.35, "scalar": 0.2},
        ]
        neg = [
            {"title": "Market jitters ahead", "weight": 0.65, "scalar": -0.6},
        ]

        kept_pos, kept_neg = app_utils.resolve_sentiment_conflicts(pos, neg)

        self.assertEqual(len(kept_pos), 0)
        self.assertEqual(len(kept_neg), 1)
        self.assertEqual(kept_neg[0]["title"], "Market jitters ahead")


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
