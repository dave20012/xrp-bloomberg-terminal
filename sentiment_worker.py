# ================= SENTIMENT WORKER v9.3 ======================= #
# Weighted institutional sentiment scoring via HF Inference Providers
# (HTTP router + hf-inference endpoint, no huggingface_hub dependency).
#
# - Fetch headlines for XRP/Ripple
# - Filter out hype / junk
# - Run FinBERT classification via:
#     POST https://router.huggingface.co/hf-inference/models/ProsusAI/finbert
# - Store:
#     {
#       "timestamp": ...,
#       "score": scalar_weighted_trimmed_mean,
#       "count": N_valid,
#       "mode": "weighted_all",
#       "articles": [
#           {
#               "source": ...,
#               "title": ...,
#               "pos": float | None,
#               "neg": float | None,
#               "neu": float | None,
#               "scalar": float | None,
#               "weight": float
#           }, ...
#       ]
#     }
# in Redis under key "news:sentiment"

import argparse
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone

import numpy as np
import requests

from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

NEWS_KEY = os.getenv("NEWS_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
RUN_INTERVAL = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # 30m default
SENTIMENT_EMA_ALPHA = float(os.getenv("SENTIMENT_EMA_ALPHA", "0.3"))
HF_TIMEOUT = int(os.getenv("HF_TIMEOUT", "18"))
HF_FAIL_FAST = int(os.getenv("HF_FAIL_FAST", "4"))  # max consecutive FinBERT misses before skipping remaining titles

# ===================== Source Weight Map =========================

TIER_1 = [
    "reuters",
    "bloomberg",
    "financial times",
    "ft",
    "wsj",
    "wall street journal",
    "forbes",
    "cnbc",
]
TIER_2 = [
    "fortune",
    "business insider",
    "marketwatch",
    "yahoo finance",
    "tradingview",
    "markets insider",
]
TIER_3 = ["coindesk", "cointelegraph", "cryptoslate", "bitcoinist", "cryptobriefing"]
TABLOID = ["biztoc", "zycrypto", "u.today", "dailyhodl", "ambcrypto"]

# Headlines that often carry bullish implications despite neutral/negative language
POSITIVE_KEYWORD_BIASES = {
    "supply shock": 0.35,
    "supply squeeze": 0.25,
    "supply crunch": 0.25,
}


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


def apply_keyword_bias(title: str, scalar: float) -> float:
    """Adjust the FinBERT scalar using simple keyword heuristics.

    Some market narratives (e.g., an upcoming supply shock) are bullish even if
    FinBERT scores them slightly negative due to words like "shock". This bias
    nudges such headlines in the appropriate direction while keeping the output
    bounded to [-1, 1].
    """

    if not title:
        return float(scalar)

    bias = 0.0
    lower = title.lower()
    for kw, boost in POSITIVE_KEYWORD_BIASES.items():
        if kw in lower:
            bias += boost

    adjusted = scalar + bias
    return float(max(-1.0, min(1.0, adjusted)))


# ====================== Headline Filter ==========================

HYPE = re.compile(
    r"(price|surge|massive|soars|dip|moon|target|prediction|forecast)",
    re.IGNORECASE,
)


def clean_headline(title: str, src: str) -> bool:
    if not title:
        return False
    t = title.strip()
    if len(t.split()) < 4:
        return False

    # allow hype ONLY from Tier-1 & Tier-2 sources
    if HYPE.search(t) and source_weight(src) < 0.6:
        return False

    return True


def dedupe_headlines(items):
    """Return headlines with duplicate titles removed (case/whitespace-insensitive)."""

    seen = set()
    unique = []

    for a in items:
        title = (a.get("title") or "").strip().lower()
        if not title:
            continue

        normalized = re.sub(r"\s+", " ", title)
        if normalized in seen:
            continue

        seen.add(normalized)
        unique.append(a)

    return unique


def normalize_titles(items):
    """Return a set of normalized titles from an iterable of article dicts."""

    titles = set()

    for a in items:
        title = (a.get("title") or "").strip()
        if not title:
            continue

        normalized = re.sub(r"\s+", " ", title).lower()
        titles.add(normalized)

    return titles


# ===================== Fetch Headlines ===========================

def fetch_headlines():
    if not NEWS_KEY:
        logging.warning("NEWS_API_KEY missing.")
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
        return r.json().get("articles", [])
    except Exception as e:
        logging.error(f"News fetch failed: {e}")
        return []


# ====================== FinBERT via HF Router ====================

def finbert(text: str):
    """
    Call HF Inference Providers for FinBERT sentiment.

    Endpoint: https://router.huggingface.co/hf-inference/models/ProsusAI/finbert
    Payload:  {"inputs": "..."}
    Expected output: list of { "label": "...", "score": float } or [[...]].
    """
    if not HF_TOKEN:
        return None

    endpoints = [
        "https://router.huggingface.co/hf-inference/models/ProsusAI/finbert",
        "https://api-inference.huggingface.co/models/ProsusAI/finbert",  # fallback to main HF inference API
    ]

    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": text}

    for attempt in range(2):  # one retry per endpoint rotation
        for url in endpoints:
            try:
                r = requests.post(url, headers=headers, json=payload, timeout=HF_TIMEOUT)
                if not r.ok:
                    logging.warning(f"FinBERT error {r.status_code} ({url}): {r.text[:120]}")
                    time.sleep(0.5)
                    continue

                resp = r.json()

                # Possible shapes:
                # 1) [ {label, score}, ... ]
                # 2) [ [ {label, score}, ... ] ]
                if isinstance(resp, list) and resp:
                    if isinstance(resp[0], dict):
                        preds = resp
                    elif isinstance(resp[0], list) and resp[0]:
                        preds = resp[0]
                    else:
                        logging.warning(f"Unexpected FinBERT shape: {type(resp[0])}")
                        return None
                else:
                    logging.warning("Empty or non-list FinBERT response")
                    return None

                scores = {x["label"].lower(): float(x["score"]) for x in preds if "label" in x}
                pos = scores.get("positive", 0.0)
                neg = scores.get("negative", 0.0)
                neu = scores.get("neutral", 0.0)
                return pos, neg, neu

            except requests.Timeout:
                logging.error(f"FinBERT inference timed out ({url}, {HF_TIMEOUT}s)")
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"FinBERT inference failed ({url}): {e}")
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
    k = max(1, int(len(arr) * 0.2))  # 20% trim
    arr_trim = arr[idx][k:-k] if len(arr) > 2 * k else arr
    w_trim = w[idx][k:-k] if len(w) > 2 * k else w
    return float(np.average(arr_trim, weights=w_trim))


