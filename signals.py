"""Signal scoring and reasoning for the XRP dashboard.

This module defines a simple framework for composing multiple
indicators into a composite score and extracting human readable
reason codes.  The implementation here is intentionally modest
compared to a full institutional quant stack but provides enough
structure for the dashboard to function until a richer model is
implemented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class SignalComponent:
    """Describe a component of the composite score.

    Attributes:
        name: Human readable name of the signal.
        max_points: Maximum contribution to the composite score.
        cap_note: Optional description explaining how the score is capped.
    """

    name: str
    max_points: float
    cap_note: str = ""


# Define a basic set of components.  These values should be tuned
# according to empirical performance; for now they provide a
# reasonable distribution of influence across different metrics.
SIGNAL_COMPONENTS: Dict[str, SignalComponent] = {
    "price_window": SignalComponent(name="Price action", max_points=5.0, cap_note="Reward when price is in breakout range"),
    "funding": SignalComponent(name="Funding rate", max_points=3.0, cap_note="Positive funding indicates bullish sentiment"),
    "oi": SignalComponent(name="Open interest", max_points=3.0, cap_note="High depth suggests strong conviction"),
    "oi_aggregated": SignalComponent(name="Aggregated OI", max_points=2.0, cap_note="Multi‑venue open interest"),
    "relative_volume": SignalComponent(name="Relative volume", max_points=2.0, cap_note="Volume relative to average"),
    "oi_change": SignalComponent(name="OI change", max_points=2.0, cap_note="Reward volatility in open interest"),
    "divergence": SignalComponent(name="Divergence", max_points=1.0, cap_note="Detects OI/price disagreement"),
}


def calibrated_conviction_probability(score: float) -> float:
    """Map an arbitrary score to a [0, 1] conviction probability.

    Uses a logistic transformation to squash unbounded scores into a
    probability.  A score of 0.0 corresponds to 0.5 and larger
    magnitudes push the probability towards 1 or 0 respectively.
    """

    try:
        return 1.0 / (1.0 + math.exp(-score))
    except Exception:
        return 0.5


def reason_score_adjustment(reasons: Dict[str, str], base_score: float) -> float:
    """Adjust the composite score based on the direction of reasons.

    A very simple adjustment: each bullish reason adds 0.5 and each
    bearish reason subtracts 0.5 from the base score.  Unknown
    directions have no effect.
    """

    adjustment = 0.0
    for direction in reasons.values():
        if direction.lower() in ("up", "bullish", "+", "positive"):
            adjustment += 0.5
        elif direction.lower() in ("down", "bearish", "-", "negative"):
            adjustment -= 0.5
    return base_score + adjustment


def derive_reason_codes(inputs: Dict[str, str]) -> List[Tuple[str, str]]:
    """Produce human readable reason codes from internal signals.

    Takes a dictionary of reason inputs such as ``{"price_direction": "up"}``
    and returns a list of key/value pairs describing the directional
    contribution.  Keys are simplified to more succinct phrases for
    display.
    """

    mapping = {
        "price_direction": "Price",  # Direction of price change
        "oi_direction": "Open interest",  # Direction of OI change
        "funding_z_score": "Funding z‑score",  # Funding implied skew
    }
    codes: List[Tuple[str, str]] = []
    for k, v in inputs.items():
        label = mapping.get(k, k.replace("_", " ").title())
        codes.append((label, v))
    return codes


def log_score_components(components: List[Dict[str, float]]) -> None:
    """Placeholder for logging.  In production this would write to a log or audit table."""

    # In a real implementation you might persist these components
    # to a database or monitoring system for audit and backtesting.
    # Here we simply pass as this function serves to document the
    # call site.
    return