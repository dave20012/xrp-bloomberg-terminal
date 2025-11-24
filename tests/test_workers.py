import json
import unittest

from redis_client import rdb
from sentiment_worker import (
    dedupe_headlines,
    normalize_titles,
    read_cached_headlines,
    read_sentiment_ema,
    run_once,
    write_sentiment_ema,
)
from xrpl_inflow_monitor import append_history


def clear_cache():
    if hasattr(rdb, "_store"):
        rdb._store.clear()
    else:
        try:
            rdb.flushdb()
        except Exception:
            pass


class SentimentWorkerCacheTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def test_sentiment_ema_roundtrip(self):
        write_sentiment_ema(0.42)
        self.assertAlmostEqual(read_sentiment_ema(), 0.42)


class InflowHistoryTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def test_append_history_keeps_recent_entries(self):
        flows = [
            {"xrp": 1_000_000, "weight": 0.5},
            {"xrp": 500_000, "weight": 1.0},
        ]

        append_history(flows, max_len=2)
        append_history(flows, max_len=2)
        append_history(flows, max_len=2)

        raw = rdb.get("xrpl:inflow_history")
        self.assertIsNotNone(raw)
        history = json.loads(raw)
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(history[-1]["total_xrp"], 1_500_000)
        self.assertGreater(history[-1]["weighted_xrp"], 0)


class SentimentHeadlineTests(unittest.TestCase):
    def test_dedupe_headlines_normalizes_title(self):
        items = [
            {"source": "A", "title": "XRP falls below $1"},
            {"source": "B", "title": "xrp falls  below   $1 "},
            {"source": "C", "title": "XRP climbs"},
        ]

        deduped = dedupe_headlines(items)

        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0]["source"], "A")

    def test_cached_headlines_read_and_normalization(self):
        clear_cache()

        payload = {
            "timestamp": "2025-11-24T00:00:00Z",
            "score": 0.1,
            "count": 2,
            "mode": "weighted_all",
            "articles": [
                {"source": "A", "title": "XRP rallies"},
                {"source": "B", "title": "XRP    rallies "},
            ],
        }

        rdb.set("news:sentiment", json.dumps(payload))

        cached = read_cached_headlines()
        normalized_payload = normalize_titles(payload["articles"])

        self.assertEqual(cached, normalized_payload)
        self.assertEqual(len(cached), 1)

    def test_run_once_sample_path(self):
        clear_cache()

        run_once(use_sample=True)

        raw = rdb.get("news:sentiment")
        self.assertIsNotNone(raw)
        payload = json.loads(raw)

        self.assertEqual(payload["count"], 4)
        self.assertTrue(all(article.get("scalar") is not None for article in payload["articles"]))


if __name__ == "__main__":
    unittest.main()
