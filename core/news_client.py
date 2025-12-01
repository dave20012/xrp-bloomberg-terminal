"""News API client for XRP-relevant headlines."""
from __future__ import annotations

from typing import Any, Dict, List

import requests

from core.config import settings


CATEGORIES = ["ripple", "xrp", "crypto", "regulation"]


def fetch_latest_news(limit: int = 25) -> List[Dict[str, Any]]:
    url = f"{settings.news_base}/v2/everything"
    query = "XRP OR Ripple"
    params = {
        "q": query,
        "pageSize": limit,
        "sortBy": "publishedAt",
        "language": "en",
        "apiKey": settings.news_api_key,
    }
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("articles", [])
