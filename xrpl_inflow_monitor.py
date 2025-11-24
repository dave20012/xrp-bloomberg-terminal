# ================= XRPL INFLOW MONITOR v9.3 ================= #
# Tracks large inflows to exchanges via Whale Alert, labels:
# - exchange (Binance / Kraken / etc)
# - ripple_corp (Ripple treasury -> exchange)
# Pushes latest snapshot to Redis under "xrpl:latest_inflows"

import json
import logging
from datetime import datetime, timezone
import os
import time
from typing import Dict, List, Optional, Set

import requests

from redis_client import rdb
from exchange_addresses import EXCHANGE_ADDRESSES, EXCHANGE_WEIGHTS, RIPPLE_CORP_ADDRESSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

WHALE_ALERT_API = "https://api.whale-alert.io/v1/transactions"
RIPPLE_DATA_API = "https://data.ripple.com/v2/accounts/{address}/transactions"
RIPPLE_DATA_HEADERS = {"User-Agent": "xrpl-inflow-monitor/1.0", "Accept": "application/json"}

RIPPLE_DATA_COOLDOWN_SECONDS = int(os.getenv("RIPPLE_DATA_COOLDOWN_SECONDS", "900"))
RIPPLE_DATA_MAX_COOLDOWN_SECONDS = int(os.getenv("RIPPLE_DATA_MAX_COOLDOWN_SECONDS", "3600"))
RIPPLE_DATA_REQUEST_INTERVAL = float(os.getenv("RIPPLE_DATA_REQUEST_INTERVAL", "1.0"))

WHALE_ALERT_KEY = os.getenv("WHALE_ALERT_KEY")
ENV_PROVIDER = os.getenv("XRPL_INFLOWS_PROVIDER", "whale_alert").lower()
PROVIDER = ENV_PROVIDER
RUN = int(os.getenv("XRPL_INFLOWS_INTERVAL", "600"))  # 10m default
MIN_XRP = float(os.getenv("XRPL_MIN_XRP", "10000000"))
LOOKBACK_SECONDS = int(os.getenv("XRPL_LOOKBACK_SECONDS", str(max(RUN * 2, 900))))

_missing_key_info_logged = False
ripple_data_cooldown_until = 0.0
ripple_data_failure_streak = 0


def resolve_provider() -> str:
    """Return the effective provider, respecting env and missing Whale Alert key."""

    global _missing_key_info_logged

    if PROVIDER == "whale_alert" and not WHALE_ALERT_KEY:
        if not _missing_key_info_logged:
            logging.info(
                "WHALE_ALERT_KEY missing; defaulting XRPL inflow provider to ripple_data",
            )
            _missing_key_info_logged = True
        return "ripple_data"

    return PROVIDER


