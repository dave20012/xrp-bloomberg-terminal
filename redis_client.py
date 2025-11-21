import os
import redis

REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("REDIS_URL environment variable not set")

rdb = redis.Redis.from_url(REDIS_URL, decode_responses=True)