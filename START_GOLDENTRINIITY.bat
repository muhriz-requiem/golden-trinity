@echo off
title GoldenTrinity — Startup
color 0A
cls

echo.
echo  ================================================
echo   GOLDENTRINIITY — STARTUP
echo  ================================================
echo.

cd /d D:\golden-trinity

echo  [1/3] Starting Docker containers...
docker-compose up -d
timeout /t 12 /nobreak >nul

echo  [2/3] Starting Python services...
start "GT-macro"     cmd /k "cd /d D:\golden-trinity && venv\Scripts\uvicorn.exe macro_service:app --port 8001"
timeout /t 2 /nobreak >nul
start "GT-risk"      cmd /k "cd /d D:\golden-trinity && venv\Scripts\uvicorn.exe risk_service:app --port 8002"
timeout /t 2 /nobreak >nul
start "GT-execution" cmd /k "cd /d D:\golden-trinity && venv\Scripts\uvicorn.exe execution_service:app --port 8003"
timeout /t 2 /nobreak >nul
start "GT-signal"    cmd /k "cd /d D:\golden-trinity && venv\Scripts\uvicorn.exe signal_service:app --port 8004"
timeout /t 2 /nobreak >nul
start "GT-xauusd"    cmd /k "cd /d D:\golden-trinity && venv\Scripts\uvicorn.exe xauusd_service:app --port 8005"
timeout /t 2 /nobreak >nul
start "GT-health"    cmd /k "cd /d D:\golden-trinity && venv\Scripts\python.exe health_monitor.py"
timeout /t 2 /nobreak >nul
start "GT-dashboard" cmd /k "cd /d D:\golden-trinity && venv\Scripts\python.exe dashboard_server.py"

echo  [3/3] Waiting for services to start...
timeout /t 8 /nobreak >nul

echo  Opening dashboard...
start "" "http://localhost:9000"

echo.
echo  ================================================
echo   ALL DONE
echo.
echo   Dashboard : http://localhost:9000
echo   N8N       : http://localhost:5678
echo   Discord   : check #gt-health
echo.
echo   Minimize this window. Do NOT close it.
echo  ================================================
echo.
pause
