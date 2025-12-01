"""Simple scheduler to run workers sequentially for lightweight deployments."""
from __future__ import annotations

import time

from workers.analytics_worker import run_once as run_analytics
from workers.inflow_worker import run_once as run_ingest
from workers.news_worker import run_once as run_news
from core.utils import logger


def main(interval: int = 900) -> None:
    while True:
        logger.info("Scheduler tick")
        try:
            run_ingest()
            run_news()
            run_analytics()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Scheduler error: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
