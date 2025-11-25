# main.py — XRP REVERSAL & BREAKOUT ENGINE v9.3
# XRP-only; XRPL inflows (weighted + Ripple OTC); Binance netflow;
# XRP/BTC & XRP/ETH flippening; HF FinBERT sentiment EMA; SMA backtest; data health.

import os
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app_utils import resolve_sentiment_conflicts, safe_get
from redis_client import rdb
from signals import (
    SIGNAL_COMPONENTS,
    log_score_components,
    posterior_conviction_probability,
)
from targets import build_target_profile, compute_atr

# =========================
# Config / constants
# =========================

def normalize_env_value(name: str) -> str:
    """Return a trimmed environment variable (blank string if missing)."""

    raw = (os.getenv(name) or "").strip()

    # Railway variables sometimes get pasted with surrounding quotes.
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        raw = raw[1:-1].strip()

    return raw


def redact_secret(value: str, keep: int = 4) -> str:
    """Return a partially redacted secret for safe display."""

    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}***{value[-keep:]}"


st.set_page_config(page_title="XRP Engine v9.3", layout="wide", initial_sidebar_state="collapsed")
st.title("XRP REVERSAL & BREAKOUT ENGINE v9.3")
st.markdown(
    "<p style='text-align: center; color: #00ff88; font-size:18px;'>"
    "XRPL Inflows (Weighted) • Ripple OTC → Exchanges • Binance Netflow • "
    "XRP/BTC & XRP/ETH Flippening • News Sentiment EMA • SMA Backtest"
    "</p>",
    unsafe_allow_html=True,
)

DEFAULT_META_REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
if "refresh_enabled" not in st.session_state:
    st.session_state["refresh_enabled"] = True
if "refresh_seconds" not in st.session_state:
    st.session_state["refresh_seconds"] = DEFAULT_META_REFRESH_SECONDS

with st.sidebar:
    st.header("Live Controls")
    st.session_state["refresh_enabled"] = st.checkbox(
        "Enable auto-refresh",
        value=st.session_state["refresh_enabled"],
        help="Disable if you want to freeze the dashboard while investigating a scenario.",
    )
    st.session_state["refresh_seconds"] = st.slider(
        "Refresh interval (seconds)",
        min_value=15,
        max_value=180,
        value=st.session_state["refresh_seconds"],
        step=5,
        help="Lower values increase API usage and rate-limit risk.",
    )
    if st.button("Refresh now", type="primary"):
        st.experimental_rerun()

    st.subheader("Config & Credentials")
    api_key = normalize_env_value("BINANCE_API_KEY")
    api_secret = normalize_env_value("BINANCE_API_SECRET")
    st.caption("Binance keys are read from environment variables; secrets are redacted for safety.")
    st.code(
        f"BINANCE_API_KEY={redact_secret(api_key)}\nBINANCE_API_SECRET={redact_secret(api_secret)}",
        language="bash",
    )
    if api_key and api_secret:
        st.success("Binance credentials loaded from environment variables.")
    else:
        st.info("Set BINANCE_API_KEY and BINANCE_API_SECRET to enable live netflow polling.")

refresh_seconds = st.session_state["refresh_seconds"]
if st.session_state["refresh_enabled"]:
    st.markdown(
        f'<meta http-equiv="refresh" content="{refresh_seconds}">',
        unsafe_allow_html=True,
    )
st.caption(
    f"Dashboard auto-refreshes every {refresh_seconds} seconds; lower values increase API usage."
)

st.markdown(
    """
    <style>
    .metric-card {
        padding: 14px 16px;
        border-radius: 12px;
        border: 1px solid rgba(255, 255, 255, 0.08);
        background: linear-gradient(135deg, rgba(0, 255, 136, 0.12), rgba(0, 255, 136, 0.02));
        margin-bottom: 12px;
    }
    .metric-title {color: #d0d5dd; font-size: 13px; margin-bottom: 4px;}
    .metric-value {color: #f8fafc; font-size: 24px; font-weight: 700;}
    .metric-sub {color: #9ca3af; font-size: 12px; margin-top: 2px;}
    .section-label {color: #9ca3af; font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase;}
    .health-pill {padding: 8px 12px; border-radius: 999px; font-size: 12px; display: inline-block; margin-right: 8px;}
    .health-ok {background: rgba(0, 255, 136, 0.16); color: #a7f3d0; border: 1px solid rgba(34, 197, 94, 0.4);} 
    .health-warn {background: rgba(234, 179, 8, 0.16); color: #fcd34d; border: 1px solid rgba(234, 179, 8, 0.4);} 
    .health-err {background: rgba(239, 68, 68, 0.16); color: #fecdd3; border: 1px solid rgba(239, 68, 68, 0.4);} 
    </style>
    """,
    unsafe_allow_html=True,
)

