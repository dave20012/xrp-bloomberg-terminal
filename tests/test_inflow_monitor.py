import os
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


class PollIntervalEnvTests(unittest.TestCase):
    def setUp(self):
        self._orig_poll = os.environ.get("XRPL_POLL_SECONDS")
        self._orig_legacy = os.environ.get("XRPL_INFLOWS_INTERVAL")

    def tearDown(self):
        if self._orig_poll is None:
            os.environ.pop("XRPL_POLL_SECONDS", None)
        else:
            os.environ["XRPL_POLL_SECONDS"] = self._orig_poll

        if self._orig_legacy is None:
            os.environ.pop("XRPL_INFLOWS_INTERVAL", None)
        else:
            os.environ["XRPL_INFLOWS_INTERVAL"] = self._orig_legacy

        import importlib

        import xrpl_inflow_monitor as monitor

        importlib.reload(monitor)

    def test_prefers_documented_poll_env(self):
        import importlib

        with mock.patch.dict(
            os.environ,
            {"XRPL_POLL_SECONDS": "45", "XRPL_INFLOWS_INTERVAL": "600"},
        ):
            import xrpl_inflow_monitor as monitor

            reloaded = importlib.reload(monitor)

        self.assertEqual(reloaded.RUN, 45)


class RippleData403HandlingTests(unittest.TestCase):
    def setUp(self):
        monitor.ripple_data_blocked_addresses.clear()
        monitor.ripple_data_cooldown_until = 0

    def test_monitored_addresses_strip_whitespace(self):
        with mock.patch.object(
            monitor,
            "EXCHANGE_ADDRESSES",
            {"Sample": [" r123 ", "", "r456"]},
        ):
            self.assertEqual(monitor.monitored_addresses(), {"r123", "r456"})

    def test_403_blocks_address_without_global_cooldown(self):
        monitor.ripple_data_blocked_addresses.clear()
        monitor.ripple_data_cooldown_until = 0

        good_response = mock.Mock(
            status_code=200,
            ok=True,
            json=lambda: {
                "transactions": [
                    {
                        "tx": {
                            "Destination": "good",
                            "Amount": str(int((monitor.MIN_XRP + 1) * 1_000_000)),
                            "Account": "src",
                        },
                        "date": "2025-01-01T00:00:00Z",
                    }
                ]
            },
        )

        with mock.patch(
            "xrpl_inflow_monitor.monitored_addresses", return_value=["bad", "good"]
        ), mock.patch("xrpl_inflow_monitor.requests.get") as mock_get, mock.patch(
            "xrpl_inflow_monitor.time.sleep"
        ) as mock_sleep:
            mock_get.side_effect = [mock.Mock(status_code=403, ok=False), good_response]
            flows = monitor.fetch_transactions_ripple_data()

        self.assertIn("bad", monitor.ripple_data_blocked_addresses)
        self.assertEqual(monitor.ripple_data_cooldown_until, 0)
        self.assertEqual(len(flows), 1)
        self.assertEqual(flows[0].get("to_address"), "good")
        mock_sleep.assert_called()


if __name__ == "__main__":
    unittest.main()
