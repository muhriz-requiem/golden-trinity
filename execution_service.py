"""
execution_service.py — Port 8003
Simulated GLD trading (local paper). No Alpaca orders placed.
Alpaca data API is used for GLD quotes only (price tracking).

SIMULATED MODE (v5):
  - All fills are simulated locally at bid/ask mid. No orders sent to Alpaca.
  - BUY fills at ask. SELL fills at bid (real short — no DUST proxy needed).
  - NAV starts at $100. MIN_POSITION_DOLLARS = $1.
  - broker = 'simulated_gld' in position_log.
  - When switching to live trading at 18: restore alpaca_post calls, set
    NAV from /v2/account, change broker to 'alpaca_live', raise MIN to $500.

Why simulated now:
  Paper trading validates signal logic and risk management, not broker connectivity.
  Alpaca live connection is reserved for when real money is involved.

Overnight gap protection:
  All positions forcibly closed at 3:50 PM ET (19:50 UTC) every trading day.
  GLD quotes still fetched from Alpaca data API for accurate exit prices.

Event calendar:
  Blocks new entries 30 min before / 15 min after high-impact macro events.
  Reduces position size 50% for medium-impact events.

FIX v2: daily_pnl_pct + NAV written to Redis after every close. Daily reset at midnight.
FIX v3: DB sync on startup — open positions reloaded from DB into Redis so EOD close
         and stop-loss monitoring survive service restarts. qty now saved to DB.
FIX v4: INSERT RETURNING id, fill cooldown, db_id for exact close targeting.
FIX v5: Simulated fills. Real SELL support. NAV=$100. MIN_POSITION_DOLLARS=$1.
"""
import os, time, asyncio, json
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg
import httpx
from event_calendar import get_calendar

load_dotenv()

# Alpaca data API only — no trading API calls
ALPACA_DATA    = "https://data.alpaca.markets"
ALPACA_KEY     = os.getenv("ALPACA_KEY")
ALPACA_SECRET  = os.getenv("ALPACA_SECRET")
REDIS_HOST     = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT     = int(os.getenv("REDIS_PORT", 6379))
DB_HOST        = os.getenv("DB_HOST", "localhost")
DB_PORT        = int(os.getenv("DB_PORT", 5432))
DB_NAME        = os.getenv("DB_NAME", "goldentrinity")
DB_USER        = os.getenv("DB_USER")
DB_PASS        = os.getenv("DB_PASS")
N8N_TRADE      = os.getenv("N8N_WEBHOOK_TRADE_FILLED", "http://localhost:5678/webhook/trade-filled")
RISK_URL       = "http://localhost:8002"

ALPACA_DATA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

STOP_LOSS_PCT        = 0.005
TAKE_PROFIT_PCT      = 0.0075
EXPIRY_HOURS         = 4
SPREAD_LIMIT         = 0.0005
MIN_POSITION_DOLLARS = 1.0     # $100 NAV — 2% hard cap = $2, minimum $1
STARTING_NAV         = 100.0   # local paper trading starting capital
BROKER_LABEL         = "simulated_gld"
EOD_CLOSE_HOUR       = 19
EOD_CLOSE_MINUTE     = 50

FILL_COOLDOWN_SEC = 120
_last_fill_time: float = 0.0

redis_client = None
db_pool      = None
calendar     = get_calendar()


def is_market_open() -> bool:
    now = datetime.now(timezone.utc)
    h   = now.hour + now.minute / 60
    return now.weekday() < 5 and 13.5 <= h < 20.0


