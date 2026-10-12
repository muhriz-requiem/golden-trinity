@echo off
title GoldenTrinity — Launcher
color 0A
cls

echo.
echo  ============================================================
echo   GOLDEN TRINITY — AI Gold Trading System
echo  ============================================================
echo.
echo  Checking Docker...

REM Check if Docker is running
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo  Docker is not running. Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo  Waiting for Docker to start (30 seconds)...
    timeout /t 30 /nobreak >nul
    echo  Checking Docker again...
    docker info >nul 2>&1
    if %errorlevel% neq 0 (
        echo.
        echo  [ERROR] Docker failed to start. Please open Docker Desktop
        echo  manually and wait for the whale icon to stop animating,
        echo  then double-click this icon again.
        echo.
        pause
        exit /b 1
    )
)
echo  Docker OK.
echo.

REM Start containers
echo  Starting Docker containers...
cd /d D:\golden-trinity
docker-compose up -d >nul 2>&1
echo  Containers OK.
echo.

REM Wait a moment for DB to be ready
timeout /t 3 /nobreak >nul

REM Start Python services
echo  Starting services...
start "GT-macro"     cmd /k "title GT-macro :8001 && D:\golden-trinity\venv\Scripts\uvicorn.exe macro_service:app --port 8001"
timeout /t 2 /nobreak >nul
start "GT-risk"      cmd /k "title GT-risk :8002 && D:\golden-trinity\venv\Scripts\uvicorn.exe risk_service:app --port 8002"
timeout /t 2 /nobreak >nul
start "GT-execution" cmd /k "title GT-execution :8003 && D:\golden-trinity\venv\Scripts\uvicorn.exe execution_service:app --port 8003"
timeout /t 2 /nobreak >nul
start "GT-signal"    cmd /k "title GT-signal :8004 && D:\golden-trinity\venv\Scripts\uvicorn.exe signal_service:app --port 8004"
timeout /t 2 /nobreak >nul
start "GT-xauusd"    cmd /k "title GT-xauusd :8005 && D:\golden-trinity\venv\Scripts\uvicorn.exe xauusd_service:app --port 8005"
timeout /t 2 /nobreak >nul
start "GT-health"    cmd /k "title GT-health && D:\golden-trinity\venv\Scripts\python.exe health_monitor.py"
timeout /t 2 /nobreak >nul
start "GT-dashboard" cmd /k "title GT-dashboard :9000 && D:\golden-trinity\venv\Scripts\python.exe dashboard_server.py"

echo  All services launched.
echo.
echo  Waiting for services to come online...
timeout /t 10 /nobreak >nul

REM Health check
echo  Health check:
curl -s http://localhost:8001/api/health >nul 2>&1 && echo    :8001 macro      OK || echo    :8001 macro      STARTING...
curl -s http://localhost:8002/api/health >nul 2>&1 && echo    :8002 risk       OK || echo    :8002 risk       STARTING...
curl -s http://localhost:8003/api/health >nul 2>&1 && echo    :8003 execution  OK || echo    :8003 execution  STARTING...
curl -s http://localhost:8004/api/health >nul 2>&1 && echo    :8004 signal     OK || echo    :8004 signal     STARTING...
curl -s http://localhost:8005/api/health >nul 2>&1 && echo    :8005 xauusd     OK || echo    :8005 xauusd     STARTING...

echo.
echo  Dashboard: http://localhost:9000
echo.
echo  Opening dashboard in browser...
timeout /t 3 /nobreak >nul
start http://localhost:9000

echo.
echo  ============================================================
echo   GoldenTrinity is running.
echo   You can close this window — services stay running.
echo  ============================================================
echo.
timeout /t 5 /nobreak >nul
exit
