# ================= XRP REVERSAL & BREAKOUT ENGINE v10.1 ================= #
# World-class XRP-only dashboard:
# - XRPL Inflows (Redis: xrpl:latest_inflows)
# - Binance netflow (signed) for XRP
# - Funding, OI, Long/Short ratio
# - News sentiment (Redis: news:sentiment)
# - XRP/BTC & XRP/ETH ratios (flip monitor)
# - Robust OHLC + volume (CoinGecko + Binance fallback)
# - Simplified SMA/volume backtest + on-chart signal annotations
# - Whale inflow table
# - Basic/Advanced view toggle

import os
import time
import json
import hmac
import hashlib
from urllib.parse import urlencode

import requests
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

from redis_client import rdb

# ============================= CONFIG ============================= #

REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
REQUEST_TIMEOUT = 10

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# ============================= PAGE SETUP ============================= #

st.set_page_config(
    page_title="XRP Engine v10.1",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("XRP REVERSAL & BREAKOUT ENGINE v10.1")
st.markdown(
    "<p style='text-align:center;color:#00ff88;'>"
    "XRPL Inflows • Binance Netflow • XRP/BTC & XRP/ETH Ratios • News Sentiment • 90D Backtest"
    "</p>",
    unsafe_allow_html=True,
)

# Auto refresh with pause toggle
pause_refresh = st.checkbox("Pause auto-refresh", value=False)
if not pause_refresh:
    st.markdown(
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">',
        unsafe_allow_html=True,
    )

show_advanced = st.checkbox("Show advanced analytics", value=True)

# ============================= HELPERS ============================= #

def safe_get(url, params=None, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if r.ok:
            return r.json()
    except:
        pass
    return None


def safe_post(url, headers=None, json_payload=None, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.post(url, headers=headers, json=json_payload, timeout=timeout)
        if r.ok:
            return r.json()
    except:
        pass
    return None


# ============================= PRICE & RATIOS ============================= #

def get_xrp_price_and_ratios():
    """
    Try CoinGecko first, then fall back to Binance spot tickers.
    Returns: (xrp_usd, xrp_btc, xrp_eth)
    """
    # 1) CoinGecko
    data = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": "ripple,bitcoin,ethereum", "vs_currencies": "usd"},
    )
    if data:
        try:
            x = float(data["ripple"]["usd"])
            b = float(data["bitcoin"]["usd"])
            e = float(data["ethereum"]["usd"])
            return x, (x / b if b else None), (x / e if e else None)
        except:
            pass

    # 2) Binance spot tickers (USDT pairs)
    ticker_map = {}
    for sym in ["XRPUSDT", "BTCUSDT", "ETHUSDT"]:
        t = safe_get("https://api.binance.com/api/v3/ticker/price", {"symbol": sym})
        if not t:
            continue
        try:
            ticker_map[sym] = float(t["price"])
        except:
            pass

    x = ticker_map.get("XRPUSDT")
    b = ticker_map.get("BTCUSDT")
    e = ticker_map.get("ETHUSDT")

    if x:
        return x, (x / b if b else None), (x / e if e else None)
    return None, None, None


# ============================= OHLC + VOLUME ============================= #

@st.cache_data(ttl=600)
def get_chart_data():
    """
    90-day daily candles & volume.
    1) CoinGecko market_chart (daily)
    2) Fallback: Binance /api/v3/klines 1d XRPUSDT
    Returns DataFrame: [date, open, high, low, close, volume]
    """
    # 1) CoinGecko
    cg = safe_get(
        "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
        {"vs_currency": "usd", "days": "90", "interval": "daily"},
    )
    if cg:
        try:
            prices = pd.DataFrame(cg["prices"], columns=["ts", "price"])
            vols = pd.DataFrame(cg["total_volumes"], columns=["ts", "volume"])
            df = prices.copy()
            df["date"] = pd.to_datetime(df["ts"], unit="ms")
            df["open"] = df["price"]
            df["high"] = df["price"]
            df["low"] = df["price"]
            df["close"] = df["price"]
            df = df.merge(vols, on="ts")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except:
            pass

    # 2) Binance klines
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
                    "quote_vol",
                    "trades",
                    "tb_base",
                    "tb_quote",
                    "ignore",
                ],
            )
            df["date"] = pd.to_datetime(df["open_time"], unit="ms")
            for c in ["open", "high", "low", "close", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
            return df[["date", "open", "high", "low", "close", "volume"]]
        except:
            pass

    return pd.DataFrame()


# ============================= XRPL INFLOWS TABLE ============================= #

@st.cache_data(ttl=60)
def load_xrpl_inflows():
    """
    Expected Redis key (from xrpl_inflow_monitor.py):
    "xrpl:latest_inflows" -> JSON list of dicts:
      [{"ts": unix_time, "xrp": <amount>, "to": "<exchange>", "type": "deposit"}, ...]
    """
    try:
        raw = rdb.get("xrpl:latest_inflows")
        if not raw:
            return pd.DataFrame()

        flows = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(flows, list):
            return pd.DataFrame()

        rows = []
        for f in flows:
            ts = f.get("ts")
            dt = pd.to_datetime(ts, unit="s") if ts is not None else None
            amt_xrp = f.get("xrp", 0) or 0
            rows.append(
                {
                    "Time (UTC)": dt,
                    "Amount (M XRP)": amt_xrp / 1e6,  # assume xrp amount is raw XRP
                    "Exchange": f.get("to", "unknown"),
                    "Type": f.get("type", "deposit"),
                }
            )
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("Time (UTC)", ascending=False)
        return df
    except:
        return pd.DataFrame()


# ============================= LIVE METRICS ============================= #

def fetch_live():
    out = {
        "price": 0.0,
        "funding_now_pct": 0.0,
        "funding_hist_pct": [0.0],
        "oi_usd": 0.0,
        "ls_ratio": 1.0,
        "binance_netflow_24h": 0.0,
        "xrpl_net_inflow_xrp": 0.0,
        "xrp_btc": None,
        "xrp_eth": None,
    }

    # Price + ratios
    px, xbtc, xeth = get_xrp_price_and_ratios()
    if px:
        out["price"] = px
    out["xrp_btc"] = xbtc
    out["xrp_eth"] = xeth

    # Funding now
    fr = safe_get("https://fapi.binance.com/fapi/v1/premiumIndex", {"symbol": "XRPUSDT"})
    if fr:
        try:
            out["funding_now_pct"] = float(fr["lastFundingRate"]) * 100.0
        except:
            pass

    # Funding history
    fh = safe_get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": "XRPUSDT", "limit": 200},
    )
    if fh:
        try:
            out["funding_hist_pct"] = [float(x["fundingRate"]) * 100.0 for x in fh[-90:]]
        except:
            pass

    # Open interest
    oi = safe_get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        {"symbol": "XRPUSDT"},
    )
    if oi and out["price"]:
        try:
            contracts = float(oi["openInterest"])
            out["oi_usd"] = contracts * out["price"]
        except:
            pass

    # Long/Short ratio
    ls = safe_get(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
        {"symbol": "XRPUSDT", "period": "5m", "limit": 1},
    )
    if ls:
        try:
            out["ls_ratio"] = float(ls[0]["longShortRatio"])
        except:
            pass

    # Binance signed netflow (XRP only)
    if BINANCE_API_KEY and BINANCE_API_SECRET:
        try:
            ts = int(time.time() * 1000)
            start = ts - 86_400_000
            base = "https://api.binance.com"
            params = {"coin": "XRP", "startTime": start, "timestamp": ts}
            qs = urlencode(params)
            sig = hmac.new(
                BINANCE_API_SECRET.encode(), qs.encode(), hashlib.sha256
            ).hexdigest()
            headers = {"X-MBX-APIKEY": BINANCE_API_KEY}

            dep = safe_get(f"{base}/sapi/v1/capital/deposit/hisrec?{qs}&signature={sig}")
            wd = safe_get(f"{base}/sapi/v1/capital/withdraw/history?{qs}&signature={sig}")

            dep_amt = (
                sum(float(d.get("amount", 0)) for d in dep if d.get("status") == 1)
                if dep
                else 0.0
            )
            wd_amt = (
                sum(
                    float(w.get("amount", 0)) - float(w.get("transactionFee", 0))
                    for w in wd
                    if w.get("status") == 6
                )
                if wd
                else 0.0
            )
            out["binance_netflow_24h"] = wd_amt - dep_amt  # positive = net outflow from Binance
        except:
            pass

    # XRPL inflows (net sum of latest snapshot)
    xrpl_df = load_xrpl_inflows()
    if not xrpl_df.empty:
        # Convert M XRP back to XRP for scoring scale if needed
        out["xrpl_net_inflow_xrp"] = float(xrpl_df["Amount (M XRP)"].sum() * 1e6)

    return out


