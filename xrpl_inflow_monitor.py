# ================= XRPL INFLOW MONITOR v10 ================= #
# Tracks large inbound flows to exchanges, pushes to Redis list

import time, os, json, logging, requests
from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
API = "https://api.whale-alert.io/v1/transactions"
KEY = os.getenv("WHALE_ALERT_KEY")
RUN = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default

def fetch():
    try:
        r=requests.get(API,params={"currency":"xrp","min_value":10000000,"limit":30,"api_key":KEY},timeout=15)
        if r.ok: return r.json().get("transactions",[])
    except: pass
    return []

def handle():
    tx=fetch(); flows=[]
    for t in tx:
        if not isinstance(t,dict): continue
        if t["to"].get("owner_type")=="exchange":
            flows.append({"ts":t.get("timestamp"),"xrp":t.get("amount"),"to":t["to"].get("owner"),"type":"deposit"})
    return flows

def push(lst):
    try: rdb.set("xrpl:latest_inflows", json.dumps(lst)); logging.info(f"XRPL inflows pushed {len(lst)}")
    except Exception as e: logging.error(e)

def loop():
    while True:
        try:
            push(handle())
        except Exception as e: logging.error(e)
        time.sleep(RUN)

if __name__=="__main__": loop()