REQUEST_TIMEOUT = 10
SENTIMENT_EMA_ALPHA = float(os.getenv("SENTIMENT_EMA_ALPHA", "0.3"))
RATIO_EMA_ALPHA = float(os.getenv("RATIO_EMA_ALPHA", "0.1"))

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def metric_card(title: str, value: str, sub: str = "", accent: str = "#00ff88") -> None:
    """Render a stylized metric card."""

    st.markdown(
        f"""
        <div class="metric-card" style="border-left: 4px solid {accent};">
            <div class="metric-title">{title}</div>
            <div class="metric-value">{value}</div>
            <div class="metric-sub">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_label(text: str) -> None:
    st.markdown(f"<div class='section-label'>{text}</div>", unsafe_allow_html=True)


def render_health_strip(
    issues: List[str],
    redis_notes: List[str],
    binance_notes: List[str],
    latency_notes: Optional[List[str]] = None,
    freshness_notes: Optional[List[str]] = None,
) -> None:
    """Compact health strip showing data and API state."""

    status = "OK" if not issues else "WARN"
    pill_class = "health-ok" if status == "OK" else "health-warn"
    with st.container():
        cols = st.columns([2, 3, 3, 2])
        cols[0].markdown(
            f"<span class='health-pill {pill_class}'>System {status}</span>",
            unsafe_allow_html=True,
        )
        if issues:
            cols[1].markdown(
                "**Data Issues**\\n" + "\\n".join(f"- {i}" for i in issues)
            )
        else:
            cols[1].markdown("**Data Issues**\\n- None detected")

        combined_notes = (redis_notes or []) + (binance_notes or []) + (freshness_notes or [])
        if combined_notes:
            cols[2].markdown(
                "**Cache / API Notes**\\n" + "\\n".join(f"- {n}" for n in combined_notes)
            )
        else:
            cols[2].markdown("**Cache / API Notes**\\n- All clear")

        if latency_notes:
            cols[3].markdown(
                "**Latency**\\n" + "\\n".join(f"- {n}" for n in latency_notes)
            )
        else:
            cols[3].markdown("**Latency**\\n- n/a")


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


def empty_live_payload() -> Dict[str, Any]:
    """Base structure for live metrics with safe defaults."""

    return {
        "price": None,
        "funding_now_pct": 0.0,
        "funding_hist_pct": [],
        "oi_usd": None,
        "long_short_ratio": 1.0,
        "binance_netflow_24h": None,
        "binance_notes": [],
        "xrp_btc": None,
        "xrp_eth": None,
        "xrpl_raw_inflow": 0.0,
        "xrpl_weighted_inflow": 0.0,
        "xrpl_ripple_otc": 0.0,
        "latency_notes": [],
        "freshness_notes": [],
    }


def cached_coingecko_simple_price(
    ids: str, vs_currencies: str = "usd", fresh_ttl: int = 60, stale_ttl: int = 300
) -> Optional[Dict[str, Any]]:
    """Fetch CoinGecko simple price with Redis-backed throttling.

    - Returns cached data if it is newer than ``fresh_ttl`` seconds to avoid
      hammering the API and triggering HTTP 429 responses.
    - On request failure, returns a recent cached payload when available (up to
      ``stale_ttl`` seconds old).
    """

    cache_key = f"cache:coingecko:simple_price:{ids}:{vs_currencies}"
    now = time.time()

    cached = cache_get_json(cache_key) or {}
    cached_ts = float(cached.get("ts", 0.0))
    cached_payload = cached.get("payload") if isinstance(cached, dict) else None

    if cached_payload and now - cached_ts <= fresh_ttl:
        return cached_payload

    payload = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": ids, "vs_currencies": vs_currencies},
        timeout=REQUEST_TIMEOUT,
    )

    if payload:
        cache_set_json(cache_key, {"ts": now, "payload": payload})
        return payload

    if cached_payload and now - cached_ts <= stale_ttl:
        return cached_payload

    return None


def cached_crypto_compare_price(symbol: str = "XRP", currency: str = "USD", ttl: int = 300) -> Optional[float]:
    """Try CryptoCompare's price endpoint as a resilient fallback."""

    cache_key = f"cache:cryptocompare:price:{symbol}:{currency}"
    now = time.time()
    cached = cache_get_json(cache_key)
    if isinstance(cached, dict):
        ts = float(cached.get("ts", 0.0))
        if now - ts <= ttl:
            try:
                return float(cached.get("price"))
            except Exception:
                pass

    params = {"fsym": symbol, "tsyms": currency}
    api_key = normalize_env_value("CRYPTOCOMPARE_API_KEY")
    if api_key:
        params["api_key"] = api_key

    resp = safe_get("https://min-api.cryptocompare.com/data/price", params=params, timeout=REQUEST_TIMEOUT)
    if resp and currency in resp:
        try:
            price = float(resp[currency])
            cache_set_json(cache_key, {"ts": now, "price": price})
            return price
        except Exception:
            return None

    return None


def cache_with_expiry(key: str, value: Any, ttl_seconds: int) -> None:
    cache_set_json(key, {"value": value, "ts": time.time(), "ttl": ttl_seconds})


def read_cached_value(key: str, ttl_seconds: int) -> Optional[Any]:
    cached = cache_get_json(key)
    if not isinstance(cached, dict):
        return None
    ts = float(cached.get("ts", 0.0))
    if time.time() - ts <= ttl_seconds:
        return cached.get("value")
    return None


def cache_age_seconds(key: str, ts_field: str = "ts") -> Optional[float]:
    cached = cache_get_json(key)
    if isinstance(cached, dict) and cached.get(ts_field) is not None:
        try:
            return float(time.time() - float(cached.get(ts_field, 0.0)))
        except Exception:
            return None
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


def _capped_positive_score(value: float, scale: float, cap: float) -> float:
    """Return a smooth, capped score for positive signals (e.g., z-scores).

    The tanh keeps extreme values from dominating while still rewarding
    outsized moves. Negative inputs return zero because they are not helpful
    to the bullish thesis.
    """

    if value is None:
        return 0.0
    scaled = np.tanh(float(value)) * scale
    return float(min(cap, max(0.0, scaled)))


def _linear_window_score(value: Optional[float], start: float, end: float, max_score: float) -> float:
    """Map ``value`` onto a linear window between ``start`` and ``end``.

    ``start`` receives the full ``max_score`` while ``end`` and beyond drop to
    zero. Values between are interpolated. If ``start`` < ``end`` the score
    decays as value increases; otherwise it increases.
    """

    if value is None:
        return 0.0
    if start == end:
        return float(max_score if value <= start else 0.0)

    val = float(value)
    lower, upper = (start, end) if start < end else (end, start)
    if val <= lower:
        return float(max_score if start < end else 0.0)
    if val >= upper:
        return float(0.0 if start < end else max_score)

    ratio = (val - lower) / (upper - lower)
    score = max_score * (1.0 - ratio if start < end else ratio)
    return float(max(0.0, min(max_score, score)))


def append_metric_history(name: str, value: Optional[float], max_age_days: int = 7, max_len: int = 480) -> None:
    """Persist metric history to Redis for delta calculations."""

    if value is None:
        return

    now = time.time()
    key = f"history:metric:{name}"
    hist = cache_get_json(key) or []
    if not isinstance(hist, list):
        hist = []

    fresh = []
    for item in hist:
        try:
            ts = float(item.get("ts", 0.0))
            if now - ts <= max_age_days * 86400:
                fresh.append({"ts": ts, "value": float(item.get("value", 0.0))})
        except Exception:
            continue

    fresh.append({"ts": now, "value": float(value)})
    cache_set_json(key, fresh[-max_len:])


def metric_delta(name: str, horizon_hours: int) -> Optional[float]:
    """Return percentage change over ``horizon_hours`` when enough history exists."""

    key = f"history:metric:{name}"
    hist = cache_get_json(key) or []
    if not isinstance(hist, list) or len(hist) < 2:
        return None

    now = time.time()
    relevant = [item for item in hist if now - float(item.get("ts", 0.0)) <= horizon_hours * 3600]
    if len(relevant) < 2:
        return None

    start = float(relevant[0].get("value", 0.0))
    end = float(relevant[-1].get("value", 0.0))
    if abs(start) <= 1e-9:
        return None
    return (end - start) / abs(start) * 100.0


def build_proxy_composite_series(
    df: pd.DataFrame,
    funding_hist: List[float],
    sentiment_ema: float,
    inflow_history: Optional[List[Dict[str, Any]]],
    netflow_hist: Optional[List[Dict[str, Any]]],
) -> Optional[pd.Series]:
    """Generate a proxy composite curve to gate backtests using available history.

    We blend volume percentile, short-term price momentum, funding z-scores,
    weighted inflows, and cached Binance netflow history when available.
    """

    if df is None or df.empty:
        return None

    base = df.sort_values("date").copy()
    vol_pct = base["volume"].rank(pct=True).fillna(0.0)
    momentum = (base["close"] / base["close"].rolling(5).mean() - 1.0).fillna(0.0)
    momentum_score = np.tanh(momentum * 8)  # emphasize fast ramps

    funding_series = pd.Series(funding_hist[-len(base) :] if funding_hist else []).reindex(
        base.index, method="pad", fill_value=0.0
    )
    if funding_series.std() <= 1e-8:
        funding_z = pd.Series(0.0, index=base.index)
    else:
        funding_z = (funding_series - funding_series.mean()) / funding_series.std()
    funding_score = np.tanh(funding_z)  # -1..1

    inflow_series: pd.Series
    if inflow_history:
        inflow_df = pd.DataFrame(inflow_history).tail(len(base))
        inflow_series = inflow_df.get("weighted_xrp", pd.Series(dtype=float)).astype(float)
        inflow_series = inflow_series.reindex(range(len(base)), method="pad", fill_value=0.0)
    else:
        inflow_series = pd.Series(0.0, index=base.index)

    inflow_norm = np.tanh((inflow_series - inflow_series.mean()) / (inflow_series.std() + 1e-6))

    netflow_series: pd.Series
    if netflow_hist:
        netflow_df = pd.DataFrame(netflow_hist).tail(len(base))
        netflow_series = netflow_df.get("value", pd.Series(dtype=float)).astype(float)
        netflow_series = netflow_series.reindex(range(len(base)), method="pad", fill_value=0.0)
    else:
        netflow_series = pd.Series(0.0, index=base.index)

    netflow_score = np.tanh(netflow_series / (abs(netflow_series).max() + 1e-6))

    sentiment_boost = float(np.tanh(sentiment_ema * 2.5)) if sentiment_ema else 0.0

    composite = (
        vol_pct * 35
        + (momentum_score + 1) * 15  # shift to 0..2 range, then scale
        + (funding_score + 1) * 15
        + (inflow_norm + 1) * 15
        + (netflow_score + 1) * 10
        + sentiment_boost * 10
    )

    return composite.clip(lower=0.0, upper=100.0)


def xrpl_flow_breakdown() -> Dict[str, Any]:
    """Return latest XRPL flow decomposition plus 7d z-scores."""

    inflows = cache_get_json("xrpl:latest_inflows") or []
    inflow_history = cache_get_json("xrpl:inflow_history") or []

    exchange_raw = sum(float(f.get("xrp", 0.0)) for f in inflows if not f.get("ripple_corp"))
    otc_raw = sum(float(f.get("xrp", 0.0)) for f in inflows if f.get("ripple_corp"))

    def zscore_from_history(field: str) -> float:
        if not isinstance(inflow_history, list) or len(inflow_history) < 3:
            return 0.0
        series = pd.Series([float(x.get(field, 0.0) or 0.0) for x in inflow_history[-42:]])
        if series.std() <= 1e-6:
            return 0.0
        return float((series.iloc[-1] - series.mean()) / series.std())

    return {
        "exchange_raw": exchange_raw,
        "otc_raw": otc_raw,
        "total_z": zscore_from_history("weighted_xrp"),
        "exchange_z": zscore_from_history("exchange_xrp"),
        "otc_z": zscore_from_history("ripple_corp_xrp"),
        "history": inflow_history,
    }


def detect_regimes(
    price_change_24h: Optional[float],
    funding_z: float,
    btc_dev: float,
    eth_dev: float,
    oi_delta_24h: Optional[float],
    volume_pct: float,
    ls_ratio: Optional[float],
) -> List[Tuple[str, str, str]]:
    """Return labeled regime badges (label, description, severity)."""

    regimes: List[Tuple[str, str, str]] = []

    if price_change_24h is not None and price_change_24h < -2.0 and funding_z > 0.8:
        regimes.append(
            (
                "Funding/Price Divergence",
                "Price down while funding positive — watch for mean reversion or squeezes",
                "warn",
            )
        )

    if abs(btc_dev) >= 2.0 or abs(eth_dev) >= 2.0:
        regimes.append(
            (
                "Ratio Detachment",
                f"XRP/BTC {btc_dev:+.1f}% / XRP/ETH {eth_dev:+.1f}% away from EMAs",
                "warn" if max(abs(btc_dev), abs(eth_dev)) < 5 else "alert",
            )
        )

    if oi_delta_24h is not None and oi_delta_24h <= -5.0:
        regimes.append(
            (
                "Liquidity Drain",
                f"Open interest off {oi_delta_24h:.1f}% in 24h — lighter liquidity",
                "alert",
            )
        )

    if volume_pct >= 80 and ls_ratio is not None and ls_ratio < 0.95:
        regimes.append(
            (
                "Short Squeeze Watch",
                "Heavy volume with short skew could force covering on upside breaks",
                "ok",
            )
        )

    return regimes


def derive_price_window(
    price: Optional[float], history: pd.DataFrame
) -> Tuple[float, float, str]:
    """Return a dynamic price scoring window anchored to recent market ranges.

    The window adapts to the last 90 days of closes and leaves headroom for
    breakouts beyond prior highs so scores keep responding as market structure
    shifts. Falls back to the original static band if no history is available.
    """

    default_start, default_end = 2.45, 3.0

    if history is None or history.empty:
        return default_start, default_end, "Price window using static $2.45–$3.00 band (no history)."

    closes = pd.to_numeric(history.get("close"), errors="coerce").dropna()
    if closes.empty:
        return default_start, default_end, "Price window using static $2.45–$3.00 band (no clean closes)."

    low = float(closes.min())
    high = float(closes.max())
    span = max(high - low, 0.01)
    q25 = float(closes.quantile(0.25))
    q65 = float(closes.quantile(0.65))

    # Give full credit when price trades near the lower quartile of the recent
    # range while allowing the upper bound to expand with volatility and new highs.
    start = min(max(q25, low + 0.1 * span), q65)
    headroom = max(span * 0.3, (price or high) * 0.1)
    end = max(high + headroom, (price or high) * 1.15)

    if end <= start:
        end = start + max(abs(start) * 0.2, 0.5)

    note = f"Dynamic price window ${start:.2f}–${end:.2f} from 90d range (low ${low:.2f}, high ${high:.2f})."
    return float(start), float(end), note


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

    if cache_get_json("xrpl:inflow_history") is None:
        redis_notes.append("Redis key `xrpl:inflow_history` missing; inflow history charts may be empty.")

    if live.get("price") is None and cache_get_json("cache:price:xrp_usd"):
        redis_notes.append("Using cached price from `cache:price:xrp_usd`.")

    missing_ratio_emas = [
        name for name in ("xrp_btc", "xrp_eth") if cache_get_json(f"ratio_ema:{name}") is None
    ]
    if missing_ratio_emas:
        formatted = ", ".join(f"`ratio_ema:{name}`" for name in missing_ratio_emas)
        redis_notes.append(f"Ratio EMA cache missing ({formatted}); rebuilding baselines from live data.")

    return issues, redis_notes


def collect_freshness_notes() -> List[str]:
    """Summaries about cache ages to spot stale datasets quickly."""

    notes: List[str] = []
    price_age = cache_age_seconds("cache:price:xrp_usd")
    if price_age is not None:
        notes.append(f"Cached price age {price_age:.0f}s")

    funding_age = cache_age_seconds("cache:funding_now_pct")
    if funding_age is not None:
        notes.append(f"Funding age {funding_age:.0f}s")

    oi_age = cache_age_seconds("cache:oi_usd")
    if oi_age is not None:
        notes.append(f"Open interest age {oi_age:.0f}s")

    sentiment_age = cache_age_seconds("news:sentiment_ema")
    if sentiment_age is not None:
        notes.append(f"Sentiment EMA age {sentiment_age:.0f}s")

    return notes

# =========================
# Chart data (90d OHLC + volume)
# =========================

@st.cache_data(ttl=600)
def get_chart_data():
    # 1) CoinGecko OHLC + volume
    ohlc = safe_get(
        "https://api.coingecko.com/api/v3/coins/ripple/ohlc",
        {"vs_currency": "usd", "days": "90"},
    )
    vol = safe_get(
        "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
        {"vs_currency": "usd", "days": "90", "interval": "daily"},
    )

    if ohlc and vol and "total_volumes" in vol:
        try:
            ohlc_df = pd.DataFrame(ohlc, columns=["ts", "open", "high", "low", "close"])
            ohlc_df["date"] = pd.to_datetime(ohlc_df["ts"], unit="ms")

            vol_df = pd.DataFrame(vol["total_volumes"], columns=["ts", "volume"])
            vol_df["date"] = pd.to_datetime(vol_df["ts"], unit="ms")

            df = pd.merge(ohlc_df, vol_df[["date", "volume"]], on="date", how="left")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            df = df.sort_values("date")
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception:
            pass

    # 2) Binance fallback
    kl = safe_get(
        "https://api.binance.com/api/v3/klines",
        {"symbol": "XRPUSDT", "interval": "1d", "limit": 90},
    )
    if kl:
        try:
            df = pd.DataFrame(
                kl,
                columns=[
                    "open_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "close_time",
                    "q",
                    "t",
                    "tb",
                    "tbq",
                    "i",
                ],
            )
            df["date"] = pd.to_datetime(df["open_time"], unit="ms")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("date")
            return df[["date", "open", "high", "low", "close", "volume"]]
        except Exception:
            pass

    return pd.DataFrame()


# =========================
# Ratio EMA helpers (flippening baseline)
# =========================

def read_ratio_ema(name: str):
    obj = cache_get_json(f"ratio_ema:{name}")
    if not obj:
        return None
    return float(obj.get("ema", 0.0))


def write_ratio_ema(name: str, value: float):
    cache_set_json(
        f"ratio_ema:{name}",
        {
            "ema": float(value),
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )


def read_cached_binance_netflow():
    obj = cache_get_json("cache:binance_netflow_24h")
    if not isinstance(obj, dict):
        return None, None
    try:
        val = float(obj.get("value", 0.0))
    except Exception:
        return None, obj.get("ts")
    return val, obj.get("ts")


def append_binance_netflow_history(value: float, ts: str, max_len: int = 120) -> None:
    history = cache_get_json("cache:binance_netflow_hist")
    if not isinstance(history, list):
        history = []

    entry_date = ts.split("T")[0]

    # Skip if last entry already represents this date + value
    if history:
        last = history[-1]
        if (
            last.get("date") == entry_date
            and abs(float(last.get("value", 0.0)) - float(value)) < 1e-9
        ):
            return

    history.append({"date": entry_date, "value": float(value), "ts": ts})
    history = history[-max_len:]
    cache_set_json("cache:binance_netflow_hist", history)


def write_cached_binance_netflow(value: float):
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cache_set_json("cache:binance_netflow_24h", {"value": float(value), "ts": ts})
    append_binance_netflow_history(value, ts)


# =========================
# Live data fetch
# =========================

def fetch_live():
    """Collect all live signals needed by the dashboard.

    Features pulled:
    - Price (CoinGecko with Redis fallback)
    - Cross-asset ratios (XRP/BTC, XRP/ETH)
    - Binance funding (current + history) and open interest
    - Binance long/short ratio (5m window)
    - Binance signed netflows (XRP) using API keys when provided
    - XRPL inflows (raw/weighted) and Ripple OTC flows from Redis
    """
    result = empty_live_payload()
    latency_notes = result["latency_notes"]
    freshness_notes = result["freshness_notes"]

    # Price with Redis fallback and throttled CoinGecko usage, then Binance/CryptoCompare
    start_ts = time.perf_counter()
    price_resp = cached_coingecko_simple_price("ripple")
    latency_notes.append(f"CoinGecko price {time.perf_counter() - start_ts:.2f}s")
    if price_resp and "ripple" in price_resp:
        try:
            px = float(price_resp["ripple"]["usd"])
            result["price"] = px
            cache_set_json("cache:price:xrp_usd", {"price": px, "ts": time.time()})
        except Exception:
            pass

    if result["price"] is None:
        ticker = safe_get(
            "https://api.binance.com/api/v3/ticker/price",
            {"symbol": "XRPUSDT"},
            timeout=REQUEST_TIMEOUT,
        )
        if ticker and "price" in ticker:
            try:
                px = float(ticker["price"])
                result["price"] = px
                cache_set_json("cache:price:xrp_usd", {"price": px, "ts": time.time()})
            except Exception:
                pass

    if result["price"] is None:
        cc_price = cached_crypto_compare_price()
        if cc_price is not None:
            result["price"] = cc_price
            cache_set_json("cache:price:xrp_usd", {"price": cc_price, "ts": time.time()})

    if result["price"] is None:
        cached = cache_get_json("cache:price:xrp_usd")
        if cached:
            result["price"] = float(cached.get("price", 0.0))
            age = cache_age_seconds("cache:price:xrp_usd")
            if age is not None:
                freshness_notes.append(f"Price cached age {age:.0f}s")

    # XRP/BTC, XRP/ETH ratios and ratio EMAs (throttled)
    ratio_resp = cached_coingecko_simple_price("ripple,bitcoin,ethereum")
    px_xrp = result["price"] or 0.0
    if ratio_resp:
        try:
            xrp = ratio_resp.get("ripple", {})
            btc = ratio_resp.get("bitcoin", {})
            eth = ratio_resp.get("ethereum", {})
            px_xrp = float(xrp.get("usd", px_xrp) or px_xrp)
            px_btc = float(btc.get("usd", 0.0) or 0.0)
            px_eth = float(eth.get("usd", 0.0) or 0.0)
            if px_btc > 0:
                result["xrp_btc"] = px_xrp / px_btc
            if px_eth > 0:
                result["xrp_eth"] = px_xrp / px_eth
        except Exception:
            pass
    else:
        # Binance public tickers as a fallback (mirrors older working implementation)
        tickers = safe_get(
            "https://api.binance.com/api/v3/ticker/price",
            {"symbols": json.dumps(["XRPUSDT", "BTCUSDT", "ETHUSDT"])},
            timeout=REQUEST_TIMEOUT,
        )
        if isinstance(tickers, list):
            px_map = {t.get("symbol"): t.get("price") for t in tickers}
            try:
                px_xrp = float(px_map.get("XRPUSDT", px_xrp) or px_xrp)
                px_btc = float(px_map.get("BTCUSDT", 0.0) or 0.0)
                px_eth = float(px_map.get("ETHUSDT", 0.0) or 0.0)
                if px_btc > 0:
                    result["xrp_btc"] = px_xrp / px_btc
                if px_eth > 0:
                    result["xrp_eth"] = px_xrp / px_eth
            except Exception:
                pass

    # Funding rate
    start_ts = time.perf_counter()
    fr_json = safe_get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        {"symbol": "XRPUSDT"},
    )
    latency_notes.append(f"Funding {time.perf_counter() - start_ts:.2f}s")
    if fr_json and "lastFundingRate" in fr_json:
        try:
            result["funding_now_pct"] = float(fr_json["lastFundingRate"]) * 100
            cache_with_expiry("cache:funding_now_pct", result["funding_now_pct"], 3600)
        except Exception:
            pass
    else:
        cached_funding_now = read_cached_value("cache:funding_now_pct", 3600)
        if cached_funding_now is not None:
            result["funding_now_pct"] = float(cached_funding_now)
            result["binance_notes"].append("Funding rate sourced from cached Binance response (API unavailable).")

    # Open interest
    start_ts = time.perf_counter()
    oi_json = safe_get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        {"symbol": "XRPUSDT"},
    )
    latency_notes.append(f"Open interest {time.perf_counter() - start_ts:.2f}s")
    if oi_json and "openInterest" in oi_json:
        try:
            oi_contracts = float(oi_json["openInterest"])
            if result["price"]:
                result["oi_usd"] = oi_contracts * result["price"]
                cache_with_expiry("cache:oi_usd", result["oi_usd"], 900)
        except Exception:
            pass
    else:
        cached_oi = read_cached_value("cache:oi_usd", 900)
        if cached_oi is not None:
            result["oi_usd"] = float(cached_oi)
            result["binance_notes"].append("Open interest sourced from cached Binance response (API unavailable).")

    # Funding history
    start_ts = time.perf_counter()
    fh_json = safe_get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": "XRPUSDT", "limit": 200},
    )
    latency_notes.append(f"Funding history {time.perf_counter() - start_ts:.2f}s")
    if fh_json:
        try:
            rates = [float(x["fundingRate"]) * 100 for x in fh_json[-90:]]
            result["funding_hist_pct"] = rates
            cache_with_expiry("cache:funding_hist_pct", rates, 6 * 3600)
        except Exception:
            pass
    else:
        cached_hist = read_cached_value("cache:funding_hist_pct", 6 * 3600)
        if cached_hist:
            result["funding_hist_pct"] = cached_hist
            result["binance_notes"].append(
                "Funding history sourced from cached Binance response (API unavailable)."
            )

    # Long/Short ratio
    ls_json = safe_get(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        {"symbol": "XRPUSDT", "period": "5m", "limit": 1},
    )
    if ls_json and isinstance(ls_json, list) and ls_json:
        try:
            result["long_short_ratio"] = float(ls_json[0]["longShortRatio"])
        except Exception:
            pass

    # Binance signed netflow (XRP)
    api_key = normalize_env_value("BINANCE_API_KEY")
    api_secret = normalize_env_value("BINANCE_API_SECRET")
    if not (api_key and api_secret):
        result["binance_notes"].append(
            "Binance netflow requires BINANCE_API_KEY and BINANCE_API_SECRET; showing cached data when available."
        )

    if api_key and api_secret:
        try:
            base = "https://api.binance.com"

            # Binance requires timestamp alignment; use server time when available
            server_time_resp = safe_get(f"{base}/api/v3/time", None)
            if server_time_resp and "serverTime" in server_time_resp:
                ts_ms = int(server_time_resp["serverTime"])
            else:
                ts_ms = int(time.time() * 1000)

            start = ts_ms - 86_400_000  # 24h
            params = {
                "coin": "XRP",
                "startTime": start,
                "timestamp": ts_ms,
                "recvWindow": 60_000,
            }
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode(), query_string.encode(), hashlib.sha256
            ).hexdigest()
            headers = {"X-MBX-APIKEY": api_key}

            dep_url = f"{base}/sapi/v1/capital/deposit/hisrec?{query_string}&signature={signature}"
            wd_url = f"{base}/sapi/v1/capital/withdraw/history?{query_string}&signature={signature}"

            dep = safe_get(dep_url, None, headers=headers)
            wd = safe_get(wd_url, None, headers=headers)
            dep = dep or []
            wd = wd or []

            dep_amt = sum(float(d.get("amount", 0)) for d in dep if d.get("status") == 1)
            wd_amt = sum(
                float(w.get("amount", 0)) - float(w.get("transactionFee", 0))
                for w in wd
                if w.get("status") == 6
            )
            # positive = more withdrawals (coins leaving Binance)
            netflow_val = wd_amt - dep_amt
            result["binance_netflow_24h"] = netflow_val
            write_cached_binance_netflow(netflow_val)
        except Exception:
            pass
    if result["binance_netflow_24h"] is None:
        cached_val, cached_ts = read_cached_binance_netflow()
        if cached_val is not None:
            result["binance_netflow_24h"] = cached_val
            if cached_ts:
                result["binance_notes"].append(
                    f"Using cached Binance netflow from {cached_ts}."
                )
    # XRPL inflows (from Redis, new v9.3 schema)
    try:
        raw = rdb.get("xrpl:latest_inflows")
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            inflows = json.loads(raw)
        else:
            inflows = []
    except Exception:
        inflows = []

    raw_sum = 0.0
    weighted_sum = 0.0
    ripple_otc = 0.0

    if inflows:
        for f in inflows:
            try:
                amt = float(f.get("xrp", 0.0))
                w = float(f.get("weight", 1.0))
                raw_sum += amt
                weighted_sum += amt * w
                if f.get("ripple_corp"):
                    ripple_otc += amt
            except Exception:
                continue
    else:
        history = cache_get_json("xrpl:inflow_history")
        if isinstance(history, list) and history:
            last = history[-1]
            try:
                raw_sum = float(last.get("total_xrp", 0.0))
                weighted_sum = float(last.get("weighted_xrp", 0.0))
            except Exception:
                raw_sum = weighted_sum = 0.0

    result["xrpl_raw_inflow"] = raw_sum
    result["xrpl_weighted_inflow"] = weighted_sum
    result["xrpl_ripple_otc"] = ripple_otc

    return result


