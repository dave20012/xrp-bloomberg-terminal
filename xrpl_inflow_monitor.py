# xrpl_inflow_monitor.py
import time
import requests
import json
from exchange_addresses import EXCHANGE_ADDRESSES
from redis_client import rdb
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

# After fetching inflow data
logging.info(f"Fetched {len(inflow_data)} XRPL inflow records")

# After any processing step
logging.info(f"Processed inflow data: {summary_stats}")

# Before sending to main reporting service
logging.info(f"Pushing inflow payload: {payload}")

XRPL_API = "https://s1.ripple.com:51234"
POLL_SECONDS = int(__import__("os").getenv("XRPL_POLL_SECONDS", "30"))
MIN_XRP = float(__import__("os").getenv("XRPL_MIN_XRP", "250000"))  # threshold

def fetch_ledger_index():
    r = requests.post(XRPL_API, json={"method": "ledger", "params": [{"ledger_index": "validated"}]}, timeout=10)
    r.raise_for_status()
    return r.json()["result"]["ledger_index"]

def fetch_tx_in_ledger(ledger_index):
    r = requests.post(
        XRPL_API,
        json={"method": "ledger", "params": [{"ledger_index": ledger_index, "transactions": True, "expand": True}]},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["result"]["ledger"].get("transactions", [])

def is_exchange_destination(addr):
    for ex, accs in EXCHANGE_ADDRESSES.items():
        if addr in accs:
            return ex
    return None

def extract_inflows(txs):
    events = []
    for tx in txs:
        if tx.get("TransactionType") != "Payment":
            continue
        amt = tx.get("Amount")
        if not isinstance(amt, str):
            continue
        dest = tx.get("Destination")
        ex = is_exchange_destination(dest)
        if not ex:
            continue
        try:
            drops = int(amt)
        except Exception:
            continue
        xrp = drops / 1_000_000.0
        if xrp < MIN_XRP:
            continue
        events.append(
            {
                "exchange": ex,
                "xrp": xrp,
                "from": tx.get("Account"),
                "destination": dest,
                "tx_hash": tx.get("hash"),
                "timestamp": tx.get("date") if tx.get("date") else None,
            }
        )
    return events

def push_payload(payload):
    try:
        # store latest inflows as JSON string
        rdb.set("xrpl:latest_inflows", json.dumps(payload))
        # maintain history
        rdb.lpush("xrpl:inflow_history", json.dumps(payload))
        rdb.ltrim("xrpl:inflow_history", 0, 199)
        logging.info(f"Payload successfully pushed: {payload}")
    except Exception as e:
        logging.error(f"Failed to push payload: {e}")

def run_loop():
    last_ledger = None
    while True:
        try:
            ledger = fetch_ledger_index()
            logging.info(f"Fetched latest ledger index: {ledger}")
            if last_ledger is None:
                last_ledger = ledger
                time.sleep(POLL_SECONDS)
                continue
            while last_ledger < ledger:
                last_ledger += 1
                try:
                    txs = fetch_tx_in_ledger(last_ledger)
                    logging.info(f"Fetched {len(txs)} transactions for ledger {last_ledger}")
                except Exception as e:
                    logging.error(f"Failed fetching transactions for ledger {last_ledger}: {e}")
                    continue
                inflows = extract_inflows(txs)
                logging.info(f"Processed {len(inflows)} inflow events for ledger {last_ledger}")
                if inflows:
                    push_payload(inflows)
            time.sleep(POLL_SECONDS)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    run_loop()
