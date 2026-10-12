# GoldenTrinity — Claude Context File
# READ THIS FILE AT THE START OF EVERY SESSION
# Last updated: March 22, 2026 (evening)
# Builder: solo developer, United States
# Built with AI coding agents - see README for the division of labour.

---

## WHAT THIS PROJECT IS

GoldenTrinity is an AI-driven gold trading system running as 5 Python FastAPI microservices
coordinated by n8n. It trades GLD (simulated, local fills) during US hours, and XAU/USD
simulated via xauusd_service during all sessions. All fills are local — no broker orders placed.

Stack: 100% free. Paper trading active. Portfolio starts at $100.
Phase: Stage 7 — Paper trading. Day 7. NOT yet at go-live criteria.
Trading mode: SIMULATED. Alpaca data API used for GLD quotes only. yfinance GC=F for XAU/USD.

---

## CRITICAL: CONTEXT FILES CAN BE STALE

The .md files in the Claude project context (/mnt/project/) are old snapshots.
ALWAYS read live files from  directly for current code state.
The /mnt/project/ files are for architecture reference only — never trust them for code.

---

## HOW TO START EVERYTHING (after reboot or sleep)

1. Open Docker Desktop — wait for the whale icon in the taskbar to go solid (not animated)
2. Double-click: START_GOLDENTRINIITY.bat
   (or start_services.bat — both work, START_GOLDENTRINIITY.bat also starts Docker)
3. Wait ~40 seconds
4. Check: http://localhost:9000/status — all 5 services should show ok:true

CRITICAL RULES:
- ALWAYS use the .bat files. NEVER launch with bare `python` or `uvicorn`.
  System Python lacks venv packages. Must use venv\Scripts\python.exe / venv\Scripts\uvicorn.exe.
- xauusd_service (port 8005) takes 45-60s to start — it imports torch + yfinance on startup.
  Don't panic if it's down for the first minute. Check again after 60s.
- Docker MUST be running before starting services. If Redis/DB connection fails → Docker is down.

After any restart, check for DB orphans before market opens:
  docker exec -it golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity
  SELECT id, asset, direction, fill_price, opened_at FROM position_log
  WHERE closed_at IS NULL ORDER BY opened_at DESC;
If orphans found: venv\Scripts\python.exe close_all_positions.py --force

---

## FILE STRUCTURE


├── .env                        — All API keys (NEVER expose this)
├── docker-compose.yml          — 4 Docker containers
├── schema.sql                  — 7 TimescaleDB tables + migration blocks
├── macro_service.py            — Port 8001
├── risk_service.py             — Port 8002
├── execution_service.py        — Port 8003
├── signal_service.py           — Port 8004
├── xauusd_service.py           — Port 8005
├── dashboard_server.py         — Port 9000
├── health_monitor.py           — Standalone (no port)
├── event_calendar.py           — Blocks trading ±30min around FOMC/CPI/NFP/PCE
├── training_report.py          — Appends to model/training_report.txt after every train run
├── train_lstm_v5.py            — Manual walk-forward LSTM training (6 configs)
├── update_training_data.py     — Weekly CSV updater + live trade exporter
├── close_all_positions.py      — Emergency position closer (GLD + XAU/USD)
├── start_services.bat          — Launches all 7 services with correct venv Python
├── START_GOLDENTRINIITY.bat    — Main startup: Docker + all services
├── start_remote_dashboard.bat  — Cloudflare tunnel for remote dashboard access
├── data\
│   ├── GLD_1min.csv            — 132,044 bars 2024-03-15 to 2026-03-13
│   ├── GLD_5min.csv            — 5-min bars (used for LSTM training)
│   ├── XAUUSD_5min.csv         — Live XAU/USD bars (written by xauusd_service, grows daily)
│   └── GLD_live_trades.csv     — Exported live trades (for LSTM retraining injection)
├── model\
│   ├── lstm_model.pt.bak       — All previous models were overfitted — renamed away
│   └── training_report.txt     — All training run reports (append-only log)
└── n8n_workflows\              — Importable n8n workflow JSONs

---

## PYTHON SERVICES — CURRENT STATE