if os.getenv("SKIP_LIVE_FETCH") == "1":
    live = empty_live_payload()
else:
    live = fetch_live()

append_metric_history("oi_usd", live.get("oi_usd"))
append_metric_history("funding_now_pct", live.get("funding_now_pct"))
append_metric_history("binance_netflow_24h", live.get("binance_netflow_24h"))

append_metric_history("oi_usd", live.get("oi_usd"))
append_metric_history("funding_now_pct", live.get("funding_now_pct"))
append_metric_history("binance_netflow_24h", live.get("binance_netflow_24h"))

# =========================
# News sentiment from Redis + EMA
# =========================

import json as _json


def read_sentiment():
    try:
        raw = rdb.get("news:sentiment")
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return _json.loads(raw)
    except Exception:
        pass
    return {"timestamp": None, "score": 0.0, "count": 0, "articles": []}


def read_sentiment_ema():
    obj = cache_get_json("news:sentiment_ema")
    if not obj:
        return None
    return float(obj.get("ema", 0.0))


def write_sentiment_ema(value: float):
    cache_set_json(
        "news:sentiment_ema",
        {"ema": float(value), "timestamp": datetime.now(timezone.utc).isoformat()},
    )


def read_etf_metrics() -> Dict[str, Any]:
    payload = cache_get_json("etf:xrp_metrics") or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "provider": payload.get("provider", "Aggregated"),
        "aum_usd": payload.get("aum_usd"),
        "volume_usd": payload.get("volume_usd"),
        "premium_pct": payload.get("premium_pct"),
        "daily_flow_usd": payload.get("daily_flow_usd"),
        "nav_usd": payload.get("nav_usd"),
        "updated_at": payload.get("updated_at"),
    }


