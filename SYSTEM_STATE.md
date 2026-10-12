# GoldenTrinity — Complete System State
**Built:** March 14–15, 2026 (1 day)
**Builder:** solo developer, United States
**Who built it:** a solo developer using AI coding agents; see README for the
division of labour between the developer and the agents.
**Phase:** Stage 7 — Paper Trading (begins Monday 9:30 AM ET)
**Last updated:** March 15, 2026

---

## QUICK REFERENCE — Most Important Things

| What | Where |
|------|-------|
| Project root | `` |
| Start everything | `start_services.bat` |
| Auto-start on login | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GoldenTrinity.bat` |
| Environment variables / API keys | `.env` |
| Docker containers | `docker ps` (4 containers) |
| n8n dashboard | http://localhost:5678 |
| Grafana dashboard | http://localhost:3000 |
| Paper trading dashboard | https://app.alpaca.markets/paper-trading |
| Discord server | GoldenTrinity (4 channels) |
| n8n API key | `<redacted - n8n public-api JWT, rotated>` |

---

## 1. FILE STRUCTURE — 

```

│
├── .env                        — All API keys and config (DO NOT SHARE)
├── docker-compose.yml          — Defines all 4 Docker containers
├── schema.sql                  — TimescaleDB table definitions (7 tables)
│
├── macro_service.py            — FastAPI service, port 8001
├── risk_service.py             — FastAPI service, port 8002
├── execution_service.py        — FastAPI service, port 8003
├── signal_service.py           — FastAPI service, port 8004
├── health_monitor.py           — Standalone health checker, posts to Discord
│
├── download_data.py            — Downloads GLD 1-min bars from Alpaca
├── train_lstm.py               — LSTM training script (v3, walk-forward)
│
├── start_services.bat          — Starts all 5 Python processes
├── autostart.bat               — Starts Docker + all services on login
├── install_deps.bat            — Installs Python packages (run once)
├── install_torch.bat           — Installs PyTorch to D drive (run once)
│
├── data\
│   └── GLD_1min.csv            — 132,044 bars, 2024-03-15 to 2026-03-13 (7.5 MB)
│
├── model\
│   ├── lstm_model.pt           — LSTM weights (205 KB) — NOTE: v3 Sharpe was negative,
│   │                             signal_service uses rule-based fallback until retrained
│   └── training_metrics.json   — Last training results
│
├── n8n_workflows\
│   ├── workflow_health_monitor.json      — Original (has parallel branch bug)
│   ├── health_monitor_fixed.json         — Fixed version (sequential)
│   ├── workflow_macro_ingestion.json     — Every 15 min macro fetch
│   ├── workflow_trade_notifier.json      — Post-fill Discord notification
│   ├── workflow_risk_alerts.json         — Risk event Discord notification
│   └── workflow_daily_report.json        — 5 PM UTC daily P&L report
│
└── venv\                       — Python 3.13 virtual environment
    └── Scripts\
        ├── uvicorn.exe         — WSGI server for all FastAPI services
        └── python.exe          — Python interpreter
