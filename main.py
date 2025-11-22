# ================= MAIN XRP TERMINAL v10 ================= #
# Single-asset XRP terminal with XRP/BTC and XRP/ETH flippens monitoring
# Features: XRPL inflows • Binance netflow • News sentiment (cached) • Backtest panel • Whale table
# Author: XRP Bloomberg Terminal – GPT Refactor

import os, time, json, hmac, hashlib
from urllib.parse import urlencode

import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from redis_client import rdb

# ===================== CONFIG ===================== #
REFRESH = int(os.getenv("META_REFRESH_SECONDS", "45"))
REQUEST_TIMEOUT = 10

BINANCE_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET")

# ===================== PAGE SETUP ===================== #
st.set_page_config(page_title="XRP Terminal", layout="wide", initial_sidebar_state="collapsed")
st.title("XRP REVERSAL & BREAKOUT ENGINE v10")
st.markdown("<p style='text-align:center;color:#00ff88;'>XRPL Inflows • Binance Netflow • XRP/BTC & XRP/ETH Flip Monitor • News Sentiment</p>",
            unsafe_allow_html=True)

# Auto Refresh
if not st.checkbox("Pause Refresh", False):
    st.markdown(f'<meta http-equiv="refresh" content="{REFRESH}">', unsafe_allow_html=True)

# ===================== API HELPERS ===================== #
def safe_get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.ok: return r.json()
    except: pass
    return None


# ===================== PRICE + RATIOS ===================== #
def get_xrp_price_and_ratios():
    data = safe_get("https://api.coingecko.com/api/v3/simple/price",
                    {"ids":"ripple,bitcoin,ethereum", "vs_currencies":"usd"})
    if not data: return None, None, None
    x = data["ripple"]["usd"]
    b = data["bitcoin"]["usd"]
    e = data["ethereum"]["usd"]
    return x, x/b, x/e  # price, XRP/BTC, XRP/ETH ratio


# ===================== OHLC + VOLUME ===================== #
@st.cache_data(ttl=600)
def get_chart_data():
    coingecko = safe_get("https://api.coingecko.com/api/v3/coins/ripple/market_chart",
                         {"vs_currency":"usd","days":"90","interval":"daily"})
    if not coingecko:
        return pd.DataFrame()

    prices = pd.DataFrame(coingecko["prices"], columns=["ts","price"])
    vols = pd.DataFrame(coingecko["total_volumes"], columns=["ts","volume"])
    df = prices.copy()
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    df["open"] = df["high"] = df["low"] = df["close"] = df["price"]
    df = df.merge(vols, on="ts")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    return df[["date","open","high","low","close","volume"]]


# ===================== LIVE METRICS ===================== #
def fetch_live():
    out = {
        "price":0, "fund":0, "fund_hist":[0], "oi_usd":0, "ls":1.0,
        "binance_flow":0, "xrpl_flow":0
    }

    # Funding now
    fr = safe_get("https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol":"XRPUSDT"})
    if fr: out["fund"] = float(fr["lastFundingRate"])*100

    # Funding history
    fh = safe_get("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol":"XRPUSDT","limit":200})
    if fh:
        out["fund_hist"] = [float(x["fundingRate"])*100 for x in fh[-90:]]

    # Price + OI
    price, xbtc, xeth = get_xrp_price_and_ratios()
    out["price"], out["xbtc"], out["xeth"] = price, xbtc, xeth

    oi = safe_get("https://fapi.binance.com/fapi/v1/openInterest", {"symbol":"XRPUSDT"})
    if oi and price: out["oi_usd"] = float(oi["openInterest"])*price

    # Long/Short ratio
    ls = safe_get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                  {"symbol":"XRPUSDT","period":"5m","limit":1})
    if ls: out["ls"] = float(ls[0]["longShortRatio"])

    # Signed Binance netflow (XRP only)
    if BINANCE_KEY and BINANCE_SECRET:
        try:
            ts = int(time.time()*1000)
            start = ts - 86400000
            base = "https://api.binance.com"
            params = {"coin":"XRP","startTime":start,"timestamp":ts}
            qs = urlencode(params)
            sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
            dep = safe_get(f"{base}/sapi/v1/capital/deposit/hisrec?{qs}&signature={sig}")
            wd  = safe_get(f"{base}/sapi/v1/capital/withdraw/history?{qs}&signature={sig}")
            da = sum(float(d.get("amount",0)) for d in dep if d.get("status")==1) if dep else 0
            wa = sum(float(w.get("amount",0))-float(w.get("transactionFee",0)) for w in wd if w.get("status")==6) if wd else 0
            out["binance_flow"] = wa - da
        except: pass

    # XRPL whale inflows (Redis)
    try:
        raw = rdb.get("xrpl:latest_inflows")
        if raw:
            infl = json.loads(raw) if isinstance(raw,str) else raw
            out["xrpl_flow"] = sum(i.get("xrp",0) for i in infl)
    except: pass

    return out


