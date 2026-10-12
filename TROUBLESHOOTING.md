# GoldenTrinity — Service Troubleshooting Guide
## For Claude sessions working on this project

---

## IMPORTANT: How to Diagnose Before Acting

**Never replace a service file without reading the error first.**
The error tells you exactly what is wrong in 5 seconds.
Replacing the whole file without reading the error wastes time and risks introducing new bugs.

---

## Step 1 — Find the Error

When a service is reported as down, always do this first:

**Check if the service is running at all:**
```
netstat -ano | findstr ":8004"
```
If nothing is listening — the service simply isn't running. This is the most common cause.
**Fix: run `start_services.bat`** (see Step 3).

If something IS listening but health check fails — then there is an actual error to diagnose.
Look at the terminal window for that service (titled GT-macro, GT-risk, GT-execution, or GT-signal).
The error will be printed there in plain text.

If the window is closed, restart the service and watch the output:
```
cd .
venv\Scripts\uvicorn.exe signal_service:app --port 8004
```

---

## Step 2 — Why Services Stop Running

Services stop for these reasons — in order of frequency:

1. **PC went to sleep or was rebooted** — services don't survive sleep/restart unless autostart ran
2. **Training window was closed** — closing the overnight_train.py terminal can close other windows too
3. **Actual crash** — a Python exception killed the process (check the terminal for the error)
4. **Port conflict** — another process took the port

**If you just woke up or rebooted:** run `start_services.bat`. No further diagnosis needed.

---

## Step 3 — All Services Down (Most Common Fix)

Run this first before anything else:
```
start_services.bat
```
This restarts all 5 processes (macro, risk, execution, signal, health_monitor).
Wait 10 seconds, then verify all 4 are healthy:
```
Invoke-RestMethod http://localhost:8001/api/health
Invoke-RestMethod http://localhost:8002/api/health
Invoke-RestMethod http://localhost:8003/api/health
Invoke-RestMethod http://localhost:8004/api/health
```

**Auto-start is also configured:** `autostart.bat` is in your Windows Startup folder
(`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GoldenTrinity.bat`).
It runs automatically on every login — starts Docker containers, waits, then starts all 5 services.
If autostart didn't fire (e.g. Docker wasn't ready in time), just run `start_services.bat` manually.

---

## Step 4 — Specific Error Fixes

### "Address already in use" / port conflict
Another process is already on that port.
```
netstat -ano | findstr ":8004"
taskkill /PID <pid_number> /F
```
Then restart the service.

### "ModuleNotFoundError: No module named X"
A Python package is missing from the venv.
```
cd .
venv\Scripts\pip install <missing_module_name>
```
Then restart the service.

### "Connection refused" (Redis or DB)
Docker containers are down. Fix:
```
cd .
docker-compose up -d
```
Wait 10 seconds, then run `start_services.bat`.

### "ALPACA_KEY is None" or auth errors
The .env file is not being loaded. Verify you are running from .:
```
cd .
venv\Scripts\uvicorn.exe signal_service:app --port 8004
```
The working directory must be . for dotenv to find the .env file.

### Service starts but health check fails
```
Invoke-RestMethod http://localhost:8004/api/health
```
Read the error body — it will say exactly which dependency failed.

### "No module named 'torch'" in signal_service
Expected. signal_service detects this and uses rule-based fallback automatically.
Not an error — the service still runs correctly.

---

## Step 5 — Verify Fix Worked

After any fix, confirm the service is healthy:
```
Invoke-RestMethod http://localhost:8004/api/health | ConvertTo-Json
```
Expected response:
```json
{
  "status": "healthy",
  "service": "signal_service",
  "latency_ms": 1.5,
  "model": "rule-based-fallback",
  "buffer_size": 0
}
```
buffer_size will be 0 outside market hours (weekends, before 9:30 AM ET). That is normal.

---

## Service-Specific Notes

### signal_service (port 8004)
- Polls Alpaca every 5 seconds for GLD quote
- buffer_size climbs from 0 to 60 after market open (9:30 AM ET weekdays)
- model will say "rule-based-fallback" — this is correct and expected
- LSTM model at model\lstm_model.pt has negative Sharpe (-22) and is intentionally not loaded
- Only generates signals during OVERLAP (13:30–20:00 UTC) and NY_AFTERNOON sessions
- Signals outside market hours = 0, this is NOT a bug

### macro_service (port 8001)
- Fetches FRED data on demand (POST /api/macro/update) or every 15 min via n8n
- If FRED API returns empty data, regime stays as last known value (NEUTRAL by default)
- Manual trigger: Invoke-RestMethod http://localhost:8001/api/macro/update -Method POST

### risk_service (port 8002)
- Runs a portfolio monitor loop every 30 seconds in the background
- If gt:system_halted = "true" in Redis, ALL new trades are blocked
- To resume after a halt: Invoke-RestMethod http://localhost:8002/api/risk/resume -Method POST

### execution_service (port 8003)
- Connects to Alpaca paper API on startup to fetch portfolio NAV
- If Alpaca API is down, service still starts but orders will fail
- Check Alpaca status at https://status.alpaca.markets if orders are not placing

---

## What NOT to Do

- Do NOT replace signal_service.py (or any service file) without reading the error first
- Do NOT change ports — they are hardcoded across multiple services and n8n workflows
- Do NOT change trading parameters (stop-loss 0.5%, take-profit 0.75%, confidence gate 0.60/0.20)
  These are locked design decisions — see SYSTEM_STATE.md section 10
- Do NOT restart Docker containers unless services cannot connect to Redis or DB
  Restarting containers wipes in-memory state (open positions in Redis)
- Do NOT run pip install without activating the venv first
  Always use: venv\Scripts\pip install X (not the system pip)

---

## Quick Reference — What Each File Does

| File | Purpose | When to touch it |
|------|---------|-----------------|
| signal_service.py | GLD tick feed + signal generation | Only if signal logic needs changing |
| macro_service.py | FRED/NewsAPI/yfinance + regime | Only if macro data sources change |
| risk_service.py | Circuit breaker + pre-trade checks | Only if risk parameters change |
| execution_service.py | Alpaca order placement + exits | Only if broker changes |
| health_monitor.py | Discord health pings | Only if Discord webhooks change |
| .env | All API keys | When keys expire or OANDA added at 18 |
| docker-compose.yml | Container definitions | Only when adding new services |
| train_lstm.py | LSTM training | When retraining model |
| overnight_train.py | Multi-config overnight search | When running extended training |

---

## Signal Service — Current Known State (March 2026)

| Item | State |
|------|-------|
| Active signal method | Rule-based EMA 9/21 + Bollinger Bands |
| LSTM model | Trained but negative Sharpe — NOT loaded |
| Signal gate | P(UP) >= 0.60 AND P(DOWN) <= 0.20 for BUY |
| Trading hours | Weekdays 9:30 AM – 4:00 PM ET only |
| Tick poll interval | Every 5 seconds |
| Buffer needed before signal | 60 ticks (~5 minutes after market open) |

---

## SYSTEM_STATE.md

The file SYSTEM_STATE.md contains the complete system state as of March 15 2026.
Read it at the start of any session before making changes.
It contains every port, every webhook URL, every API key reference, every design decision.
