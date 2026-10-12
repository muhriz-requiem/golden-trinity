"""
risk_service.py — Port 8002
Portfolio monitor, circuit breaker, pre-trade risk checks.
Runs as independent process — cannot be blocked by execution logic.

FIX v2:
  - Circuit breaker now monitors XAU/USD price for >1.5% moves in 60s.
  - MODEL_CONFIDENCE_COLLAPSE monitored and causes a halt.
  - Daily drawdown works because execution_service writes gt:daily_pnl_pct.

FIX v3:
  - Collapse detection now requires SUSTAINED collapse before halting.
    Old: one detection in a 30-second poll → immediate halt.
    New: collapse must be present for 2 consecutive 30-second checks (60s total)
         before the halt is triggered. A brief burst at market open (normal choppy
         price action) clears itself in under 60 seconds and will NOT cause a halt.
  - Added market-open grace period: no collapse halting for first 10 minutes
    after 9:30 AM ET (13:30 UTC). This is when price action is always most
    chaotic — signals naturally flip rapidly for a few minutes, then stabilise.
"""
import os, time, asyncio, json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg
import httpx

load_dotenv()

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
DB_HOST    = os.getenv("DB_HOST", "localhost")
DB_PORT    = int(os.getenv("DB_PORT", 5432))
DB_NAME    = os.getenv("DB_NAME", "goldentrinity")
DB_USER    = os.getenv("DB_USER")
DB_PASS    = os.getenv("DB_PASS")
N8N_RISK   = os.getenv("N8N_WEBHOOK_RISK_ALERT", "http://localhost:5678/webhook/risk-alert")

redis_client = None
db_pool      = None

# Portfolio limits
MAX_GOLD_EXPOSURE_PCT = 0.35
MAX_MINING_PCT        = 0.12
DAILY_DRAWDOWN_HALT   = -0.03
SPREAD_LIMIT          = 0.0005

# Circuit breaker
CB_THRESHOLD_PCT = 0.015   # 1.5% price move
CB_WINDOW_SEC    = 60

# FIX v3: require N consecutive collapse detections before halting
# Each check runs every 30 seconds, so 2 checks = 60 seconds sustained collapse.
COLLAPSE_CHECKS_REQUIRED = 2

# Market-open grace period: don't check collapse for this many minutes after open
MARKET_OPEN_GRACE_MINUTES = 10

MINING_STOCKS = {"NEM", "GOLD", "AEM"}

_price_history: list[dict] = []
_collapse_consecutive: int = 0   # FIX v3: counter for sustained collapse detection


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5
    )
    asyncio.create_task(portfolio_monitor_loop())
    asyncio.create_task(circuit_breaker_monitor_loop())
    asyncio.create_task(collapse_alert_monitor_loop())
    print("[risk_service] Started.")
    print(f"[risk_service] Collapse halt requires {COLLAPSE_CHECKS_REQUIRED} consecutive detections "
          f"({COLLAPSE_CHECKS_REQUIRED * 30}s sustained) + {MARKET_OPEN_GRACE_MINUTES}min market-open grace period.")
    yield
    await redis_client.close()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)


# ── Portfolio monitor ──────────────────────────────────────────────────────

async def portfolio_monitor_loop():
    while True:
        try:
            await check_portfolio()
        except Exception as e:
            print(f"[risk_service] Monitor error: {e}")
        await asyncio.sleep(30)

