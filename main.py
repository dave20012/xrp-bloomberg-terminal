"""BlackRock-style XRP macro surveillance dashboard."""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import plotly.graph_objects as go
import requests
import streamlit as st

from app_utils import cache_get_json, normalize_env_value, safe_get
from redis_client import rdb

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
        return st.session_state.get(key, default)
    except Exception:  # noqa: BLE001
        return SESSION_FALLBACK.get(key, default)


def set_state(key: str, value: Any) -> None:
    try:
        st.session_state[key] = value
    except Exception:  # noqa: BLE001
        SESSION_FALLBACK[key] = value


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


# =========================
# Data acquisition
# =========================


COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_FAPI = "https://fapi.binance.com"
REQUEST_TIMEOUT = 12


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

    try:
        resp = requests.get(
            f"{BINANCE_FAPI}/futures/data/fundingRate",
            params={"symbol": "XRPUSDT", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok and resp.json():
            funding_raw = resp.json()[0]
            funding = float(funding_raw.get("fundingRate", 0.0))
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
            oi = float(oi_raw.get("sumOpenInterest", 0.0))
    except Exception:  # noqa: BLE001
        oi = None

    return {"funding": funding, "open_interest": oi}


def fetch_xrpl_flows() -> Dict[str, Any]:
    inflows = cache_get_json("xrpl:latest_inflows")
    outflows = cache_get_json("xrpl:latest_outflows")
    inflow_meta = cache_get_json("xrpl:latest_inflows_meta") or {}
    history = cache_get_json("xrpl:inflow_history")

    def _total_amount(flows: Any) -> float:
        if not isinstance(flows, list):
            return 0.0
        total = 0.0
        for entry in flows:
            try:
                total += float(entry.get("amount", 0.0))
            except Exception:  # noqa: BLE001
                continue
        return total

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
        meta = flows.get("meta", {})
        source = meta.get("provider", "unknown")
        ts = to_datetime(meta.get("timestamp"))
        styled_metric("Flow heartbeat", source.title(), f"{time_ago(ts)} via {source}")


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
        oi = futures.get("open_interest")
        styled_metric("Open Interest", f"${oi:,.0f}" if oi else "–", "Binance USDⓈ-M")

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

creds = load_api_credentials()
price_snapshot = fetch_price_snapshot()
price_history = fetch_price_history()
futures = fetch_funding_and_oi()
flows = fetch_xrpl_flows()
sentiment = fetch_sentiment()

render_market_header(price_snapshot, flows)
st.divider()

col_left, col_right = st.columns([1.4, 1])
with col_left:
    render_price_panel(price_history)
    st.divider()
    render_liquidity_panel(futures, flows)

with col_right:
    render_sentiment_panel(sentiment)
    st.divider()
    render_health_panel(creds, price_snapshot, sentiment)

st.markdown("""
---
**Stewardship note:** All external data is polled best-effort. Binance credentials remain on-disk only; nothing is persisted beyond the session. XRPL inflow/outflow telemetry assumes the associated worker is running.\
\
For production hardening, pin upstream dependencies and run behind a private Redis instance.
""")
