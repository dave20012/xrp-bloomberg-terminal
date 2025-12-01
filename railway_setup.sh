#!/usr/bin/env bash
set -euo pipefail

read -rp "Railway project name: " PROJECT_NAME

if ! command -v railway >/dev/null 2>&1; then
  echo "Install the Railway CLI first: https://railway.app/cli" && exit 1
fi

echo "Linking project..."
railway init --project "$PROJECT_NAME"

# Create services
railway service:create web || true
railway service:create inflow-worker || true
railway service:create analytics-worker || true
railway service:create news-worker || true

# Suggest plugins
railway add --service web postgresql || true
railway add --service web redis || true

cat <<EOT

Remember to set environment variables in Railway (or via 'railway variables set'):
  BINANCE_API_KEY, BINANCE_API_SECRET
  CRYPTOCOMPARE_API_KEY, DEEPSEEK_API_KEY
  NEWS_API_KEY, HF_TOKEN

Configure commands per service in the Railway dashboard:
  web: streamlit run main.py --server.port 8080 --server.address 0.0.0.0
  inflow-worker: python workers/inflow_worker.py --loop
  analytics-worker: python workers/analytics_worker.py --loop
  news-worker: python workers/news_worker.py --loop
EOT
