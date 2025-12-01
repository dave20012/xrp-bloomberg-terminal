from core.models import OIMetrics
from core.signals import (
    aggregate_scores,
    compute_flow_signal,
    compute_oi_leverage_score,
    compute_volume_signal,
)


def test_volume_signal_zscore():
    volumes = [100] * 10 + [200]
    sig = compute_volume_signal(volumes)
    assert sig.zscore > 0
    assert sig.regime in {"high", "normal", "low"}


def test_flow_signal_regime_changes():
    flows = [-50, -20, 10, 80]
    sig = compute_flow_signal(flows)
    assert isinstance(sig.net_flow, float)
    assert sig.regime in {"high", "normal", "low"}


def test_oi_leverage_score_bounds():
    metrics = [
        OIMetrics(timestamp=None, exchange="binance", oi=1000, funding=0.01, ls_ratio=1.1, volume=2000),
        OIMetrics(timestamp=None, exchange="binance", oi=1200, funding=0.02, ls_ratio=1.2, volume=2500),
    ]
    score = compute_oi_leverage_score(metrics)
    assert 0 <= score <= 100


def test_aggregate_scores_weighting():
    total = aggregate_scores(
        flow_score=60,
        oi_score=55,
        volume_score=50,
        manipulation_score=20,
        regulatory_score=40,
    )
    assert 0 <= total <= 100
