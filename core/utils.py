"""General utilities for logging, retries, and math helpers."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Callable


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("xrp-intel")


def retry(action: Callable[[], Any], attempts: int = 3, delay: float = 2.0) -> Any:
    """Retry a function with linear backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Attempt %s failed: %s", attempt, exc)
            if attempt == attempts:
                raise
            time.sleep(delay * attempt)


def now_ts() -> datetime:
    return datetime.utcnow()


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100
