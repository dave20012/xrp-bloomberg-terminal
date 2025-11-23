# ================= XRPL INFLOW MONITOR v10.2 ================= #
# Tracks large XRPL flows into exchanges & Ripple OTC → exchanges.
# Pushes latest snapshot into Redis key: "xrpl:latest_inflows".

import time
import os
import json
import logging
import requests

from redis_client import rdb
from exchange_addresses import EXCHANGE_ADDRESSES, RIPPLE_CORP_ADDRESSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

API = "https://api.whale-alert.io/v1/transactions"
KEY = os.getenv("WHALE_ALERT_KEY")
RUN = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default


def _exchange_from_address(addr: str) -> str | None:
    if not addr:
        return None
    for name, addrs in EXCHANGE_ADDRESSES.items():
        if addr in addrs:
            return name
    return None


def fetch_transactions():
    if not KEY:
        logging.warning("WHALE_ALERT_KEY missing; XRPL inflow monitor idle.")
        return []

    try:
        r = requests.get(
            API,
            params={
                "currency": "xrp",
                "min_value": 10_000_000,  # >10M USD threshold
                "limit": 50,
                "api_key": KEY,
            },
            timeout=20,
        )
        if not r.ok:
            logging.warning(f"Whale Alert error: {r.status_code} {r.text[:120]}")
            return []
        data = r.json()
        return data.get("transactions", []) or []
    except Exception as e:
        logging.error(f"Whale Alert fetch failed: {e}")
        return []


def build_flows():
    txs = fetch_transactions()
    flows = []

    for t in txs:
        if not isinstance(t, dict):
            continue
        if t.get("blockchain") != "ripple":
            continue

        amount_xrp = float(t.get("amount", 0.0) or 0.0)
        if amount_xrp <= 0:
            continue

        ts = t.get("timestamp")
        amount_usd = t.get("amount_usd")
        from_obj = t.get("from", {}) or {}
        to_obj = t.get("to", {}) or {}

        from_addr = from_obj.get("address")
        to_addr = to_obj.get("address")
        to_type = to_obj.get("owner_type")
        to_owner = to_obj.get("owner")

        # try to resolve exchange name via address dictionary
        exchange_name = _exchange_from_address(to_addr)
        if not exchange_name and to_type == "exchange":
            exchange_name = to_owner

        if not exchange_name:
            # not clearly an exchange inflow
            continue

        # classify Ripple OTC if sender is corporate + receiver is exchange
        flow_type = "exchange_inflow"
        if from_addr in RIPPLE_CORP_ADDRESSES:
            flow_type = "ripple_otc"

        flows.append(
            {
                "timestamp": ts,
                "xrp": amount_xrp,
                "amount_usd": amount_usd,
                "exchange": exchange_name,
                "from_address": from_addr,
                "to_address": to_addr,
                "flow_type": flow_type,  # "exchange_inflow" or "ripple_otc"
            }
        )

    return flows


def push(flows):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(flows))
        logging.info(
            f"XRPL inflows pushed: {len(flows)} records "
            f"({sum(f.get('xrp', 0.0) for f in flows):,.0f} XRP)"
        )
    except Exception as e:
        logging.error(f"XRPL inflow Redis push failed: {e}")


def loop():
    while True:
        try:
            flows = build_flows()
            push(flows)
        except Exception as e:
            logging.error(f"XRPL inflow loop error: {e}")
        time.sleep(RUN)


if __name__ == "__main__":
    loop()
