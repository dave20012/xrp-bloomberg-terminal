"""Periodic data ingestion worker for the XRP quant dashboard.

This module polls multiple external APIs every 5 minutes to collect
market, derivatives, on‑chain and sentiment telemetry.  The resulting
metrics are normalised and written into a TimescaleDB instance via
the functions defined in ``db.py``.  Once stored, these snapshots
form the basis for the Streamlit dashboard and any downstream
backtesting or quantitative research.

To run the worker manually:

    python worker.py --once

The default behaviour without ``--once`` is to run continuously in
an infinite loop with a 5 minute sleep between iterations.  The
worker initialises the database schema on first start by calling
``initialize_db()``.

Environment variables required:

    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB
        Connection settings for the Timescale/PostgreSQL instance.

    BINANCE_API_KEY, BINANCE_API_SECRET (optional)
        API credentials for Binance; if set, certain endpoints may
        allow higher rate limits.  Not required for the public
        endpoints used here.

    COINGECKO_API_KEY (optional)
        If using a paid CoinGecko plan this key will be passed via
        the ``x-cg-pro-api-key`` header when fetching price data.

The worker imports from ``main.py`` to reuse helper functions such as
``fetch_funding_and_oi``, ``fetch_xrpl_flows``, ``fetch_sentiment`` and
``compute_signal_stack``.  Those functions encapsulate all external
dependency logic, keeping this file focused on scheduling and
persistence.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import psycopg2

from db import (
    initialize_db,
    insert_onchain_flow,
    insert_signal_snapshot,
    fetch_latest_snapshot,
)

import main as app_main


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def poll_once() -> None:
    """Perform a single polling cycle and write a snapshot to the database."""
    # Ensure tables exist before writing
    initialize_db()

    # Retrieve the latest snapshot to compute deltas
    prev = fetch_latest_snapshot()
    prev_price: Optional[float] = None
    prev_oi: Optional[float] = None
    if prev:
        # signals_snapshot schema: timestamp, price, oi_total, funding_rt,
        # ls_ratio, rvol, oi_change, divergence, composite_score
        (_, prev_price, prev_oi, _, _, _, _, _, _) = prev

    # ---------------------------------------------------------------------
    # Fetch fresh data from helper functions defined in main.py
    # ---------------------------------------------------------------------
    # Price snapshot
    price_snap = app_main.fetch_price_snapshot()
    price_usd: Optional[float] = price_snap.get("price") if isinstance(price_snap, dict) else None
    price_dict: Dict[str, Optional[float]] = {"price": price_usd}

    # Funding, open interest, long/short ratio, relative volume, aggregated OI
    futures = app_main.fetch_funding_and_oi()
    funding = futures.get("funding")
    ls_ratio = futures.get("long_short_ratio")
    agg_oi = futures.get("aggregated_open_interest")
    rvol = futures.get("relative_volume")

    # Compute open interest change relative to previous snapshot
    oi_change: Optional[float] = None
    if agg_oi is not None and prev_oi is not None:
        try:
            oi_change = float(agg_oi) - float(prev_oi)
        except Exception:
            oi_change = None

    # Divergence flag: price and OI move in opposite directions
    divergence: Optional[bool] = None
    if price_usd is not None and prev_price is not None and oi_change is not None:
        try:
            price_change = float(price_usd) - float(prev_price)
            divergence = (price_change > 0 and oi_change < 0) or (price_change < 0 and oi_change > 0)
        except Exception:
            divergence = None
    else:
        divergence = False

    # XRPL inflows/outflows
    flows = app_main.fetch_xrpl_flows()
    latest_inflow = flows.get("latest_inflow", 0.0) if isinstance(flows, dict) else 0.0
    latest_outflow = flows.get("latest_outflow", 0.0) if isinstance(flows, dict) else 0.0

    # Sentiment
    sentiment = app_main.fetch_sentiment()

    # Build futures dictionary for signal scoring.  Note: we include
    # raw open interest ("open_interest") for completeness even though
    # aggregated_open_interest may subsume it.  compute_signal_stack
    # gracefully handles None values.
    futures_dict = {
        "funding": funding,
        "open_interest": futures.get("open_interest"),
        "aggregated_open_interest": agg_oi,
        "long_short_ratio": ls_ratio,
        "relative_volume": rvol,
    }

    # Compute the signal stack and obtain a composite score.  The
    # returned dict contains the detailed component contributions and
    # normalised composite percentage on [0,100], plus a probability
    # calibration.  We only persist the composite percentage and leave
    # more granular details for the dashboard.
    try:
        stack = app_main.compute_signal_stack(price_dict, futures_dict, flows, sentiment)
        composite_pct: float = float(stack.get("composite", 0.0))
    except Exception as exc:
        logging.error(f"compute_signal_stack failed: {exc}")
        composite_pct = 0.0

    # Persist on‑chain flows into the database.  Net flow is computed
    # within insert_onchain_flow().  Use ISO format for timescale
    # compatibility.
    ts = datetime.now(timezone.utc)
    iso_ts = ts.isoformat()
    try:
        insert_onchain_flow(iso_ts, float(latest_inflow), float(latest_outflow))
    except Exception as exc:
        logging.warning(f"Failed to insert on‑chain flows: {exc}")

    # Persist the consolidated snapshot.  Missing values are stored as
    # NULL.  composite_pct is recorded out of 100; downstream
    # consumers may divide by 100 when interpreting as probability.
    snapshot_row = (
        iso_ts,
        price_usd,
        agg_oi,
        funding,
        ls_ratio,
        rvol,
        oi_change,
        divergence,
        composite_pct,
    )
    try:
        insert_signal_snapshot(snapshot_row)
    except Exception as exc:
        logging.warning(f"Failed to insert signal snapshot: {exc}")

    logging.info(
        f"Snapshot captured at {iso_ts}: price={price_usd}, agg_oi={agg_oi}, funding={funding}, "
        f"ls_ratio={ls_ratio}, rvol={rvol}, oi_change={oi_change}, divergence={divergence}, composite={composite_pct}"
    )


def run_loop() -> None:
    """Run the polling loop indefinitely with a 5 minute cadence."""
    while True:
        poll_once()
        # Sleep exactly 5 minutes (300 seconds).  This is a best
        # effort; the actual interval may drift depending on API
        # latency.  Consider using a scheduling framework (e.g.
        # cron or Celery beat) for production deployments.
        time.sleep(300)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="XRP market data ingestion worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single polling cycle and exit instead of looping",
    )
    args = parser.parse_args()
    if args.once:
        poll_once()
    else:
        run_loop()