# ============================= NEWS SENTIMENT (REDIS) ============================= #

def read_sentiment():
    try:
        raw = rdb.get("news:sentiment")
        if raw:
            payload = json.loads(raw)
            return {
                "score": float(payload.get("score", 0.0) or 0.0),
                "count": int(payload.get("count", 0) or 0),
                "timestamp": payload.get("timestamp"),
            }
    except:
        pass
    return {"score": 0.0, "count": 0, "timestamp": None}


# ============================= SCORING ENGINE ============================= #

def compute_score(live, news):
    fund_hist = live.get("funding_hist_pct") or [0.0]
    fund_now = live.get("funding_now_pct") or 0.0
    std = np.std(fund_hist)
    fund_z = (fund_now - np.mean(fund_hist)) / (std if std > 1e-8 else 1e-8)

    ls = live.get("ls_ratio") or 1.0
    oi = live.get("oi_usd") or 0.0
    price = live.get("price") or 0.0
    net_binance = live.get("binance_netflow_24h") or 0.0
    xrpl_xrp = live.get("xrpl_net_inflow_xrp") or 0.0
    news_score = news.get("score", 0.0) or 0.0

    pts = {
        "Funding Z-Score": max(0.0, fund_z * 22.0),
        "Whale Flow (XRPL)": max(0.0, xrpl_xrp / 60e6 * 14.0),  # 60M XRP ~ 14 pts
        "Price < $2.45": 28.0 if price < 2.45 else 0.0,
        "OI > $2.7B": 16.0 if oi > 2.7e9 else 0.0,
        "Binance Netflow Bullish": max(0.0, net_binance / 100e6 * 30.0),
        "Short Squeeze Setup": max(0.0, (2.0 - ls) * 20.0),
        "Positive News": 15.0 if news_score > 0.2 else 0.0,
    }

    total = min(100.0, sum(pts.values()))
    return total, pts, fund_z


