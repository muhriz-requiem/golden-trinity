# GoldenTrinity — Master Reference Document
# READ THIS AT THE START OF EVERY NEW CLAUDE CHAT
# Last updated: March 17, 2026 (Session 3)
# Builder: solo developer, United States
# Built with AI coding agents - see README for the division of labour.

---

## WHO THIS IS

- **Who built it:** solo developer, United States, working with AI coding agents
- **No credit card, no SSN, no brokerage account** (under 18 at the time of writing)
- **At 18:** opens Alpaca live + OANDA live — changes 4 env vars — system goes live unchanged
- **PC:** Windows, project at `.`
- **Monitors via:** Discord (main alerts), dashboard at `http://localhost:9000`

---

## WHAT GOLDENTRINIITY IS

An AI-driven autonomous gold trading system. Three parallel layers:
1. **Macro Layer** — reads macroeconomic data, classifies BULLISH/NEUTRAL/BEARISH regime
2. **Execution Layer** — LSTM model generates signals, places paper trades automatically
3. **Risk Layer** — independent process, circuit breaker, unilateral halt authority

Paper trading now on GLD (Alpaca) + simulated XAU/USD (yfinance). Live at 18.

---

## HOW TO START EVERYTHING (after reboot)

### Step 1 — Make sure Docker Desktop is open
Should auto-start on login. If not, open from Start menu.
Wait for whale icon in taskbar to go solid ("Docker Desktop is running").

### Step 2 — Double-click:
```
START_GOLDENTRINIITY.bat
```

That's it. Opens automatically in ~30 seconds. Does:
1. `docker-compose up -d` (TimescaleDB, Redis, n8n, Grafana)
2. Waits 12s for containers
3. Launches all 7 terminal windows
4. Waits 8s
5. Opens `http://localhost:9000` (dashboard) in browser

### Overnight prep
- Plug laptop into power
- Windows Settings → System → Power → "When plugged in sleep after" → **Never**
- Screen can turn off, but sleep must be disabled

### Verify everything running
- Dashboard at `http://localhost:9000` — all dots green
- Discord `#gt-health` — ping within 5 minutes
- XAU/USD price showing ~$5,000 in header

---

## COMPLETE SERVICE MAP

| Service | Port | File | Role |
|---|---|---|---|
| macro_service | 8001 | macro_service.py | LightGBM regime classifier, FRED/NewsAPI data |
| risk_service | 8002 | risk_service.py | Circuit breaker, portfolio monitor, pre-trade checks |
| execution_service | 8003 | execution_service.py | GLD via Alpaca paper, EOD close, event calendar |
| signal_service | 8004 | signal_service.py | LSTM inference + rule-based EMA/Bollinger fallback |
| xauusd_service | 8005 | xauusd_service.py | Simulated XAU/USD + continuous LSTM retraining |
| health_monitor | — | health_monitor.py | Discord health pings every 5 min |
| dashboard_server | 9000 | dashboard_server.py | Serves dashboard + proxy API + WebSocket CMD |

### Docker containers
| Container | Port | Purpose |
|---|---|---|
| golden-trinity-timescaledb-1 | 5432 | All persistent data |
| golden-trinity-redis-1 | 6379 | Live cache, regime state, tick streams |
| golden-trinity-n8n-1 | 5678 | Workflow orchestration, Discord alerts |
| golden-trinity-grafana-1 | 3000 | Dashboard (optional) |

---

## DASHBOARD

**URL:** `http://localhost:9000` (NOT by opening dashboard.html directly — that causes CORS errors)
**Served by:** `dashboard_server.py` (FastAPI on port 9000)

### How it works
- `dashboard_server.py` is a FastAPI proxy — all browser API calls go to `/proxy/{service}/...`
- This avoids all CORS issues (same origin)
- Also has WebSocket `/ws/cmd` for real CMD execution in panels
- N8N health at `/proxy/n8n/health` (separate route, must be before generic proxy)

