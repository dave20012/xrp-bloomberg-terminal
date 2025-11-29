#!/usr/bin/env python3
"""
Institutional-grade backtesting engine for the XRP Quant Console.

This replaces the earlier toy backtester with a more realistic engine
that:

  • Pulls historical price + signal snapshots from PostgreSQL
  • Simulates trades bar-by-bar using the SAME composite scores the
    live console uses
  • Supports transaction costs, stop-loss / take-profit, and max
    holding period
  • Can either:
      – run a single configuration, or
      – sweep over a grid of (entry, exit) thresholds to find the
        best Sharpe / PnL
  • Optionally stores the equity curve into the `backtest_results`
    table (for use in the dashboard)

Usage examples
--------------

Single run, simple thresholds:

    python backtest.py --start 2024-01-01 --end 2024-11-26 \\
        --entry 70 --exit 40 --fee-bps 5

Parameter sweep over thresholds (find best Sharpe):

    python backtest.py --start 2024-01-01 --end 2024-11-26 \\
        --optimize --entry-grid 60 65 70 75 --exit-grid 25 30 35

Store best run's equity curve into PostgreSQL:

    python backtest.py --start 2024-01-01 --end 2024-11-26 \\
        --entry 70 --exit 40 --store-db

Environment
-----------

By default we expect a single DATABASE_URL, e.g.:

    postgresql://user:pass@host:port/dbname

If DATABASE_URL is not set, we fall back to the PG_* variables:

    PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple, Dict

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_db_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url

    host = os.getenv("PGHOST")
    user = os.getenv("PGUSER")
    pw = os.getenv("PGPASSWORD")
    db = os.getenv("PGDATABASE") or os.getenv("PG_DB")
    port = os.getenv("PGPORT") or "5432"
    if not (host and user and pw and db):
        raise RuntimeError(
            "DATABASE_URL not set and PGHOST/PGUSER/PGPASSWORD/PGDATABASE incomplete. "
            "Set DATABASE_URL to your Railway Postgres URL."
        )
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


def load_snapshots(
    start: Optional[str],
    end: Optional[str],
) -> pd.DataFrame:
    """
    Load time-series data for backtesting from signals_snapshot.

    Returns a DataFrame indexed by timestamp with at least:
        price           (float)
        composite_score (float)
        funding_rt      (float, nullable)
        ls_ratio        (float, nullable)
        oi_total        (float, nullable)
        rvol            (float, nullable)
        oi_change       (float, nullable)
        divergence      (bool, nullable)
    """
    db_url = _get_db_url()
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT
                    timestamp,
                    price,
                    composite_score,
                    funding_rt,
                    ls_ratio,
                    oi_total,
                    rvol,
                    oi_change,
                    divergence
                FROM signals_snapshot
            """
            params: List = []
            clauses: List[str] = []
            if start:
                clauses.append("timestamp >= %s")
                params.append(start)
            if end:
                clauses.append("timestamp <= %s")
                params.append(end)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY timestamp ASC"
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp",
            "price",
            "composite_score",
            "funding_rt",
            "ls_ratio",
            "oi_total",
            "rvol",
            "oi_change",
            "divergence",
        ],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df.set_index("timestamp", inplace=True)
    # Ensure numeric dtypes
    for col in ["price", "composite_score", "funding_rt", "ls_ratio", "oi_total", "rvol", "oi_change"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "divergence" in df.columns:
        df["divergence"] = df["divergence"].astype("boolean")

    # Drop rows with missing price or composite
    df = df[df["price"].notna() & df["composite_score"].notna()]
    return df


# ---------------------------------------------------------------------------
# Strategy configuration
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """
    Configuration for a single backtest run.

    Attributes
    ----------
    entry_threshold : float
        Enter long when composite_score >= this value and filters pass.
    exit_threshold : float
        Exit long when composite_score <= this value OR filters fail.
    fee_bps : float
        Round-trip fee in basis points (e.g. 10 = 0.10%).
    stop_loss_pct : float
        Hard stop loss in fractional terms (e.g. 0.1 = 10%).
        Applied on trade level (from entry price).
    take_profit_pct : float
        Hard take-profit in fractional terms (e.g. 0.25 = 25%).
    max_bars_holding : Optional[int]
        Maximum number of bars to hold a trade. None = unlimited.
    min_rvol : float
        Require rVOL >= this for entry (liquidity filter).
    max_funding_abs : float
        Require |funding_rt| <= this for entry; avoids crowded trades.
    min_ls_ratio : Optional[float]
        If set, require long/short ratio >= this (bullish skew).
    max_ls_ratio : Optional[float]
        If set, require long/short ratio <= this (bearish skew filter).
    use_divergence_exit : bool
        If True, exit early when divergence flag is bearish.
    """

    entry_threshold: float = 70.0
    exit_threshold: float = 40.0
    fee_bps: float = 8.0
    stop_loss_pct: float = 0.12
    take_profit_pct: float = 0.30
    max_bars_holding: Optional[int] = None
    min_rvol: float = 0.75
    max_funding_abs: float = 0.0005
    min_ls_ratio: Optional[float] = None
    max_ls_ratio: Optional[float] = None
    use_divergence_exit: bool = True


@dataclass
class Trade:
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    bars_held: int
    gross_return: float
    net_return: float
    reason_exit: str


@dataclass
class BacktestResult:
    config: StrategyConfig
    trades: List[Trade]
    equity_curve: pd.Series
    buy_hold_curve: pd.Series
    stats: Dict[str, float]


# ---------------------------------------------------------------------------
# Core simulation logic
# ---------------------------------------------------------------------------


def _annualization_factor(index: pd.DatetimeIndex) -> float:
    """
    Estimate annualization factor based on median bar spacing.
    """
    if len(index) < 2:
        return 1.0
    deltas = (index[1:] - index[:-1]).astype("timedelta64[s]").astype(float)
    median_sec = float(np.median(deltas))
    if median_sec <= 0:
        return 1.0
    bars_per_year = 365.0 * 24.0 * 3600.0 / median_sec
    return bars_per_year


def simulate_strategy(df: pd.DataFrame, cfg: StrategyConfig) -> BacktestResult:
    """
    Run a bar-by-bar long-only simulation on the given DataFrame.

    df must have columns: price, composite_score, funding_rt, ls_ratio,
    rvol, oi_change, divergence (nullable).
    """

    prices = df["price"].astype(float)
    scores = df["composite_score"].astype(float)
    funding = df["funding_rt"].astype(float)
    rvol = df["rvol"].astype(float)
    ls = df["ls_ratio"].astype(float)
    div = df["divergence"] if "divergence" in df.columns else pd.Series(False, index=df.index)

    ret = prices.pct_change().fillna(0.0)

    fee = cfg.fee_bps / 10_000.0

    position = 0  # 0 = flat, 1 = long
    entry_price = 0.0
    entry_time: Optional[datetime] = None
    bars_in_trade = 0
    trades: List[Trade] = []

    equity = [1.0]
    equity_times = [df.index[0]]
    strat_ret_per_bar: List[float] = [0.0]

    for t, price, score, f, rv, ls_val, dv in zip(
        df.index, prices, scores, funding, rvol, ls, div
    ):
        if math.isnan(price) or math.isnan(score):
            # Cannot trade without price & score; stay flat
            equity.append(equity[-1])
            equity_times.append(t)
            strat_ret_per_bar.append(0.0)
            continue

        # Compute filters
        rv_ok = (not math.isnan(rv)) and rv >= cfg.min_rvol
        f_ok = (not math.isnan(f)) and (abs(f) <= cfg.max_funding_abs)
        ls_ok = True
        if cfg.min_ls_ratio is not None and not math.isnan(ls_val):
            ls_ok = ls_ok and (ls_val >= cfg.min_ls_ratio)
        if cfg.max_ls_ratio is not None and not math.isnan(ls_val):
            ls_ok = ls_ok and (ls_val <= cfg.max_ls_ratio)

        filters_ok = rv_ok and f_ok and ls_ok

        exit_reason = None

        if position == 0:
            # Consider entry
            if score >= cfg.entry_threshold and filters_ok:
                position = 1
                entry_price = price
                entry_time = t.to_pydatetime()
                bars_in_trade = 0
                # Pay half fee on entry
                strat_ret = -fee / 2.0
            else:
                strat_ret = 0.0
        else:
            # In a trade; update holding period and check risk exits
            bars_in_trade += 1
            px_change = (price / entry_price) - 1.0

            # Stop loss
            if px_change <= -cfg.stop_loss_pct:
                position = 0
                exit_reason = "stop_loss"
            # Take profit
            elif px_change >= cfg.take_profit_pct:
                position = 0
                exit_reason = "take_profit"
            # Max holding
            elif cfg.max_bars_holding is not None and bars_in_trade >= cfg.max_bars_holding:
                position = 0
                exit_reason = "time_exit"
            # Divergence-based early exit
            elif cfg.use_divergence_exit and bool(dv):
                position = 0
                exit_reason = "divergence"
            # Score / filter exit
            elif (score <= cfg.exit_threshold) or (not filters_ok):
                position = 0
                exit_reason = "signal_exit"

            # Realized or unrealized return this bar
            strat_ret = ret.loc[t] * position  # position after decisions

            if exit_reason and entry_time is not None:
                # Close at current bar's price; apply remaining half fee
                gross = price / entry_price - 1.0
                net = gross - fee
                trade = Trade(
                    entry_time=entry_time,
                    exit_time=t.to_pydatetime(),
                    entry_price=float(entry_price),
                    exit_price=float(price),
                    bars_held=bars_in_trade,
                    gross_return=float(gross),
                    net_return=float(net),
                    reason_exit=exit_reason,
                )
                trades.append(trade)
                # Flat after this bar
                position = 0
                entry_time = None
                entry_price = 0.0
                bars_in_trade = 0
                strat_ret = net  # realize P&L at exit
        # Update equity
        new_equity = equity[-1] * (1.0 + strat_ret)
        equity.append(new_equity)
        equity_times.append(t)
        strat_ret_per_bar.append(strat_ret)

    equity_series = pd.Series(equity[1:], index=pd.DatetimeIndex(equity_times[1:]))

    # Buy & hold curve from first price
    buy_hold = (1.0 + ret).cumprod()

    stats = compute_stats(
        equity_series,
        buy_hold,
        trades,
        pd.Series(strat_ret_per_bar[1:], index=df.index),
    )

    return BacktestResult(
        config=cfg,
        trades=trades,
        equity_curve=equity_series,
        buy_hold_curve=buy_hold,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def compute_stats(
    equity: pd.Series,
    buy_hold: pd.Series,
    trades: List[Trade],
    strat_ret_per_bar: pd.Series,
) -> Dict[str, float]:
    if equity.empty:
        return {
            "final_equity": 1.0,
            "buy_hold": 1.0,
            "sharpe": 0.0,
            "max_drawdown": 0.0,
            "num_trades": 0,
            "win_rate": 0.0,
            "avg_trade_return": 0.0,
        }

    final_equity = float(equity.iloc[-1])
    final_bh = float(buy_hold.iloc[-1])

    # Max drawdown
    running_max = equity.cummax()
    drawdowns = equity / running_max - 1.0
    max_dd = float(drawdowns.min())

    # Per-bar Sharpe
    af = _annualization_factor(equity.index)
    r = strat_ret_per_bar.values
    mean_r = float(np.mean(r))
    std_r = float(np.std(r, ddof=1)) if len(r) > 1 else 0.0
    sharpe = float(math.sqrt(af) * mean_r / std_r) if std_r > 0 else 0.0

    # Trade-level stats
    num_trades = len(trades)
    if num_trades > 0:
        wins = [t.net_return for t in trades if t.net_return > 0]
        win_rate = len(wins) / num_trades if num_trades > 0 else 0.0
        avg_trade_ret = float(np.mean([t.net_return for t in trades]))
    else:
        win_rate = 0.0
        avg_trade_ret = 0.0

    return {
        "final_equity": final_equity,
        "buy_hold": final_bh,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "num_trades": float(num_trades),
        "win_rate": win_rate,
        "avg_trade_return": avg_trade_ret,
    }


# ---------------------------------------------------------------------------
# Persistence helpers (optional)
# ---------------------------------------------------------------------------


def store_equity_curve_to_db(equity: pd.Series, buy_hold: pd.Series) -> None:
    """
    Persist equity curve into backtest_results table as:

        timestamp, strategy_return, buy_hold_return

    This matches the schema created by setup_db.py and allows the
    dashboard to pull the latest backtest run if desired.
    """
    db_url = _get_db_url()
    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                rows = []
                for ts in equity.index:
                    ts_iso = ts.to_pydatetime().isoformat()
                    strat_val = float(equity.loc[ts])
                    bench_val = float(buy_hold.loc[ts])
                    rows.append((ts_iso, strat_val, bench_val))
                execute_batch(
                    cur,
                    """
                    INSERT INTO backtest_results (timestamp, strategy_return, buy_hold_return)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (timestamp) DO UPDATE
                    SET strategy_return = EXCLUDED.strategy_return,
                        buy_hold_return = EXCLUDED.buy_hold_return
                    """,
                    rows,
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Parameter sweep / optimisation
# ---------------------------------------------------------------------------


def run_single_backtest(
    df: pd.DataFrame,
    cfg: StrategyConfig,
) -> BacktestResult:
    return simulate_strategy(df, cfg)


def run_grid_search(
    df: pd.DataFrame,
    entry_grid: Iterable[float],
    exit_grid: Iterable[float],
    base_cfg: StrategyConfig,
) -> Tuple[BacktestResult, List[BacktestResult]]:
    """
    Sweep over entry/exit combinations.
    Returns (best_result, all_results_sorted_by_sharpe_desc)
    """
    results: List[BacktestResult] = []
    for entry in entry_grid:
        for exit_ in exit_grid:
            if exit_ >= entry:
                # Must exit below entry threshold; skip invalid combos
                continue
            cfg = dataclasses.replace(base_cfg, entry_threshold=entry, exit_threshold=exit_)
            res = simulate_strategy(df, cfg)
            results.append(res)

    # Sort by Sharpe then final_equity
    results.sort(
        key=lambda r: (r.stats.get("sharpe", 0.0), r.stats.get("final_equity", 1.0)),
        reverse=True,
    )
    best = results[0] if results else run_single_backtest(df, base_cfg)
    return best, results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Institutional-grade backtester for XRP Quant Console")

    p.add_argument("--start", help="Start date (YYYY-MM-DD)", default=None)
    p.add_argument("--end", help="End date (YYYY-MM-DD)", default=None)

    # Single-run config
    p.add_argument("--entry", type=float, default=70.0, help="Composite score entry threshold")
    p.add_argument("--exit", type=float, default=40.0, help="Composite score exit threshold")

    # Risk / cost parameters
    p.add_argument("--fee-bps", type=float, default=8.0, help="Round-trip fee in basis points")
    p.add_argument("--stop-loss", type=float, default=0.12, help="Stop loss in fractional terms (0.1 = 10%)")
    p.add_argument("--take-profit", type=float, default=0.30, help="Take profit in fractional terms (0.2 = 20%)")
    p.add_argument(
        "--max-bars",
        type=int,
        default=None,
        help="Maximum bars to hold a trade (None = unlimited)",
    )
    p.add_argument("--min-rvol", type=float, default=0.75, help="Minimum rVOL to allow entries")
    p.add_argument(
        "--max-funding",
        type=float,
        default=0.0005,
        help="Maximum absolute funding rate allowed for entries",
    )
    p.add_argument("--min-ls", type=float, default=None, help="Minimum long/short ratio (optional)")
    p.add_argument("--max-ls", type=float, default=None, help="Maximum long/short ratio (optional)")

    p.add_argument(
        "--no-divergence-exit",
        action="store_true",
        help="Disable divergence-based early exits",
    )

    # Optimisation flags
    p.add_argument(
        "--optimize",
        action="store_true",
        help="Run a grid search over entry/exit thresholds",
    )
    p.add_argument(
        "--entry-grid",
        nargs="+",
        type=float,
        default=[60.0, 65.0, 70.0, 75.0, 80.0],
        help="Entry thresholds to sweep when --optimize is set",
    )
    p.add_argument(
        "--exit-grid",
        nargs="+",
        type=float,
        default=[25.0, 30.0, 35.0, 40.0],
        help="Exit thresholds to sweep when --optimize is set",
    )

    # Persistence / output
    p.add_argument(
        "--store-db",
        action="store_true",
        help="Store the best run's equity curve into backtest_results table",
    )
    p.add_argument(
        "--export-trades",
        type=str,
        default=None,
        help="Optional path to CSV file where trades will be exported",
    )

    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    args = parse_args()
    df = load_snapshots(args.start, args.end)
    if df.empty:
        logging.warning("No snapshot data found for the specified range.")
        return

    base_cfg = StrategyConfig(
        entry_threshold=args.entry,
        exit_threshold=args.exit,
        fee_bps=args.fee_bps,
        stop_loss_pct=args.stop_loss,
        take_profit_pct=args.take_profit,
        max_bars_holding=args.max_bars,
        min_rvol=args.min_rvol,
        max_funding_abs=args.max_funding,
        min_ls_ratio=args.min_ls,
        max_ls_ratio=args.max_ls,
        use_divergence_exit=not args.no_divergence_exit,
    )

    if args.optimize:
        logging.info("🏁 Running grid search over entry/exit thresholds ...")
        best, all_results = run_grid_search(df, args.entry_grid, args.exit_grid, base_cfg)
    else:
        best = run_single_backtest(df, base_cfg)
        all_results = [best]

    # Summary of best run
    s = best.stats
    logging.info("=== BEST CONFIGURATION ===")
    logging.info(f"Entry / Exit: {best.config.entry_threshold:.1f} / {best.config.exit_threshold:.1f}")
    logging.info(f"Final equity:  {s['final_equity']:.3f}x (buy-and-hold {s['buy_hold']:.3f}x)")
    logging.info(f"Sharpe ratio:  {s['sharpe']:.2f}")
    logging.info(f"Max drawdown:  {s['max_drawdown']:.2%}")
    logging.info(f"Trades:        {int(s['num_trades'])}, Win rate: {s['win_rate']:.1%}")
    logging.info(f"Avg trade R:   {s['avg_trade_return']:.3%}")

    # Optional: export trades
    if args.export_trades and best.trades:
        rows = []
        for t in best.trades:
            rows.append(
                {
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "bars_held": t.bars_held,
                    "gross_return": t.gross_return,
                    "net_return": t.net_return,
                    "reason_exit": t.reason_exit,
                }
            )
        trades_df = pd.DataFrame(rows)
        trades_df.to_csv(args.export_trades, index=False)
        logging.info(f"💾 Exported {len(rows)} trades to {args.export_trades}")

    # Optional: store equity curve to DB
    if args.store_db:
        logging.info("💾 Storing equity curve to backtest_results table ...")
        store_equity_curve_to_db(best.equity_curve, best.buy_hold_curve)
        logging.info("✅ Backtest results stored in DB.")


if __name__ == "__main__":
    main()
