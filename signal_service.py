"""
signal_service.py — Port 8004
LSTM signal generation + rule-based fallback. Outputs P(UP), P(FLAT), P(DOWN) per tick.

FIX v2:
  - MODEL_CONFIDENCE_COLLAPSE detection reworked for rule-based mode.
FIX v3:
  - Dual signal routing: GLD (Alpaca, US hours) + XAU/USD (simulated, all sessions).
  - XAUUSD_URL added. _send_signal now fires at both brokers simultaneously.
  - GLD signals only during OVERLAP + NY_AFTERNOON.
  - XAU/USD signals during all sessions except CLOSED (adds Asian + London hours).
"""
import os, time, asyncio, json
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg
import httpx
import numpy as np

load_dotenv()

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

ALPACA_HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET
}

TICK_WINDOW             = 60
EXECUTION_URL           = "http://localhost:8003"   # GLD via simulated fills (execution_service)
XAUUSD_URL              = "http://localhost:8005"   # XAU/USD simulated (all sessions)
QUOTE_STALENESS_SECONDS = 60

# Collapse thresholds
LSTM_COLLAPSE_THRESHOLD = 0.30
RULE_FLIP_THRESHOLD     = 0.60

redis_client          = None
db_pool               = None
price_buffer          = []
signal_window_stats   = []
direction_flip_stats  = []
model                 = None
_market_closed_logged = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool, model
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5
    )
    model = _load_model()
    await redis_client.delete("gt:collapse_alert")
    halt_reason = await redis_client.get("gt:halt_reason")
    if halt_reason and halt_reason.decode() == "MODEL_CONFIDENCE_COLLAPSE":
        await redis_client.delete("gt:system_halted")
        await redis_client.delete("gt:halt_reason")
        print("[signal_service] Cleared MODEL_CONFIDENCE_COLLAPSE halt from previous session")
    asyncio.create_task(tick_feed_loop())
    mode = "LSTM" if model else "rule-based fallback"
    print(f"[signal_service] Started. Model: {mode}")
    print(f"[signal_service] GLD feed: Alpaca (OVERLAP + NY_AFTERNOON only)")
    print(f"[signal_service] XAU/USD feed: simulated via xauusd_service (all sessions)")
    yield
    await redis_client.close()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)


# ── Model loading ──────────────────────────────────────────────────────────

def _load_model():
    try:
        import torch
        import torch.nn as nn
        model_path = r"D:\golden-trinity\model\lstm_model.pt"
        if not os.path.exists(model_path):
            return None
        state            = torch.load(model_path, map_location="cpu")
        saved_input_size = state["lstm.weight_ih_l0"].shape[1]

        class LSTMModel(nn.Module):
            def __init__(self, input_size):
                super().__init__()
                self.lstm    = nn.LSTM(input_size=input_size, hidden_size=64,
                                       num_layers=2, batch_first=True)
                self.attn    = nn.Linear(64, 1)
                self.fc      = nn.Linear(64 + 6, 3)
                self.softmax = nn.Softmax(dim=-1)
            def forward(self, x, extra):
                out, _  = self.lstm(x)
                attn_w  = torch.softmax(self.attn(out), dim=1)
                ctx     = (attn_w * out).sum(dim=1)
                return self.softmax(self.fc(torch.cat([ctx, extra], dim=-1)))

        m = LSTMModel(input_size=saved_input_size)
        m.load_state_dict(state)
        m.eval()
        print(f"[signal_service] LSTM loaded (input_size={saved_input_size})")
        return m
    except Exception as e:
        print(f"[signal_service] Model load skipped: {e}")
        return None


# ── Tick feed ──────────────────────────────────────────────────────────────