### Features
- Live XAU/USD price, regime badge, signal badge, positions, NAV, UTC+ET clock
- Green/red service dots with latency (200ms is normal — that's proxy overhead, not trade latency)
- 6 terminal panels: Macro, Risk, Stats, Execution, Signal, XAU/USD+Training
- Real CMD boxes — type commands, press Enter, output streams live via WebSocket
- Arrow Up/Down for command history per panel
- Text is selectable/copyable in all panels
- HALT / RESUME buttons on risk panel
- Retrain progress bars and go-live progress
- **"📄 PAPER TRADING — NO REAL MONEY"** badge in header

### About the 200ms latency shown
- Services respond internally in 1-2ms (real trade execution speed)
- 200ms = browser → proxy → service → back (display only, irrelevant to trading)
- Not a problem at all

---

## KEY FILE LOCATIONS

```

├── START_GOLDENTRINIITY.bat    ← MAIN STARTUP — double-click after reboot
├── start_services.bat          ← launches all 7 Python services (called by above)
├── dashboard.html              ← DO NOT open directly — use http://localhost:9000
├── dashboard_server.py         ← FastAPI proxy server for dashboard (port 9000)
├── close_all_positions.py      ← emergency position closer
├── event_calendar.py           ← macro event blocker (FOMC/CPI/NFP/PCE)
├── update_training_data.py     ← weekly CSV updater + live trade exporter
├── fxcm_adapter.py             ← NOT USED (FXCM banned for US residents)
│
├── macro_service.py            (port 8001)
├── risk_service.py             (port 8002)
├── execution_service.py        (port 8003)
├── signal_service.py           (port 8004)
├── xauusd_service.py           (port 8005)
├── health_monitor.py
│
├── train_lstm_v5.py            ← manual training run when needed
├── train_lstm_v4.py            ← old, buggy, do not use
├── overnight_train.py          ← old, buggy, do not use
│
├── data\
│   ├── GLD_1min.csv            (132k bars, 2024–2026)
│   ├── GLD_5min.csv            (37k bars, 2024–2026)
│   ├── XAUUSD_5min.csv         (live bars logged by xauusd_service, grows daily)
│   └── GLD_live_trades.csv     (paper trade outcomes for training)
│
├── model\
│   ├── lstm_model.pt           (active model — loaded by signal_service)
│   ├── lstm_model_new.pt       (temp file during atomic hot-swap, then renamed)
│   └── training_metrics_v5.json
│
├── n8n_workflows\              ← workflow JSON backups
│   ├── health_monitor_v2.json
│   ├── daily_report_v2.json
│   ├── trade_notifier_v2.json
│   └── macro_ingestion_v2.json
│
├── GOLDENTRINIITY_MASTER_REFERENCE.md  ← THIS FILE
├── CLAUDE_session_learnings.md
├── .env                        (ALL API KEYS — never share)
└── docker-compose.yml
```

---

## N8N WORKFLOWS (6 total, all active)

**Dashboard:** `http://localhost:5678`
**Login:** `<redacted - personal account>`

| ID | Name | Trigger | Status |
|---|---|---|---|
| P67Ec1Xo9tGNPXoh | GT — Health Monitor | Every 5 min | ✅ Updated session 3 |
| WihN7DIcMzEsXQ83 | GT — Macro Ingestion | Every 15 min | ✅ Updated session 3 |
| fheBR3yPITeSIexZ | GT — Risk Alert Handler | Webhook POST | ✅ Updated session 3 |
| gqB3lPGfU0n4X9t8 | GT — Trade Notifier | Webhook POST | ✅ Updated session 3 |
| hKQ8QYgQHsD702Rn | GT — Daily P&L Report | 4 PM ET weekdays | ✅ Updated session 3 |
| 4Xu2QglQm00c3JvT | GT — COT Ingestion | Fri 4:30 PM ET | ✅ NEW session 3 (activate manually in UI if needed) |

### What each workflow does (updated)

**Health Monitor** — pings all 5 services (now includes xauusd :8005). Only posts to `#gt-alerts @everyone` when a service is DOWN. Posts to `#gt-health` when all healthy.

**Macro Ingestion** — triggers macro_service every 15 min. Posts to `#gt-alerts` when regime changes to BULLISH or BEARISH (not NEUTRAL). Shows real rate, DXY, VIX, GVZ/VIX, news.

**Risk Alert Handler** — receives POST from risk_service after any risk event. Routes critical alerts (CIRCUIT_BREAKER, DRAWDOWN_LIMIT, MODEL_CONFIDENCE_COLLAPSE) to `#gt-alerts @everyone`.

**Trade Notifier** — receives POST from execution_service after every fill. Posts trade card to `#gt-trades`. Now correctly labeled PAPER/SIMULATED/SELL→DUST.

**Daily P&L Report** — fires at 4 PM ET weekdays. Posts to `#gt-reports` with: P&L, win rate, signal stats, LSTM training status, go-live progress toward 200 trades.

**COT Ingestion** (NEW) — fires Fridays at 4:30 PM ET. Downloads CFTC gold COT data, parses managed money and commercial positions, posts crowding signal to `#gt-reports`. Flags CROWDED_LONG (>200k net) as elevated reversal risk.

### Webhook URLs
```
POST http://localhost:5678/webhook/trade-filled   ← execution_service fires after fill
POST http://localhost:5678/webhook/risk-alert     ← risk_service fires after event
```

**CRITICAL:** n8n is NEVER in the execution path. Python services call each other directly.

---

## TRADING PARAMETERS (LOCKED)

| Parameter | Value |
|---|---|
| Max single position | 2% NAV |
| Max gold exposure | 35% NAV |
| Max mining stocks | 12% NAV sub-limit |
| Stop-loss | 0.5% adverse from entry |
| Take-profit | 0.75% above entry (1.5:1 R:R) |
| Time expiry | 4 hours |
| BUY gate | P(UP) ≥ 0.60 AND P(DOWN) ≤ 0.20 |
| SELL gate | P(DOWN) ≥ 0.60 AND P(UP) ≤ 0.20 |
| Kelly multiplier | 0.5× permanently (half-Kelly) |
| Kelly lookback | 252 trades |
| Kelly cold-start | Fixed 5% fraction (first 252 trades) |
| Daily drawdown halt | -3% |
| Circuit breaker | >1.5% move in 60 seconds |
| Spread limit | <0.05% of price |
| Regime BULLISH | 1.2× size |
| Regime NEUTRAL | 1.0× size |
| Regime BEARISH | 0.5× size |
| Session Asian | 0.6× |
| Session London | 0.9× |
| Session Overlap | 1.0× |
| Session NY Afternoon | 0.8× |
| EOD forced close | 3:50 PM ET (19:50 UTC) — GLD only |
| Min position | $500 floor |
| Fractional trading | ON (Alpaca setting) |

---

## XAUUSD SERVICE (port 8005)

**Why it exists:** All US forex brokers require SSN + age 18. No legal workaround for a 15-year-old US resident. Built a simulated broker instead.

**Price feed:**
- Primary: `GC=F` gold futures via yfinance — 23h/day weekdays
- Fallback: `GLD` ETF × 10.87 ratio
- Sanity check: rejects prices outside $1,500–$8,000/oz
- Gold price as of March 2026: ~$5,000/oz (major rally from ~$2,000 in 2024)

**Simulated fills:**
- BUY at ask (mid + 0.015%), SELL at bid (mid - 0.015%), total spread 0.03%
- Full stop-loss, take-profit, time expiry, regime change exits
- Real SELL signals work (no DUST proxy needed unlike GLD)
- All 4 sessions active (24/5 — no EOD close needed)

**Continuous training:**
- Collects 5-min bars to `data/XAUUSD_5min.csv` every 5 min
- After 50 new bars (~4 hours), fires training cycle
- Retrains every 6 hours using historical + live bars combined
- Walk-forward validation (v5 logic — no data leakage)
- Atomic model swap: writes `lstm_model_new.pt` then renames to `lstm_model.pt`
- Deploys new model only if avg Sharpe > 0.3
- Posts results to Discord `#gt-reports`

---

## LSTM MODEL

**Architecture:** LSTM + Attention, 2 layers, hidden=64, input_size=4
**Inputs:** 60-tick window (price, volume, EMA diff, Bollinger) + session encoding + regime embedding
**Output:** [P(UP), P(FLAT), P(DOWN)] summing to 1.0
**Active signal:** Rule-based EMA/Bollinger fallback (LSTM file is .bak — was broken)
**Continuous training:** xauusd_service retrains every 6h automatically

### Training version history
| Version | Result | Root cause |
|---|---|---|
| v1 (train_lstm.py) | Sharpe -22 | 1-min bars too noisy |
| overnight v1+v2 | Sharpe 0.0 | Sim gate too tight (0.50/0.25), zero trades fired |
| v4 | FAKE Sharpe 14-54 | Data leakage + missing `if __name__ == "__main__"` |
| v5 (current) | Honest, avg Sharpe metric | All bugs fixed |
| continuous (xauusd_service) | Runs every 6h | Streamlined v5 logic |

### v5 fixes (all critical)
1. Threshold computed from training slice only (not full dataset)
2. Purge gap = forward_bars between train end and val start
3. Annualisation: 252×78 for 5-min, 252×390 for 1-min
4. Early stopping: 15 epochs without improvement
5. Overfit guard raised to 4.0
6. Real metric: AVERAGE across 3 folds, not max
7. Missing `if __name__ == "__main__": main()` was causing silent exit

---

## SIGNAL FLOW

```
signal_service polls GLD price every 5s
→ Checks quote timestamp — if >60s old, market closed, skip
→ Builds 60-tick buffer
→ Generates P(UP)/P(FLAT)/P(DOWN) via LSTM or rule-based
→ Checks dual gate: P(UP)≥0.60 AND P(DOWN)≤0.20
→ If passes → POST execution_service /api/execution/order
  → Check EOD window (3:50 PM ET) → reject if yes
  → Check event calendar (FOMC/CPI/NFP/PCE) → reject if blocked
  → Check session (OVERLAP or NY_AFTERNOON) → reject if closed
  → POST risk_service /api/signals/validate
    → Check daily drawdown < -3%
    → Check total exposure < 35% NAV
    → Check spread < 0.05%
    → Returns {approved: bool}
  → If approved → Alpaca quote → spread check
  → Calculate position size (Kelly formula)
  → Place market order via Alpaca paper
  → Confirm fill → save to Redis + TimescaleDB
  → Fire async POST to n8n /webhook/trade-filled (non-blocking)
```

**n8n is NEVER in the execution path.**

---

## MACRO LAYER

| Input | Source | Notes |
|---|---|---|
| CPI YoY% | FRED CPIAUCSL (13 obs) | Compute YoY — raw value ~315, NOT a % |
| Fed Funds Rate | FRED DFF | Latest observation |
| Real Rate | DFF - CPI_YoY | Negative = BULLISH for gold |
| DXY | FRED DTWEXBGS | Inverse correlation with gold |
| VIX | FRED VIXCLS | High = risk-off = often BULLISH |
| 10Y-2Y Spread | FRED T10Y2Y | Inversion precedes gold rallies |
| GVZ | yfinance ^GVZ | GVZ/VIX ratio = gold-specific fear |
| News sentiment | NewsAPI (100/day, 4hr batches) | ~48 calls/day |
| COT Managed Money | CFTC free CSV (Fridays) | Crowded longs = reversal risk |
| COT Commercial | CFTC free CSV | Producers hedging = expect price fall |

**Regime rules:**
- BULLISH (1.2×): falling real rates, weakening DXY, elevated VIX, MM < 50th pct
- NEUTRAL (1.0×): no strong signal
- BEARISH (0.5×): rising real rates, strengthening DXY, falling VIX, MM > 80th pct
- COT crowding cap: MM ≥ 80th pct → BULLISH capped at 1.0×

---

## RISK LAYER

**Tier 1 — Per-trade:** 2% NAV cap, spread check, dual gate, exposure headroom, drawdown check
**Tier 2 — Portfolio (30s):** ≤35% gold, ≤12% mining, -3% drawdown halt
**Tier 3 — Circuit breaker:** >1.5% in 60s, API latency >200ms, model confidence collapse

**Circuit breaker open positions:**
- Adverse → close at market immediately
- Favourable → 0.3% trailing stop, let it run

---

## EVENT CALENDAR (event_calendar.py)

**HIGH impact — BLOCK 30 min before / 15 min after:**
FOMC (8/year, 2 PM ET), CPI (monthly ~13th, 8:30 AM ET), NFP (first Friday, 8:30 AM ET), PCE (last Friday, 8:30 AM ET)

**MEDIUM impact — REDUCE size 50%:**
PPI (day after CPI)

All 2026 dates hardcoded. Test: `python event_calendar.py`

---

## OVERNIGHT GAP PROTECTION

GLD only trades 9:30 AM–4:00 PM ET. Stops can't protect overnight gaps.
**Fix:** EOD forced close at 3:50 PM ET every weekday — all GLD positions closed.
XAU/USD (xauusd_service) has no EOD close — trades 24/5.

**Manual close:** `python close_all_positions.py --force`

---

## PAPER TRADING VALIDATION

**Minimum:** 30 days, 200 completed trades

**Go-live criteria (ALL 5 must pass):**
| # | Criterion | Required |
|---|---|---|
| 1 | Win rate | ≥ 50% over ≥ 200 trades |
| 2 | Avg slippage | ≤ 0.03% per trade |
| 3 | Software halts | Zero from code errors |
| 4 | Model confidence collapse | ≤ 2/week average |
| 5 | Rolling 30-day Sharpe | ≥ 1.0 |

**Status as of March 17, 2026:**
- 23 closed GLD trades from Day 1 (73.9% win rate, all TAKE_PROFIT)
- Need ~177 more trades, ~29 more days

**Daily routine (60 seconds):**
1. `#gt-alerts` — any circuit breakers overnight?
2. `#gt-reports` — daily P&L summary
3. Dashboard — all dots green?

---

## DATABASE

**Connect:** `docker exec -it golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity`

**Useful queries:**
```sql
-- Today's closed trades
SELECT asset, direction, fill_price, exit_price, pnl_pct, exit_type
FROM position_log WHERE DATE(closed_at) = CURRENT_DATE AND pnl_pct IS NOT NULL;

-- Win rate all time
SELECT COUNT(*) trades,
       ROUND(AVG(CASE WHEN pnl_pct > 0 THEN 1.0 ELSE 0.0 END)*100,1) win_pct,
       ROUND(AVG(pnl_pct)*100,4) avg_pnl
FROM position_log WHERE pnl_pct IS NOT NULL;

-- Open positions
SELECT asset, direction, fill_price, stop_loss, take_profit, expiry_at
FROM position_log WHERE closed_at IS NULL;
```

---

## REDIS KEY REFERENCE

| Key | Value | Set by |
|---|---|---|
| gt:regime | BULLISH/NEUTRAL/BEARISH | macro_service |
| gt:nav | Portfolio value ($) | execution_service |
| gt:open_positions | JSON array GLD positions | execution_service |
| gt:system_halted | "true" if halted | risk_service |
| gt:signal:current | Latest GLD signal JSON | signal_service |
| gt:ticks:gld | Redis Stream — GLD ticks | signal_service |
| gt:xauusd_positions | JSON array XAU/USD positions | xauusd_service |
| gt:xauusd_price | Latest XAU/USD price | xauusd_service |
| gt:ticks:xauusd | Redis Stream — XAU/USD ticks | xauusd_service |

---

## ALPACA ACCOUNT SETTINGS

| Setting | Value |
|---|---|
| Allow PTP Entry | OFF |
| Max Margin Multiplier | 1 (no margin) |
| Day Trade Buying Power Check | Block on Entry |
| Shorting | Disabled |
| Options Level | Leave as-is |
| Fractional Trading | ON |

---

## WHAT DOESN'T WORK / DEAD ENDS

| Thing | Status | Reason |
|---|---|---|
| Oracle Cloud | BLOCKED | Requires credit card |
| Fly.io | BLOCKED | Requires credit card for deployment |
| OANDA US | BLOCKED | Requires SSN + age 18 |
| FXCM | BLOCKED | Banned from serving US residents since 2017 |
| Forex.com | Limited | 90 days only |
| All US forex brokers | BLOCKED | NFA/CFTC requires SSN |
| Kafka | NOT USED | Replaced by Redis Streams |
| Slack | NOT USED | Free tier deletes messages. Discord keeps forever. |

---

## ENVIRONMENT VARIABLES (.env)

```
TRADING_MODE=paper
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_KEY=<saved>
ALPACA_SECRET=<saved>
FRED_API_KEY=<saved>
NEWS_API_KEY=<saved>
DISCORD_WEBHOOK_ALERTS=<saved>    ← circuit breakers, regime changes, service down
DISCORD_WEBHOOK_TRADES=<saved>    ← every trade fill
DISCORD_WEBHOOK_REPORTS=<saved>   ← daily report, COT, training results
DISCORD_WEBHOOK_HEALTH=<saved>    ← health pings every 5 min
DB_HOST=localhost
DB_PORT=5432
DB_NAME=goldentrinity
DB_USER=gt_user
DB_PASS=<saved>
REDIS_HOST=localhost
REDIS_PORT=6379
N8N_ENCRYPTION_KEY=<saved>
N8N_WEBHOOK_TRADE_FILLED=http://localhost:5678/webhook/trade-filled
N8N_WEBHOOK_RISK_ALERT=http://localhost:5678/webhook/risk-alert
FXCM_API_TOKEN=           ← blank (FXCM blocked for US residents)
FXCM_DEMO=true
```

### Go-live at 18 (change ONLY these 4 lines)
```
TRADING_MODE=live
ALPACA_BASE_URL=https://api.alpaca.markets
OANDA_PRACTICE=false
OANDA_ACCOUNT_ID=<live id>    + update OANDA_API_KEY
```
Zero code changes. Restart services. Done.

---

## COMMON COMMANDS

```bash
# Start everything (after reboot)
START_GOLDENTRINIITY.bat

# Dashboard
http://localhost:9000

# Check all service health
http://localhost:9000/status

# Check XAU/USD price
http://localhost:9000/proxy/xauusd/api/xauusd/price

# Check training status
http://localhost:9000/proxy/xauusd/api/xauusd/training_status

# Emergency — close all positions
cd .
venv\Scripts\python.exe close_all_positions.py --force

# Manual LSTM training
venv\Scripts\python.exe train_lstm_v5.py

# Update training data
venv\Scripts\python.exe update_training_data.py

# Event calendar check
venv\Scripts\python.exe event_calendar.py

# Check containers
docker ps

# Connect to database
docker exec -it golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity

# Check Redis
docker exec golden-trinity-redis-1 redis-cli GET gt:regime
docker exec golden-trinity-redis-1 redis-cli GET gt:open_positions
```

---

## UPGRADE ROADMAP

| When | What | How |
|---|---|---|
| Now | LSTM training | xauusd_service retrains every 6h automatically |
| Now | More paper trades | System runs itself |
| 200 trades + Sharpe ≥ 1.0 | Go-live evaluation | Check all 5 criteria |
| At 18 | OANDA live + Alpaca live | Change 4 .env lines, restart |
| LSTM Sharpe plateaus | Temporal Fusion Transformer | Rebuild signal_service model |
| Credit card available | Fly.io or Oracle Cloud | 24/7 hosting |

---

## PENDING ITEMS FOR NEXT SESSION

1. Check LSTM v5 results (`model/training_metrics_v5.json`) — did any config hit avg Sharpe > 1.0?
2. Check continuous training results from overnight — Discord `#gt-reports`
3. Manually activate COT workflow if still inactive: `http://localhost:5678` → GT — COT Ingestion → toggle Active
4. Close stale Day 1 positions: `python close_all_positions.py --force`
5. Rotate API keys (Alpaca, FRED, NewsAPI, Discord webhooks)
6. Fix MODEL_CONFIDENCE_COLLAPSE cosmetic logging (fires too often, not a real alert)
7. Accumulate paper trades — need ~177 more for go-live evaluation
8. Consider downloading more recent GLD data: `python update_training_data.py`

---

## FOR CLAUDE — HOW TO HELP

1. Read this file at start of every session
2. Also read `CLAUDE_session_learnings.md` for session-specific bugs/fixes
3. Never re-open locked decisions (n8n exec path, 35% exposure, half-Kelly, etc.)
4. Default to free tools — flag upgrades clearly
5. DB name is `goldentrinity` (NOT goldentriniity)
6. Dashboard is at `http://localhost:9000` — NOT by opening dashboard.html directly
7. Dashboard server (dashboard_server.py) is the proxy — all API calls go through port 9000
8. PowerShell MCP tool times out on torch imports — use Start-Process for heavy scripts
9. Use semicolons not && in PowerShell commands
10. Gold price as of March 2026 is ~$5,000/oz (major rally — was ~$2,000 in 2024)
11. 200ms dashboard latency is normal — proxy overhead, not trade execution speed
12. n8n API can update workflows via PUT with full workflow JSON object fetched first
13. n8n healthz returns `{"status":"ok"}` — check `status === "ok"` not `ok` property
14. Dashboard n8n health route must be defined BEFORE generic `/proxy/{service}/{path}` route
15. COT workflow ID: 4Xu2QglQm00c3JvT — may need manual activation in n8n UI
16. When the developer asks to build something, build it — give actual files, actual code