news_payload = read_sentiment()
articles = news_payload.get("articles", [])

# =========================
# Sentiment Mode Toggle + EMA
# =========================

st.subheader("Sentiment Mode")
sent_mode = st.radio(
    "Filter sentiment by source:",
    ["Weighted (All Sources)", "Institutional Only"],
    horizontal=True,
)

prev_ema = read_sentiment_ema()

if news_payload.get("count", 0) <= 0 or not articles:
    inst_sent = 0.0
    bull_intensity = 0.0
    bear_intensity = 0.0
    if prev_ema is None:
        ema_sent = 0.0
    else:
        ema_sent = prev_ema
else:
    inst_sent, bull_intensity, bear_intensity = compute_sentiment_components(
        articles, sent_mode
    )
    if prev_ema is None:
        ema_sent = inst_sent
    else:
        ema_sent = SENTIMENT_EMA_ALPHA * inst_sent + (1.0 - SENTIMENT_EMA_ALPHA) * prev_ema

write_sentiment_ema(ema_sent)

# =========================
# Ratio EMAs (for flippening)
# =========================

btc_ratio = live.get("xrp_btc")
eth_ratio = live.get("xrp_eth")

btc_ema = read_ratio_ema("xrp_btc")
eth_ema = read_ratio_ema("xrp_eth")

