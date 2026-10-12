"""
xauusd_service.py — Port 8005
==============================
Simulated XAU/USD broker + 24/7 continuous LSTM training loop.

Two things in one service:

1. SIMULATED XAU/USD BROKER
   - Price feed: yfinance GC=F (gold futures) — free, no account, 24/5
   - Fills: simulated at mid price + 0.03% spread (realistic spot spread)
   - Positions: stored in Redis + TimescaleDB, same format as Alpaca
   - Shorts: fully supported (real SELL, not DUST proxy)
   - Sessions: all 4 sessions active (Asian, London, Overlap, NY)
   - No EOD forced close — XAU/USD trades 24/5 so overnight holds are fine

2. CONTINUOUS LSTM TRAINING
   - Runs a background training loop every RETRAIN_INTERVAL_HOURS
   - Each cycle: fetch fresh data → retrain best config → hot-swap model
   - signal_service detects the new model file and reloads automatically
   - Training uses both historical CSV data AND live paper trade outcomes
   - Over time: model improves on your actual trading patterns

Why yfinance GC=F instead of a broker:
   - GC=F (gold futures) tracks spot XAU/USD within $1-2 at all times
   - Free, no account, no SSN, no age restriction
   - Available 23h/day on weekdays (futures close 1h/day for maintenance)
   - Good enough for paper trading validation

Run: uvicorn xauusd_service:app --port 8005

FIX v2:
  - xauusd_signal() exposure check now combines GLD + XAU/USD positions
    (was reading only gt:xauusd_positions — ignored GLD, allowed 35+ open positions)
  - xauusd_signal() now enforces MAX_XAU_POSITIONS=5 hard cap
  - _close_xauusd_position() now updates gt:daily_pnl_pct and gt:nav in Redis
    (was missing entirely — dashboard showed $0 Day P&L, drawdown halt was blind)
"""
import os, json, time, asyncio, threading
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg
import httpx
import requests as req_sync
import yfinance as yf
from training_report import write_training_report

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
DB_HOST    = os.getenv("DB_HOST",    "localhost")
DB_PORT    = int(os.getenv("DB_PORT", 5432))
DB_NAME    = os.getenv("DB_NAME",    "goldentrinity")
DB_USER    = os.getenv("DB_USER")
DB_PASS    = os.getenv("DB_PASS")

EXECUTION_URL = "http://localhost:8003"
RISK_URL      = "http://localhost:8002"
N8N_TRADE     = os.getenv("N8N_WEBHOOK_TRADE_FILLED",
                           "http://localhost:5678/webhook/trade-filled")
DISCORD_HEALTH  = os.getenv("DISCORD_WEBHOOK_HEALTH")
DISCORD_REPORTS = os.getenv("DISCORD_WEBHOOK_REPORTS")

MODEL_DIR      = r"D:\golden-trinity\model"
MODEL_FILE     = os.path.join(MODEL_DIR, "lstm_model.pt")
MODEL_NEW_FILE = os.path.join(MODEL_DIR, "lstm_model_new.pt")   # written here, then renamed
DATA_5MIN      = r"D:\golden-trinity\data\GLD_5min.csv"
DATA_1MIN      = r"D:\golden-trinity\data\GLD_1min.csv"
XAUUSD_FILE    = r"D:\golden-trinity\data\XAUUSD_5min.csv"      # live price log
LIVE_TRADES    = r"D:\golden-trinity\data\GLD_live_trades.csv"

os.makedirs(MODEL_DIR, exist_ok=True)
os.makedirs(r"D:\golden-trinity\data", exist_ok=True)

# Simulated broker settings
SPREAD_PCT      = 0.0003    # 0.03% — realistic spot gold spread
STOP_LOSS_PCT   = 0.005
TAKE_PROFIT_PCT = 0.0075
EXPIRY_HOURS    = 4
MIN_DOLLARS     = 1.0      # matches execution_service ($100 NAV, 2% cap = $2 min)
FILL_COOLDOWN_SEC = 120    # 2-minute cooldown between XAU/USD fills
_last_xau_fill_time: float = 0.0

# FIX v2: hard cap on simultaneous XAU/USD positions
# 120s cooldown alone is insufficient over a 23h trading day —
# positions accumulate throughout the session and can reach 35+.
# With MAX_XAU_POSITIONS=5 and 4h expiry, max concurrent exposure
# is bounded at 5 × 2% NAV = 10% from XAU/USD alone.
MAX_XAU_POSITIONS = 5

# Continuous training settings
RETRAIN_INTERVAL_HOURS = 6     # retrain every 6 hours
MIN_NEW_BARS_TO_RETRAIN = 50   # only retrain if at least this many new bars collected
TRAIN_WINDOW        = 60       # bars of history per sample
TRAIN_FORWARD       = 6        # bars ahead to predict
TRAIN_PERCENTILE    = 35       # label threshold percentile
TRAIN_HIDDEN        = 64
TRAIN_EPOCHS        = 60
TRAIN_LR            = 0.001
TRAIN_EARLY_STOP    = 15
BATCH_SIZE          = 512
SIM_HIGH            = 0.40   # p_up >= 0.40 for BUY (loose training gate, live gate is 0.60)
SIM_LOW             = 0.30   # p_down <= 0.30 for BUY (prevents split signals at training stage)

