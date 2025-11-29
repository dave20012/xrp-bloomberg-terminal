import importlib
import os
import unittest

from redis_client import rdb


def clear_cache():
    if hasattr(rdb, "_store"):
        rdb._store.clear()
    else:
        try:
            rdb.flushdb()
        except Exception:
            pass


class DivergenceStateTests(unittest.TestCase):
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

    def _reset_module(self):
        import main

        module = importlib.reload(main)
        try:
            for key in ("last_agg_oi", "last_price"):
                if key in module.st.session_state:
                    del module.st.session_state[key]
        except Exception:
            pass
        module.SESSION_FALLBACK.clear()
        return module

    def test_divergence_updates_open_interest_state(self):
        module = self._reset_module()

        base_price = {"price": 0.5}
        base_futures = {
            "funding": 0.01,
            "open_interest": None,
            "aggregated_open_interest": 100.0,
            "long_short_ratio": 1.0,
            "relative_volume": 1.0,
        }
        flows = {"latest_inflow": 0.0, "latest_outflow": 0.0}
        sentiment = {"ema": 0.1}

        # Prime state with an initial snapshot.
        module.compute_signal_stack(base_price, base_futures, flows, sentiment)

        # Second snapshot flips price direction relative to OI, which should trigger divergence.
        second_price = {"price": 0.4}
        second_futures = {**base_futures, "aggregated_open_interest": 150.0}
        result = module.compute_signal_stack(second_price, second_futures, flows, sentiment)

        divergence = next(d for d in result["details"] if d["key"] == "divergence")

        self.assertTrue(divergence["available"], "divergence check should have prior OI state")
        self.assertEqual(
            divergence["points"],
            module.SIGNAL_COMPONENTS["divergence"].max_points,
            "divergence points should max out when price and OI move oppositely",
        )


if __name__ == "__main__":
    unittest.main()