# ====================== Utility ==================================

def ts():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def push(payload):
    try:
        rdb.set("news:sentiment", json.dumps(payload))
        logging.info(f"Pushed → {payload}")
    except Exception as e:
        logging.error(f"Redis push failed: {e}")


def read_sentiment_ema():
    try:
        raw = rdb.get("news:sentiment_ema")
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            obj = json.loads(raw)
            return float(obj.get("ema", 0.0))
    except Exception as e:
        logging.warning(f"Sentiment EMA read failed: {e}")
    return None


def write_sentiment_ema(value: float):
    try:
        rdb.set(
            "news:sentiment_ema",
            json.dumps({"ema": float(value), "timestamp": ts()}),
        )
    except Exception as e:
        logging.error(f"Sentiment EMA write failed: {e}")


def read_cached_headlines():
    """Return normalized titles from the last sentiment payload, if available."""

    try:
        raw = rdb.get("news:sentiment")
        if not raw:
            return None

        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")

        obj = json.loads(raw)
        arts = obj.get("articles", [])
        return normalize_titles(arts)
    except Exception as e:
        logging.warning(f"Cached headline read failed: {e}")
    return None


# ======================= Main Routine =============================

def run_once(use_sample: bool = False):
    if use_sample:
        filtered = [
            {"source": "Reuters", "title": "XRP adoption grows as enterprise pilots expand"},
            {"source": "CoinDesk", "title": "XRP price dips on market volatility"},
            {"source": "Bloomberg", "title": "Ripple secures new payment corridor"},
            {"source": "Yahoo Finance", "title": "Analysts debate XRP long-term outlook"},
        ]
    else:
        arts_raw = fetch_headlines()
        filtered = []

        for a in arts_raw:
            src = a.get("source", {}).get("name", "") or ""
            title = (a.get("title") or "").strip()
            if clean_headline(title, src):
                filtered.append({"source": src, "title": title})

        filtered = dedupe_headlines(filtered)

    logging.info(f"Valid headlines: {len(filtered)}")

    if not filtered:
        push({"timestamp": ts(), "score": 0.0, "count": 0, "mode": "weighted_all", "articles": []})
        return

    cached_titles = read_cached_headlines()
    current_titles = normalize_titles(filtered)

    if cached_titles is not None and current_titles and cached_titles == current_titles:
        logging.info("Headlines unchanged from last run; skipping duplicate scoring.")
        return

    random.shuffle(filtered)
    scored = []
    scalar_scores = []
    weights = []

    consecutive_failures = 0

    for idx, a in enumerate(filtered[:24]):  # cap per run
        w = source_weight(a["source"])

        if use_sample:
            pos, neg, neu = (0.55, 0.20, 0.25) if idx % 2 == 0 else (0.25, 0.55, 0.20)
            res = (pos, neg, neu)
        else:
            res = finbert(a["title"])

        if res is not None:
            pos, neg, neu = res
            scalar = apply_keyword_bias(a["title"], pos - neg)
            scored.append(
                {
                    **a,
                    "pos": pos,
                    "neg": neg,
                    "neu": neu,
                    "scalar": scalar,
                    "weight": w,
                }
            )
            scalar_scores.append(scalar)
            weights.append(w)
            consecutive_failures = 0
        else:
            scored.append(
                {
                    **a,
                    "pos": None,
                    "neg": None,
                    "neu": None,
                    "scalar": None,
                    "weight": w,
                }
            )
            consecutive_failures += 1

        if not use_sample and consecutive_failures >= HF_FAIL_FAST:
            logging.error(
                f"Skipping remaining headlines after {consecutive_failures} consecutive FinBERT failures"
            )
            break

        if not use_sample:
            time.sleep(0.22)

    final = weighted_trimmed_mean(scalar_scores, weights)

    prev_ema = read_sentiment_ema()
    ema_sent = final if prev_ema is None else SENTIMENT_EMA_ALPHA * final + (1.0 - SENTIMENT_EMA_ALPHA) * prev_ema

    write_sentiment_ema(ema_sent)

    push(
        {
            "timestamp": ts(),
            "score": final,
            "count": len(scalar_scores),
            "mode": "weighted_all",
            "articles": scored,
        }
    )


def run_loop(use_sample: bool = False):
    while True:
        try:
            run_once(use_sample=use_sample)
        except Exception as e:
            logging.error(f"Sentiment loop error: {e}")
        time.sleep(RUN_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Sentiment worker")
    parser.add_argument("--once", action="store_true", help="Run a single iteration")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use built-in sample headlines and scores (no network calls)",
    )
    args = parser.parse_args()

    if args.once:
        run_once(use_sample=args.sample)
    else:
        run_loop(use_sample=args.sample)


if __name__ == "__main__":
    main()
