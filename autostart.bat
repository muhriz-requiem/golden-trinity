@echo off
REM GoldenTrinity Auto-Start — runs on Windows login via Task Scheduler
REM Task: trigger=At logon, action=this file, start in=D:\golden-trinity

REM Wait for Windows and desktop to fully load
timeout /t 20 /nobreak

REM Start Docker Desktop if not running
tasklist /FI "IMAGENAME eq Docker Desktop.exe" 2>NUL | find /I /N "Docker Desktop.exe" >NUL
if errorlevel 1 (
    echo Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    timeout /t 30 /nobreak
)

REM Start Docker containers
cd /d D:\golden-trinity
docker-compose up -d

REM Wait for containers to be healthy
timeout /t 15 /nobreak

REM Start Python services in order (each gets 2s to bind its port)
start "GT-macro"     cmd /k "D:\golden-trinity\venv\Scripts\uvicorn.exe macro_service:app --port 8001"
timeout /t 2 /nobreak
start "GT-risk"      cmd /k "D:\golden-trinity\venv\Scripts\uvicorn.exe risk_service:app --port 8002"
timeout /t 2 /nobreak
start "GT-execution" cmd /k "D:\golden-trinity\venv\Scripts\uvicorn.exe execution_service:app --port 8003"
timeout /t 2 /nobreak
start "GT-signal"    cmd /k "D:\golden-trinity\venv\Scripts\uvicorn.exe signal_service:app --port 8004"
timeout /t 2 /nobreak
start "GT-xauusd"    cmd /k "D:\golden-trinity\venv\Scripts\uvicorn.exe xauusd_service:app --port 8005"
timeout /t 2 /nobreak
start "GT-health"    cmd /k "D:\golden-trinity\venv\Scripts\python.exe health_monitor.py"
timeout /t 2 /nobreak
start "GT-dashboard" cmd /k "D:\golden-trinity\venv\Scripts\python.exe dashboard_server.py"

echo GoldenTrinity autostart complete.
echo Services: 8001 macro | 8002 risk | 8003 execution | 8004 signal | 8005 xauusd | 9000 dashboard
