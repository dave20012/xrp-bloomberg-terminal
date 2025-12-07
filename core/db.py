"""Database models and session helpers using SQLAlchemy."""
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

from core.config import settings
from core.utils import logger

Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(UTC)


class OHLCV(Base):
    __tablename__ = "ohlcv"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    source = Column(String, default="binance")


class ExchangeFlow(Base):
    __tablename__ = "exchange_flows"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    exchange = Column(String, nullable=False)
    direction = Column(String, nullable=False)  # in or out
    amount_xrp = Column(Float, nullable=False)
    net_flow_xrp = Column(Float, nullable=False)


class DerivativesMetric(Base):
    __tablename__ = "derivatives_metrics"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    exchange = Column(String, default="binance")
    oi = Column(Float, nullable=True)
    funding = Column(Float, nullable=True)
    ls_ratio = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)


class CompositeScore(Base):
    __tablename__ = "composite_scores"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    flow_score = Column(Float, nullable=True)
    oi_score = Column(Float, nullable=True)
    volume_score = Column(Float, nullable=True)
    manipulation_score = Column(Float, nullable=True)
    regulatory_score = Column(Float, nullable=True)
    overall_score = Column(Float, nullable=True)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow, index=True)
    type = Column(String, nullable=False)
    subtype = Column(String, nullable=True)
    tags = Column(JSON, nullable=True)
    source = Column(String, nullable=False)
    severity = Column(Float, nullable=True)


try:
    engine = create_engine(settings.database_url, echo=False, future=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
except Exception as exc:  # noqa: BLE001
    logger.warning(
        "Database engine unavailable during startup; continuing without DB: %s", exc
    )
    engine = None
    SessionLocal = None


def create_tables() -> bool:
    """Create database tables if they do not exist.

    Returns
    -------
    bool
        True when the tables were created or already exist, False if the
        database is unreachable.
    """

    if engine is None:
        logger.warning("Database engine not initialized; skipping table creation.")
        return False

    try:
        Base.metadata.create_all(bind=engine)
        return True
    except SQLAlchemyError as exc:
        logger.warning(
            "Skipping table creation because the database is unavailable: %s", exc
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Unexpected error during table creation; continuing without DB: %s", exc
        )
        return False


def get_session():
    """Provide a new SQLAlchemy session."""
    if SessionLocal is None:
        raise RuntimeError("Database is not available; cannot create a session.")

    return SessionLocal()
