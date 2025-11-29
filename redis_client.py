"""Redis client wrapper for the XRP dashboard.

This module exposes a single ``rdb`` attribute representing a
Redis client.  The connection details are read from the
environment using helper functions from ``app_utils``.  A
graceful fallback is provided when the optional ``redis`` library
is not installed or when connection details are missing.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from app_utils import normalize_env_value

try:
    import redis  # type: ignore
except ImportError:
    redis = None


class DummyRedis(dict):
    """In‑memory fallback when Redis is unavailable.

    This minimal dict‑backed client implements ``get`` and ``set``
    methods to mimic the most common Redis operations used in the
    dashboard.  It is not a full replacement but allows code to run
    in environments where the redis package is not installed or
    a real server is not configured.
    """

    def get(self, key: str) -> Any:
        return super().get(key)

    def set(self, key: str, value: Any, ex: Optional[int] = None) -> bool:  # noqa: D401
        # ``ex`` (expire) parameter is ignored in the dummy implementation.
        super().__setitem__(key, value)
        return True


def _create_redis_connection() -> Any:
    """Create a Redis connection or fall back to DummyRedis.

    Reads connection parameters from the environment.  If the
    ``redis`` library is installed and at least a host is provided,
    attempts to establish a real connection.  Otherwise returns an
    in‑memory dictionary that implements the minimal API.
    """

    host = normalize_env_value('REDIS_HOST')
    if not host or redis is None:
        return DummyRedis()

    port = normalize_env_value('REDIS_PORT') or '6379'
    db = normalize_env_value('REDIS_DB') or '0'
    password = normalize_env_value('REDIS_PASSWORD') or None
    try:
        return redis.Redis(host=host, port=int(port), db=int(db), password=password, decode_responses=True)
    except Exception:
        # Fall back silently on any connection error
        return DummyRedis()


# Public client instance used across the codebase.
rdb = _create_redis_connection()