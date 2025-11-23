# main.py — XRP REVERSAL & BREAKOUT ENGINE v9.2
# XRP-only, XRPL inflows (weighted + Ripple OTC), Binance netflow,
# XRP/BTC & XRP/ETH flippening, sentiment EMA + bull/bear, SMA backtest.

import os
import hmac
import hashlib
import time
from datetime import datetime
from urllib.parse import urlencode

import streamlit as st
import pandas as pd
import numpy as np
import requests
import plotly.graph_objects as go
import json

from redis_client import rdb
from exchange_addresses import EXCHANGE_ADDRESSES

# =========================
# Config / constants
# =========================

st.set_page_config(
    page_title="XRP Engine v9.2",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.title("XRP REVERSAL & BREAKOUT ENGINE v9.2")
st.markdown(
    "<p style='text-align: center; color: #00ff88; font-size:18px;'>"
    "XRPL Inflows (Weighted) • Ripple OTC → Exchanges • Binance Netflow • "
    "XRP/BTC & XRP/ETH Flippening • News Sentiment EMA • SMA Backtest"
    "</p>",
    unsafe_allow_html=True,
)

META_REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
st.markdown(
    f'<meta http-equiv="refresh" content="{META_REFRESH_SECONDS}">', unsafe_allow_html=True
)

REQUEST_TIMEOUT = 10
SENTIMENT_EMA_ALPHA = float(os.getenv("SENTIMENT_EMA_ALPHA", "0.3"))


# =========================
# Helpers
# =========================

def safe_get(url, params=None, timeout=REQUEST_TIMEOUT):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        if not r.ok:
            return None
        return r.json()
    except Exception:
        return None


def exchange_weight(name: str | None) -> float:
    if not name:
        return 0.5
    s = name.lower()
    # heavyweight spot + derivatives venues
    if any(x in s for x in ["binance", "kraken", "bitstamp", "bybit", "bitbank", "bithumb", "upbit"]):
        return 1.5
    # regionals / solid retail
    if any(x in s for x in ["uphold", "bitso", "bitrue", "bitkub", "cex.io", "gate"]):
        return 1.0
    return 0.5


# =========================
# Chart data (90d OHLC + volume)
# =========================

@st.cache_data(ttl=600)
def get_chart_data():
    # CoinGecko OHLC + volume
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

    # Binance fallback
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


@st.cache_data(ttl=900)
def get_flippening_baseline(days: int = 30):
    """
    Fetch 30-day XRP/BTC & XRP/ETH history using CoinGecko market_chart.
    Returns uplift (%) vs 30-day mean.
    """
    btc = safe_get(
        "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
        {"vs_currency": "btc", "days": days, "interval": "daily"},
    )
    eth = safe_get(
        "https://api.coingecko.com/api/v3/coins/ripple/market_chart",
        {"vs_currency": "eth", "days": days, "interval": "daily"},
    )

    def _uplift(payload):
        if not payload or "prices" not in payload:
            return None
        prices = [p[1] for p in payload["prices"]]
        if not prices:
            return None
        last = prices[-1]
        mean = float(np.mean(prices))
        if mean <= 0:
            return None
        return (last / mean - 1.0) * 100.0

    return {
        "xrp_btc_uplift": _uplift(btc),
        "xrp_eth_uplift": _uplift(eth),
    }


# =========================
# Live data fetch
# =========================

def fetch_live():
    result = {
        "price": None,
        "funding_now_pct": 0.0,
        "funding_hist_pct": [],
        "oi_usd": None,
        "long_short_ratio": 1.0,
        "binance_netflow_24h": 0.0,
        "xrp_btc": None,
        "xrp_eth": None,
    }

    # price
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ripple", "vs_currencies": "usd"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            result["price"] = float(r.json()["ripple"]["usd"])
    except Exception:
        pass

    # XRP/BTC, XRP/ETH ratios
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ripple,bitcoin,ethereum", "vs_currencies": "usd"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            data = r.json()
            xrp = data.get("ripple", {})
            btc = data.get("bitcoin", {})
            eth = data.get("ethereum", {})
            px_xrp = float(xrp.get("usd", result["price"] or 0.0) or 0.0)
            px_btc = float(btc.get("usd", 0.0) or 0.0)
            px_eth = float(eth.get("usd", 0.0) or 0.0)
            if px_btc > 0:
                result["xrp_btc"] = px_xrp / px_btc
            if px_eth > 0:
                result["xrp_eth"] = px_xrp / px_eth
    except Exception:
        pass

    # funding
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": "XRPUSDT"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            result["funding_now_pct"] = float(r.json()["lastFundingRate"]) * 100.0
    except Exception:
        pass

    # open interest
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": "XRPUSDT"},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            oi_contracts = float(r.json()["openInterest"])
            if result["price"]:
                result["oi_usd"] = oi_contracts * result["price"]
    except Exception:
        pass

    # funding history
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": "XRPUSDT", "limit": 200},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok:
            rates = [float(x["fundingRate"]) * 100.0 for x in r.json()[-90:]]
            result["funding_hist_pct"] = rates
    except Exception:
        pass

    # long/short ratio
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": "XRPUSDT", "period": "5m", "limit": 1},
            timeout=REQUEST_TIMEOUT,
        )
        if r.ok and r.json():
            result["long_short_ratio"] = float(r.json()[0]["longShortRatio"])
    except Exception:
        pass

    # Binance signed netflow (XRP on-chain)
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if api_key and api_secret and api_key.strip() and api_secret.strip():
        try:
            ts = int(time.time() * 1000)
            start = ts - 86_400_000
            base = "https://api.binance.com"

            params = {"coin": "XRP", "startTime": start, "timestamp": ts}
            query_string = urlencode(params)
            signature = hmac.new(
                api_secret.encode(), query_string.encode(), hashlib.sha256
            ).hexdigest()
            headers = {"X-MBX-APIKEY": api_key}

            dep_url = f"{base}/sapi/v1/capital/deposit/hisrec?{query_string}&signature={signature}"
            wd_url = f"{base}/sapi/v1/capital/withdraw/history?{query_string}&signature={signature}"

            dep = requests.get(dep_url, headers=headers, timeout=REQUEST_TIMEOUT).json()
            wd = requests.get(wd_url, headers=headers, timeout=REQUEST_TIMEOUT).json()

            dep_amt = sum(float(d.get("amount", 0)) for d in dep if d.get("status") == 1)
            wd_amt = sum(
                float(w.get("amount", 0)) - float(w.get("transactionFee", 0))
                for w in wd
                if w.get("status") == 6
            )
            result["binance_netflow_24h"] = wd_amt - dep_amt
        except Exception:
            pass

    return result


