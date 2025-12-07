from core import binance_client, cc_client


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_binance_order_book(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=10, **kwargs):
        return DummyResponse({"bids": [["0.5", "100"]], "asks": [["0.6", "50"]]})

    monkeypatch.setattr(binance_client.requests, "get", fake_get)
    book = binance_client.fetch_order_book()
    stats = binance_client.summarize_order_book(book)
    assert stats["bid_volume"] > 0
    assert "depth_imbalance" in stats


def test_cc_client(monkeypatch):
    def fake_get(url, params=None, timeout=10):
        return DummyResponse({"Data": {"Data": [{"close": 0.5, "volumefrom": 1000}]}})

    monkeypatch.setattr(cc_client.requests, "get", fake_get)
    data = cc_client.fetch_ohlcv(limit=1)
    assert isinstance(data, list)
    assert data[0]["close"] == 0.5
