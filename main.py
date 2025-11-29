"""BlackRock-style XRP macro surveillance dashboard."""

import math
import os
import importlib.util
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import plotly.graph_objects as go
import requests
import streamlit as st

from app_utils import cache_get_json, normalize_env_value, safe_get
from redis_client import rdb
from signals import (
    SIGNAL_COMPONENTS,
    calibrated_conviction_probability,
    derive_reason_codes,
    log_score_components,
    reason_score_adjustment,
)

from db import fetch_latest_flow, fetch_latest_snapshot
from xrpl_utils import fetch_account_overview, parse_account_input

# =========================
# Page config & styling
# =========================
st.set_page_config(
    page_title="XRP Quant Governance Console",
    layout="wide",
    initial_sidebar_state="collapsed",
)

PALETTE = {
    "bg": "#0b1021",
    "panel": "rgba(255, 255, 255, 0.04)",
    "accent": "#4ade80",
    "muted": "#9ca3af",
    "text": "#e5e7eb",
    "alert": "#f97316",
    "error": "#ef4444",
}

st.markdown(
    f"""
    <style>
    body {{background-color: {PALETTE['bg']}; color: {PALETTE['text']};}}
    .metric {{background:{PALETTE['panel']}; padding:14px 16px; border-radius:14px; border:1px solid rgba(255,255,255,0.06);}}
    .metric h3 {{font-size:14px; color:{PALETTE['muted']}; margin-bottom:6px; text-transform:uppercase; letter-spacing:0.08em;}}
    .metric .value {{font-size:26px; font-weight:700; color:{PALETTE['text']};}}
    .metric .note {{font-size:13px; color:{PALETTE['muted']}; margin-top:4px;}}
    .section-title {{font-size:18px; font-weight:700; letter-spacing:0.02em;}}
    .tag {{display:inline-block; padding:6px 10px; border-radius:999px; font-size:12px; margin-right:6px;}}
    .tag-ok {{background:rgba(74,222,128,0.14); color:#bbf7d0; border:1px solid rgba(74,222,128,0.5);}}
    .tag-warn {{background:rgba(249,115,22,0.16); color:#fed7aa; border:1px solid rgba(249,115,22,0.4);}}
    .tag-err {{background:rgba(239,68,68,0.16); color:#fecdd3; border:1px solid rgba(239,68,68,0.4);}}
    </style>
    """,
    unsafe_allow_html=True,
)

# =========================
# Helpers
# =========================

SESSION_FALLBACK: Dict[str, Any] = {}


def get_state(key: str, default: Any) -> Any:
    try:
        if key in st.session_state:
            return st.session_state.get(key, default)
    except Exception:  # noqa: BLE001
        pass
    return SESSION_FALLBACK.get(key, default)


def set_state(key: str, value: Any) -> None:
    SESSION_FALLBACK[key] = value
    try:
        st.session_state[key] = value
    except Exception:  # noqa: BLE001
        return


def redact_secret(value: str, keep: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}***{value[-keep:]}"


def to_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def time_ago(ts: Optional[datetime]) -> str:
    if not ts:
        return "unknown"
    delta = datetime.now(timezone.utc) - ts
    if delta < timedelta(seconds=90):
        return f"{int(delta.total_seconds())}s ago"
    if delta < timedelta(hours=2):
        return f"{int(delta.total_seconds() // 60)}m ago"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours}h ago"


