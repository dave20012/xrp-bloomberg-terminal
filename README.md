# XRP Bloomberg Terminal

Streamlit dashboard with supporting workers that surface XRP price action, XRPL inflows, Binance positioning, and FinBERT news sentiment. Data is cached in Redis so the web app and workers can share signals.

## Signal scoring overview
- **Headline sentiment** – FinBERT scores (`pos`, `neg`, `scalar`) are weighted by per-source reliability. “Institutional Only” mode keeps weights ≥ 0.6. Bullish/bearish intensities are instantaneous weighted averages (no 3-day rollups). The sentiment EMA (α = 0.3 by default) is the only smoothed series.
- **Composite score (0–100)** – The live “NEUTRAL/WATCH/ALERT” label comes from weighted sub-scores:
  - Funding z-score: up to 22 pts via a capped tanh.
  - Whale flow (weighted XRPL inflow): up to 14 pts, scaled against 60M XRP.
  - Price window: up to 28 pts when price is under $2.45, linearly decaying to $3.00.
  - Open interest: up to 16 pts when OI ≥ $2.7B, decaying to $1.5B.
  - Binance netflow: up to 30 pts, scaled against 100M XRP equivalent.
  - Short-squeeze setup: up to 20 pts when L/S ratio ≤ 1.0, decaying to 2.0.
  - News sentiment EMA: up to 15 pts when EMA ≥ 0.3, decaying to 0.05.
  - Flippening flow: up to 15 pts when BTC/ETH ratio uplift is positive **and** weighted inflow > 10M XRP.
- The sub-scores are **not equal-weighted**; they are capped and summed (clamped at 100). See [`docs/dashboard_redesign.md`](docs/dashboard_redesign.md) for improvement ideas and target/stop guidance.

## Architecture
- **Web app (`main.py`)** pulls market data from CoinGecko (Binance fallback), funding/oi/netflow from Binance, and reads Redis for XRPL inflows plus sentiment. Plotly renders price/volume charts and SMA backtests.
- **XRPL inflow worker (`xrpl_inflow_monitor.py`)** fetches on-chain activity from Ripple Data or Whale Alert and writes JSON payloads to Redis (including optional exchange outflows).
- **Sentiment worker (`sentiment_worker.py`)** calls News API + FinBERT (when HF_TOKEN provided) and stores article-level scores + EMA inputs in Redis.
- **Shared Redis keys** keep the flows connected:
  - `news:sentiment` – latest sentiment payload written by the worker.
  - `news:sentiment_ema` – cached EMA used by the web app between refreshes.
  - `xrpl:latest_inflows` – most recent XRPL inflow slice.
  - `xrpl:latest_inflows_meta` – heartbeat describing the last inflow poll (timestamp/provider).
  - `xrpl:latest_outflows` – most recent XRPL outflow slice (if enabled).
  - `xrpl:latest_outflows_meta` – heartbeat describing the last outflow poll (timestamp/provider).
  - `xrpl:inflow_history` – rolling inflow history for charts/analytics.
  - `xrpl:outflow_history` – rolling outflow history for charts/analytics.
  - `cache:price:xrp_usd` – price fallback when APIs are unavailable.
  - `ratio_ema:xrp_btc`, `ratio_ema:xrp_eth` – cached EMA baselines for flippening ratios.

## Quick start (local)
1. Create a virtual environment and install deps:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in any keys you have (optional).
3. Start Redis (Docker example):
   ```bash
   docker run --rm -p 6379:6379 redis
   ```
4. Run the dashboard:
   ```bash
   streamlit run main.py
   ```
5. Run workers locally when you want live inflows/sentiment and to persist
   data into PostgreSQL:
   ```bash
   # In one terminal: XRPL inflow monitor (writes to Redis and can also feed DB via the web app)
   REDIS_URL=redis://localhost:6379 python xrpl_inflow_monitor.py

   # In another terminal: sentiment worker (writes sentiment to Redis)
   REDIS_URL=redis://localhost:6379 python sentiment_worker.py

   # In a third terminal: data ingestion worker (writes snapshots and flows to Timescale/PostgreSQL)
   PG_HOST=localhost PG_PORT=5432 PG_USER=... PG_PASSWORD=... PG_DB=... python worker.py --once
   ```

   The `worker.py` script inserts a consolidated snapshot of price, open
   interest, funding, long/short ratio, relative volume and on‑chain
   flows into TimescaleDB every 5 minutes (when run without `--once`).

