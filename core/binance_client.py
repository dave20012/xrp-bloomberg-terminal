from __future__ import annotations
"""Lightweight Binance API client for XRP pairs."""

from typing import Any, Dict, List

import requests

from core.config import settings
from core.utils import logger


HEADERS = {"X-MBX-APIKEY": settings.binance_api_key or ""}


def _handle_response(resp: requests.Response) -> Any:
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("Location", "<unknown>")
        preview = resp.text[:500]
        logger.error(
            "Binance request redirected (%s) to %s. Body preview: %s",
            resp.status_code,
            location,
            preview,
        )
        raise requests.HTTPError(
            f"Unexpected redirect {resp.status_code} -> {location}", response=resp
        )

    resp.raise_for_status()

    try:
        return resp.json()
    except ValueError:  # pragma: no cover - defensive logging
        preview = resp.text[:500]
        logger.error(
            "Binance response is not valid JSON (status %s). Body preview: %s",
            resp.status_code,
            preview,
        )
        raise


def fetch_recent_trades(symbol: str = "XRPUSDT", limit: int = 1000) -> List[Dict[str, Any]]:
    url = f"{settings.binance_base}/api/v3/trades"
    params = {"symbol": symbol, "limit": min(limit, 1000)}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10, allow_redirects=False)
    return _handle_response(resp)


def fetch_order_book(symbol: str = "XRPUSDT", limit: int = 50) -> Dict[str, Any]:
    url = f"{settings.binance_base}/api/v3/depth"
    params = {"symbol": symbol, "limit": limit}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10, allow_redirects=False)
    return _handle_response(resp)


def fetch_funding_rate(symbol: str = "XRPUSDT") -> Dict[str, Any]:
    url = f"{settings.binance_futures_base}/fapi/v1/premiumIndex"
    params = {"symbol": symbol}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10, allow_redirects=False)
    return _handle_response(resp)


def fetch_open_interest(symbol: str = "XRPUSDT") -> Dict[str, Any]:
    url = f"{settings.binance_futures_base}/futures/data/openInterestHist"
    params = {"symbol": symbol, "period": "5m", "limit": 1}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10, allow_redirects=False)
    data = _handle_response(resp)
    return data[0] if data else {}


def fetch_long_short_ratio(symbol: str = "XRPUSDT") -> Dict[str, Any]:
    url = f"{settings.binance_futures_base}/futures/data/topLongShortAccountRatio"
    params = {"symbol": symbol, "period": "5m", "limit": 1}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
    data = _handle_response(resp)
    return data[0] if data else {}


def summarize_order_book(book: Dict[str, Any]) -> Dict[str, float]:
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_volume = sum(float(b[1]) for b in bids)
    ask_volume = sum(float(a[1]) for a in asks)
    depth_imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume + 1e-9)
    return {
        "bid_volume": bid_volume,
        "ask_volume": ask_volume,
        "depth_imbalance": depth_imbalance,
    }
