"""Simple backtesting module for the XRP quant dashboard.

This script provides a rudimentary backtesting framework that
demonstrates how to use the time‑series data stored in the
``signals_snapshot`` table to evaluate a trading strategy.  The
strategy buys (goes long) when the composite score exceeds a
``long_entry`` threshold and exits when it falls below a
``long_exit`` threshold.  Results are benchmarked against a
buy‑and‑hold strategy and summarised as compounded returns.

Usage:

    python backtest.py --start 2025-01-01 --end 2025-06-01 --entry 65 --exit 35

Environment variables required:

    PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB

Note: this script is intentionally basic.  Serious quantitative
analysis should consider transaction costs, slippage, position sizing
and more sophisticated entry/exit rules.  Nonetheless it provides a
useful template for experimenting with the stored metrics.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime
from typing import List, Tuple

import pandas as pd

from db import get_connection


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


def load_snapshot_data(start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    """Retrieve snapshots from the database within a date range."""
    conn = get_connection()
    query = "SELECT timestamp, price, composite_score FROM signals_snapshot"
    conditions: List[str] = []
    params: List = []
    if start:
        conditions.append("timestamp >= %s")
        params.append(start)
    if end:
        conditions.append("timestamp <= %s")
        params.append(end)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp ASC;"
    df = pd.DataFrame(columns=["timestamp", "price", "composite_score"])
    with conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            if rows:
                df = pd.DataFrame(rows, columns=["timestamp", "price", "composite_score"])
    conn.close()
    if not df.empty:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df.sort_values("timestamp", inplace=True)
        df.set_index("timestamp", inplace=True)
    return df


def backtest_strategy(df: pd.DataFrame, long_entry: float, long_exit: float) -> Tuple[pd.Series, pd.Series]:
    """Run a threshold‑based long/flat strategy and compute returns.

    Parameters
    ----------
    df: DataFrame
        Must contain a DatetimeIndex and columns "price" and
        "composite_score" (0–100).
    long_entry: float
        Composite score threshold above which to enter a long position.
    long_exit: float
        Composite score threshold below which to exit (flat).

    Returns
    -------
    Tuple[pd.Series, pd.Series]
        A tuple of (strategy cumulative return, buy&hold cumulative return).
    """
    if df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    prices = df["price"].astype(float)
    comps = df["composite_score"].astype(float)
    # Generate signals: 1 for long, 0 for flat
    signals = pd.Series(0, index=prices.index, dtype=int)
    position = 0
    for ts, score in comps.items():
        if position == 0 and score >= long_entry:
            position = 1
        elif position == 1 and score <= long_exit:
            position = 0
        signals.loc[ts] = position
    # Compute returns
    pct_changes = prices.pct_change().fillna(0.0)
    strategy_returns = (signals.shift(1).fillna(0) * pct_changes).add(1).cumprod()
    buy_hold_returns = pct_changes.add(1).cumprod()
    return strategy_returns, buy_hold_returns


def main(start: Optional[str], end: Optional[str], entry: float, exit: float) -> None:
    df = load_snapshot_data(start, end)
    if df.empty:
        logging.info("No data found for the specified range.")
        return
    strat, bench = backtest_strategy(df, entry, exit)
    # Print summary statistics
    final_strat = strat.iloc[-1] if not strat.empty else 1.0
    final_bench = bench.iloc[-1] if not bench.empty else 1.0
    logging.info(f"Backtest results from {start or df.index.min()} to {end or df.index.max()}")
    logging.info(f"Strategy final return: {final_strat:.2f}x")
    logging.info(f"Buy & hold final return: {final_bench:.2f}x")
    # Optionally display time series summary to user
    print("Date,Strategy_Return,BuyHold_Return")
    for ts in df.index:
        strat_val = strat.loc[ts] if ts in strat.index else None
        bench_val = bench.loc[ts] if ts in bench.index else None
        print(f"{ts.isoformat()},{strat_val:.6f},{bench_val:.6f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest XRP composite score strategy")
    parser.add_argument("--start", help="Start date (ISO format)", default=None)
    parser.add_argument("--end", help="End date (ISO format)", default=None)
    parser.add_argument("--entry", type=float, default=65.0, help="Composite score threshold to enter long")
    parser.add_argument("--exit", type=float, default=35.0, help="Composite score threshold to exit long")
    args = parser.parse_args()
    main(args.start, args.end, args.entry, args.exit)