# main.py — Streamlit dashboard (full features - FIXED CHART ALIGNMENT)
import os
import hmac
import hashlib
import time
from urllib.parse import urlencode
from datetime import datetime
import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
from redis_client import rdb

st.set_page_config(page_title="XRP Engine v8.4", layout="wide", initial_sidebar_state="collapsed")
st.title("XRP REVERSAL & BREAKOUT ENGINE v8.4")
st.markdown("<p style='text-align: center; color: #00ff88; font-size:18px;'>Real Binance Netflow • XRPL inflows News Sentiment (cached) Market refresh 45s News refresh 30m</p>", unsafe_allow_html=True)

# Client-side non-blocking refresh
META_REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
st.markdown(f'<meta http-equiv="refresh" content="{META_REFRESH_SECONDS}">', unsafe_allow_html=True)
REQUEST_TIMEOUT = 10

# ------------------------- #
# OHLC + volume (cached)   #
# ------------------------- #
@st.cache_data(ttl=300)
def fetch_ohlc_volume():
    try:
        ohlc_raw = requests.get(
            "https://api.coingecko.com/api/v3/coins/ripple/ohlc",
            params={"vs_currency": "usd", "days": 90},
            timeout=REQUEST_TIMEOUT,
        )
        if not ohlc_raw.ok:
            return pd.DataFrame(), pd.DataFrame()
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
            volume = pd.DataFrame()
        else:
            vol_j = vol_raw.json()
            volume = pd.DataFrame(vol_j.get("total_volumes", []), columns=["ts", "volume"])
            volume["date_full"] = pd.to_datetime(volume["ts"], unit="ms")
    except Exception:
        volume = pd.DataFrame()

    return ohlc, volume

ohlc, volume_daily = fetch_ohlc_volume()