def styled_metric(title: str, value: str, note: str = "") -> None:
    st.markdown(
        f"""
        <div class="metric">
            <h3>{title}</h3>
            <div class="value">{value}</div>
            <div class="note">{note}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _clamp(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def drops_to_xrp(value: Optional[float]) -> float:
    try:
        return float(value or 0.0) / 1_000_000
    except Exception:  # noqa: BLE001
        return 0.0


def compute_signal_stack(
    price: Dict[str, Optional[float]],
    futures: Dict[str, Optional[float]],
    flows: Dict[str, Any],
    sentiment: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate raw telemetry into composite-ready signal contributions."""

    details: List[Dict[str, Any]] = []
    reason_inputs: Dict[str, Any] = {}
    prev_price = get_state("last_price", None)
    prev_agg_oi = get_state("last_agg_oi", None)

    def add_component(
        key: str,
        *,
        points: float,
        available: bool,
        status: str,
        note: str,
    ) -> None:
        meta = SIGNAL_COMPONENTS.get(key)
        max_points = meta.max_points if meta else 0.0
        details.append(
            {
                "key": key,
                "name": meta.name if meta else key,
                "points": max(0.0, min(points, max_points)),
                "max_points": max_points,
                "status": status,
                "note": note,
                "available": available,
                "cap_note": meta.cap_note if meta else "",
            }
        )

    price_usd = price.get("price")
    price_meta = SIGNAL_COMPONENTS.get("price_window")
    if price_usd is None:
        add_component(
            "price_window",
            points=0.0,
            available=False,
            status="Waiting on price feed",
            note="CoinGecko price missing; composite gated until price returns.",
        )
    else:
        if price_usd <= 2.45:
            price_points = price_meta.max_points
            status = "In breakout zone"
        elif price_usd >= 3.0:
            price_points = 0.0
            status = "Extended; patience warranted"
        else:
            scale = _clamp((3.0 - price_usd) / (3.0 - 2.45))
            price_points = price_meta.max_points * scale
            status = "Approaching extension"
        add_component(
            "price_window",
            points=price_points,
            available=True,
            status=status,
            note=f"${price_usd:,.4f} vs. $2.45–$3.00 window",
        )
        reason_inputs["price_status"] = status

    funding = futures.get("funding")
    fund_meta = SIGNAL_COMPONENTS.get("funding")
    if funding is None:
        add_component(
            "funding",
            points=0.0,
            available=False,
            status="Funding unavailable",
            note="Binance funding endpoint did not return data.",
        )
    else:
        funding_bps = funding * 10_000
        scaled = math.tanh(max(funding_bps, 0.0) / 10.0)
        funding_points = fund_meta.max_points * scaled
        status = "Bullish carry" if funding_bps > 0.5 else "Flat/neutral"
        add_component(
            "funding",
            points=funding_points,
            available=True,
            status=status,
            note=f"{funding_bps:+.2f} bps drift (tanh-capped)",
        )
        # Treat funding bps as an approximate z-score proxy for reasoning.
        reason_inputs["funding_z_score"] = funding_bps / 5.0

    oi_usd = futures.get("open_interest")
    oi_meta = SIGNAL_COMPONENTS.get("oi")
    if oi_usd is None:
        add_component(
            "oi",
            points=0.0,
            available=False,
            status="OI missing",
            note="Open interest endpoint unavailable.",
        )
    else:
        depth_scale = _clamp((oi_usd - 1_500_000_000) / (2_700_000_000 - 1_500_000_000))
        oi_points = depth_scale * oi_meta.max_points
        status = "Depth supportive" if depth_scale >= 0.6 else "Shallow liquidity"
        add_component(
            "oi",
            points=oi_points,
            available=True,
            status=status,
            note=f"${oi_usd:,.0f} open interest vs. $1.5B–$2.7B lane",
        )

    # Aggregated open interest across exchanges (Binance + Bybit). This
    # component rewards multi‑venue liquidity. Full points are awarded when
    # aggregated OI exceeds $4B and decays linearly to $2B.
    agg_oi = futures.get("aggregated_open_interest")
    agg_meta = SIGNAL_COMPONENTS.get("oi_aggregated")
    if agg_meta:
        if agg_oi is None:
            add_component(
                "oi_aggregated",
                points=0.0,
                available=False,
                status="Aggregated OI missing",
                note="Could not fetch aggregated open interest across venues.",
            )
        else:
            agg_scale = _clamp((agg_oi - 2_000_000_000) / (4_000_000_000 - 2_000_000_000))
            agg_points = agg_meta.max_points * agg_scale
            status = "Depth supportive" if agg_scale >= 0.6 else "Shallow liquidity"
            add_component(
                "oi_aggregated",
                points=agg_points,
                available=True,
                status=status,
                note=f"${agg_oi:,.0f} aggregated OI",
            )

    # Relative volume component. Measures how much the current trading volume
    # exceeds its recent average. rVOL values greater than 1.0 indicate volume
    # above the baseline. Full credit is achieved at rVOL ≥ 3.0 and fades to
    # zero by 1.0.
    rvol = futures.get("relative_volume")
    rvol_meta = SIGNAL_COMPONENTS.get("relative_volume")
    if rvol_meta:
        if rvol is None:
            add_component(
                "relative_volume",
                points=0.0,
                available=False,
                status="Volume unavailable",
                note="Could not compute relative volume; CoinGecko volume API missing.",
            )
        else:
            # Scale between 1.0 and 3.0
            rvol_scale = _clamp((rvol - 1.0) / (3.0 - 1.0))
            rvol_points = rvol_meta.max_points * rvol_scale
            status = "High activity" if rvol >= 2.0 else "Average volume"
            add_component(
                "relative_volume",
                points=rvol_points,
                available=True,
                status=status,
                note=f"rVOL {rvol:.2f}",
            )

    # Open interest change component. Compute the delta of aggregated OI versus
    # the previous snapshot stored in session state. Positive changes indicate
    # additional leverage; negative changes suggest liquidation or deleveraging.
    oi_change_meta = SIGNAL_COMPONENTS.get("oi_change")
    if oi_change_meta:
        agg_oi = futures.get("aggregated_open_interest")
        last_agg = prev_agg_oi
        delta_oi: Optional[float] = None
        if agg_oi is not None and last_agg is not None:
            delta_oi = agg_oi - last_agg
        if delta_oi is None or agg_oi is None or last_agg is None:
            add_component(
                "oi_change",
                points=0.0,
                available=False,
                status="Change unavailable",
                note="Insufficient history to compute OI change.",
            )
        else:
            # Scale on absolute delta; full points at ±200M USD
            abs_delta = abs(delta_oi)
            change_scale = _clamp(abs_delta / 200_000_000.0)
            change_points = oi_change_meta.max_points * change_scale
            status = "Increasing OI" if delta_oi > 0 else "Decreasing OI"
            add_component(
                "oi_change",
                points=change_points,
                available=True,
                status=status,
                note=f"{delta_oi:,.0f} change vs. last snapshot",
            )
            reason_inputs["oi_direction"] = "up" if delta_oi > 0 else "down"

    # Divergence component. Detects directional disagreement between OI and price.
    div_meta = SIGNAL_COMPONENTS.get("divergence")
    if div_meta:
        agg_oi = futures.get("aggregated_open_interest")
        last_agg = prev_agg_oi
        price_now = price.get("price")
        last_price = prev_price
        divergence_detected = False
        if agg_oi is not None and last_agg is not None and price_now is not None and last_price is not None:
            delta_oi = agg_oi - last_agg
            delta_price = price_now - last_price
            # Bullish divergence: OI ↑, price ↓; bearish divergence: OI ↓, price ↑
            if delta_oi > 0 and delta_price < 0:
                divergence_detected = True
                div_status = "Bullish divergence"
            elif delta_oi < 0 and delta_price > 0:
                divergence_detected = True
                div_status = "Bearish divergence"
            else:
                divergence_detected = False
            if price_now != last_price:
                reason_inputs["price_direction"] = "up" if delta_price > 0 else "down"
            if delta_oi != 0:
                reason_inputs["oi_direction"] = "up" if delta_oi > 0 else "down"
        if divergence_detected:
            add_component(
                "divergence",
                points=div_meta.max_points,
                available=True,
                status=div_status,
                note="OI and price moving in opposite directions.",
            )
        else:
            add_component(
                "divergence",
                points=0.0,
                available=True if (agg_oi is not None and last_agg is not None and price_now is not None and last_price is not None) else False,
                status="No divergence",
                note="No significant OI/price divergence detected.",
            )

    if price.get("price") is not None:
        set_state("last_price", price.get("price"))
    if futures.get("aggregated_open_interest") is not None:
        set_state("last_agg_oi", futures.get("aggregated_open_interest"))

    inflow = flows.get("latest_inflow")
    flow_meta = SIGNAL_COMPONENTS.get("whale_flow")
    if inflow is None:
        add_component(
            "whale_flow",
            points=0.0,
            available=False,
            status="XRPL inflow missing",
            note="Redis cache empty; worker may be offline.",
        )
    else:
        flow_scale = _clamp(float(inflow) / 60_000_000)
        flow_points = flow_scale * flow_meta.max_points
        status = "Exchange demand building" if inflow > 10_000_000 else "Muted demand"
        add_component(
            "whale_flow",
            points=flow_points,
            available=True,
            status=status,
            note=f"{float(inflow):,.0f} XRP tagged inflow (capped at 60M)",
        )

    outflow = flows.get("latest_outflow")
    netflow_meta = SIGNAL_COMPONENTS.get("netflow")
    if inflow is None or outflow is None:
        add_component(
            "netflow",
            points=0.0,
            available=False,
            status="Netflow unknown",
            note="Need both inflow and outflow slices to score withdrawals.",
        )
    else:
        withdrawals = max(outflow - inflow, 0.0)
        netflow_scale = _clamp(withdrawals / 100_000_000)
        netflow_points = netflow_scale * netflow_meta.max_points
        status = "Exchanges net-withdrawing" if withdrawals > 0 else "Flat/accumulating"
        add_component(
            "netflow",
            points=netflow_points,
            available=True,
            status=status,
            note=f"{withdrawals:,.0f} XRP 24h net withdrawal bias",
        )
        reason_inputs["netflow_xrp"] = withdrawals if outflow >= inflow else inflow - outflow

    sentiment_ema = sentiment.get("ema")
    sent_meta = SIGNAL_COMPONENTS.get("sentiment")
    if sentiment_ema is None:
        add_component(
            "sentiment",
            points=0.0,
            available=False,
            status="Sentiment cache missing",
            note="Sentiment worker has not populated EMA.",
        )
    else:
        sentiment_scale = _clamp((sentiment_ema - 0.05) / (0.30 - 0.05))
        sentiment_points = sent_meta.max_points * sentiment_scale
        status = "Headline tone supportive" if sentiment_ema >= 0.15 else "Muted tone"
        add_component(
            "sentiment",
            points=sentiment_points,
            available=True,
            status=status,
            note=f"EMA {sentiment_ema:+.2f} vs. +0.05/+0.30 lane",
        )
        reason_inputs["sentiment_ema"] = sentiment_ema

    # Long/short ratio based squeeze component. A value ≤1.0 implies more shorts
    # than longs, favouring a short‑squeeze setup; values ≥2.0 imply longs dominate.
    ls_ratio = futures.get("long_short_ratio")
    squeeze_meta = SIGNAL_COMPONENTS.get("squeeze")
    if ls_ratio is None:
        # When the long/short ratio cannot be fetched (e.g. due to network errors),
        # treat the squeeze setup as neutral rather than punitive. Assign half of
        # the component’s maximum points and mark the input as unavailable.
        default_scale = 0.5
        squeeze_points = squeeze_meta.max_points * default_scale if squeeze_meta else 0.0
        add_component(
            "squeeze",
            points=squeeze_points,
            available=False,
            status="Neutral (L/S ratio unavailable)",
            note="Default neutral weight applied due to missing long/short ratio.",
        )
    else:
        # Linearly scale between 1.0 and 2.0: full points at ≤1.0, zero at ≥2.0.
        ls_scale = _clamp((2.0 - ls_ratio) / (2.0 - 1.0))
        squeeze_points = squeeze_meta.max_points * ls_scale
        status = "Short squeeze risk" if ls_ratio <= 1.0 else "Long‑skewed"
        add_component(
            "squeeze",
            points=squeeze_points,
            available=True,
            status=status,
            note=f"L/S ratio {ls_ratio:.2f}",
        )

    total_points = sum(d["points"] for d in details if d["available"])
    total_cap = sum(d["max_points"] for d in details if d["available"])
    normalized = (total_points / total_cap * 100.0) if total_cap else 0.0
    normalized = min(100.0, max(0.0, normalized))

    log_score_components({d["key"]: d["points"] for d in details})

    return {
        "details": details,
        "composite": normalized,
        "probability": calibrated_conviction_probability(normalized),
        "coverage": total_cap,
        "reason_inputs": reason_inputs,
    }


# =========================
# Data acquisition
# =========================


COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"
REQUEST_TIMEOUT = 12

def fetch_long_short_ratio(period: str = "1h") -> Optional[float]:
    """
    Fetch the global long/short account ratio for XRP futures from Binance.

    Binance exposes a public endpoint for the long/short ratio of each symbol.
    The API is queried without authentication and returns the most recent
    longShortRatio when no start/end timestamps are provided. This signal
    highlights potential short‑squeeze setups: values ≤1.0 indicate that
    shorts dominate long positions.  According to Binance documentation, the
    endpoint accepts the trading pair and a period parameter, and if
    startTime/endTime are omitted the latest data is returned【611834218709811†L84-L110】.

    Parameters
    ----------
    period: str
        The time interval for the ratio; valid values include "5m", "15m",
        "30m", "1h", "2h", "4h", "6h", "12h", and "1d"【611834218709811†L100-L104】.

    Returns
    -------
    Optional[float]
        The numeric long/short ratio, or None if unavailable or on error.
    """
    try:
        url = f"{BINANCE_FAPI}/futures/data/globalLongShortAccountRatio"
        params = {"pair": "XRPUSDT", "period": period, "limit": 1}
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            payload = resp.json()
            if isinstance(payload, list) and payload:
                record = payload[0]
                ratio_str = record.get("longShortRatio")
                if ratio_str is not None:
                    try:
                        return float(ratio_str)
                    except Exception:
                        return None
    except Exception:
        # Ignore transient errors; caller will handle missing data gracefully
        return None
    return None

# ============================================================================
# Additional data providers
#
# The Binance endpoints occasionally return "unknown" or fail due to rate
# limits. To enhance resilience, we integrate Bybit's public V5 endpoints for
# both open interest and the long/short account ratio. These endpoints are
# publicly documented and do not require authentication. According to the
# Bybit API documentation, the account‑ratio endpoint returns buyRatio and
# sellRatio fields, representing the ratio of accounts holding long and short
# positions respectively【467619510942920†L95-L104】. The open interest endpoint
# returns the total open interest per symbol measured in the base asset【208719992864950†L94-L128】.

def fetch_bybit_long_short_ratio(period: str = "1h") -> Optional[float]:
    """
    Fetch the long/short account ratio for XRPUSDT from Bybit.

    This function queries the V5 market account ratio endpoint. It requests
    linear USDT‑margined perpetual data for a one‑hour window (by default). If
    the API responds successfully, the ratio is computed as buyRatio / sellRatio.
    On any error or missing fields the function returns None.

    Parameters
    ----------
    period: str
        The time interval for the ratio; valid values include "5min",
        "15min", "30min", "1h", "4h", and "1d"【467619510942920†L110-L115】.

    Returns
    -------
    Optional[float]
        The numeric long/short ratio, or None if unavailable.
    """
    try:
        url = f"{BYBIT_API}/v5/market/account-ratio"
        params = {
            "category": "linear",
            "symbol": "XRPUSDT",
            "period": period,
            "limit": 1,
        }
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        data = resp.json() or {}
        result = data.get("result", {})
        lst = result.get("list", [])
        if lst:
            entry = lst[0] or {}
            try:
                buy_ratio = float(entry.get("buyRatio", 0))
                sell_ratio = float(entry.get("sellRatio", 0))
                if sell_ratio > 0:
                    return buy_ratio / sell_ratio
            except Exception:
                return None
    except Exception:
        return None
    return None


def fetch_bybit_open_interest(period: str = "1h") -> Optional[float]:
    """
    Fetch open interest for XRPUSDT from Bybit's V5 market endpoint.

    The endpoint returns open interest measured in the base asset for linear
    contracts. This function retrieves the most recent data point (limit=1)
    for the given interval and converts the value to float. If the endpoint
    fails or returns invalid data, None is returned.

    Parameters
    ----------
    period: str
        The interval time; valid values include "5min", "15min", "30min",
        "1h", "4h", and "1d"【208719992864950†L110-L117】.

    Returns
    -------
    Optional[float]
        The open interest in base asset units (XRP), or None on error.
    """
    try:
        url = f"{BYBIT_API}/v5/market/open-interest"
        params = {
            "category": "linear",
            "symbol": "XRPUSDT",
            "intervalTime": period,
            "limit": 1,
        }
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if not resp.ok:
            return None
        data = resp.json() or {}
        result = data.get("result", {})
        lst = result.get("list", [])
        if lst:
            entry = lst[0] or {}
            oi_str = entry.get("openInterest")
            try:
                return float(oi_str) if oi_str is not None else None
            except Exception:
                return None
    except Exception:
        return None
    return None


def fetch_aggregated_open_interest(period: str = "1h") -> Optional[float]:
    """
    Aggregate open interest across Binance and Bybit and convert to USD.

    This helper attempts to pull open interest from Binance's openInterest
    endpoint (USDⓈ‑M contracts) and from Bybit's open‑interest endpoint for
    linear contracts. It then multiplies each base‑asset value by the latest
    XRP/USD price fetched via CoinGecko. If both venues return data the
    results are summed; if only one succeeds that value is returned. Returns
    None if neither API returns valid data.

    Parameters
    ----------
    period: str
        Interval parameter passed to the Bybit request. Binance's endpoint
        returns a snapshot and ignores this value.

    Returns
    -------
    Optional[float]
        The aggregated open interest in USD, or None when unavailable.
    """
    price_data = fetch_price_snapshot()
    price_usd = price_data.get("price")
    total_usd: float = 0.0

    # Binance open interest (base asset). Convert to USD.
    try:
        resp = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/openInterest", params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT
        )
        if resp.ok:
            payload = resp.json() or {}
            oi_str = payload.get("openInterest")
            if oi_str is not None and price_usd:
                total_usd += float(oi_str) * float(price_usd)
    except Exception:
        pass

    # Bybit open interest (base asset). Convert to USD.
    bybit_oi = None
    try:
        bybit_oi = fetch_bybit_open_interest(period)
    except Exception:
        bybit_oi = None
    if bybit_oi is not None and price_usd:
        total_usd += bybit_oi * float(price_usd)

    return total_usd if total_usd > 0 else None


