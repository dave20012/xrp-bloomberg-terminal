import logging
import os
from typing import Dict, Optional

import redis

logger = logging.getLogger(__name__)


class _InMemoryRedis:
    """Minimal Redis-like fallback used when no REDIS_URL is configured."""

    def __init__(self) -> None:
        self._store: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value


REDIS_URL = os.getenv("REDIS_URL")

if REDIS_URL and REDIS_URL.startswith("${"):
    logger.warning("REDIS_URL placeholder detected; using in-memory cache instead.")
    REDIS_URL = ""

if not REDIS_URL:
    logger.warning("REDIS_URL not set; using in-memory cache (data not persisted).")
    rdb = _InMemoryRedis()
else:
    rdb = redis.Redis.from_url(REDIS_URL, decode_responses=True)

__all__ = ["rdb"]
