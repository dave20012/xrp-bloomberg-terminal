"""Worker that ingests market data and writes to Postgres/Redis."""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Dict, List

from core import binance_client, deepseek_client
from core.db import (
    CompositeScore,
    DerivativesMetric,
    ExchangeFlow,
    OHLCV,
    SessionLocal,
    create_tables,
)
from core.redis_client import cache_json
from core.utils import logger

create_tables()


def _save_ohlcv_from_trades(trades: List[Dict]) -> None:
    if not trades:
        return
    prices = [float(t["price"]) for t in trades]
    volumes = [float(t["qty"]) for t in trades]
    ohlcv = OHLCV(
        timestamp=datetime.utcnow(),
        open=prices[0],
        high=max(prices),
        low=min(prices),
        close=prices[-1],
        volume=sum(volumes),
        source="binance-trades",
    )
    session = SessionLocal()
    with session.begin():
        session.add(ohlcv)


def _save_derivatives_metrics() -> None:
    oi = binance_client.fetch_open_interest()
    funding = binance_client.fetch_funding_rate()
    ls = binance_client.fetch_long_short_ratio()
    metric = DerivativesMetric(
        timestamp=datetime.utcnow(),
        exchange="binance",
        oi=float(oi.get("sumOpenInterest", 0) or oi.get("sumOpenInterestValue", 0) or 0),
        funding=float(funding.get("lastFundingRate", 0)),
        ls_ratio=float(ls.get("longShortRatio", 1)),
        volume=float(oi.get("sumOpenInterestValue", 0) or 0),
    )
    session = SessionLocal()
    with session.begin():
        session.add(metric)


def _save_exchange_flows() -> None:
    intel = {}
    try:
        intel = deepseek_client.fetch_market_intel()
    except Exception as exc:  # noqa: BLE001
        logger.info("DeepSeek intel unavailable: %s", exc)
    flows = intel.get("exchange_flows", []) if isinstance(intel, dict) else []
    session = SessionLocal()
    with session.begin():
        for flow in flows:
            record = ExchangeFlow(
                timestamp=datetime.utcnow(),
                exchange=flow.get("exchange", "unknown"),
                direction=flow.get("direction", "in"),
                amount_xrp=float(flow.get("amount_xrp", 0)),
                net_flow_xrp=float(flow.get("net_flow_xrp", flow.get("amount_xrp", 0))),
            )
            session.add(record)


def _snapshot_to_cache() -> None:
    session = SessionLocal()
    with session.begin():
        recent_scores = session.query(CompositeScore).order_by(CompositeScore.timestamp.desc()).limit(50).all()
        flows = session.query(ExchangeFlow).order_by(ExchangeFlow.timestamp.desc()).limit(50).all()
        oi = session.query(DerivativesMetric).order_by(DerivativesMetric.timestamp.desc()).limit(50).all()
        price = session.query(OHLCV).order_by(OHLCV.timestamp.desc()).limit(200).all()
    payload = {
        "scores": [
            {
                "timestamp": s.timestamp.isoformat(),
                "flow_score": s.flow_score,
                "oi_score": s.oi_score,
                "volume_score": s.volume_score,
                "manipulation_score": s.manipulation_score,
                "regulatory_score": s.regulatory_score,
                "overall_score": s.overall_score,
            }
            for s in recent_scores
        ],
        "flows": [
            {
                "timestamp": f.timestamp.isoformat(),
                "exchange": f.exchange,
                "direction": f.direction,
                "amount_xrp": f.amount_xrp,
                "net_flow_xrp": f.net_flow_xrp,
            }
            for f in flows
        ],
        "oi": [
            {
                "timestamp": m.timestamp.isoformat(),
                "exchange": m.exchange,
                "oi": m.oi,
                "funding": m.funding,
                "ls_ratio": m.ls_ratio,
                "volume": m.volume,
            }
            for m in oi
        ],
        "price": [
            {
                "timestamp": p.timestamp.isoformat(),
                "open": p.open,
                "high": p.high,
                "low": p.low,
                "close": p.close,
                "volume": p.volume,
                "source": p.source,
            }
            for p in price
        ],
    }
    cache_json("dashboard:snapshot", payload, ttl_seconds=600)


def run_once() -> None:
    trades = binance_client.fetch_recent_trades()
    _save_ohlcv_from_trades(trades)
    _save_derivatives_metrics()
    _save_exchange_flows()
    _snapshot_to_cache()
    logger.info("Ingestion cycle complete")


def main(loop: bool = False, interval: int = 300) -> None:
    while True:
        try:
            run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Inflow worker error: %s", exc)
        if not loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between polls")
    args = parser.parse_args()
    main(loop=args.loop, interval=args.interval)
