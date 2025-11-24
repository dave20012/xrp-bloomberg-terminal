import importlib
import importlib
import json
import os
import unittest
from unittest import mock

from redis_client import rdb


def clear_cache():
    if hasattr(rdb, "_store"):
        rdb._store.clear()
    else:
        try:
            rdb.flushdb()
        except Exception:
            pass


class FetchLiveInflowsTests(unittest.TestCase):
    def setUp(self):
        self._orig_skip = os.environ.get("SKIP_LIVE_FETCH")
        os.environ["SKIP_LIVE_FETCH"] = "1"
        clear_cache()

    def tearDown(self):
        if self._orig_skip is None:
            os.environ.pop("SKIP_LIVE_FETCH", None)
        else:
            os.environ["SKIP_LIVE_FETCH"] = self._orig_skip
        clear_cache()
        import main

        importlib.reload(main)

    def _reload_main(self):
        import main

        return importlib.reload(main)

    def test_fetch_live_aggregates_latest_inflows(self):
        module = self._reload_main()

        flows = [
            {"xrp": 1_000_000, "weight": 1.0, "ripple_corp": False},
            {"xrp": 500_000, "weight": 0.5, "ripple_corp": True},
        ]
        rdb.set("xrpl:latest_inflows", json.dumps(flows))

        with mock.patch.object(
            module, "cached_coingecko_simple_price", return_value={"ripple": {"usd": 0.5}}
        ), mock.patch.object(
            module, "cached_crypto_compare_price", return_value=0.5
        ), mock.patch.object(
            module, "safe_get", return_value=None
        ):
            live = module.fetch_live()

        self.assertAlmostEqual(live["xrpl_raw_inflow"], 1_500_000)
        self.assertAlmostEqual(live["xrpl_weighted_inflow"], 1_250_000)
        self.assertAlmostEqual(live["xrpl_ripple_otc"], 500_000)

    def test_fetch_live_includes_outflows_and_net(self):
        module = self._reload_main()

        inflows = [{"xrp": 2_000_000, "weight": 1.0}]
        outflows = [{"xrp": 500_000, "weight": 0.5}]
        rdb.set("xrpl:latest_inflows", json.dumps(inflows))
        rdb.set("xrpl:latest_outflows", json.dumps(outflows))

        with mock.patch.object(
            module, "cached_coingecko_simple_price", return_value={"ripple": {"usd": 0.5}}
        ), mock.patch.object(
            module, "cached_crypto_compare_price", return_value=0.5
        ), mock.patch.object(
            module, "safe_get", return_value=None
        ):
            live = module.fetch_live()

        self.assertAlmostEqual(live["xrpl_raw_inflow"], 2_000_000)
        self.assertAlmostEqual(live["xrpl_raw_outflow"], 500_000)
        self.assertAlmostEqual(live["xrpl_netflow"], 1_500_000)
        self.assertAlmostEqual(live["xrpl_weighted_outflow"], 250_000)

    def test_fetch_live_falls_back_to_history_when_latest_missing(self):
        module = self._reload_main()

        history = [
            {"timestamp": "2025-01-01T00:00:00Z", "total_xrp": 3_000_000, "weighted_xrp": 1_500_000}
        ]
        rdb.set("xrpl:inflow_history", json.dumps(history))

        with mock.patch.object(
            module, "cached_coingecko_simple_price", return_value={"ripple": {"usd": 0.5}}
        ), mock.patch.object(
            module, "cached_crypto_compare_price", return_value=0.5
        ), mock.patch.object(
            module, "safe_get", return_value=None
        ):
            live = module.fetch_live()

        self.assertAlmostEqual(live["xrpl_raw_inflow"], 3_000_000)
        self.assertAlmostEqual(live["xrpl_weighted_inflow"], 1_500_000)
        self.assertEqual(live["xrpl_ripple_otc"], 0.0)


if __name__ == "__main__":
    unittest.main()