live = fetch_live()


# =========================
# XRPL flows from Redis
# =========================

def read_xrpl_flows():
    try:
        raw = rdb.get("xrpl:latest_inflows")
        if not raw:
            return []
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        flows = json.loads(raw)
        if isinstance(flows, list):
            return flows
    except Exception:
        pass
    return []


xrpl_flows = read_xrpl_flows()

raw_xrp = sum(float(f.get("xrp", 0.0) or 0.0) for f in xrpl_flows)
weighted_xrp = 0.0
otc_xrp = 0.0

for f in xrpl_flows:
    flow_type = f.get("flow_type", "exchange_inflow")
    exch = f.get("exchange")
    amt = float(f.get("xrp", 0.0) or 0.0)
    if flow_type == "ripple_otc":
        otc_xrp += amt
    else:
        weighted_xrp += amt * exchange_weight(exch)


# =========================
# News sentiment from Redis
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
    try:
        raw = rdb.get("news:sentiment_ema")
        if raw:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            obj = _json.loads(raw)
            return float(obj.get("ema", 0.0))
    except Exception:
        pass
    return None


def write_sentiment_ema(value: float):
    try:
        payload = {"ema": float(value), "timestamp": datetime.utcnow().isoformat() + "Z"}
        rdb.set("news:sentiment_ema", _json.dumps(payload))
    except Exception:
        pass


def compute_sentiment_components(articles, mode: str):
    usable = []
    for a in articles:
        w = a.get("weight", 0.0)
        scalar = a.get("scalar")
        pos = a.get("pos")
        neg = a.get("neg")
        if scalar is None or pos is None or neg is None:
            continue
        if mode == "Institutional Only" and (w is None or w < 0.6):
            continue
        usable.append(a)

    if not usable:
        return 0.0, 0.0, 0.0

    weights = np.array([float(u.get("weight", 0.0) or 0.0) for u in usable])
    if weights.sum() <= 0:
        return 0.0, 0.0, 0.0

    pos_arr = np.array([float(u.get("pos", 0.0) or 0.0) for u in usable])
    neg_arr = np.array([float(u.get("neg", 0.0) or 0.0) for u in usable])
    scalar_arr = pos_arr - neg_arr

    bull = float(np.average(pos_arr, weights=weights))
    bear = float(np.average(neg_arr, weights=weights))
    inst = float(np.average(scalar_arr, weights=weights))
    return inst, bull, bear


