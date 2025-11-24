# ================= XRPL INFLOW MONITOR v9.3 ================= #
# Tracks large inflows to exchanges via Whale Alert, labels:
# - exchange (Binance / Kraken / etc)
# - ripple_corp (Ripple treasury -> exchange)
# Pushes latest snapshot to Redis under "xrpl:latest_inflows"

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

import requests

from redis_client import rdb
from exchange_addresses import EXCHANGE_ADDRESSES, EXCHANGE_WEIGHTS, RIPPLE_CORP_ADDRESSES

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

WHALE_ALERT_API = "https://api.whale-alert.io/v1/transactions"
RIPPLE_DATA_API = "https://data.ripple.com/v2/accounts/{address}/transactions"
RIPPLE_DATA_HEADERS = {"User-Agent": "xrpl-inflow-monitor/1.0", "Accept": "application/json"}
RIPPLE_RPC_ENDPOINTS = [
    e.strip()
    for e in os.getenv(
        "XRPL_RPC_ENDPOINTS",
        "https://s2.ripple.com:51234/,https://s1.ripple.com:51234/",
    ).split(",")
    if e.strip()
]

RIPPLE_DATA_COOLDOWN_SECONDS = int(os.getenv("RIPPLE_DATA_COOLDOWN_SECONDS", "900"))
RIPPLE_DATA_MAX_COOLDOWN_SECONDS = int(os.getenv("RIPPLE_DATA_MAX_COOLDOWN_SECONDS", "3600"))
RIPPLE_DATA_REQUEST_INTERVAL = float(os.getenv("RIPPLE_DATA_REQUEST_INTERVAL", "1.0"))

WHALE_ALERT_KEY = os.getenv("WHALE_ALERT_KEY")
ENV_PROVIDER = os.getenv("XRPL_INFLOWS_PROVIDER", "whale_alert").lower()
PROVIDER = ENV_PROVIDER


def _read_poll_interval() -> int:
    """Read the XRPL poll interval, supporting legacy + documented env vars."""

    poll_env = os.getenv("XRPL_POLL_SECONDS")
    legacy_env = os.getenv("XRPL_INFLOWS_INTERVAL")

    if poll_env:
        return int(poll_env)
    if legacy_env:
        return int(legacy_env)
    return 600


RUN = _read_poll_interval()  # 10m default
MIN_XRP = float(os.getenv("XRPL_MIN_XRP", "10000000"))
MONITOR_OUTFLOWS = (os.getenv("XRPL_MONITOR_OUTFLOWS", "1").strip() or "1").lower() in (
    "1",
    "true",
    "yes",
)
LOOKBACK_SECONDS = int(os.getenv("XRPL_LOOKBACK_SECONDS", str(max(RUN * 2, 900))))

_missing_key_info_logged = False
ripple_data_cooldown_until = 0.0
ripple_data_failure_streak = 0
ripple_data_blocked_addresses: Set[str] = set()
_rpc_endpoint_index = 0


def _note_ripple_data_error(status_code: Optional[int] = None) -> None:
    """Track Ripple Data failures and set a cooldown after repeated errors."""

    global ripple_data_failure_streak, ripple_data_cooldown_until

    ripple_data_failure_streak += 1
    if ripple_data_failure_streak >= 3:
        backoff = min(
            RIPPLE_DATA_COOLDOWN_SECONDS * ripple_data_failure_streak,
            RIPPLE_DATA_MAX_COOLDOWN_SECONDS,
        )
        ripple_data_cooldown_until = time.time() + backoff
        logging.warning(
            "Ripple Data errors reached %s (last status: %s); cooling down for %ss",
            ripple_data_failure_streak,
            status_code,
            backoff,
        )


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


def fetch_transactions_whale_alert() -> Tuple[List[Dict], List[Dict]]:
    """Fetch inflow/outflow transactions using Whale Alert (paid API)."""
    if not WHALE_ALERT_KEY:
        logging.warning("WHALE_ALERT_KEY missing.")
        return [], []

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
            return [], []
        return r.json().get("transactions", []), []
    except Exception as e:
        logging.error(f"Whale Alert fetch failed: {e}")
        return [], []


def parse_timestamp(date_str: str) -> int:
    if not date_str:
        return 0
    try:
        return int(time.mktime(time.strptime(date_str, "%Y-%m-%dT%H:%M:%SZ")))
    except Exception:
        return 0