def load_api_credentials() -> Dict[str, str]:
    return {
        "binance_key": normalize_env_value("BINANCE_API_KEY"),
        "binance_secret": normalize_env_value("BINANCE_API_SECRET"),
        "news_api": normalize_env_value("NEWS_API_KEY"),
        "whale_alert": normalize_env_value("WHALE_ALERT_KEY"),
    }


def fetch_price_snapshot() -> Dict[str, Optional[float]]:
    params = {
        "ids": "ripple",
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_24hr_vol": "true",
    }
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/simple/price", params=params, timeout=REQUEST_TIMEOUT
        )
        if resp.ok:
            data = resp.json().get("ripple", {})
            return {
                "price": data.get("usd"),
                "change": data.get("usd_24h_change"),
            }
    except Exception:  # noqa: BLE001
        pass

    fallback = cache_get_json("cache:price:xrp_usd")
    if isinstance(fallback, (int, float)):
        return {"price": float(fallback), "change": None}
    return {"price": None, "change": None}


# -----------------------------------------------------------------------------
# Volume and market analytics
# -----------------------------------------------------------------------------
def fetch_market_data(days: int = 30) -> Optional[List[Tuple[datetime, float, float]]]:
    """
    Retrieve price and volume history from CoinGecko for XRP.

    This helper calls the market_chart endpoint and returns a list of
    (timestamp, price, volume) tuples. Volumes reflect total trading volume in
    the quote currency (USD) for each time slice. If the API call fails the
    function returns None.

    Parameters
    ----------
    days: int
        Number of days of history to fetch.

    Returns
    -------
    Optional[List[Tuple[datetime, float, float]]]
        A list of (datetime, price, volume) tuples, or None on error.
    """
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/coins/ripple/market_chart",
            params={"vs_currency": "usd", "days": days},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            raw = resp.json() or {}
            prices = raw.get("prices", [])
            vols = raw.get("total_volumes", [])
            history: List[Tuple[datetime, float, float]] = []
            # Ensure lengths match before zipping
            n = min(len(prices), len(vols))
            for idx in range(n):
                ts_price, price_val = prices[idx]
                _, vol_val = vols[idx]
                history.append(
                    (
                        datetime.fromtimestamp(ts_price / 1000, tz=timezone.utc),
                        float(price_val),
                        float(vol_val),
                    )
                )
            return history
    except Exception:
        return None
    return None


