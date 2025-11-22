# xrpl_inflow_monitor.py — XRPL INFLOW MONITOR v10.1
# Tracks large inbound flows to exchanges, pushes to Redis for dashboard

import time
import os
import json
import logging
import requests
from redis_client import rdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

API = "https://api.whale-alert.io/v1/transactions"
KEY = os.getenv("WHALE_ALERT_KEY")
RUN = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default


def fetch_transactions():
    """Fetch large XRP transactions from Whale Alert."""
    if not KEY:
        logging.warning("WHALE_ALERT_KEY missing.")
        return []

    try:
        r = requests.get(
            API,
            params={
                "currency": "xrp",
                "min_value": 10_000_000,
                "limit": 30,
                "api_key": KEY,
            },
            timeout=15,
        )
        if not r.ok:
            logging.warning(f"Whale Alert error: {r.status_code} {r.text[:120]}")
            return []
        data = r.json()
        return data.get("transactions", []) or []
    except Exception as e:
        logging.error(f"Whale Alert fetch failed: {e}")
        return []


def extract_exchange_inflows():
    """
    Convert Whale Alert transactions to a normalized list.

    Structure pushed to Redis:
    [
      {
        "timestamp": <unix>,
        "xrp": <float>,                # XRP amount
        "exchange": <str or None>,
        "from": <str or None>,
        "destination": <str or None>,
        "type": "deposit"
      },
      ...
    ]
    """
    txs = fetch_transactions()
    flows = []

    for t in txs:
        if not isinstance(t, dict):
            continue

        to_obj = t.get("to") or {}
        from_obj = t.get("from") or {}

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
                "exchange": to_obj.get("owner"),
                "from": from_obj.get("owner"),
                "destination": to_obj.get("owner"),
                "type": "deposit",
            }
        )

    return flows


def push(flows):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(flows))
        logging.info(f"XRPL inflows pushed {len(flows)} entries")
    except Exception as e:
        logging.error(f"XRPL inflows push failed: {e}")


def loop():
    while True:
        try:
            flows = extract_exchange_inflows()
            push(flows)
        except Exception as e:
            logging.error(f"XRPL inflow loop error: {e}")
        time.sleep(RUN)


if __name__ == "__main__":
    loop()