def is_eod_close_time() -> bool:
    now = datetime.now(timezone.utc)
    return (now.weekday() < 5 and
            now.hour == EOD_CLOSE_HOUR and
            now.minute >= EOD_CLOSE_MINUTE)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5
    )

    # NAV — use stored value or default to STARTING_NAV.
    # No Alpaca account fetch — this is a local simulated portfolio.
    existing_nav = await redis_client.get("gt:nav")
    if not existing_nav:
        await redis_client.set("gt:nav", str(STARTING_NAV))
        print(f"[execution_service] NAV initialised: ${STARTING_NAV:.2f}")
    else:
        print(f"[execution_service] NAV loaded from Redis: ${float(existing_nav):.2f}")

    # Reset daily P&L on new calendar day
    today_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_reset = await redis_client.get("gt:last_reset_date")
    last_reset = last_reset.decode() if last_reset else None
    if not await redis_client.get("gt:daily_pnl_pct") or last_reset != today_str:
        await redis_client.set("gt:daily_pnl_pct", "0.0")
        await redis_client.set("gt:last_reset_date", today_str)
        if last_reset and last_reset != today_str:
            print(f"[execution_service] New day ({last_reset} -> {today_str}) — daily P&L reset to 0")

    # Auto-clear stale collapse halt
    halt_reason = await redis_client.get("gt:halt_reason")
    if halt_reason and halt_reason.decode() == "MODEL_CONFIDENCE_COLLAPSE":
        await redis_client.delete("gt:system_halted")
        await redis_client.delete("gt:halt_reason")
        await redis_client.delete("gt:collapse_alert")
        print("[execution_service] Cleared MODEL_CONFIDENCE_COLLAPSE halt from previous session")

    await _sync_positions_from_db()

    asyncio.create_task(position_monitor_loop())
    print(f"[execution_service] Started (SIMULATED). EOD close: {EOD_CLOSE_HOUR}:{EOD_CLOSE_MINUTE:02d} UTC")
    print(f"[execution_service] Fill cooldown: {FILL_COOLDOWN_SEC}s | MIN_POS: ${MIN_POSITION_DOLLARS}")
    print(f"[execution_service] Calendar: {calendar.status_string()}")
    yield
    await redis_client.close()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)


# ── DB → Redis position sync ───────────────────────────────────────────────

async def _sync_positions_from_db():
    """Reload unclosed simulated_gld positions from DB into Redis on startup."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, asset, direction, fill_price, size_pct, qty,
                       stop_loss, take_profit, expiry_at,
                       regime_at_entry, confidence_at_entry, session,
                       slippage_pct, opened_at
                FROM position_log
                WHERE closed_at IS NULL
                  AND broker = 'simulated_gld'
                  AND pnl_pct IS NULL
                ORDER BY opened_at ASC
            """)

        if not rows:
            await redis_client.set("gt:open_positions", "[]")
            return

        nav = float(await redis_client.get("gt:nav") or STARTING_NAV)
        positions = []
        for r in rows:
            fill = float(r["fill_price"] or 0)
            size = float(r["size_pct"] or 0.02)
            qty  = float(r["qty"]) if r["qty"] and float(r["qty"]) > 0 else (
                round(size * nav / fill, 4) if fill > 0 else 0.001)

            expiry = r["expiry_at"]
            if expiry and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            direction = r["direction"]
            if float(r["stop_loss"] or 0) > 0:
                stop_loss = float(r["stop_loss"])
            else:
                stop_loss = round(fill * (1 - STOP_LOSS_PCT) if direction == "BUY"
                                  else fill * (1 + STOP_LOSS_PCT), 4)
            if float(r["take_profit"] or 0) > 0:
                take_profit = float(r["take_profit"])
            else:
                take_profit = round(fill * (1 + TAKE_PROFIT_PCT) if direction == "BUY"
                                    else fill * (1 - TAKE_PROFIT_PCT), 4)

            positions.append({
                "db_id":       r["id"],
                "id":          f"sim_{r['id']}",
                "asset":       r["asset"],
                "direction":   direction,
                "entry_price": fill,
                "fill_price":  fill,
                "size_pct":    size,
                "qty":         qty,
                "stop_loss":   stop_loss,
                "take_profit": take_profit,
                "expiry_at":   expiry.isoformat() if expiry else (
                    datetime.now(timezone.utc) + timedelta(hours=EXPIRY_HOURS)).isoformat(),
                "regime":      r["regime_at_entry"] or "NEUTRAL",
                "session":     r["session"] or "OVERLAP",
                "confidence":  float(r["confidence_at_entry"] or 0.65),
                "slippage_pct": float(r["slippage_pct"] or 0),
                "opened_at":   r["opened_at"].isoformat() if r["opened_at"] else
                               datetime.now(timezone.utc).isoformat(),
            })

        await redis_client.set("gt:open_positions", json.dumps(positions))
        print(f"[execution_service] Synced {len(positions)} open position(s) from DB")
        for p in positions:
            print(f"  → db_id={p['db_id']} {p['direction']} {p['asset']} "
                  f"@ ${p['fill_price']:.2f} | SL=${p['stop_loss']:.2f} TP=${p['take_profit']:.2f}")

    except Exception as e:
        print(f"[execution_service] DB sync failed: {e} — starting with empty position list")
        await redis_client.set("gt:open_positions", "[]")


