# XRP Intelligence Terminal

A volume-first XRP analytics and monitoring platform focused on market structure, exchange flows, leverage regimes, and regulatory context. Built for lightweight Railway deployment with Streamlit, PostgreSQL, and Redis.

## Philosophy
- **Volume and flows drive the narrative.** Price is contextual, not the core signal.
- **Regulatory/news are overlays.** Headlines are tagged and tracked, not naively converted to sentiment.
- **Deterministic and explainable.** Scores are rule-based and configurable.

## Features
- Exchange inflow/outflow pressure using curated wallet lists and DeepSeek enrichment.
- Derivatives metrics: open interest, funding, and long/short skew from Binance futures.
- Volume regime and anomaly detection using rolling z-scores.
- Accumulation vs. distribution heuristics driven by flow + price divergence.
- Manipulation hints via order book depth imbalance and volume spikes.
- Regulatory and macro tagging of headlines via Hugging Face inference + NewsAPI.
- Composite scoring system combining flow, leverage, volume, manipulation, and regulatory risk.
- Streamlit dashboard with dark-friendly layout, cached snapshots via Redis.

## Project Layout
```
main.py                  # Streamlit dashboard
core/                    # Shared libraries
  config.py              # Environment + endpoints
  db.py                  # SQLAlchemy models and session helpers
  redis_client.py        # Redis caching helpers
  binance_client.py      # Binance spot/futures lightweight client
  cc_client.py           # CryptoCompare historical data
  deepseek_client.py     # DeepSeek enrichment
  news_client.py         # NewsAPI integration
  hf_client.py           # Hugging Face inference helper
  signals.py             # Signal and scoring logic
  models.py              # Typed domain models
  exchange_addresses.py  # Known exchange wallets
  utils.py               # Logging, retry, math helpers
workers/                 # Background services
  inflow_worker.py       # Ingest flows/OHLCV/OI and cache snapshot
  analytics_worker.py    # Compute composite scores
  news_worker.py         # Pull and tag headlines
  scheduler.py           # Optional sequential runner
.github/workflows/ci.yml # Minimal CI with pytest
requirements.txt         # Python dependencies
.env.example             # Environment template
railway_setup.sh/.bat    # Railway helper scripts
```

## Setup
1. Clone the repository and create a virtual environment.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in API keys.
4. Ensure PostgreSQL and Redis are reachable; defaults assume local services.

## Running Locally
### Database
The app auto-creates tables on first run. To pre-create manually:
```bash
python -c "from core.db import create_tables; create_tables()"
```

### Workers
Run workers once or in a loop:
```bash
python workers/inflow_worker.py --loop --interval 300
python workers/news_worker.py --loop --interval 1800
python workers/analytics_worker.py --loop --interval 600
```

### Dashboard
```bash
streamlit run main.py --server.port 8080 --server.address 0.0.0.0
```

## Railway Deployment
### One-time setup
Use the helper script (requires Railway CLI):
```bash
chmod +x railway_setup.sh
./railway_setup.sh
```
The script will prompt for a project name, create/link services, and suggest environment variables. Windows users can run `railway_setup.bat`.

### Services
- **web**: `streamlit run main.py --server.port 8080 --server.address 0.0.0.0`
- **inflow-worker**: `python workers/inflow_worker.py --loop`
- **analytics-worker**: `python workers/analytics_worker.py --loop`
- **news-worker**: `python workers/news_worker.py --loop`

Provision PostgreSQL and Redis via Railway plugins; `DATABASE_URL` and `REDIS_URL` are injected automatically. Add API keys in the Railway dashboard or via CLI.

## Testing
Run the test suite:
```bash
pytest
```

## CI/CD
GitHub Actions workflow (`.github/workflows/ci.yml`) runs tests on pushes to `main`. Connect the repo to Railway for automated deployments using Railway's GitHub integration.

## Notes
- All external data uses documented APIs; no scraping.
- Deep learning runs only on hosted inference (Hugging Face); nothing heavy runs locally.
- Composite scores are explainable and volume/flow-first by design.
