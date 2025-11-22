# ================= SENTIMENT WORKER v10 ================= #
# Fetch headlines with strict anti-hype filter, score via FinBERT, push to Redis

import os, time, json, random, re, requests, numpy as np, logging
from datetime import datetime, timezone
from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

NEWS_KEY = os.getenv("NEWS_API_KEY")
HF_TOKEN = os.getenv("HF_TOKEN")
RUN = int(os.getenv("SENTIMENT_RUN_INTERVAL", "1800"))  # 30m

HYPE = re.compile(r"(price|surge|massive|soars|dip|moon|target|prediction|forecast)", re.IGNORECASE)

def clean(t):
    if not t: return False
    t=t.strip()
    if len(t.split())<5: return False
    if HYPE.search(t): return False
    return True

def fetch():
    if not NEWS_KEY: return []
    try:
        r = requests.get("https://newsapi.org/v2/everything",
                         params={"q":"(XRP OR Ripple)","searchIn":"title","language":"en","pageSize":20,"sortBy":"publishedAt","apiKey":NEWS_KEY}, timeout=15)
        if not r.ok: return []
        return r.json().get("articles",[])
    except: return []

def finbert(text):
    if not HF_TOKEN: return None
    try:
        r = requests.post("https://api-inference.huggingface.co/models/ProsusAI/finbert",
                          headers={"Authorization":f"Bearer {HF_TOKEN}"}, json={"inputs":text}, timeout=15)
        if not r.ok:
            logging.warning(f"FinBERT error {r.status_code}: {r.text[:120]}")
            return None
        res=r.json()
        if not isinstance(res,list) or not res: return None
        try:
            s={i["label"]:i["score"] for i in res[0]}
            return s.get("positive",0)-s.get("negative",0)
        except: return None
    except Exception as e:
        logging.error(e); return None

def trim(s):
    if not s: return 0.0
    a=np.sort(np.array(s)); k=int(len(a)*0.2)
    return float(np.mean(a[k:len(a)-k] if len(a)>2*k else a))

def ts(): return datetime.now(timezone.utc).isoformat().replace("+00:00","Z")

def push(p):
    try: rdb.set("news:sentiment", json.dumps(p)); logging.info(f"Pushed → {p}")
    except Exception as e: logging.error(e)

def run_once():
    arts=fetch()
    good=[{"source":a.get("source",{}).get("name"),"title":a.get("title")} for a in arts if clean(a.get("title"))]
    logging.info(f"Valid headlines: {len(good)}")
    if not good:
        push({"timestamp":ts(),"score":0,"count":0,"articles":[]}); return

    random.shuffle(good)
    svals=[]; scored=[]
    for a in good[:12]:
        sc=finbert(a["title"])
        scored.append({**a,"score":sc})
        if sc is not None: svals.append(sc)
        time.sleep(0.25)

    push({"timestamp":ts(),"score":trim(svals),"count":len(svals),"articles":scored})

def run_loop():
    while True:
        try: run_once()
        except Exception as e: logging.error(e)
        time.sleep(RUN)

if __name__=="__main__": run_loop()
