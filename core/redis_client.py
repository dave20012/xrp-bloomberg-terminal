"""Redis client helper."""
import json
import redis
from core.config import settings


def get_redis_client() -> redis.Redis:
    return redis.from_url(settings.redis_url, decode_responses=True)


def cache_json(key: str, payload: dict, ttl_seconds: int = 300) -> None:
    client = get_redis_client()
    client.set(key, json.dumps(payload), ex=ttl_seconds)


def get_cached_json(key: str) -> dict | None:
    client = get_redis_client()
    raw = client.get(key)
    if not raw:
        return None
    return json.loads(raw)