if btc_ratio is not None:
    if btc_ema is None:
        btc_ema = btc_ratio
    else:
        btc_ema = RATIO_EMA_ALPHA * btc_ratio + (1.0 - RATIO_EMA_ALPHA) * btc_ema
    write_ratio_ema("xrp_btc", btc_ema)

if eth_ratio is not None:
    if eth_ema is None:
        eth_ema = eth_ratio
    else:
        eth_ema = RATIO_EMA_ALPHA * eth_ratio + (1.0 - RATIO_EMA_ALPHA) * eth_ema
    write_ratio_ema("xrp_eth", eth_ema)

btc_uplift_pct = (
    (btc_ratio / btc_ema - 1.0) * 100.0 if btc_ratio and btc_ema else 0.0
)
eth_uplift_pct = (
    (eth_ratio / eth_ema - 1.0) * 100.0 if eth_ratio and eth_ema else 0.0
)

# Flippening flow score: uplift conditioned on weighted inflows
weighted_inflow_m = (live.get("xrpl_weighted_inflow") or 0.0) / 1e6
avg_positive_uplift = (
    max(btc_uplift_pct, 0.0) + max(eth_uplift_pct, 0.0)
) / 2.0 if (btc_uplift_pct or eth_uplift_pct) else 0.0

if weighted_inflow_m > 10.0:
    flip_score = min(15.0, avg_positive_uplift / 2.0)
else:
    flip_score = 0.0

# =========================
# Scoring engine
# =========================

chart_df = get_chart_data()
price_window_start, price_window_end, price_window_note = derive_price_window(
    live.get("price"), chart_df
)
volume_latest = (
    float(chart_df["volume"].iloc[-1]) if not chart_df.empty and "volume" in chart_df else None
)
volume_percentile = (
    float(chart_df["volume"].rank(pct=True).iloc[-1] * 100.0)
    if not chart_df.empty and "volume" in chart_df
    else 0.0
)

fund_hist = live.get("funding_hist_pct") or [0.0]
fund_now = live.get("funding_now_pct") or 0.0
fund_z = (fund_now - np.mean(fund_hist)) / (
    np.std(fund_hist) if np.std(fund_hist) > 1e-8 else 1e-8
)

funding_score = _capped_positive_score(
    fund_z, scale=SIGNAL_COMPONENTS["funding"].max_points, cap=SIGNAL_COMPONENTS["funding"].max_points
)

whale_flow_score = min(
    SIGNAL_COMPONENTS["whale_flow"].max_points,
    max(0.0, (live.get("xrpl_weighted_inflow") or 0.0) / 60e6 * SIGNAL_COMPONENTS["whale_flow"].max_points),
)

price_score = _linear_window_score(
    live.get("price"),
    start=price_window_start,
    end=price_window_end,
    max_score=SIGNAL_COMPONENTS["price_window"].max_points,
)

oi_score = _linear_window_score(
    (live.get("oi_usd") or 0.0) / 1e9,
    start=2.7,
    end=1.5,
    max_score=SIGNAL_COMPONENTS["oi"].max_points,
)

netflow_score = min(
    SIGNAL_COMPONENTS["netflow"].max_points,
    max(
        0.0,
        (-(live.get("binance_netflow_24h") or 0.0))
        / 100e6
        * SIGNAL_COMPONENTS["netflow"].max_points,
    ),
)

ls_ratio = live.get("long_short_ratio", 1.0) or 0.0
short_squeeze_score = min(
    SIGNAL_COMPONENTS["squeeze"].max_points, max(0.0, (2.0 - ls_ratio) * SIGNAL_COMPONENTS["squeeze"].max_points)
)

sentiment_score = _linear_window_score(
    ema_sent, start=0.3, end=0.05, max_score=SIGNAL_COMPONENTS["sentiment"].max_points
)

points = {
    SIGNAL_COMPONENTS["funding"].name: funding_score,
    SIGNAL_COMPONENTS["whale_flow"].name: whale_flow_score,
    SIGNAL_COMPONENTS["price_window"].name: price_score,
    SIGNAL_COMPONENTS["oi"].name: oi_score,
    SIGNAL_COMPONENTS["netflow"].name: netflow_score,
    SIGNAL_COMPONENTS["squeeze"].name: short_squeeze_score,
    SIGNAL_COMPONENTS["sentiment"].name: sentiment_score,
    SIGNAL_COMPONENTS["flippening"].name: flip_score,
}
total_score = float(min(100.0, sum(points.values())))
log_score_components(points)

