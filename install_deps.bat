@echo off
cd /d D:\golden-trinity
echo Installing Python packages...
venv\Scripts\pip install fastapi uvicorn httpx redis asyncpg python-dotenv requests yfinance lightgbm pandas numpy alpaca-py
echo.
echo Done! Press any key to close.
pause
