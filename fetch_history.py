#!/usr/bin/env python3
"""
fetch_history.py

Fetch historical XRP market + derivatives data from Binance and write it to CSV
in the format expected by `import_backfill.py`.

It pulls:
  - 5m spot klines (price_close, volume)
  - 5m open interest history (XRPUSDT perp)
  - 5m global long/short account ratio
  - 8h funding rate history mapped to the nearest 5m bar

Usage (Windows PowerShell example):

    python fetch_history.py ^
        --symbol XRPUSDT ^
        --start 2024-01-01 ^
        --end 2024-02-01 ^
        --outfile data/binance_xrp_5m.csv

Then backfill:

    python import_backfill.py --csv data/binance_xrp_5m.csv
"""

import argparse
import csv
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional

import requests


BINANCE_SPOT = "https://api.binance.com"
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_FUTURES_DATA = "https://fapi.binance.com/futures/data"


def parse_date(s: str) -> datetime:
    """Parse YYYY-MM-DD or ISO8601 into UTC datetime."""
    if len(s) == 10:
        dt = datetime.fromisoformat(s)
    else:
        s_clean = s.replace("Z", "")
        dt = datetime.fromisoformat(s_clean)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def safe_get(url: str, params: Dict, max_retries: int = 3, sleep_sec: int = 1):
    last_err = None
    for _ in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                # rate limited
                time.sleep(2 * sleep_sec)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            time.sleep(sleep_sec)
    raise RuntimeError(f"Failed GET {url} after {max_retries} retries: {last_err}")


def fetch_spot_klines(symbol: str, interval: str,
                      start: datetime, end: datetime) -> List[Dict]:
    """Fetch 5m spot klines for [start, end) from /api/v3/klines."""
    out: List[Dict] = []
    start_ms = ms(start)
    end_ms = ms(end)
    limit = 1000  # max per call

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = safe_get(f"{BINANCE_SPOT}/api/v3/klines", params)
        if not data:
            break
        for k in data:
            open_time = int(k[0])
            if open_time >= end_ms:
                break
            close_price = float(k[4])
            volume = float(k[5])
            out.append(
                {
                    "open_time": open_time,
                    "close_price": close_price,
                    "volume": volume,
                }
            )
        # Move start_ms forward
        last_open = int(data[-1][0])
        # interval is 5m => 5 * 60 * 1000
        start_ms = last_open + 5 * 60 * 1000
        # Avoid hammering
        time.sleep(0.15)
    return out


def fetch_oi_hist(symbol: str, period: str,
                  start: datetime, end: datetime) -> List[Dict]:
    """Fetch open interest history from /futures/data/openInterestHist."""
    out: List[Dict] = []
    start_ms = ms(start)
    end_ms = ms(end)
    limit = 500

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = safe_get(f"{BINANCE_FUTURES_DATA}/openInterestHist", params)
        if not data:
            break
        for row in data:
            ts = int(row["timestamp"])
            if ts >= end_ms:
                break
            oi = float(row["sumOpenInterest"])
            out.append({"time": ts, "oi": oi})
        last_ts = int(data[-1]["timestamp"])
        start_ms = last_ts + 5 * 60 * 1000
        time.sleep(0.15)
    return out


def fetch_ls_ratio(symbol: str, period: str,
                   start: datetime, end: datetime) -> List[Dict]:
    """Fetch global long/short account ratio history."""
    out: List[Dict] = []
    start_ms = ms(start)
    end_ms = ms(end)
    limit = 500

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "period": period,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = safe_get(
            f"{BINANCE_FUTURES_DATA}/globalLongShortAccountRatio", params
        )
        if not data:
            break
        for row in data:
            ts = int(row["timestamp"])
            if ts >= end_ms:
                break
            long_ratio = float(row["longAccount"])
            short_ratio = float(row["shortAccount"])
            # convert to long/short ratio ( >= 0 )
            ls = long_ratio / max(short_ratio, 1e-9)
            out.append({"time": ts, "ls": ls})
        last_ts = int(data[-1]["timestamp"])
        start_ms = last_ts + 5 * 60 * 1000
        time.sleep(0.15)
    return out


