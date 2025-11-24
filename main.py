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
import requests
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app_utils import (
    cache_get_json,
    cache_set_json,
    compute_sentiment_components,
    describe_data_health,
    safe_get,
)
from redis_client import rdb

# =========================
# Config / constants
# =========================

st.set_page_config(page_title="XRP Engine v9.3", layout="wide", initial_sidebar_state="collapsed")
st.title("XRP REVERSAL & BREAKOUT ENGINE v9.3")
st.markdown(
    "<p style='text-align: center; color: #00ff88; font-size:18px;'>"
    "XRPL Inflows (Weighted) • Ripple OTC → Exchanges • Binance Netflow • "
    "XRP/BTC & XRP/ETH Flippening • News Sentiment EMA • SMA Backtest"
    "</p>",
    unsafe_allow_html=True,
)

META_REFRESH_SECONDS = int(os.getenv("META_REFRESH_SECONDS", "45"))
st.markdown(f'<meta http-equiv="refresh" content="{META_REFRESH_SECONDS}">', unsafe_allow_html=True)
st.caption(
    f"Dashboard auto-refreshes every {META_REFRESH_SECONDS} seconds; lower values increase API usage."
)

REQUEST_TIMEOUT = 10
SENTIMENT_EMA_ALPHA = float(os.getenv("SENTIMENT_EMA_ALPHA", "0.3"))
RATIO_EMA_ALPHA = float(os.getenv("RATIO_EMA_ALPHA", "0.1"))

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
    result = {
        "price": None,
        "funding_now_pct": 0.0,
        "funding_hist_pct": [],
        "oi_usd": None,
        "long_short_ratio": 1.0,
        "binance_netflow_24h": None,
        "xrp_btc": None,
        "xrp_eth": None,
        "xrpl_raw_inflow": 0.0,
        "xrpl_weighted_inflow": 0.0,
        "xrpl_ripple_otc": 0.0,
    }

    # Price with Redis fallback
    price_live_ok = False
    try:
        pd_json = safe_get(
            "https://api.coingecko.com/api/v3/simple/price",
            {"ids": "ripple", "vs_currencies": "usd"},
        )
        if pd_json and "ripple" in pd_json:
            px = float(pd_json["ripple"]["usd"])
            result["price"] = px
            price_live_ok = True
            cache_set_json("cache:price:xrp_usd", {"price": px, "ts": time.time()})
    except Exception:
        pass

    if not price_live_ok:
        cached = cache_get_json("cache:price:xrp_usd")
        if cached:
            result["price"] = float(cached.get("price", 0.0))

    # XRP/BTC, XRP/ETH ratios and ratio EMAs
    ratio_resp = safe_get(
        "https://api.coingecko.com/api/v3/simple/price",
        {"ids": "ripple,bitcoin,ethereum", "vs_currencies": "usd"},
    )
    if ratio_resp:
        try:
            xrp = ratio_resp.get("ripple", {})
            btc = ratio_resp.get("bitcoin", {})
            eth = ratio_resp.get("ethereum", {})
            px_xrp = float(xrp.get("usd", result["price"] or 0.0) or 0.0)
            px_btc = float(btc.get("usd", 0.0) or 0.0)
            px_eth = float(eth.get("usd", 0.0) or 0.0)
            if px_btc > 0:
                result["xrp_btc"] = px_xrp / px_btc
            if px_eth > 0:
                result["xrp_eth"] = px_xrp / px_eth
        except Exception:
            pass

    # Funding rate
    fr_json = safe_get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        {"symbol": "XRPUSDT"},
    )
    if fr_json and "lastFundingRate" in fr_json:
        try:
            result["funding_now_pct"] = float(fr_json["lastFundingRate"]) * 100
        except Exception:
            pass

    # Open interest
    oi_json = safe_get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        {"symbol": "XRPUSDT"},
    )
    if oi_json and "openInterest" in oi_json:
        try:
            oi_contracts = float(oi_json["openInterest"])
            if result["price"]:
                result["oi_usd"] = oi_contracts * result["price"]
        except Exception:
            pass

    # Funding history
    fh_json = safe_get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        {"symbol": "XRPUSDT", "limit": 200},
    )
    if fh_json:
        try:
            rates = [float(x["fundingRate"]) * 100 for x in fh_json[-90:]]
            result["funding_hist_pct"] = rates
        except Exception:
            pass

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
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    if api_key and api_secret and api_key.strip() and api_secret.strip():
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

            dep = safe_get(dep_url, None)
            wd = safe_get(wd_url, None)
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
        cached_val, _ = read_cached_binance_netflow()
        if cached_val is not None:
            result["binance_netflow_24h"] = cached_val
        else:
            result["binance_netflow_24h"] = 0.0

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

    result["xrpl_raw_inflow"] = raw_sum
    result["xrpl_weighted_inflow"] = weighted_sum
    result["xrpl_ripple_otc"] = ripple_otc

    return result


