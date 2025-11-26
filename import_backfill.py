"""Import historical XRP market data into the TimescaleDB.

This script accepts a CSV file containing historical snapshots and
persists them into the TimescaleDB used by the XRP quant dashboard.
It computes relative volume (rVOL), open interest change and price
divergence on the fly and uses ``compute_signal_stack`` from
``main.py`` to derive composite scores.  The resulting records are
inserted via ``insert_signal_snapshot``.  Optionally it may also
populate the market_candles and derivatives_oi tables if the CSV
contains appropriate columns, although this is not required for
signal computation.

Usage:

    python import_backfill.py --csv path/to/historical.csv

The CSV must contain at least the following columns:

    timestamp (ISO 8601 or UNIX epoch seconds)
    price_close (float)
    volume (float)
    aggregated_oi_usd (float)
    funding_rate (float, optional)
    long_short_ratio (float, optional)

If the funding or long/short ratio columns are missing they will
default to None.  Additional columns are ignored.  The script sorts
rows by timestamp ascending and computes rVOL over a 20‑period window.

Environment variables required:

    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB

Example CSV format:

    timestamp,price_close,volume,aggregated_oi_usd,funding_rate,long_short_ratio
    2025-01-01T00:00:00Z,0.618,123456789,500000000,0.0001,1.2
    2025-01-01T00:05:00Z,0.620,110000000,510000000,0.00011,1.15
    ...
"""

from __future__ import annotations

import argparse
import csv
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from db import initialize_db, insert_signal_snapshot
import main as app_main


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def parse_csv(path: str) -> pd.DataFrame:
    """Load the CSV into a DataFrame with proper types and order."""
    df = pd.read_csv(path)
    if "timestamp" not in df.columns or "price_close" not in df.columns or "volume" not in df.columns or "aggregated_oi_usd" not in df.columns:
        raise ValueError(
            "CSV must contain timestamp, price_close, volume and aggregated_oi_usd columns"
        )
    # Parse timestamps (supports ISO8601 or seconds since epoch)
    def parse_ts(val: str) -> datetime:
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            # Assume seconds since epoch
            return datetime.fromtimestamp(float(val), tz=timezone.utc)

    df["timestamp"] = df["timestamp"].apply(parse_ts)
    df.sort_values("timestamp", inplace=True)
    # Ensure numeric types
    for col in ["price_close", "volume", "aggregated_oi_usd"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "funding_rate" in df.columns:
        df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce")
    else:
        df["funding_rate"] = None
    if "long_short_ratio" in df.columns:
        df["long_short_ratio"] = pd.to_numeric(df["long_short_ratio"], errors="coerce")
    else:
        df["long_short_ratio"] = None
    return df


def compute_signals(df: pd.DataFrame) -> List[Dict]:
    """Compute rVOL, OI change, divergence and composite for each row."""
    # Compute 20‑period SMA for volume and rVOL
    df = df.copy()
    df["rvol"] = df["volume"] / df["volume"].rolling(window=20, min_periods=20).mean()
    # Compute OI change by diffing aggregated_oi_usd
    df["oi_change"] = df["aggregated_oi_usd"].diff()
    # Compute price change for divergence
    df["price_change"] = df["price_close"].diff()
    # Divergence flag: price up & OI down or vice versa
    df["divergence"] = (
        (df["price_change"] > 0) & (df["oi_change"] < 0)
    ) | (
        (df["price_change"] < 0) & (df["oi_change"] > 0)
    )
    # Iterate rows and compute composite using compute_signal_stack
    results: List[Dict] = []
    for idx, row in df.iterrows():
        ts: datetime = row["timestamp"]
        price = row["price_close"]
        agg_oi = row["aggregated_oi_usd"]
        funding = row["funding_rate"]
        ls_ratio = row["long_short_ratio"]
        rvol = row["rvol"] if pd.notna(row["rvol"]) else None
        oi_change = row["oi_change"] if pd.notna(row["oi_change"]) else None
        divergence = bool(row["divergence"]) if pd.notna(row["divergence"]) else False
        price_dict = {"price": price}
        futures_dict = {
            "funding": funding,
            "open_interest": None,
            "aggregated_open_interest": agg_oi,
            "long_short_ratio": ls_ratio,
            "relative_volume": rvol,
        }
        # For backfill we cannot rely on real‑time flows or sentiment; pass empty structures
        flows = {"latest_inflow": 0.0, "latest_outflow": 0.0, "meta": {}, "history": None}
        sentiment = {"bull": 0.0, "bear": 0.0, "instant": 0.0, "ema": None, "articles": [], "timestamp": None}
        try:
            stack = app_main.compute_signal_stack(price_dict, futures_dict, flows, sentiment)
            composite = float(stack.get("composite", 0.0))
        except Exception:
            composite = 0.0
        results.append(
            {
                "timestamp": ts,
                "price": price,
                "oi_total": agg_oi,
                "funding_rt": funding,
                "ls_ratio": ls_ratio,
                "rvol": rvol,
                "oi_change": oi_change,
                "divergence": divergence,
                "composite_score": composite,
            }
        )
    return results


def main(csv_path: str) -> None:
    logging.info(f"Importing historical data from {csv_path}")
    df = parse_csv(csv_path)
    records = compute_signals(df)
    # Initialise the DB and insert snapshots
    initialize_db()
    for rec in records:
        iso_ts = rec["timestamp"].astimezone(timezone.utc).isoformat()
        snapshot_row = (
            iso_ts,
            rec["price"],
            rec["oi_total"],
            rec["funding_rt"],
            rec["ls_ratio"],
            rec["rvol"],
            rec["oi_change"],
            rec["divergence"],
            rec["composite_score"],
        )
        try:
            insert_signal_snapshot(snapshot_row)
        except Exception as exc:
            logging.warning(f"Failed to insert snapshot for {iso_ts}: {exc}")
    logging.info(f"Imported {len(records)} snapshots into the database")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill XRP market data into TimescaleDB")
    parser.add_argument("--csv", required=True, help="Path to the CSV file containing historical data")
    args = parser.parse_args()
    main(args.csv)