| Service             | Port | Role                                                        |
|---------------------|------|-------------------------------------------------------------|
| macro_service.py    | 8001 | Regime classifier. FRED + NewsAPI + yfinance. 15min cycle.  |
| risk_service.py     | 8002 | Portfolio monitor (30s). Circuit breaker. Pre-trade checks. |
| execution_service.py| 8003 | Simulated GLD fills. Alpaca data quotes. NAV=$100.          |
| signal_service.py   | 8004 | Rule-based signals. Dual-routes GLD + XAU/USD.              |
| xauusd_service.py   | 8005 | Simulated XAU/USD. Continuous LSTM training every 6h.       |
| health_monitor.py   | none | Discord #gt-health pings every 5 min.                       |
| dashboard_server.py | 9000 | Dashboard at http://localhost:9000                          |

---

## DOCKER CONTAINERS

| Container                     | Port | Purpose              |
|-------------------------------|------|----------------------|
| golden-trinity-timescaledb-1  | 5432 | All persistent data  |
| golden-trinity-redis-1        | 6379 | State + tick streams |
| golden-trinity-n8n-1          | 5678 | Orchestration        |
| golden-trinity-grafana-1      | 3000 | Dashboard (unused)   |

Two orphan containers (boring_murdock, compassionate_chebyshev) — ignore, they don't interfere.

Start: cd . && docker-compose up -d
Check: docker ps
Connect to DB: docker exec -it golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity

---

## DATABASE

Host: localhost:5432 | DB: goldentrinity | User: gt_user | Pass: in .env as DB_PASS

7 Tables:
  macro_snapshots  — Every 15-min macro fetch (16 columns incl. spx_20d_return, audusd_20d_return)
  signal_log       — Every signal generated
  position_log     — Full trade lifecycle. broker='simulated_gld' (GLD) or 'simulated' (XAU/USD)
  risk_alert_log   — Every risk event
  health_checks    — Every n8n health ping
  daily_reports    — Daily P&L summary
  cot_snapshots    — Weekly CFTC COT data

IMPORTANT: position_log.broker values:
  'simulated_gld'  = GLD trades (execution_service)
  'simulated'      = XAU/USD trades (xauusd_service)
  'alpaca_paper'   = old label (pre-Mar 21) — close_all_positions.py handles both

---

## TRADING RESULTS

Pre-reset results (archived):
  Mar 16: 29 trades, 75.9% win, +$237.20
  Mar 18: 3 trades, 0.0% win, -$26.00  (collapse halt day)
  Mar 19: 215 trades, 51.6% win, +$420.57
  All-time: 247 trades, 55.1% win, +$631.77

Current portfolio (reset Mar 21, fresh start):
  NAV: $100.00 | Starting fresh | Go-live 30-day counter starts from reset

---

## SIGNAL ENGINE — CURRENT STATE (as of Mar 22 evening)

5 indicators, all must score to fire:

  1. EMA 9/21 crossover     — trend direction
  2. 15-tick momentum       — price direction over 15 ticks
  3. Price vs 20-tick SMA   — above/below mean
  4. Acceleration           — momentum of momentum (5-tick windows)
  5. RSI-14 (NEW Mar 22)    — <35 = oversold confirms BUY, >65 = overbought confirms SELL

Score range: -5 to +5
Fire threshold: score ≥ +4 → BUY signal (p_up=0.70)
               score ≤ -4 → SELL signal (p_down=0.70)
(Was ≥3/≤-3 before Mar 22. Raised to reduce stop-loss exits from marginal signals.)