news_payload = read_sentiment()
articles = news_payload.get("articles", [])


# =========================
# Sentiment Mode toggle
# =========================

st.subheader("Sentiment Mode")
sent_mode = st.radio(
    "Filter sentiment by source:",
    ["Weighted (All Sources)", "Institutional Only"],
    horizontal=True,
)

inst_sent, bull_intensity, bear_intensity = compute_sentiment_components(
    articles, sent_mode
)

prev_ema = read_sentiment_ema()
if prev_ema is None:
    ema_sent = inst_sent
else:
    ema_sent = SENTIMENT_EMA_ALPHA * inst_sent + (1.0 - SENTIMENT_EMA_ALPHA) * prev_ema
write_sentiment_ema(ema_sent)


# =========================
# Flippening baseline
# =========================

flip = get_flippening_baseline(days=30)
uplift_btc = flip.get("xrp_btc_uplift")
uplift_eth = flip.get("xrp_eth_uplift")

if uplift_btc is None:
    uplift_btc = 0.0
if uplift_eth is None:
    uplift_eth = 0.0

flip_raw = max(0.0, (uplift_btc + uplift_eth) / 2.0)
# boost if on-chain weighted inflow is net positive
if weighted_xrp > 0:
    flip_raw *= 1.25

flip_score = min(12.0, flip_raw / 2.0)  # compress into ~0–12 pts


# =========================
# Scoring engine
# =========================

fund_hist = live.get("funding_hist_pct") or [0.0]
fund_now = live.get("funding_now_pct") or 0.0
fund_z = (fund_now - np.mean(fund_hist)) / (
    np.std(fund_hist) if np.std(fund_hist) > 1e-8 else 1e-8
)

points = {
    "Funding Z-Score": max(0.0, fund_z * 22.0),
    "Whale Flow (XRPL, weighted)": max(
        0.0, (weighted_xrp or 0.0) / 60e6 * 14.0
    ),
    "Price < $2.45": 28.0 if (live.get("price") or 0.0) < 2.45 else 0.0,
    "OI > $2.7B": 16.0 if (live.get("oi_usd") or 0.0) > 2.7e9 else 0.0,
    "Binance Netflow Bullish": max(
        0.0, (live.get("binance_netflow_24h") or 0.0) / 100e6 * 30.0
    ),
    "Short Squeeze Setup": max(
        0.0, (2.0 - live.get("long_short_ratio", 1.0)) * 20.0
    ),
    "Positive News (EMA)": 15.0 if ema_sent > 0.2 else 0.0,
    "Flippening Flow": flip_score,
}
total_score = float(min(100.0, sum(points.values())))


# =========================
# UI — Metrics
# =========================

st.markdown("### Live Metrics")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric(
    "XRP Price",
    f"${live.get('price', 0.0):.4f}" if live.get("price") else "—",
)
c2.metric("XRP/BTC", f"{live.get('xrp_btc'):.8f}" if live.get("xrp_btc") else "—")
c3.metric("XRP/ETH", f"{live.get('xrp_eth'):.8f}" if live.get("xrp_eth") else "—")
c4.metric("Funding", f"{live.get('funding_now_pct', 0.0):+.4f}%")
c5.metric("OI (USD)", f"${(live.get('oi_usd') or 0.0)/1e9:.2f}B")
c6.metric("L/S Ratio", f"{live.get('long_short_ratio', 1.0):.2f}")

st.markdown("### Sentiment & Flow")
s1, s2, s3, s4 = st.columns(4)
label = "Inst. Sentiment EMA" if sent_mode == "Institutional Only" else "News Sentiment EMA"
s1.metric(label, f"{ema_sent:+.3f}", delta=f"{inst_sent:+.3f} now")
s2.metric("Bullish Intensity", f"{bull_intensity:+.3f}")
s3.metric("Bearish Intensity", f"{bear_intensity:+.3f}")
s4.metric("XRPL Inflows (raw, M XRP)", f"{raw_xrp/1e6:+.1f}")

