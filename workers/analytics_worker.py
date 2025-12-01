"""Analytics worker to compute composite scores."""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta

from core import binance_client
from core.db import CompositeScore, DerivativesMetric, Event, ExchangeFlow, OHLCV, SessionLocal, create_tables
from core.redis_client import cache_json
from core.signals import (
    FlowSignal,
    VolumeSignal,
    aggregate_scores,
    compute_flow_signal,
    compute_manipulation_hint,
    compute_oi_leverage_score,
    compute_regulatory_score,
    compute_volume_signal,
)
from core.utils import logger

create_tables()


def _load_recent_data(hours: int = 48):
    session = SessionLocal()
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with session.begin():
        flows = session.query(ExchangeFlow).filter(ExchangeFlow.timestamp >= cutoff).order_by(ExchangeFlow.timestamp).all()
        oi = session.query(DerivativesMetric).filter(DerivativesMetric.timestamp >= cutoff).order_by(DerivativesMetric.timestamp).all()
        ohlcv = session.query(OHLCV).filter(OHLCV.timestamp >= cutoff).order_by(OHLCV.timestamp).all()
        events = session.query(Event).filter(Event.timestamp >= cutoff).order_by(Event.timestamp).all()
    return flows, oi, ohlcv, events


def _save_score(flow_sig: FlowSignal, vol_sig: VolumeSignal, oi_score: float, manipulation_score: float, regulatory_score: float) -> None:
    score = aggregate_scores(
        flow_score=50 + flow_sig.zscore * 10,
        oi_score=oi_score,
        volume_score=50 + vol_sig.zscore * 10,
        manipulation_score=manipulation_score * 100,
        regulatory_score=regulatory_score,
    )
    composite = CompositeScore(
        timestamp=datetime.utcnow(),
        flow_score=50 + flow_sig.zscore * 10,
        oi_score=oi_score,
        volume_score=50 + vol_sig.zscore * 10,
        manipulation_score=manipulation_score * 100,
        regulatory_score=regulatory_score,
        overall_score=score,
    )
    session = SessionLocal()
    with session.begin():
        session.add(composite)
    cache_json(
        "latest:score",
        {
            "flow": flow_sig.__dict__,
            "volume": vol_sig.__dict__,
            "oi_score": oi_score,
            "manipulation_score": manipulation_score,
            "regulatory_score": regulatory_score,
            "overall": score,
        },
        ttl_seconds=600,
    )


def run_once() -> None:
    flows, oi_metrics, ohlcv, events = _load_recent_data()
    flow_values = [f.net_flow_xrp for f in flows]
    volumes = [row.volume for row in ohlcv]
    flow_sig = compute_flow_signal(flow_values)
    vol_sig = compute_volume_signal(volumes)
    oi_score = compute_oi_leverage_score(
        [
            oi_metrics[i]
            for i in range(len(oi_metrics))
        ]
    )
    order_book = binance_client.fetch_order_book()
    order_book_stats = binance_client.summarize_order_book(order_book)
    volume_spike = vol_sig.zscore > 1.5
    manipulation_hint = compute_manipulation_hint(order_book_stats, volume_spike)
    reg_events = [e.tags or {} for e in events if e.type == "regulatory"]
    reg_score = compute_regulatory_score(reg_events)
    _save_score(flow_sig, vol_sig, oi_score, manipulation_hint.risk_score, reg_score)
    logger.info("Analytics computed: overall=%s", manipulation_hint.risk_score)


def main(loop: bool = False, interval: int = 600) -> None:
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Analytics worker failure: %s", exc)
        if not loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=600)
    args = parser.parse_args()
    main(loop=args.loop, interval=args.interval)
