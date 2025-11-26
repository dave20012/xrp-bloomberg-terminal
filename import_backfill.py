#!/usr/bin/env python3
"""
import_backfill.py
Fully automatic historical data import (worker mode).
"""

import os
import sys
import time
import logging
import pandas as pd
import psycopg2
from datetime import datetime, timedelta

# Correct modules for your repo:
from data_fetch import fetch_historical_market  # <-- UPDATED
from xrpl_flow import fetch_historical_flows    # <-- UPDATED

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(message)s"
)

# ---------- DB URL Selection ----------
def get_db_url():
    public = os.getenv("DATABASE_PUBLIC_URL")
    private = os.getenv("DATABASE_URL")
    # Prefer public (proxy) when running locally
    if public and "proxy" in public:
        return public
    return private or public

# ---------- Runtime Bounds ----------
def get_days():
    try:
        return int(os.getenv("BACKFILL_DAYS", "180"))
    except:
        return 180

# ---------- DB Write Helper ----------
def db_write(table: str, df: pd.DataFrame, conflict_key="timestamp"):
    if df.empty:
        logging.warning(f"⚠ No data to insert into table: {table}")
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
            ON CONFLICT ({conflict_key}) DO NOTHING;
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
        logging.info("📊 Fetching historical market data...")
        mk = fetch_historical_market(days)
        db_write("signals_snapshot", mk)
        logging.info("✔ Market backfill complete.")
    except Exception as e:
        logging.error(f"Market backfill fail: {e}")

    # 2) XRPL Flows
    try:
        logging.info("🌊 Importing historical XRPL flows...")
        flows = fetch_historical_flows(days)
        db_write("xrpl_flows", flows, conflict_key="ledger_index")
        logging.info("✔ XRPL flow backfill complete.")
    except Exception as e:
        logging.error(f"XRPL flow backfill fail: {e}")

    logging.info("🎉 Backfill fully done!")
    print("🎉 Backfill fully done!")

if __name__ == "__main__":
    main()