# ===================== NEWS SENTIMENT ===================== #
def get_sentiment():
    try:
        raw = rdb.get("news:sentiment")
        if raw: return json.loads(raw)
    except: pass
    return {"score":0.0,"count":0}


# ===================== SCORE ===================== #
def compute_score(live, sentiment):
    fund_hist = live["fund_hist"] or [0]
    fund_z = (live["fund"] - np.mean(fund_hist)) / (np.std(fund_hist) if np.std(fund_hist)>1e-8 else 1e-8)

    pts = {
        "Funding Z": max(0, fund_z*22),
        "Whale Flow": max(0, live["xrpl_flow"]/60e6 * 14),
        "Price < 2.45": 28 if live["price"]<2.45 else 0,
        "OI > 2.7B": 16 if live["oi_usd"]>2.7e9 else 0,
        "Netflow": max(0, live["binance_flow"]/100e6 * 30),
        "Short Squeeze": max(0, (2-live["ls"])*20),
        "News": 15 if sentiment["score"]>0.2 else 0
    }
    return min(100,sum(pts.values())), pts


# ===================== UI ===================== #
live = fetch_live()
sent = get_sentiment()
score, breakdown = compute_score(live, sent)

# TOP METRICS
c1,c2,c3,c4,c5,c6,c7 = st.columns(7)
c1.metric("XRP", f"${live['price']:.4f}")
c2.metric("XRP/BTC", f"{live['xbtc']:.8f}")
c3.metric("XRP/ETH", f"{live['xeth']:.8f}")
c4.metric("Funding", f"{live['fund']:+.4f}%")
c5.metric("OI", f"${live['oi_usd']/1e9:.2f}B")
c6.metric("L/S", f"{live['ls']:.2f}")
c7.metric("News", f"{sent['score']:+.3f}", f"{sent['count']} articles")

# Score block
s1, s2 = st.columns([1,2])
color = "#00aa44" if score>=80 else "#00cc88" if score>=65 else "#cc3344" if score<=35 else "#444444"
with s1: st.markdown(f'<p style="font-size:86px;text-align:center;color:{color};font-weight:bold;">{score}</p>', unsafe_allow_html=True)
with s2:
    label = "STRONG BUY" if score>=80 else "ACCUMULATION" if score>=65 else "CAUTION" if score<=35 else "NEUTRAL"
    st.markdown(f"<h2 style='color:{color};margin-top:30px;'>{label}</h2>", unsafe_allow_html=True)

# Score table
st.write("### Score Breakdown")
for k,v in breakdown.items(): st.write(f"• {k}: {v:.1f}")

# Chart
st.write("### 90-Day XRP Chart")
df = get_chart_data()
if not df.empty:
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=df["date"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
                                 increasing_line_color="#26a69a", decreasing_line_color="#ef5350", name="Price"))
    fig.add_trace(go.Bar(x=df["date"], y=df["volume"], marker_color="rgba(100,150,255,0.35)", yaxis="y2", name="Volume"))
    fig.update_layout(height=700, template="plotly_dark",
                      yaxis=dict(title="Price USD", domain=[0.35,1.0]),
                      yaxis2=dict(title="Volume", domain=[0,0.28]),
                      xaxis=dict(rangeslider_visible=False),
                      hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

# Footer
st.caption("v10 — XRP Only • Flippens Monitor • XRPL + Binance Netflow • News Cached")
