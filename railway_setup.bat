@echo off
setlocal ENABLEEXTENSIONS
set /p PROJECT_NAME="Railway project name: "

where railway >nul 2>nul
if %errorlevel% neq 0 (
  echo Install the Railway CLI first: https://railway.app/cli
  exit /b 1
)

echo Linking project...
railway init --project "%PROJECT_NAME%"

echo Creating services (safe to ignore 'already exists' notices)...
railway service:create web || echo web may already exist
railway service:create inflow-worker || echo inflow-worker may already exist
railway service:create analytics-worker || echo analytics-worker may already exist
railway service:create news-worker || echo news-worker may already exist

echo Suggesting plugins for the web service...
railway add --service web postgresql || echo postgresql plugin may already exist
railway add --service web redis || echo redis plugin may already exist

echo.
echo Remember to set environment variables (via dashboard or 'railway variables set'):
echo   BINANCE_API_KEY, BINANCE_API_SECRET
echo   CRYPTOCOMPARE_API_KEY, DEEPSEEK_API_KEY
echo   NEWS_API_KEY, HF_TOKEN
echo.
echo Configure commands per service in the Railway dashboard:
echo   web: streamlit run main.py --server.port 8080 --server.address 0.0.0.0
echo   inflow-worker: python workers/inflow_worker.py --loop
echo   analytics-worker: python workers/analytics_worker.py --loop
echo   news-worker: python workers/news_worker.py --loop
endlocal
