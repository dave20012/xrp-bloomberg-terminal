"""
import_backfill.py

Imports historical market data from a CSV (produced by fetch_history.py)
into the Postgres `market_history` table.

Assumes columns:
    timestamp, price_close, volume,
    aggregated_oi_usd, funding_rate, long_short_ratio

Usage:
    railway run python import_backfill.py --csv historical.csv
"""

from __future__ import annotations
import os
import argparse

import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        raise ValueError("CSV must contain 'timestamp' column.")
    # Basic cleaning; keep as-is, you already filtered future rows in fetch_history
    return df


def float_or_none(x):
    if pd.isna(x):
        return None
    return float(x)


def insert_market_history(df: pd.DataFrame, db_url: str, batch_size: int = 1000):
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            sql = """
            INSERT INTO market_history (
                timestamp,
                price_close,
                volume,
                aggregated_oi_usd,
                funding_rate,
                long_short_ratio
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (timestamp) DO UPDATE
            SET
                price_close       = EXCLUDED.price_close,
                volume            = EXCLUDED.volume,
                aggregated_oi_usd = EXCLUDED.aggregated_oi_usd,
                funding_rate      = EXCLUDED.funding_rate,
                long_short_ratio  = EXCLUDED.long_short_ratio;
            """

            rows = []
            for _, r in df.iterrows():
                rows.append(
                    (
                        r["timestamp"],
                        float_or_none(r.get("price_close")),
                        float_or_none(r.get("volume")),
                        float_or_none(r.get("aggregated_oi_usd")),
                        float_or_none(r.get("funding_rate")),
                        float_or_none(r.get("long_short_ratio")),
                    )
                )

            print(f"📥 Inserting {len(rows)} rows into market_history ...")
            for i in range(0, len(rows), batch_size):
                chunk = rows[i : i + batch_size]
                execute_batch(cur, sql, chunk)
                conn.commit()
                print(f"  ... {min(i + batch_size, len(rows))}/{len(rows)} committed")

    finally:
        conn.close()
    print("🎉 Import complete.")


def main():
    parser = argparse.ArgumentParser(description="Import historical market data CSV into Postgres.")
    parser.add_argument("--csv", required=True, help="Path to historical.csv")
    args = parser.parse_args()

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise SystemExit("❌ DATABASE_URL not set. Run via `railway run` or export locally.")

    df = load_csv(args.csv)
    insert_market_history(df, db_url)


if __name__ == "__main__":
    main()
