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
RUN_INTERVAL = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # default 30m

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
            return []
        return r.json().get("articles", [])
    except Exception:
        return []

def finbert_infer(text):
    if not HF_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    url = "https://api-inference.huggingface.co/models/ProsusAI/finbert"
    try:
        r = requests.post(url, headers=headers, json={"inputs": text}, timeout=15)
        if not r.ok:
            return None
        resp = r.json()
        if isinstance(resp, dict):
            return None
        if isinstance(resp, list) and resp:
            d = resp[0]
            # resp[0] expected list of dicts with label+score OR dict mapping
            if isinstance(d, dict) and "label" in d:
                # improbable shape; skip
                return None
            try:
                scores = {it["label"]: it["score"] for it in d}
                return float(scores.get("positive", 0.0) - scores.get("negative", 0.0))
            except Exception:
                return None
    except Exception:
        return None
    return None

def compute_trimmed_mean(scores):
    if not scores:
        return None
    arr = np.sort(np.array(scores))
    k = int(len(arr) * TRIM_FRACTION)
    if len(arr) <= 2 * k:
        trimmed = arr
    else:
        trimmed = arr[k: len(arr) - k]
    return float(np.mean(trimmed))

def run_once():
    # prefer authoritative domains
    domains = ",".join(
        [
            "wsj.com",
            "bloomberg.com",
            "reuters.com",
            "ft.com",
            "fortune.com",
            "businessinsider.com",
            "theverge.com",
            "techcrunch.com",
            "wired.com",
            "arstechnica.com",
        ]
    )
    arts = fetch_headlines(domains=domains)
    cleaned = []
    for a in arts:
        title = (a.get("title") or "").strip()
        src = a.get("source", {}).get("name", "unknown")
        if _reject_headline(title):
            continue
        cleaned.append({"source": src, "title": title, "url": a.get("url")})
    if not cleaned:
        payload = {"timestamp": datetime_str(), "score": 0.0, "count": 0, "articles": []}
        rdb.set("news:sentiment", json.dumps(payload))
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
    mean = compute_trimmed_mean(scores) if scores else None
    payload = {"timestamp": datetime_str(), "score": mean if mean is not None else 0.0, "count": len(scores), "articles": scored}
    rdb.set("news:sentiment", json.dumps(payload))
    return payload

def datetime_str():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_loop():
    while True:
        try:
            run_once()
        except Exception:
            pass
        time.sleep(RUN_INTERVAL)

if __name__ == "__main__":
    run_loop()


