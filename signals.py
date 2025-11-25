"""Shared signal definitions and audit utilities for the XRP dashboard."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict

from redis_client import rdb


@dataclass(frozen=True)
class SignalComponent:
    """Metadata for a composite-score component."""

    name: str
    max_points: float
    description: str
    hint: str = ""
    cap_note: str = ""


SIGNAL_COMPONENTS: Dict[str, SignalComponent] = {
    "funding": SignalComponent(
        name="Funding Z-Score",
        max_points=22.0,
        description="Rewards positive funding when it materially diverges from the rolling mean without letting outliers dominate.",
        hint="Extremes are tanh-capped so sharp dislocations do not swamp the composite.",
        cap_note="Scaled with tanh; clipped at +22 points.",
    ),
    "whale_flow": SignalComponent(
        name="Weighted XRPL Inflow",
        max_points=14.0,
        description="Linear weight against 60M XRP of tagged inflows to surface sustained exchange demand.",
        hint="Weights favor higher-confidence exchange tags to reduce OTC bleed-through.",
        cap_note="Capped at 60M XRP-equivalent (14 pts).",
    ),
    "price_window": SignalComponent(
        name="Price Window $2.45–$3.00",
        max_points=28.0,
        description="Dynamic price window anchored to breakout zones; full credit near $2.45 with soft decay toward $3.00 and beyond.",
        hint="Keeps score sensitive to structure even as highs shift upward.",
        cap_note="Linear decay to $3.00; bounded at 28 pts.",
    ),
    "oi": SignalComponent(
        name="Open Interest Depth",
        max_points=16.0,
        description="Rewards OI ≥ $2.7B with linear decay to $1.5B to capture liquidity conditions.",
        hint="Soft floor ensures low-liquidity environments do not accidentally score high.",
        cap_note="Full 16 pts above $2.7B; fades to zero by $1.5B.",
    ),
    "netflow": SignalComponent(
        name="Binance Netflow",
        max_points=30.0,
        description="Bullish when 24h net withdrawals approach 100M XRP-equivalent; linear until the cap.",
        hint="Caps protect against single-wallet shocks overwhelming other components.",
        cap_note="Scaled vs. 100M XRP; 30-pt ceiling.",
    ),
    "squeeze": SignalComponent(
        name="Short Squeeze Setup",
        max_points=20.0,
        description="Long/short ratio inversion with decay from ≤1.0 to 2.0 captures skew-driven squeeze risk.",
        hint="Emphasizes asymmetry when shorts dominate but tempers noise when ratios normalize.",
        cap_note="Full 20 pts at L/S 1.0 or below; zero by 2.0.",
    ),
    "sentiment": SignalComponent(
        name="News Sentiment EMA",
        max_points=15.0,
        description="FinBERT EMA (α=0.3) with linear decay from +0.30 to +0.05 to smooth headline whipsaws.",
        hint="Only smoothed sentiment input; instantaneous tiles remain unsmoothed.",
        cap_note="15 pts at EMA ≥ 0.30; fades to zero by 0.05.",
    ),
    "flippening": SignalComponent(
        name="Flippening Flow",
        max_points=15.0,
        description="Positive XRP/BTC and XRP/ETH ratio uplift gated by >10M XRP weighted inflow.",
        hint="Requires both ratio strength and fresh exchange demand.",
        cap_note="Averages positive ratio uplift; 15-pt maximum.",
    ),
}


def log_score_components(points: Dict[str, float]) -> None:
    """Persist the latest component contributions for audit overlays."""

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "points": {k: float(v) for k, v in points.items()},
    }
    try:
        rdb.set("score:components", json.dumps(payload))
    except Exception:
        # Best-effort only; dashboards should never break on audit logging.
        return


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def calibrated_conviction_probability(total_score: float, beta: float = 0.12) -> float:
    """Map the composite score to a calibrated probability label.

    The mapping is intentionally soft (beta controls steepness) so mid-range
    composites land near coin-flip conviction while high scores approach 80–90%.
    """

    centered = (total_score - 50.0) * beta
    return float(sigmoid(centered))


def posterior_conviction_probability(
    *,
    total_score: float,
    fund_z: float,
    netflow_score: float,
    oi_score: float,
    sentiment_score: float,
    ratio_uplift: float,
    intercept: float = -0.6,
) -> float:
    """Blend composite score and feature-specific signals into a posterior probability.

    The weights are calibrated to historical feature vectors so that:
    - The composite score provides the primary slope.
    - Funding and netflow skew bias the probability upward when both lean bullish.
    - Sentiment and ratio uplift add modest lift while being capped to avoid
      over-reacting to one-off headlines or illiquid pairs.
    """

    score_term = (total_score - 50.0) * 0.05
    funding_term = math.tanh(fund_z / 2.5) * 0.9
    netflow_term = (netflow_score / SIGNAL_COMPONENTS["netflow"].max_points) * 1.1
    oi_term = (oi_score / SIGNAL_COMPONENTS["oi"].max_points) * 0.6
    sentiment_term = (sentiment_score / SIGNAL_COMPONENTS["sentiment"].max_points) * 0.5
    ratio_term = max(0.0, ratio_uplift) * 0.05

    logit = intercept + score_term + funding_term + netflow_term + oi_term + sentiment_term + ratio_term
    probability = sigmoid(logit)
    return float(min(0.98, max(0.02, probability)))
