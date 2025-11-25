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


SIGNAL_COMPONENTS: Dict[str, SignalComponent] = {
    "funding": SignalComponent(
        name="Funding Z-Score",
        max_points=22.0,
        description="Reward elevated funding when it meaningfully diverges from the 90-sample mean without letting extremes dominate.",
        hint="Positive funding leading price is typically fuel for squeezes; capped to reduce noise during chop.",
    ),
    "whale_flow": SignalComponent(
        name="Whale Flow (XRPL, weighted)",
        max_points=14.0,
        description="Weighted XRPL inflows, favoring tagged exchanges with higher confidence weights.",
        hint="Sized to avoid flow spikes overwhelming market structure signals.",
    ),
    "price_window": SignalComponent(
        name="Price window $2.45–$3.00",
        max_points=28.0,
        description="Dynamic price window that rewards proximity to breakout zones without hard caps on upside.",
        hint="Window adapts to 90d highs so scores stay responsive as structure shifts.",
    ),
    "oi": SignalComponent(
        name="OI > $2.7B",
        max_points=16.0,
        description="Open interest normalized to USD with a soft floor around prior breakout levels.",
    ),
    "netflow": SignalComponent(
        name="Binance Netflow Bullish",
        max_points=8.0,
        description="Net withdrawals from Binance over the last 24h to capture accumulation off-exchange.",
    ),
    "squeeze": SignalComponent(
        name="Short Squeeze Setup",
        max_points=15.0,
        description="Long/short ratio pressure inverted into squeeze potential when shorts dominate.",
    ),
    "sentiment": SignalComponent(
        name="Positive News (EMA)",
        max_points=15.0,
        description="FinBERT-weighted sentiment EMA, slower than spot readings to mute headline whipsaws.",
    ),
    "flippening": SignalComponent(
        name="Flippening Flow",
        max_points=15.0,
        description="Uplift in XRP/BTC and XRP/ETH ratios gated by fresh XRPL exchange inflows.",
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
