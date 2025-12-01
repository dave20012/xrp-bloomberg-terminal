"""Streamlit dashboard for XRP analytics platform."""
from __future__ import annotations

from typing import Dict

import pandas as pd
import streamlit as st

from core.db import CompositeScore, DerivativesMetric, ExchangeFlow, OHLCV, SessionLocal, create_tables
from core.redis_client import get_cached_json
from core.utils import logger

st.set_page_config(page_title="XRP Intelligence", layout="wide")
st.title("XRP Intelligence Terminal")
st.caption("Volume-first analytics, enriched with flows and regulatory context")

tables_ready = create_tables()
if not tables_ready:
    st.warning(
        "Database connection unavailable. Falling back to cached dashboard snapshot when possible."
    )


def fetch_recent_scores(limit: int = 200) -> pd.DataFrame:
    session = SessionLocal()
    with session.begin():
        rows = (
            session.query(CompositeScore)
            .order_by(CompositeScore.timestamp.desc())
            .limit(limit)
            .all()
        )
    data = [
        {
            "timestamp": r.timestamp,
            "flow_score": r.flow_score,
            "oi_score": r.oi_score,
            "volume_score": r.volume_score,
            "manipulation_score": r.manipulation_score,
            "regulatory_score": r.regulatory_score,
            "overall_score": r.overall_score,
        }
        for r in reversed(rows)
    ]
    return pd.DataFrame(data)


def fetch_flows(limit: int = 200) -> pd.DataFrame:
    session = SessionLocal()
    with session.begin():
        rows = (
            session.query(ExchangeFlow)
            .order_by(ExchangeFlow.timestamp.desc())
            .limit(limit)
            .all()
        )
    data = [
        {
            "timestamp": r.timestamp,
            "exchange": r.exchange,
            "direction": r.direction,
            "net_flow_xrp": r.net_flow_xrp,
        }
        for r in reversed(rows)
    ]
    return pd.DataFrame(data)


def fetch_oi_metrics(limit: int = 200) -> pd.DataFrame:
    session = SessionLocal()
    with session.begin():
        rows = (
            session.query(DerivativesMetric)
            .order_by(DerivativesMetric.timestamp.desc())
            .limit(limit)
            .all()
        )
    data = [
        {
            "timestamp": r.timestamp,
            "oi": r.oi,
            "funding": r.funding,
            "ls_ratio": r.ls_ratio,
            "exchange": r.exchange,
        }
        for r in reversed(rows)
    ]
    return pd.DataFrame(data)


def fetch_price_volume(limit: int = 400) -> pd.DataFrame:
    session = SessionLocal()
    with session.begin():
        rows = (
            session.query(OHLCV)
            .order_by(OHLCV.timestamp.desc())
            .limit(limit)
            .all()
        )
    data = [
        {
            "timestamp": r.timestamp,
            "close": r.close,
            "volume": r.volume,
        }
        for r in reversed(rows)
    ]
    return pd.DataFrame(data)


@st.cache_data(ttl=300)
def load_data() -> Dict[str, pd.DataFrame]:
    try:
        score_df = fetch_recent_scores()
        flows_df = fetch_flows()
        oi_df = fetch_oi_metrics()
        price_df = fetch_price_volume()
        return {"scores": score_df, "flows": flows_df, "oi": oi_df, "price": price_df}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Falling back to cached JSON due to %s", exc)
        cached = get_cached_json("dashboard:snapshot") or {}
        return {
            "scores": pd.DataFrame(cached.get("scores", [])),
            "flows": pd.DataFrame(cached.get("flows", [])),
            "oi": pd.DataFrame(cached.get("oi", [])),
            "price": pd.DataFrame(cached.get("price", [])),
        }


data = load_data()
score_df = data["scores"]
flows_df = data["flows"]
oi_df = data["oi"]
price_df = data["price"]


latest_overall = score_df["overall_score"].iloc[-1] if not score_df.empty else 50.0
risk_color = "🟢" if latest_overall >= 65 else "🟡" if latest_overall >= 45 else "🔴"

col1, col2, col3, col4 = st.columns(4)
col1.metric("Overall State", f"{risk_color} {latest_overall:.1f}")
if not score_df.empty:
    col2.metric("Flow Score", f"{score_df['flow_score'].iloc[-1]:.1f}")
    col3.metric("Leverage Score", f"{score_df['oi_score'].iloc[-1]:.1f}")
    col4.metric("Volume Regime", f"{score_df['volume_score'].iloc[-1]:.1f}")

st.divider()

left, right = st.columns((2, 1))
with left:
    st.subheader("Price & Volume")
    if price_df.empty:
        st.info("No price data yet. Workers will populate soon.")
    else:
        price_chart = price_df.set_index("timestamp")["close"]
        volume_chart = price_df.set_index("timestamp")["volume"]
        st.line_chart(price_chart, height=300)
        st.area_chart(volume_chart, height=200, use_container_width=True)

with right:
    st.subheader("Composite Scores")
    if score_df.empty:
        st.warning("Scores are not yet available.")
    else:
        st.line_chart(score_df.set_index("timestamp")[["overall_score", "flow_score", "oi_score", "volume_score"]])

st.divider()

col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Exchange Net Flows")
    if flows_df.empty:
        st.info("Awaiting flow data.")
    else:
        st.bar_chart(flows_df.set_index("timestamp")["net_flow_xrp"], height=300)

with col_b:
    st.subheader("Open Interest & Funding")
    if oi_df.empty:
        st.info("No derivatives metrics yet.")
    else:
        metrics = oi_df.set_index("timestamp")
        st.line_chart(metrics[["oi", "ls_ratio"]])
        st.area_chart(metrics[["funding"]])

st.divider()
st.markdown("### Methodology")
st.write(
    """
    Volume and exchange flows drive the analytics. Regulatory and news signals are used as overlays,
    never as the primary driver. Scores are deterministic and can be traced back to raw inputs.
    """
)


if __name__ == "__main__" and not st.runtime.exists():
    import os
    import sys

    from streamlit.web import cli as stcli

    sys.argv = ["streamlit", "run", os.path.abspath(__file__)]
    raise SystemExit(stcli.main())
