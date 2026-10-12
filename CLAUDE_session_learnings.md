# GoldenTrinity — Session Learnings Log
# Append-only. Each entry records lessons from a build/trading session.
# Never edit old entries. Add new ones at the bottom.

---

## 2026-03-14 — Architecture Audit

- v1.0 had n8n IN the execution path — caused 10-30ms latency per trade. CRITICAL FIX: n8n is now post-fill only.
- v1.0 used raw CPIAUCSL index (~315) as a percentage. Correct: compute YoY % from 13 observations.
- v1.0 single confidence gate allowed UP=61% DOWN=38% through. Correct: DUAL gate (both conditions required).
- v1.0 had zero exit logic — system accumulated positions indefinitely. Added 5 exit conditions.
- v1.0 used 80% max gold exposure. Correct: 35% max.
- Kafka replaced by Redis Streams (already in stack, Kafka is overkill).
- Discord chosen over Slack (Slack free tier deletes messages after 90 days).

---

## 2026-03-15 — Initial Build

- DB name is `goldentrinity` (NOT `goldentriniity` — typo fixed before first run).
- OANDA demo signup inaccessible — replaced with GLD ETF via Alpaca for paper phase.
- n8n was already running before Stage 2 — do not add it to docker-compose.yml again.
- Services must be started from . for dotenv to load .env.

---

## 2026-03-16 — Day 1 Paper Trading

- 29 trades, 75.9% win rate, +$237.20.
- EOD close at 19:50 UTC successfully closed all positions.
- Dashboard "Change" label was showing tick-to-tick GLD price delta (±$1), not portfolio P&L.
  Fixed to "Day P&L $" showing daily_pnl_pct × NAV in green/red.

---

## 2026-03-18 — Model Collapse + Bug Day

- MODEL_CONFIDENCE_COLLAPSE fired at 9:38 AM ET (3 minutes after market open).
- Root cause: rule-based collapse tracked ALL ticks, not just directional ones.
  Fix: collapse now tracks only directional ticks (score >= 2 or <= -2) for flip rate.
- System halted all day — 3 positions left open with Redis/DB desync.
- Fix: _sync_positions_from_db() added to lifespan — reloads unclosed DB positions into Redis.
- 3 orphaned positions manually closed. -$26.00.
- DB UPDATE bug: "ORDER BY LIMIT in UPDATE" is invalid PostgreSQL. Fixed to use subquery.
- Fill cooldown added (300s → later 120s) to prevent simultaneous positions at open.
- daily_pnl_pct never written to Redis after closes → drawdown halt was always off. Fixed.
- Date-aware daily P&L reset added — prevents stale P&L carrying over on restart.
- LSTM model fold Sharpes [0.857, 2.671, 8.414] — OVERFIT. Renamed to .bak.
- Collapse detection requires 2 consecutive 30s polls before halt (was immediate).
- 10-minute market-open grace period added to risk_service — no collapse halts for first 10 min.

---

## 2026-03-19 — XAU/USD Sessions Enabled + Exposure Bug

- XAU/USD trading enabled for all sessions via xauusd_service (port 8005).
- signal_service dual-routes: GLD (US hours) + XAU/USD (23h/day).
- When GLD market closed: signal_service reads gt:xauusd_price for synthetic ticks.
- Fill cooldown reduced 300s → 120s (GLD and XAU/USD, independent timers).

RESULTS (Mar 19):
  215 trades, 51.6% win rate, +$420.57
  Asian: 162 trades, 54.3% win, +$381.37
  London: 53 trades, 43.4% win, +$39.20 (weak — later reduced to 0.3x Mar 22)
  All-time total: 247 trades, 55.1%, +$631.77