def compute_rvol(window: int = 20) -> Optional[float]:
    """
    Compute relative volume (rVOL) for XRP.

    rVOL is defined as the latest volume divided by the simple moving average of
    volume over a specified window. An rVOL > 1.0 indicates above‑average
    participation. If insufficient data are available or the moving average is
    zero, returns None.

    Parameters
    ----------
    window: int
        Number of periods for the moving average.

    Returns
    -------
    Optional[float]
        The relative volume, or None if unavailable.
    """
    history = fetch_market_data(days=30)
    if not history or len(history) < window + 1:
        return None
    # Extract volumes and compute SMA
    volumes = [vol for _, _, vol in history]
    current_vol = volumes[-1]
    sma = sum(volumes[-window:]) / float(window)
    if sma <= 0:
        return None
    return current_vol / sma


def cached_coingecko_simple_price() -> Dict[str, Dict[str, float]]:
    data = safe_get(
        f"{COINGECKO_BASE}/simple/price",
        params={
            "ids": "ripple",
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        },
        timeout=REQUEST_TIMEOUT,
    )
    if isinstance(data, dict):
        return data

    fallback = cache_get_json("cache:price:xrp_usd")
    if isinstance(fallback, (int, float)):
        return {"ripple": {"usd": float(fallback)}}
    return {"ripple": {}}


def cached_crypto_compare_price() -> Optional[float]:
    data = safe_get(
        "https://min-api.cryptocompare.com/data/price",
        params={"fsym": "XRP", "tsyms": "USD"},
        timeout=REQUEST_TIMEOUT,
    )
    if isinstance(data, dict):
        usd = data.get("USD")
        if isinstance(usd, (int, float)):
            return float(usd)
    return None