# ── Alpaca data API (quotes only — no trading) ─────────────────────────────

async def alpaca_data_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(f"{ALPACA_DATA}{path}", headers=ALPACA_DATA_HEADERS)
        r.raise_for_status()
        return r.json()


# ── Position sizing ────────────────────────────────────────────────────────

def calculate_position_size(nav: float, regime: str, session: str, confidence: float,
                             win_rate: float, avg_win: float, avg_loss: float,
                             trade_count: int, event_multiplier: float = 1.0) -> float:
    REGIME_MULT  = {"BULLISH": 1.2, "NEUTRAL": 1.0, "BEARISH": 0.5}
    SESSION_MULT = {"ASIAN": 0.6, "LONDON": 0.3, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8, "CLOSED": 0.0}  # LONDON reduced
    r = REGIME_MULT.get(regime, 1.0)
    s = SESSION_MULT.get(session, 0.0)
    if s == 0:
        return 0.0
    if trade_count < 252:
        kelly = 0.05
    else:
        loss_rate = 1.0 - win_rate
        kelly = max(0.0, min(((win_rate - loss_rate) / (avg_win / avg_loss if avg_loss else 1)) * 0.5, 0.25))
    raw      = nav * kelly * r * s * confidence * event_multiplier
    hard_cap = nav * 0.02
    result   = min(raw, hard_cap)
    return max(result, MIN_POSITION_DOLLARS) if 0 < result < MIN_POSITION_DOLLARS else result


# ── Session ────────────────────────────────────────────────────────────────

def get_current_session() -> str:
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 7:     return "ASIAN"
    elif 7 <= h < 13:  return "LONDON"
    elif 13 <= h < 17: return "OVERLAP"
    elif 17 <= h < 21: return "NY_AFTERNOON"
    else:              return "CLOSED"


# ── Simulated order placement ──────────────────────────────────────────────