BUGS FOUND (fixed in live files same session):
  - 35 positions open simultaneously = 69.2% NAV exposure (vs 35% limit).
    Cause: xauusd_signal only checked gt:xauusd_positions, ignored GLD positions.
    Fix: MAX_XAU_POSITIONS=5 hard cap. Combined exposure check (both position lists).
  - XAU/USD closes did not update gt:daily_pnl_pct or gt:nav in Redis.
    Dashboard Day P&L always showed $0 for XAU/USD days. Drawdown halt was blind.
    Fix: _close_xauusd_position() now has Redis P&L + NAV update block.

PYTHON LESSON:
  Python 3.13 does NOT allow backslash escapes inside f-string expressions.
  WRONG: f"msg: {val if val else \"default\"}"
  RIGHT: tmp = val if val else "default"; f"msg: {tmp}"

---

## 2026-03-21 — Context Sync + Alpaca Disconnected + SPX/AUD Macro Inputs

DISCOVERY: /mnt/project/ context files were stale. Live  files
  already had all Mar 19 fixes applied. Always read live files directly.

EXECUTION_SERVICE REWRITTEN TO v5 (simulated):
  - No Alpaca orders placed. Alpaca data API kept for GLD quotes only.
  - BUY fills at ask. SELL fills at bid (real short, no DUST proxy).
  - NAV = $100 (STARTING_NAV constant). MIN_POSITION_DOLLARS = $1.
  - broker label: 'simulated_gld'
  - _close_position no longer fires Alpaca sell — DB + Redis only.
  - All PnL calculations direction-aware.

PORTFOLIO RESET:
  Deleted all rows: position_log (295), signal_log (17,993), daily_reports.
  Redis reset: gt:nav=100, gt:daily_pnl_pct=0, open positions cleared.
  Previous +$631.77 / 247 trades archived in this log only.

NEW MACRO INPUTS (macro_service v3):
  SPX 20-day return (^GSPC): <-2%→+1 BULLISH, >+3%→-1 BEARISH
    Why: SPX direction captures orderly selloffs that VIX misses (low VIX + falling SPX still bullish gold).
  AUD/USD 20-day return (AUDUSD=X): >+1%→+1, <-1%→-1
    Why: Australia exports ~10% world gold. AUD confirms if gold move is real demand vs dollar weakness.
  Both added to macro_snapshots DB. Regime range now -6 to +6 (was -5 to +5).

CONTINUOUS TRAINING PATTERN:
  All runs show monotone fold Sharpes (0→2→5+). FOLD_MAX guard (2.0) blocks all deploys.
  Root cause: XAUUSD_5min.csv only covers one regime (Mar 2026 bull run).
  Expected. Will self-correct mid-April after diverse data accumulates.

POWERSHELL MCP LESSON:
  Syntax checking via python -c "import ast..." times out on large files.
  Best: read specific lines of the file directly (FileSystem read + offset/limit).

---

## 2026-03-22 — Full System Audit, 6 Bug Fixes, Win Rate Improvements

=== MORNING: FULL AUDIT ===

BUG 1 (CRITICAL): xauusd_service MIN_DOLLARS = 500 with NAV=$100
  Every XAU/USD position was floored from $2 to $500 = 500% NAV. Silent.
  Fix: MIN_DOLLARS = 1.0

BUG 2 (CRITICAL): place_xauusd_order logged size_pct = size_usd / 100000 (hardcoded old NAV)
  With $100 NAV, a $2 position logged size_pct = 0.00002 instead of 0.02.
  Fix: pre-compute _order_nav = float(await redis.get("gt:nav")) before DB insert.

BUG 3 (SIGNIFICANT): risk_service check_portfolio and validate_signal only checked
  gt:open_positions (GLD). Completely blind to XAU/USD in gt:xauusd_positions.
  Fix: both functions now read both lists and sum combined exposure.

BUG 4 (CRITICAL): close_all_positions.py used broker='alpaca_paper' (renamed to 'simulated_gld' Mar 21)
  Orphan cleanup would have silently found 0 rows.
  Fix: WHERE broker IN ('alpaca_paper','simulated_gld')

