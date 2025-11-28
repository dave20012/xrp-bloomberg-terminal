#!/usr/bin/env python3
"""
setup_db.py

Creates required tables and indexes for the XRP Quant Console
on Railway PostgreSQL using the correct DB URL.

Automatically prefers DATABASE_PUBLIC_URL when available
(used for local connections). Falls back to private DATABASE_URL
when running inside Railway.

Requires:
    pip install psycopg2-binary python-dotenv
"""

import os
import sys
import psycopg2

def get_correct_db_url():
    """
    Prefer DATABASE_PUBLIC_URL (hosted Postgres reachable from local)
    Fall back to internal DATABASE_URL when running inside Railway.
    """
    public = os.getenv("DATABASE_PUBLIC_URL")
    private = os.getenv("DATABASE_URL")

    # Prefer the public one when local script is executed
    if public and "trolley.proxy" in public:
        return public

    # Fall back for workers inside Railway container
    if private:
        return private

    return None


SQL = """
DROP TABLE IF EXISTS market_history;

CREATE TABLE market_history (
    timestamp TIMESTAMPTZ PRIMARY KEY,
    price_close NUMERIC,
    volume NUMERIC,
    aggregated_oi_usd NUMERIC,
    funding_rate NUMERIC,
    long_short_ratio NUMERIC
);

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
    url = get_correct_db_url()

    if not url:
        print("❌ ERROR: No valid database URL found.")
        print("➡ Ensure Railway environment has:")
        print("   DATABASE_URL=${{ Postgres.DATABASE_URL }}")
        print("   DATABASE_PUBLIC_URL=<auto copy from connect tab>")
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

    print("🎉 Database setup complete!")
    print("▶ You can now run:")
    print("   💠 `import_backfill.py`  — seed historical market, OI & flows")
    print("   ⚡ `worker.py`          — start live ingestion loop")
    print("   📚 `store_backtest_results.py` — archive strategy signals")
    conn.close()


if __name__ == "__main__":
    main()
