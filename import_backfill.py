#!/usr/bin/env python3
"""
import_backfill.py
Historical backfill for XRP Quant Console.
"""

import os
import logging
import psycopg2
import pandas as pd

from fetch_history import fetch_historical_market
from xrpl_inflow_monitor import fetch_historical_flows

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

# ---------- DB URL Selector ----------
def get_db_url():
    # Prefer public proxy for local runs
    public = os.getenv("DATABASE_PUBLIC_URL")
    private = os.getenv("DATABASE_URL")

    if public and "proxy" in public:
        return public
    return private or public

# ---------- Write Helper ----------
def insert_dataframe(table: str, df: pd.DataFrame, conflict_key="timestamp"):
    if df.empty:
        logging.warning(f"⚠ No data fetched for table: {table}")
        return

    url = get_db_url()
    if not url:
        raise ValueError("💥 No DATABASE_URL / DATABASE_PUBLIC_URL found.")

    conn = psycopg2.connect(url, sslmode="require")
    cur = conn.cursor()

    for _, row in df.iterrows():
        cols = ",".join(df.columns)
        placeholders = ",".join(["%s"] * len(row))
        sql = f"""
            INSERT INTO {table} ({cols})
            VALUES ({placeholders})
            ON CONFLICT ({conflict_key}) DO NOTHING;
        """
        try:
            cur.execute(sql, tuple(row))
        except Exception as e:
            logging.error(f"💥 Insert failed [{table}]: {e}")

    conn.commit()
    cur.close()
    conn.close()

# ---------- MAIN ----------
def main():
    days = int(os.getenv("BACKFILL_DAYS", "180"))
    logging.info(f"📥 Backfilling {days} days of data ..")

    # 1) Market Data
    try:
        logging.info("📊 Fetching market history (price, funding, OI)..")
        mk = fetch_historical_market(days)
        insert_dataframe("signals_snapshot", mk)
        logging.info("✔ Market history imported.")
    except Exception as e:
        logging.error(f"❌ Market backfill error: {e}")

    # 2) XRPL Flows
    try:
        logging.info("🌊 Fetching XRPL inflow/outflow history..")
        flows = fetch_historical_flows(days)
        insert_dataframe("xrpl_flows", flows, conflict_key="ledger_index")
        logging.info("✔ XRPL flows imported.")
    except Exception as e:
        logging.error(f"❌ XRPL flow backfill error: {e}")

    logging.info("🎉 FULL BACKFILL COMPLETE")

if __name__ == "__main__":
    main()
