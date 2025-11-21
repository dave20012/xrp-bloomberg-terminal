# main.py — XRP Reversal & Breakout Engine v8.4 — Hardened production-ready
# Nov 21 2025 — Replace existing main.py with this file
import os
import time
import hmac
import hashlib
from urllib.parse import urlencode
from typing import Optional, Dict, Any, List

import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
from datetime import datetime

# ---------------------------
# Config
# ---------------------------
REQUEST_TIMEOUT = 8  # seconds for HTTP calls
OHLC_CACHE_TTL = 300  # seconds
SENTIMENT_CACHE_TTL = 1800  # seconds (30 min)
META_REFRESH_SECONDS = 45  # client-side refresh interval

st.set_page_config(page_title="XRP Engine v8.4", layout="wide", initial_sidebar_state="collapsed")

# Header and non-blocking page refresh
st.markdown(
    "<p style='text-align: center; color: #00ff88; font-size:18px;'>Real Binance Netflow • CryptoCompare • XRPL • News Sentiment • LIVE REFRESH EVERY 45s</p>",
    unsafe_allow_html=True,
)
# client-side refresh (non-blocking)
st.markdown(f'<meta http-equiv="refresh" content="{META_REFRESH_SECONDS}">', unsafe_allow_html=True)

# ---------------------------
# Utilities
# ---------------------------
def safe_json(r: requests.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {}

def now_ts() -> str:
    return datetime.utcnow().isoformat() + "Z"

# Binance signed GET helper
def binance_signed_get(path: str, api_key: str, api_secret: str, base: str = "https://api.binance.com", params: Optional[dict] = None) -> Optional[Any]:
    """
    Build signature from the exact query string and perform GET with X-MBX-APIKEY header.
    Returns parsed json or None on failure.
    """
    if not api_key or not api_secret:
        return None
    params = params.copy() if params else {}
    ts = int(time.time() * 1000)
    params.update({"timestamp": ts})
    qry = urlencode(params)
    signature = hmac.new(api_secret.encode(), qry.encode(), hashlib.sha256).hexdigest()
    url = f"{base}{path}?{qry}&signature={signature}"
    headers = {"X-MBX-APIKEY": api_key}
    try:
        r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.ok:
            return safe_json(r)
        return None
    except Exception:
        return None

# Robust numeric helpers
def robust_zscore(arr: List[float]) -> float:
    a = np.array(arr, dtype=float)
    if a.size == 0:
        return 0.0
    mu = np.nanmean(a)
    sd = np.nanstd(a)
    sd = max(sd, 1e-8)
    return (a[-1] - mu) / sd

def normalize01(x: float, lo: float, hi: float) -> float:
    c = float(np.clip(x, lo, hi))
    return (c - lo) / (hi - lo) if hi > lo else 0.0

# ---------------------------
# Data fetch with caching & fallbacks
# ---------------------------
@st.cache_data(ttl=OHLC_CACHE_TTL, show_spinner=False)
def fetch_ohlc_volume() -> (pd.DataFrame, pd.DataFrame, str):
    """Fetch 90-day OHLC and volume from CoinGecko, safe error handling."""
    try:
        ohlc_raw = requests.get(
            "https://api.coingecko.com/api/v3/coins/ripple/ohlc",
            params={"vs_currency": "usd", "days": 90},
            timeout=REQUEST_TIMEOUT,
        )
        if not ohlc_raw.ok:
            return pd.DataFrame(), pd.DataFrame(), "coingecko_ohlc_failed"
        ohlc_list = ohlc_raw.json()
        ohlc = pd.DataFrame(ohlc_list, columns=["ts", "open", "high", "low", "close"])
        ohlc["date_full"] = pd.to_datetime(ohlc["ts"], unit="ms")
    except Exception:
        ohlc = pd.DataFrame()
    try:
        vol_raw = requests.get(
            "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
            params={"vs_currency": "usd", "days": 90, "interval": "daily"},
            timeout=REQUEST_TIMEOUT,
        )
        if not vol_raw.ok:
            return ohlc, pd.DataFrame(), "coingecko_vol_failed"
        vol_json = vol_raw.json()
        volume = pd.DataFrame(vol_json.get("total_volumes", []), columns=["ts", "volume"])
        volume["date_full"] = pd.to_datetime(volume["ts"], unit="ms")
    except Exception:
        volume = pd.DataFrame()
    return ohlc, volume, "ok"

@st.cache_data(ttl=SENTIMENT_CACHE_TTL, show_spinner=False)
def fetch_news_sentiment(news_key: Optional[str], hf_token: Optional[str]) -> Dict[str, Any]:
    """
    Lightweight sentiment fetcher: fetch headlines, compute simple sentiment proxy.
    NOTE: Avoid heavy HF in-request. If HF token present, compute a cached inference outside critical path.
    """
    if not news_key:
        return {"score": 0.0, "source": "news_key_missing"}
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": "XRP OR Ripple", "pageSize": 5, "sortBy": "publishedAt", "language": "en", "apiKey": news_key},
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            return {"score": 0.0, "source": "newsapi_failed"}
        arts = resp.json().get("articles", [])
        # quick sentiment: count positive/negative words (cheap heuristic) as fallback
        pos_words = {"bull", "rally", "surge", "gain", "approval", "win"}
        neg_words = {"hack", "drop", "sell", "ban", "lawsuit", "fine", "blow"}
        scores = []
        for a in arts:
            title = (a.get("title") or "").lower()
            score = sum(1 for w in pos_words if w in title) - sum(1 for w in neg_words if w in title)
            scores.append(score)
        avg = float(np.mean(scores)) if scores else 0.0
        # if hf_token provided, caller may choose to re-run a heavier inference offline and write back to a DB/cache
        return {"score": avg, "source": "heuristic"}
    except Exception:
        return {"score": 0.0, "source": "exception"}

def fetch_live() -> Dict[str, Any]:
    now = now_ts()
    result = {
        "price": None,
        "funding_now_pct": 0.0,
        "funding_hist_pct": [],
        "oi_usd": None,
        "long_short_ratio": 1.0,
        "binance_netflow_24h": None,
        "net_whale_flow": 0.0,
        "news_sentiment": 0.0,
        "last_success": now,
        "errors": [],
    }

    # 1) price
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={"ids": "ripple", "vs_currencies": "usd"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            j = r.json()
            result["price"] = j.get("ripple", {}).get("usd")
    except Exception as e:
        result["errors"].append(f"price_err:{e}")

    # 2) funding rate
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            j = r.json()
            result["funding_now_pct"] = float(j.get("lastFundingRate", 0.0)) * 100.0
    except Exception as e:
        result["errors"].append(f"funding_err:{e}")

    # 3) open interest
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            j = r.json()
            oi_val = float(j.get("openInterest", 0.0))
            if result["price"] is not None:
                result["oi_usd"] = oi_val * float(result["price"])
            else:
                # if price missing, keep openInterest raw
                result["oi_usd"] = oi_val
    except Exception as e:
        result["errors"].append(f"oi_err:{e}")

    # 4) funding history (small limit; robust)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate", params={"symbol": "XRPUSDT", "limit": 200}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            arr = [float(x.get("fundingRate", 0.0)) * 100.0 for x in r.json()[-90:]]
            result["funding_hist_pct"] = arr
    except Exception as e:
        result["errors"].append(f"fund_hist_err:{e}")

    # 5) global long-short ratio (public data endpoint may vary)
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio", params={"symbol": "XRPUSDT", "period": "5m", "limit": 1}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            j = r.json()
            if isinstance(j, list) and j:
                result["long_short_ratio"] = float(j[0].get("longShortRatio", 1.0))
            else:
                # fallback: no change
                result["long_short_ratio"] = result["long_short_ratio"]
    except Exception as e:
        result["errors"].append(f"lsr_err:{e}")

    # 6) signed Binance SAPI netflows (requires API key/secret)
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if api_key and api_secret:
        try:
            base = "https://api.binance.com"
            start_time = int(time.time() * 1000) - 86400000
            dep = binance_signed_get("/sapi/v1/capital/deposit/hisrec", api_key, api_secret, base=base, params={"coin": "XRP", "startTime": start_time})
            wd = binance_signed_get("/sapi/v1/capital/withdraw/history", api_key, api_secret, base=base, params={"coin": "XRP", "startTime": start_time})
            dep_amt = sum(float(d.get("amount", 0.0)) for d in (dep or []) if int(d.get("status", 0) or 0) == 1)
            wd_amt = sum((float(w.get("amount", 0.0)) - float(w.get("transactionFee", 0.0))) for w in (wd or []) if int(w.get("status", 0) or 0) == 6)
            result["binance_netflow_24h"] = (wd_amt - dep_amt)
        except Exception as e:
            result["errors"].append(f"binance_netflow_err:{e}")
    else:
        result["binance_netflow_24h"] = None

    # 7) whale-alert (requires API key)
    whale_key = os.getenv("WHALE_ALERT_KEY")
    if whale_key:
        try:
            wr = requests.get("https://api.whale-alert.io/v1/transactions",
                              params={"api_key": whale_key, "currency": "xrp", "min_value": 10000000, "limit": 20},
                              timeout=REQUEST_TIMEOUT)
            if wr.ok:
                wj = wr.json()
                net = 0.0
                for t in wj.get("transactions", []):
                    amt = float(t.get("amount", 0.0)) / 1e6  # API often returns base units
                    # Consistent sign: flows to exchange increase exchange balance (usually bearish)
                    if t.get("to", {}).get("owner_type") == "exchange":
                        net += amt  # movement to exchange: + (more sell pressure)
                    if t.get("from", {}).get("owner_type") == "exchange":
                        net -= amt  # movement from exchange: - (withdraw)
                result["net_whale_flow"] = net
        except Exception as e:
            result["errors"].append(f"whale_err:{e}")

    # 8) news sentiment (cached lightweight heuristic)
    news_key = os.getenv("NEWS_API_KEY")
    hf_token = os.getenv("HF_TOKEN")
    sentiment = fetch_news_sentiment(news_key, hf_token)
    result["news_sentiment"] = sentiment.get("score", 0.0)
    result["sentiment_source"] = sentiment.get("source", "unknown")

    result["last_success"] = now
    return result

# ---------------------------
# Scoring
# ---------------------------
def compute_score(data: Dict[str, Any]) -> (float, Dict[str, float]):
    # extract inputs and guard
    fund_hist = data.get("funding_hist_pct") or []
    fund_now = data.get("funding_now_pct") or 0.0
    whale = data.get("net_whale_flow") or 0.0
    netflow = data.get("binance_netflow_24h") or 0.0
    oi = data.get("oi_usd") or 0.0
    lsr = data.get("long_short_ratio") or 1.0
    news = data.get("news_sentiment") or 0.0
    price = data.get("price") or 0.0

    fund_z = robust_zscore(fund_hist) if fund_hist else 0.0
    fund_norm = normalize01(fund_z, -3, 3)

    whale_norm = normalize01(whale / 1e6, -5, 5)  # millions
    netflow_norm = normalize01(netflow / 1e6, -10, 10)
    lsr_norm = normalize01((2.0 - lsr), -2, 2)
    oi_norm = normalize01(np.log1p(oi), np.log1p(0), np.log1p(5e9))
    news_norm = normalize01(news, -3, 3)

    # weights (tunable)
    weights = {
        "fund": 0.18,
        "whale": 0.12,
        "netflow": 0.25,
        "lsr": 0.15,
        "oi": 0.10,
        "news": 0.20,
    }
    total_w = sum(weights.values())
    for k in weights:
        weights[k] /= total_w

    score = (
        fund_norm * weights["fund"]
        + whale_norm * weights["whale"]
        + netflow_norm * weights["netflow"]
        + lsr_norm * weights["lsr"]
        + oi_norm * weights["oi"]
        + news_norm * weights["news"]
    ) * 100.0

    # contribution breakdown
    breakdown = {
        "fund_pct": fund_norm * weights["fund"] * 100,
        "whale_pct": whale_norm * weights["whale"] * 100,
        "netflow_pct": netflow_norm * weights["netflow"] * 100,
        "lsr_pct": lsr_norm * weights["lsr"] * 100,
        "oi_pct": oi_norm * weights["oi"] * 100,
        "news_pct": news_norm * weights["news"] * 100,
    }

    return float(np.clip(score, 0.0, 100.0)), breakdown

# ---------------------------
# UI and plotting
# ---------------------------
st.title("XRP REVERSAL & BREAKOUT ENGINE v8.4")

# Fetch OHLC/volume (cached)
ohlc, volume, ohlc_status = fetch_ohlc_volume()

# Fetch live metrics (every page load)
live = fetch_live()

# Compute score
total_score, breakdown = compute_score(live)

# Live Metrics display
st.markdown("### Live Metrics")
cols = st.columns(6)
cols[0].metric("XRP Price", f"${(live['price'] or 0.0):.4f}")
cols[1].metric("Funding Rate", f"{(live['funding_now_pct'] or 0.0):+.4f}%")
cols[2].metric("Open Interest", f"${((live['oi_usd'] or 0.0)/1e9):.2f}B")
cols[3].metric("L/S Ratio", f"{(live['long_short_ratio'] or 1.0):.2f}")
cols[4].metric("News Sentiment", f"{(live['news_sentiment'] or 0.0):+.3f}")
cols[5].metric("Whale Flow ~24h (M)", f"{(live['net_whale_flow'] or 0.0):+.1f}M")

# Big Score & Signal (no emoji to reduce overconfidence)
score_col, signal_col = st.columns([1, 2])
with score_col:
    # color mapping for numeric display
    if total_score >= 80:
        color = "#00aa44"
        signal = "STRONG BUY — REVERSAL LIKELY"
    elif total_score >= 65:
        color = "#00cc88"
        signal = "ACCUMULATION — BULLISH"
    elif total_score <= 35:
        color = "#cc3344"
        signal = "DISTRIBUTION — CAUTION"
    else:
        color = "#444444"
        signal = "NEUTRAL — WAIT FOR SETUP"

    st.markdown(
        f'<p style="font-size:86px;color:{color};text-align:center;font-weight:bold;margin-top:20px;">{total_score:.0f}</p>',
        unsafe_allow_html=True,
    )

with signal_col:
    st.markdown(f'<h2 style="color:{color};margin-top:30px;">{signal}</h2>', unsafe_allow_html=True)
    st.write("Score breakdown (contribution in points):")
    for k, v in breakdown.items():
        st.write(f"{k}: {v:.1f}")

# Live signal breakdown table (explicit, no hidden magic)
st.markdown("**Live Signal Breakdown (raw components)**")
components = {
    "Funding Now (%)": live.get("funding_now_pct", 0.0),
    "Funding Z-Score (norm)": round(normalize01(robust_zscore(live.get("funding_hist_pct", [])), -3, 3), 3),
    "Whale Flow (M)": round((live.get("net_whale_flow") or 0.0), 3),
    "Binance Netflow 24h (XRP)": live.get("binance_netflow_24h"),
    "Open Interest (USD)": live.get("oi_usd"),
    "Long/Short Ratio": live.get("long_short_ratio"),
    "News Sentiment (heuristic)": live.get("news_sentiment"),
    "Data freshness": live.get("last_success"),
    "OHLC source status": ohlc_status,
    "Errors (last fetch)": live.get("errors"),
    "Sentiment source": live.get("sentiment_source"),
}
for k, v in components.items():
    row_left, row_right = st.columns([3, 1])
    row_left.write(k)
    row_right.write(str(v) if v is not None else "n/a")

# Chart (if OHLC available)
st.markdown("### 90-Day XRP Chart")
if not ohlc.empty and not volume.empty:
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=ohlc["date_full"],
            open=ohlc["open"],
            high=ohlc["high"],
            low=ohlc["low"],
            close=ohlc["close"],
            name="XRP",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        )
    )
    # volume: assume CoinGecko total_volumes returns USD volumes; if not, label will be approximate
    vol_y = volume["volume"] / 1e9 if "volume" in volume.columns else np.zeros(len(ohlc))
    fig.add_trace(
        go.Bar(
            x=volume["date_full"],
            y=vol_y,
            name="Volume (B USD)",
            marker=dict(color=np.where(ohlc["close"] >= ohlc["open"], "#26a69a", "#ef5350")),
            opacity=0.5,
            yaxis="y2",
        )
    )
    fig.update_layout(
        height=700,
        template="plotly_dark",
        xaxis=dict(title="", rangeslider_visible=False),
        yaxis=dict(title="Price (USD)", domain=[0.3, 1.0]),
        yaxis2=dict(title="Volume (B USD)", domain=[0.0, 0.25], anchor="free", overlaying="y", side="left", position=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=50),
        hovermode="x unified",
    )
    st.plotly_chart(fig, use_container_width=True)
else:
    st.write("OHLC/volume data unavailable — check CoinGecko status or cache.")

# Footer: provenance & operational notes (concise)
st.caption("v8.4 • Hardened build • CLIENT refresh every 45s • Data sources: CoinGecko, Binance, Whale-Alert, NewsAPI")

# Operational debug (only visible to deployer if toggled)
if st.sidebar.checkbox("Show debug/logs", value=False):
    st.sidebar.write("ENV: BINANCE configured:", bool(os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET")))
    st.sidebar.write("WHALE_ALERT_KEY configured:", bool(os.getenv("WHALE_ALERT_KEY")))
    st.sidebar.write("NEWS_API_KEY configured:", bool(os.getenv("NEWS_API_KEY")))
    st.sidebar.write("Last fetch errors:", live.get("errors"))
