"""Configuration utilities for environment variables and API endpoints."""
import os
from dataclasses import dataclass


def get_env(name: str, default: str | None = None) -> str:
    """Fetch an environment variable with optional default."""
    value = os.getenv(name, default)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return value


@dataclass(slots=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/xrp_intel")
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    binance_api_key: str | None = os.getenv("BINANCE_API_KEY")
    binance_api_secret: str | None = os.getenv("BINANCE_API_SECRET")
    news_api_key: str | None = os.getenv("NEWS_API_KEY")
    hf_token: str | None = os.getenv("HF_TOKEN")
    cc_api_key: str | None = os.getenv("CRYPTOCOMPARE_API_KEY")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")

    # base URLs
    binance_base: str = "https://api.binance.com"
    binance_futures_base: str = "https://fapi.binance.com"
    cryptocompare_base: str = "https://min-api.cryptocompare.com"
    deepseek_base: str = "https://api.deepseek.com"
    news_base: str = "https://newsapi.org"
    hf_inference_base: str = "https://api-inference.huggingface.co/models"


settings = Settings()
PG_URL = settings.database_url
REDIS_URL = settings.redis_url
