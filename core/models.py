"""Typed domain models for analytics and UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass(slots=True)
class FlowSnapshot:
    timestamp: datetime
    exchange: str
    net_flow_xrp: float
    direction: str
    amount_xrp: float


@dataclass(slots=True)
class OIMetrics:
    timestamp: datetime
    exchange: str
    oi: float
    funding: float
    ls_ratio: float
    volume: float | None = None


@dataclass(slots=True)
class VolumeSnapshot:
    timestamp: datetime
    volume: float
    price: float


@dataclass(slots=True)
class EventTag:
    timestamp: datetime
    type: str
    subtype: str | None
    tags: Dict[str, Any]
    severity: float | None
    source: str


@dataclass(slots=True)
class ScoreSnapshot:
    timestamp: datetime
    flow_score: float
    oi_score: float
    volume_score: float
    manipulation_score: float
    regulatory_score: float
    overall_score: float


@dataclass(slots=True)
class ManipulationHint:
    timestamp: datetime
    depth_imbalance: float
    rapid_wall_change: bool
    risk_score: float
    note: str
