"""Worker that ingests market data and writes to Postgres/Redis."""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core import binance_client, deepseek_client
from core.config import settings
from core.db import (
    CompositeScore,
    DerivativesMetric,
    ExchangeFlow,
    OHLCV,
    SessionLocal,
    create_tables,
    engine,
)
from core.redis_client import cache_json
from core.utils import logger

create_tables()


def _log_db_status() -> None:
    url = settings.database_url

    if SessionLocal is None or engine is None:
        logger.info("Inflow worker database status: unavailable (url=%s)", url)
        return

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("Inflow worker database status: connected (url=%s)", url)
    except SQLAlchemyError as exc:
        logger.warning(
            "Inflow worker database status: unavailable (url=%s): %s", url, exc
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Inflow worker database status: unexpected error (url=%s): %s", url, exc
        )


def _session_factory_ready() -> bool:
    if SessionLocal is None:
        logger.warning(
            "SessionLocal not configured; skipping database writes this cycle."
        )
        return False

    try:
        session = SessionLocal()
        session.close()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not create database session; skipping writes this cycle: %s", exc
        )
        return False


def _get_session():
    try:
        return SessionLocal()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not create database session: %s", exc)
        return None


def _save_ohlcv_from_trades(trades: List[Dict], *, db_enabled: bool) -> Dict:
    if not trades:
        return {}
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
    if db_enabled:
        session = _get_session()
        if session is not None:
            with session.begin():
                session.add(ohlcv)
    return {
        "timestamp": ohlcv.timestamp.isoformat(),
        "open": ohlcv.open,
        "high": ohlcv.high,
        "low": ohlcv.low,
        "close": ohlcv.close,
        "volume": ohlcv.volume,
        "source": ohlcv.source,
    }


def _save_derivatives_metrics(*, db_enabled: bool) -> Dict:
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
    if db_enabled:
        session = _get_session()
        if session is not None:
            with session.begin():
                session.add(metric)
    return {
        "timestamp": metric.timestamp.isoformat(),
        "exchange": metric.exchange,
        "oi": metric.oi,
        "funding": metric.funding,
        "ls_ratio": metric.ls_ratio,
        "volume": metric.volume,
    }


def _save_exchange_flows(*, db_enabled: bool) -> List[Dict]:
    intel = {}
    try:
        intel = deepseek_client.fetch_market_intel()
    except Exception as exc:  # noqa: BLE001
        logger.info("DeepSeek intel unavailable: %s", exc)
    flows = intel.get("exchange_flows", []) if isinstance(intel, dict) else []
    if db_enabled and flows:
        session = _get_session()
        if session is not None:
            with session.begin():
                for flow in flows:
                    record = ExchangeFlow(
                        timestamp=datetime.utcnow(),
                        exchange=flow.get("exchange", "unknown"),
                        direction=flow.get("direction", "in"),
                        amount_xrp=float(flow.get("amount_xrp", 0)),
                        net_flow_xrp=float(
                            flow.get("net_flow_xrp", flow.get("amount_xrp", 0))
                        ),
                    )
                    session.add(record)
    return [
        {
            "exchange": flow.get("exchange", "unknown"),
            "direction": flow.get("direction", "in"),
            "amount_xrp": float(flow.get("amount_xrp", 0)),
            "net_flow_xrp": float(
                flow.get("net_flow_xrp", flow.get("amount_xrp", 0))
            ),
        }
        for flow in flows
    ]


def _snapshot_to_cache(*, db_enabled: bool) -> None:
    if not db_enabled:
        logger.info("Database unavailable; skipping dashboard snapshot cache refresh.")
        return

    session = _get_session()
    if session is None:
        logger.info("Cannot refresh snapshot cache because database session failed.")
        return

    with session.begin():
        recent_scores = (
            session.query(CompositeScore)
            .order_by(CompositeScore.timestamp.desc())
            .limit(50)
            .all()
        )
        flows = (
            session.query(ExchangeFlow)
            .order_by(ExchangeFlow.timestamp.desc())
            .limit(50)
            .all()
        )
        oi = (
            session.query(DerivativesMetric)
            .order_by(DerivativesMetric.timestamp.desc())
            .limit(50)
            .all()
        )
        price = (
            session.query(OHLCV)
            .order_by(OHLCV.timestamp.desc())
            .limit(200)
            .all()
        )
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


def _cache_latest_api_data(
    ohlcv: Dict, derivatives: Dict, exchange_flows: List[Dict]
) -> None:
    cache_json(
        "inflow:latest",  # diagnostic cache even when DB is down
        {
            "ohlcv": ohlcv,
            "derivatives": derivatives,
            "exchange_flows": exchange_flows,
        },
        ttl_seconds=600,
    )


def run_once() -> None:
    db_enabled = _session_factory_ready()
    trades = binance_client.fetch_recent_trades()
    ohlcv = _save_ohlcv_from_trades(trades, db_enabled=db_enabled)
    derivatives = _save_derivatives_metrics(db_enabled=db_enabled)
    exchange_flows = _save_exchange_flows(db_enabled=db_enabled)
    _cache_latest_api_data(ohlcv, derivatives, exchange_flows)
    _snapshot_to_cache(db_enabled=db_enabled)
    logger.info("Ingestion cycle complete")


def main(loop: bool = False, interval: int = 300) -> None:
    _log_db_status()
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