def fetch_xrp_usd_price() -> float:
    """Fetch current XRP/USD price for threshold conversion."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ripple", "vs_currencies": "usd"},
            timeout=10,
        )
        if not resp.ok:
            logging.warning(f"Price API error: {resp.status_code}")
            return 0.0
        data = resp.json() or {}
        xrp = data.get("ripple") or {}
        return float(xrp.get("usd") or 0.0)
    except Exception as e:
        logging.error(f"Failed to fetch XRP price: {e}")
        return 0.0


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


def fetch_transactions_whale_alert() -> List[Dict]:
    """Fetch inflow transactions using Whale Alert (paid API)."""
    if not WHALE_ALERT_KEY:
        logging.warning("WHALE_ALERT_KEY missing.")
        return []

    price_usd = fetch_xrp_usd_price()
    if price_usd <= 0:
        logging.warning("XRP/USD price unavailable; using raw XRPL_MIN_XRP as USD for Whale Alert threshold")
    min_value_usd = MIN_XRP * price_usd if price_usd > 0 else MIN_XRP

    try:
        r = requests.get(
            WHALE_ALERT_API,
            params={
                "currency": "xrp",
                "min_value": min_value_usd,
                "limit": 50,
                "api_key": WHALE_ALERT_KEY,
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


def parse_timestamp(date_str: str) -> int:
    if not date_str:
        return 0
    try:
        return int(time.mktime(time.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return 0


def monitored_addresses() -> Set[str]:
    addrs: Set[str] = set()
    for lst in EXCHANGE_ADDRESSES.values():
        addrs.update(lst)
    return addrs


def fetch_cached_flows() -> List[Dict]:
    """Return the last successful inflow snapshot from Redis (if available)."""

    try:
        raw = rdb.get("xrpl:latest_inflows")
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    return []


def prefer_cached_when_empty(flows: List[Dict]) -> List[Dict]:
    """Return cached inflows when a polling run produced no data."""

    if flows:
        return flows

    cached = fetch_cached_flows()
    if cached:
        logging.info("Using cached XRPL inflows because fresh poll returned no data")
        return cached

    return []


def fetch_transactions_ripple_data() -> List[Dict]:
    """Fetch inflows to curated exchange addresses using Ripple Data (free)."""

    global ripple_data_failure_streak

    flows: List[Dict] = []
    start = time.time() - LOOKBACK_SECONDS
    start_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start))

    global ripple_data_cooldown_until
    now = time.time()
    if ripple_data_cooldown_until and now < ripple_data_cooldown_until:
        logging.warning(
            "Ripple Data API is in cooldown until %s; reusing cached inflows where possible",
            datetime.fromtimestamp(ripple_data_cooldown_until, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        return fetch_cached_flows()

    for address in monitored_addresses():
        try:
            resp = requests.get(
                RIPPLE_DATA_API.format(address=address),
                params={
                    "type": "Payment",
                    "result": "tesSUCCESS",
                    "limit": 50,
                    "start": start_str,
                },
                headers=RIPPLE_DATA_HEADERS,
                timeout=15,
            )
            if resp.status_code == 403:
                ripple_data_failure_streak = min(ripple_data_failure_streak + 1, 10)
                cooldown_seconds = min(
                    RIPPLE_DATA_COOLDOWN_SECONDS * (2 ** (ripple_data_failure_streak - 1)),
                    RIPPLE_DATA_MAX_COOLDOWN_SECONDS,
                )
                ripple_data_cooldown_until = time.time() + cooldown_seconds
                logging.warning(
                    "Ripple Data API returned 403 for %s; halting further XRPL inflow requests for %.0fs (failure streak: %d)",
                    address,
                    cooldown_seconds,
                    ripple_data_failure_streak,
                )
                cached = fetch_cached_flows()
                if cached:
                    logging.info("Serving cached XRPL inflows during Ripple Data cooldown")
                    return cached
                break
            if not resp.ok:
                logging.warning(f"Ripple Data API error {resp.status_code} for {address}")
                time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
                continue

            data = resp.json()
            ripple_data_failure_streak = 0
            for entry in data.get("transactions", []):
                tx = entry.get("tx", {})
                destination = tx.get("Destination", "")
                if destination != address:
                    continue  # ensure inflow into monitored address

                amt = tx.get("Amount")
                try:
                    # For XRP, Amount is in drops (string)
                    xrp_amt = float(amt) / 1_000_000 if amt is not None else 0.0
                except Exception:
                    continue

                if xrp_amt < MIN_XRP:
                    continue

                timestamp = parse_timestamp(entry.get("date") or entry.get("executed_time"))
                from_addr = tx.get("Account", "")

                lower_from = from_addr.lower() if from_addr else ""
                ripple_corp = from_addr in RIPPLE_CORP_ADDRESSES or lower_from.startswith("ripple")
                canonical_ex = owner_from_address(address)
                w = exchange_weight(canonical_ex)

                flows.append(
                    {
                        "timestamp": timestamp,
                        "xrp": xrp_amt,
                        "exchange": canonical_ex,
                        "to_address": address,
                        "from_address": from_addr,
                        "to_owner": canonical_ex,
                        "from_owner": "",  # Ripple Data API does not provide owner strings
                        "weight": w,
                        "ripple_corp": ripple_corp,
                    }
                )
            time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
        except Exception as e:
            logging.error(f"Ripple Data fetch failed for {address}: {e}")

    # De-duplicate by transaction hash + destination to avoid duplicates across pages
    uniq = {}
    for f in flows:
        key = (f.get("from_address"), f.get("to_address"), f.get("timestamp"), f.get("xrp"))
        if key not in uniq:
            uniq[key] = f
    return list(uniq.values())


def fetch_transactions(provider: Optional[str] = None) -> List[Dict]:
    resolved_provider = provider or resolve_provider()

    if resolved_provider == "ripple_data":
        return fetch_transactions_ripple_data()

    if resolved_provider != "whale_alert":
        logging.warning(
            "Unknown XRPL inflow provider %s; defaulting to Whale Alert", resolved_provider
        )

    txs = fetch_transactions_whale_alert()
    if txs:
        return txs

    logging.info("Whale Alert unavailable or empty; falling back to Ripple Data API")
    return fetch_transactions_ripple_data()


def build_flows():
    resolved_provider = resolve_provider()

    if resolved_provider == "ripple_data":
        return prefer_cached_when_empty(fetch_transactions_ripple_data())

    txs = fetch_transactions(resolved_provider)
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

    return prefer_cached_when_empty(flows)


def push(flows):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(flows))
        rdb.set(
            "xrpl:latest_inflows_meta",
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "provider": resolve_provider(),
                    "count": len(flows),
                    "run_seconds": RUN,
                }
            ),
        )
        logging.info(f"XRPL inflows snapshot pushed: {len(flows)} txs")
        append_history(flows)
    except Exception as e:
        logging.error(f"XRPL inflows push failed: {e}")


def append_history(flows, max_len: int = 240):
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_xrp": float(sum(f.get("xrp", 0.0) for f in flows)),
        "weighted_xrp": float(sum(f.get("xrp", 0.0) * f.get("weight", 1.0) for f in flows)),
    }

    try:
        raw = rdb.get("xrpl:inflow_history")
        history = json.loads(raw) if raw else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    history.append(snapshot)
    history = history[-max_len:]

    try:
        rdb.set("xrpl:inflow_history", json.dumps(history))
    except Exception as e:
        logging.error(f"XRPL inflow history write failed: {e}")


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
