#!/usr/bin/env python3
"""
setup_db.py

Creates required tables and indexes for the XRP Quant Console
on Railway PostgreSQL using the `DATABASE_URL` environment variable.

Run:
    python setup_db.py

Requires:
    pip install psycopg2-binary python-dotenv
"""

import os
import sys
import psycopg2
from urllib.parse import urlparse


SQL = """
CREATE TABLE IF NOT EXISTS signals_snapshot (
    timestamp        TIMESTAMPTZ PRIMARY KEY,
    price_usd        NUMERIC,
    volume           NUMERIC,
    aggregated_oi_usd NUMERIC,
    funding_rate     NUMERIC,
    long_short_ratio NUMERIC,
    rvol             NUMERIC,
    oi_change        NUMERIC,
    divergence       BOOLEAN,
    composite_score  NUMERIC
);

CREATE TABLE IF NOT EXISTS backtest_results (
    timestamp         TIMESTAMPTZ PRIMARY KEY,
    strategy_return   NUMERIC,
    buy_hold_return   NUMERIC
);

CREATE TABLE IF NOT EXISTS xrpl_flows (
    timestamp        TIMESTAMPTZ PRIMARY KEY,
    inflow_xrp       NUMERIC,
    outflow_xrp      NUMERIC,
    netflow_xrp      NUMERIC
);

CREATE INDEX IF NOT EXISTS idx_snapshot_time ON signals_snapshot (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_time ON backtest_results (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_flows_time ON xrpl_flows (timestamp DESC);
"""


def main():
    url = os.getenv("DATABASE_URL")

    if not url:
        print("❌ ERROR: DATABASE_URL environment variable not found.")
        print("➡ Set it in Railway (using `${{ Postgres.DATABASE_URL }}`), then try again.")
        sys.exit(1)

    print(f"📡 Connecting to PostgreSQL...")
    try:
        conn = psycopg2.connect(url, sslmode="require")
    except Exception as e:
        print("❌ Failed to connect to the database:")
        print(str(e))
        sys.exit(1)

    print("🚧 Creating tables & indexes...")
    try:
        with conn.cursor() as cur:
            cur.execute(SQL)
        conn.commit()
    except Exception as e:
        print("❌ Error applying schema:")
        print(str(e))
        conn.rollback()
        sys.exit(1)

    print("✅ Database setup complete!")
    print("🎉 You can now:")
    print("   • Run `worker.py` to ingest live data")
    print("   • Run `import_backfill.py` to seed historical data")
    print("   • Run `store_backtest_results.py` to archive strategy results")
    conn.close()


if __name__ == "__main__":
    main()
