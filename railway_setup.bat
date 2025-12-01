@echo off
set /p PROJECT_NAME="Railway project name: "

where railway >nul 2>nul
if %errorlevel% neq 0 (
  echo Install the Railway CLI first: https://railway.app/cli
  exit /b 1
)

echo Linking project...
railway init --project %PROJECT_NAME%

railway service:create web
railway service:create inflow-worker
railway service:create analytics-worker
railway service:create news-worker

railway add --service web postgresql
railway add --service web redis

echo Remember to set API keys: BINANCE_API_KEY, BINANCE_API_SECRET, CRYPTOCOMPARE_API_KEY, DEEPSEEK_API_KEY, NEWS_API_KEY, HF_TOKEN
