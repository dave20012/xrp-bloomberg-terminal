"""Database helper functions for the XRP quant dashboard.

This module provides a simple wrapper around a TimescaleDB (PostgreSQL)
instance. It includes functions to initialise the schema, connect to the
database and insert or upsert records for different metric tables.

Environment variables expected:

    PG_URL or DATABASE_URL: full PostgreSQL connection string (preferred)
    PG_HOST: hostname of the PostgreSQL server (used when URL is absent)
    PG_PORT: port of the PostgreSQL server (optional, default 5432)
    PG_USER: database user
    PG_PASSWORD: database password
    PG_DB: database name

TimescaleDB extension should be enabled on the server. The initialisation
function will attempt to create the extension and hypertables if they do
not already exist.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_batch

from app_utils import normalize_env_value


def _clean_env(name: str, *, default: str = "") -> str:
    """Return a trimmed environment variable, preserving an optional default."""

    raw = normalize_env_value(name)
    return raw if raw else default


def get_connection() -> psycopg2.extensions.connection:
    """Create a new database connection using environment variables.

    Preferred inputs:
    - ``PG_URL`` or ``DATABASE_URL`` set to a full PostgreSQL connection URL
      (e.g. ``postgresql://user:pass@host:5432/dbname?sslmode=require``).
    - Otherwise, individual ``PG_HOST``, ``PG_PORT``, ``PG_USER``, ``PG_PASSWORD``,
      and ``PG_DB`` values are used after being trimmed of stray quotes/whitespace.
    """

    # Allow a single URL-style secret to drive the connection for external hosts.
    pg_url = _clean_env("PG_URL") or _clean_env("DATABASE_URL")
    # Accept both postgres:// and postgresql:// schemes by normalising the URL.
    # Some deployment platforms (e.g. Railway, Heroku) prefix the connection string
    # with "postgres://", which is not recognised by psycopg2. Normalise this to
    # "postgresql://" so psycopg2 can parse it. Skip any placeholder values.
    if pg_url and not pg_url.startswith("${"):
        normalized_url = pg_url.replace("postgres://", "postgresql://")
        return psycopg2.connect(dsn=normalized_url)

    host = _clean_env("PG_HOST")
    user = _clean_env("PG_USER")
    dbname = _clean_env("PG_DB")

    if not all([host, user, dbname]):
        raise ValueError(
            "Missing database settings: set PG_URL/DATABASE_URL or PG_HOST/PG_USER/PG_DB."
        )

    return psycopg2.connect(
        host=host,
        port=_clean_env("PG_PORT", default=os.getenv("PG_PORT", "5432")),
        user=user,
        password=_clean_env("PG_PASSWORD"),
        dbname=dbname,
    )


def initialize_db() -> None:
    """
    Initialise the database schema.

    Creates the TimescaleDB extension and tables for market candles, derivatives
    open interest, on-chain flows, sentiment feed and signals snapshots. If
    tables already exist this function is a no-op. It also converts tables
    into hypertables to leverage Timescale's time-series optimisations.
    """
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            # Enable the TimescaleDB extension. Use CASCADE to ensure any
            # dependencies are installed as well. Without CASCADE the extension
            # may fail to load on fresh PostgreSQL instances.
            cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")

            # Market candles table: OHLCV for spot market
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS market_candles (
                    timestamp TIMESTAMPTZ PRIMARY KEY,
                    price_open NUMERIC,
                    price_high NUMERIC,
                    price_low NUMERIC,
                    price_close NUMERIC,
                    volume NUMERIC
                );
                """
            )
            # Turn into hypertable
            cur.execute(
                "SELECT create_hypertable('market_candles', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
            )

            # Derivatives open interest table: per exchange open interest and derivatives metrics
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS derivatives_oi (
                    timestamp TIMESTAMPTZ,
                    exchange TEXT,
                    oi_usd NUMERIC,
                    oi_coin NUMERIC,
                    funding_rt NUMERIC,
                    ls_ratio NUMERIC,
                    PRIMARY KEY (timestamp, exchange)
                );
                """
            )
            cur.execute(
                "SELECT create_hypertable('derivatives_oi', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
            )

            # On-chain flows table: XRPL inflows/outflows
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS onchain_flows (
                    timestamp TIMESTAMPTZ PRIMARY KEY,
                    flow_in_xrp NUMERIC,
                    flow_out_xrp NUMERIC,
                    net_flow_xrp NUMERIC
                );
                """
            )
            cur.execute(
                "SELECT create_hypertable('onchain_flows', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
            )

            # Sentiment feed table: holds raw headlines and sentiment scores
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sentiment_feed (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMPTZ,
                    headline TEXT,
                    source TEXT,
                    score_raw NUMERIC,
                    score_ema NUMERIC,
                    weight NUMERIC
                );
                """
            )
            # We do not convert sentiment_feed into a hypertable because it is small

            # Signals snapshot table: store computed composite and component scores for audit/backtesting
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS signals_snapshot (
                    timestamp TIMESTAMPTZ PRIMARY KEY,
                    price NUMERIC,
                    oi_total NUMERIC,
                    funding_rt NUMERIC,
                    ls_ratio NUMERIC,
                    rvol NUMERIC,
                    oi_change NUMERIC,
                    divergence BOOLEAN,
                    composite_score NUMERIC
                );
                """
            )
            cur.execute(
                "SELECT create_hypertable('signals_snapshot', 'timestamp', if_not_exists => TRUE, migrate_data => TRUE);"
            )
    conn.close()