BUG 5 (COSMETIC): signal_service comment said "Alpaca paper" — updated.
BUG 6 (COSMETIC): dashboard_server init log said "Alpaca" — updated.

HEALTH MONITOR BUG:
  health_monitor.py crashed: 'No time zone found with key America/New_York'
  Cause: zoneinfo present but IANA database not installed (Windows Store Python).
  except ImportError doesn't catch KeyError.
  Fix: removed zoneinfo entirely. Uses manual UTC offset (EDT=-4, EST=-5) always.
  LESSON: Never use zoneinfo on Windows. Always use timedelta for timezone arithmetic.

STARTUP LESSON: start_services.bat uses correct venv paths. Never launch with bare `python`.
  Start-Process with bare python.exe uses system Python which lacks all packages.

=== EVENING: WIN RATE IMPROVEMENTS ===

Five changes implemented to increase win rate from ~55% baseline.
All changes confirmed working in live files after fix.

CHANGE 1 — Regime-direction alignment (signal_service._send_signal):
  BULLISH+SELL dropped. BEARISH+BUY dropped. NEUTRAL accepts both.
  Rationale: taking SELL signals in a BULLISH macro regime fights the identified trend.
  This is the primary driver of stop-loss exits.

CHANGE 2 — Score threshold raised 3→4 (signal_service._rule_based_signal):
  Was: score ≥ 3 fires BUY (p_up=0.65). Now: score ≥ 4 fires BUY (p_up=0.70).
  Fewer trades, better signal quality, lower stop-loss exit rate.

CHANGE 3 — RSI-14 added as 5th indicator (signal_service._rule_based_signal):
  def _rsi(prices, period=14) added above _rule_based_signal.
  rsi_score = +1 if rsi<35 (oversold), -1 if rsi>65 (overbought), 0 otherwise.
  Score range now -5 to +5. Fire threshold 4/5.

CHANGE 4 — London 0.9x → 0.3x (xauusd_service + execution_service SESSION_MULT):
  Mar 19 data: London had 43.4% win rate. Breakeven for 1.5:1 R:R = ~40%.
  Reduced to 0.3x (not blocked, just de-risked significantly).

CHANGE 5 — 10-minute market open cool-down (execution_service.place_order):
  Blocks GLD entries 13:30-13:40 UTC (9:30-9:40 AM ET).
  Added after EOD check, before calendar check.

=== CRITICAL REGEX LESSON — CAUSED CRASH ===

Symptom: xauusd_service crashed on import with SyntaxError at line 284.
  "unexpected character after line continuation character"