Regime-direction alignment filter (NEW Mar 22):
  BULLISH regime + SELL signal → DROPPED (don't short into bullish macro)
  BEARISH regime + BUY signal  → DROPPED (don't go long into bearish macro)
  NEUTRAL regime               → both directions accepted

---

## SESSION SCHEDULE + MULTIPLIERS

GLD: weekdays 13:30-20:00 UTC (9:30 AM-4:00 PM ET), forced EOD close at 19:50 UTC
XAU/USD: weekdays ~00:00-21:00 UTC (23h/day via yfinance GC=F)

  00:00-07:00 UTC  Asian session     XAU/USD only   0.6x size
  07:00-13:30 UTC  London session    XAU/USD only   0.3x size  ← reduced Mar 22 (low win rate)
  13:30-20:00 UTC  Overlap+NY        GLD + XAU/USD  1.0x / 0.8x
  20:00-21:00 UTC  Post-NY           XAU/USD only   0.8x
  21:00-00:00 UTC  CLOSED            No trading

Market open cool-down (NEW Mar 22): No new GLD entries 13:30-13:40 UTC (first 10 min of US open).
London reduced from 0.9x to 0.3x because Mar 19 data showed 43.4% win rate (below breakeven).

---

## MACRO LAYER (macro_service.py — port 8001)

Runs every 15 min via n8n. Writes regime to Redis gt:regime.

| Input               | Source   | Series/Ticker  | Regime score                              |
|---------------------|----------|----------------|-------------------------------------------|
| CPI YoY %           | FRED     | CPIAUCSL×13    | Feeds real rate (NEVER use raw index)     |
| Fed Funds Rate      | FRED     | DFF            | Feeds real rate                           |
| Real Rate (DFF-CPI) | Computed | —              | <0→+2, <1→+1, >2→-1, >3→-2 (dominates)  |
| DXY                 | FRED     | DTWEXBGS       | >2% below 12m mean→+1, above→-1          |
| VIX                 | FRED     | VIXCLS         | >25→+1 (fear), <15→-1 (risk-on)          |
| 10Y-2Y spread       | FRED     | T10Y2Y         | Stored, not scored                        |
| GVZ                 | yfinance | ^GVZ           | GVZ/VIX ratio stored                     |
| SPX 20d return      | yfinance | ^GSPC          | <-2%→+1 (risk-off), >+3%→-1             |
| AUD/USD 20d return  | yfinance | AUDUSD=X       | >+1%→+1 (confirm), <-1%→-1              |
| News sentiment      | NewsAPI  | —              | >0.3→+1, <-0.3→-1 (4hr batch ~48/day)   |
| COT managed money   | CFTC.gov | —              | ≥80th pct→caps BULLISH at NEUTRAL        |

Score ≥+2 = BULLISH (1.2x), ≤-2 = BEARISH (0.5x), else NEUTRAL (1.0x). Range: -6 to +6.
Manual trigger: Invoke-RestMethod http://localhost:8001/api/macro/update -Method POST

---

## RISK LAYER (risk_service.py — port 8002)

Three tiers running independently — cannot be blocked by execution:

Per-trade (/api/signals/validate — called directly by execution_service):
  halt check → daily drawdown check → spread <0.05% → dual confidence gate →
  combined GLD+XAU/USD 35% exposure check

Portfolio monitor (every 30s):
  combined GLD+XAU/USD exposure vs 35% → EXPOSURE_BREACH alert
  daily P&L vs -3% → DRAWDOWN_LIMIT halt

Circuit breaker (every 10s):
  >1.5% price move in 60s → CIRCUIT_BREAKER halt + open position handling

Collapse monitor (every 30s):
  gt:collapse_alert "true" for 2 consecutive checks (60s sustained) → MODEL_CONFIDENCE_COLLAPSE halt
  10-minute market-open grace period (13:30-13:40 UTC) — suppresses collapse detection at open

Resume after halt: Invoke-RestMethod http://localhost:8002/api/risk/resume -Method POST

---

## TRADING PARAMETERS (LOCKED — DO NOT CHANGE)

| Parameter              | Value                                          |
|------------------------|------------------------------------------------|
| Starting NAV           | $100 (simulated)                               |
| Max single position    | 2% NAV = $2                                    |
| Min position           | $1                                             |
| Max gold exposure      | 35% NAV combined (GLD + XAU/USD)               |
| Max XAU/USD positions  | 5 simultaneous hard cap                        |
| Stop-loss              | 0.5% adverse from entry                        |
| Take-profit            | 0.75% above entry (1.5:1 R:R)                  |
| Time expiry            | 4 hours                                        |
| BUY gate               | P(UP) ≥ 0.60 AND P(DOWN) ≤ 0.20               |
| SELL gate              | P(DOWN) ≥ 0.60 AND P(UP) ≤ 0.20               |
| Kelly multiplier       | 0.5x permanently                               |
| Kelly cold-start       | 5% fixed (first 252 trades)                    |
| Daily drawdown halt    | -3%                                            |
| Circuit breaker        | >1.5% move in 60s                              |
| Fill cooldown GLD      | 120s                                           |
| Fill cooldown XAU/USD  | 120s (separate timer)                          |
| EOD close GLD          | 19:50 UTC (3:50 PM ET)                         |
| SELL direction         | Real short (not DUST proxy)                    |

Regime multipliers: BULLISH=1.2x, NEUTRAL=1.0x, BEARISH=0.5x
Session multipliers: Asian=0.6x, London=0.3x, Overlap=1.0x, NY_Afternoon=0.8x

---

## REDIS KEY STRUCTURE

| Key                  | Value                          | Set by                     |
|----------------------|--------------------------------|----------------------------|
| gt:regime            | BULLISH / NEUTRAL / BEARISH    | macro_service              |
| gt:regime_updated    | ISO timestamp                  | macro_service              |
| gt:macro_snapshot    | JSON — full macro data         | macro_service              |
| gt:nav               | Portfolio value ($)            | execution_service+xauusd   |
| gt:open_positions    | JSON array — GLD positions     | execution_service          |
| gt:xauusd_positions  | JSON array — XAU/USD positions | xauusd_service             |
| gt:daily_pnl_pct     | Float — combined GLD+XAU/USD   | execution_service+xauusd   |
| gt:system_halted     | "true" if halted               | risk_service               |
| gt:halt_reason       | Reason string                  | risk_service               |
| gt:signal:current    | Latest signal JSON             | signal_service             |
| gt:ticks:gld         | Redis Stream — GLD ticks       | signal_service             |
| gt:ticks:xauusd      | Redis Stream — XAU/USD ticks   | xauusd_service             |
| gt:xauusd_price      | Latest XAU/USD price           | xauusd_service             |
| gt:collapse_alert    | "true" if model collapsing     | signal_service             |
| gt:last_reset_date   | YYYY-MM-DD of last daily reset | execution_service          |

---

## LSTM MODEL STATUS

Active model: DISABLED. rule-based-fallback running.
All previous models overfitted (fold Sharpes always monotone increasing: 0→2→5+).
Root cause: training data covers only one market regime (2024-2026 gold bull run).
Fix: accumulate 4-6 weeks of diverse live paper data, then retrain in mid-April.

Continuous training: xauusd_service retrains every 6h automatically.
Deployment guard: avg_sharpe > 0.3 AND < 2.0 AND no single fold > 2.0.

When a good model is trained in Colab:
  1. Copy lstm_model.pt to model\lstm_model.pt
  2. Open http://localhost:9000/proxy/signal/api/signal/reload_model
  3. Confirm response: {"model": "LSTM loaded"}
  Deploy ONLY if: avg Sharpe ≥ 1.0 AND folds are consistent (not monotone increasing).

Colab training: see CLAUDE_session_learnings.md for full instructions.

---

## GO-LIVE CRITERIA (30-day minimum from Mar 21 reset, all 5 must pass)

1. Win rate ≥ 50% over ≥ 200 completed trades
2. Avg slippage ≤ 0.03% per trade
3. Zero unexpected software halts
4. Model confidence collapse ≤ 2 per week average
5. Rolling 30-day Sharpe ≥ 1.0

Failure on any = 2-week extension + fix + reset counter.

---

## N8N

Dashboard: http://localhost:5678 | Login: `<redacted - personal account>`

5 Active Workflows:
| ID               | Name                    | Trigger              |
|------------------|-------------------------|----------------------|
| P67Ec1Xo9tGNPXoh | GT — Health Monitor     | Every 5 min          |
| WihN7DIcMzEsXQ83 | GT — Macro Ingestion    | Every 15 min         |
| fheBR3yPITeSIexZ | GT — Risk Alert Handler | Webhook /risk-alert  |
| gqB3lPGfU0n4X9t8 | GT — Trade Notifier     | Webhook /trade-filled|
| hKQ8QYgQHsD702Rn | GT — Daily P&L Report   | 5 PM UTC weekdays    |

n8n is NEVER in the execution path. Receives post-fill notifications only.
Python → n8n webhooks:
  POST http://localhost:5678/webhook/trade-filled
  POST http://localhost:5678/webhook/risk-alert

---

## DISCORD WEBHOOKS

> **Redacted for publication.** All four webhook URLs were live credentials and
> have been replaced with placeholders. They were rotated before publication.

| Channel     | URL                                     |
|-------------|-----------------------------------------|
| #gt-health  | `<discord-webhook-health>`              |
| #gt-alerts  | `<discord-webhook-alerts>`              |
| #gt-trades  | `<discord-webhook-trades>`              |
| #gt-reports | `<discord-webhook-reports>`             |

---

## WIN RATE IMPROVEMENTS (implemented Mar 22 evening)

Five changes made to increase win rate from ~55% baseline:

1. Regime-direction alignment (signal_service._send_signal)
   BULLISH+SELL → dropped. BEARISH+BUY → dropped. NEUTRAL → both allowed.

2. Score threshold raised from 3 to 4 (signal_service._rule_based_signal)
   Now requires 4/5 indicators to agree vs 3/4. Fewer trades, better quality.

3. RSI-14 added as 5th indicator (signal_service._rule_based_signal)
   Oversold RSI (<35) confirms BUY. Overbought RSI (>65) confirms SELL.
   Buying into oversold improves timing vs buying into extended moves.

4. London session size multiplier 0.9x → 0.3x (xauusd_service + execution_service)
   Mar 19 data: London had 43.4% win rate. Breakeven for 1.5:1 R:R is ~40%.
   Reduced to 0.3x to limit exposure in underperforming session.

5. 10-minute market open cool-down (execution_service.place_order)
   Blocks new GLD entries 13:30-13:40 UTC. First 10 min of US open are chaotic.

---

## REGEX EDITING LESSON (CRITICAL — from Mar 22)

When using PowerShell regex to replace part of a dict line like:
  SESSION_MULT = {"ASIAN": 0.6, "LONDON": 0.9, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}

The pattern "LONDON": 0.9, matches only up to the comma.
If you add a comment after the replacement, you WILL eat the rest of the line.
The resulting broken line looks like:
  SESSION_MULT = {"ASIAN": 0.6, "LONDON": 0.3,   # comment "OVERLAP": 1.0...}

This is a SyntaxError. xauusd_service will crash on import with no obvious message.

SAFE APPROACH for dict line edits: replace the ENTIRE dict value, not just one entry.
  $old = '"ASIAN": 0.6, "LONDON": 0.9, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}'
  $new = '"ASIAN": 0.6, "LONDON": 0.3, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}'

Always verify after any dict edit:
  venv\Scripts\python.exe -c "import py_compile; py_compile.compile('xauusd_service.py')"

---

## LIVE GO-LIVE SWITCH (at 18)

In .env: 4 changes
  TRADING_MODE=live
  ALPACA_BASE_URL=https://api.alpaca.markets
  OANDA_PRACTICE=false
  OANDA_ACCOUNT_ID=<live> + OANDA_API_KEY=<live>

In execution_service.py: 8 changes
  1. Add alpaca_post() helper
  2. place_order: replace simulated fill with alpaca_post("/v2/orders")
  3. _close_position: add alpaca_post sell
  4. lifespan: fetch NAV from alpaca_trading_get("/v2/account")
  5. _reset_daily_state: restore NAV from Alpaca
  6. BROKER_LABEL = 'alpaca_live'
  7. MIN_POSITION_DOLLARS = 500.0
  8. Remove STARTING_NAV constant

Zero other code changes. All signal/risk/macro logic identical.

---

## DAILY CHECKLIST (60 seconds)

1. Check Discord #gt-alerts — any overnight circuit breakers?
2. Check Discord #gt-reports — yesterday's P&L
3. docker ps — all 4 containers showing "Up"?
4. http://localhost:9000/status — all 5 services ok:true?

---

## HOW TO START A CLAUDE SESSION

Tell Claude: "Read CONTEXT_FOR_CLAUDE.md"
Then if working on a specific service: "Also read [service].py"
The /mnt/project/ context files are stale — always read live D:\ files.