def monitored_addresses() -> Set[str]:
    """Return sanitized exchange addresses to monitor."""

    addrs: Set[str] = set()
    for lst in EXCHANGE_ADDRESSES.values():
        for addr in lst:
            if not addr:
                continue
            addr_clean = addr.strip()
            if addr_clean:
                addrs.add(addr_clean)
    return addrs


def rotate_rpc_endpoint() -> str:
    """Return the next XRPL public RPC endpoint in a round-robin fashion."""

    global _rpc_endpoint_index

    if not RIPPLE_RPC_ENDPOINTS:
        return ""

    endpoint = RIPPLE_RPC_ENDPOINTS[_rpc_endpoint_index % len(RIPPLE_RPC_ENDPOINTS)]
    _rpc_endpoint_index += 1
    return endpoint


def ripple_epoch_to_unix(ripple_seconds: Optional[int]) -> int:
    """Convert Ripple epoch seconds to unix epoch seconds."""

    if ripple_seconds is None:
        return 0
    try:
        return int(ripple_seconds) + 946684800  # Ripple epoch starts 2000-01-01
    except Exception:
        return 0


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


def fetch_cached_outflows() -> List[Dict]:
    """Return the last successful outflow snapshot from Redis (if available)."""

    try:
        raw = rdb.get("xrpl:latest_outflows")
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


def fetch_transactions_rippled_rpc() -> Tuple[List[Dict], List[Dict]]:
    """Fetch inflows + optional outflows using public rippled JSON-RPC servers."""

    flows: List[Dict] = []
    outflows: List[Dict] = []
    start = time.time() - LOOKBACK_SECONDS

    for address in monitored_addresses():
        endpoint = rotate_rpc_endpoint()
        if not endpoint:
            logging.warning("No XRPL RPC endpoints configured")
            break

        try:
            resp = requests.post(
                endpoint,
                json={
                    "method": "account_tx",
                    "params": [
                        {
                            "account": address,
                            "ledger_index_min": -1,
                            "ledger_index_max": -1,
                            "limit": 200,
                            "forward": False,
                        }
                    ],
                },
                timeout=20,
                headers={"User-Agent": "xrpl-inflow-monitor/1.0"},
            )

            if not resp.ok:
                logging.warning(
                    "rippled RPC error %s for %s via %s", resp.status_code, address, endpoint
                )
                time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
                continue

            data = resp.json() or {}
            result = data.get("result") or {}
            transactions = result.get("transactions") or []

            for entry in transactions:
                tx = entry.get("tx") or {}

                if tx.get("TransactionType") != "Payment":
                    continue

                destination = tx.get("Destination", "")
                amt = tx.get("Amount")
                if not isinstance(amt, (int, float, str)):
                    continue  # Ignore non-XRP payments

                try:
                    xrp_amt = float(amt) / 1_000_000
                except Exception:
                    continue

                if xrp_amt < MIN_XRP:
                    continue

                ripple_ts = tx.get("date") or entry.get("tx_json", {}).get("date")
                ts = ripple_epoch_to_unix(ripple_ts)

                if ts and ts < start:
                    continue

                from_addr = tx.get("Account", "")
                lower_from = from_addr.lower() if from_addr else ""
                ripple_corp = from_addr in RIPPLE_CORP_ADDRESSES or lower_from.startswith(
                    "ripple"
                )
                canonical_ex = owner_from_address(address)
                w = exchange_weight(canonical_ex)

                if destination == address:
                    flows.append(
                        {
                            "timestamp": ts,
                            "xrp": xrp_amt,
                            "exchange": canonical_ex,
                            "to_address": address,
                            "from_address": from_addr,
                            "to_owner": canonical_ex,
                            "from_owner": "",  # rippled RPC does not resolve owners
                            "weight": w,
                            "ripple_corp": ripple_corp,
                        }
                    )

                if MONITOR_OUTFLOWS and from_addr == address and destination:
                    outflows.append(
                        {
                            "timestamp": ts,
                            "xrp": xrp_amt,
                            "exchange": canonical_ex,
                            "to_address": destination,
                            "from_address": from_addr,
                            "to_owner": owner_from_address(destination),
                            "from_owner": canonical_ex,
                            "weight": w,
                            "ripple_corp": False,
                        }
                    )
            time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
        except Exception as e:
            logging.error("rippled RPC fetch failed for %s via %s: %s", address, endpoint, e)
            time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)

    uniq = {}
    for f in flows:
        key = (f.get("from_address"), f.get("to_address"), f.get("timestamp"), f.get("xrp"))
        if key not in uniq:
            uniq[key] = f

    uniq_out = {}
    for f in outflows:
        key = (f.get("from_address"), f.get("to_address"), f.get("timestamp"), f.get("xrp"))
        if key not in uniq_out:
            uniq_out[key] = f
    return list(uniq.values()), list(uniq_out.values())