close_series = chart_df["close"] if not chart_df.empty and "close" in chart_df else None
atr_val = compute_atr(chart_df)
last_price = live.get("price") or (float(close_series.iloc[-1]) if close_series is not None else None)
risk_profile = build_target_profile(
    last_price,
    atr_val,
    ratio_bias=avg_positive_uplift,
    closes=close_series,
)

conviction_prob = posterior_conviction_probability(
    total_score=total_score,
    fund_z=fund_z,
    netflow_score=netflow_score,
    oi_score=oi_score,
    sentiment_score=sentiment_score,
    ratio_uplift=avg_positive_uplift,
)

oi_delta_24h = metric_delta("oi_usd", 24)
netflow_delta_24h = metric_delta("binance_netflow_24h", 24)
funding_delta_7d = metric_delta("funding_now_pct", 24 * 7)

price_change_24h = None
if not chart_df.empty and len(chart_df) >= 2:
    last_close = float(chart_df["close"].iloc[-1])
    prev_close = float(chart_df["close"].iloc[-2])
    if prev_close:
        price_change_24h = (last_close / prev_close - 1.0) * 100.0

flow_breakdown = xrpl_flow_breakdown()
netflow_hist = cache_get_json("history:metric:binance_netflow_24h")

proxy_composite = build_proxy_composite_series(
    chart_df, fund_hist, ema_sent, flow_breakdown.get("history"), netflow_hist
)
regimes = detect_regimes(
    price_change_24h,
    fund_z,
    btc_uplift_pct,
    eth_uplift_pct,
    oi_delta_24h,
    volume_percentile,
    live.get("long_short_ratio"),
)

# =========================
# Data health banner
# =========================

issues, redis_notes = describe_data_health(live, news_payload)
freshness_notes = (live.get("freshness_notes") or []) + collect_freshness_notes()
render_health_strip(
    issues,
    redis_notes,
    live.get("binance_notes") or [],
    latency_notes=live.get("latency_notes"),
    freshness_notes=freshness_notes,
)
st.caption(f"Last updated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")

# =========================
# UI — Redesigned layout
# =========================

def _fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    try:
        return f"${value:,.0f}"
    except Exception:
        return "N/A"


def render_sparkline_from_history(name: str, title: str, color: str = "#00ff88") -> None:
    history = cache_get_json(f"history:metric:{name}")
    if not history:
        st.info(f"No history for {title} yet.")
        return
    df = pd.DataFrame(history)
    if df.empty or "value" not in df:
        st.info(f"No history for {title} yet.")
        return
    df["ts"] = pd.to_datetime(df["ts"], unit="s", errors="coerce")
    df = df.dropna(subset=["ts"])
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["ts"],
            y=df["value"],
            mode="lines",
            line=dict(color=color, width=2),
            fill="tozeroy",
            fillcolor="rgba(0,255,136,0.12)",
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=10),
        height=160,
        template="plotly_dark",
        showlegend=False,
        xaxis_title="",
        yaxis_title="",
    )
    st.markdown(f"**{title}**")
    st.plotly_chart(fig, use_container_width=True)


etf_metrics = read_etf_metrics()
with st.expander("XRP ETF market", expanded=True):
    section_label(f"Provider: {etf_metrics.get('provider', 'Aggregated')}")
    etf_cols = st.columns(4)
    etf_cols[0].metric(
        "AUM",
        _fmt_usd(etf_metrics.get("aum_usd")),
    )
    etf_cols[1].metric(
        "Daily Flow",
        _fmt_usd(etf_metrics.get("daily_flow_usd")),
    )
    etf_cols[2].metric(
        "Secondary Volume",
        _fmt_usd(etf_metrics.get("volume_usd")),
    )
    premium = etf_metrics.get("premium_pct")
    etf_cols[3].metric(
        "Premium / Discount",
        f"{premium:+.2f}%" if premium is not None else "N/A",
    )
    nav = etf_metrics.get("nav_usd")
    st.caption(
        f"NAV: {_fmt_usd(nav)} • Updated {etf_metrics.get('updated_at', 'n/a')}"
    )

spark_cols = st.columns(3)
with spark_cols[0]:
    render_sparkline_from_history("oi_usd", "Open Interest (7d)")
with spark_cols[1]:
    render_sparkline_from_history("funding_now_pct", "Funding % (7d)")
with spark_cols[2]:
    render_sparkline_from_history("binance_netflow_24h", "Binance Netflow (7d)")

section_label("Market overview")
o1, o2, o3 = st.columns([1.2, 1, 1])
with o1:
    metric_card(
        "XRP Price",
        f"${live.get('price', 0.0):.4f}" if live.get("price") else "—",
        sub=f"Refresh {refresh_seconds}s • Funding {live.get('funding_now_pct', 0.0):+.4f}%",
    )
    metric_card("Open Interest", f"${(live.get('oi_usd') or 0.0)/1e9:.2f}B", "Binance futures OI")
with o2:
    metric_card("XRP/BTC", f"{btc_ratio:.8f}" if btc_ratio else "—", "vs EMA uplift {btc_uplift_pct:+.2f}%")
    metric_card("XRP/ETH", f"{eth_ratio:.8f}" if eth_ratio else "—", "vs EMA uplift {eth_uplift_pct:+.2f}%")
with o3:
    metric_card("L/S Ratio", f"{live.get('long_short_ratio', 1.0):.2f}", "Short-squeeze setup")
    netflow_live = live.get("binance_netflow_24h")
    metric_card(
        "Binance Netflow (24h)",
        f"{netflow_live/1e6:+.1f}M XRP" if netflow_live is not None else "N/A",
    )
    metric_card(
        "Spot Volume (24h)",
        f"${volume_latest/1e6:,.1f}M" if volume_latest else "—",
        sub=f"{volume_percentile:.0f}th pct vs 90d" if volume_latest else "No volume history",
    )

section_label("Flows & sentiment")
f1, f2, f3 = st.columns(3)
label = "Inst. Sentiment EMA" if sent_mode == "Institutional Only" else "News Sentiment EMA"
with f1:
    metric_card(label, f"{ema_sent:+.3f}", sub=f"Bull {bull_intensity:+.3f} • Bear {bear_intensity:+.3f}")
    metric_card("Ripple OTC → Exchanges", f"{(live.get('xrpl_ripple_otc') or 0.0)/1e6:+.1f}M XRP")
with f2:
    metric_card(
        "XRPL Inflows",
        f"{(live.get('xrpl_raw_inflow') or 0.0)/1e6:+.1f}M raw",
        sub=f"Weighted {(live.get('xrpl_weighted_inflow') or 0.0)/1e6:+.1f}M",
    )
    metric_card(
        "XRPL Outflows",
        f"{(live.get('xrpl_raw_outflow') or 0.0)/1e6:+.1f}M raw",
        sub=f"Weighted {(live.get('xrpl_weighted_outflow') or 0.0)/1e6:+.1f}M",
    )
with f3:
    metric_card(
        "XRPL Netflow",
        f"{(live.get('xrpl_netflow') or 0.0)/1e6:+.1f}M XRP",
        sub=f"Flippening score {flip_score:.2f}",
    )

flow_chart_col, flow_meta_col = st.columns([1.8, 1])
with flow_chart_col:
    flow_hist_df = pd.DataFrame(flow_breakdown.get("history", [])).tail(21)
    if not flow_hist_df.empty:
        flow_hist_df["timestamp"] = pd.to_datetime(flow_hist_df.get("timestamp"))
        flow_hist_df = flow_hist_df.sort_values("timestamp")
        flow_hist_df["exchange_xrp"] = flow_hist_df.get("exchange_xrp", 0.0).fillna(0.0) / 1e6
        flow_hist_df["ripple_corp_xrp"] = flow_hist_df.get("ripple_corp_xrp", 0.0).fillna(0.0) / 1e6

        fig_flow = go.Figure()
        fig_flow.add_bar(
            x=flow_hist_df["timestamp"],
            y=flow_hist_df["exchange_xrp"],
            name="Exchange",
            marker_color="rgba(0,255,136,0.6)",
        )
        fig_flow.add_bar(
            x=flow_hist_df["timestamp"],
            y=flow_hist_df["ripple_corp_xrp"],
            name="Ripple OTC",
            marker_color="rgba(255,255,255,0.35)",
        )
        fig_flow.update_layout(
            barmode="stack",
            template="plotly_dark",
            height=280,
            margin=dict(l=30, r=20, t=30, b=30),
            legend_orientation="h",
        )
        st.plotly_chart(fig_flow, use_container_width=True)
    else:
        st.info("XRPL flow history unavailable.")

