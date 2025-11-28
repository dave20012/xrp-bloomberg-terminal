"""
migrate_db.py

Creates the market_history table used for backfilled market data.

Run on Railway:
    railway run python migrate_db.py
"""

import os
import psycopg2
from psycopg2.extras import execute_batch


DDL = """
CREATE TABLE IF NOT EXISTS market_history (
    timestamp        TIMESTAMPTZ PRIMARY KEY,
    price_close      DOUBLE PRECISION,
    volume           DOUBLE PRECISION,
    aggregated_oi_usd DOUBLE PRECISION,
    funding_rate     DOUBLE PRECISION,
    long_short_ratio DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_market_history_ts
    ON market_history (timestamp);
"""


def main():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("❌ DATABASE_URL not set in environment.")

    print("📡 Connecting to PostgreSQL...")
    conn = psycopg2.connect(db_url)
    conn.autocommit = True

    try:
        with conn.cursor() as cur:
            print("🚧 Applying DDL for market_history ...")
            cur.execute(DDL)
        print("🎉 Migration complete: market_history ready.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