@app.post("/api/execution/order")
async def place_order(payload: dict):
    global _last_fill_time
    asset     = payload.get("asset", "GLD")
    direction = payload.get("direction")
    p_up      = payload.get("p_up", 0)
    p_down    = payload.get("p_down", 0)
    session   = get_current_session()

    if is_eod_close_time():
        return {"status": "rejected", "reason": "EOD close window — no new entries after 3:50 PM ET"}

    # Market open cool-down: no entries in first 10 minutes of US session.
    # 13:30-13:40 UTC = 9:30-9:40 AM ET. Price action at open is chaotic —
    # spreads are wide, institutions are repositioning, signals flip rapidly.
    now_utc = datetime.now(timezone.utc)
    open_h  = now_utc.hour + now_utc.minute / 60
    if 13.5 <= open_h < 13.667:   # 13:30 to 13:40 UTC
        return {"status": "rejected", "reason": "Market open cool-down — no entries in first 10 min"}

    elapsed = time.time() - _last_fill_time
    if _last_fill_time > 0 and elapsed < FILL_COOLDOWN_SEC:
        remaining = int(FILL_COOLDOWN_SEC - elapsed)
        return {"status": "rejected", "reason": f"Cooldown: {remaining}s until next entry allowed"}

    cal_blocked, cal_reason = calendar.is_blocked()
    if cal_blocked:
        return {"status": "rejected", "reason": f"Calendar: {cal_reason}"}

    event_mult = calendar.get_size_multiplier()
    confidence = p_up if direction == "BUY" else p_down

    if session not in ("OVERLAP", "NY_AFTERNOON"):
        return {"status": "rejected", "reason": f"Not tradeable in {session} session"}

    nav         = float(await redis_client.get("gt:nav") or STARTING_NAV)
    regime      = (await redis_client.get("gt:regime") or b"NEUTRAL").decode()
    trade_stats = await _get_trade_stats()

    estimated_dollars = calculate_position_size(
        nav, regime, session, confidence,
        trade_stats["win_rate"], trade_stats["avg_win"],
        trade_stats["avg_loss"], trade_stats["count"],
        event_multiplier=event_mult
    )
    estimated_pct = round(estimated_dollars / nav, 4) if nav > 0 else 0.01

    risk_resp = await _call_risk_service({
        "direction": direction, "p_up": p_up, "p_down": p_down,
        "spread_pct": 0.0002, "size_pct": estimated_pct
    })
    if not risk_resp.get("approved"):
        return {"status": "rejected", "reason": risk_resp.get("reason")}

    # Fetch live GLD quote for accurate fill price (data API only — no order placed)
    try:
        q   = await alpaca_data_get(f"/v2/stocks/{asset}/quotes/latest")
        ask = float(q.get("quote", {}).get("ap", 0))
        bid = float(q.get("quote", {}).get("bp", 0))
    except Exception as e:
        return {"status": "rejected", "reason": f"Quote error: {e}"}

    if ask == 0 or bid == 0:
        return {"status": "rejected", "reason": "Invalid quote (market may be closed)"}

    mid_price  = (ask + bid) / 2
    spread_pct = (ask - bid) / mid_price if mid_price > 0 else 1
    if spread_pct > SPREAD_LIMIT:
        return {"status": "rejected", "reason": f"Spread {spread_pct:.4%} too wide"}

    dollar_size = calculate_position_size(
        nav, regime, session, confidence,
        trade_stats["win_rate"], trade_stats["avg_win"],
        trade_stats["avg_loss"], trade_stats["count"],
        event_multiplier=event_mult
    )

    # Simulated fill: BUY at ask, SELL at bid
    fill_price  = ask if direction == "BUY" else bid
    qty         = round(max(0.001, dollar_size / fill_price), 4)
    slippage    = abs(fill_price - mid_price) / mid_price

    # Stop/take are direction-aware
    if direction == "BUY":
        stop_loss   = round(fill_price * (1 - STOP_LOSS_PCT),   4)
        take_profit = round(fill_price * (1 + TAKE_PROFIT_PCT), 4)
    else:  # SELL (short)
        stop_loss   = round(fill_price * (1 + STOP_LOSS_PCT),   4)
        take_profit = round(fill_price * (1 - TAKE_PROFIT_PCT), 4)

    expiry_at = datetime.now(timezone.utc) + timedelta(hours=EXPIRY_HOURS)
    opened_at = datetime.now(timezone.utc)

    position = {
        "id":          f"sim_{int(opened_at.timestamp()*1000)}",
        "db_id":       None,
        "asset":       asset,
        "direction":   direction,
        "entry_price": mid_price,
        "fill_price":  fill_price,
        "size_pct":    round(dollar_size / nav, 4),
        "qty":         qty,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "expiry_at":   expiry_at.isoformat(),
        "regime":      regime,
        "session":     session,
        "confidence":  confidence,
        "slippage_pct": round(slippage, 6),
        "opened_at":   opened_at.isoformat(),
    }

    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO position_log
                (opened_at, asset, direction, entry_price, fill_price, qty, size_pct,
                 stop_loss, take_profit, expiry_at, regime_at_entry,
                 confidence_at_entry, session, slippage_pct, broker)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                RETURNING id
            """, opened_at, asset, direction, mid_price, fill_price, qty,
                position["size_pct"], stop_loss, take_profit, expiry_at,
                regime, confidence, session, round(slippage, 6), BROKER_LABEL)
        position["db_id"] = row["id"]
    except Exception as e:
        print(f"[execution_service] DB log error: {e}")

    positions = json.loads(await redis_client.get("gt:open_positions") or "[]")
    positions.append(position)
    await redis_client.set("gt:open_positions", json.dumps(positions))

    _last_fill_time = time.time()

    asyncio.create_task(_notify_n8n(position))

    if event_mult < 1.0:
        print(f"[execution_service] Event size reduction: {event_mult:.0%}")
    print(f"[execution_service] SIM FILL {direction} {qty}x {asset} @ ${fill_price:.2f} | "
          f"db_id={position['db_id']} SL=${stop_loss:.2f} TP=${take_profit:.2f} | "
          f"next entry in {FILL_COOLDOWN_SEC}s")
    return {"status": "filled", "position": position}


# ── Position monitor ───────────────────────────────────────────────────────

async def position_monitor_loop():
    eod_closed_today = False
    last_date        = datetime.now(timezone.utc).date()

    while True:
        try:
            now  = datetime.now(timezone.utc)
            date = now.date()

            if date != last_date:
                eod_closed_today = False
                last_date        = date
                await _reset_daily_state()

            if is_eod_close_time() and not eod_closed_today:
                positions_raw = await redis_client.get("gt:open_positions")
                positions     = json.loads(positions_raw) if positions_raw else []
                if positions:
                    print(f"[execution_service] EOD CLOSE — closing {len(positions)} position(s)")
                    for pos in positions:
                        try:
                            q     = await alpaca_data_get(f"/v2/stocks/{pos['asset']}/quotes/latest")
                            price = float(q.get("quote", {}).get("ap", pos["fill_price"]))
                        except Exception:
                            price = pos["fill_price"]
                        await _close_position(pos, "EOD_CLOSE", price)
                    await redis_client.set("gt:open_positions", "[]")
                else:
                    print(f"[execution_service] EOD CLOSE — no open positions")
                eod_closed_today = True
            elif is_market_open():
                await check_exits()

        except Exception as e:
            print(f"[execution_service] Monitor error: {e}")
        await asyncio.sleep(30)


async def _reset_daily_state():
    """Reset daily P&L at midnight. NAV carries forward — no broker fetch needed."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await redis_client.set("gt:daily_pnl_pct", "0.0")
    await redis_client.set("gt:last_reset_date", today_str)
    nav = float(await redis_client.get("gt:nav") or STARTING_NAV)
    print(f"[execution_service] Daily reset — NAV=${nav:.2f} daily_pnl=0")


