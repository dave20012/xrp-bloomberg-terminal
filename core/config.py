"""Configuration utilities for environment variables and API endpoints."""
import os
from dataclasses import dataclass


def _coalesce_env(name: str, default: str) -> str:
    """Return a usable environment value, falling back when unset or templated.

    Some deployment environments populate variables with placeholders such as
    ``${DATABASE_URL}`` when an actual value is not provided. These strings are
    not valid connection URLs and cause SQLAlchemy to fail during import. This
    helper treats missing, empty, or placeholder values as absent and returns
    the provided default instead.
    """

    value = os.getenv(name)
    if value is None:
        return default

    stripped = value.strip()
    if not stripped or stripped == f"${{{name}}}":
        return default

    return stripped


@dataclass(slots=True)
class Settings:
    database_url: str = _coalesce_env(
        "DATABASE_URL", "sqlite:///./xrp_intel.db"
    )
    redis_url: str = _coalesce_env("REDIS_URL", "redis://localhost:6379/0")
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