# ============================= SIMPLE SMA BACKTEST ============================= #

@st.cache_data(ttl=600)
def compute_backtest(chart_df: pd.DataFrame):
    """
    Simplified backtest on price only:
    - SMA(10) crossing above SMA(30)
    - Volume > 1.2x 10-day average volume
    Entry: signal day close
    Exit: 10 bars later (or last bar)
    Returns metrics + list of signals for annotation.
    """
    df = chart_df.copy()
    if df.empty:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "signals": [],
        }

    df = df.sort_values("date").reset_index(drop=True)
    df["sma_fast"] = df["close"].rolling(10).mean()
    df["sma_slow"] = df["close"].rolling(30).mean()
    df["vol_ma"] = df["volume"].rolling(10).mean()

    signals = []
    rets = []

    for i in range(31, len(df) - 10):
        prev_fast = df.loc[i - 1, "sma_fast"]
        prev_slow = df.loc[i - 1, "sma_slow"]
        fast = df.loc[i, "sma_fast"]
        slow = df.loc[i, "sma_slow"]
        if any(np.isnan(x) for x in [prev_fast, prev_slow, fast, slow]):
            continue

        cross_up = prev_fast <= prev_slow and fast > slow
        vol_ok = df.loc[i, "volume"] > 1.2 * df.loc[i, "vol_ma"]

        if not (cross_up and vol_ok):
            continue

        entry_price = df.loc[i, "close"]
        if entry_price <= 0:
            continue

        exit_idx = min(i + 10, len(df) - 1)
        exit_price = df.loc[exit_idx, "close"]
        ret_pct = (exit_price / entry_price - 1.0) * 100.0
        rets.append(ret_pct)

        signals.append(
            {
                "date": df.loc[i, "date"],
                "price": float(entry_price),
                "exit_date": df.loc[exit_idx, "date"],
                "exit_price": float(exit_price),
                "ret_pct": float(ret_pct),
            }
        )

    if not rets:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "signals": [],
        }

    rets = np.array(rets)
    num_trades = len(rets)
    win_rate = float((rets > 0).sum() / num_trades * 100.0)
    avg_return = float(rets.mean())

    eq = np.cumprod(1.0 + rets / 100.0)
    peak = np.maximum.accumulate(eq)
    drawdown = eq / peak - 1.0
    max_drawdown = float(drawdown.min() * 100.0)

    std = rets.std()
    sharpe = float((rets.mean() / std) * np.sqrt(num_trades)) if std > 1e-8 else 0.0

    return {
        "num_trades": num_trades,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "max_drawdown": max_drawdown,
        "sharpe": sharpe,
        "signals": signals,
    }