redis_client = None
db_pool      = None
_price_buffer: list[dict] = []
_last_price: float = 0.0
_training_active = False
_bars_since_retrain = 0
_last_continuous_fold_sharpes: list = []   # populated by _train_single_config for reporting


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5
    )

    # Ensure XAU/USD position key exists
    if not await redis_client.get("gt:xauusd_positions"):
        await redis_client.set("gt:xauusd_positions", "[]")

    # Start background tasks
    asyncio.create_task(price_feed_loop())
    asyncio.create_task(position_monitor_loop())
    asyncio.create_task(continuous_training_loop())

    print("[xauusd_service] Started")
    print(f"[xauusd_service] Price feed: yfinance GC=F (gold futures proxy)")
    print(f"[xauusd_service] Retrain every {RETRAIN_INTERVAL_HOURS}h")
    print(f"[xauusd_service] Spread: {SPREAD_PCT*100:.3f}%")
    print(f"[xauusd_service] Max simultaneous positions: {MAX_XAU_POSITIONS}")
    print(f"[xauusd_service] Exposure check: combined GLD + XAU/USD vs 35% limit")

    _post_discord(DISCORD_HEALTH,
        f"🟡 **XAU/USD service started**\n"
        f"Simulated broker + continuous LSTM training\n"
        f"Retrain every {RETRAIN_INTERVAL_HOURS}h | spread={SPREAD_PCT*100:.3f}% | "
        f"max_pos={MAX_XAU_POSITIONS}")
    yield
    await redis_client.close()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)


# ── Price feed ────────────────────────────────────────────────────────────

