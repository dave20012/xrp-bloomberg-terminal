# ================= SENTIMENT WORKER v10.4 =======================
# Weighted institutional sentiment for XRP via FinBERT on HF router.
# Pushes raw per-article scores + aggregate scalar to Redis "news:sentiment".

import os
import time
import json
import random
import re
import logging

import numpy as np
import requests
from datetime import datetime, timezone

from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

NEWS_KEY     = os.getenv("NEWS_API_KEY")
HF_TOKEN     = os.getenv("HF_TOKEN")
RUN_INTERVAL = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # 30m default

# ===================== Source Weight Map =========================

TIER_1 = [
    "reuters", "bloomberg", "financial times", "ft",
    "wsj", "wall street journal", "forbes", "cnbc"
]
TIER_2 = [
    "fortune", "business insider", "marketwatch", "yahoo finance",
    "tradingview", "markets insider", "barron"
]
TIER_3 = [
    "coindesk", "cointelegraph", "cryptoslate", "bitcoinist",
    "cryptobriefing", "decrypt", "newsbtc"
]
TABLOID = ["biztoc", "zycrypto", "u.today", "dailyhodl", "ambcrypto"]


def source_weight(src: str) -> float:
    if not src:
        return 0.25
    s = src.lower()
    if any(x in s for x in TIER_1):
        return 1.00
    if any(x in s for x in TIER_2):
        return 0.65
    if any(x in s for x in TIER_3):
        return 0.35
    if any(x in s for x in TABLOID):
        return 0.05
    return 0.15


# ====================== Headline Filter ==========================

HYPE = re.compile(
    r"(price|surge|massive|soars|plunge|crash|dip|moon|target|prediction|forecast|"
    r"to \$\d+|100x|1000x|explode)",
    re.IGNORECASE,
)


def clean_headline(title: str, src: str) -> bool:
    if not title:
        return False
    t = title.strip()
    if len(t.split()) < 4:
        return False

    # Allow hype only from higher-quality sources
    if HYPE.search(t) and source_weight(src) < 0.6:
        return False

    return True


# ===================== Fetch Headlines ===========================

def fetch():
    if not NEWS_KEY:
        logging.warning("NEWS_API_KEY missing; sentiment worker idle.")
        return []

    try:
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "(XRP OR Ripple)",
                "searchIn": "title",
                "language": "en",
                "pageSize": 50,
                "sortBy": "publishedAt",
                "apiKey": NEWS_KEY,
            },
            timeout=15,
        )
        if not r.ok:
            logging.warning(f"News API error: {r.status_code} {r.text[:120]}")
            return []
        return r.json().get("articles", []) or []
    except Exception as e:
        logging.error(f"News fetch failed: {e}")
        return []


# ====================== FinBERT via HF Router ====================

FINBERT_URL = "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert"


def finbert(text: str):
    """
    Call FinBERT text-classification via HF router.
    Returns (pos, neg, neu) or None.
    """
    if not HF_TOKEN:
        return None

    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": text}

    for _ in range(2):  # simple retry
        try:
            r = requests.post(FINBERT_URL, headers=headers, json=payload, timeout=25)
            if not r.ok:
                logging.warning(f"FinBERT error {r.status_code}: {r.text[:100]}")
                time.sleep(0.4)
                continue

            resp = r.json()

            # handle both [ [ {label,score} ] ] and [ {label,score} ] formats
            if isinstance(resp, list) and resp:
                if isinstance(resp[0], list):
                    arr = resp[0]
                else:
                    arr = resp

                scores = {}
                for item in arr:
                    if isinstance(item, dict):
                        lab = str(item.get("label", "")).lower()
                        val = float(item.get("score", 0.0) or 0.0)
                        scores[lab] = val

                pos = float(scores.get("positive", 0.0))
                neg = float(scores.get("negative", 0.0))
                neu = float(scores.get("neutral", 0.0))
                return pos, neg, neu

        except Exception as e:
            logging.error(f"FinBERT inference failed: {e}")
            time.sleep(0.5)

    return None


# ===================== Score Aggregation ==========================

def weighted_trimmed_mean(scores, weights):
    if not scores:
        return 0.0
    arr = np.array(scores, dtype=float)
    w = np.array(weights, dtype=float)
    if w.sum() <= 0:
        return 0.0

    idx = arr.argsort()
    k = max(1, int(len(arr) * 0.2))
    if len(arr) > 2 * k:
        arr_trim = arr[idx][k:-k]
        w_trim = w[idx][k:-k]
    else:
        arr_trim = arr
        w_trim = w

    return float(np.average(arr_trim, weights=w_trim))


# ====================== Utility ==================================

def ts():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def push(payload):
    try:
        rdb.set("news:sentiment", json.dumps(payload))
        logging.info(
            f"Pushed sentiment: score={payload.get('score'):.4f}, "
            f"count={payload.get('count')}"
        )
    except Exception as e:
        logging.error(f"Redis push failed: {e}")


# ======================= Main Routine =============================

def run_once():
    articles = fetch()
    cleaned = []

    for a in articles:
        src = (a.get("source", {}) or {}).get("name", "") or ""
        title = (a.get("title") or "").strip()
        if clean_headline(title, src):
            cleaned.append({"source": src, "title": title})

    logging.info(f"Valid headlines: {len(cleaned)}")

    if not cleaned:
        push(
            {
                "timestamp": ts(),
                "score": 0.0,
                "count": 0,
                "mode": "weighted_all",
                "articles": [],
            }
        )
        return

    random.shuffle(cleaned)
    scored = []
    scalar_scores = []
    weights = []

    for art in cleaned[:24]:  # score up to 24 headlines per run
        res = finbert(art["title"])
        w = source_weight(art["source"])
        if res is not None:
            pos, neg, neu = res
            scalar = pos - neg
            scored.append(
                {
                    **art,
                    "pos": pos,
                    "neg": neg,
                    "neu": neu,
                    "scalar": scalar,
                    "weight": w,
                }
            )
            scalar_scores.append(scalar)
            weights.append(w)
        else:
            scored.append(
                {**art, "pos": None, "neg": None, "neu": None, "scalar": None, "weight": w}
            )
        time.sleep(0.22)

    agg = weighted_trimmed_mean(scalar_scores, weights)
    payload = {
        "timestamp": ts(),
        "score": float(agg),
        "count": int(len(scalar_scores)),
        "mode": "weighted_all",
        "articles": scored,
    }
    push(payload)


# ======================= Loop ====================================

def run_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            logging.error(f"Sentiment loop error: {e}")
        time.sleep(RUN_INTERVAL)


if __name__ == "__main__":
    run_loop()
