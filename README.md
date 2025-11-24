# XRP Bloomberg Terminal

Streamlit dashboard with supporting workers that surface XRP price action, XRPL inflows, Binance positioning, and FinBERT news sentiment. Data is cached in Redis so the web app and workers can share signals.

## Architecture
- **Web app (`main.py`)** pulls market data from CoinGecko (Binance fallback), funding/oi/netflow from Binance, and reads Redis for XRPL inflows plus sentiment. Plotly renders price/volume charts and SMA backtests.
- **XRPL inflow worker (`xrpl_inflow_monitor.py`)** fetches on-chain activity from Ripple Data or Whale Alert and writes JSON payloads to Redis.
- **Sentiment worker (`sentiment_worker.py`)** calls News API + FinBERT (when HF_TOKEN provided) and stores article-level scores + EMA inputs in Redis.
- **Shared Redis keys** keep the flows connected:
  - `news:sentiment` – latest sentiment payload written by the worker.
  - `news:sentiment_ema` – cached EMA used by the web app between refreshes.
  - `xrpl:latest_inflows` – most recent XRPL inflow slice.
  - `xrpl:inflow_history` – rolling inflow history for charts/analytics.
  - `cache:price:xrp_usd` – price fallback when APIs are unavailable.

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
5. Run workers locally when you want live inflows/sentiment:
   ```bash
   REDIS_URL=redis://localhost:6379 python xrpl_inflow_monitor.py
   REDIS_URL=redis://localhost:6379 python sentiment_worker.py
   ```

## Deployment (Railway)
1. Push this repo to your Git provider and connect it to Railway (or push directly via Railway CLI).
2. **Provision Redis as a separate service**: use the built-in Redis plugin (Dashboard → Plugins → Redis). Railway gives you `REDIS_URL` automatically—no custom Dockerfile is required. Avoid pointing this repo at a Redis service with a start command such as `docker-entrypoint.sh redis-server`; this image is Python-based and does not ship the Redis entrypoint, so the container will crash with `docker-entrypoint.sh: not found`. If you truly need a self-hosted Redis container, create a new Railway service that uses the official `redis:7-alpine` image and a simple start command like `redis-server --save 60 1 --dir $RAILWAY_VOLUME_MOUNT_PATH`.
3. Configure environment variables (Project → Variables):
   - `REDIS_URL` (set automatically with the plugin)
   - `BINANCE_API_KEY`, `BINANCE_API_SECRET` (optional; netflow)
   - `NEWS_API_KEY` (optional; sentiment worker)
   - `WHALE_ALERT_KEY` (required only if `XRPL_INFLOWS_PROVIDER=whale_alert`)
   - `XRPL_INFLOWS_PROVIDER` (`whale_alert` paid, or `ripple_data` free)
   - `XRPL_MIN_XRP` (optional threshold for inflow alerts, default 10,000,000 XRP)
   - `XRPL_LOOKBACK_SECONDS` (optional when using `ripple_data`; default max(RUN*2, 900))
   - `HF_TOKEN` (optional; FinBERT inference)
   - `META_REFRESH_SECONDS` (optional, default 45)
   - `XRPL_POLL_SECONDS` (optional, default 30)
   - `SENTIMENT_RUN_INTERVAL` (optional override, default 1800 seconds)
4. Railway will detect the Procfile and run:
   - `web`: Streamlit app
   - `worker_inflow`: XRPL inflow monitor
   - `worker_sentiment`: sentiment worker
5. Deploy and monitor logs:
   - Web logs show Streamlit errors and API failures.
   - Worker logs show XRPL inflow and sentiment polling runs.
6. Verify Redis keys are populated (`news:sentiment`, `xrpl:latest_inflows`, `xrpl:inflow_history`).

## Configuration & security notes
- Use `.env` (based on `.env.example`) locally and Railway project variables in production.
- Keep API keys scoped minimally; CoinGecko is rate-limited—lowering `META_REFRESH_SECONDS` increases traffic.
- Missing keys are handled gracefully with cached fallbacks, but the UI now surfaces Redis/cache health hints when data is stale.

## Testing
Run lightweight unit tests:
```bash
python -m unittest discover -s tests
```