async def price_feed_loop():
    """
    Poll yfinance GC=F every 30 seconds for live gold price.
    GC=F = gold futures, tracks spot XAU/USD within $1-2.
    Runs 23h/day on weekdays (futures close 1h for maintenance 5-6 PM ET).
    """
    global _price_buffer, _last_price, _bars_since_retrain
    last_bar_time = None

    while True:
        try:
            # yfinance is synchronous — run in thread pool to avoid blocking
            loop  = asyncio.get_event_loop()
            price = await loop.run_in_executor(None, _fetch_gold_price)

            if price and price > 0:
                now = datetime.now(timezone.utc)
                bid = price * (1 - SPREAD_PCT / 2)
                ask = price * (1 + SPREAD_PCT / 2)

                _last_price = price
                _price_buffer.append({
                    "price":     price,
                    "bid":       bid,
                    "ask":       ask,
                    "volume":    1.0,
                    "timestamp": now.isoformat()
                })
                if len(_price_buffer) > 300:
                    _price_buffer = _price_buffer[-300:]

                # Write to Redis Streams
                await redis_client.xadd("gt:ticks:xauusd", {
                    "price":     str(price),
                    "bid":       str(bid),
                    "ask":       str(ask),
                    "volume":    "1",
                    "timestamp": str(int(now.timestamp() * 1000))
                })
                await redis_client.xtrim("gt:ticks:xauusd", maxlen=2000)
                await redis_client.set("gt:xauusd_price", str(price))

                # Log 5-minute bars for training data
                bar_minute = (now.minute // 5) * 5
                bar_time   = now.replace(minute=bar_minute, second=0, microsecond=0)
                if last_bar_time != bar_time:
                    last_bar_time = bar_time
                    _bars_since_retrain += 1
                    await _log_bar(bar_time, price)

                # Check position exits every tick
                await check_xauusd_exits(price)

        except Exception as e:
            print(f"[xauusd_service] Price feed error: {e}")

        await asyncio.sleep(30)


def _fetch_gold_price() -> float:
    """
    Fetch XAU/USD price via yfinance.
    GC=F = gold futures (primary) — accurate, 24/5 availability.
    GLD  = gold ETF (fallback, US hours only).
    Sanity range: $1500-$8000/oz (gold was ~$5000 in March 2026).
    """
    # Primary: GC=F futures — most accurate 24/5 gold price
    try:
        hist = yf.Ticker("GC=F").history(period="1d", interval="1m")
        if not hist.empty:
            price = float(hist["Close"].iloc[-1])
            if 1500 < price < 8000:
                return price
    except Exception:
        pass

    # Fallback: GLD ETF during US hours
    # GLD holds ~1/10.87 oz at current price levels (GLD~$460 = XAU~$5000)
    try:
        hist = yf.Ticker("GLD").history(period="1d", interval="1m")
        if not hist.empty:
            gld = float(hist["Close"].iloc[-1])
            xau = gld * 10.87
            if 1500 < xau < 8000:
                return round(xau, 2)
    except Exception:
        pass

    return 0.0


async def _log_bar(bar_time: datetime, price: float):
    """Append 5-min bar to XAUUSD_5min.csv for training."""
    try:
        row = pd.DataFrame([{
            "timestamp": bar_time.isoformat(),
            "open":      price,
            "high":      price,
            "low":       price,
            "close":     price,
            "volume":    1.0
        }]).set_index("timestamp")

        if os.path.exists(XAUUSD_FILE):
            row.to_csv(XAUUSD_FILE, mode="a", header=False)
        else:
            row.to_csv(XAUUSD_FILE)
    except Exception:
        pass


# ── Simulated order execution ──────────────────────────────────────────────

@app.post("/api/xauusd/signal")
async def xauusd_signal(payload: dict):
    """
    Full signal pipeline for XAU/USD simulated broker.
    Called by signal_service for every passing signal in every session.
    Runs: halt check → cooldown → position cap → combined exposure check → Kelly sizing → fill.

    FIX v2: exposure check now combines GLD (gt:open_positions) + XAU/USD (gt:xauusd_positions).
    Old: only read gt:xauusd_positions — ignored GLD entirely, allowed 35+ concurrent positions.
    Also added MAX_XAU_POSITIONS hard cap — cooldown alone is insufficient over 23h.
    """
    global _last_xau_fill_time

    direction = payload.get("direction")
    p_up      = float(payload.get("p_up", 0))
    p_down    = float(payload.get("p_down", 0))
    regime    = payload.get("regime", "NEUTRAL")
    session   = payload.get("session", "OVERLAP")

    # No price available yet
    if _last_price <= 0:
        return {"status": "rejected", "reason": "No XAU/USD price yet"}

    # Halt check
    halted = await redis_client.get("gt:system_halted")
    if halted and halted.decode() == "true":
        reason = await redis_client.get("gt:halt_reason")
        halt_str = reason.decode() if reason else "unknown"
        return {"status": "rejected", "reason": f"System halted: {halt_str}"}

    # Cooldown check (separate from GLD cooldown)
    elapsed = time.time() - _last_xau_fill_time
    if _last_xau_fill_time > 0 and elapsed < FILL_COOLDOWN_SEC:
        remaining = int(FILL_COOLDOWN_SEC - elapsed)
        return {"status": "rejected", "reason": f"XAU/USD cooldown: {remaining}s remaining"}

    # FIX v2: pre-trade risk check — combined GLD + XAU/USD exposure
    nav           = float(await redis_client.get("gt:nav") or 100.0)
    xau_positions = json.loads(await redis_client.get("gt:xauusd_positions") or "[]")
    gld_positions = json.loads(await redis_client.get("gt:open_positions") or "[]")

    # Hard cap: max 5 simultaneous XAU/USD positions
    # Without this, a 23h trading day accumulates positions throughout the session.
    # 120s cooldown slows entry but doesn't bound the total — this does.
    if len(xau_positions) >= MAX_XAU_POSITIONS:
        return {"status": "rejected",
                "reason": f"Max simultaneous XAU/USD positions reached ({len(xau_positions)}/{MAX_XAU_POSITIONS})"}

    # Combine exposure from both brokers — 35% limit applies to total gold
    xau_exp   = sum(p.get("size_usd", MIN_DOLLARS) / nav for p in xau_positions)
    gld_exp   = sum(p.get("size_pct", 0) for p in gld_positions)
    total_exp = xau_exp + gld_exp

    conf    = p_up if direction == "BUY" else p_down
    est_pct = min(nav * 0.02, max(MIN_DOLLARS, nav * 0.05 * conf)) / nav
    if total_exp + est_pct > 0.35:
        return {"status": "rejected",
                "reason": f"Combined exposure {total_exp:.1%} (XAU={xau_exp:.1%} GLD={gld_exp:.1%}) would breach 35% limit"}

    # Kelly position sizing (same formula as execution_service)
    REGIME_MULT  = {"BULLISH": 1.2, "NEUTRAL": 1.0, "BEARISH": 0.5}
    SESSION_MULT = {"ASIAN": 0.6, "LONDON": 0.3, "OVERLAP": 1.0, "NY_AFTERNOON": 0.8}  # LONDON reduced: low win rate
    r_mult    = REGIME_MULT.get(regime, 1.0)
    s_mult    = SESSION_MULT.get(session, 0.6)
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pnl_pct FROM position_log WHERE pnl_pct IS NOT NULL "
                "ORDER BY closed_at DESC LIMIT 252"
            )
        pnls   = [float(r["pnl_pct"]) for r in rows]
        wins   = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]
        wr     = len(wins) / len(pnls) if pnls else 0.55
        aw     = sum(wins)   / len(wins)   if wins   else 0.0075
        al     = sum(losses) / len(losses) if losses else 0.005
        tc     = len(pnls)
    except Exception:
        wr, aw, al, tc = 0.55, 0.0075, 0.005, 0

    if tc < 252:
        kelly = 0.05
    else:
        loss_rate = 1.0 - wr
        kelly = max(0.0, min(((wr - loss_rate) / (aw / al if al else 1)) * 0.5, 0.25))

    raw_dollars = nav * kelly * r_mult * s_mult * conf
    size_usd    = min(raw_dollars, nav * 0.02)    # 2% NAV hard cap
    size_usd    = max(size_usd, MIN_DOLLARS)      # minimum $1 (matches execution_service)

    # Fill the order
    fill_resp = await place_xauusd_order({
        "direction":  direction,
        "size_usd":   size_usd,
        "confidence": conf,
        "regime":     regime,
        "session":    session,
    })

    if fill_resp.get("status") == "filled":
        _last_xau_fill_time = time.time()
        print(f"[xauusd_service] Signal filled: {direction} ${size_usd:.0f} "
              f"({session}) regime={regime} conf={conf:.2f} | "
              f"pos={len(xau_positions)+1}/{MAX_XAU_POSITIONS} exp={total_exp+est_pct:.1%}")

    return fill_resp