async def check_exits():
    positions_raw = await redis_client.get("gt:open_positions")
    if not positions_raw:
        return
    positions = json.loads(positions_raw)
    if not positions:
        return

    regime    = (await redis_client.get("gt:regime") or b"NEUTRAL").decode()
    now       = datetime.now(timezone.utc)
    remaining = []

    for pos in positions:
        try:
            q   = await alpaca_data_get(f"/v2/stocks/{pos['asset']}/quotes/latest")
            ask = float(q.get("quote", {}).get("ap", 0))
            bid = float(q.get("quote", {}).get("bp", 0))
            if ask == 0 or bid == 0:
                remaining.append(pos)
                continue
            mid   = (ask + bid) / 2
            direction = pos.get("direction", "BUY")

            exit_reason = None
            if direction == "BUY":
                if mid <= pos["stop_loss"]:    exit_reason = "STOP_LOSS"
                elif mid >= pos["take_profit"]: exit_reason = "TAKE_PROFIT"
            else:  # SELL (short)
                if mid >= pos["stop_loss"]:    exit_reason = "STOP_LOSS"
                elif mid <= pos["take_profit"]: exit_reason = "TAKE_PROFIT"

            if not exit_reason:
                expiry = datetime.fromisoformat(pos["expiry_at"])
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                if now >= expiry:
                    exit_reason = "TIME_EXPIRY"

            if not exit_reason:
                if direction == "BUY"  and regime == "BEARISH": exit_reason = "REGIME_CHANGE"
                if direction == "SELL" and regime == "BULLISH": exit_reason = "REGIME_CHANGE"

            if exit_reason:
                await _close_position(pos, exit_reason, mid)
            else:
                remaining.append(pos)
        except Exception as e:
            print(f"[execution_service] Exit check error for {pos.get('asset')}: {e}")
            remaining.append(pos)

    await redis_client.set("gt:open_positions", json.dumps(remaining))