```

---

## 2. PYTHON SERVICES — Ports and Roles

| Service | Port | File | Role | Health endpoint |
|---------|------|------|------|----------------|
| macro_service | 8001 | macro_service.py | Fetches FRED/NewsAPI/yfinance. Classifies regime. Writes to Redis. | GET /api/health |
| risk_service | 8002 | risk_service.py | Portfolio monitor loop (30s). Circuit breaker. Pre-trade checks. | GET /api/health |
| execution_service | 8003 | execution_service.py | Alpaca paper order placement. Trade lifecycle. Exit management. | GET /api/health |
| signal_service | 8004 | signal_service.py | GLD tick feed. EMA+Bollinger signals. LSTM inference (when model ready). | GET /api/health |
| health_monitor | none | health_monitor.py | Pings all 4 services every 5 min. Posts to Discord #gt-health. | standalone script |

**Important:** signal_service currently uses **rule-based fallback** (EMA 9/21 + Bollinger Bands), NOT the LSTM model. The LSTM weights in model/lstm_model.pt are from a failed training run (negative Sharpe). The service auto-loads LSTM if lstm_model.pt exists and has valid weights — retrain with train_lstm.py when ready.

**Start command for each service:**
```
venv\Scripts\uvicorn.exe <service_name>:app --port <port>
```
Working directory must be ``

**Start all at once:** double-click `start_services.bat`

---

## 3. DOCKER CONTAINERS

| Container name | Port | Image | Purpose |
|----------------|------|-------|---------|
| golden-trinity-timescaledb-1 | 5432 | timescale/timescaledb:latest-pg15 | All persistent data |
| golden-trinity-redis-1 | 6379 | redis:7-alpine | Regime state, tick cache, Redis Streams |
| golden-trinity-n8n-1 | 5678 | n8nio/n8n:latest | Workflow orchestration |
| golden-trinity-grafana-1 | 3000 | grafana/grafana:latest | Dashboard |

**Start containers:** `cd . && docker-compose up -d`
**Check containers:** `docker ps`
**Stop containers:** `docker-compose down` (from .)

Two orphan containers also exist (boring_murdock, compassionate_chebyshev) — these are leftover from an old standalone n8n run, safe to ignore, do not interfere.

**Docker data volumes (persisted on disk):**
- `golden-trinity_timescale_data` — all database data
- `golden-trinity_n8n_data` — all n8n workflows and credentials
- `golden-trinity_grafana_data` — Grafana dashboards

---

## 4. DATABASE — TimescaleDB

**Connection:**
- Host: localhost:5432
- Database: goldentrinity
- User: gt_user
- Password: in .env as DB_PASS

**Connect via Docker:** `docker exec -it golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity`

**7 Tables:**

| Table | Purpose | Current rows |
|-------|---------|-------------|
| macro_snapshots | Every macro ingestion (15 min) | 6 |
| signal_log | Every signal generated by signal_service | 0 (fills Monday) |
| position_log | Full trade lifecycle — entry to exit | 0 (fills Monday) |
| risk_alert_log | Every risk event fired | 0 |
| health_checks | Every n8n health ping | 0 |
| daily_reports | Daily P&L summary | 0 |
| cot_snapshots | Weekly CFTC COT data | 0 |

---

## 5. REDIS — Key Structure

**Connect:** `docker exec -it golden-trinity-redis-1 redis-cli`

| Redis Key | Value | Set by |
|-----------|-------|--------|
| gt:regime | "BULLISH" / "NEUTRAL" / "BEARISH" | macro_service |
| gt:regime_updated | ISO timestamp | macro_service |
| gt:macro_snapshot | JSON of full macro data | macro_service |
| gt:nav | Portfolio value in dollars | execution_service |
| gt:open_positions | JSON array of open trades | execution_service |
| gt:daily_pnl_pct | Float e.g. 0.012 | execution_service |
| gt:system_halted | "true" if halted | risk_service |
| gt:halt_reason | Reason string | risk_service |
| gt:signal:current | Latest GLD signal JSON | signal_service |
| gt:ticks:gld | Redis Stream of GLD ticks | signal_service |
| gt:collapse_alert | "true" if model collapsing | signal_service |

---

## 6. N8N WORKFLOWS

**Dashboard:** http://localhost:5678
**Account:** `<redacted - personal account>`
**API key:** see QUICK REFERENCE above

**5 Active Workflows:**

| Workflow ID | Name | Trigger | What it does |
|-------------|------|---------|-------------|
| P67Ec1Xo9tGNPXoh | GT — Health Monitor | Every 5 min | Pings all 4 services via host.docker.internal. Posts to #gt-health. Alerts #gt-alerts if any service down. |
| WihN7DIcMzEsXQ83 | GT — Macro Ingestion | Every 15 min | Calls POST /api/macro/update on macro_service. Posts regime change to #gt-reports. |
| fheBR3yPITeSIexZ | GT — Risk Alert Handler | Webhook POST /webhook/risk-alert | Receives risk events from risk_service. Posts to #gt-alerts. |
| gqB3lPGfU0n4X9t8 | GT — Trade Notifier | Webhook POST /webhook/trade-filled | Receives fills from execution_service. Posts to #gt-trades. |
| hKQ8QYgQHsD702Rn | GT — Daily P&L Report | 5 PM UTC weekdays | Queries execution_service and signal_service metrics. Posts to #gt-reports. |

**Known issue:** Health Monitor n8n workflow has a parallel branch bug (executes but errors). Fixed by health_monitor.py Python script running as a separate process instead. Both run concurrently — health_monitor.py is the reliable one.

**n8n webhook URLs (Python posts to these):**
- Trade filled: http://localhost:5678/webhook/trade-filled
- Risk alert: http://localhost:5678/webhook/risk-alert

---

## 7. DISCORD WEBHOOKS

**Server name:** GoldenTrinity
**4 channels and their webhook URLs:**

> **Redacted for publication.** All four webhook URLs were live credentials and have
> been replaced with placeholders. They were rotated before this repository was
> published.

| Channel | Purpose | Webhook URL |
|---------|---------|-------------|
| #gt-health | Service health every 5 min | `<discord-webhook-health>` |
| #gt-alerts | Circuit breakers, risk events @everyone | `<discord-webhook-alerts>` |
| #gt-trades | Every trade fill | `<discord-webhook-trades>` |
| #gt-reports | Daily P&L, macro regime changes, COT | `<discord-webhook-reports>` |

---

## 8. API KEYS AND CREDENTIALS

All stored in `.env`. Summary:

| Service | Key name in .env | Notes |
|---------|-----------------|-------|
| Alpaca paper trading | ALPACA_KEY, ALPACA_SECRET | Paper only. Base URL: https://paper-api.alpaca.markets |
| FRED (macro data) | FRED_API_KEY | Free, unlimited. Used for CPI, DFF, DXY, VIX, T10Y2Y |
| NewsAPI | NEWS_API_KEY | Free tier, 100 req/day. Batched to 4-hr windows (~48/day) |
| Discord webhooks | DISCORD_WEBHOOK_* | 4 webhooks, see section 7 |
| TimescaleDB | DB_HOST/PORT/NAME/USER/PASS | localhost:5432, db=goldentrinity, user=gt_user |
| Redis | REDIS_HOST/PORT | localhost:6379 |
| n8n | N8N_ENCRYPTION_KEY | Set in docker-compose.yml env |

**OANDA:** Skipped for paper trading. GLD ETF via Alpaca replaces XAU/USD spot. Add OANDA at 18 for live trading — only 4 .env changes needed.

---

## 9. MACRO DATA SOURCES

| Data | Source | FRED Series | Notes |
|------|--------|-------------|-------|
| CPI YoY % | FRED | CPIAUCSL | MUST compute YoY from 13 observations. Never use raw index (~315). |
| Fed Funds Rate | FRED | DFF | Latest observation |
| Real Rate | Computed | — | DFF minus CPI_YoY |
| Dollar Index | FRED | DTWEXBGS | Broad dollar index |
| VIX | FRED | VIXCLS | Broad fear index |
| 10Y-2Y Spread | FRED | T10Y2Y | Yield curve |
| GVZ | yfinance | ^GVZ | Gold volatility index. Fetched via yf.Ticker('^GVZ') |
| News Sentiment | NewsAPI | — | Keyword scoring, range -1 to +1 |
| COT data | CFTC.gov | — | n8n workflow, Fridays 4PM ET (not yet set up) |

**Current regime:** NEUTRAL
**Regime logic:** Rule-based scoring in macro_service.py using real rate, DXY, VIX, news sentiment. LightGBM classifier planned but not yet trained. Multipliers: BULLISH=1.2x, NEUTRAL=1.0x, BEARISH=0.5x.

---

## 10. TRADING PARAMETERS (DO NOT CHANGE THESE)

| Parameter | Value | Reason |
|-----------|-------|--------|
| Max single position | 2% NAV | Hard cap |
| Max total gold exposure | 35% NAV | Was 80% in v1.0 — concentration risk |
| Max mining stocks | 12% NAV sub-limit | Company-specific noise |
| Stop-loss | 0.5% adverse from entry | Hard stop, cannot be overridden |
| Take-profit | 0.75% above entry | 1.5:1 reward/risk |
| Time expiry | 4 hours | Close if neither target nor stop hit |
| BUY confidence gate | P(UP) >= 0.60 AND P(DOWN) <= 0.20 | Dual condition — both required |
| SELL confidence gate | P(DOWN) >= 0.60 AND P(UP) <= 0.20 | Dual condition — both required |
| Kelly multiplier | 0.5x permanently | Half-Kelly. Full Kelly causes catastrophic drawdowns. |
| Kelly lookback | 252 trades | ~1 year of trade history |
| Kelly cold-start | 0.25x full Kelly | Used for first 252 trades |
| Daily drawdown halt | -3% | Halts new entries only |
| Circuit breaker | >1.5% price move in 60s | Halts all + handles open positions |
| API latency limit | 200ms | Switch to market orders if exceeded |

---

## 11. SESSION SCHEDULE

GLD only trades during US equity market hours (9:30 AM – 4:00 PM ET). In paper mode, OANDA/XAU/USD is skipped so Asian and London sessions have no trading.

| Session | UTC | Multiplier | Assets | Paper mode |
|---------|-----|-----------|--------|------------|
| ASIAN | 00:00–07:00 | 0.6x | XAU/USD only | NO TRADING (no OANDA) |
| LONDON | 07:00–13:00 | 0.9x | XAU/USD only | NO TRADING (no OANDA) |
| OVERLAP | 13:00–17:00 | 1.0x | GLD + NEM + GOLD + AEM | ACTIVE |
| NY_AFTERNOON | 17:00–21:00 | 0.8x | GLD + NEM + GOLD + AEM | ACTIVE |
| CLOSED | 21:00–00:00 + weekends | none | — | NO TRADING |

Effective paper trading window: **13:30–20:00 UTC** (9:30 AM–4:00 PM ET) weekdays only.

---

## 12. SIGNAL FLOW (HOW A TRADE HAPPENS)

```
1. signal_service polls Alpaca every 5 seconds for GLD quote
2. Adds tick to 60-tick rolling buffer
3. Runs EMA 9/21 crossover + Bollinger Band deviation
4. Outputs P(UP), P(FLAT), P(DOWN) — must sum to 1.0
5. Checks dual confidence gate: P(UP)>=0.60 AND P(DOWN)<=0.20 for BUY
6. If passes: POST to execution_service /api/execution/order
7. execution_service calls risk_service /api/signals/validate (direct HTTP, not n8n)
8. If approved: checks spread < 0.05%, computes position size (Kelly x regime x session)
9. Places market order via Alpaca paper API
10. Receives fill confirmation
11. Logs to Redis (open_positions) and TimescaleDB (position_log)
12. Fires async POST to n8n /webhook/trade-filled (non-blocking)
13. n8n posts Discord card to #gt-trades
14. Position monitor loop (30s) watches for stop/target/expiry/regime change
```

**Critical rule:** n8n is NEVER in the execution path. It only receives notifications after fills. Python services call each other directly via localhost HTTP.

---

## 13. LSTM MODEL STATUS

| Item | Status |
|------|--------|
| Training data | GLD 1-min bars, 132,044 rows, 2024-03-15 to 2026-03-13 |
| Data file | data\GLD_1min.csv (7.5 MB) |
| Model file | model\lstm_model.pt (205 KB) |
| Last training | March 15, 2026 — v3 |
| Last Sharpe | -22.156 (negative — model NOT in use) |
| Active model | Rule-based EMA fallback |
| Why negative Sharpe | 1-minute bars are ~70% noise. Model predicted consistently wrong direction. |
| Fix plan | Retrain on 5-minute bars. Use Google Colab for free GPU. Revisit after 4-6 weeks of paper trading data. |
| Architecture | LSTM(input=4, hidden=64, layers=2) + Attention + FC(70, 3) → softmax |
| Input features | Normalised price, volume, EMA diff, Bollinger Band deviation |
| Extra features | Regime embedding, session one-hot (4), GVZ/VIX ratio |

**To retrain:** Download 5-min data (change `1Min` to `5Min` in download_data.py), then run train_lstm.py. Target Sharpe 1.5–2.0. Above 3.0 = overfitting.

---

## 14. PAPER TRADING GO-LIVE CRITERIA

All 5 must pass over a minimum 30-day period before switching to live money:

| # | Criterion | Target | Where to check |
|---|-----------|--------|----------------|
| 1 | Win rate | ≥ 50% over ≥ 200 trades | position_log table |
| 2 | Average slippage | ≤ 0.03% per trade | position_log.slippage_pct |
| 3 | Software halts | Zero unexpected errors | risk_alert_log, service logs |
| 4 | Model confidence collapse | ≤ 2 per week average | signal_log, #gt-alerts |
| 5 | Rolling 30-day Sharpe | ≥ 1.0 | daily_reports table |

If any criterion fails: extend 2 weeks, fix the failing component, reset the counter.

**Breakeven win rate with current setup:** 40% (stop 0.5% / take-profit 0.75% = 1.5:1 R:R). Profitable at 45%+.

---

## 15. GO-LIVE SWITCH (AT 18 OR CUSTODIAL ACCOUNT)

Only 4 changes in `.env`:

```
TRADING_MODE=paper        →  TRADING_MODE=live
ALPACA_BASE_URL=https://paper-api.alpaca.markets  →  https://api.alpaca.markets
OANDA_PRACTICE=true       →  OANDA_PRACTICE=false
OANDA_ACCOUNT_ID=         →  your live OANDA account ID
OANDA_API_KEY=            →  your live OANDA API key
```

Then restart all Python services. Zero code changes. Everything validated in paper is valid for live.

---

## 16. ACCOUNT PATHWAY

| Phase | When | What |
|-------|------|------|
| Paper trading | Now | Alpaca paper + no OANDA. Free. No age restriction. |
| Custodial (optional) | Before 18 | Parent opens IBKR Family (UGMA/UTMA). Replace Alpaca in execution_service.py with IBKR TWS API. |
| Own accounts | At 18 | Open Alpaca live + OANDA live. Change 4 env vars. Done. |

---

## 17. DAILY CHECKLIST (60 SECONDS)

1. Open Discord GoldenTrinity server
2. Check **#gt-alerts** — any overnight circuit breakers or risk events?
3. Check **#gt-reports** — yesterday's P&L summary (posts 5 PM UTC)
4. Open a terminal: `docker ps` — all 4 containers showing "Up"?

If a service is down: run `start_services.bat` to restart everything.
If Docker is down: open Docker Desktop, wait for it to start, then `docker-compose up -d` from ..

---

## 18. WEEKLY CHECKLIST (10 MINUTES)

- Are NEUTRAL regime trades performing better than BEARISH regime trades?
- Are high-confidence signals (P>=0.75) outperforming medium-confidence (0.60-0.74)?
- What is the stop-loss exit rate? If above 60%, entry signals need review.
- Is the rolling 30-day Sharpe trending up or down?
- SQL query to check: `SELECT exit_type, COUNT(*), AVG(pnl_pct) FROM position_log GROUP BY exit_type;`

---

## 19. UPGRADE TRIGGERS

| When | What to do |
|------|-----------|
| Want 24/7 uptime without keeping PC on | Oracle Cloud Always Free (cloud.oracle.com, no card). 2 AMD VMs. Copy .. Run docker-compose up -d. |
| LSTM Sharpe negative / below 1.5 | Retrain on 5-min bars via Google Colab (free GPU). Change `1Min` to `5Min` in download_data.py. |
| NewsAPI 100 req/day not enough | Switch to GNews API (also free 100/day). Update n8n Macro Ingestion workflow node. |
| yfinance GVZ unreliable | Switch to Alpha Vantage free tier. Update fetch_gvz() in macro_service.py. |
| Turn 18 | Open Alpaca live + OANDA live. Change 4 env vars. Done. |
| Parent opens IBKR custodial | Replace Alpaca+OANDA calls in execution_service.py with IBKR TWS API. All other services unchanged. |

---

## 20. TROUBLESHOOTING

**Services won't start / "port already in use":**
```
netstat -ano | findstr :8001
taskkill /PID <pid> /F
```

**Docker containers down after reboot:**
```
cd .
docker-compose up -d
```

**n8n workflows not running:**
- Go to http://localhost:5678
- Check all 5 workflows show green (Published/Active)
- n8n API: GET http://localhost:5678/api/v1/workflows (use API key from section 1)

**Discord not receiving messages:**
- Python health_monitor.py posts directly — check it's running (`tasklist | findstr python`)
- Test webhook directly: `Invoke-RestMethod <webhook_url> -Method POST -Body '{"content":"test"}' -ContentType "application/json"`

**Signal buffer stuck at 0:**
- Normal outside market hours (weekends, before 9:30 AM ET / 13:30 UTC)
- On a weekday during market hours: check signal_service log window for errors

**Redis connection refused:**
- `docker start golden-trinity-redis-1`

**Database connection refused:**
- `docker start golden-trinity-timescaledb-1`
- Wait 10 seconds then restart Python services

**Regime stuck at NEUTRAL:**
- Trigger manual macro update: `Invoke-RestMethod http://localhost:8001/api/macro/update -Method POST`

---

*This document reflects the exact state of GoldenTrinity as of March 15, 2026 — built in one day from scratch.*