@app.post("/api/xauusd/order")
async def place_xauusd_order(payload: dict):
    """
    Place a simulated XAU/USD order.
    Fills immediately at bid (SELL) or ask (BUY) price.
    No broker. No age restriction. No SSN.
    """
    direction  = payload.get("direction")    # "BUY" or "SELL"
    size_usd   = float(payload.get("size_usd", MIN_DOLLARS))
    confidence = float(payload.get("confidence", 0.65))
    regime     = payload.get("regime", "NEUTRAL")
    session    = payload.get("session", "OVERLAP")

    if _last_price <= 0:
        return {"status": "rejected", "reason": "No price available yet"}

    # Simulated fill
    bid = _last_price * (1 - SPREAD_PCT / 2)
    ask = _last_price * (1 + SPREAD_PCT / 2)

    fill_price  = ask if direction == "BUY" else bid
    units       = round(size_usd / fill_price, 4)   # oz of gold
    stop_loss   = round(fill_price * (1 - STOP_LOSS_PCT)   if direction == "BUY"
                        else fill_price * (1 + STOP_LOSS_PCT),   4)
    take_profit = round(fill_price * (1 + TAKE_PROFIT_PCT) if direction == "BUY"
                        else fill_price * (1 - TAKE_PROFIT_PCT), 4)
    expiry_at   = datetime.now(timezone.utc) + timedelta(hours=EXPIRY_HOURS)
    opened_at   = datetime.now(timezone.utc)

    position = {
        "id":           f"xau_{int(opened_at.timestamp()*1000)}",
        "asset":        "XAU/USD",
        "direction":    direction,
        "fill_price":   fill_price,
        "units":        units,
        "size_usd":     size_usd,
        "stop_loss":    stop_loss,
        "take_profit":  take_profit,
        "expiry_at":    expiry_at.isoformat(),
        "regime":       regime,
        "session":      session,
        "confidence":   confidence,
        "slippage_pct": SPREAD_PCT / 2,
        "opened_at":    opened_at.isoformat(),
    }

    # Compute size_pct using actual current NAV (not hardcoded 100k)
    _order_nav = float(await redis_client.get("gt:nav") or 100.0)
    position["size_pct"] = round(size_usd / max(_order_nav, 1.0), 6)

    positions = json.loads(await redis_client.get("gt:xauusd_positions") or "[]")
    positions.append(position)
    await redis_client.set("gt:xauusd_positions", json.dumps(positions))

    # Log to DB — use RETURNING id so _close_xauusd_position can target exact row
    try:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO position_log
                (opened_at, asset, direction, entry_price, fill_price, size_pct,
                 stop_loss, take_profit, expiry_at, regime_at_entry,
                 confidence_at_entry, session, slippage_pct, broker)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                RETURNING id
            """, opened_at, "XAU/USD", direction, _last_price, fill_price,
                position["size_pct"],
                stop_loss, take_profit, expiry_at,
                regime, confidence, session, SPREAD_PCT / 2, "simulated")
        position["db_id"] = row["id"]
    except Exception as e:
        position["db_id"] = None
        print(f"[xauusd_service] DB log error: {e}")

    # Fire n8n notification
    asyncio.create_task(_notify_n8n(position))

    print(f"[xauusd_service] ✅ SIMULATED {direction} {units:.4f}oz XAU/USD "
          f"@ ${fill_price:.3f} | SL=${stop_loss:.3f} TP=${take_profit:.3f}")
    return {"status": "filled", "position": position}


async def check_xauusd_exits(current_price: float):
    """Check stop/target/expiry for all open XAU/USD positions."""
    raw = await redis_client.get("gt:xauusd_positions")
    if not raw:
        return
    positions = json.loads(raw)
    if not positions:
        return

    now       = datetime.now(timezone.utc)
    regime    = (await redis_client.get("gt:regime") or b"NEUTRAL").decode()
    remaining = []

    for pos in positions:
        direction   = pos["direction"]
        stop_loss   = pos["stop_loss"]
        take_profit = pos["take_profit"]
        expiry      = datetime.fromisoformat(pos["expiry_at"])
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        exit_reason = None

        if direction == "BUY":
            if current_price <= stop_loss:     exit_reason = "STOP_LOSS"
            elif current_price >= take_profit: exit_reason = "TAKE_PROFIT"
        else:  # SELL
            if current_price >= stop_loss:     exit_reason = "STOP_LOSS"
            elif current_price <= take_profit: exit_reason = "TAKE_PROFIT"

        if not exit_reason and now >= expiry:
            exit_reason = "TIME_EXPIRY"

        if not exit_reason:
            if direction == "BUY"  and regime == "BEARISH": exit_reason = "REGIME_CHANGE"
            if direction == "SELL" and regime == "BULLISH": exit_reason = "REGIME_CHANGE"

        if exit_reason:
            await _close_xauusd_position(pos, exit_reason, current_price)
        else:
            remaining.append(pos)

    await redis_client.set("gt:xauusd_positions", json.dumps(remaining))


async def _close_xauusd_position(pos: dict, exit_reason: str, exit_price: float):
    """
    Record simulated XAU/USD close.

    FIX v2: now updates gt:daily_pnl_pct and gt:nav in Redis after every close.
    Old: only wrote to TimescaleDB — Redis daily P&L was never updated for XAU/USD trades.
    Impact of old bug:
      - Dashboard always showed $0 Day P&L on XAU/USD-only trading days
      - risk_service drawdown halt (-3%) was completely blind to XAU/USD losses
      - gt:nav drifted from reality over time
    """
    opened_at = datetime.fromisoformat(pos["opened_at"])
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60

    direction  = pos["direction"]
    fill_price = pos["fill_price"]
    pnl = ((exit_price - fill_price) / fill_price
           if direction == "BUY"
           else (fill_price - exit_price) / fill_price)

    # Update TimescaleDB using stored db_id for exact row targeting.
    # FIX: old subquery (ORDER BY opened_at DESC LIMIT 1) hit the wrong row when
    # multiple XAU/USD positions were open simultaneously, corrupting exit prices.
    db_id = pos.get("db_id")
    try:
        async with db_pool.acquire() as conn:
            if db_id:
                await conn.execute("""
                    UPDATE position_log SET
                        closed_at = NOW(), exit_price = $1, exit_type = $2,
                        time_in_trade_min = $3, pnl_pct = $4
                    WHERE id = $5
                """, exit_price, exit_reason, round(mins, 2), round(pnl, 6), int(db_id))
            else:
                await conn.execute("""
                    UPDATE position_log SET
                        closed_at = NOW(), exit_price = $1, exit_type = $2,
                        time_in_trade_min = $3, pnl_pct = $4
                    WHERE id = (
                        SELECT id FROM position_log
                        WHERE asset = 'XAU/USD' AND closed_at IS NULL
                        ORDER BY opened_at ASC LIMIT 1
                    )
                """, exit_price, exit_reason, round(mins, 2), round(pnl, 6))
    except Exception as e:
        print(f"[xauusd_service] DB close error (db_id={db_id}): {e}")

    # FIX v2: update Redis daily P&L and NAV — mirrors execution_service GLD close logic
    # Without this, the dashboard Day P&L shows $0 for XAU/USD-heavy days,
    # and risk_service drawdown halt is blind to XAU/USD losses.
    try:
        nav         = float(await redis_client.get("gt:nav") or 100.0)
        size_pct    = pos.get("size_usd", MIN_DOLLARS) / nav
        current_pnl = float(await redis_client.get("gt:daily_pnl_pct") or 0)
        new_pnl     = current_pnl + (pnl * size_pct)
        await redis_client.set("gt:daily_pnl_pct", str(round(new_pnl, 6)))
        nav_change  = pnl * size_pct * nav
        await redis_client.set("gt:nav", str(round(nav + nav_change, 2)))
    except Exception as e:
        print(f"[xauusd_service] Redis P&L update error: {e}")

    print(f"[xauusd_service] CLOSED {direction} XAU/USD — "
          f"{exit_reason} @ ${exit_price:.3f} | PnL:{pnl:.2%} | {mins:.1f}min")


# ── Position monitor ──────────────────────────────────────────────────────

async def position_monitor_loop():
    """Backup monitor — checks exits every 60s in case price feed missed one."""
    while True:
        try:
            if _last_price > 0:
                await check_xauusd_exits(_last_price)
        except Exception as e:
            print(f"[xauusd_service] Monitor error: {e}")
        await asyncio.sleep(60)


# ── Continuous LSTM training ───────────────────────────────────────────────

async def continuous_training_loop():
    """
    Retrain the LSTM every RETRAIN_INTERVAL_HOURS using fresh data.
    Runs in background — never blocks trading.
    When a better model is found, hot-swaps the model file.
    signal_service reloads the model file automatically on next restart,
    or you can call /api/xauusd/reload_model to hot-swap without restart.
    """
    global _training_active, _bars_since_retrain

    # Wait 30 minutes before first training attempt
    # (let price feed collect some data first)
    print(f"[xauusd_service] Training loop started — first run in 30 min")
    await asyncio.sleep(30 * 60)

    while True:
        try:
            if not _training_active:
                if _bars_since_retrain >= MIN_NEW_BARS_TO_RETRAIN:
                    print(f"\n[xauusd_service] 🔁 Starting training cycle "
                          f"({_bars_since_retrain} new bars collected)")
                    _training_active = True

                    # Run training in a thread — it's CPU-bound and would block async
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(None, _run_training_cycle)

                    if result:
                        avg_sh, config_name = result
                        _bars_since_retrain = 0

                        msg = (f"🔁 **Retrain complete** — avg_sharpe={avg_sh:.3f} "
                               f"config={config_name}\n"
                               f"{'✅ Model deployed' if avg_sh > 0.3 else '⚠️ Below threshold — keeping old model'}")
                        print(f"[xauusd_service] {msg}")
                        _post_discord(DISCORD_REPORTS, msg)

                    _training_active = False
                else:
                    remaining = MIN_NEW_BARS_TO_RETRAIN - _bars_since_retrain
                    print(f"[xauusd_service] Training check: "
                          f"{_bars_since_retrain}/{MIN_NEW_BARS_TO_RETRAIN} new bars "
                          f"({remaining} more needed)")

        except Exception as e:
            print(f"[xauusd_service] Training loop error: {e}")
            _training_active = False

        # Check every hour whether it's time to retrain
        await asyncio.sleep(RETRAIN_INTERVAL_HOURS * 3600)


def _run_training_cycle() -> tuple[float, str] | None:
    """
    Synchronous training — runs in thread pool.
    Returns (avg_sharpe, config_name) of best model, or None if failed.
    Writes new model to MODEL_NEW_FILE first, then renames to MODEL_FILE.
    This ensures signal_service never reads a half-written model file.
    """
    try:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[training] Device: {device}")

        # Load all available data — historical + live XAU/USD bars
        dfs = {}

        if os.path.exists(DATA_5MIN):
            df_hist = pd.read_csv(DATA_5MIN, index_col="timestamp", parse_dates=True)
            # Append live XAUUSD bars if available
            if os.path.exists(XAUUSD_FILE):
                df_live = pd.read_csv(XAUUSD_FILE, index_col="timestamp", parse_dates=True)
                df_combined = pd.concat([df_hist, df_live])
                df_combined = df_combined[~df_combined.index.duplicated(keep="last")]
                df_combined = df_combined.sort_index()
                dfs["5min"] = df_combined
                print(f"[training] 5min data: {len(df_hist)} hist + {len(df_live)} live "
                      f"= {len(df_combined)} total bars")
            else:
                dfs["5min"] = df_hist
                print(f"[training] 5min data: {len(df_hist)} bars (no live data yet)")

        if not dfs:
            print("[training] No data available")
            return None

        # Single config — the best one from v5 testing
        cfg = {
            "name":       "continuous",
            "data":       "5min",
            "forward":    TRAIN_FORWARD,
            "window":     TRAIN_WINDOW,
            "hidden":     TRAIN_HIDDEN,
            "percentile": TRAIN_PERCENTILE,
            "lr":         TRAIN_LR,
            "epochs":     TRAIN_EPOCHS,
        }

        avg_sh, best_st = _train_single_config(cfg, dfs, device)
        deployed = False
        # Deployment guard: reject if avg too high (overfit) OR any single fold too high
        DEPLOY_MIN   = 0.3    # minimum avg Sharpe to deploy
        DEPLOY_MAX   = 2.0    # maximum avg Sharpe before suspecting overfit
        FOLD_MAX     = 2.0    # maximum any single fold Sharpe allowed
        any_fold_overfit = any(s > FOLD_MAX for s in _last_continuous_fold_sharpes)
        avg_overfit      = avg_sh > DEPLOY_MAX

        if best_st is not None and avg_sh > DEPLOY_MIN and not any_fold_overfit and not avg_overfit:
            m = LSTMModel(TRAIN_HIDDEN)
            m.load_state_dict(best_st)
            torch.save(m.state_dict(), MODEL_NEW_FILE)
            os.replace(MODEL_NEW_FILE, MODEL_FILE)
            deployed = True
            print(f"[training] Model deployed avg_sharpe={avg_sh:.3f}")
        elif any_fold_overfit:
            print(f"[training] BLOCKED: a fold exceeded {FOLD_MAX} (folds={_last_continuous_fold_sharpes}) — overfitting")
        elif avg_overfit:
            print(f"[training] BLOCKED: avg_sharpe={avg_sh:.3f} > {DEPLOY_MAX} — suspicious, not deploying")
        else:
            print(f"[training] avg_sharpe={avg_sh:.3f} below {DEPLOY_MIN} — keeping existing model")

        # Collect data stats for training report
        total_bars = len(dfs["5min"]) if "5min" in dfs else 0
        live_bars = 0
        live_trades = 0
        try:
            if os.path.exists(XAUUSD_FILE): live_bars = len(pd.read_csv(XAUUSD_FILE))
            if os.path.exists(LIVE_TRADES): live_trades = len(pd.read_csv(LIVE_TRADES))
        except Exception: pass

        write_training_report(
            source="continuous", config_name=cfg["name"],
            fold_sharpes=_last_continuous_fold_sharpes,
            avg_sharpe=avg_sh, deployed=deployed,
            data_info={"bars_5min": total_bars, "live_xauusd_bars": live_bars,
                       "live_trades": live_trades, "forward": TRAIN_FORWARD,
                       "window": TRAIN_WINDOW, "percentile": TRAIN_PERCENTILE,
                       "device": str(device)},
            extra={"epochs": TRAIN_EPOCHS, "sim_high": SIM_HIGH, "sim_low": SIM_LOW,
                   "retrain_every_h": RETRAIN_INTERVAL_HOURS}
        )

        return avg_sh, cfg["name"]

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[training] Cycle error: {e}")
        write_training_report(source="continuous", config_name="continuous",
            fold_sharpes=[], avg_sharpe=-999.0, deployed=False,
            data_info={}, error=f"{e}\n{tb}")
        return None


def _train_single_config(cfg: dict, dfs: dict, device) -> tuple[float, dict | None]:
    """
    Streamlined single-config walk-forward training.
    Returns (avg_sharpe, best_state_dict).
    """
    df     = dfs[cfg["data"]]
    window = cfg["window"]
    fwd    = cfg["forward"]
    hidden = cfg["hidden"]
    epochs = cfg["epochs"]
    lr     = cfg["lr"]
    pct    = cfg["percentile"]
    tf     = cfg["data"]

    n  = len(df)
    fs = n // 4

    fold_sharpes = []
    best_sh      = -999.0
    best_st      = None

    for fold in range(3):
        train_end = fs * (fold + 1)
        val_start = train_end + fwd
        val_end   = min(val_start + fs, n)

        if val_end - val_start < 200:
            continue

        closes = df["close"].values
        thr    = _threshold_from_slice(closes, 0, train_end, fwd, pct)

        tX, tXe, tY = _prepare(df, 0,         train_end, window, fwd, thr)
        vX, vXe, vY = _prepare(df, val_start, val_end,   window, fwd, thr)

        if len(tY) < 200 or len(vY) < 50:
            continue

        up = (tY==0).sum(); fl = (tY==1).sum(); dn = (tY==2).sum()
        print(f"[training] Fold {fold+1} | train={len(tY)} "
              f"UP={up/len(tY):.0%} FLAT={fl/len(tY):.0%} DOWN={dn/len(tY):.0%} "
              f"| val={len(vY)}")

        cnt     = np.bincount(tY, minlength=3)
        sw      = (1.0 / (cnt + 1e-8))[tY]
        sampler = WeightedRandomSampler(sw, num_samples=min(len(tY), 40000), replacement=True)
        tl = DataLoader(_DS(tX, tXe, tY), batch_size=BATCH_SIZE, sampler=sampler)
        vl = DataLoader(_DS(vX, vXe, vY), batch_size=BATCH_SIZE, shuffle=False)

        model = LSTMModel(TRAIN_HIDDEN).to(device)
        cw    = torch.tensor([2.0, 1.0, 2.0]).to(device)
        crit  = nn.CrossEntropyLoss(weight=cw)
        opt   = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-5)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)

        fold_best = -999.0
        fold_st   = None
        no_imp    = 0

        for ep in range(epochs):
            model.train()
            for x, e, y in tl:
                x, e, y = x.to(device), e.to(device), y.to(device)
                opt.zero_grad()
                crit(model(x, e), y).backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                opt.step()
            sched.step()

            rets, acc, n_tr = _sim_returns(model, vl, device)
            # FIX: annualise by trade frequency, not bars per year
            # Old sqrt(19656)~140x multiplier produced fake Sharpe of 14+
            if len(rets) >= 10:
                r = np.array(rets)
                bpy_bars        = 252 * 78 if tf == "5min" else 252 * 390
                trades_per_year = (len(r) / max(val_end - val_start, 1)) * bpy_bars
                ann_factor      = np.sqrt(max(trades_per_year, 1))
                sh = float(r.mean() / (r.std() + 1e-8) * ann_factor)
            else:
                sh = 0.0

            if sh > fold_best:
                fold_best = sh
                fold_st   = {k: v.clone() for k, v in model.state_dict().items()}
                no_imp    = 0
            else:
                no_imp += 1

            if (ep + 1) % 20 == 0:
                print(f"[training]   Ep {ep+1}/{epochs} "
                      f"Sharpe:{sh:.3f} Best:{fold_best:.3f} "
                      f"Trades:{n_tr} Acc:{acc:.1%}")

            if no_imp >= TRAIN_EARLY_STOP:
                print(f"[training]   Early stop at ep {ep+1}")
                break
            if sh > 2.0:  # corrected threshold on trade-annualised scale
                print(f"[training]   Overfit guard at {sh:.2f}")
                break

        fold_sharpes.append(fold_best)
        print(f"[training] Fold {fold+1} best: {fold_best:.3f}")

        if fold_best > best_sh:
            best_sh = fold_best
            best_st = fold_st

    valid = [s for s in fold_sharpes if s > -100]
    avg   = float(np.mean(valid)) if valid else -999.0
    print(f"[training] Avg Sharpe: {avg:.3f} | Folds: {[round(s,2) for s in fold_sharpes]}")
    global _last_continuous_fold_sharpes
    _last_continuous_fold_sharpes = fold_sharpes[:]
    return avg, best_st


# ── ML helpers ────────────────────────────────────────────────────────────

class LSTMModel(nn.Module):
    def __init__(self, hidden=64):
        super().__init__()
        self.lstm = nn.LSTM(input_size=4, hidden_size=hidden,
                            num_layers=2, batch_first=True, dropout=0.2)
        self.attn = nn.Linear(hidden, 1)
        self.fc   = nn.Linear(hidden + 6, 3)
        self.soft = nn.Softmax(dim=-1)

    def forward(self, x, extra):
        out, _ = self.lstm(x)
        a   = torch.softmax(self.attn(out), dim=1)
        ctx = (a * out).sum(dim=1)
        return self.soft(self.fc(torch.cat([ctx, extra], dim=-1)))


class _DS(Dataset):
    def __init__(self, X, e, y):
        self.X = torch.tensor(X, dtype=torch.float32)
        self.e = torch.tensor(e, dtype=torch.float32)
        self.y = torch.tensor(y, dtype=torch.long)
    def __len__(self):        return len(self.y)
    def __getitem__(self, i): return self.X[i], self.e[i], self.y[i]


def _ema(arr, p):
    k = 2 / (p + 1); out = np.zeros(len(arr)); out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i-1] * (1 - k)
    return out

def _session_enc(h):
    if 0 <= h < 7:    return [1,0,0,0]
    elif 7 <= h < 13: return [0,1,0,0]
    elif 13 <= h < 17:return [0,0,1,0]
    elif 17 <= h < 21:return [0,0,0,1]
    return [0,0,0,0]

def _threshold_from_slice(closes, start, end, fwd, pct):
    rets = [abs((closes[i+fwd]-closes[i])/closes[i]) for i in range(start, end-fwd)]
    return float(np.percentile(rets, pct))

def _make_label(closes, i, fwd, thr):
    if i+fwd >= len(closes): return 1
    r = (closes[i+fwd]-closes[i])/closes[i]
    return 0 if r > thr else (2 if r < -thr else 1)

def _prepare(df, s, e, window, fwd, thr):
    closes = df["close"].values; vols = df["volume"].values
    hours  = df.index.hour
    e9 = _ema(closes,9); e21 = _ema(closes,21)
    rm = pd.Series(closes).rolling(20).mean().values
    rs = pd.Series(closes).rolling(20).std().values
    X, Xe, Y = [], [], []
    for i in range(max(s, window+21), e-fwd):
        wc = closes[i-window:i]; wv = vols[i-window:i]
        ed = (e9[i-window:i]-e21[i-window:i])/(closes[i]+1e-8)
        std = rs[i] if rs[i]>0 else 1e-8
        bb  = (wc-rm[i])/std
        np_ = (wc-wc.mean())/(wc.std()+1e-8)
        nv  = (wv-wv.mean())/(wv.std()+1e-8)
        Xe.append([0.0]+_session_enc(int(hours[i]))+[1.0])
        X.append(np.stack([np_,nv,ed,bb],axis=-1))
        Y.append(_make_label(closes,i,fwd,thr))
    return (np.array(X,dtype=np.float32),
            np.array(Xe,dtype=np.float32),
            np.array(Y,dtype=np.int64))

def _sim_returns(model, loader, device):
    model.eval(); rets=[]; correct=total=0
    with torch.no_grad():
        for x,e,y in loader:
            p   = model(x.to(device),e.to(device))
            pu  = p[:,0].cpu().numpy()
            pd_ = p[:,2].cpu().numpy()
            yt  = y.numpy()
            for i in range(len(pu)):
                if pu[i]>=SIM_HIGH and pd_[i]<=SIM_LOW:
                    hit=yt[i]==0; rets.append(0.0015 if hit else -0.0007)
                    correct+=int(hit); total+=1
                elif pd_[i]>=SIM_HIGH and pu[i]<=SIM_LOW:
                    hit=yt[i]==2; rets.append(0.0015 if hit else -0.0007)
                    correct+=int(hit); total+=1
    return rets, (correct/total if total>0 else 0), total


# ── Helpers ───────────────────────────────────────────────────────────────

def _post_discord(webhook, msg):
    if not webhook: return
    try: req_sync.post(webhook, json={"content": msg}, timeout=5)
    except Exception: pass

async def _notify_n8n(fill_data):
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(N8N_TRADE, json=fill_data)
    except Exception: pass


# ── API endpoints ─────────────────────────────────────────────────────────

@app.get("/api/xauusd/price")
async def get_price():
    return {
        "price": _last_price,
        "bid":   round(_last_price * (1 - SPREAD_PCT/2), 3) if _last_price else 0,
        "ask":   round(_last_price * (1 + SPREAD_PCT/2), 3) if _last_price else 0,
        "ts":    datetime.now(timezone.utc).isoformat()
    }


@app.post("/api/xauusd/close")
async def close_xauusd_position(payload: dict):
    """Close a specific XAU/USD position by ID, or all positions if no ID given."""
    pos_id = payload.get("position_id")
    raw    = await redis_client.get("gt:xauusd_positions")
    positions = json.loads(raw) if raw else []

    closed_count = 0
    remaining    = []
    for pos in positions:
        if pos_id is None or pos.get("id") == pos_id:
            price = _last_price if _last_price > 0 else pos["fill_price"]
            await _close_xauusd_position(pos, "MANUAL_SHUTDOWN", price)
            closed_count += 1
        else:
            remaining.append(pos)

    await redis_client.set("gt:xauusd_positions", json.dumps(remaining))
    return {"status": "ok", "closed": closed_count, "remaining": len(remaining)}

@app.get("/api/xauusd/positions")
async def get_positions():
    raw = await redis_client.get("gt:xauusd_positions")
    return {"positions": json.loads(raw) if raw else []}

@app.post("/api/xauusd/reload_model")
async def reload_model():
    """Hot-reload the LSTM model in signal_service without restarting it."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post("http://localhost:8004/api/signal/reload_model")
            return r.json()
    except Exception as e:
        return {"status": "error", "reason": str(e)}