async def check_portfolio():
    # Check BOTH GLD (gt:open_positions) and XAU/USD (gt:xauusd_positions)
    # exposure against the 35% combined limit
    positions_raw = await redis_client.get("gt:open_positions")
    xau_raw       = await redis_client.get("gt:xauusd_positions")
    positions     = json.loads(positions_raw) if positions_raw else []
    xau_positions = json.loads(xau_raw) if xau_raw else []

    nav              = float(await redis_client.get("gt:nav") or 100.0)
    gold_positions   = [p for p in positions if p.get("asset") not in {"DUST"}]
    mining_positions = [p for p in positions if p.get("asset") in MINING_STOCKS]
    gld_pct          = sum(p.get("size_pct", 0) for p in gold_positions)
    xau_pct          = sum(p.get("size_usd", 0) / max(nav, 1.0) for p in xau_positions)
    total_gold_pct   = gld_pct + xau_pct
    daily_pnl        = float(await redis_client.get("gt:daily_pnl_pct") or 0)

    if total_gold_pct > MAX_GOLD_EXPOSURE_PCT:
        await fire_alert("EXPOSURE_BREACH",
                         f"Total gold {total_gold_pct:.1%} > 35% (GLD={gld_pct:.1%} XAU={xau_pct:.1%})", "Trim positions")

    halted = await redis_client.get("gt:system_halted")
    if daily_pnl <= DAILY_DRAWDOWN_HALT and not (halted and halted.decode() == "true"):
        await redis_client.set("gt:system_halted", "true")
        await redis_client.set("gt:halt_reason", "DRAWDOWN_LIMIT")
        await fire_alert("DRAWDOWN_LIMIT",
                         f"Daily P&L {daily_pnl:.2%} hit -3% halt", "Halted new entries")


# ── Circuit breaker price monitor ─────────────────────────────────────────

async def circuit_breaker_monitor_loop():
    global _price_history
    while True:
        try:
            price   = None
            xau_raw = await redis_client.get("gt:xauusd_price")
            if xau_raw:
                price = float(xau_raw)
            else:
                sig_raw = await redis_client.get("gt:signal:current")
                if sig_raw:
                    price = json.loads(sig_raw).get("price")

            if price and price > 0:
                now = datetime.now(timezone.utc).timestamp()
                _price_history.append({"price": price, "ts": now})
                _price_history = [p for p in _price_history if now - p["ts"] <= 90]

                if len(_price_history) >= 2:
                    window_start = now - CB_WINDOW_SEC
                    old_prices   = [p for p in _price_history if p["ts"] <= window_start]
                    if old_prices:
                        oldest   = old_prices[-1]["price"]
                        move_pct = abs(price - oldest) / oldest

                        if move_pct > CB_THRESHOLD_PCT:
                            halted = await redis_client.get("gt:system_halted")
                            if not (halted and halted.decode() == "true"):
                                direction = "UP" if price > oldest else "DOWN"
                                print(f"[risk_service] CIRCUIT BREAKER — "
                                      f"{move_pct:.2%} {direction} in 60s @ ${price:.2f}")
                                await redis_client.set("gt:system_halted", "true")
                                await redis_client.set("gt:halt_reason", "CIRCUIT_BREAKER")
                                await fire_alert("CIRCUIT_BREAKER",
                                                 f"{move_pct:.2%} {direction} in 60s @ ${price:.2f}",
                                                 "Halted all entries. Manual resume required.")
        except Exception as e:
            print(f"[risk_service] CB monitor error: {e}")
        await asyncio.sleep(10)


# ── Collapse alert monitor ─────────────────────────────────────────────────

def _is_market_open_grace_period() -> bool:
    """
    Returns True during the first MARKET_OPEN_GRACE_MINUTES after 9:30 AM ET (13:30 UTC).
    During this window, collapse detection is suppressed — price action is always
    chaotic for a few minutes at open and generates false collapse alerts.
    """
    now   = datetime.now(timezone.utc)
    h     = now.hour + now.minute / 60
    # Market opens at 13:30 UTC. Grace period ends at 13:30 + grace minutes.
    grace_end = 13.5 + (MARKET_OPEN_GRACE_MINUTES / 60)
    return now.weekday() < 5 and 13.5 <= h < grace_end


