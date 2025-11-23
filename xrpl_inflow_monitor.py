# ================= XRPL INFLOW MONITOR v9.3 ================= #
# Tracks large inflows to exchanges via Whale Alert, labels:
# - exchange (Binance / Kraken / etc)
# - ripple_corp (Ripple treasury -> exchange)
# Pushes latest snapshot to Redis under "xrpl:latest_inflows"

import time
import os
import json
import logging
import requests

from redis_client import rdb
from exchange_addresses import EXCHANGE_ADDRESSES, EXCHANGE_WEIGHTS, RIPPLE_CORP_ADDRESSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

API = "https://api.whale-alert.io/v1/transactions"
KEY = os.getenv("WHALE_ALERT_KEY")
RUN = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default


def owner_from_address(addr: str) -> str:
    """Resolve a deposit address to canonical exchange name using EXCHANGE_ADDRESSES."""
    if not addr:
        return ""
    a = addr.strip()
    for ex, lst in EXCHANGE_ADDRESSES.items():
        if a in lst:
            return ex
    return ""


def exchange_weight(exchange: str) -> float:
    return float(EXCHANGE_WEIGHTS.get(exchange, 0.5))


def fetch_transactions():
    if not KEY:
        logging.warning("WHALE_ALERT_KEY missing.")
        return []

    try:
        r = requests.get(
            API,
            params={
                "currency": "xrp",
                "min_value": 10_000_000,  # >10M XRP
                "limit": 50,
                "api_key": KEY,
            },
            timeout=15,
        )
        if not r.ok:
            logging.warning(f"Whale Alert error: {r.status_code}")
            return []
        return r.json().get("transactions", [])
    except Exception as e:
        logging.error(f"Whale Alert fetch failed: {e}")
        return []


def build_flows():
    txs = fetch_transactions()
    flows = []

    for t in txs:
        if not isinstance(t, dict):
            continue
        to_obj = t.get("to") or {}
        from_obj = t.get("from") or {}

        # Only care about inflows INTO exchanges
        if to_obj.get("owner_type") != "exchange":
            continue

        try:
            amt = float(t.get("amount", 0.0))
        except Exception:
            continue
        if amt <= 0:
            continue

        ts = t.get("timestamp")
        to_addr = to_obj.get("address", "")
        from_addr = from_obj.get("address", "")
        to_owner = to_obj.get("owner", "")
        from_owner = from_obj.get("owner", "")

        # Attempt to normalise exchange label
        canonical_ex = owner_from_address(to_addr) or to_owner or "Unknown"

        # Ripple OTC detection: Ripple corporate/treasury → exchange
        ripple_corp = False
        if from_addr in RIPPLE_CORP_ADDRESSES or (from_owner or "").lower().startswith("ripple"):
            ripple_corp = True

        w = exchange_weight(canonical_ex)

        flows.append(
            {
                "timestamp": ts,
                "xrp": amt,
                "exchange": canonical_ex,
                "to_address": to_addr,
                "from_address": from_addr,
                "to_owner": to_owner,
                "from_owner": from_owner,
                "weight": w,
                "ripple_corp": ripple_corp,
            }
        )

    return flows


def push(flows):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(flows))
        logging.info(f"XRPL inflows snapshot pushed: {len(flows)} txs")
    except Exception as e:
        logging.error(f"XRPL inflows push failed: {e}")


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