def upsert_market_candles(rows: Iterable[Tuple]) -> None:
    """
    Bulk upsert market candle rows.

    Each row should be a tuple: (timestamp, open, high, low, close, volume).
    If a row already exists (same timestamp), it will be updated with the new
    values.
    """
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            execute_batch(
                cur,
                """
                INSERT INTO market_candles (timestamp, price_open, price_high, price_low, price_close, volume)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (timestamp)
                DO UPDATE SET price_open = EXCLUDED.price_open,
                               price_high = EXCLUDED.price_high,
                               price_low  = EXCLUDED.price_low,
                               price_close= EXCLUDED.price_close,
                               volume     = EXCLUDED.volume;
                """,
                list(rows),
            )
    conn.close()


def upsert_derivatives_oi(rows: Iterable[Tuple]) -> None:
    """
    Bulk upsert derivatives open interest rows.

    Each row should be: (timestamp, exchange, oi_usd, oi_coin, funding_rt, ls_ratio).
    On conflict of (timestamp, exchange), update metrics.
    """
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            execute_batch(
                cur,
                """
                INSERT INTO derivatives_oi (timestamp, exchange, oi_usd, oi_coin, funding_rt, ls_ratio)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (timestamp, exchange)
                DO UPDATE SET oi_usd    = EXCLUDED.oi_usd,
                               oi_coin   = EXCLUDED.oi_coin,
                               funding_rt= EXCLUDED.funding_rt,
                               ls_ratio  = EXCLUDED.ls_ratio;
                """,
                list(rows),
            )
    conn.close()


def insert_onchain_flow(timestamp: str, flow_in_xrp: float, flow_out_xrp: float) -> None:
    """
    Insert a single on-chain flow row.
    Computes net_flow_xrp automatically.
    """
    net_flow = flow_in_xrp - flow_out_xrp
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO onchain_flows (timestamp, flow_in_xrp, flow_out_xrp, net_flow_xrp)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (timestamp)
                DO UPDATE SET flow_in_xrp = EXCLUDED.flow_in_xrp,
                               flow_out_xrp = EXCLUDED.flow_out_xrp,
                               net_flow_xrp = EXCLUDED.net_flow_xrp;
                """,
                (timestamp, flow_in_xrp, flow_out_xrp, net_flow),
            )
    conn.close()


def insert_sentiment(timestamp: str, headline: str, source: str, score_raw: float, score_ema: float, weight: float) -> None:
    """
    Insert a sentiment headline with associated metrics.
    """
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sentiment_feed (timestamp, headline, source, score_raw, score_ema, weight)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (timestamp, headline, source, score_raw, score_ema, weight),
            )
    conn.close()


def insert_signal_snapshot(row: Tuple) -> None:
    """
    Insert or update a signals snapshot.

    Expects a tuple:
    (timestamp, price, oi_total, funding_rt, ls_ratio, rvol, oi_change, divergence, composite_score)
    """
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals_snapshot (timestamp, price, oi_total, funding_rt, ls_ratio, rvol, oi_change, divergence, composite_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (timestamp)
                DO UPDATE SET price          = EXCLUDED.price,
                               oi_total       = EXCLUDED.oi_total,
                               funding_rt     = EXCLUDED.funding_rt,
                               ls_ratio       = EXCLUDED.ls_ratio,
                               rvol           = EXCLUDED.rvol,
                               oi_change      = EXCLUDED.oi_change,
                               divergence      = EXCLUDED.divergence,
                               composite_score= EXCLUDED.composite_score;
                """
                """,
                row,
            )
    conn.close()


def fetch_latest_snapshot() -> Optional[Tuple]:
    """
    Retrieve the latest signals snapshot from the database.
    Returns a tuple or None if no data exists.
    """
    conn = get_connection()
    row: Optional[Tuple] = None
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT timestamp, price, oi_total, funding_rt, ls_ratio, rvol, oi_change, divergence, composite_score FROM signals_snapshot ORDER BY timestamp DESC LIMIT 1;"
            )
            result = cur.fetchone()
            if result:
                row = result
    conn.close()
    return row


def fetch_latest_flow() -> Optional[Tuple]:
    """Return the newest on-chain flow tuple from the database."""

    conn = get_connection()
    row: Optional[Tuple] = None
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT timestamp, flow_in_xrp, flow_out_xrp, net_flow_xrp FROM onchain_flows ORDER BY timestamp DESC LIMIT 1;"
            )
            result = cur.fetchone()
            if result:
                row = result
    conn.close()
    return row