async def collapse_alert_monitor_loop():
    """
    FIX v3: sustained collapse detection.

    Requires the collapse alert to be present for COLLAPSE_CHECKS_REQUIRED consecutive
    30-second polls before triggering a halt. This means:
      - A 30-second burst of bad signals at market open → counter=1 → no halt.
        By the next check 30 seconds later, signal_service has cleared the alert → counter resets.
      - A genuine sustained model failure lasting 60+ seconds → counter=2 → halt fires.

    Also suppresses detection during the market-open grace period entirely.
    """
    global _collapse_consecutive
    while True:
        try:
            collapse = await redis_client.get("gt:collapse_alert")
            halted   = await redis_client.get("gt:system_halted")

            if collapse and collapse.decode() == "true":
                if _is_market_open_grace_period():
                    # Suppress during grace period — clear the alert and reset counter
                    if _collapse_consecutive > 0:
                        print(f"[risk_service] Collapse alert suppressed (market-open grace period)")
                    _collapse_consecutive = 0
                    await redis_client.delete("gt:collapse_alert")
                elif not (halted and halted.decode() == "true"):
                    _collapse_consecutive += 1
                    print(f"[risk_service] Collapse alert detected "
                          f"({_collapse_consecutive}/{COLLAPSE_CHECKS_REQUIRED})")

                    if _collapse_consecutive >= COLLAPSE_CHECKS_REQUIRED:
                        print(f"[risk_service] SUSTAINED MODEL_CONFIDENCE_COLLAPSE — halting")
                        await redis_client.set("gt:system_halted", "true")
                        await redis_client.set("gt:halt_reason", "MODEL_CONFIDENCE_COLLAPSE")
                        await fire_alert(
                            "MODEL_CONFIDENCE_COLLAPSE",
                            f"Collapse sustained for {_collapse_consecutive * 30}s "
                            f"(>{COLLAPSE_CHECKS_REQUIRED} consecutive detections)",
                            "Full system halt. Manual resume required."
                        )
                        _collapse_consecutive = 0   # reset after firing
            else:
                # Alert cleared by signal_service — reset consecutive counter
                if _collapse_consecutive > 0:
                    print(f"[risk_service] Collapse alert cleared — counter reset "
                          f"(was {_collapse_consecutive})")
                _collapse_consecutive = 0

        except Exception as e:
            print(f"[risk_service] Collapse monitor error: {e}")
        await asyncio.sleep(30)


# ── Alert helper ───────────────────────────────────────────────────────────

