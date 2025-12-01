"""Signal computation library for XRP intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

import numpy as np

from core.models import ManipulationHint, OIMetrics


@dataclass(slots=True)
class VolumeSignal:
    zscore: float
    regime: str
    baseline: float
    latest: float


@dataclass(slots=True)
class FlowSignal:
    net_flow: float
    zscore: float
    regime: str


def compute_zscore(series: Sequence[float]) -> float:
    arr = np.array(series[-30:])
    if arr.size == 0:
        return 0.0
    mean = arr.mean()
    std = arr.std(ddof=1) if arr.size > 1 else 1.0
    return float((arr[-1] - mean) / (std if std != 0 else 1.0))


def classify_regime(zscore: float, low: float = -0.8, high: float = 0.8) -> str:
    if zscore >= high:
        return "high"
    if zscore <= low:
        return "low"
    return "normal"


def compute_volume_signal(volumes: Sequence[float]) -> VolumeSignal:
    if not volumes:
        return VolumeSignal(0.0, "normal", 0.0, 0.0)
    z = compute_zscore(volumes)
    regime = classify_regime(z)
    baseline = float(np.mean(volumes[-24:])) if len(volumes) >= 24 else float(np.mean(volumes))
    return VolumeSignal(zscore=z, regime=regime, baseline=baseline, latest=float(volumes[-1]))


def compute_flow_signal(net_flows: Sequence[float]) -> FlowSignal:
    if not net_flows:
        return FlowSignal(0.0, 0.0, "normal")
    z = compute_zscore(net_flows)
    regime = classify_regime(z)
    return FlowSignal(net_flow=float(net_flows[-1]), zscore=z, regime=regime)


def compute_oi_leverage_score(metrics: Iterable[OIMetrics]) -> float:
    values = list(metrics)
    if not values:
        return 50.0
    latest = values[-1]
    oi_change = 0.0
    if len(values) > 1:
        prev = values[-2]
        oi_change = ((latest.oi - prev.oi) / prev.oi) * 100 if prev.oi else 0.0
    funding_bias = latest.funding * 100 if latest.funding is not None else 0.0
    ls_skew = (latest.ls_ratio - 1) * 100 if latest.ls_ratio else 0.0
    score = 50 + 0.2 * oi_change + 0.3 * funding_bias + 0.1 * ls_skew
    return float(np.clip(score, 0, 100))


def compute_manipulation_hint(order_book_stats: Dict[str, float], volume_spike: bool) -> ManipulationHint:
    depth_imbalance = order_book_stats.get("depth_imbalance", 0.0)
    rapid_wall_change = abs(depth_imbalance) > 0.5
    risk = 0.5 * abs(depth_imbalance) + (0.5 if volume_spike else 0.0)
    note = "Depth skew + spike" if rapid_wall_change and volume_spike else "Skewed depth" if rapid_wall_change else "Calm"
    return ManipulationHint(
        timestamp=None,  # filled by caller
        depth_imbalance=depth_imbalance,
        rapid_wall_change=rapid_wall_change,
        risk_score=float(np.clip(risk, 0, 1)),
        note=note,
    )


def compute_regulatory_score(events: List[Dict[str, float]]) -> float:
    if not events:
        return 25.0
    threat = np.mean([e.get("regulatory_threat", 0.0) for e in events]) if events else 0.0
    support = np.mean([e.get("regulatory_support", 0.0) for e in events]) if events else 0.0
    base = 50 + (support - threat) * 50
    return float(np.clip(base, 0, 100))


def aggregate_scores(
    flow_score: float,
    oi_score: float,
    volume_score: float,
    manipulation_score: float,
    regulatory_score: float,
    weights: Dict[str, float] | None = None,
) -> float:
    weights = weights or {
        "flow": 0.3,
        "oi": 0.25,
        "volume": 0.2,
        "manipulation": 0.15,
        "regulatory": 0.1,
    }
    total = (
        flow_score * weights["flow"]
        + oi_score * weights["oi"]
        + volume_score * weights["volume"]
        + (100 - manipulation_score) * weights["manipulation"]
        + regulatory_score * weights["regulatory"]
    )
    return float(np.clip(total, 0, 100))