# ============================= FETCH + CALCULATE ============================= #

live = fetch_live()
news = read_sentiment()
chart_df = get_chart_data()
score, score_breakdown, fund_z = compute_score(live, news)
backtest = compute_backtest(chart_df)
xrpl_table = load_xrpl_inflows()

# ============================= TOP METRICS ============================= #

st.markdown("### Live Metrics")
m1, m2, m3, m4, m5, m6, m7 = st.columns(7)
m1.metric("XRP Price", f"${live['price']:.4f}" if live["price"] else "—")
m2.metric("XRP/BTC", f"{live['xrp_btc']:.8f}" if live["xrp_btc"] else "—")
m3.metric("XRP/ETH", f"{live['xrp_eth']:.8f}" if live["xrp_eth"] else "—")
m4.metric("Funding", f"{live['funding_now_pct']:+.4f}%")
m5.metric("OI (USD)", f"${live['oi_usd']/1e9:.2f}B")
m6.metric("L/S Ratio", f"{live['ls_ratio']:.2f}")
m7.metric("News Sentiment", f"{news['score']:+.3f}", f"{news['count']} articles")

# ============================= SCORE PANEL ============================= #

score_col, signal_col = st.columns([1, 2])
if score >= 80:
    color, label = "#00aa44", "STRONG BUY — REVERSAL LIKELY"
elif score >= 65:
    color, label = "#00cc88", "ACCUMULATION — BULLISH"
elif score <= 35:
    color, label = "#cc3344", "DISTRIBUTION — CAUTION"
else:
    color, label = "#444444", "NEUTRAL — WAIT"

with score_col:
    st.markdown(
        f'<p style="font-size:86px;color:{color};text-align:center;font-weight:bold;">{score:.0f}</p>',
        unsafe_allow_html=True,
    )
with signal_col:
    st.markdown(
        f'<h2 style="color:{color};margin-top:30px;">{label}</h2>',
        unsafe_allow_html=True,
    )
    st.write(f"Funding Z-Score: {fund_z:+.2f}")

st.write("**Score breakdown**")
for k, v in score_breakdown.items():
    st.write(f"• {k}: {v:.1f}")

# ============================= RAW INPUTS ============================= #

