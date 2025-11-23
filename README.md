Deployment steps (Railway):

1) Push this repo to your Git provider and connect it to Railway (or push directly via Railway CLI).

2) Add Redis plugin in Railway:
   - Railway Dashboard → Plugins → Redis
   - Copy Redis connection string (REDIS_URL) and make sure it's available in project env

3) Set environment variables in Railway (Project → Variables):
   - REDIS_URL (set automatically when adding Redis plugin)
   - BINANCE_API_KEY, BINANCE_API_SECRET (optional; for netflow)
   - NEWS_API_KEY (optional; for sentiment worker)
   - WHALE_ALERT_KEY (required only if XRPL_INFLOWS_PROVIDER=whale_alert)
   - XRPL_INFLOWS_PROVIDER ("whale_alert" paid, or "ripple_data" free)
   - XRPL_MIN_XRP (optional threshold for inflow alerts, default 10,000,000 XRP)
   - XRPL_LOOKBACK_SECONDS (optional when using ripple_data provider; default max(RUN*2, 900))
   - HF_TOKEN (optional; FinBERT inference)
   - META_REFRESH_SECONDS (optional, default 45)
   - XRPL_POLL_SECONDS (optional, default 30)
   - SENTIMENT_RUN_INTERVAL (optional override, default 1800 seconds)

4) Railway will detect the Procfile and run:
   - web : streamlit app
   - worker_inflow : XRPL inflow monitor
   - worker_sentiment : sentiment worker

   Ensure project resource limits cover the workers.

5) Deploy. Monitor logs:
   - Web logs show Streamlit errors and app prints
   - Worker logs show xrpl_inflow and sentiment runs

6) Verify:
   - Open app URL (web)
   - Confirm Redis keys being set:
     - news:sentiment  (JSON payload)
     - xrpl:latest_inflows (JSON)
     - xrpl:inflow_history (list)

Notes:
- Redis is necessary for cross-process data sharing on Railway.
- Do not commit secrets.
- If you cannot run HF inference (no HF_TOKEN or rate limits), sentiment_worker will still write heuristic defaults (0) or empty scores.
- Set `XRPL_INFLOWS_PROVIDER=ripple_data` to avoid the paid Whale Alert API; this uses the curated exchange hot-wallet list in
  `exchange_addresses.py` and the public Ripple Data API with `XRPL_MIN_XRP` as the threshold.
- Local testing: run workers locally with REDIS_URL pointing to a local Redis, then run `streamlit run main.py`.
