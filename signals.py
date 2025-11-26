"""Shared signal definitions and audit utilities for the XRP dashboard."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

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
    # Aggregated open interest across major venues (e.g., Binance + Bybit). The goal
    # is to reflect liquidity beyond a single exchange. Full credit is given
    # when aggregated OI surpasses $4B and fades to zero by $2B. This
    # encourages awareness of cross‑exchange flows and prevents liquidity
    # blind spots.
    "oi_aggregated": SignalComponent(
        name="Aggregated Open Interest Depth",
        max_points=16.0,
        description="Rewards aggregated OI ≥ $4B with linear decay to $2B to capture multi‑venue liquidity.",
        hint="Combines open interest from multiple exchanges; avoids overweighting any single venue.",
        cap_note="Full 16 pts above $4B; fades to zero by $2B.",
    ),

    # Change in open interest relative to the previous snapshot. Positive OI
    # changes when price rises signal leveraged conviction; negative changes
    # during price falls can indicate capitulation or deleveraging. Full points
    # accrue when the absolute delta exceeds $200M in either direction, with
    # linear scaling to $0 at 0. A separate divergence component will account
    # for directional disagreement between OI and price.
    "oi_change": SignalComponent(
        name="OI Change",
        max_points=12.0,
        description="Rewards absolute changes in aggregated open interest beyond $0.2B; penalises stagnation.",
        hint="Measures the magnitude of leverage entering or exiting the market.",
        cap_note="Full 12 pts at ±$0.2B change; zero at no change.",
    ),

    # Relative volume (rVOL) gauges whether the current trading volume materially
    # exceeds its recent average. It’s computed as current volume divided by
    # the moving average of the last N periods. Full points are awarded when
    # rVOL ≥ 3.0 and fade to zero by 1.0.
    "relative_volume": SignalComponent(
        name="Relative Volume",
        max_points=10.0,
        description="Rewards high trading activity when current volume is ≥3× its moving average.",
        hint="Helps identify breakouts backed by volume rather than idle markets.",
        cap_note="Full 10 pts at rVOL ≥ 3.0; zero by 1.0.",
    ),

    # Divergence between price and open interest signals when sentiment and
    # positioning are misaligned. Bullish divergence occurs when OI rises while
    # price falls; bearish divergence occurs when OI falls while price rises.
    # This component awards points when such divergences are detected and
    # decays to zero otherwise.
    "divergence": SignalComponent(
        name="OI‑Price Divergence",
        max_points=8.0,
        description="Flags misalignment between price and aggregated OI direction (bearish/bullish divergences).",
        hint="Bullish divergence: OI ↑, price ↓; bearish divergence: OI ↓, price ↑.",
        cap_note="8 pts awarded per detected divergence; zero otherwise.",
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

# ============================================================================
# Reason code utilities
#
# To provide clearer guidance to traders, the signal stack can emit
# human‑readable reason codes that explain why a given score was high or low.
# These codes capture the qualitative interpretation of quantitative metrics.
# They can also be used to adjust the composite score when configured to do so.

def derive_reason_codes(snapshot: Dict[str, Any]) -> List[str]:
    """Derive human-readable reason codes from a snapshot of market telemetry.

    Parameters
    ----------
    snapshot: Dict[str, Any]
        A dictionary containing key metrics such as funding Z-score, price
        direction, OI change and divergence, net flows and sentiment. See
        ``compute_signal_stack`` in ``main.py`` for the keys expected.

    Returns
    -------
    List[str]
        A list of strings explaining the primary drivers of the current
        composite score. An empty list indicates no actionable signal.
    """
    reasons: List[str] = []

    # Funding: classify carry regime
    fund_z = snapshot.get("funding_z_score")
    if fund_z is not None:
        if fund_z > 0.5:
            reasons.append(f"Funding elevated (Z={fund_z:.2f}) → longs paying shorts")
        elif fund_z < -0.5:
            reasons.append(f"Funding suppressed (Z={fund_z:.2f}) → shorts dominant")

    # OI and price divergence
    oi_dir = snapshot.get("oi_direction")
    price_dir = snapshot.get("price_direction")
    if oi_dir == "up" and price_dir == "down":
        reasons.append("Bullish divergence: OI ↑ while price ↓ (leveraged accumulation)")
    elif oi_dir == "down" and price_dir == "up":
        reasons.append("Bearish divergence: OI ↓ while price ↑ (deleveraging)")

    # Netflow: exchange deposits/withdrawals
    net_flow = snapshot.get("netflow_xrp")
    if net_flow is not None:
        if net_flow > 500_000:
            reasons.append(f"{net_flow/1e6:.1f}M XRP withdrawn from exchange → accumulation")
        elif net_flow < -500_000:
            reasons.append(f"{abs(net_flow)/1e6:.1f}M XRP deposited to exchange → sell pressure")

    # Sentiment
    sent = snapshot.get("sentiment_ema")
    if sent is not None:
        if sent >= 0.15:
            reasons.append("Headline tone supportive (EMA positive)")
        elif sent <= 0.05:
            reasons.append("Headline tone muted or leaning bearish")

    # Price window
    price_status = snapshot.get("price_status")
    if price_status:
        if price_status == "In breakout zone":
            reasons.append("Price acting inside breakout window")
        elif price_status == "Extended; patience warranted":
            reasons.append("Price extended above breakout window")

    return reasons


def reason_score_adjustment(reasons: List[str]) -> float:
    """Compute a composite score adjustment based on qualitative reasons.

    Each reason can influence the total score positively or negatively. The
    adjustments are intentionally modest so that quantitative signals remain
    primary. You can tune these weights to emphasise or de‑emphasise
    particular regimes. The output is expressed in raw points (not percent)
    and should be applied before normalising against the total possible
    points.

    Parameters
    ----------
    reasons: List[str]
        The list of reason codes generated by ``derive_reason_codes``.

    Returns
    -------
    float
        A raw point adjustment (positive or negative) to add to the total
        unnormalised score.
    """
    adjustment = 0.0
    for r in reasons:
        text = r.lower()
        # Bullish reasons (increase score)
        if "bullish divergence" in text:
            adjustment += 3.0
        if "accumulation" in text and "withdrawn" in text:
            adjustment += 2.0
        if "supportive" in text or "breakout" in text:
            adjustment += 2.0
        # Bearish reasons (decrease score)
        if "bearish divergence" in text:
            adjustment -= 3.0
        if "sell pressure" in text or "deposited" in text:
            adjustment -= 2.0
        if "muted" in text or "extended" in text:
            adjustment -= 1.0
    return adjustment