Root cause: PowerShell regex replacement of "LONDON": 0.9, with "LONDON": 0.3,  # comment
  ate the rest of the dictionary onto the same line.
  Result: SESSION_MULT = {"ASIAN": 0.6, "LONDON": 0.3,   # comment "OVERLAP": 1.0...}
  This is valid-looking text but a Python SyntaxError.

Diagnosis: crash message references line 284 (inside a docstring) — NOT the real error line.
  Python's line counter shifts when earlier lines are malformed.
  To find the real bad line: search for "LONDON" in the file directly.

Fix: replace the ENTIRE dict string, not just one entry.
  $old = '"ASIAN": 0.6, "LONDON": 0.9, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}'
  $new = '"ASIAN": 0.6, "LONDON": 0.3, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}'

Same bug hit execution_service.py simultaneously — fixed same way.

Always verify after any dict edit with:
  venv\Scripts\python.exe -c "import py_compile; py_compile.compile('filename.py')"
  (faster than full import for syntax checking)

=== LSTM TRAINING SESSIONS (Colab) ===

SESSION 1 — GLD_5min.csv only (36,695 bars, 2024-2026):
  Best: v5_D avg_sharpe=6.529, folds=[2.71, 6.89, 9.98] — OVERFIT (monotone)
  All configs showed monotone fold increases. Bull-run only data.

SESSION 2 — GLD_5min.csv + yfinance GC=F daily 2004-2024 (combined 41,718 bars):
  Best: v5_F avg_sharpe=2.163, folds=[0.0, 0.0, 6.49] — NOT deployable
  Folds 1+2 = zero (model collapses on 2004-2018 data), fold 3 = 6.49 (memorises recent).
  Root cause: daily bars (2004-2018) mixed with 5-min bars (2024-2026) creates
  incompatible volatility scales. Model can't reconcile them.
  Colab code had false positive: monotone check only caught strictly increasing folds.
  0.0 < 0.0 < 6.49 failed the check (0.0 is not < 0.0) → printed "consistent folds".
  In reality [0.0, 0.0, 6.49] is worse than [0.45, 2.95, 4.55] — fold 3 dominates.

CORRECT DEPLOYMENT CRITERIA:
  ✅ Deploy if: avg Sharpe ≥ 1.0 AND folds are close together (no fold >2x any other fold)
     Example of real signal: [0.6, 0.7, 0.8] — consistent across regimes
  ❌ Never deploy if: any fold is 0.0, or last fold is >2x the first fold
  ❌ Never deploy if: avg Sharpe > 3.0 (overfit)

NEXT RETRAIN: mid-April 2026. By then XAUUSD_5min.csv will have 4-6 weeks of diverse data.
  Use 5-minute bars throughout (don't mix daily). Upload XAUUSD_5min.csv alongside GLD_5min.csv.

---

## GENERAL LESSONS (Applies to All Sessions)

### PowerShell File Editing
- NEVER use regex to replace part of a dict/list line. Replace the ENTIRE value.
- Never use $lines[0..N] where N could exceed array length.
- After any file edit that affects Python syntax: run py_compile to verify.
  venv\Scripts\python.exe -c "import py_compile; py_compile.compile('file.py')"
- Python syntax error line numbers can be misleading when the file has a BOM or
  when an earlier malformed line shifts all subsequent line numbers.

### Python on Windows
- Python 3.13: NO backslash escapes inside f-string expressions. Use temp variables.
- zoneinfo DOES NOT WORK on Windows Store Python (IANA database missing).
  Always use: utc_offset = timedelta(hours=-4 if 3<=month<=11 else -5)
- Services that import torch/yfinance take 45-60s to start. Always wait before health check.
- Files with BOM (UTF-8-sig) fail ast.parse with "invalid non-printable character U+FEFF".

### Architecture Rules
- n8n is NEVER in the execution path. Post-fill only.
- Exposure check MUST sum BOTH gt:open_positions (GLD) AND gt:xauusd_positions (XAU/USD).
- XAU/USD closes MUST update gt:daily_pnl_pct and gt:nav (same as GLD closes).
- Always add max_simultaneous cap when adding position sources. Cooldown alone insufficient.
- When renaming a broker label, grep ALL files for the old label before committing.
- After any service restart: check DB for orphaned open positions (closed_at IS NULL).

### Regex Editing (IMPORTANT)
- Replacing part of a dict line with a regex that includes a comment WILL eat the rest.
- Always replace the ENTIRE dict value string. Never inject comments mid-line via regex.
- Verify with py_compile after any edit that touches Python dict/list syntax.

### Training
- Overfitting: folds like [0.45, 2.95, 4.55] — monotone, last fold 10x first = memorising trend.
- Mixed timescale training (daily + 5min) produces [0.0, 0.0, 6.49] — different problem.
  The model can't handle two volatility regimes in the same dataset. Use ONE timescale.
- Walk-forward MUST use purge gap (fwd_bars gap between train end and val start).
- Deploy only if: avg ≥ 1.0 AND consistent folds (no fold dominating, no zeros).

### Context Files
- /mnt/project/ files are snapshots — always read live  files.
- CONTEXT_FOR_CLAUDE.md is the authoritative state doc — update at end of every session.
- CLAUDE_session_learnings.md is append-only — never edit old entries.
