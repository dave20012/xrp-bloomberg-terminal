"""Fetches news and tags regulatory impact."""
from __future__ import annotations

import argparse
import time
from datetime import datetime

from core.db import Event, SessionLocal, create_tables
from core.hf_client import classify_headline
from core.news_client import fetch_latest_news
from core.utils import logger

create_tables()


def run_once(limit: int = 20) -> None:
    articles = fetch_latest_news(limit=limit)
    session = SessionLocal()
    with session.begin():
        for article in articles:
            headline = article.get("title") or ""
            source = article.get("source", {}).get("name", "news")
            scores = classify_headline(headline)
            subtype = "regulatory" if scores.get("regulatory_threat", 0) > 0.3 else "macro"
            evt = Event(
                timestamp=datetime.fromisoformat(article.get("publishedAt", datetime.utcnow().isoformat().replace("Z", ""))),
                type="regulatory" if "regulat" in headline.lower() else "news",
                subtype=subtype,
                tags=scores,
                source=source,
                severity=max(scores.values()) if scores else 0.0,
            )
            session.add(evt)
    logger.info("Stored %s news events", len(articles))


def main(loop: bool = False, interval: int = 1800) -> None:
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("News worker error: %s", exc)
        if not loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    main(loop=args.loop, interval=args.interval)