async def _close_position(pos: dict, exit_reason: str, exit_price: float):
    """Close a simulated GLD position — no broker order, just DB + Redis update."""
    opened_at = datetime.fromisoformat(pos["opened_at"])
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    mins      = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60
    direction = pos.get("direction", "BUY")

    # PnL is direction-aware
    if direction == "BUY":
        pnl = (exit_price - pos["fill_price"]) / pos["fill_price"]
    else:  # SELL (short)
        pnl = (pos["fill_price"] - exit_price) / pos["fill_price"]

    db_id = pos.get("db_id")
    try:
        async with db_pool.acquire() as conn:
            if db_id:
                await conn.execute("""
                    UPDATE position_log
                    SET closed_at = NOW(), exit_price = $1, exit_type = $2,
                        time_in_trade_min = $3, pnl_pct = $4
                    WHERE id = $5
                """, exit_price, exit_reason, round(mins, 2), round(pnl, 6), int(db_id))
            else:
                await conn.execute("""
                    UPDATE position_log
                    SET closed_at = NOW(), exit_price = $1, exit_type = $2,
                        time_in_trade_min = $3, pnl_pct = $4
                    WHERE id = (
                        SELECT id FROM position_log
                        WHERE broker = 'simulated_gld'
                          AND asset = $5
                          AND closed_at IS NULL
                        ORDER BY opened_at DESC
                        LIMIT 1
                    )
                """, exit_price, exit_reason, round(mins, 2), round(pnl, 6), pos["asset"])
    except Exception as e:
        print(f"[execution_service] DB close error (db_id={db_id}): {e}")

    try:
        size_pct    = pos.get("size_pct", 0)
        current_pnl = float(await redis_client.get("gt:daily_pnl_pct") or 0)
        new_pnl     = current_pnl + (pnl * size_pct)
        await redis_client.set("gt:daily_pnl_pct", str(round(new_pnl, 6)))
    except Exception as e:
        print(f"[execution_service] daily_pnl update error: {e}")

    try:
        nav     = float(await redis_client.get("gt:nav") or STARTING_NAV)
        nav_chg = pnl * pos.get("size_pct", 0) * nav
        await redis_client.set("gt:nav", str(round(nav + nav_chg, 4)))
    except Exception as e:
        print(f"[execution_service] NAV update error: {e}")

    print(f"[execution_service] CLOSED {direction} {pos['asset']} — "
          f"{exit_reason} @ ${exit_price:.2f} | PnL:{pnl:.2%} | {mins:.1f}min | db_id={db_id}")


# ── Helpers ────────────────────────────────────────────────────────────────

