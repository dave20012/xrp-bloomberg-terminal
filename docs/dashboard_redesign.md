# Dashboard redesign and signal reliability

## How the composite score works today
- Inputs are weighted and capped per `main.py`:
  - Funding z-score: up to 22 pts (`tanh` capped positive values).
  - Weighted XRPL inflow: up to 14 pts, linear vs. 60M XRP.
  - Price window: up to 28 pts at <$2.45, decays to $3.00.
  - Open interest: up to 16 pts at ≥$2.7B, decays to $1.5B.
  - Binance netflow: up to 30 pts, linear vs. 100M XRP equivalent.
  - Short-squeeze setup: up to 20 pts when L/S ≤ 1.0, decays to 2.0.
  - News sentiment EMA: up to 15 pts at EMA ≥ 0.3, decays to 0.05.
  - Flippening flow: up to 15 pts when BTC/ETH ratio uplift is positive **and** weighted inflow > 10M XRP.
- Total score is the capped sum (0–100) feeding the NEUTRAL/WATCH/ALERT badge. Components are deliberately **not** equal-weighted; they emphasize price levels, funding extremes, and liquidity/flow.
- Bullish/bearish intensities are instantaneous weighted averages of FinBERT `pos`/`neg` per headline; they are not smoothed or multi-day aggregates. The sentiment EMA (α = 0.3) is the only time-smoothed sentiment metric.

## Reliability notes
- Price direction and the composite score can diverge in the short term; the score reflects the weighted state of flows, funding, sentiment, and ratio baselines rather than price momentum.
- Missing feeds fall back to cached values and surface in the UI’s data-health banner so operators can correlate unusual scores with stale inputs.
- Netflow, inflows, and sentiment caps prevent single feeds from overwhelming the total.

## Targeting and trade-setup guidance (roadmap)
1. **Directional conviction + target bands**
   - Add probabilistic labels (e.g., `Bullish 65%`) using a calibrated classifier on historical feature vectors (`fund_z`, `netflow_score`, `oi_score`, `sentiment_score`, ratio uplift). Log posterior probability next to the composite score.
   - Attach dynamic target/stop bands derived from ATR on XRP/USD and ratio z-scores; display as "Entry $x | TP1 $y | TP2 $z | Invalidation $w".
2. **Flow decomposition**
   - Separate exchange vs. OTC inflows; plot stacked bars with a 7-day z-score overlay to explain whale-driven moves.
   - Add delta tiles showing 24h/7d changes for OI, netflow, and funding.
3. **Sentiment drill-down**
   - Show top 5 negative/positive headlines with source weights and time buckets (0–1h, 1–6h, 6–24h). Include a toggle to exclude low-weight sources without switching modes.
4. **Regime detection**
   - Flag when funding/price diverge (e.g., negative funding with rising price) and when ratios detach from EMAs; display regime badges ("Squeeze", "Liquidity drain", "Carry unwind").
5. **Visual/UX upgrades**
   - Two-row hero: price/ratio cards + sparkline minis on top; flows/sentiment heatmaps below.
   - Use consistent typography, padding, and dark-mode palette; align columns and add tooltips explaining each sub-score and cap.
   - Add a compact "system health" strip (Redis freshness, last worker run, API throttling) with green/amber/red states.
6. **Backtest panel**
   - Re-run the SMA/volume backtest with the composite score as an entry filter; chart hit-rate and average return per bucket. Allow toggling thresholds (e.g., score ≥ 65) to see lift.

## Implementation checklist
- Add a `signals.py` module to centralize weighting constants, caps, and tooltips so the UI and workers share a single source of truth.
- Log each component’s contribution alongside the total in Redis (e.g., `score:components`) for auditability and overlay on historical charts.
- Introduce a `targets.py` utility to compute ATR-based price bands and risk metrics (win rate, max drawdown) for suggested targets.
- Build a `SentimentPanel` helper to render weighted headline tables with source badges and time buckets.
- Create a reusable `HealthStrip` component for freshness and throttle alerts, driving consistent UX cues across tabs.
