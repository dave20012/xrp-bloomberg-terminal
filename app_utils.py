"""Utility functions for the XRP dashboard.

This module centralises a few helper routines used throughout the
application.  It includes functions to normalise environment
variables, safely traverse nested data structures and fetch
JSON data with a simple in‑process cache.

The helpers defined here avoid external dependencies and provide
reasonable defaults so that the rest of the codebase can import
them without needing to wrap common logic repeatedly.  Should you
wish to swap in a more sophisticated caching or configuration
framework, these functions provide a single point of indirection.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any, Dict, Iterable, Optional

import requests


def normalize_env_value(name: str) -> str:
    """Return a trimmed environment variable.

    Many deployment platforms populate environment variables with
    extraneous quotes or whitespace.  This helper strips common
    leading/trailing characters so that downstream code does not
    need to perform this sanitisation repeatedly.

    Args:
        name: The environment variable name to look up.

    Returns:
        The normalised value or an empty string if unset.
    """

    raw = os.getenv(name)
    if raw is None:
        return ""
    return raw.strip().strip("\'\"")


@functools.lru_cache(maxsize=128)
def cache_get_json(url: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 10) -> Any:
    """Fetch a JSON document and cache the result in memory.

    A thin wrapper around ``requests.get`` that caches responses for
    repeated calls.  Since Streamlit reruns the script frequently,
    caching reduces the number of outbound requests and improves
    responsiveness.  The cache is process‑local and will not
    persist across interpreter restarts.

    Args:
        url: The absolute URL to fetch.
        params: Optional dictionary of query parameters.
        timeout: Timeout in seconds for the HTTP request.

    Returns:
        Parsed JSON content from the response.  On any exception the
        cache entry is invalidated and an empty dictionary is
        returned.
    """

    try:
        resp = requests.get(url, params=params or {}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        # Remove potentially poisoned cache entry.
        cache_get_json.cache_clear()
        return {}


def safe_get(data: Any, path: str, default: Any = None) -> Any:
    """Safely traverse nested objects by dotted path.

    When working with deeply nested dictionaries or objects it is
    common to guard each attribute access.  This helper takes a
    dotted path (e.g. ``"foo.bar.baz"``) and attempts to walk the
    structure, returning a default value if any attribute lookup
    fails.

    Args:
        data: The dictionary or object to traverse.
        path: Dotted attribute or key path.
        default: Fallback value if any lookup fails.

    Returns:
        The located value or the ``default`` if lookup fails.
    """

    try:
        current = data
        for part in path.split('.'):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part)
            if current is None:
                return default
        return current
    except Exception:
        return default