def fetch_price_history(days: int = 30) -> Optional[List[Tuple[datetime, float]]]:
    try:
        resp = requests.get(
            f"{COINGECKO_BASE}/coins/ripple/market_chart",
            params={"vs_currency": "usd", "days": days},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            raw = resp.json().get("prices", [])
            history: List[Tuple[datetime, float]] = []
            for ts, price in raw:
                history.append(
                    (datetime.fromtimestamp(ts / 1000, tz=timezone.utc), float(price))
                )
            return history
    except Exception:  # noqa: BLE001
        return None
    return None


def fetch_funding_and_oi() -> Dict[str, Optional[float]]:
    funding: Optional[float] = None
    oi: Optional[float] = None

    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return None

    funding_endpoints = [
        (
            f"{BINANCE_FAPI}/fapi/v1/fundingRate",
            {"symbol": "XRPUSDT", "limit": 1},
            lambda payload: _to_float(payload[0].get("fundingRate"))
            if isinstance(payload, list) and payload
            else None,
        ),
        (
            f"{BINANCE_FAPI}/fapi/v1/premiumIndex",
            {"symbol": "XRPUSDT"},
            lambda payload: _to_float(payload.get("lastFundingRate"))
            if isinstance(payload, dict)
            else None,
        ),
        (
            f"{BINANCE_FAPI}/futures/data/fundingRate",
            {"symbol": "XRPUSDT", "limit": 1},
            lambda payload: _to_float(payload[0].get("fundingRate"))
            if isinstance(payload, list) and payload
            else None,
        ),
    ]

    for url, params, extractor in funding_endpoints:
        if funding is not None:
            break
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if not resp.ok:
                continue

            payload = resp.json() or {}
            funding = extractor(payload)
        except Exception:  # noqa: BLE001
            funding = None

    try:
        resp = requests.get(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": "XRPUSDT", "period": "5m", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok and resp.json():
            oi_raw = resp.json()[0]
            oi = _to_float(oi_raw.get("sumOpenInterest"))
    except Exception:  # noqa: BLE001
        oi = None

    if oi is None:
        try:
            resp = requests.get(
                f"{BINANCE_FAPI}/fapi/v1/openInterest",
                params={"symbol": "XRPUSDT"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.ok:
                payload = resp.json() or {}
                oi = _to_float(payload.get("openInterest"))
        except Exception:  # noqa: BLE001
            oi = None

    # When running in constrained environments (e.g., unit tests or CI without
    # network access), skip auxiliary lookups that would otherwise trigger
    # additional HTTP requests. This keeps the fallback behaviour focused on
    # funding and OI while still returning a valid shape for downstream
    # consumers.
    if os.getenv("SKIP_LIVE_FETCH"):
        return {
            "funding": funding,
            "open_interest": oi,
            "long_short_ratio": None,
            "aggregated_open_interest": None,
            "relative_volume": None,
        }

    # Attempt to fetch the long/short ratio for XRP perpetual futures. We first
    # query Binance; if this fails we fall back to Bybit’s account‑ratio endpoint.
    ls_ratio = None
    try:
        ls_ratio = fetch_long_short_ratio()
    except Exception:
        ls_ratio = None
    if ls_ratio is None:
        try:
            ls_ratio = fetch_bybit_long_short_ratio()
        except Exception:
            ls_ratio = None

    # Aggregate open interest across exchanges. If both Binance and Bybit
    # endpoints fail the aggregated value will be None.
    aggregated_oi: Optional[float] = None
    try:
        aggregated_oi = fetch_aggregated_open_interest()
    except Exception:
        aggregated_oi = None

    # Compute relative volume (rVOL) using CoinGecko volume history. This value
    # reflects how current volume compares to its recent average. It is
    # independent of open interest and may be missing when API calls fail.
    try:
        rvol = compute_rvol()
    except Exception:
        rvol = None

    return {
        "funding": funding,
        "open_interest": oi,
        "long_short_ratio": ls_ratio,
        "aggregated_open_interest": aggregated_oi,
        "relative_volume": rvol,
    }


def fetch_xrpl_flows() -> Dict[str, Any]:
    inflows = cache_get_json("xrpl:latest_inflows")
    outflows = cache_get_json("xrpl:latest_outflows")
    inflow_meta = cache_get_json("xrpl:latest_inflows_meta") or {}
    history = cache_get_json("xrpl:inflow_history")

    def _fallback_inflows_when_missing() -> Tuple[List[Dict], List[Dict], Optional[str]]:
        """Attempt a live fetch when Redis lacks XRPL inflow snapshots.

        Prefers Whale Alert data when a key is configured; otherwise falls back to
        Ripple Data for a free, unauthenticated snapshot.
        """

        if os.getenv("SKIP_LIVE_FETCH") or os.getenv("PYTEST_CURRENT_TEST"):
            return [], [], None

        if importlib.util.find_spec("xrpl_inflow_monitor") is None:
            return [], [], None

        import xrpl_inflow_monitor  # type: ignore

        provider = xrpl_inflow_monitor.resolve_provider()
        live_inflows, live_outflows = xrpl_inflow_monitor.fetch_transactions(provider)

        if live_inflows or live_outflows:
            return live_inflows, live_outflows, provider

        # If Whale Alert is empty or unavailable, try Ripple Data as a free fallback.
        ripple_inflows, ripple_outflows = xrpl_inflow_monitor.fetch_transactions("ripple_data")
        return ripple_inflows, ripple_outflows, "ripple_data"

    def _total_amount(flows: Any) -> float:
        if not isinstance(flows, list):
            return 0.0
        total = 0.0
        for entry in flows:
            try:
                total += float(entry.get("xrp") or entry.get("amount") or 0.0)
            except Exception:  # noqa: BLE001
                continue
        return total

    if not inflows:
        inflows, outflows, provider = _fallback_inflows_when_missing()
        if inflows:
            inflow_meta = inflow_meta or {}
            inflow_meta.setdefault(
                "updated_at",
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            inflow_meta.setdefault("provider", provider or "unknown")
            inflow_meta.setdefault("count", len(inflows))
            inflow_meta.setdefault("run_seconds", 0)
    # Normalize timestamp for dashboard compatibility: copy updated_at to timestamp
    if isinstance(inflow_meta, dict):
        # If inflow_meta stores 'updated_at' but not 'timestamp', promote it. Downstream
        # consumers like the Streamlit dashboard expect a 'timestamp' key.
        if inflow_meta.get("updated_at") and not inflow_meta.get("timestamp"):
            inflow_meta["timestamp"] = inflow_meta.get("updated_at")

    return {
        "latest_inflow": _total_amount(inflows),
        "latest_outflow": _total_amount(outflows),
        "meta": inflow_meta,
        "history": history if isinstance(history, list) else None,
    }


def fetch_sentiment() -> Dict[str, Any]:
    payload = cache_get_json("news:sentiment") or {}
    ema_obj = cache_get_json("news:sentiment_ema") or {}
    articles = payload.get("articles", [])

    pos = payload.get("pos", 0.0)
    neg = payload.get("neg", 0.0)
    scalar = payload.get("scalar", 0.0)
    ema = ema_obj.get("ema")
    ts_raw = ema_obj.get("timestamp") or payload.get("timestamp")
    ts = to_datetime(ts_raw)

    return {
        "bull": pos,
        "bear": neg,
        "instant": scalar,
        "ema": ema,
        "articles": articles,
        "timestamp": ts,
    }


def hydrate_with_db_fallbacks(
    price_snapshot: Dict[str, Optional[float]],
    futures: Dict[str, Optional[float]],
    flows: Dict[str, Any],
    sentiment: Dict[str, Any],
) -> List[str]:
    """Fill missing telemetry from the PostgreSQL snapshot tables."""

    notes: List[str] = []

    try:
        snap = fetch_latest_snapshot()
    except Exception as exc:  # noqa: BLE001
        snap = None
        notes.append(f"DB snapshot unavailable ({exc})")

    if snap:
        (_, price_db, oi_total, funding_db, ls_ratio_db, rvol_db, _, _, _) = snap
        if price_snapshot.get("price") is None and price_db is not None:
            price_snapshot["price"] = float(price_db)
            notes.append("price from DB")
        if futures.get("aggregated_open_interest") is None and oi_total is not None:
            futures["aggregated_open_interest"] = float(oi_total)
            notes.append("agg OI from DB")
        if futures.get("funding") is None and funding_db is not None:
            futures["funding"] = float(funding_db)
            notes.append("funding from DB")
        if futures.get("long_short_ratio") is None and ls_ratio_db is not None:
            futures["long_short_ratio"] = float(ls_ratio_db)
            notes.append("L/S from DB")
        if futures.get("relative_volume") is None and rvol_db is not None:
            futures["relative_volume"] = float(rvol_db)
            notes.append("rVOL from DB")

    try:
        flow_row = fetch_latest_flow()
    except Exception as exc:  # noqa: BLE001
        flow_row = None
        notes.append(f"flows fallback failed ({exc})")

    if flow_row:
        if not isinstance(flows, dict):
            flows = {}
        _, flow_in, flow_out, _ = flow_row
        if flows.get("latest_inflow") in (None, 0.0) and flow_in is not None:
            flows["latest_inflow"] = float(flow_in)
            notes.append("XRPL inflow from DB")
        if flows.get("latest_outflow") in (None, 0.0) and flow_out is not None:
            flows["latest_outflow"] = float(flow_out)
            notes.append("XRPL outflow from DB")

    if sentiment.get("ema") is None:
        # Sentiment snapshots are not yet persisted to Postgres.
        notes.append("sentiment cache only")

    return notes


def fetch_live() -> Dict[str, float]:
    """Backward-compatible live snapshot used by unit tests and workers."""

    inflows = cache_get_json("xrpl:latest_inflows")
    history = cache_get_json("xrpl:inflow_history") or []

    raw_inflow = 0.0
    weighted_inflow = 0.0
    ripple_otc = 0.0

    if isinstance(inflows, list) and inflows:
        for entry in inflows:
            try:
                xrp = float(entry.get("xrp") or entry.get("amount") or 0.0)
                weight = float(entry.get("weight", 1.0))
            except Exception:  # noqa: BLE001
                xrp = 0.0
                weight = 1.0
            raw_inflow += xrp
            weighted_inflow += xrp * weight
            if entry.get("ripple_corp"):
                ripple_otc += xrp
    elif history:
        latest = history[-1]
        raw_inflow = float(latest.get("total_xrp", 0.0))
        weighted_inflow = float(latest.get("weighted_xrp", raw_inflow))
        ripple_otc = float(latest.get("ripple_otc", 0.0))

    price_data = cached_coingecko_simple_price()
    price_usd = price_data.get("ripple", {}).get("usd")
    if price_usd is None:
        price_usd = cached_crypto_compare_price() or 0.0

    return {
        "price_usd": float(price_usd or 0.0),
        "xrpl_raw_inflow": float(raw_inflow),
        "xrpl_weighted_inflow": float(weighted_inflow),
        "xrpl_ripple_otc": float(ripple_otc),
    }


@st.cache_data(ttl=180, show_spinner=False)
def load_account_snapshot(address: str) -> Dict[str, Any]:
    return fetch_account_overview(address)


# =========================
# Presentation
# =========================


def render_market_header(price: Dict[str, Optional[float]], flows: Dict[str, Any]) -> None:
    col1, col2, col3, col4 = st.columns([1.5, 1, 1, 1])

    price_val = price.get("price")
    change_val = price.get("change")
    price_text = f"${price_val:,.4f}" if price_val else "–"
    change_text = "–"
    if change_val is not None:
        change_text = f"{change_val:+.2f}% 24h"
    styled_metric("Spot", price_text, change_text)

    with col2:
        inflow = flows.get("latest_inflow") or 0.0
        styled_metric("XRPL → Exchanges", f"{inflow:,.0f} XRP", "Last observed slice")

    with col3:
        outflow = flows.get("latest_outflow") or 0.0
        styled_metric("Exchanges → XRPL", f"{outflow:,.0f} XRP", "Last observed slice")

    with col4:
        meta = flows.get("meta", {}) or {}
        # Prefer a 'timestamp' field when available, but fall back to the legacy
        # 'updated_at' key written by the inflow worker. Without this fallback
        # the flow heartbeat shows "unknown" even when inflow snapshots are fresh.
        source = meta.get("provider", "unknown") or "unknown"
        ts_raw = meta.get("timestamp") or meta.get("updated_at")
        ts = to_datetime(ts_raw)
        styled_metric(
            "Flow heartbeat",
            source.title(),
            f"{time_ago(ts)} via {source}"
        )


def render_signal_panel(
    stack: Dict[str, Any],
    reasons: List[str],
    adjusted_composite: float,
    reason_adjustment_pts: float,
    fallback_notes: List[str],
) -> None:
    st.markdown("### Signals & Conviction")

    raw_composite = stack.get("composite", 0.0)
    raw_conviction = stack.get("probability", 0.0) * 100.0
    adj_conviction = calibrated_conviction_probability(adjusted_composite) * 100.0
    coverage = stack.get("coverage", 0.0) or 0.0

    col_a, col_b, col_c = st.columns([1.2, 1.1, 1])
    with col_a:
        styled_metric(
            "Conviction (adj)",
            f"{adjusted_composite:.1f} / 100",
            f"Qual tilt {reason_adjustment_pts:+.1f} pts · {adj_conviction:.1f}% calibrated",
        )
        st.progress(adjusted_composite / 100.0)

    with col_b:
        styled_metric(
            "Quant-only composite",
            f"{raw_composite:.1f} / 100",
            f"{raw_conviction:.1f}% calibrated · Coverage {coverage:.1f} pts",
        )
        st.progress(raw_composite / 100.0)

    missing = [d for d in stack.get("details", []) if not d.get("available")]
    if missing or fallback_notes:
        with col_c:
            if missing:
                st.info(
                    "Signals rescaled to available inputs; missing feeds: "
                    + ", ".join(d.get("name", d.get("key", "")) for d in missing)
                )
            if fallback_notes:
                st.caption("Fallbacks → " + " | ".join(fallback_notes))

    if reasons:
        st.markdown("#### Why now (reason codes)")
        st.markdown(
            " ".join(
                f"<span class='tag tag-ok'>{reason}</span>" for reason in reasons
            ),
            unsafe_allow_html=True,
        )

    for detail in stack.get("details", []):
        max_points = detail.get("max_points") or 1.0
        fill = max(0.0, min(1.0, (detail.get("points", 0.0) / max_points)))
        badge_cls = "tag-ok" if detail.get("available") else "tag-warn"
        st.markdown(
            f"""
            <div class="metric" style="margin-top:10px;">
                <h3>{detail.get("name")}</h3>
                <div class="value">{detail.get("points", 0.0):.1f} / {max_points:.1f} pts</div>
                <div class="note">
                    <span class="tag {badge_cls}" style="margin-right:6px;">{detail.get("status")}</span>
                    {detail.get("note", "")}
                </div>
                <div style="height:8px; background:{PALETTE['panel']}; border-radius:999px; overflow:hidden; margin-top:10px;">
                    <div style="width:{fill*100:.0f}%; height:100%; background:{PALETTE['accent']};"></div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_price_panel(history: Optional[List[Tuple[datetime, float]]]) -> None:
    st.markdown("### Market Structure")
    if not history:
        st.info("Price history unavailable from CoinGecko.")
        return

    times, prices = zip(*history)
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=list(times), y=list(prices), mode="lines", line=dict(color=PALETTE["accent"], width=3))
    )
    fig.update_layout(
        height=300,
        margin=dict(l=20, r=20, t=10, b=10),
        paper_bgcolor=PALETTE["bg"],
        plot_bgcolor=PALETTE["bg"],
        font=dict(color=PALETTE["text"]),
        yaxis_title="USD",
    )
    st.plotly_chart(fig, use_container_width=True)


def render_liquidity_panel(futures: Dict[str, Optional[float]], flows: Dict[str, Any]) -> None:
    st.markdown("### Liquidity & Funding")
    col1, col2, col3 = st.columns(3)

    with col1:
        funding = futures.get("funding")
        note = "Binance perp funding"
        styled_metric("Funding", f"{funding:+.4f}%" if funding is not None else "–", note)

    with col2:
        # Prefer aggregated open interest if available; fall back to the
        # Binance-only snapshot. Display a contextually appropriate note.
        agg_oi = futures.get("aggregated_open_interest")
        oi = futures.get("open_interest")
        value = agg_oi if agg_oi is not None else oi
        note = "Aggregated across major venues" if agg_oi is not None else "Binance USDⓈ-M"
        styled_metric("Open Interest", f"${value:,.0f}" if value else "–", note)

    with col3:
        inflow = flows.get("latest_inflow") or 0.0
        outflow = flows.get("latest_outflow") or 0.0
        balance = inflow - outflow
        label = "Net inflow last poll"
        color = PALETTE["accent"] if balance >= 0 else PALETTE["alert"]
        st.markdown(
            f"<div class='metric' style='border-left:4px solid {color};'>"
            f"<h3>Flow Skew</h3><div class='value'>{balance:,.0f} XRP</div>"
            f"<div class='note'>{label}</div></div>",
            unsafe_allow_html=True,
        )


def render_sentiment_panel(sent: Dict[str, Any]) -> None:
    st.markdown("### News & Sentiment")
    col1, col2 = st.columns([1, 1])

    with col1:
        instant = sent.get("instant", 0.0)
        ema = sent.get("ema")
        ts = sent.get("timestamp")
        styled_metric(
            "FinBERT Instant",
            f"{instant:+.2f}",
            f"EMA {ema:+.2f} · {time_ago(ts)}" if ema is not None else f"{time_ago(ts)}",
        )

    with col2:
        bull = sent.get("bull", 0.0)
        bear = sent.get("bear", 0.0)
        skew = bull - bear
        styled_metric("Bull-Bear Skew", f"{skew:+.2f}", f"Bull {bull:.2f} | Bear {bear:.2f}")

    articles = sent.get("articles", [])
    if articles:
        st.markdown("#### Headlines driving sentiment")
        for art in articles[:6]:
            title = art.get("title") or "Untitled"
            source = art.get("source") or "Unknown"
            weight = art.get("weight", "")
            st.write(f"• **{title}** — {source} (w={weight})")
    else:
        st.info("No cached headlines in Redis.")


def render_health_panel(creds: Dict[str, str], price: Dict[str, Optional[float]], sent: Dict[str, Any]) -> None:
    st.markdown("### System Health")
    tags: List[str] = []

    def pill(text: str, cls: str) -> str:
        return f"<span class='tag {cls}'>{text}</span>"

    if price.get("price") is not None:
        tags.append(pill("CoinGecko ok", "tag-ok"))
    else:
        tags.append(pill("CoinGecko stale", "tag-warn"))

    if sent.get("timestamp"):
        tags.append(pill("Sentiment cache", "tag-ok"))
    else:
        tags.append(pill("Sentiment missing", "tag-warn"))

    if creds.get("binance_key") and creds.get("binance_secret"):
        tags.append(pill("Binance keys loaded", "tag-ok"))
    else:
        tags.append(pill("Binance keys not set", "tag-warn"))

    st.markdown("""<div style='margin-bottom:8px;'>""" + "".join(tags) + "</div>", unsafe_allow_html=True)

    st.write("**Credentials (redacted)**")
    st.code(
        "\n".join(
            [
                f"BINANCE_API_KEY={redact_secret(creds.get('binance_key', ''))}",
                f"BINANCE_API_SECRET={redact_secret(creds.get('binance_secret', ''))}",
                f"NEWS_API_KEY={redact_secret(creds.get('news_api', ''))}",
                f"WHALE_ALERT_KEY={redact_secret(creds.get('whale_alert', ''))}",
            ]
        ),
        language="bash",
    )


def render_account_panel(
    address: Optional[str],
    tag: Optional[int],
    overview: Dict[str, Any],
    notes: List[str],
) -> None:
    st.markdown("### XRPL Account Intelligence")

    if notes:
        st.caption(" | ".join(notes))

    if not address:
        st.info("Enter a classic address or X-address with tag to inspect balances and flows.")
        return

    if overview.get("account") is None:
        st.warning("XRPL account snapshot unavailable (offline mode or invalid address).")
        return

    account_data = overview.get("account") or {}
    balance = drops_to_xrp(account_data.get("Balance"))
    reserve = account_data.get("OwnerCount") or 0
    sequence = account_data.get("Sequence")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        styled_metric("Balance", f"{balance:,.2f} XRP", f"Seq {sequence or '-'}")
    with col_b:
        styled_metric("Owner Count", f"{reserve}", "Objects impacting reserve")
    with col_c:
        styled_metric("Tag", str(tag) if tag is not None else "–", "Destination tag (optional)")

    trustlines = overview.get("trustlines") or []
    offers = overview.get("offers") or []
    txs = overview.get("transactions") or []

    with st.expander("Trustlines & AMM exposures", expanded=False):
        if trustlines:
            st.dataframe(
                [
                    {
                        "Counterparty": tl.get("account"),
                        "Currency": tl.get("currency"),
                        "Balance": tl.get("balance"),
                        "Limit": tl.get("limit"),
                    }
                    for tl in trustlines[:8]
                ],
                hide_index=True,
            )
        else:
            st.info("No trustlines reported for this account.")

    with st.expander("Offers & AMM depth", expanded=False):
        if offers:
            st.dataframe(offers[:8], hide_index=True)
        else:
            st.info("No standing offers detected.")

    with st.expander("Latest successful transactions", expanded=True):
        if txs:
            for tx in txs[:6]:
                ts = to_datetime(tx.get("date"))
                st.write(
                    f"• {tx.get('type') or 'Transaction'} — {tx.get('amount')} "
                    f"vs {tx.get('counterparty', 'unknown')} ({time_ago(ts) if ts else 'n/a'})"
                )
        else:
            st.info("No recent successful transactions returned by Ripple Data.")


# =========================
# Layout
# =========================


DEFAULT_REFRESH = int(os.getenv("META_REFRESH_SECONDS", "60"))
set_state("refresh_enabled", get_state("refresh_enabled", True))
set_state("refresh_seconds", get_state("refresh_seconds", DEFAULT_REFRESH))

with st.sidebar:
    st.header("Console Controls")
    refresh_enabled = get_state("refresh_enabled", True)
    refresh_seconds = get_state("refresh_seconds", DEFAULT_REFRESH)

    refresh_enabled = st.checkbox(
        "Auto-refresh",
        value=refresh_enabled,
        help="Disable to freeze state for investigation.",
    )
    refresh_seconds = st.slider(
        "Refresh cadence (s)",
        min_value=20,
        max_value=180,
        step=5,
        value=refresh_seconds,
    )
    set_state("refresh_enabled", refresh_enabled)
    set_state("refresh_seconds", refresh_seconds)
    st.caption("CoinGecko requests are rate-limited; avoid aggressive refresh in production.")
    if st.button("Manual refresh", type="primary"):
        st.experimental_rerun()

    st.subheader("XRPL Account Inspector")
    default_account = normalize_env_value("XRPL_INSPECT_ACCOUNT")
    account_input = st.text_input(
        "Classic address or X-address", value=default_account, placeholder="r..."
    )
    tag_raw = st.text_input("Destination tag (optional)", value="")
    tag_value: Optional[int] = None
    if tag_raw.strip():
        if tag_raw.strip().isdigit():
            tag_value = int(tag_raw.strip())
        else:
            st.warning("Destination tags must be numeric.")

refresh_enabled = get_state("refresh_enabled", True)
refresh_seconds = get_state("refresh_seconds", DEFAULT_REFRESH)

if refresh_enabled:
    st.markdown(
        f'<meta http-equiv="refresh" content="{refresh_seconds}" />',
        unsafe_allow_html=True,
    )

st.title("XRP Quant Governance Console")
st.caption(
    "Institutional-grade monitoring of price, funding, liquidity, and sentiment — engineered for repeatability and auditability."
)

account_address, account_tag, account_notes = parse_account_input(account_input, tag_value)

creds = load_api_credentials()
price_snapshot = fetch_price_snapshot()
price_history = fetch_price_history()
futures = fetch_funding_and_oi()
flows = fetch_xrpl_flows()
sentiment = fetch_sentiment()
fallback_notes = hydrate_with_db_fallbacks(price_snapshot, futures, flows, sentiment)
signal_stack = compute_signal_stack(price_snapshot, futures, flows, sentiment)
reason_inputs = signal_stack.get("reason_inputs", {}) or {}
if "netflow_xrp" not in reason_inputs:
    inflow_val = flows.get("latest_inflow") if isinstance(flows, dict) else None
    outflow_val = flows.get("latest_outflow") if isinstance(flows, dict) else None
    if inflow_val is not None and outflow_val is not None:
        reason_inputs["netflow_xrp"] = float(outflow_val) - float(inflow_val)
reasons = derive_reason_codes(reason_inputs)
reason_adjustment_pts = reason_score_adjustment(reasons)
coverage_budget = signal_stack.get("coverage", 0.0) or 0.0
raw_composite = signal_stack.get("composite", 0.0) or 0.0
if coverage_budget > 0:
    adjusted_composite = max(
        0.0, min(100.0, raw_composite + (reason_adjustment_pts / coverage_budget) * 100.0)
    )
else:
    adjusted_composite = raw_composite

account_overview = (
    load_account_snapshot(account_address)
    if account_address
    else {"account": None, "trustlines": [], "transactions": [], "offers": []}
)

render_market_header(price_snapshot, flows)
st.divider()

col_left, col_right = st.columns([1.4, 1])
with col_left:
    render_signal_panel(
        signal_stack, reasons, adjusted_composite, reason_adjustment_pts, fallback_notes
    )
    st.divider()
    render_price_panel(price_history)
    st.divider()
    render_liquidity_panel(futures, flows)

with col_right:
    render_sentiment_panel(sentiment)
    st.divider()
    render_health_panel(creds, price_snapshot, sentiment)

st.divider()
render_account_panel(account_address, account_tag, account_overview, account_notes)

st.markdown("""
---
**Stewardship note:** All external data is polled best-effort. Binance credentials remain on-disk only; nothing is persisted beyond the session. XRPL inflow/outflow telemetry assumes the associated worker is running.\
\
For production hardening, pin upstream dependencies and run behind a private Redis instance.
""")