def prefer_cached_when_empty(flows: List[Dict]) -> List[Dict]:
    """Return cached inflows when a polling run produced no data."""

    if flows:
        return flows

    cached = fetch_cached_flows()
    if cached:
        logging.info("Using cached XRPL inflows because fresh poll returned no data")
        return cached

    return []


def prefer_cached_outflows(outflows: List[Dict]) -> List[Dict]:
    """Return cached outflows when a polling run produced no data."""

    if outflows:
        return outflows

    cached = fetch_cached_outflows()
    if cached:
        logging.info("Using cached XRPL outflows because fresh poll returned no data")
        return cached

    return []


def fetch_transactions_ripple_data() -> Tuple[List[Dict], List[Dict]]:
    """Fetch inflows/outflows to curated exchange addresses using Ripple Data (free)."""

    global ripple_data_failure_streak

    flows: List[Dict] = []
    outflows: List[Dict] = []
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
        cached_in = fetch_cached_flows()
        cached_out = fetch_cached_outflows()
        if cached_in or cached_out:
            return cached_in, cached_out

        logging.info("Cached XRPL inflows unavailable; falling back to rippled RPC during Ripple Data cooldown")
        return fetch_transactions_rippled_rpc()

    for address in monitored_addresses():
        if address in ripple_data_blocked_addresses:
            logging.debug("Skipping Ripple Data address %s previously blocked after 403", address)
            continue
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
                ripple_data_blocked_addresses.add(address)
                logging.warning(
                    "Ripple Data API returned 403 for %s; removing from inflow polling set for this run",
                    address,
                )
                time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
                continue
            if not resp.ok:
                logging.warning(f"Ripple Data API error {resp.status_code} for {address}")
                _note_ripple_data_error(resp.status_code)
                if ripple_data_cooldown_until and time.time() < ripple_data_cooldown_until:
                    break
                time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
                continue

            data = resp.json()
            ripple_data_failure_streak = 0
            ripple_data_cooldown_until = 0
            for entry in data.get("transactions", []):
                tx = entry.get("tx", {})
                destination = tx.get("Destination", "")
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

                if destination == address:
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

                if MONITOR_OUTFLOWS and from_addr == address and destination:
                    outflows.append(
                        {
                            "timestamp": timestamp,
                            "xrp": xrp_amt,
                            "exchange": canonical_ex,
                            "to_address": destination,
                            "from_address": from_addr,
                            "to_owner": owner_from_address(destination),
                            "from_owner": canonical_ex,
                            "weight": w,
                            "ripple_corp": False,
                        }
                    )
            time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)
        except Exception as e:
            logging.error(f"Ripple Data fetch failed for {address}: {e}")
            _note_ripple_data_error()
            if ripple_data_cooldown_until and time.time() < ripple_data_cooldown_until:
                break
            time.sleep(RIPPLE_DATA_REQUEST_INTERVAL)

    if not flows and (not outflows or not MONITOR_OUTFLOWS):
        logging.info("Ripple Data returned no flows; trying rippled RPC fallback")
        return fetch_transactions_rippled_rpc()

    # De-duplicate by transaction hash + destination to avoid duplicates across pages
    uniq = {}
    for f in flows:
        key = (f.get("from_address"), f.get("to_address"), f.get("timestamp"), f.get("xrp"))
        if key not in uniq:
            uniq[key] = f

    uniq_out = {}
    for f in outflows:
        key = (f.get("from_address"), f.get("to_address"), f.get("timestamp"), f.get("xrp"))
        if key not in uniq_out:
            uniq_out[key] = f
    return list(uniq.values()), list(uniq_out.values())


def fetch_transactions(provider: Optional[str] = None) -> Tuple[List[Dict], List[Dict]]:
    resolved_provider = provider or resolve_provider()

    if resolved_provider == "ripple_data":
        return fetch_transactions_ripple_data()

    if resolved_provider == "rippled":
        return fetch_transactions_rippled_rpc()

    if resolved_provider != "whale_alert":
        logging.warning(
            "Unknown XRPL inflow provider %s; defaulting to Whale Alert", resolved_provider
        )

    inflows, outflows = fetch_transactions_whale_alert()
    if inflows or outflows:
        return inflows, outflows

    logging.info("Whale Alert unavailable or empty; falling back to Ripple Data API")
    return fetch_transactions_ripple_data()


