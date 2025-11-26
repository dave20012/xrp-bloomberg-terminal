#!/usr/bin/env python3
"""
import_backfill.py
Fully automatic historical data import
Runs as a worker script ONLY (no UI mode).

Pulls:
- Binance OHLCV + funding + OI
- Aggregated open interest normalization
- XRPL flows (best effort, skip 403s)
- Writes batched into DB

Requires:
    psycopg2-binary
    requests
    pandas
    python-dotenv (optional)
"""

import os
import sys
import time
import logging
import traceback
import pandas as pd
import psycopg2
from datetime import datetime, timedelta

from market_data import fetch_history  # you already have this
from xrpl_flow import pull_flows       # you already have this

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(message)s"
)

# ---------- DB URL Selection ----------
def get_db_url():
    public = os.getenv("DATABASE_PUBLIC_URL")
    private = os.getenv("DATABASE_URL")
    if public and "trolley.proxy" in public:
        return public
    if private:
        return private
    return None

# ---------- Runtime Bounds ----------
def get_days():
    try:
        return int(os.getenv("BACKFILL_DAYS", "180"))
    except:
        return 180

# ---------- DB Write Helper ----------
def db_write(table: str, df: pd.DataFrame):
    if df.empty:
        return

    url = get_db_url()
    if not url:
        raise ValueError("No DB URL found in environment.")

    conn = psycopg2.connect(url, sslmode="require")
    cur = conn.cursor()

    for _, row in df.iterrows():
        placeholders = ",".join(["%s"] * len(row))
        sql = f"""
            INSERT INTO {table} ({','.join(df.columns)})
            VALUES ({placeholders})
            ON CONFLICT (timestamp) DO NOTHING;
        """
        try:
            cur.execute(sql, tuple(row))
        except Exception as e:
            logging.error(f"Insert fail on {table}: {e}")
            continue

    conn.commit()
    cur.close()
    conn.close()

# ---------- MAIN ----------
def main():
    days = get_days()
    logging.info(f"📥 Starting backfill for {days} days...")

    # 1) Market + OI + Funding
    try:
        logging.info("📊 Fetching price + funding + OI...")
        mk = fetch_history(days)
        db_write("signals_snapshot", mk)
        logging.info("✔ Market backfill complete.")
    except Exception as e:
        logging.error("Market backfill fail:")
        logging.error(traceback.format_exc())

    # 2) XRPL Flows (best effort)
    try:
        logging.info("🌊 Importing XRPL flows...")
        flows = pull_flows(days)
        db_write("xrpl_flows", flows)
        logging.info("✔ XRPL flow backfill complete.")
    except Exception as e:
        logging.error("XRPL flow backfill fail:")
        logging.error(traceback.format_exc())

    logging.info("🎉 Backfill fully done!")
    print("🎉 Backfill fully done!")

if __name__ == "__main__":
    main()