if show_advanced:
    st.markdown("### Live Signal Breakdown (raw)")
    for k, v in {
        "Funding Now (%)": live.get("funding_now_pct"),
        "Funding Z-Score": round(fund_z, 4),
        "XRPL Net Inflow (M XRP)": (live.get("xrpl_net_inflow_xrp") or 0.0) / 1e6,
        "Binance Netflow 24h (XRP)": live.get("binance_netflow_24h"),
        "Open Interest $": live.get("oi_usd"),
        "L/S Ratio": live.get("ls_ratio"),
        "News Sentiment": news.get("score"),
        "News Count": news.get("count"),
    }.items():
        c_a, c_b = st.columns([3, 1])
        c_a.write(k)
        c_b.write(str(v) if v is not None else "—")

# ============================= BACKTEST PANEL ============================= #

if show_advanced:
    st.markdown("### 90-Day SMA + Volume Backtest (Price-only Approximation)")
    b1, b2, b3, b4, b5 = st.columns(5)
    b1.metric("Signals", backtest["num_trades"])
    b2.metric("Win Rate", f"{backtest['win_rate']:.1f}%")
    b3.metric("Avg Return / Trade", f"{backtest['avg_return']:+.1f}%")
    b4.metric("Max Drawdown", f"{backtest['max_drawdown']:.1f}%")
    b5.metric("Sharpe (approx)", f"{backtest['sharpe']:.2f}")

# ============================= XRPL INFLOWS TABLE ============================= #

if show_advanced:
    st.markdown("### XRPL → Exchange Inflows (Last Snapshot)")
    if not xrpl_table.empty:
        def color_rows(row):
            if row["Type"].lower() == "deposit":
                return ["background-color: #330000"] * len(row)
            return [""] * len(row)

        st.dataframe(
            xrpl_table.style.apply(color_rows, axis=1),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No recent XRPL inflows snapshot found.")

# ============================= CHART + SIGNAL ANNOTATIONS ============================= #

st.markdown("### 90-Day XRP Candles + Volume + Backtest Signals")
if not chart_df.empty:
    fig = go.Figure()

    # Price
    fig.add_trace(
        go.Candlestick(
            x=chart_df["date"],
            open=chart_df["open"],
            high=chart_df["high"],
            low=chart_df["low"],
            close=chart_df["close"],
            name="XRP",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            yaxis="y1",
        )
    )

    # Volume
    fig.add_trace(
        go.Bar(
            x=chart_df["date"],
            y=chart_df["volume"],
            name="Volume",
            marker_color="rgba(100,150,255,0.35)",
            yaxis="y2",
        )
    )

    # Backtest signals (annotate last 10)
    if backtest["signals"]:
        for s in backtest["signals"][-10:]:
            dt = s["date"]
            price = s["price"]
            label_text = f"{s['ret_pct']:+.1f}%"
            fig.add_trace(
                go.Scatter(
                    x=[dt],
                    y=[price],
                    mode="markers",
                    marker=dict(color="#ffff00", size=10, symbol="star"),
                    name="Backtest Signal",
                    showlegend=False,
                )
            )
            fig.add_annotation(
                x=dt,
                y=price,
                text=label_text,
                showarrow=True,
                arrowhead=2,
                ax=0,
                ay=-30,
                bgcolor="rgba(0,0,0,0.7)",
                font=dict(color="#ffffff", size=11),
            )

    fig.update_layout(
        height=700,
        template="plotly_dark",
        xaxis=dict(rangeslider_visible=False),
        yaxis=dict(title="Price (USD)", domain=[0.35, 1.0]),
        yaxis2=dict(
            title="Volume",
            domain=[0.0, 0.28],
            overlaying="y",
            side="right",
        ),
        hovermode="x unified",
        margin=dict(l=50, r=50, t=50, b=50),
    )

    st.plotly_chart(fig, use_container_width=True)
else:
    st.error("Chart data unavailable — CoinGecko and Binance fallbacks failed.")

# ============================= FOOTER ============================= #

st.caption(
    "v10.1 — XRP-only • XRPL Inflows • Binance Netflow • XRP/BTC & XRP/ETH • "
    "News Sentiment (cached) • SMA Backtest + Signal Annotations • Robust Fallbacks"
)
