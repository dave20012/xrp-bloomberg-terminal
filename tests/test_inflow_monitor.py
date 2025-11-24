import unittest
from unittest import mock

import xrpl_inflow_monitor as monitor


class FetchTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.orig_provider = monitor.PROVIDER
        self.orig_key = monitor.WHALE_ALERT_KEY
        self.orig_missing_flag = monitor._missing_key_info_logged

    def tearDown(self):
        monitor.PROVIDER = self.orig_provider
        monitor.WHALE_ALERT_KEY = self.orig_key
        monitor._missing_key_info_logged = self.orig_missing_flag

    def test_whale_alert_falls_back_to_ripple_data_when_empty(self):
        monitor.PROVIDER = "whale_alert"
        monitor.WHALE_ALERT_KEY = "abc"
        monitor._missing_key_info_logged = False
        with mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_whale_alert", return_value=[]
        ) as mock_whale, mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_ripple_data",
            return_value=[{"xrp": 1}],
        ) as mock_ripple:
            txs = monitor.fetch_transactions()

        self.assertEqual(txs, [{"xrp": 1}])
        mock_whale.assert_called_once()
        mock_ripple.assert_called_once()

    def test_provider_ripple_data_short_circuits(self):
        monitor.PROVIDER = "ripple_data"
        with mock.patch("xrpl_inflow_monitor.fetch_transactions_whale_alert") as mock_whale, mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_ripple_data",
            return_value=[{"xrp": 2}],
        ) as mock_ripple:
            txs = monitor.fetch_transactions()

        self.assertEqual(txs, [{"xrp": 2}])
        mock_ripple.assert_called_once()
        mock_whale.assert_not_called()

    def test_missing_whale_alert_key_defaults_to_ripple_data(self):
        monitor.PROVIDER = "whale_alert"
        monitor.WHALE_ALERT_KEY = None
        monitor._missing_key_info_logged = False

        with mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_whale_alert",
        ) as mock_whale, mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_ripple_data", return_value=[{"xrp": 3}]
        ) as mock_ripple:
            txs = monitor.fetch_transactions()

        self.assertEqual(txs, [{"xrp": 3}])
        mock_ripple.assert_called_once()
        mock_whale.assert_not_called()

    def test_build_flows_prefers_cached_when_ripple_data_empty(self):
        monitor.PROVIDER = "ripple_data"

        with mock.patch(
            "xrpl_inflow_monitor.fetch_transactions_ripple_data", return_value=[]
        ) as mock_ripple, mock.patch(
            "xrpl_inflow_monitor.fetch_cached_flows", return_value=[{"xrp": 4}]
        ) as mock_cached:
            flows = monitor.build_flows()

        self.assertEqual(flows, [{"xrp": 4}])
        mock_ripple.assert_called_once()
        mock_cached.assert_called_once()

    def test_build_flows_prefers_cached_when_whale_alert_empty(self):
        monitor.PROVIDER = "whale_alert"
        monitor.WHALE_ALERT_KEY = "key"

        with mock.patch("xrpl_inflow_monitor.fetch_transactions", return_value=[]) as mock_fetch, mock.patch(
            "xrpl_inflow_monitor.fetch_cached_flows", return_value=[{"xrp": 5}]
        ) as mock_cached:
            flows = monitor.build_flows()

        self.assertEqual(flows, [{"xrp": 5}])
        mock_fetch.assert_called_once()
        mock_cached.assert_called_once()

    def test_sample_flows_provide_structured_payload(self):
        flows = monitor.sample_flows()

        self.assertGreaterEqual(len(flows), 2)
        for flow in flows:
            self.assertIn("xrp", flow)
            self.assertIn("exchange", flow)


if __name__ == "__main__":
    unittest.main()