live = fetch_live()

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

fund_hist = live.get("funding_hist_pct") or [0.0]
fund_now = live.get("funding_now_pct") or 0.0
fund_z = (fund_now - np.mean(fund_hist)) / (
    np.std(fund_hist) if np.std(fund_hist) > 1e-8 else 1e-8
)

points = {
    "Funding Z-Score": max(0.0, fund_z * 22.0),
    "Whale Flow (XRPL, weighted)": max(
        0.0, (live.get("xrpl_weighted_inflow") or 0.0) / 60e6 * 14.0
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
# Data health banner
# =========================

issues, redis_notes = describe_data_health(live, news_payload)
if issues:
    st.warning("Data issues: " + ", ".join(issues))
if redis_notes:
    st.info("Redis/cache notes:\n- " + "\n- ".join(redis_notes))

# =========================
# UI — Metrics
# =========================

st.markdown("### Live Metrics")
c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric(
    "XRP Price",
    f"${live.get('price', 0.0):.4f}" if live.get("price") else "—",
)
c2.metric("XRP/BTC", f"{btc_ratio:.8f}" if btc_ratio else "—")
c3.metric("XRP/ETH", f"{eth_ratio:.8f}" if eth_ratio else "—")
c4.metric("Funding", f"{live.get('funding_now_pct', 0.0):+.4f}%")
c5.metric("OI (USD)", f"${(live.get('oi_usd') or 0.0)/1e9:.2f}B")
c6.metric("L/S Ratio", f"{live.get('long_short_ratio', 1.0):.2f}")

st.markdown("### Sentiment & Flow")
s1, s2, s3, s4, s5 = st.columns(5)
label = (
    "Inst. Sentiment EMA" if sent_mode == "Institutional Only" else "News Sentiment EMA"
)
s1.metric(label, f"{ema_sent:+.3f}", delta=f"{inst_sent:+.3f} now")
s2.metric("Bullish Intensity", f"{bull_intensity:+.3f}")
s3.metric("Bearish Intensity", f"{bear_intensity:+.3f}")
s4.metric(
    "XRPL Inflows (raw, M XRP)",
    f"{(live.get('xrpl_raw_inflow') or 0.0)/1e6:+.1f}",
)
s5.metric(
    "Ripple OTC → Exchanges (M XRP)",
    f"{(live.get('xrpl_ripple_otc') or 0.0)/1e6:+.1f}",
)

st.metric(
    "XRPL Inflows (weighted, M XRP)",
    f"{(live.get('xrpl_weighted_inflow') or 0.0)/1e6:+.1f}",
)

st.metric("Flippening Flow Score", f"{flip_score:.2f}")
st.write(
    f"XRP/BTC uplift vs EMA baseline: {btc_uplift_pct:+.2f}%  |  "
    f"XRP/ETH uplift vs EMA baseline: {eth_uplift_pct:+.2f}%"
)

# Score
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
    "XRPL Net Inflow (raw, M XRP)": (live.get("xrpl_raw_inflow") or 0.0) / 1e6,
    "XRPL Net Inflow (weighted, M XRP)": (live.get("xrpl_weighted_inflow") or 0.0)
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