with flow_meta_col:
    metric_card(
        "Flow Z-Score (7d)",
        f"{flow_breakdown.get('total_z', 0.0):+.2f}",
        sub=f"Exch {flow_breakdown.get('exchange_z', 0.0):+.2f} • OTC {flow_breakdown.get('otc_z', 0.0):+.2f}",
    )
    metric_card(
        "OI Δ", f"{oi_delta_24h:+.1f}%" if oi_delta_24h is not None else "N/A", sub="24h change"
    )
    metric_card(
        "Netflow Δ",
        f"{netflow_delta_24h:+.1f}%" if netflow_delta_24h is not None else "N/A",
        sub="Binance 24h",
    )
    metric_card(
        "Funding Δ",
        f"{funding_delta_7d:+.1f}%" if funding_delta_7d is not None else "N/A",
        sub="7d drift",
    )

# Score + composition
score_col, signal_col = st.columns([1, 2])
with score_col:
    if total_score >= 80:
        color, signal = "#00aa44", "STRONG BUY — REVERSAL LIKELY"
    elif total_score >= 65:
        color, signal = "#00cc88", "ACCUMULATION — BULLISH"
    elif total_score <= 35:
        color, signal = "#cc3344", "DISTRIBUTION — CAUTION"
    else:
        color, signal = "#444444", "NEUTRAL — WAIT"
    st.markdown(
        f'<p style="font-size:86px;color:{color};text-align:center;font-weight:bold;">{total_score:.0f}</p>',
        unsafe_allow_html=True,
    )
    st.metric(
        "Conviction Probability",
        f"{conviction_prob * 100:.1f}%",
        help="Calibrated from composite score, sentiment, and flow intensity",
    )
    st.metric(
        "ATR Risk Band",
        risk_profile.get("band"),
        f"ATR {risk_profile.get('atr_pct', 0.0):.1f}%" if risk_profile.get("atr_pct") else "N/A",
    )
    if risk_profile.get("entry"):
        st.metric(
            "Targets",
            f"Entry ${risk_profile['entry']:.3f}",
            help=risk_profile.get("text"),
        )
    else:
        st.metric("Targets", "N/A", help=risk_profile.get("text"))
    risk_stats = risk_profile.get("risk", {}) or {}
    if risk_stats.get("win_rate") is not None and risk_stats.get("max_drawdown_pct") is not None:
        st.metric(
            "Win / Drawdown",
            f"{risk_stats['win_rate']:.0f}% / {risk_stats['max_drawdown_pct']:.1f}%",
            help="Historical close-series win rate vs. daily changes and max drawdown.",
        )
    else:
        st.metric("Win / Drawdown", "N/A", help="Waiting for sufficient close history.")
with signal_col:
    st.markdown(
        f'<h2 style="color:{color};margin-top:30px;">{signal}</h2>',
        unsafe_allow_html=True,
    )
    st.write(f"Funding Z-Score: {fund_z:+.2f}")
    if regimes:
        st.markdown("**Regime Overlays**")
        for label, desc, severity in regimes:
            bg = "rgba(0,255,136,0.18)" if severity == "ok" else ("rgba(234,179,8,0.2)" if severity == "warn" else "rgba(239,68,68,0.2)")
            st.markdown(
                f"<div style='padding:8px 10px;border-radius:8px;margin-bottom:6px;background:{bg};'>"
                f"<strong>{label}</strong><br/><span style='font-size:12px;color:#d0d5dd;'>{desc}</span></div>",
                unsafe_allow_html=True,
            )
    score_df = (
        pd.DataFrame({"Component": points.keys(), "Points": points.values()})
        .sort_values("Points", ascending=False)
        .reset_index(drop=True)
    )
    score_df["Points"] = score_df["Points"].map(lambda x: f"{x:.1f}")
    st.table(score_df)
    st.caption(price_window_note)

# =========================
# Live Signal Breakdown (raw)
# =========================

with st.expander("Live Signal Breakdown (raw)", expanded=False):
    raw_items = {
        "Funding Now (%)": live.get("funding_now_pct"),
        "Funding Z-Score": round(fund_z, 4),
        "XRPL Inflows (raw, M XRP)": (live.get("xrpl_raw_inflow") or 0.0) / 1e6,
        "XRPL Outflows (raw, M XRP)": (live.get("xrpl_raw_outflow") or 0.0) / 1e6,
        "XRPL Netflow (raw, M XRP)": (live.get("xrpl_netflow") or 0.0) / 1e6,
        "XRPL Inflows (weighted, M XRP)": (live.get("xrpl_weighted_inflow") or 0.0)
        / 1e6,
        "XRPL Outflows (weighted, M XRP)": (live.get("xrpl_weighted_outflow") or 0.0)
        / 1e6,
        "Ripple OTC → Exchanges (M XRP)": (live.get("xrpl_ripple_otc") or 0.0) / 1e6,
        "Binance Netflow 24h (XRP)": live.get("binance_netflow_24h"),
        "Open Interest $": live.get("oi_usd") or 0.0,
        "L/S Ratio": live.get("long_short_ratio"),
        "News Sentiment (inst)": inst_sent,
        "News Sentiment EMA": ema_sent,
        "Bullish Intensity": bull_intensity,
        "Bearish Intensity": bear_intensity,
        "News Count": news_payload.get("count", 0),
        "Flippening Score": flip_score,
    }
    for k, v in raw_items.items():
        a, b = st.columns([3, 1])
        a.write(k)
        b.write("Quiet" if v == 0 else str(v))

with st.expander("Sentiment Drill-down", expanded=False):
    st.write("Top weighted headlines driving the sentiment EMA. Use the toggle to drop low-confidence sources.")
    exclude_low_conf = st.checkbox("Hide weights < 0.35", value=True)
    filtered_articles = [
        a for a in articles if a.get("scalar") is not None and (not exclude_low_conf or (a.get("weight", 0.0) or 0.0) >= 0.35)
    ]
    if not filtered_articles:
        st.info("No scored headlines available yet.")
    else:
        bucket_ts = news_payload.get("timestamp")
        bucket_label = "Latest sentiment window"
        if bucket_ts:
            try:
                ts_dt = datetime.fromisoformat(str(bucket_ts).replace("Z", "+00:00"))
                delta_m = (datetime.now(timezone.utc) - ts_dt).total_seconds() / 60
                bucket_label = f"Updated {delta_m:.0f} minutes ago"
            except Exception:
                bucket_label = "Latest sentiment window"

        pos_candidates = sorted(filtered_articles, key=lambda a: a.get("scalar", 0.0), reverse=True)
        neg_candidates = sorted(filtered_articles, key=lambda a: a.get("scalar", 0.0))

        resolved_pos, resolved_neg = resolve_sentiment_conflicts(pos_candidates, neg_candidates)

        top_pos = resolved_pos[:3]
        top_neg = resolved_neg[:3]

        cpos, cneg = st.columns(2)
        with cpos:
            st.markdown("**Top Positive**")
            for art in top_pos:
                st.markdown(
                    f"• {art.get('title', 'N/A')}  \\n+                    <span style='color:#9ca3af;font-size:12px;'>Src {art.get('source','?')} • w={art.get('weight',0):.2f} • {bucket_label}</span>",
                    unsafe_allow_html=True,
                )
        with cneg:
            st.markdown("**Top Negative**")
            for art in top_neg:
                st.markdown(
                    f"• {art.get('title', 'N/A')}  \\n+                    <span style='color:#9ca3af;font-size:12px;'>Src {art.get('source','?')} • w={art.get('weight',0):.2f} • {bucket_label}</span>",
                    unsafe_allow_html=True,
                )

# =========================
# Simple SMA Backtest on Price
# =========================

st.markdown("### 90-Day SMA + Volume Backtest (Price-only Approximation)")


