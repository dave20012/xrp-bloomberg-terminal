"""Database helper functions for the XRP quant dashboard.

This module implements PostgreSQL access for TimescaleDB-compatible
schema, but operates safely on plain PostgreSQL (such as Railway)
because hypertable creation steps are wrapped in non-fatal optional calls.

Environment variables supported:

    PG_URL or DATABASE_URL  – preferred full connection string
    PG_HOST
    PG_PORT
    PG_USER
    PG_PASSWORD
    PG_DB

All functions are safe to call on startup; schema will self-initialize.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Tuple

import psycopg2
from psycopg2.extras import execute_batch

from app_utils import normalize_env_value


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _clean_env(name: str, *, default: str = "") -> str:
    """Return normalized env var, ensuring consistent formatting."""
    raw = normalize_env_value(name)
    return raw if raw else default


def get_connection() -> psycopg2.extensions.connection:
    """Return a fresh PostgreSQL connection using the most sensible env inputs."""
    pg_url = _clean_env("PG_URL") or _clean_env("DATABASE_URL")

    if pg_url and not pg_url.startswith("${"):
        norm = pg_url.replace("postgres://", "postgresql://")
        return psycopg2.connect(dsn=norm)

    host = _clean_env("PG_HOST")
    user = _clean_env("PG_USER")
    dbname = _clean_env("PG_DB")

    if not all([host, user, dbname]):
        raise ValueError(
            "Database credentials missing: set PG_URL/DATABASE_URL or PG_HOST/PG_USER/PG_DB."
        )

    return psycopg2.connect(
        host=host,
        port=_clean_env("PG_PORT", default=os.getenv("PG_PORT", "5432")),
        user=user,
        password=_clean_env("PG_PASSWORD"),
        dbname=dbname,
    )


def _optional(cur, sql: str, label: str) -> None:
    """Run optional SQL (Timescale extension/hypertable). Never fatal."""
    try:
        cur.execute(sql)
    except Exception as exc:
        print(f"[db] Optional step skipped ({label}): {exc}")


# ---------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------

def initialize_db() -> None:
    """Create tables if needed; remain safe on plain PostgreSQL."""
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:

            _optional(
                cur,
                "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;",
                "timescaledb extension"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS market_candles (
                    timestamp   TIMESTAMPTZ PRIMARY KEY,
                    price_open  NUMERIC,
                    price_high  NUMERIC,
                    price_low   NUMERIC,
                    price_close NUMERIC,
                    volume      NUMERIC
                );
                """
            )
            _optional(
                cur,
                "SELECT create_hypertable('market_candles','timestamp',if_not_exists=>TRUE,migrate_data=>TRUE);",
                "hypertable market_candles"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS derivatives_oi (
                    timestamp   TIMESTAMPTZ,
                    exchange    TEXT,
                    oi_usd      NUMERIC,
                    oi_coin     NUMERIC,
                    funding_rt  NUMERIC,
                    ls_ratio    NUMERIC,
                    PRIMARY KEY (timestamp, exchange)
                );
                """
            )
            _optional(
                cur,
                "SELECT create_hypertable('derivatives_oi','timestamp',if_not_exists=>TRUE,migrate_data=>TRUE);",
                "hypertable derivatives_oi"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS onchain_flows (
                    timestamp    TIMESTAMPTZ PRIMARY KEY,
                    flow_in_xrp  NUMERIC,
                    flow_out_xrp NUMERIC,
                    net_flow_xrp NUMERIC
                );
                """
            )
            _optional(
                cur,
                "SELECT create_hypertable('onchain_flows','timestamp',if_not_exists=>TRUE,migrate_data=>TRUE);",
                "hypertable onchain_flows"
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sentiment_feed (
                    id         SERIAL PRIMARY KEY,
                    timestamp  TIMESTAMPTZ,
                    headline   TEXT,
                    source     TEXT,
                    score_raw  NUMERIC,
                    score_ema  NUMERIC,
                    weight     NUMERIC
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS signals_snapshot (
                    timestamp       TIMESTAMPTZ PRIMARY KEY,
                    price           NUMERIC,
                    oi_total        NUMERIC,
                    funding_rt      NUMERIC,
                    ls_ratio        NUMERIC,
                    rvol            NUMERIC,
                    oi_change       NUMERIC,
                    divergence      BOOLEAN,
                    composite_score NUMERIC
                );
                """
            )
            _optional(
                cur,
                "SELECT create_hypertable('signals_snapshot','timestamp',if_not_exists=>TRUE,migrate_data=>TRUE);",
                "hypertable signals_snapshot"
            )

    conn.close()


# ---------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------

def upsert_market_candles(rows: Iterable[Tuple]) -> None:
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            execute_batch(
                cur,
                """
                INSERT INTO market_candles (timestamp, price_open, price_high, price_low, price_close, volume)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (timestamp) DO UPDATE SET
                    price_open  = EXCLUDED.price_open,
                    price_high  = EXCLUDED.price_high,
                    price_low   = EXCLUDED.price_low,
                    price_close = EXCLUDED.price_close,
                    volume      = EXCLUDED.volume;
                """,
                list(rows),
            )
    conn.close()


def upsert_derivatives_oi(rows: Iterable[Tuple]) -> None:
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            execute_batch(
                cur,
                """
                INSERT INTO derivatives_oi
                (timestamp, exchange, oi_usd, oi_coin, funding_rt, ls_ratio)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (timestamp, exchange) DO UPDATE SET
                    oi_usd     = EXCLUDED.oi_usd,
                    oi_coin    = EXCLUDED.oi_coin,
                    funding_rt = EXCLUDED.funding_rt,
                    ls_ratio   = EXCLUDED.ls_ratio;
                """,
                list(rows),
            )
    conn.close()


def insert_onchain_flow(timestamp: str, flow_in_xrp: float, flow_out_xrp: float) -> None:
    net = flow_in_xrp - flow_out_xrp
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO onchain_flows
                (timestamp, flow_in_xrp, flow_out_xrp, net_flow_xrp)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (timestamp) DO UPDATE SET
                    flow_in_xrp  = EXCLUDED.flow_in_xrp,
                    flow_out_xrp = EXCLUDED.flow_out_xrp,
                    net_flow_xrp = EXCLUDED.net_flow_xrp;
                """,
                (timestamp, flow_in_xrp, flow_out_xrp, net),
            )
    conn.close()


def insert_sentiment(timestamp: str, headline: str, source: str,
                     score_raw: float, score_ema: float, weight: float) -> None:
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO sentiment_feed
                (timestamp, headline, source, score_raw, score_ema, weight)
                VALUES (%s,%s,%s,%s,%s,%s);
                """,
                (timestamp, headline, source, score_raw, score_ema, weight),
            )
    conn.close()


def insert_signal_snapshot(row: Tuple) -> None:
    conn = get_connection()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals_snapshot
                (timestamp, price, oi_total, funding_rt, ls_ratio, rvol,
                 oi_change, divergence, composite_score)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (timestamp) DO UPDATE SET
                    price           = EXCLUDED.price,
                    oi_total        = EXCLUDED.oi_total,
                    funding_rt      = EXCLUDED.funding_rt,
                    ls_ratio        = EXCLUDED.ls_ratio,
                    rvol            = EXCLUDED.rvol,
                    oi_change       = EXCLUDED.oi_change,
                    divergence      = EXCLUDED.divergence,
                    composite_score = EXCLUDED.composite_score;
                """,
                row,
            )
    conn.close()


# ---------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------

def fetch_latest_snapshot() -> Optional[Tuple]:
    conn = get_connection()
    row = None
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, price, oi_total, funding_rt, ls_ratio,
                       rvol, oi_change, divergence, composite_score
                FROM signals_snapshot
                ORDER BY timestamp DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
    conn.close()
    return row


def fetch_latest_flow() -> Optional[Tuple]:
    conn = get_connection()
    row = None
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT timestamp, flow_in_xrp, flow_out_xrp, net_flow_xrp
                FROM onchain_flows
                ORDER BY timestamp DESC
                LIMIT 1;
                """
            )
            row = cur.fetchone()
    conn.close()
    return row
