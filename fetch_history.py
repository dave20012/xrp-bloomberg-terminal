"""
Historical market data fetching utilities for the XRP quant dashboard.

This script:
- Fetches 5m spot price & volume from Binance.
- Fetches open interest (OI) where Binance allows it (recent ~30 days max).
- Fetches global long/short account ratio (LS) where available (same window).
- Fetches funding rates (8h buckets) and aligns them to 5m bars.
- Safely handles long date ranges by chunking.
- Clamps future end dates to 'now' to avoid invalid Binance requests.
- Drops ONLY future rows that have neither OI nor LS (your requested rule).

Output CSV columns:
    timestamp (ISO 8601 string, UTC)
    price_close (float)
    volume (float)
    aggregated_oi_usd (float or None)
    funding_rate (float or None)
    long_short_ratio (float or None)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests
from dateutil.parser import isoparse

# -------------------------------------------------------------------
# Binance endpoints / constants
# -------------------------------------------------------------------
BINANCE_SPOT = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FUTURES_DATA = "https://fapi.binance.com/futures/data"

SYMBOL = "XRPUSDT"
INTERVAL = "5m"
FIVE_MIN_MS = 5 * 60 * 1000
EIGHT_HOURS_MS = 8 * 60 * 60 * 1000

# Chunk size for spot/funding/LS fetching
MAX_CHUNK_DAYS = 30

# Binance derivatives HTTP retention (conservative)
OI_LOOKBACK_DAYS = 30  # OI + LS beyond this are not reliably served by HTTP


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _safe_get(url: str, params: Dict[str, str], *, max_retries: int = 3, sleep_sec: float = 1.0):
    """Perform a GET request with retries for transient errors."""
    last_err: Optional[Exception] = None
    for _ in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                # Rate limit, back off a bit
                time.sleep(2 * sleep_sec)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as err:
            last_err = err
            time.sleep(sleep_sec)
    raise RuntimeError(f"Failed GET {url}: {last_err}")


def _nearest_leq(ts: int, series: List[Dict], key: str) -> Optional[float]:
    """Return the value for which 'time' <= ts and closest to ts."""
    best: Optional[float] = None
    for row in series:
        if row["time"] <= ts:
            best = row[key]
        else:
            break
    return best


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# -------------------------------------------------------------------
# Spot OHLCV
# -------------------------------------------------------------------
def fetch_spot_klines(symbol: str, start: datetime, end: datetime) -> List[Dict]:
    """Fetch spot 5m OHLCV from /api/v3/klines."""
    out: List[Dict] = []
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": 1000,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = _safe_get(f"{BINANCE_SPOT}/api/v3/klines", params)
        if not data:
            break

        for k in data:
            ts = int(k[0])
            if ts >= end_ms:
                break
            out.append(
                {
                    "time": ts,
                    "price_close": float(k[4]),
                    "volume": float(k[5]),
                }
            )

        last_open = int(data[-1][0])
        start_ms = last_open + FIVE_MIN_MS
        time.sleep(0.05)

    return out


# -------------------------------------------------------------------
# Open Interest – best-effort, last ~30 days only
# -------------------------------------------------------------------
def fetch_oi_hist(symbol: str, start: datetime, end: datetime) -> List[Dict]:
    """
    Fetch OI history from /futures/data/openInterestHist.

    Binance only supports relatively recent history (docs and practice show ~30 days).
    We clamp any start earlier than (now - OI_LOOKBACK_DAYS) and
    if the requested window is entirely older, we return [] and DO NOT raise.
    """
    out: List[Dict] = []
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    now = datetime.now(timezone.utc)
    min_start = now - timedelta(days=OI_LOOKBACK_DAYS)

    # If entire period is older than Binance's retention window, skip OI.
    if end <= min_start:
        print("ℹ OI: requested window is older than Binance retention; skipping OI (will be null).")
        return out

    # Clamp start time to min_start to avoid invalid startTime.
    if start < min_start:
        print(f"ℹ OI: clamping start from {start.date()} to {min_start.date()} to satisfy Binance limits.")
        start = min_start

    # Also ensure we never ask past 'now'
    if end > now:
        print(f"ℹ OI: clamping end from {end.date()} to {now.date()} (no future data).")
        end = now

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 500

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": INTERVAL,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        try:
            data = _safe_get(f"{BINANCE_FUTURES_DATA}/openInterestHist", params)
        except RuntimeError as e:
            print(f"⚠ OI fetch failed: {e}. Skipping OI for this window.")
            return []

        if not data:
            break

        for row in data:
            ts = int(row["timestamp"])
            if ts >= end_ms:
                break
            out.append({"time": ts, "oi": float(row["sumOpenInterest"])})

        last_ts = int(data[-1]["timestamp"])
        start_ms = last_ts + FIVE_MIN_MS
        time.sleep(0.05)

    return out


# -------------------------------------------------------------------
# Long/Short account ratio – same retention assumption as OI
# -------------------------------------------------------------------
def fetch_ls_ratio(symbol: str, start: datetime, end: datetime) -> List[Dict]:
    """Fetch global long/short account ratio history."""
    out: List[Dict] = []
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    now = datetime.now(timezone.utc)
    min_start = now - timedelta(days=OI_LOOKBACK_DAYS)

    if end <= min_start:
        print("ℹ LS ratio: requested window older than retention; skipping LS (will be null).")
        return out

    if start < min_start:
        print(f"ℹ LS ratio: clamping start from {start.date()} to {min_start.date()}.")
        start = min_start

    if end > now:
        print(f"ℹ LS ratio: clamping end from {end.date()} to {now.date()} (no future data).")
        end = now

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 500

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": INTERVAL,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        try:
            data = _safe_get(f"{BINANCE_FUTURES_DATA}/globalLongShortAccountRatio", params)
        except RuntimeError as e:
            print(f"⚠ LS ratio fetch failed: {e}. Skipping LS for this window.")
            return []

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
        start_ms = last_ts + FIVE_MIN_MS
        time.sleep(0.05)

    return out


# -------------------------------------------------------------------
# Funding history (8h buckets)
# -------------------------------------------------------------------
def fetch_funding_hist(symbol: str, start: datetime, end: datetime) -> List[Dict]:
    """Fetch funding rate history (8h buckets) from /fapi/v1/fundingRate."""
    out: List[Dict] = []
    start = _ensure_utc(start)
    end = _ensure_utc(end)
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
        try:
            data = _safe_get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params)
        except RuntimeError as e:
            print(f"⚠ Funding fetch failed: {e}. Skipping funding for this window.")
            return []

        if not data:
            break

        for row in data:
            ts = int(row["fundingTime"])
            if ts >= end_ms:
                break
            out.append({"time": ts, "rate": float(row["fundingRate"])})

        last_ts = int(data[-1]["fundingTime"])
        start_ms = last_ts + EIGHT_HOURS_MS
        time.sleep(0.05)

    return out


# -------------------------------------------------------------------
# Chunked historical fetch (spot + OI + LS + funding)
# -------------------------------------------------------------------
def fetch_historical_market(start: datetime, end: datetime) -> pd.DataFrame:
    """
    Fetch historical market and derivatives data for XRPUSDT across [start, end).

    Behaviour:
      - Clamps end to <= now (no future queries).
      - Splits the range into MAX_CHUNK_DAYS chunks.
      - For each chunk, fetches spot, OI, LS, funding and merges into 5m bars.
      - At the end, drops ONLY future rows where both OI and LS are null.
    """
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    now = datetime.now(timezone.utc)
    if end > now:
        print(f"⚠ Requested end {end.date()} is in the future. Clamping to {now.date()}.")
        end = now

    all_records: List[Dict] = []

    cur = start
    while cur < end:
        chunk_end = min(cur + timedelta(days=MAX_CHUNK_DAYS), end)
        print(f"⏱ Fetching chunk: {cur.date()} → {chunk_end.date()}")

        # Fetch each data source (best effort)
        klines = fetch_spot_klines(SYMBOL, cur, chunk_end)
        oi_hist = fetch_oi_hist(SYMBOL, cur, chunk_end)
        ls_hist = fetch_ls_ratio(SYMBOL, cur, chunk_end)
        fund_hist = fetch_funding_hist(SYMBOL, cur, chunk_end)

        # Sort for nearest_leq
        oi_sorted = sorted(oi_hist, key=lambda x: x["time"])
        ls_sorted = sorted(ls_hist, key=lambda x: x["time"])
        fund_sorted = sorted(fund_hist, key=lambda x: x["time"])

        for k in klines:
            ts = k["time"]
            price = k["price_close"]
            vol = k["volume"]

            oi_coin = _nearest_leq(ts, oi_sorted, "oi") if oi_sorted else None
            ls_ratio = _nearest_leq(ts, ls_sorted, "ls") if ls_sorted else None
            funding_rate = _nearest_leq(ts, fund_sorted, "rate") if fund_sorted else None

            agg_oi_usd = (oi_coin * price) if oi_coin is not None else None

            all_records.append(
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

        cur = chunk_end

    df = pd.DataFrame.from_records(all_records)
    if df.empty:
        return df

    # Ensure chronological
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    # Drop ONLY future rows where BOTH OI & LS are null
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], utc=True)
    now_utc = datetime.now(timezone.utc)
    mask_future = df["timestamp_dt"] > now_utc
    mask_both_null = df["aggregated_oi_usd"].isna() & df["long_short_ratio"].isna()
    df = df[~(mask_future & mask_both_null)]
    df.drop(columns=["timestamp_dt"], inplace=True)

    return df


# -------------------------------------------------------------------
# CLI entrypoint
# -------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch historical XRP/USDT 5m market data from Binance and write to CSV."
    )
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--outfile", required=True, help="Output CSV filename")

    args = parser.parse_args()

    start_date = isoparse(args.start)
    end_date = isoparse(args.end)

    df = fetch_historical_market(start_date, end_date)
    df.to_csv(args.outfile, index=False)
    print(f"🎉 Wrote {len(df)} rows to {args.outfile}")