# ------------------------- #
# Live market data         #
# ------------------------- #
def fetch_live():
    result = {
        "price": None, "funding_now_pct": 0.0, "funding_hist_pct": [], "oi_usd": None,
        "long_short_ratio": 1.0, "binance_netflow_24h": None, "net_whale_flow": 0.0,
    }
    # price
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "ripple", "vs_currencies": "usd"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            result["price"] = r.json().get("ripple", {}).get("usd")
    except Exception: pass

    # funding
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            result["funding_now_pct"] = float(r.json().get("lastFundingRate", 0.0)) * 100.0
    except Exception: pass

    # open interest
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                         params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            oi_val = float(r.json().get("openInterest", 0.0))
            if result["price"] is not None:
                result["oi_usd"] = oi_val * result["price"]
            else:
                result["oi_usd"] = oi_val
    except Exception: pass

    # funding history
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": "XRPUSDT", "limit": 200}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            arr = [float(x.get("fundingRate", 0.0)) * 100.0 for x in r.json()[-90:]]
            result["funding_hist_pct"] = arr
    except Exception: pass

    # long/short
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                         params={"symbol": "XRPUSDT", "period": "5m", "limit": 1}, timeout=REQUEST_TIMEOUT)
        if r.ok and isinstance(r.json(), list) and r.json():
            result["long_short_ratio"] = float(r.json()[0].get("longShortRatio", 1.0))
    except Exception: pass

    # Binance signed netflow (optional API keys)
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if api_key and api_secret:
        try:
            base = "https://api.binance.com"
            ts = int(time.time() * 1000)
            start = ts - 86400000
            # deposits
            params_dep = {"coin": "XRP", "startTime": start}
            qry = urlencode({**params_dep, "timestamp": ts})
            sig = hmac.new(api_secret.encode(), qry.encode(), hashlib.sha256).hexdigest()
            dep = requests.get(f"{base}/sapi/v1/capital/deposit/hisrec?{urlencode(params_dep)}&timestamp={ts}&signature={sig}",
                               headers={"X-MBX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT).json()
            # withdrawals
            params_wd = {"coin": "XRP", "startTime": start}
            qry2 = urlencode({**params_wd, "timestamp": ts})
            sig2 = hmac.new(api_secret.encode(), qry2.encode(), hashlib.sha256).hexdigest()
            wd = requests.get(f"{base}/sapi/v1/capital/withdraw/history?{urlencode(params_wd)}&timestamp={ts}&signature={sig2}",
                              headers={"X-MBX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT).json()

            dep_amt = sum(float(d.get("amount", 0)) for d in (dep or []) if int(d.get("status", 0) or 0) == 1)
            wd_amt = sum((float(w.get("amount", 0)) - float(w.get("transactionFee", 0))) for w in (wd or []) if int(w.get("status", 0) or 0) == 6)
            result["binance_netflow_24h"] = wd_amt - dep_amt
        except Exception: pass

    # XRPL inflows from Redis
    try:
        inflow_json = rdb.get("xrpl:latest_inflows")
        if inflow_json:
            inflows = eval(inflow_json) if isinstance(inflow_json, str) else inflow_json
            result["net_whale_flow"] = sum(i.get("xrp", 0.0) for i in inflows)
    except Exception: pass

    return result

live = fetch_live()

# ------------------------- #
# News sentiment           #
# ------------------------- #
import json as _json
def read_sentiment():
    try:
        raw = rdb.get("news:sentiment")
        return _json.loads(raw) if raw else {"score": 0.0, "count": 0, "articles": [], "timestamp": None}
    except Exception:
        return {"score": 0.0, "count": 0, "articles": [], "timestamp": None}

news_payload = read_sentiment()
news_sent = news_payload.get("score", 0.0)

# ------------------------- #
# Scoring                  #
# ------------------------- #
fund_hist = live.get("funding_hist_pct", []) or [0.0]
fund_now = live.get("funding_now_pct", 0.0) or 0.0
fund_z = (fund_now - np.mean(fund_hist)) / (np.std(fund_hist) if np.std(fund_hist) > 1e-8 else 1e-8)

whale_z = (live.get("net_whale_flow", 0.0) or 0.0) / 60e6
netflow_z = (live.get("binance_netflow_24h", 0.0) or 0.0) / 100e6
lsr_z = max(0, (2.0 - (live.get("long_short_ratio") or 1.0)) / 1.0)

points = {
    "Funding Z-Score": max(0, fund_z * 22),
    "Whale Flow Bullish": max(0, whale_z * 14),
    "Price < $2.45": 28 if (live.get("price") or 0.0) < 2.45 else 0,
    "OI > $2.7B": 16 if (live.get("oi_usd") or 0.0) > 2.7e9 else 0,
    "Binance Netflow Bullish": max(0, netflow_z * 30),
    "Short Squeeze Setup": lsr_z * 20,
    "Positive News": 15 if (news_sent or 0.0) > 0.2 else 0,
}
total_score = min(100, sum(points.values()))

# ------------------------- UI Metrics ----------#
st.markdown("### Live Metrics")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("XRP Price", f"${(live.get('price') or 0.0):.4f}")
c2.metric("Funding Rate", f"{(live.get('funding_now_pct') or 0.0):+.4f}%")
c3.metric("Open Interest", f"${((live.get('oi_usd') or 0.0)/1e9):.2f}B")
c4.metric("L/S Ratio", f"{(live.get('long_short_ratio') or 1.0):.2f}")
c5.metric("News Sentiment (cached)", f"{(news_sent or 0.0):+.3f}", delta=f"{news_payload.get('count',0)} articles")
c6.metric("XRPL Exchange Inflows (last push)", f"{(live.get('net_whale_flow') or 0.0):+.1f}M")

# Score & Signal
score_col, signal_col = st.columns([1,2])
with score_col:
    if total_score >= 80:
        color, signal = "#00aa44", "STRONG BUY — REVERSAL LIKELY"
    elif total_score >= 65:
        color, signal = "#00cc88", "ACCUMULATION — BULLISH"
    elif total_score <= 35:
        color, signal = "#cc3344", "DISTRIBUTION — CAUTION"
    else:
        color, signal = "#444444", "NEUTRAL — WAIT FOR SETUP"
    st.markdown(f'<p style="font-size:86px;color:{color};text-align:center;font-weight:bold;margin-top:20px;">{total_score:.0f}</p>', unsafe_allow_html=True)
with signal_col:
    st.markdown(f'<h2 style="color:{color};margin-top:30px;">{signal}</h2>', unsafe_allow_html=True)

st.write("Score breakdown (points):")
for k, v in points.items():
    st.write(f"{k}: {v:.1f}")

# Raw inputs table
st.markdown("**Live Signal Breakdown (raw inputs)**")
components = {
    "Funding Now (%)": live.get("funding_now_pct"),
    "Funding Z-Score (raw)": round(float(fund_z), 4),
    "Whale Flow (M)": round((live.get("net_whale_flow") or 0.0), 3),
    "Binance Netflow 24h (XRP)": live.get("binance_netflow_24h"),
    "Open Interest (USD)": live.get("oi_usd"),
    "Long/Short Ratio": live.get("long_short_ratio"),
    "News Sentiment (cached)": news_payload.get("score"),
    "News Count": news_payload.get("count"),
    "Sentiment ts": news_payload.get("timestamp"),
}
for k, v in components.items():
    a, b = st.columns([3,1])
    a.write(k)
    b.write(str(v) if v is not None else "n/a")

# ------------------------- #
# FIXED CHART - PERFECT ALIGNMENT
# ------------------------- #
st.markdown("### 90-Day XRP Chart")

if not ohlc.empty and not volume_daily.empty:
    # Round both to daily for merging (OHLC is 4h, volume is daily)
    ohlc_daily = ohlc.copy()
    ohlc_daily["date"] = ohlc_daily["date_full"].dt.floor('D')
    volume_daily["date"] = volume_daily["date_full"].dt.floor('D')

    # Aggregate OHLC to daily candles
    daily_ohlc = ohlc_daily.groupby("date").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "date_full": "last"  # take last timestamp of the day for plotting
    }).reset_index()

    # Merge volume
    df = pd.merge(daily_ohlc, volume_daily[["date", "volume"]], on="date", how="left")
    df["volume"] = df["volume"].fillna(0)
    df = df.sort_values("date")  # ensure chronological order

    fig = go.Figure()

    # Candlesticks
    fig.add_trace(go.Candlestick(
        x=df["date_full"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name="XRP",
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350'
    ))

    # Volume bars (aligned perfectly)
    fig.add_trace(go.Bar(
        x=df["date_full"],
        y=df["volume"] / 1e9,
        name="Volume (B USD)",
        marker_color='rgba(100, 150, 255, 0.5)',
        yaxis="y2"
    ))

    fig.update_layout(
        height=700,
        template="plotly_dark",
        xaxis=dict(title="", rangeslider_visible=False),
        yaxis=dict(title="Price (USD)", domain=[0.3, 1.0]),
        yaxis2=dict(
            title="Volume (B USD)",
            domain=[0.0, 0.25],
            overlaying="y",
            side="right"
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=50, r=50, t=50, b=50),
        hovermode="x unified"
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.write("OHLC/volume data unavailable — check CoinGecko or cache.")

# ------------------------- #
# Footer                   #
# ------------------------- #
st.caption("v8.4 • Fixed volume alignment • Market refresh every 45s • News sentiment updated every 30m • XRPL inflows detected by worker")