t1, t2, t3 = st.columns(3)
t1.metric("Ripple OTC → Exchanges (M XRP)", f"{otc_xrp/1e6:+.1f}")
t2.metric("XRPL Inflows (weighted, M XRP)", f"{weighted_xrp/1e6:+.1f}")
t3.metric("Flippening Flow Score", f"{flip_score:.2f}")

st.write(f"XRP/BTC uplift vs 30d: {uplift_btc:+.2f}%")
st.write(f"XRP/ETH uplift vs 30d: {uplift_eth:+.2f}%")

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
with signal_col:
    st.markdown(
        f'<h2 style="color:{color};margin-top:30px;">{signal}</h2>',
        unsafe_allow_html=True,
    )
    st.write(f"Funding Z-Score: {fund_z:+.2f}")

st.write("**Score breakdown**")
for k, v in points.items():
    st.write(f"• {k}: {v:.1f}")


# =========================
# Live Signal Breakdown (raw)
# =========================

st.markdown("**Live Signal Breakdown (raw)**")
raw_items = {
    "Funding Now (%)": live.get("funding_now_pct"),
    "Funding Z-Score": round(fund_z, 4),
    "XRPL Net Inflow (raw, M XRP)": raw_xrp / 1e6,
    "XRPL Net Inflow (weighted, M XRP)": weighted_xrp / 1e6,
    "Ripple OTC → Exchanges (M XRP)": otc_xrp / 1e6,
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
    b.write("0" if v is None else str(v))


# =========================
# Simple SMA Backtest on Price
# =========================

st.markdown("### 90-Day SMA + Volume Backtest (Price-only Approximation)")
chart_df = get_chart_data()


def run_sma_backtest(df: pd.DataFrame, fast: int = 7, slow: int = 21):
    if df.empty:
        return {
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "equity": pd.Series(dtype=float),
            "signals": [],
            "df": df,
        }

    df = df.copy().sort_values("date")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["sma_fast"] = df["close"].rolling(fast).mean()
    df["sma_slow"] = df["close"].rolling(slow).mean()

    df["signal"] = 0
    df.loc[df["sma_fast"] > df["sma_slow"], "signal"] = 1
    df["signal_shift"] = df["signal"].shift(1).fillna(0)

    df["ret"] = df["close"].pct_change().fillna(0.0)
    df["strategy_ret"] = df["ret"] * df["signal_shift"]

    equity = (1.0 + df["strategy_ret"]).cumprod() * 100.0

    entries = df[(df["signal_shift"] == 0) & (df["signal"] == 1)].index
    exits = df[(df["signal_shift"] == 1) & (df["signal"] == 0)].index

    if len(entries) > len(exits) and len(entries) > 0:
        exits = exits.append(pd.Index([df.index[-1]]))

    trade_returns = []
    signals = []
    for ent, ex in zip(entries, exits):
        p_ent = df.loc[ent, "close"]
        p_ex = df.loc[ex, "close"]
        if p_ent and p_ex and p_ent > 0:
            r = (p_ex / p_ent - 1.0) * 100.0
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
                (np.mean(df["strategy_ret"]) / np.std(df["strategy_ret"])) * np.sqrt(252)
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
            x=bt["df"].sort_values("date")["date"],
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


# =========================
# XRPL inflow table
# =========================

st.markdown("### XRPL → Exchange Inflows (Last Snapshot)")
if xrpl_flows:
    df_x = pd.DataFrame(xrpl_flows)
    df_disp = df_x.copy()

    if "xrp" in df_disp.columns:
        df_disp["xrp_m"] = df_disp["xrp"].astype(float) / 1e6

    # normalise timestamp column name
    if "timestamp" in df_disp.columns:
        df_disp["timestamp"] = df_disp["timestamp"].astype(str)

    cols = []
    for c in ["timestamp", "exchange", "flow_type", "xrp_m", "from_address", "to_address"]:
        if c in df_disp.columns:
            cols.append(c)

    st.dataframe(df_disp[cols], hide_index=True)
else:
    st.info("No recent XRPL inflows snapshot found.")


# =========================
# FINAL CHART: Candles + Volume + Backtest Signals
# =========================

st.markdown("### 90-Day XRP Candles + Volume + Backtest Signals")

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
            title="Volume (B)", domain=[0.0, 0.3], overlaying="y", side="right"
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
    "v9.2 — XRP only • XRPL Inflows (Weighted) • Ripple OTC → Exchanges • "
    "Binance Netflow • XRP/BTC & XRP/ETH Flippening • News Sentiment EMA "
    "• SMA Backtest + Signal Annotations"
)
