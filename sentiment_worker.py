# ================= SENTIMENT WORKER v10.4 ======================= #
# Institutional-weighted FinBERT sentiment via official HF SDK
# Outputs positive/negative/neutral + weighted scalar to Redis
# Compatible with XRP Engine v9.1+

import os, time, json, random, re, numpy as np, logging
from datetime import datetime, timezone
from redis_client import rdb

# ---- HuggingFace Official SDK (stable) ---- #
from huggingface_hub import InferenceClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# ===========================================================
# CONFIG
# ===========================================================
NEWS_KEY     = os.getenv("NEWS_API_KEY")
HF_TOKEN     = os.getenv("HF_TOKEN")
RUN_INTERVAL = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # default 30m

# Safe model allocation
HF_CLIENT = InferenceClient(api_key=HF_TOKEN) if HF_TOKEN else None


# ===================== Source Weight Map =========================

TIER_1 = ["reuters", "bloomberg", "financial times", "ft", "wsj", "wall street journal", "forbes", "cnbc"]
TIER_2 = ["fortune", "business insider", "marketwatch", "yahoo finance", "tradingview", "markets insider"]
TIER_3 = ["coindesk", "cointelegraph", "cryptoslate", "bitcoinist", "cryptobriefing"]
TABLOID = ["biztoc", "zycrypto", "u.today", "dailyhodl", "ambcrypto"]

def source_weight(src: str) -> float:
    if not src: return 0.25
    s = src.lower()
    if any(x in s for x in TIER_1): return 1.00
    if any(x in s for x in TIER_2): return 0.65
    if any(x in s for x in TIER_3): return 0.35
    if any(x in s for x in TABLOID): return 0.05
    return 0.15


# ====================== Headline Filter ==========================

HYPE = re.compile(r"(price|surge|massive|soars|dip|moon|target|prediction|forecast)", re.IGNORECASE)

def clean_headline(title: str, src: str) -> bool:
    if not title: return False
    t = title.strip()
    if len(t.split()) < 4: return False

    # reject hype from non-serious sources
    if HYPE.search(t) and source_weight(src) < 0.6:
        return False

    return True


# ===================== Fetch Headlines ===========================

def fetch():
    if not NEWS_KEY:
        logging.warning("NEWS_API_KEY missing.")
        return []
    try:
        import requests
        r = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "(XRP OR Ripple)",
                "searchIn": "title",
                "language": "en",
                "pageSize": 50,
                "sortBy": "publishedAt",
                "apiKey": NEWS_KEY
            }, timeout=15
        )
        if not r.ok:
            logging.warning(f"News API error: {r.status_code}")
            return []
        return r.json().get("articles", [])
    except Exception as e:
        logging.error(f"News fetch failed: {e}")
        return []


# ====================== FinBERT via HF SDK =======================

def finbert(text: str):
    """
    Returns (positive, negative, neutral) float scores.
    Uses retry to handle transient errors.
    """
    if not HF_CLIENT:
        return None

    for _ in range(2):  # retry once
        try:
            res = HF_CLIENT.text_classification(text, model="ProsusAI/finbert")
            scores = {x["label"]: x["score"] for x in res}
            return (
                scores.get("positive", 0.0),
                scores.get("negative", 0.0),
                scores.get("neutral", 0.0),
            )
        except Exception as e:
            logging.warning(f"FinBERT error, retrying: {str(e)[:120]}")
            time.sleep(0.3)

    return None


# ===================== Score Aggregation ==========================

def weighted_trimmed_mean(scores, weights):
    if not scores: return 0.0
    arr = np.array(scores)
    w   = np.array(weights)
    if w.sum() <= 0: return 0.0

    idx = arr.argsort()
    k = max(1, int(len(arr) * 0.2))
    arr_trim = arr[idx][k:-k] if len(arr) > 2*k else arr
    w_trim   = w[idx][k:-k]   if len(w)   > 2*k else w
    return float(np.average(arr_trim, weights=w_trim))


# ====================== Utility ==================================

def ts(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def push(payload):
    try:
        rdb.set("news:sentiment", json.dumps(payload))
        logging.info(f"Pushed → {payload}")
    except Exception as e:
        logging.error(f"Redis push failed: {e}")


# ======================= Main Routine =============================

def run_once():
    arts = fetch()
    good = []

    for a in arts:
        src = a.get("source", {}).get("name", "")
        title = (a.get("title") or "").strip()
        if clean_headline(title, src):
            good.append({"source": src, "title": title})

    logging.info(f"Valid headlines: {len(good)}")

    if not good:
        push({"timestamp": ts(), "score": 0.0, "count": 0, "articles": []})
        return

    random.shuffle(good)
    scored = []
    scalars = []
    weights = []

    for a in good[:16]:
        res = finbert(a["title"])
        w = source_weight(a["source"])

        if res is not None:
            pos, neg, neu = res
            scalar = pos - neg

            scored.append({**a, "pos": pos, "neg": neg, "neu": neu, "scalar": scalar, "weight": w})
            scalars.append(scalar)
            weights.append(w)
        else:
            scored.append({**a, "pos": None, "neg": None, "neu": None, "scalar": None, "weight": w})

        time.sleep(0.22)

    final = weighted_trimmed_mean(scalars, weights)
    push({
        "timestamp": ts(),
        "score": final,
        "count": len(scalars),
        "mode": "weighted_all",
        "articles": scored
    })


# ======================= Loop ====================================

def run_loop():
    while True:
        try:
            run_once()
        except Exception as e:
            logging.error(f"Loop error: {e}")
        time.sleep(RUN_INTERVAL)


if __name__ == "__main__":
    run_loop()
