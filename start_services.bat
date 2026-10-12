@echo off
cd /d D:\golden-trinity
echo Starting GoldenTrinity Python Services...

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

echo.
echo All services launched.
echo.
echo   8001 - macro_service
echo   8002 - risk_service
echo   8003 - execution_service
echo   8004 - signal_service
echo   8005 - xauusd_service
echo   9000 - dashboard (http://localhost:9000)
echo.
pause
