# sentiment_worker.py
import time
import os
import json
import random
import re
import requests
import numpy as np
from redis_client import rdb
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

# Example after fetching data
logging.info(f"Fetched {len(fetched_articles)} articles from news API")

# Example after processing data
logging.info(f"Processed sentiment for {len(processed_articles)} articles")

# Example before sending to main service
logging.info(f"Pushing payload: {processed_payload}")

NEWS_KEY = os.getenv("NEWS_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
PAGE_SIZE = int(os.getenv("SENTIMENT_PAGE_SIZE", "20"))
TRIM_FRACTION = float(os.getenv("SENTIMENT_TRIM", "0.2"))
RUN_INTERVAL = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # 30m default

_HYPE = re.compile(
    r"(price|surge|explode|massive|soars|crashes|moon|bullish|bearish|target|prediction|forecast|huge|urgent|alert|will|may|could|projected)",
    re.IGNORECASE,
)

def _reject_headline(t: str) -> bool:
    if not t:
        return True
    t = t.strip()
    if _HYPE.search(t):
        return True
    if len(t.split()) < 5:
        return True
    uc_ratio = sum(1 for c in t if c.isupper()) / max(1, len(t))
    if uc_ratio > 0.35:
        return True
    if "XRP" in t.upper() and len(t) < 30:
        return True
    return False

def fetch_headlines(domains=None):
    if not NEWS_KEY:
        return []
    params = {
        "q": "(XRP OR Ripple) NOT (BTC OR bitcoin)",
        "searchIn": "title",
        "pageSize": PAGE_SIZE,
        "sortBy": "publishedAt",
        "language": "en",
        "apiKey": NEWS_KEY,
    }
    if domains:
        params["domains"] = domains
    try:
        r = requests.get("https://newsapi.org/v2/everything", params=params, timeout=15)
        if not r.ok:
            logging.warning(f"News API returned bad status: {r.status_code}")
            return []
        articles = r.json().get("articles", [])
        logging.info(f"Fetched {len(articles)} articles from News API")
        return articles
    except Exception as e:
        logging.error(f"Failed fetching headlines: {e}")
        return []

def finbert_infer(text):
    if not HF_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    url = "https://api-inference.huggingface.co/models/ProsusAI/finbert"
    try:
        r = requests.post(url, headers=headers, json={"inputs": text}, timeout=15)
        if not r.ok:
            logging.warning(f"FinBERT API returned bad status for text: {text[:30]}...")
            return None
        resp = r.json()
        if isinstance(resp, dict) or not resp:
            return None
        d = resp[0]
        if isinstance(d, dict) and "label" in d:
            return None
        try:
            scores = {it["label"]: it["score"] for it in d}
            return float(scores.get("positive", 0.0) - scores.get("negative", 0.0))
        except Exception as e:
            logging.error(f"Failed to parse FinBERT response: {e}")
            return None
    except Exception as e:
        logging.error(f"FinBERT inference failed: {e}")
        return None

def compute_trimmed_mean(scores):
    if not scores:
        return 0.0
    arr = np.sort(np.array(scores))
    k = int(len(arr) * TRIM_FRACTION)
    trimmed = arr[k: len(arr) - k] if len(arr) > 2 * k else arr
    return float(np.mean(trimmed))

def datetime_str():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def push_payload(payload):
    try:
        rdb.set("news:sentiment", json.dumps(payload))
        logging.info(f"Pushed payload: {payload}")
    except Exception as e:
        logging.error(f"Failed to push payload: {e}")

def run_once():
    domains = ",".join([
        "wsj.com", "bloomberg.com", "reuters.com", "ft.com", "fortune.com",
        "businessinsider.com", "theverge.com", "techcrunch.com", "wired.com", "arstechnica.com"
    ])
    articles = fetch_headlines(domains)
    cleaned = []
    for a in articles:
        title = (a.get("title") or "").strip()
        src = a.get("source", {}).get("name", "unknown")
        if _reject_headline(title):
            continue
        cleaned.append({"source": src, "title": title, "url": a.get("url")})
    logging.info(f"Processed {len(cleaned)} valid articles")

    if not cleaned:
        payload = {"timestamp": datetime_str(), "score": 0.0, "count": 0, "articles": []}
        push_payload(payload)
        return payload

    random.shuffle(cleaned)
    to_score = cleaned[:12]
    scored = []
    scores = []
    for a in to_score:
        s = finbert_infer(a["title"])
        scored.append({"source": a["source"], "title": a["title"], "score": s})
        if s is not None:
            scores.append(s)
        time.sleep(0.35)
    mean_score = compute_trimmed_mean(scores)
    payload = {"timestamp": datetime_str(), "score": mean_score, "count": len(scores), "articles": scored}
    push_payload(payload)
    return payload

def run_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            logging.error(f"Run loop error: {e}")
        time.sleep(RUN_INTERVAL)

if __name__ == "__main__":
    run_loop()
