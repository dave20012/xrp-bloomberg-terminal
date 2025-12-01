"""CryptoCompare client for historical context."""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core.config import settings


def fetch_ohlcv(symbol: str = "XRP", currency: str = "USD", limit: int = 200) -> List[Dict[str, Any]]:
    url = f"{settings.cryptocompare_base}/data/v2/histohour"
    params = {"fsym": symbol, "tsym": currency, "limit": limit, "api_key": settings.cc_api_key}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data.get("Data", {}).get("Data", [])
