import os
import unittest
from unittest import mock

# Avoid live XRPL fetches triggered by importing the Streamlit app.
os.environ.setdefault("SKIP_LIVE_FETCH", "1")

import main


class FundingAndOpenInterestFallbackTests(unittest.TestCase):
    @staticmethod
    def _resp(payload, ok=True):
        class DummyResponse:
            def __init__(self, data, ok):
                self._data = data
                self.ok = ok

            def json(self):
                return self._data

        return DummyResponse(payload, ok)

    @mock.patch("main.requests.get")
    def test_fallback_endpoints_used_when_primary_missing(self, mock_get):
        mock_get.side_effect = [
            self._resp([]),  # primary funding endpoint returns empty list
            self._resp({"lastFundingRate": "-0.0004"}),  # premiumIndex fallback
            self._resp([]),  # primary open interest endpoint returns empty list
            self._resp({"openInterest": "987654.321"}),  # openInterest fallback
        ]

        result = main.fetch_funding_and_oi()

        self.assertEqual(mock_get.call_count, 4)
        self.assertAlmostEqual(result["funding"], -0.0004)
        self.assertAlmostEqual(result["open_interest"], 987654.321)


if __name__ == "__main__":
    unittest.main()
