"""Redis client helper."""
import json
from urllib.parse import urlparse, urlunparse

import redis
from core.config import looks_like_placeholder, settings


def _strip_placeholder_password(url: str) -> str:
    """Remove templated passwords from Redis URLs to avoid AUTH errors."""

    parsed = urlparse(url)
    if not parsed.password:
        return url

    if not looks_like_placeholder(parsed.password):
        return url

    netloc_host = parsed.hostname or ""
    netloc = netloc_host
    if parsed.username:
        netloc = f"{parsed.username}@{netloc_host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def get_redis_client() -> redis.Redis:
    sanitized_url = _strip_placeholder_password(settings.redis_url)
    return redis.from_url(sanitized_url, decode_responses=True)


def cache_json(key: str, payload: dict, ttl_seconds: int = 300) -> None:
    client = get_redis_client()
    client.set(key, json.dumps(payload), ex=ttl_seconds)


def get_cached_json(key: str) -> dict | None:
    client = get_redis_client()
    raw = client.get(key)
    if not raw:
        return None
    return json.loads(raw)