async def _call_risk_service(payload: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(f"{RISK_URL}/api/signals/validate", json=payload)
            return r.json()
    except Exception as e:
        print(f"[execution_service] Risk service unreachable: {e}")
        return {"approved": False, "reason": "Risk service unreachable"}

async def _notify_n8n(fill_data: dict):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(N8N_TRADE, json=fill_data)
    except Exception:
        pass

async def _get_trade_stats() -> dict:
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pnl_pct FROM position_log WHERE pnl_pct IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 252"
            )
        if not rows:
            return {"win_rate": 0.55, "avg_win": 0.0075, "avg_loss": 0.005, "count": 0}
        pnls   = [float(r["pnl_pct"]) for r in rows]
        wins   = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        return {
            "win_rate": len(wins) / len(pnls),
            "avg_win":  sum(wins)   / len(wins)   if wins   else 0.0075,
            "avg_loss": sum(losses) / len(losses) if losses else 0.005,
            "count":    len(pnls)
        }
    except Exception:
        return {"win_rate": 0.55, "avg_win": 0.0075, "avg_loss": 0.005, "count": 0}


# ── API ────────────────────────────────────────────────────────────────────

@app.get("/api/execution/positions")
async def get_positions():
    raw = await redis_client.get("gt:open_positions")
    return {"positions": json.loads(raw) if raw else []}

@app.post("/api/execution/close/{position_id}")
async def close_position_manual(position_id: str):
    positions = json.loads(await redis_client.get("gt:open_positions") or "[]")
    for pos in positions:
        if pos.get("id") == position_id or str(pos.get("db_id")) == position_id:
            try:
                q   = await alpaca_data_get(f"/v2/stocks/{pos['asset']}/quotes/latest")
                ask = float(q.get("quote", {}).get("ap", pos["fill_price"]))
                bid = float(q.get("quote", {}).get("bp", pos["fill_price"]))
                price = (ask + bid) / 2 if ask > 0 and bid > 0 else pos["fill_price"]
            except Exception:
                price = pos["fill_price"]
            await _close_position(pos, "MANUAL", price)
            remaining = [p for p in positions
                         if p.get("id") != position_id and str(p.get("db_id")) != position_id]
            await redis_client.set("gt:open_positions", json.dumps(remaining))
            return {"status": "closed"}
    return {"status": "not_found"}

@app.get("/api/metrics/daily")
async def daily_metrics():
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT pnl_pct, exit_type, regime_at_entry, session, confidence_at_entry
                FROM position_log
                WHERE closed_at >= NOW() - INTERVAL '24 hours' AND pnl_pct IS NOT NULL
            """)
        if not rows:
            return {"trade_count": 0, "total_pnl_pct": 0, "win_rate": 0}
        pnls = [float(r["pnl_pct"]) for r in rows]
        wins = len([p for p in pnls if p > 0])
        return {
            "trade_count":    len(rows),
            "total_pnl_pct":  round(sum(pnls), 4),
            "win_rate":       round(wins / len(rows), 4),
            "avg_confidence": round(
                sum(float(r["confidence_at_entry"] or 0) for r in rows) / len(rows), 4)
        }
    except Exception as e:
        return {"trade_count": 0, "total_pnl_pct": 0, "win_rate": 0, "error": str(e)}

@app.get("/api/health")
async def health():
    start      = time.time()
    await redis_client.ping()
    latency_ms = round((time.time() - start) * 1000, 2)
    nav        = await redis_client.get("gt:nav")
    positions  = await redis_client.get("gt:open_positions")
    daily_pnl  = await redis_client.get("gt:daily_pnl_pct")
    cooldown_remaining = max(0, int(FILL_COOLDOWN_SEC - (time.time() - _last_fill_time)))
    return {
        "status":             "healthy",
        "service":            "execution_service",
        "mode":               "simulated_gld",
        "latency_ms":         latency_ms,
        "nav":                float(nav) if nav else STARTING_NAV,
        "daily_pnl_pct":      float(daily_pnl) if daily_pnl else 0.0,
        "open_positions":     len(json.loads(positions)) if positions else 0,
        "market_open":        is_market_open(),
        "eod_close_time":     f"{EOD_CLOSE_HOUR}:{EOD_CLOSE_MINUTE:02d} UTC",
        "fill_cooldown_sec":  cooldown_remaining,
        "calendar_status":    calendar.status_string(),
    }
