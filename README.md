# GoldenTrinity

An AI-driven gold trading system built over 10 days in March 2026, deployed as seven
Python/FastAPI microservices with Docker, TimescaleDB, Redis and n8n.

**It never produced a working trading model, and its headline performance record turned
out to be wrong. This repository is published because those two facts are more
interesting than the system would have been if it had worked.**

Archived. Do not run it in production. See [SECURITY.md](SECURITY.md) — the dashboard
has an unauthenticated remote-code-execution bug.

---

## The result

The project's own notes claimed:

```
RESULTS (Mar 19):
  215 trades, 51.6% win rate, +$420.57
  All-time total: 247 trades, 55.1%, +$631.77
```

That number is not in the database. The record was **deleted during development**:

```
PORTFOLIO RESET:
  Deleted all rows: position_log (295), signal_log (17,993), daily_reports.
  Redis reset: gt:nav=100, gt:open_positions cleared.
  Previous +$631.77 / 247 trades archived in this log only.
```

The 23 trades that survive, in [`data/archive_reset2_mar23.txt`](data/archive_reset2_mar23.txt),
came from the final session — **9 take-profits against 14 stop-losses, a 39.1% win
rate**, not 55.1%.

Three further problems, all documented in this repo by the project at the time:

- **The broker was never real after March 21.** `execution_service.py` was rewritten to
  fill orders locally against a yfinance price. The only surviving evidence of actual
  Alpaca paper orders is one log line: `NAV loaded: $96,653.62`.
- **The price feed was untrustworthy.** `log_risk.txt` records
  `CIRCUIT BREAKER - 4.19% DOWN in 60s @ $4496.30`. A 4.19% move in gold in one minute
  is not physically possible; it is a `yfinance GC=F` artifact. Every simulated P&L
  figure was priced off that feed.
- **An encoding bug silently disabled order submission.** `log_signal.txt` is 2,732
  repetitions of the same error: a 🔔 emoji in a `print()` statement raised
  `UnicodeEncodeError` on a cp1252 console *before* the line that dispatched the order
  to the execution service. Signals were written to the database; orders were never sent.

---

## The part that worked: refusing to ship a fake model

Fifteen LSTM training runs are recorded in
[`model/training_report.txt`](model/training_report.txt). Not one produced a
deployable model. The pipeline caught its own results:

| Run | Reported | Verdict |
|-----|----------|---------|
| #4 | avg Sharpe **3.98**, best fold **8.41** | `DEPLOYED: YES` + `*** OVERFIT WARNING ***` |
| #5 | *identical numbers, re-evaluated* | `OVERFIT` — *"model memorized training data. NOT safe to deploy."* |

Run #5 is the point. The same numbers that run #4 deployed were rejected four minutes
later once the gate was corrected. The deployment criteria that came out of it:

```
DEPLOY if:   avg Sharpe >= 1.0 AND no fold > 2x any other fold
NEVER deploy if: any fold is 0.0
NEVER deploy if: the last fold is > 2x the first fold
NEVER deploy if: avg Sharpe > 3.0 (overfit)
```

with the reasoning written out:

> *A model with `[0.4, 0.5, 0.6]` avg=0.5 is real. A model with `[0.0, 0.0, 14.0]`
> avg=4.7 is leakage.*

A later check was wrong in an instructive way — the monotonicity guard only rejected
*strictly increasing* folds, so `[0.0, 0.0, 6.49]` passed it, because `0.0 < 0.0` is
false. The guard was fixed rather than the result.

> ⚠️ **A note on `model/training_metrics_v4.json`.** It reports
> `best_sharpe: 54.111` with folds `[0.448, 0.148, 54.111]`. That is overfitting and
> the file does not say so. It is published next to `training_report.txt` on purpose:
> reading it alone is exactly the mistake this project was about.

---

## Architecture

| Service | Port | File | Role |
|---------|------|------|------|
| macro | 8001 | `macro_service.py` | Regime classifier (BULLISH/NEUTRAL/BEARISH) from FRED + yfinance + news sentiment |
| risk | 8002 | `risk_service.py` | Independent kill switch, exposure caps, circuit breaker, drawdown halt |
| execution | 8003 | `execution_service.py` | Order sizing, simulated fills, position lifecycle |
| signal | 8004 | `signal_service.py` | EMA/Bollinger rule-based signals, LSTM inference path |
| xauusd | 8005 | `xauusd_service.py` | Simulated XAU/USD broker + continuous retraining loop |
| health | — | `health_monitor.py` | Service heartbeat alerting |
| dashboard | 9000 | `dashboard_server.py` | Web dashboard (see SECURITY.md) |

`risk_service.py` runs as a **separate process** from the execution path specifically so
it can halt the system unilaterally — the one design decision here worth copying.

The live strategy was never the LSTM. It was a five-indicator majority vote in
`signal_service.py:_rule_based_signal()` (EMA 9/21, 15-tick momentum, SMA cross,
acceleration, RSI-14), which only clears the 0.60 probability gate when at least four
of five agree.

---

## What I did vs. what the AI agents did

This project was built with AI coding agents (Claude). Being precise about that:

- **The developer chose the architecture, the constraints, and the questions.** Every
  broker decision in the "Dead Ends" table was a regulatory fact — a 15-year-old in the
  US cannot open an SSN-requiring forex account — and the decision to simulate locally
  instead of fake an account was theirs.
- **The agents wrote the code**, under direction.
- **The developer read the failure logs and wrote the post-mortems.** The debugging
  notes in [`CLAUDE_session_learnings.md`](CLAUDE_session_learnings.md) are first-person
  records of diagnosing broken edits, including the one that matters: a PowerShell regex
  replacement that ate a Python dictionary onto one line and produced a `SyntaxError`
  whose reported line number pointed at a docstring 200 lines away.
- **The developer made the call not to deploy a model it did not trust**, and recorded
  the criteria.

## Why publish an archived project?

Because the failure is the useful part. Every number in this repository was checked
against the assumptions underneath it, and several of them did not survive. That is the
habit the later projects in this account were built to test.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env    # (the original .env is not published - it held live keys)
```

Requires Docker for TimescaleDB/Redis/n8n. **Read SECURITY.md first.**

## Licence

MIT.