async def tick_feed_loop():
    global _market_closed_logged
    while True:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{ALPACA_DATA}/v2/stocks/GLD/quotes/latest",
                    headers=ALPACA_HEADERS
                )
                if r.status_code == 200:
                    data  = r.json()
                    quote = data.get("quote", {})
                    bid   = float(quote.get("bp", 0))
                    ask   = float(quote.get("ap", 0))

                    if bid <= 0 or ask <= 0:
                        if not _market_closed_logged:
                            print(f"[signal_service] GLD market closed (bid/ask = 0)")
                            _market_closed_logged = True
                        # Even when GLD is closed, generate signals for XAU/USD
                        # using last known price from xauusd_service
                        await _process_xauusd_only_tick()
                    else:
                        quote_ts_str = quote.get("t") or quote.get("qt")
                        stale = False
                        if quote_ts_str:
                            try:
                                quote_ts = datetime.fromisoformat(
                                    quote_ts_str.replace("Z", "+00:00"))
                                age_secs = (datetime.now(timezone.utc) - quote_ts).total_seconds()
                                stale = age_secs > QUOTE_STALENESS_SECONDS
                            except Exception:
                                pass

                        if stale:
                            if not _market_closed_logged:
                                print(f"[signal_service] GLD market closed — stale quote")
                                _market_closed_logged = True
                            await _process_xauusd_only_tick()
                        else:
                            if _market_closed_logged:
                                print(f"[signal_service] GLD market open — resuming")
                                _market_closed_logged = False
                                price_buffer.clear()
                            mid = (bid + ask) / 2
                            vol = float(quote.get("bs", 0)) + float(quote.get("as", 0))
                            await process_tick(mid, vol, bid, ask)
                else:
                    print(f"[signal_service] Quote HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"[signal_service] Tick feed error: {e}")
        await asyncio.sleep(5)


async def _process_xauusd_only_tick():
    """
    When GLD market is closed, pull the current XAU/USD price from xauusd_service
    and run signals against that. Allows Asian + London session trading.
    """
    try:
        xau_raw = await redis_client.get("gt:xauusd_price")
        if not xau_raw:
            return
        xau_price = float(xau_raw)
        if xau_price <= 0:
            return
        # Use XAU/USD price as synthetic GLD-equivalent tick
        # (GVZ/VIX and macro regime still apply — same signal logic)
        spread  = xau_price * 0.0003
        bid_xau = xau_price - spread / 2
        ask_xau = xau_price + spread / 2
        await process_tick(xau_price, 1.0, bid_xau, ask_xau, xauusd_only=True)
    except Exception as e:
        print(f"[signal_service] XAU/USD-only tick error: {e}")


async def process_tick(price: float, volume: float, bid: float, ask: float,
                       xauusd_only: bool = False):
    global price_buffer, signal_window_stats, direction_flip_stats
    now = datetime.now(timezone.utc)

    price_buffer.append({"price": price, "volume": volume, "timestamp": now.isoformat()})
    if len(price_buffer) > TICK_WINDOW:
        price_buffer = price_buffer[-TICK_WINDOW:]

    await redis_client.xadd("gt:ticks:gld", {
        "price": str(price), "bid": str(bid), "ask": str(ask),
        "volume": str(volume), "timestamp": str(int(now.timestamp() * 1000))
    })
    await redis_client.xtrim("gt:ticks:gld", maxlen=1000)

    if len(price_buffer) < TICK_WINDOW:
        return

    p_up, p_flat, p_down = await generate_signal()
    passed, direction    = passes_gate(p_up, p_flat, p_down)

    await _update_collapse_detection(p_up, p_flat, p_down, passed, direction, now)

    signal = {
        "timestamp":    now.isoformat(),
        "asset":        "GLD",
        "direction":    direction,
        "p_up":         p_up,
        "p_flat":       p_flat,
        "p_down":       p_down,
        "passed_gate":  passed,
        "price":        price,
        "xauusd_only":  xauusd_only,
    }
    await redis_client.set("gt:signal:current", json.dumps(signal))

    regime  = (await redis_client.get("gt:regime") or b"NEUTRAL").decode()
    session = get_current_session()

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO signal_log
                (timestamp, asset, direction, confidence_up, confidence_flat,
                 confidence_down, regime, session, passed_gate)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """, now, "GLD", direction, p_up, p_flat, p_down, regime, session, passed)
    except Exception as e:
        print(f"[signal_service] DB log error: {e}")

    if passed:
        print(f"[signal_service] \U0001f514 SIGNAL {direction} "
              f"({'XAU/USD only' if xauusd_only else 'GLD+XAU/USD'}) — "
              f"P(UP)={p_up:.2f} P(DOWN)={p_down:.2f} @ ${price:.2f} [{session}]")
        asyncio.create_task(
            _send_signal(signal, regime, session, price, bid, ask, xauusd_only))


async def _update_collapse_detection(p_up, p_flat, p_down, passed, direction, now):
    cutoff = now.timestamp() - 300

    if model is not None:
        signal_window_stats.append({"passed": passed, "time": now.isoformat()})
        signal_window_stats[:] = [
            s for s in signal_window_stats
            if datetime.fromisoformat(s["time"]).replace(tzinfo=timezone.utc).timestamp() > cutoff
        ]
        if len(signal_window_stats) >= 30:
            fail_rate = sum(1 for s in signal_window_stats if not s["passed"]) / len(signal_window_stats)
            if fail_rate > LSTM_COLLAPSE_THRESHOLD:
                await redis_client.set("gt:collapse_alert", "true")
            else:
                await redis_client.delete("gt:collapse_alert")
        if passed:
            signal_window_stats.clear()
    else:
        if direction is not None:
            direction_flip_stats.append({"direction": direction, "time": now.isoformat()})
        direction_flip_stats[:] = [
            s for s in direction_flip_stats
            if datetime.fromisoformat(s["time"]).replace(tzinfo=timezone.utc).timestamp() > cutoff
        ]
        if len(direction_flip_stats) >= 10:
            dirs  = [s["direction"] for s in direction_flip_stats]
            flips = sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i-1])
            flip_rate = flips / max(len(dirs) - 1, 1)
            if flip_rate > RULE_FLIP_THRESHOLD:
                await redis_client.set("gt:collapse_alert", "true")
            else:
                await redis_client.delete("gt:collapse_alert")
        if passed:
            direction_flip_stats.clear()


async def _send_signal(signal: dict, regime: str, session: str, price: float,
                       bid: float, ask: float, xauusd_only: bool = False):
    """
    Route signals to the right broker based on session:
      GLD via Alpaca (execution_service):  OVERLAP + NY_AFTERNOON only (US hours)
      XAU/USD simulated (xauusd_service):  ALL sessions except CLOSED (23h/day)
    Both run independent risk checks and Kelly sizing on their side.
    """
    spread_pct = (ask - bid) / price if price > 0 else 1
    direction  = signal["direction"]

    # Regime-direction alignment: don't trade against a confirmed macro regime.
    # BULLISH regime means macro indicators favour rising gold — SELL signals into
    # that regime fight the trend and are the primary driver of stop-loss exits.
    # NEUTRAL: take both directions.
    if regime == "BULLISH" and direction == "SELL":
        return   # don't short into a bullish macro regime
    if regime == "BEARISH" and direction == "BUY":
        return   # don't go long into a bearish macro regime

    # ── GLD via simulated fills — US market hours only ────────────────────
    if not xauusd_only and session in ("OVERLAP", "NY_AFTERNOON"):
        gld_payload = {
            "asset":      "GLD",
            "direction":  direction,
            "p_up":       signal["p_up"],
            "p_down":     signal["p_down"],
            "p_flat":     signal["p_flat"],
            "spread_pct": spread_pct,
            "regime":     regime,
            "session":    session,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r      = await client.post(f"{EXECUTION_URL}/api/execution/order",
                                           json=gld_payload)
                result = r.json()
                status = result.get("status")
                reason = result.get("reason", "")
                if status == "filled":
                    print(f"[signal_service] \u2705 GLD order filled")
                elif status not in ("rejected", "skipped"):
                    print(f"[signal_service] \u26a0\ufe0f  GLD {status}: {reason}")
        except Exception as e:
            print(f"[signal_service] GLD signal error: {e}")

    # ── XAU/USD simulated — ALL non-closed sessions (23h/day) ─────────────
    if session != "CLOSED":
        xau_payload = {
            "direction":  direction,
            "p_up":       signal["p_up"],
            "p_down":     signal["p_down"],
            "p_flat":     signal["p_flat"],
            "spread_pct": spread_pct,
            "regime":     regime,
            "session":    session,
            "price":      price,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r      = await client.post(f"{XAUUSD_URL}/api/xauusd/signal",
                                           json=xau_payload)
                result = r.json()
                status = result.get("status")
                reason = result.get("reason", "")
                if status == "filled":
                    print(f"[signal_service] \u2705 XAU/USD order filled ({session})")
                elif status not in ("rejected", "skipped"):
                    print(f"[signal_service] \u26a0\ufe0f  XAU/USD {status}: {reason}")
        except Exception as e:
            print(f"[signal_service] XAU/USD signal error: {e}")


# ── Signal generation ──────────────────────────────────────────────────────

async def generate_signal() -> tuple[float, float, float]:
    if model is not None:
        return await _lstm_signal()
    return _rule_based_signal()

async def _lstm_signal() -> tuple[float, float, float]:
    try:
        import torch
        prices  = np.array([t["price"]  for t in price_buffer[-TICK_WINDOW:]], dtype=np.float32)
        volumes = np.array([t["volume"] for t in price_buffer[-TICK_WINDOW:]], dtype=np.float32)
        prices  = (prices - prices.mean()) / (prices.std() + 1e-8)
        volumes = (volumes - volumes.mean()) / (volumes.std() + 1e-8)

        input_size = model.lstm.input_size if hasattr(model.lstm, "input_size") else 4
        if input_size == 2:
            features = np.stack([prices, volumes], axis=-1)
        else:
            ema9  = _ema(prices, 9)
            ema21 = _ema(prices, 21)
            ed    = ema9 - ema21
            bb    = (prices - prices.mean()) / (prices.std() + 1e-8)
            features = np.stack([prices, volumes, ed, bb], axis=-1)

        x       = torch.tensor(features).unsqueeze(0)
        regime  = (await redis_client.get("gt:regime") or b"NEUTRAL").decode()
        r_emb   = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}.get(regime, 0.0)
        session = get_current_session()
        s_enc   = [1 if session == s else 0
                   for s in ["ASIAN", "LONDON", "OVERLAP", "NY_AFTERNOON"]]
        snap_raw = await redis_client.get("gt:macro_snapshot")
        gvz_vix  = 1.0
        if snap_raw:
            snap    = json.loads(snap_raw)
            gvz_vix = float(snap.get("gvz_vix_ratio") or 1.0)
        extra = torch.tensor([[r_emb] + s_enc + [gvz_vix]], dtype=torch.float32)
        with torch.no_grad():
            out = model(x, extra).squeeze().numpy()
        return float(out[0]), float(out[1]), float(out[2])
    except Exception as e:
        print(f"[signal_service] LSTM inference error: {e}")
        return _rule_based_signal()

def _rsi(prices: np.ndarray, period: int = 14) -> float:
    """RSI-14: <35 = oversold (bullish entry), >65 = overbought (bearish entry)."""
    if len(prices) < period + 1:
        return 50.0
    deltas   = np.diff(prices)
    gains    = np.where(deltas > 0, deltas, 0.0)
    losses   = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[-period:].mean()
    avg_loss = losses[-period:].mean()
    if avg_loss == 0:
        return 100.0
    return float(100 - (100 / (1 + avg_gain / avg_loss)))


def _rule_based_signal() -> tuple[float, float, float]:
    if len(price_buffer) < 21:
        return 0.33, 0.34, 0.33

    prices = np.array([t["price"] for t in price_buffer])
    n      = len(prices)

    # 1. EMA 9/21 crossover
    ema9      = _ema(prices, 9)
    ema21     = _ema(prices, 21)
    ema_score = 1 if ema9[-1] > ema21[-1] else -1

    # 2. Momentum (15-tick)
    lookback  = min(15, n - 1)
    mom       = (prices[-1] - prices[-lookback]) / (prices[-lookback] + 1e-8)
    mom_score = 1 if mom > 0.0003 else (-1 if mom < -0.0003 else 0)

    # 3. Price vs 20-tick SMA
    sma20     = prices[-20:].mean() if n >= 20 else prices.mean()
    sma_score = 1 if prices[-1] > sma20 else -1

    # 4. Acceleration (momentum of momentum)
    if n >= 10:
        recent      = (prices[-1] - prices[-5])  / (prices[-5]  + 1e-8)
        older       = (prices[-5] - prices[-10]) / (prices[-10] + 1e-8)
        accel_score = 1 if recent > older else -1
    else:
        accel_score = 0

    # 5. RSI-14: oversold confirms BUY, overbought confirms SELL
    # Buying into oversold RSI and selling into overbought RSI improves
    # entry timing vs. buying/selling into already-extended moves.
    rsi       = _rsi(prices, 14)
    rsi_score = 1 if rsi < 35 else (-1 if rsi > 65 else 0)

    total = ema_score + mom_score + sma_score + accel_score + rsi_score
    # Range: -5 to +5.
    # Threshold raised from 3/4 to 4/5 — requires stronger consensus.
    # Fewer trades but much lower stop-loss rate.

    if   total >= 4:  p_up, p_flat, p_down = 0.70, 0.15, 0.15
    elif total == 3:  p_up, p_flat, p_down = 0.52, 0.28, 0.20
    elif total == 2:  p_up, p_flat, p_down = 0.42, 0.35, 0.23
    elif total == 1:  p_up, p_flat, p_down = 0.35, 0.35, 0.30
    elif total == 0:  p_up, p_flat, p_down = 0.33, 0.34, 0.33
    elif total == -1: p_up, p_flat, p_down = 0.30, 0.35, 0.35
    elif total == -2: p_up, p_flat, p_down = 0.23, 0.35, 0.42
    elif total == -3: p_up, p_flat, p_down = 0.20, 0.28, 0.52
    else:             p_up, p_flat, p_down = 0.15, 0.15, 0.70

    t = p_up + p_flat + p_down
    return round(p_up/t, 4), round(p_flat/t, 4), round(p_down/t, 4)


def _ema(prices: np.ndarray, period: int) -> np.ndarray:
    k   = 2 / (period + 1)
    ema = np.zeros(len(prices))
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = prices[i] * k + ema[i-1] * (1 - k)
    return ema

def passes_gate(p_up: float, p_flat: float, p_down: float) -> tuple[bool, str | None]:
    if p_up >= 0.60 and p_down <= 0.20:
        return True, "BUY"
    if p_down >= 0.60 and p_up <= 0.20:
        return True, "SELL"
    return False, None

def get_current_session() -> str:
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 7:     return "ASIAN"
    elif 7 <= h < 13:  return "LONDON"
    elif 13 <= h < 17: return "OVERLAP"
    elif 17 <= h < 21: return "NY_AFTERNOON"
    else:              return "CLOSED"


# ── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/signal/current")
async def current_signal():
    data = await redis_client.get("gt:signal:current")
    return json.loads(data) if data else {"signal": None}

@app.post("/api/signal/reload_model")
async def reload_model():
    global model
    model = _load_model()
    return {"status": "ok",
            "model": "LSTM loaded" if model else "rule-based (no model file found)"}

@app.get("/api/metrics/daily")
async def daily_metrics():
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT passed_gate FROM signal_log "
                "WHERE timestamp >= NOW() - INTERVAL '24 hours'")
        total   = len(rows)
        passed  = sum(1 for r in rows if r["passed_gate"])
        collapse = await redis_client.get("gt:collapse_alert")
        return {
            "total_signals":  total,
            "passed_signals": passed,
            "pass_rate":      round(passed / total, 4) if total > 0 else 0,
            "collapse_alert": bool(collapse),
            "model":          "LSTM" if model else "rule-based",
        }
    except Exception as e:
        return {"total_signals": 0, "passed_signals": 0, "pass_rate": 0, "error": str(e)}

@app.get("/api/health")
async def health():
    start      = time.time()
    await redis_client.ping()
    latency_ms = round((time.time() - start) * 1000, 2)
    collapse   = await redis_client.get("gt:collapse_alert")
    return {
        "status":         "healthy",
        "service":        "signal_service",
        "latency_ms":     latency_ms,
        "model":          "LSTM" if model else "rule-based-fallback",
        "buffer_size":    len(price_buffer),
        "market_closed":  _market_closed_logged,
        "collapse_alert": bool(collapse),
    }
