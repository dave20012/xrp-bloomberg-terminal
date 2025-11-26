"""
Historical market data fetching utilities for the XRP quant dashboard.

This module provides helper functions to download historical market and
derivatives statistics for a given symbol from public endpoints.  It
includes routines to pull spot prices/volumes, open interest history,
funding rates and long/short account ratios from Binance's public API.

The primary entry point is ``fetch_historical_market(days)`` which
returns a pandas ``DataFrame`` covering the past ``days`` worth of
5‑minute bars for XRP/USDT.  Each row contains:

    timestamp (ISO 8601 string)
    price_close (float)
    volume (float)
    aggregated_oi_usd (float) – open interest in base coin multiplied by
                               the close price to normalise to USD
    funding_rate (float or None)
    long_short_ratio (float or None)

These records can then be ingested into the TimescaleDB via the
``import_backfill.py`` script.  The functions defined here do not
require any API key as they target Binance's public futures and spot
endpoints.

Note: Network exceptions are propagated to the caller.  Consumers
should handle exceptions and implement retries where appropriate.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests

BINANCE_SPOT = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FUTURES_DATA = "https://fapi.binance.com/futures/data"


def _safe_get(url: str, params: Dict[str, str], *, max_retries: int = 3, sleep_sec: float = 1.0):
    """Perform a GET request with basic retry logic for transient errors."""
    last_err: Optional[Exception] = None
    for _ in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                _time.sleep(2 * sleep_sec)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as err:
            last_err = err
            _time.sleep(sleep_sec)
    raise RuntimeError(f"Failed GET {url}: {last_err}")


def _fetch_spot_klines(symbol: str, interval: str, start: datetime, end: datetime) -> List[Dict[str, float]]:
    """Fetch spot OHLCV data from Binance /api/v3/klines."""
    out: List[Dict[str, float]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 1000
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = _safe_get(f"{BINANCE_SPOT}/api/v3/klines", params)
        if not data:
            break
        for k in data:
            open_time = int(k[0])
            if open_time >= end_ms:
                break
            close_price = float(k[4])
            volume = float(k[5])
            out.append({"open_time": open_time, "close_price": close_price, "volume": volume})
        last_open = int(data[-1][0])
        start_ms = last_open + 5 * 60 * 1000
        _time.sleep(0.1)
    return out


def _fetch_oi_hist(symbol: str, period: str, start: datetime, end: datetime) -> List[Dict[str, float]]:
    """Fetch open interest history from /futures/data/openInterestHist."""
    out: List[Dict[str, float]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 500
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = _safe_get(f"{BINANCE_FUTURES_DATA}/openInterestHist", params)
        if not data:
            break
        for row in data:
            ts = int(row["timestamp"])
            if ts >= end_ms:
                break
            out.append({"time": ts, "oi": float(row["sumOpenInterest"])})
        last_ts = int(data[-1]["timestamp"])
        start_ms = last_ts + 5 * 60 * 1000
        _time.sleep(0.1)
    return out


def _fetch_ls_ratio(symbol: str, period: str, start: datetime, end: datetime) -> List[Dict[str, float]]:
    """Fetch global long/short account ratio history."""
    out: List[Dict[str, float]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 500
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = _safe_get(f"{BINANCE_FUTURES_DATA}/globalLongShortAccountRatio", params)
        if not data:
            break
        for row in data:
            ts = int(row["timestamp"])
            if ts >= end_ms:
                break
            long_ratio = float(row["longAccount"])
            short_ratio = float(row["shortAccount"])
            ls = long_ratio / max(short_ratio, 1e-9)
            out.append({"time": ts, "ls": ls})
        last_ts = int(data[-1]["timestamp"])
        start_ms = last_ts + 5 * 60 * 1000
        _time.sleep(0.1)
    return out


def _fetch_funding_hist(symbol: str, start: datetime, end: datetime) -> List[Dict[str, float]]:
    """Fetch funding rate history (8h buckets) from /fapi/v1/fundingRate."""
    out: List[Dict[str, float]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 1000
    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = _safe_get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params)
        if not data:
            break
        for row in data:
            ts = int(row["fundingTime"])
            if ts >= end_ms:
                break
            out.append({"time": ts, "rate": float(row["fundingRate"])})
        last_ts = int(data[-1]["fundingTime"])
        start_ms = last_ts + 8 * 60 * 60 * 1000
        _time.sleep(0.1)
    return out


def _nearest_leq(ts: int, series: List[Tuple[int, float]]) -> Optional[float]:
    """Return the value whose timestamp <= ts and is closest to ts."""
    best: Optional[float] = None
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def fetch_historical_market(days: int) -> pd.DataFrame:
    """Fetch historical market and derivatives data for XRPUSDT."""
    if days <= 0:
        raise ValueError("days must be a positive integer")
    end_time = datetime.utcnow().replace(tzinfo=timezone.utc)
    start_time = end_time - timedelta(days=days)
    symbol = "XRPUSDT"
    klines = _fetch_spot_klines(symbol, "5m", start_time, end_time)
    oi_hist = _fetch_oi_hist(symbol, "5m", start_time, end_time)
    ls_hist = _fetch_ls_ratio(symbol, "5m", start_time, end_time)
    fund_hist = _fetch_funding_hist(symbol, start_time, end_time)
    oi_sorted = sorted([(x["time"], x["oi"]) for x in oi_hist])
    ls_sorted = sorted([(x["time"], x["ls"]) for x in ls_hist])
    fund_sorted = sorted([(x["time"], x["rate"]) for x in fund_hist])
    records: List[Dict[str, Optional[float]]] = []
    for k in klines:
        ts = k["open_time"]
        price = k["close_price"]
        vol = k["volume"]
        oi_coin = _nearest_leq(ts, oi_sorted)
        ls_ratio = _nearest_leq(ts, ls_sorted)
        funding_rate = _nearest_leq(ts, fund_sorted)
        agg_oi_usd = (oi_coin * price) if oi_coin is not None else None
        records.append(
            {
                "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "price_close": price,
                "volume": vol,
                "aggregated_oi_usd": agg_oi_usd,
                "funding_rate": funding_rate,
                "long_short_ratio": ls_ratio,
            }
        )
    df = pd.DataFrame.from_records(records)
    df.sort_values("timestamp", inplace=True)
    return df