6. Optional: backfill historical data into the database using a CSV.
   Prepare a CSV with columns `timestamp`, `price_close`, `volume`,
   `aggregated_oi_usd`, `funding_rate` and `long_short_ratio`.  Then run:
   ```bash
   python import_backfill.py --csv path/to/your_data.csv
   ```

7. Optional: run a simple backtest on the stored composite scores:
   ```bash
   python backtest.py --start 2025-01-01 --end 2025-06-01 --entry 65 --exit 35
   ```
   Adjust `--entry` and `--exit` to reflect your risk tolerance.  The
   script prints both the strategy and buy‑and‑hold cumulative returns.

## Deployment (Railway)
1. Push this repo to your Git provider and connect it to Railway (or push directly via Railway CLI).
2. **Provision Redis as a separate service**: use the built-in Redis plugin (Dashboard → Plugins → Redis). Railway gives you `REDIS_URL` automatically—no custom Dockerfile is required. Avoid pointing this repo at a Redis service with a start command such as `docker-entrypoint.sh redis-server`; this image is Python-based and does not ship the Redis entrypoint, so the container will crash with `docker-entrypoint.sh: not found`. If you truly need a self-hosted Redis container, create a new Railway service that uses the official `redis:7-alpine` image and a simple start command like `redis-server --save 60 1 --dir $RAILWAY_VOLUME_MOUNT_PATH`.
3. Configure environment variables (Project → Variables):
   - `REDIS_URL` (set automatically with the plugin)
   - `BINANCE_API_KEY`, `BINANCE_API_SECRET` (optional; netflow)
   - `NEWS_API_KEY` (optional; sentiment worker)
   - `WHALE_ALERT_KEY` (required only if `XRPL_INFLOWS_PROVIDER=whale_alert`)
   - `XRPL_INFLOWS_PROVIDER` (`whale_alert` paid, `ripple_data` free, `rippled` RPC fallback)
   - `XRPL_MIN_XRP` (optional threshold for inflow/outflow alerts, default 10,000,000 XRP)
   - `XRPL_MONITOR_OUTFLOWS` (set to `0` to disable publishing exchange outflows; defaults to on)
   - `XRPL_LOOKBACK_SECONDS` (optional when using `ripple_data`; default max(RUN*2, 900))
   - `XRPL_RPC_ENDPOINTS` (comma-separated rippled JSON-RPC URLs; defaults to public s1/s2 endpoints)
   - `RIPPLE_DATA_COOLDOWN_SECONDS` / `RIPPLE_DATA_MAX_COOLDOWN_SECONDS` (cooldown/backoff applied after repeated Ripple Data errors; defaults 900s/3600s)
   - `HF_TOKEN` (optional; FinBERT inference)
   - `META_REFRESH_SECONDS` (optional, default 45)
   - `XRPL_POLL_SECONDS` (optional, default 30; `XRPL_INFLOWS_INTERVAL` still supported for backwards compatibility)
   - `SENTIMENT_RUN_INTERVAL` (optional override, default 1800 seconds)
4. Railway will detect the Procfile and run the following processes:
   - `web`: Streamlit app
   - `worker_inflow`: XRPL inflow monitor
   - `worker_sentiment`: sentiment worker
   - `worker_data`: 5‑minute ingestion worker that writes snapshots
     and flows into TimescaleDB
5. Deploy and monitor logs:
   - Web logs show Streamlit errors and API failures.
   - Worker logs show XRPL inflow and sentiment polling runs.
6. Verify Redis keys are populated (`news:sentiment`, `xrpl:latest_inflows`, `xrpl:inflow_history`).

7. When using the TimescaleDB plugin (configured in `railway.json`), set
   the database environment variables (`PG_HOST`, `PG_PORT`,
   `PG_USER`, `PG_PASSWORD`, `PG_DB`) in the Railway dashboard.  The
   ingestion worker (`worker_data`) will automatically create the
   required tables on first run and persist snapshots every 5 minutes.

> **Binance key format:** paste the raw `API Key` and `Secret Key` strings from the Binance dashboard. Do **not** include `${{ }}` wrappers, quotes, or trailing spaces—formatted CI placeholders will be rejected by Binance with `API-key format invalid`. The app trims accidental surrounding quotes, but storing the bare values in Railway variables avoids silent authentication failures.

## Configuration & security notes
- Use `.env` (based on `.env.example`) locally and Railway project variables in production.
- Keep API keys scoped minimally; CoinGecko is rate-limited—lowering `META_REFRESH_SECONDS` increases traffic.
- Missing keys are handled gracefully with cached fallbacks, but the UI now surfaces Redis/cache health hints when data is stale.

## Testing
Run lightweight unit tests:
```bash
python -m unittest discover -s tests
```

