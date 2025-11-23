# ================= XRPL INFLOW MONITOR v10.1 ================= #
# Tracks large XRP inbound flows to exchanges via Whale Alert
# Pushes structured snapshot to Redis: xrpl:latest_inflows

import time
import os
import json
import logging
import requests
from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

API_URL = "https://api.whale-alert.io/v1/transactions"
WHALE_ALERT_KEY = os.getenv("WHALE_ALERT_KEY")
RUN_INTERVAL = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default


def fetch_raw():
    if not WHALE_ALERT_KEY:
        logging.warning("WHALE_ALERT_KEY missing.")
        return []

    try:
        r = requests.get(
            API_URL,
            params={
                "currency": "xrp",
                "min_value": 10_000_000,  # 10M XRP threshold
                "limit": 30,
                "api_key": WHALE_ALERT_KEY,
            },
            timeout=15,
        )
        if not r.ok:
            logging.warning(f"Whale Alert error: {r.status_code}")
            return []
        data = r.json()
        return data.get("transactions", [])
    except Exception as e:
        logging.error(f"Whale Alert fetch failed: {e}")
        return []


def normalize_transactions(tx_list):
    """
    Normalize Whale Alert schema into a simple XRPL inflow snapshot:
    - only transactions where 'to.owner_type' == 'exchange'
    - fields: timestamp, xrp, exchange, from, from_address, to_address
    """
    flows = []
    for t in tx_list:
        if not isinstance(t, dict):
            continue

        to_obj = t.get("to", {}) or {}
        from_obj = t.get("from", {}) or {}

        if to_obj.get("owner_type") != "exchange":
            continue

        try:
            amt = float(t.get("amount", 0.0))
        except Exception:
            amt = 0.0

        flows.append(
            {
                "timestamp": t.get("timestamp"),
                "xrp": amt,
                "exchange": to_obj.get("owner") or "unknown",
                "from": from_obj.get("owner") or "unknown",
                "from_address": from_obj.get("address"),
                "to_address": to_obj.get("address"),
                "txid": t.get("hash") or t.get("transaction_hash"),
            }
        )

    return flows


def push_snapshot(flows):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(flows))
        logging.info(f"XRPL inflows snapshot pushed: {len(flows)} records")
    except Exception as e:
        logging.error(f"Redis push failed: {e}")


def run_once():
    raw = fetch_raw()
    flows = normalize_transactions(raw)
    push_snapshot(flows)


def loop():
    while True:
        try:
            run_once()
        except Exception as e:
            logging.error(f"XRPL inflow loop error: {e}")
        time.sleep(RUN_INTERVAL)


if __name__ == "__main__":
    loop()
