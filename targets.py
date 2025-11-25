"""ATR-derived target bands and risk metrics shared across the dashboard."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd


def compute_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """Return the latest Average True Range from OHLC data."""

    if df is None or df.empty or not {"high", "low", "close"}.issubset(df.columns):
        return None

    df_sorted = df.sort_values("date")
    high = df_sorted["high"].astype(float)
    low = df_sorted["low"].astype(float)
    close = df_sorted["close"].astype(float)
    prev_close = close.shift(1)

    tr = pd.DataFrame(
        {
            "hl": high - low,
            "hc": (high - prev_close).abs(),
            "lc": (low - prev_close).abs(),
        }
    ).max(axis=1)

    atr = tr.rolling(period).mean().iloc[-1]
    return float(atr) if not pd.isna(atr) else None


def compute_risk_metrics(closes: Optional[pd.Series]) -> Dict[str, Optional[float]]:
    """Compute simple win-rate and drawdown metrics from a close series."""

    if closes is None:
        return {"win_rate": None, "max_drawdown_pct": None}

    clean = pd.to_numeric(closes, errors="coerce").dropna()
    if clean.empty:
        return {"win_rate": None, "max_drawdown_pct": None}

    returns = clean.pct_change().dropna()
    win_rate = float((returns > 0).mean() * 100.0) if not returns.empty else None

    equity = (1.0 + returns).cumprod()
    rolling_peak = equity.cummax()
    drawdown = ((equity / rolling_peak) - 1.0).min() if not equity.empty else None
    drawdown_pct = float(drawdown * 100.0) if drawdown is not None else None

    return {"win_rate": win_rate, "max_drawdown_pct": drawdown_pct}


def build_target_profile(
    price: Optional[float],
    atr: Optional[float],
    *,
    ratio_bias: float = 0.0,
    closes: Optional[pd.Series] = None,
) -> Dict[str, Any]:
    """Return ATR-based entry, target bands, and contextual risk metrics."""

    if price is None or atr is None or price <= 0 or atr <= 0:
        return {
            "band": "N/A",
            "atr_pct": None,
            "entry": None,
            "tp1": None,
            "tp2": None,
            "invalidation": None,
            "text": "ATR unavailable; waiting for sufficient OHLC candles.",
            "risk": {"win_rate": None, "max_drawdown_pct": None},
        }

    atr_pct = atr / price * 100.0
    if atr_pct >= 12:
        band = "High Volatility"
    elif atr_pct >= 7:
        band = "Elevated"
    else:
        band = "Contained"

    bias_factor = 1.0 + max(0.0, ratio_bias) / 50.0
    tp1 = price + atr * 1.5 * bias_factor
    tp2 = price + atr * 2.5 * bias_factor
    invalidation = max(price - atr * 1.2, 0.0)

    risk = compute_risk_metrics(closes)

    text = (
        f"Entry ${price:.4f} | TP1 ${tp1:.4f} | TP2 ${tp2:.4f} | "
        f"Invalidation ${invalidation:.4f}"
    )

    return {
        "band": band,
        "atr_pct": atr_pct,
        "entry": price,
        "tp1": tp1,
        "tp2": tp2,
        "invalidation": invalidation,
        "text": text,
        "risk": risk,
    }