def build_flows() -> Tuple[List[Dict], List[Dict]]:
    resolved_provider = resolve_provider()

    if resolved_provider == "ripple_data":
        inflows, outflows = fetch_transactions_ripple_data()
        return prefer_cached_when_empty(inflows), prefer_cached_outflows(outflows)

    txs, whale_outflows = fetch_transactions(resolved_provider)
    flows = []
    outflows: List[Dict] = []

    for t in txs:
        if not isinstance(t, dict):
            continue
        to_obj = t.get("to") or {}
        from_obj = t.get("from") or {}

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

        if to_obj.get("owner_type") == "exchange":
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

        if MONITOR_OUTFLOWS and from_obj.get("owner_type") == "exchange":
            from_exchange = owner_from_address(from_addr) or from_owner or "Unknown"
            outflows.append(
                {
                    "timestamp": ts,
                    "xrp": amt,
                    "exchange": from_exchange,
                    "to_address": to_addr,
                    "from_address": from_addr,
                    "to_owner": to_owner,
                    "from_owner": from_owner,
                    "weight": exchange_weight(from_exchange),
                    "ripple_corp": False,
                }
            )

    return prefer_cached_when_empty(flows), prefer_cached_outflows(outflows or whale_outflows)


def push(inflows: List[Dict], outflows: List[Dict]):
    try:
        rdb.set("xrpl:latest_inflows", json.dumps(inflows))
        rdb.set(
            "xrpl:latest_inflows_meta",
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "provider": resolve_provider(),
                    "count": len(inflows),
                    "run_seconds": RUN,
                }
            ),
        )
        logging.info(f"XRPL inflows snapshot pushed: {len(inflows)} txs")
        append_history(inflows)

        if MONITOR_OUTFLOWS:
            rdb.set("xrpl:latest_outflows", json.dumps(outflows))
            rdb.set(
                "xrpl:latest_outflows_meta",
                json.dumps(
                    {
                        "updated_at": datetime.now(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "provider": resolve_provider(),
                        "count": len(outflows),
                        "run_seconds": RUN,
                    }
                ),
            )
            append_outflow_history(outflows)
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


def append_outflow_history(outflows: List[Dict], max_len: int = 240):
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "total_xrp": float(sum(f.get("xrp", 0.0) for f in outflows)),
        "weighted_xrp": float(sum(f.get("xrp", 0.0) * f.get("weight", 1.0) for f in outflows)),
    }

    try:
        raw = rdb.get("xrpl:outflow_history")
        history = json.loads(raw) if raw else []
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    history.append(snapshot)
    history = history[-max_len:]

    try:
        rdb.set("xrpl:outflow_history", json.dumps(history))
    except Exception as e:
        logging.error(f"XRPL outflow history write failed: {e}")


def sample_flows() -> Tuple[List[Dict], List[Dict]]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    inflows = [
        {
            "timestamp": now,
            "xrp": 2_000_000,
            "exchange": "binance",
            "to_address": "sample_binance", 
            "from_address": "sample_wallet_a",
            "to_owner": "Binance",
            "from_owner": "",
            "weight": exchange_weight("binance"),
            "ripple_corp": False,
            "txid": "sample-binance",
        },
        {
            "timestamp": now,
            "xrp": 750_000,
            "exchange": "kraken",
            "to_address": "sample_kraken",
            "from_address": "sample_wallet_b",
            "to_owner": "Kraken",
            "from_owner": "",
            "weight": exchange_weight("kraken"),
            "ripple_corp": False,
            "txid": "sample-kraken",
        },
    ]

    return inflows, []


def loop(use_sample: bool = False):
    while True:
        try:
            flows, outflows = sample_flows() if use_sample else build_flows()
            push(flows, outflows)
        except Exception as e:
            logging.error(f"XRPL inflow loop error: {e}")
        time.sleep(RUN)


def main():
    parser = argparse.ArgumentParser(description="XRPL inflow monitor")
    parser.add_argument("--once", action="store_true", help="Run a single iteration")
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use built-in sample inflow events (no network calls)",
    )
    args = parser.parse_args()

    if args.once:
        flows, outflows = sample_flows() if args.sample else build_flows()
        push(flows, outflows)
    else:
        loop(use_sample=args.sample)


if __name__ == "__main__":
    main()
