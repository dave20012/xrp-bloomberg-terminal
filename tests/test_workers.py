import json
import unittest

from redis_client import rdb
from sentiment_worker import dedupe_headlines, read_sentiment_ema, write_sentiment_ema
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


if __name__ == "__main__":
    unittest.main()