def run_sma_backtest(
    df: pd.DataFrame, fast: int = 7, slow: int = 21, gate: Optional[pd.Series] = None
):
    if df.empty or len(df) < slow:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "equity": pd.Series(dtype=float),
            "signals": [],
        }
    df = df.copy().sort_values("date")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()

    valid = df["sma_fast"].notna() & df["sma_slow"].notna()
    if gate is not None:
        gate_aligned = gate.reindex(df.index).fillna(False)
    else:
        gate_aligned = pd.Series(True, index=df.index)

    df["signal"] = np.where(valid & gate_aligned & (df["sma_fast"] > df["sma_slow"]), 1, 0)
    df["signal_shift"] = df["signal"].shift(1, fill_value=0)

    df["ret"] = df["close"].pct_change().fillna(0.0)
    turnover = df["signal"].diff().abs().fillna(df["signal"].abs())
    fee_rate = 0.001  # 10 bps per entry/exit leg to penalize churn
    df["strategy_ret"] = df["ret"] * df["signal_shift"] - turnover * fee_rate

    equity = (1.0 + df["strategy_ret"]).cumprod() * 100.0

    entries = df.index[(df["signal_shift"] == 0) & (df["signal"] == 1)]
    exits = df.index[(df["signal_shift"] == 1) & (df["signal"] == 0)]

    if len(entries) > len(exits) and len(entries) > 0:
        exits = exits.union(pd.Index([df.index[-1]]))

    trade_returns = []
    signals = []
    for ent, ex in zip(entries, exits):
        p_ent = df.loc[ent, "close"]
        p_ex = df.loc[ex, "close"]
        if p_ent and p_ex and p_ent > 0:
            r = ((p_ex / p_ent) - 1.0 - 2 * fee_rate) * 100.0
            trade_returns.append(r)
            signals.append(
                {
                    "entry_date": df.loc[ent, "date"],
                    "exit_date": df.loc[ex, "date"],
                    "entry_price": float(p_ent),
                    "exit_price": float(p_ex),
                    "return_pct": float(r),
                }
            )

    num_trades = len(trade_returns)
    if num_trades > 0:
        win_rate = len([r for r in trade_returns if r > 0]) / num_trades * 100.0
        avg_return = float(np.mean(trade_returns))
        eq_arr = equity.values
        roll_max = np.maximum.accumulate(eq_arr)
        dd = (eq_arr - roll_max) / roll_max
        max_drawdown = float(dd.min()) * 100.0
        if np.std(df["strategy_ret"]) > 1e-8:
            sharpe = float(
                (np.mean(df["strategy_ret"]) / np.std(df["strategy_ret"]))
                * np.sqrt(252)
            )
        else:
            sharpe = 0.0
    else:
        win_rate = avg_return = max_drawdown = sharpe = 0.0

    return {
        "num_trades": num_trades,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "equity": equity,
        "signals": signals,
        "df": df,
    }


bt = run_sma_backtest(chart_df)

m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Signals", bt["num_trades"])
m2.metric("Win Rate", f"{bt['win_rate']:.1f}%")
m3.metric("Avg Return / Trade", f"{bt['avg_return']:+.1f}%")
m4.metric("Max Drawdown", f"{bt['max_drawdown']:+.1f}%")
m5.metric("Sharpe (approx)", f"{bt['sharpe']:.2f}")

if not bt["equity"].empty:
    eq_fig = go.Figure()
    eq_fig.add_trace(
        go.Scatter(
            x=chart_df.sort_values("date")["date"],
            y=bt["equity"],
            mode="lines",
            name="Equity (SMA strategy)",
        )
    )
    eq_fig.update_layout(
        height=300,
        template="plotly_dark",
        margin=dict(l=40, r=40, t=40, b=40),
        yaxis_title="Equity (start=100)",
    )
    st.plotly_chart(eq_fig, use_container_width=True)

if proxy_composite is not None:
    st.markdown("#### Composite-gated SMA variants")
    gate65 = proxy_composite >= 65
    gate80 = proxy_composite >= 80
    bt65 = run_sma_backtest(chart_df, gate=gate65)
    bt80 = run_sma_backtest(chart_df, gate=gate80)

    g1, g2 = st.columns(2)
    with g1:
        st.metric("Signals (≥65)", bt65["num_trades"], help="Only take SMA longs when proxy composite ≥ 65")
        st.metric("Win % (≥65)", f"{bt65['win_rate']:.1f}%")
        st.metric("Avg Return (≥65)", f"{bt65['avg_return']:+.1f}%")
    with g2:
        st.metric("Signals (≥80)", bt80["num_trades"], help="Only take SMA longs when proxy composite ≥ 80")
        st.metric("Win % (≥80)", f"{bt80['win_rate']:.1f}%")
        st.metric("Avg Return (≥80)", f"{bt80['avg_return']:+.1f}%")

if proxy_composite is not None:
    st.markdown("#### Composite-gated SMA variants")
    gate65 = proxy_composite >= 65
    gate80 = proxy_composite >= 80
    bt65 = run_sma_backtest(chart_df, gate=gate65)
    bt80 = run_sma_backtest(chart_df, gate=gate80)

    g1, g2 = st.columns(2)
    with g1:
        st.metric("Signals (≥65)", bt65["num_trades"], help="Only take SMA longs when proxy composite ≥ 65")
        st.metric("Win % (≥65)", f"{bt65['win_rate']:.1f}%")
        st.metric("Avg Return (≥65)", f"{bt65['avg_return']:+.1f}%")
    with g2:
        st.metric("Signals (≥80)", bt80["num_trades"], help="Only take SMA longs when proxy composite ≥ 80")
        st.metric("Win % (≥80)", f"{bt80['win_rate']:.1f}%")
        st.metric("Avg Return (≥80)", f"{bt80['avg_return']:+.1f}%")

# =========================
# XRPL inflow table
# =========================

st.markdown("### XRPL → Exchange Inflows (Last Snapshot)")
try:
    raw = rdb.get("xrpl:latest_inflows")
    if raw:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        inflows = json.loads(raw)
    else:
        inflows = []
except Exception:
    inflows = []

if inflows:
    xrpl_df = pd.DataFrame(inflows)
    xrpl_df_display = xrpl_df.copy()

    if "xrp" in xrpl_df_display.columns:
        xrpl_df_display["xrp_m"] = xrpl_df_display["xrp"].astype(float) / 1e6
    if "timestamp" in xrpl_df_display.columns:
        xrpl_df_display["timestamp"] = xrpl_df_display["timestamp"].astype(str)

    cols = []
    for col in ["timestamp", "exchange", "xrp_m", "from_owner", "to_owner"]:
        if col in xrpl_df_display.columns:
            cols.append(col)

    st.dataframe(xrpl_df_display[cols], hide_index=True)
else:
    st.info("No recent XRPL inflows snapshot found.")

# =========================
# FINAL CHART: Candles + Volume + Signals
# =========================

st.markdown("### 90-Day XRP Candles + Volume + SMA Signals")

if not chart_df.empty:
    df = chart_df.sort_values("date")
    fig = go.Figure()

    fig.add_trace(
        go.Candlestick(
            x=df["date"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="XRP",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            yaxis="y1",
        )
    )

    fig.add_trace(
        go.Bar(
            x=df["date"],
            y=df["volume"] / 1e9,
            name="Volume (B)",
            marker_color="rgba(100,150,255,0.4)",
            yaxis="y2",
        )
    )

    for sig in bt["signals"]:
        fig.add_trace(
            go.Scatter(
                x=[sig["entry_date"]],
                y=[sig["entry_price"]],
                mode="markers",
                marker=dict(symbol="triangle-up", size=10),
                name="Entry",
                showlegend=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[sig["exit_date"]],
                y=[sig["exit_price"]],
                mode="markers",
                marker=dict(symbol="triangle-down", size=10),
                name="Exit",
                showlegend=False,
            )
        )

    fig.update_layout(
        height=700,
        template="plotly_dark",
        xaxis=dict(rangeslider_visible=False),
        yaxis=dict(title="Price (USD)", domain=[0.35, 1.0]),
        yaxis2=dict(
            title="Volume (B)",
            domain=[0.0, 0.3],
            overlaying="y",
            side="right",
        ),
        hovermode="x unified",
        margin=dict(l=50, r=50, t=50, b=50),
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.error("Chart data unavailable — both CoinGecko and Binance failed")

# =========================
# Footer
# =========================

st.caption(
    "v9.3 — XRP only • XRPL Inflows (Weighted + Ripple OTC) • Binance Netflow • "
    "XRP/BTC & XRP/ETH Flippening • News Sentiment (EMA + Bull/Bear) • "
    "SMA Backtest + Signal Annotations"
)
