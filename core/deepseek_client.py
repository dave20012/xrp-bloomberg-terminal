"""DeepSeek client for enriched XRP metrics."""
from __future__ import annotations

from typing import Any, Dict

import requests

from core.config import settings


def fetch_market_intel(symbol: str = "XRP") -> Dict[str, Any]:
    url = f"{settings.deepseek_base}/v1/crypto/intel"
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}
    params = {"symbol": symbol}
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()
