import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from redis_client import rdb

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def cache_set_json(key: str, obj: Any) -> None:
    try:
        rdb.set(key, json.dumps(obj))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to set Redis key %s: %s", key, exc)


def cache_get_json(key: str) -> Any:
    try:
        raw = rdb.get(key)
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read Redis key %s: %s", key, exc)
    return None


def safe_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    headers: Optional[Dict[str, str]] = None,
) -> Any:
    try:
        resp = requests.get(url, params=params, timeout=timeout, headers=headers)
        if not resp.ok:
            logger.warning("GET %s failed with status %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("GET %s raised exception: %s", url, exc)
        return None


def compute_sentiment_components(articles: List[Dict[str, Any]], mode: str) -> Tuple[float, float, float]:
    """Compute (instant_sentiment_scalar, bull, bear) from per-article scores."""
    usable: List[Dict[str, Any]] = []
    for a in articles:
        scalar = a.get("scalar")
        weight = a.get("weight", 0.0)
        if scalar is None or not isinstance(weight, (int, float)):
            continue
        if mode == "Institutional Only" and weight < 0.6:
            continue
        usable.append(a)

    if not usable:
        return 0.0, 0.0, 0.0

    weights = [u.get("weight", 0.0) or 0.0 for u in usable]
    pos_arr = [u.get("pos", 0.0) or 0.0 for u in usable]
    neg_arr = [u.get("neg", 0.0) or 0.0 for u in usable]
    scalar_arr = [u.get("scalar", 0.0) or 0.0 for u in usable]

    weight_sum = sum(weights)
    if weight_sum <= 0:
        return 0.0, 0.0, 0.0

    bull = sum(p * w for p, w in zip(pos_arr, weights)) / weight_sum
    bear = sum(n * w for n, w in zip(neg_arr, weights)) / weight_sum
    inst = sum(s * w for s, w in zip(scalar_arr, weights)) / weight_sum
    return float(inst), float(bull), float(bear)


def _parse_inflow_timestamp(value: Any) -> Optional[datetime]:
    """Return a timezone-aware datetime for inflow timestamps (epoch or ISO)."""

    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:  # noqa: BLE001
            return None

    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None

    return None


def _infer_latest_inflow_ts(inflows: Any) -> Optional[datetime]:
    """Return the newest timestamp from inflow entries (if available)."""

    if not isinstance(inflows, list):
        return None

    parsed: List[datetime] = []
    for entry in inflows:
        if not isinstance(entry, dict):
            continue
        ts = _parse_inflow_timestamp(entry.get("timestamp"))
        if ts:
            parsed.append(ts)

    if not parsed:
        return None

    return max(parsed)


def describe_data_health(live: Dict[str, Any], news_payload: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (issues, redis_notes) to surface data freshness and missing feeds."""
    issues: List[str] = []
    redis_notes: List[str] = []

    if not live.get("price"):
        issues.append("XRP price feed unavailable (CoinGecko)")
    if live.get("oi_usd") is None:
        issues.append("Open interest missing")
    if not live.get("funding_hist_pct"):
        issues.append("Funding history unavailable")

    sentiment_count = news_payload.get("count", 0)
    if sentiment_count == 0:
        issues.append("News sentiment missing")
        redis_notes.append("Redis key `news:sentiment` not found or empty.")
        if cache_get_json("news:sentiment_ema") is None:
            redis_notes.append("Redis key `news:sentiment_ema` missing (sentiment EMA fallback unavailable).")

    xrpl_meta = cache_get_json("xrpl:latest_inflows_meta")
    xrpl_inflows = cache_get_json("xrpl:latest_inflows")

    xrpl_ts = None
    xrpl_run_seconds = 600
    if isinstance(xrpl_meta, dict):
        xrpl_run_seconds = int(xrpl_meta.get("run_seconds") or xrpl_run_seconds)
        xrpl_ts = _parse_inflow_timestamp(xrpl_meta.get("updated_at"))

    if xrpl_ts is None:
        xrpl_ts = _infer_latest_inflow_ts(xrpl_inflows)

    xrpl_fresh = False
    if xrpl_ts:
        grace = max(xrpl_run_seconds * 3, 900)  # tolerate temporary outages
        xrpl_fresh = datetime.now(timezone.utc) - xrpl_ts <= timedelta(seconds=grace)

    if xrpl_inflows is None or not xrpl_fresh:
        redis_notes.append("Redis key `xrpl:latest_inflows` empty or stale.")

    if cache_get_json("xrpl:latest_outflows") is None:
        redis_notes.append("Redis key `xrpl:latest_outflows` empty; exchange withdrawal tracking unavailable.")

    if cache_get_json("xrpl:inflow_history") is None:
        redis_notes.append("Redis key `xrpl:inflow_history` missing; inflow history charts may be empty.")

    if cache_get_json("xrpl:outflow_history") is None:
        redis_notes.append("Redis key `xrpl:outflow_history` missing; outflow history charts may be empty.")

    if live.get("price") is None and cache_get_json("cache:price:xrp_usd"):
        redis_notes.append("Using cached price from `cache:price:xrp_usd`.")

    missing_ratio_emas = [
        name for name in ("xrp_btc", "xrp_eth") if cache_get_json(f"ratio_ema:{name}") is None
    ]
    if missing_ratio_emas:
        formatted = ", ".join(f"`ratio_ema:{name}`" for name in missing_ratio_emas)
        redis_notes.append(f"Ratio EMA cache missing ({formatted}); rebuilding baselines from live data.")

    return issues, redis_notes