@app.get("/api/xauusd/training_status")
async def training_status():
    return {
        "training_active":    _training_active,
        "bars_since_retrain": _bars_since_retrain,
        "bars_needed":        MIN_NEW_BARS_TO_RETRAIN,
        "retrain_interval_h": RETRAIN_INTERVAL_HOURS,
        "model_exists":       os.path.exists(MODEL_FILE),
        "model_age_h":        round(
            (time.time() - os.path.getmtime(MODEL_FILE)) / 3600, 1
        ) if os.path.exists(MODEL_FILE) else None,
        "max_positions":      MAX_XAU_POSITIONS,
    }

@app.get("/api/health")
async def health():
    start = time.time()
    await redis_client.ping()
    raw     = await redis_client.get("gt:xauusd_positions")
    gld_raw = await redis_client.get("gt:open_positions")
    xau_pos = json.loads(raw) if raw else []
    gld_pos = json.loads(gld_raw) if gld_raw else []
    nav     = float(await redis_client.get("gt:nav") or 100.0)
    xau_exp = sum(p.get("size_usd", MIN_DOLLARS) / nav for p in xau_pos)
    gld_exp = sum(p.get("size_pct", 0) for p in gld_pos)
    return {
        "status":             "healthy",
        "service":            "xauusd_service",
        "latency_ms":         round((time.time()-start)*1000, 2),
        "last_price":         _last_price,
        "open_positions":     len(xau_pos),
        "max_positions":      MAX_XAU_POSITIONS,
        "xau_exposure_pct":   round(xau_exp * 100, 2),
        "gld_exposure_pct":   round(gld_exp * 100, 2),
        "combined_exp_pct":   round((xau_exp + gld_exp) * 100, 2),
        "training_active":    _training_active,
        "bars_since_retrain": _bars_since_retrain,
    }