async def fire_alert(alert_type: str, details: str, action: str):
    now = datetime.now(timezone.utc)
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO risk_alert_log (timestamp, alert_type, details, action_taken)
                VALUES ($1, $2, $3, $4)
            """, now, alert_type, json.dumps({"message": details}), action)
    except Exception as e:
        print(f"[risk_service] DB log error: {e}")
    asyncio.create_task(_post_n8n_alert({
        "alert_type": alert_type, "details": details, "action_taken": action
    }))
    print(f"[risk_service] ALERT {alert_type}: {details}")

async def _post_n8n_alert(payload: dict):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(N8N_RISK, json=payload)
    except Exception:
        pass


# ── Pre-trade risk check ───────────────────────────────────────────────────

@app.post("/api/signals/validate")
async def validate_signal(payload: dict):
    halted = await redis_client.get("gt:system_halted")
    if halted and halted.decode() == "true":
        reason = await redis_client.get("gt:halt_reason")
        return {"approved": False, "reason": f"System halted: {reason.decode() if reason else 'unknown'}"}

    daily_pnl = float(await redis_client.get("gt:daily_pnl_pct") or 0)
    if daily_pnl <= DAILY_DRAWDOWN_HALT:
        return {"approved": False, "reason": f"Daily drawdown limit hit: {daily_pnl:.2%}"}

    spread_pct = payload.get("spread_pct", 0)
    if spread_pct > SPREAD_LIMIT:
        return {"approved": False, "reason": f"Spread {spread_pct:.4%} exceeds 0.05% limit"}

    p_up      = payload.get("p_up", 0)
    p_down    = payload.get("p_down", 0)
    direction = payload.get("direction")
    if direction == "BUY" and not (p_up >= 0.60 and p_down <= 0.20):
        return {"approved": False, "reason": f"BUY gate failed: P(UP)={p_up:.2f} P(DOWN)={p_down:.2f}"}
    if direction == "SELL" and not (p_down >= 0.60 and p_up <= 0.20):
        return {"approved": False, "reason": f"SELL gate failed: P(DOWN)={p_down:.2f} P(UP)={p_up:.2f}"}

    positions_raw = await redis_client.get("gt:open_positions")
    xau_raw       = await redis_client.get("gt:xauusd_positions")
    positions     = json.loads(positions_raw) if positions_raw else []
    xau_positions = json.loads(xau_raw) if xau_raw else []
    nav           = float(await redis_client.get("gt:nav") or 100.0)
    gold_positions = [p for p in positions if p.get("asset") not in {"DUST"}]
    gld_pct        = sum(p.get("size_pct", 0) for p in gold_positions)
    xau_pct        = sum(p.get("size_usd", 0) / max(nav, 1.0) for p in xau_positions)
    total_pct      = gld_pct + xau_pct
    new_size       = payload.get("size_pct", 0)
    if total_pct + new_size > MAX_GOLD_EXPOSURE_PCT:
        return {"approved": False, "reason": f"Would breach 35% exposure limit ({total_pct:.1%} current: GLD={gld_pct:.1%} XAU={xau_pct:.1%})"}

    return {"approved": True, "reason": "All checks passed"}


# ── Control endpoints ──────────────────────────────────────────────────────

@app.post("/api/risk/circuit_breaker")
async def trigger_circuit_breaker(payload: dict):
    await redis_client.set("gt:system_halted", "true")
    await redis_client.set("gt:halt_reason", "CIRCUIT_BREAKER")
    await fire_alert("CIRCUIT_BREAKER", payload.get("reason", "Manual trigger"), "Halted all entries")
    return {"status": "HALTED"}

@app.post("/api/risk/halt")
async def manual_halt():
    await redis_client.set("gt:system_halted", "true")
    await redis_client.set("gt:halt_reason", "MANUAL")
    return {"status": "HALTED"}

@app.post("/api/risk/resume")
async def manual_resume():
    global _collapse_consecutive
    _collapse_consecutive = 0
    await redis_client.delete("gt:system_halted")
    await redis_client.delete("gt:halt_reason")
    await redis_client.delete("gt:collapse_alert")
    print("[risk_service] System resumed manually — all halt flags cleared")
    return {"status": "RESUMED"}

@app.get("/api/risk/status")
async def risk_status():
    halted    = await redis_client.get("gt:system_halted")
    reason    = await redis_client.get("gt:halt_reason")
    daily_pnl = await redis_client.get("gt:daily_pnl_pct")
    positions = await redis_client.get("gt:open_positions")
    pos_list  = json.loads(positions) if positions else []
    gold_pos  = [p for p in pos_list if p.get("asset") not in {"DUST"}]
    total_exp = sum(p.get("size_pct", 0) for p in gold_pos)
    collapse  = await redis_client.get("gt:collapse_alert")
    return {
        "halted":                 bool(halted and halted.decode() == "true"),
        "halt_reason":            reason.decode() if reason else None,
        "daily_pnl_pct":          float(daily_pnl) if daily_pnl else 0.0,
        "total_exposure_pct":     round(total_exp, 4),
        "open_positions":         len(pos_list),
        "collapse_alert":         bool(collapse and collapse.decode() == "true"),
        "collapse_consecutive":   _collapse_consecutive,
        "collapse_required":      COLLAPSE_CHECKS_REQUIRED,
        "grace_period_active":    _is_market_open_grace_period(),
    }

@app.get("/api/health")
async def health():
    start = time.time()
    await redis_client.ping()
    latency_ms = round((time.time() - start) * 1000, 2)
    return {
        "status":     "healthy",
        "service":    "risk_service",
        "latency_ms": latency_ms,
        "cb_samples": len(_price_history),
    }