def fetch_funding_hist(symbol: str,
                       start: datetime, end: datetime) -> List[Dict]:
    """
    Fetch funding rate history (8h buckets) from /fapi/v1/fundingRate.
    We'll later map it to 5m bars by "last known" value.
    """
    out: List[Dict] = []
    start_ms = ms(start)
    end_ms = ms(end)
    limit = 1000

    while start_ms < end_ms:
        params = {
            "symbol": symbol,
            "limit": limit,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        data = safe_get(f"{BINANCE_FAPI}/fapi/v1/fundingRate", params)
        if not data:
            break
        for row in data:
            ts = int(row["fundingTime"])
            if ts >= end_ms:
                break
            rate = float(row["fundingRate"])
            out.append({"time": ts, "rate": rate})
        last_ts = int(data[-1]["fundingTime"])
        # funding is 8h; jump forward 8h
        start_ms = last_ts + 8 * 60 * 60 * 1000
        time.sleep(0.15)
    return out


def nearest_leq(ts: int, series: List[Tuple[int, float]]) -> Optional[float]:
    """
    Given a sorted list of (timestamp_ms, value), return the value whose
    timestamp <= ts and is closest to ts. If none, return None.
    """
    # simple linear scan backwards is fine for small windows; you can optimize
    # with binary search if needed.
    best = None
    for t, v in series:
        if t <= ts:
            best = v
        else:
            break
    return best


def build_rows(
    klines: List[Dict],
    oi_hist: List[Dict],
    ls_hist: List[Dict],
    funding_hist: List[Dict],
) -> List[Dict]:
    """Combine all series into a single list of rows keyed by kline open time."""
    # sort inputs
    klines_sorted = sorted(klines, key=lambda x: x["open_time"])
    oi_sorted = sorted((x["time"], x["oi"]) for x in oi_hist)
    ls_sorted = sorted((x["time"], x["ls"]) for x in ls_hist)
    fund_sorted = sorted((x["time"], x["rate"]) for x in funding_hist)

    rows: List[Dict] = []

    for k in klines_sorted:
        ts = k["open_time"]
        close_price = k["close_price"]
        vol = k["volume"]

        oi_coin = nearest_leq(ts, oi_sorted)
        ls_ratio = nearest_leq(ts, ls_sorted)
        funding_rate = nearest_leq(ts, fund_sorted)

        if oi_coin is None:
            agg_oi_usd = None
        else:
            agg_oi_usd = oi_coin * close_price

        rows.append(
            {
                "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "price_close": close_price,
                "volume": vol,
                "aggregated_oi_usd": agg_oi_usd,
                "funding_rate": funding_rate,
                "long_short_ratio": ls_ratio,
            }
        )
    return rows


def write_csv(rows: List[Dict], outfile: str) -> None:
    os.makedirs(os.path.dirname(outfile), exist_ok=True)
    fieldnames = [
        "timestamp",
        "price_close",
        "volume",
        "aggregated_oi_usd",
        "funding_rate",
        "long_short_ratio",
    ]
    with open(outfile, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch historical XRP data from Binance and export CSV."
    )
    parser.add_argument(
        "--symbol",
        default="XRPUSDT",
        help="Binance symbol (default: XRPUSDT)",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Start date (YYYY-MM-DD or ISO8601)",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date (YYYY-MM-DD or ISO8601, exclusive bound)",
    )
    parser.add_argument(
        "--outfile",
        required=True,
        help="Output CSV file path",
    )
    args = parser.parse_args()

    start = parse_date(args.start)
    end = parse_date(args.end)

    print(f"Fetching spot klines {args.symbol} 5m from {start} to {end} ...")
    klines = fetch_spot_klines(args.symbol, "5m", start, end)
    print(f"Got {len(klines)} klines")

    print("Fetching open interest history (5m)...")
    oi_hist = fetch_oi_hist(args.symbol, "5m", start, end)
    print(f"Got {len(oi_hist)} OI points")

    print("Fetching long/short ratio history (5m)...")
    ls_hist = fetch_ls_ratio(args.symbol, "5m", start, end)
    print(f"Got {len(ls_hist)} L/S ratio points")

    print("Fetching funding rate history (8h)...")
    funding_hist = fetch_funding_hist(args.symbol, start, end)
    print(f"Got {len(funding_hist)} funding points")

    print("Combining series into rows...")
    rows = build_rows(klines, oi_hist, ls_hist, funding_hist)
    print(f"Final rows: {len(rows)}")

    print(f"Writing CSV to {args.outfile} ...")
    write_csv(rows, args.outfile)
    print("Done.")


if __name__ == "__main__":
    main()
