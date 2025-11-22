# main.py — XRP REVERSAL & BREAKOUT ENGINE v8.5 — FINAL BULLETPROOF VERSION
import os
import hmac
import hashlib
import time
from urllib.parse import urlencode
import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
from redis_client import rdb  # assuming you have this file

st.set_page_config(page_title="XRP Engine v8.5", layout="wide", initial_sidebar_state="collapsed")
st.title("XRP REVERSAL & BREAKOUT ENGINE v8.5")
st.markdown("<p style='text-align: center; color: #00ff88; font-size:18px;'>Real Binance Netflow • XRPL inflows • News Sentiment (cached) • Market refresh 45s • News refresh 30m</p>", unsafe_allow_html=True)

# Auto refresh
META_REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
st.markdown(f'<meta http-equiv="refresh" content="{META_REFRESH_SECONDS}">', unsafe_allow_html=True)
REQUEST_TIMEOUT = 10

# ========================= #
# OHLC + Volume — BULLETPROOF
# ========================= #
@st.cache_data(ttl=600)
def get_chart_data():
    # 1. Try CoinGecko market_chart (daily candles + volume)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
            params={"vs_currency": "usd", "days": "90", "interval": "daily"},
            timeout=10
        )
        if r.ok:
            data = r.json()
            prices = pd.DataFrame(data["prices"], columns=["ts", "price"])
            volumes = pd.DataFrame(data["total_volumes"], columns=["ts", "volume"])
            df = prices.copy()
            df["date"] = pd.to_datetime(df["ts"], unit="ms").dt.date
            df["open"] = df["price"]
            df["high"] = df["price"]
            df["low"] = df["price"]
            df["close"] = df["price"]
            df = df.merge(volumes, on="ts", how="left")
            df["volume"] = df["volume"].fillna(0)
            return df[["date", "open", "high", "low", "close", "volume"]]
    except:
        pass

    # 2. Fallback → Binance public daily klines (never rate-limited)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "XRPUSDT", "interval": "1d", "limit": 90},
            timeout=10
        )
        if r.ok:
            raw = r.json()
            df = pd.DataFrame(raw, columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_vol", "trades", "tb_base", "tb_quote", "ignore"
            ])
            df["date"] = pd.to_datetime(df["open_time"], unit="ms").dt.date
            df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
            return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        st.error(f"Both data sources failed: {e}")

    return pd.DataFrame()

# ========================= #
# Live market data
# ========================= #
def fetch_live():
    result = {
        "price": None, "funding_now_pct": 0.0, "funding_hist_pct": [], "oi_usd": None,
        "long_short_ratio": 1.0, "binance_netflow_24h": None, "net_whale_flow": 0.0,
    }

    # Price
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "ripple", "vs_currencies": "usd"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            result["price"] = r.json()["ripple"]["usd"]
    except: pass

    # Funding rate
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                         params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            result["funding_now_pct"] = float(r.json()["lastFundingRate"]) * 100
    except: pass

    # Open Interest
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                         params={"symbol": "XRPUSDT"}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            oi_contracts = float(r.json()["openInterest"])
            if result["price"]:
                result["oi_usd"] = oi_contracts * result["price"]
    except: pass

    # Funding history (for Z-score)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": "XRPUSDT", "limit": 200}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            rates = [float(x["fundingRate"]) * 100 for x in r.json()[-90:]]
            result["funding_hist_pct"] = rates
    except: pass

    # Long/Short ratio
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                         params={"symbol": "XRPUSDT", "period": "5m", "limit": 1}, timeout=REQUEST_TIMEOUT)
        if r.ok and r.json():
            result["long_short_ratio"] = float(r.json()[0]["longShortRatio"])
    except: pass

    # === BINANCE SIGNED NETFLOW — NOW WORKS ON RAILWAY SHARED VARS ===
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if api_key and api_secret and api_key.strip() and api_secret.strip():  # ← this was missing!
        try:
            ts = int(time.time() * 1000)
            start = ts - 86_400_000  # 24h
            base = "https://api.binance.com"

            # Deposits
            params = {"coin": "XRP", "startTime": start, "timestamp": ts}
            query_string = urlencode(params)
            signature = hmac.new(api_secret.encode(), query_string.encode(), hashlib.sha256).hexdigest()
            dep_url = f"{base}/sapi/v1/capital/deposit/hisrec?{query_string}&signature={signature}"
            dep = requests.get(dep_url, headers={"X-MBX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT).json()

            # Withdrawals
            wd_url = f"{base}/sapi/v1/capital/withdraw/history?{query_string}&signature={signature}"
            wd = requests.get(wd_url, headers={"X-MBX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT).json()

            dep_amt = sum(float(d.get("amount", 0)) for d in dep if d.get("status") == 1)
            wd_amt = sum(float(w.get("amount", 0)) - float(w.get("transactionFee", 0)) 
                        for w in wd if w.get("status") == 6)

            result["binance_netflow_24h"] = wd_amt - dep_amt
        except Exception as e:
            st.sidebar.error(f"Binance netflow error: {e}")

    # XRPL whale inflows from Redis
    try:
        raw = rdb.get("xrpl:latest_inflows")
        if raw:
            inflows = json.loads(raw) if isinstance(raw, str) else raw
            result["net_whale_flow"] = sum(i.get("xrp", 0) for i in inflows)
    except: pass

    return result

live = fetch_live()

# ========================= #
# News sentiment from Redis
# ========================= #
import json as _json
def read_sentiment():
    try:
        raw = rdb.get("news:sentiment")
        if raw:
            return _json.loads(raw)
    except:
        pass
    return {"score": 0.0, "count": 0, "timestamp": None}

news_payload = read_sentiment()
news_sent = news_payload.get("score", 0.0)

# ========================= #
# Scoring engine
# ========================= #
fund_hist = live.get("funding_hist_pct") or [0.0]
fund_now = live.get("funding_now_pct") or 0.0
fund_z = (fund_now - np.mean(fund_hist)) / (np.std(fund_hist) if np.std(fund_hist) > 1e-8 else 1e-8)

points = {
    "Funding Z-Score": max(0, fund_z * 22),
    "Whale Flow Bullish": max(0, (live.get("net_whale_flow") or 0) / 60e6 * 14),
    "Price < $2.45": 28 if (live.get("price") or 0) < 2.45 else 0,
    "OI > $2.7B": 16 if (live.get("oi_usd") or 0) > 2.7e9 else 0,
    "Binance Netflow Bullish": max(0, (live.get("binance_netflow_24h") or 0) / 100e6 * 30),
    "Short Squeeze Setup": max(0, (2.0 - live.get("long_short_ratio", 1.0)) * 20),
    "Positive News": 15 if news_sent > 0.2 else 0,
}
total_score = min(100, sum(points.values()))

# ========================= #
# UI — Metrics
# ========================= #
st.markdown("### Live Metrics")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("XRP Price", f"${live.get('price', 0):.4f}" if live.get('price') else "—")
c2.metric("Funding Rate", f"{live.get('funding_now_pct', 0):+.4f}%")
c3.metric("Open Interest", f"${(live.get('oi_usd') or 0)/1e9:.2f}B")
c4.metric("L/S Ratio", f"{live.get('long_short_ratio', 1):.2f}")
c5.metric("News Sentiment", f"{news_sent:+.3f}", delta=f"{news_payload.get('count',0)} articles")
c6.metric("XRPL Inflows", f"{(live.get('net_whale_flow') or 0):+.1f}M")

# Score
score_col, signal_col = st.columns([1,2])
with score_col:
    if total_score >= 80:
        color, signal = "#00aa44", "STRONG BUY — REVERSAL LIKELY"
    elif total_score >= 65:
        color, signal = "#00cc88", "ACCUMULATION — BULLISH"
    elif total_score <= 35:
        color, signal = "#cc3344", "DISTRIBUTION — CAUTION"
    else:
        color, signal = "#444444", "NEUTRAL — WAIT"
    st.markdown(f'<p style="font-size:86px;color:{color};text-align:center;font-weight:bold;">{total_score:.0f}</p>', unsafe_allow_html=True)
with signal_col:
    st.markdown(f'<h2 style="color:{color};margin-top:30px;">{signal}</h2>', unsafe_allow_html=True)

st.write("**Score breakdown**")
for k, v in points.items():
    st.write(f"• {k}: {v:.1f}")

# Raw inputs table
st.markdown("**Live Signal Breakdown (raw)**")
for k, v in {
    "Funding Now (%)": live.get("funding_now_pct"),
    "Funding Z-Score": round(fund_z, 4),
    "Whale Flow (M)": round(live.get("net_whale_flow") or 0, 3),
    "Binance Netflow 24h": live.get("binance_netflow_24h"),
    "Open Interest $": live.get("oi_usd"),
    "L/S Ratio": live.get("long_short_ratio"),
    "News Sentiment": news_sent,
    "News Count": news_payload.get("count"),
}.items():
    a, b = st.columns([3,1])
    a.write(k)
    b.write(str(v) if v is not None else "—")

# ========================= #
# FINAL BULLETPROOF CHART
# ========================= #
st.markdown("### 90-Day XRP Chart")
chart_df = get_chart_data()

if not chart_df.empty:
    fig = go.Figure()

    # Price (top chart)
    fig.add_trace(go.Candlestick(
        x=chart_df["date"],
        open=chart_df["open"],
        high=chart_df["high"],
        low=chart_df["low"],
        close=chart_df["close"],
        name="Price",
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        yaxis="y1"
    ))

    # Volume (bottom chart, independent scale)
    fig.add_trace(go.Bar(
        x=chart_df["date"],
        y=chart_df["volume"],
        name="Volume",
        marker_color='rgba(100,150,255,0.4)',
        yaxis="y2"
    ))

    fig.update_layout(
        height=700,
        template="plotly_dark",
        xaxis=dict(rangeslider_visible=False),
        yaxis=dict(title="Price (USD)", domain=[0.35, 1.0]),  # top chart
        yaxis2=dict(title="Volume (USD)", domain=[0, 0.3]),    # bottom chart
        hovermode="x unified",
        margin=dict(l=50, r=50, t=50, b=50)
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.error("Chart data unavailable — both CoinGecko and Binance failed")


# ========================= #
# Footer
# ========================= #
st.caption("v8.5 — Bulletproof chart + Railway shared vars fixed • Running on ↑↑↑")





