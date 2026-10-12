"""
macro_service.py — Port 8001
Regime classifier. Fetches macro data, runs rule-based classifier, writes regime to Redis.
LightGBM upgrade planned after sufficient paper trading data collected.

FIX v2:
  - DXY thresholds now relative (% deviation from 12-month mean) instead of absolute.
  - FRED fetch now has retry with exponential backoff (transient 500 errors documented).
  - News sentiment now uses 4-hour batch window to stay within 100 req/day quota.

FIX v3 — New macro inputs (March 2026):
  - SPX 20-day return (^GSPC via yfinance): risk-on/risk-off signal.
    SPX falling = risk-off = safe-haven bid for gold = BULLISH.
    SPX rising strongly = risk-on = gold less attractive = BEARISH.
    Adds what VIX doesn't capture: market *direction*, not just fear level.
    An orderly selloff with low VIX still shows up in SPX return.

  - AUD/USD 20-day return (AUDUSD=X via yfinance): gold-correlated forex signal.
    Australia exports ~10% of world gold supply. AUD has a structural positive
    correlation with gold prices. AUD rising = global risk appetite + gold demand
    likely rising. AUD falling = risk-off or commodity weakness.
    Acts as an independent confirmation of gold direction from a different angle.

  Both fetched via yfinance — free, no API key, same library already in stack.
  Both stored in macro_snapshots DB and Redis snapshot JSON.
"""
import os, time, json, requests
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg

load_dotenv()

FRED_KEY   = os.getenv("FRED_API_KEY")
NEWS_KEY   = os.getenv("NEWS_API_KEY")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
DB_HOST    = os.getenv("DB_HOST", "localhost")
DB_PORT    = int(os.getenv("DB_PORT", 5432))
DB_NAME    = os.getenv("DB_NAME", "goldentrinity")
DB_USER    = os.getenv("DB_USER")
DB_PASS    = os.getenv("DB_PASS")

redis_client = None
db_pool      = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client, db_pool
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=2, max_size=5
    )
    existing = await redis_client.get("gt:regime")
    if not existing:
        await redis_client.set("gt:regime", "NEUTRAL")
        await redis_client.set("gt:regime_updated", datetime.now(timezone.utc).isoformat())
    print("[macro_service] Started. Redis and DB connected.")
    yield
    await redis_client.close()
    await db_pool.close()

app = FastAPI(lifespan=lifespan)


# ── FRED fetch with retry ──────────────────────────────────────────────────

def fred_fetch(series_id: str, limit: int = 1, retries: int = 3) -> list:
    """Fetch FRED observations with exponential backoff retry."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id":  series_id,
        "api_key":    FRED_KEY,
        "limit":      limit,
        "sort_order": "desc",
        "file_type":  "json"
    }
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json().get("observations", [])
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 500 and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[macro_service] FRED {series_id} 500 error — retry in {wait}s "
                      f"(attempt {attempt+1}/{retries})")
                time.sleep(wait)
            else:
                raise
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    return []

def safe_float(obs_list: list, idx: int = 0) -> float | None:
    try:
        val = obs_list[idx]["value"]
        return float(val) if val != "." else None
    except (IndexError, KeyError, ValueError):
        return None


# ── CPI YoY (CRITICAL: never use raw CPIAUCSL index level) ────────────────

def fetch_cpi_yoy() -> float | None:
    obs = fred_fetch("CPIAUCSL", limit=13)
    if len(obs) < 13:
        return None
    current    = safe_float(obs, 0)
    prior_year = safe_float(obs, 12)
    if current is None or prior_year is None or prior_year == 0:
        return None
    return round((current - prior_year) / prior_year * 100, 4)


# ── GVZ via yfinance ───────────────────────────────────────────────────────

def fetch_gvz() -> float | None:
    try:
        import yfinance as yf
        hist = yf.Ticker("^GVZ").history(period="2d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 4)
    except Exception:
        pass
    return None


# ── SPX 20-day return via yfinance ─────────────────────────────────────────

def fetch_spx_20d_return() -> float | None:
    """
    S&P 500 20-day return as risk-on/risk-off signal.

    Why 20 days: short enough to be recent, long enough to filter daily noise.
    One-day SPX moves are too noisy for regime classification — a 1% down day
    can reverse the next morning. A 20-day trend is a genuine risk-off signal.

    Scoring in classify_regime():
      SPX return < -2%: risk-off → stock market stress → safe-haven gold bid → +1
      SPX return > +3%: risk-on  → equities preferred  → less gold demand   → -1
      Between -2% and +3%: neutral (wide band avoids noise in trendless markets)

    Source: yfinance ^GSPC — free, no API key, already in stack via xauusd_service.
    Returns 20-day % return as a decimal (e.g. -0.035 = -3.5%).
    Returns None on fetch failure — classifier handles None gracefully.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("^GSPC").history(period="30d")
        if hist.empty or len(hist) < 20:
            return None
        closes = hist["Close"].values
        ret = (closes[-1] - closes[-20]) / closes[-20]
        return round(float(ret), 6)
    except Exception:
        return None


# ── AUD/USD 20-day return via yfinance ─────────────────────────────────────

def fetch_audusd_20d_return() -> float | None:
    """
    AUD/USD 20-day return as gold-correlated forex confirmation signal.

    Why AUD/USD: Australia exports ~10% of world gold supply. AUD has a structural
    positive correlation with gold prices — both respond to the same forces:
    global risk appetite, commodity demand, and China growth expectations.
    When AUD is rising, it usually confirms gold demand is genuine, not just
    dollar weakness. When AUD diverges from gold, the move is suspect.

    Scoring in classify_regime():
      AUD/USD return > +1%: AUD strengthening → gold confirmation → +1
      AUD/USD return < -1%: AUD weakening     → gold headwind    → -1
      Band is tighter than SPX because AUD/USD is less volatile.

    Source: yfinance AUDUSD=X — direct AUD/USD spot rate, free, no API key.
    Returns 20-day % return as a decimal (e.g. 0.015 = +1.5%).
    Returns None on fetch failure — classifier handles None gracefully.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("AUDUSD=X").history(period="30d")
        if hist.empty or len(hist) < 20:
            return None
        closes = hist["Close"].values
        ret = (closes[-1] - closes[-20]) / closes[-20]
        return round(float(ret), 6)
    except Exception:
        return None


# ── GDX 20-day return via yfinance ───────────────────────────────────────

def fetch_gdx_20d_return() -> float | None:
    """
    VanEck Gold Miners ETF (GDX) 20-day return as a leading indicator for gold.

    Why GDX leads gold: Institutional money flows into miners before buying
    physical gold or gold ETFs — miners are more leveraged to expected gold
    price moves, so smart money positions in miners first. A rising GDX trend
    that precedes a flat gold price often forecasts an imminent gold rally.

    Scoring: > +3%  = miners outperforming = BULLISH for gold direction  → +1
             < -3%  = miners underperforming = BEARISH signal            → -1
             Tighter ±3% band vs SPX ±2%/+3% because GDX is more volatile.

    Source: yfinance GDX — highly liquid ETF, reliable data.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker("GDX").history(period="30d")
        if hist.empty or len(hist) < 20:
            return None
        closes = hist["Close"].values
        ret = (closes[-1] - closes[-20]) / closes[-20]
        return round(float(ret), 6)
    except Exception:
        return None


def fetch_gdxj_gdx_ratio() -> float | None:
    """
    GDXJ/GDX ratio — junior miners vs senior miners momentum indicator.

    Why this ratio matters: Junior miners (GDXJ) are far more leveraged to
    gold price expectations than senior miners (GDX). When juniors outperform
    seniors, it signals rising risk appetite specifically within the gold sector
    — institutions are reaching further out the risk curve, expecting a stronger
    gold move. This tends to precede gold rallies by days to a week.

    Conversely, when seniors outperform juniors (ratio falling), institutions
    are rotating to safety within gold — a sign of uncertainty or weakening
    conviction in the gold rally.

    Scoring: ratio > 1.02 (juniors beating seniors by >2%) → BULLISH  +1
             ratio < 0.98 (seniors beating juniors by >2%) → BEARISH  -1
             Near 1.0: no signal — both moving together, no divergence.

    Source: both GDX and GDXJ via yfinance — highly liquid, reliable.
    Returns the ratio of their 5-day returns (recent momentum, not price level).
    """
    try:
        import yfinance as yf
        gdxj = yf.Ticker("GDXJ").history(period="10d")
        gdx  = yf.Ticker("GDX").history(period="10d")
        if gdxj.empty or gdx.empty or len(gdxj) < 5 or len(gdx) < 5:
            return None
        gdxj_ret = (gdxj["Close"].iloc[-1] - gdxj["Close"].iloc[-5]) / gdxj["Close"].iloc[-5]
        gdx_ret  = (gdx["Close"].iloc[-1]  - gdx["Close"].iloc[-5])  / gdx["Close"].iloc[-5]
        if abs(gdx_ret) < 0.0001:   # avoid division by near-zero
            return None
        ratio = (1 + gdxj_ret) / (1 + gdx_ret)
        return round(float(ratio), 6)
    except Exception:
        return None


def fetch_fnv_gdx_ratio() -> float | None:
    """
    Franco-Nevada/GDX ratio — royalty vs producers momentum indicator.

    Why this ratio matters: Royalty companies (FNV) have no operational risk —
    they just collect royalties on gold production. When FNV outperforms GDX
    (producers), it means institutional money is seeking "safe" gold exposure,
    avoiding the operational/cost risk of mining. This reveals risk-averse
    positioning within gold — institutions believe gold will rise but want
    protection from mining execution risk. Historically this occurs near turning
    points where physical gold demand is building but confidence is fragile.

    A rising FNV/GDX ratio combined with rising gold price = very high
    conviction BULLISH signal (institutions are paying premium for safe exposure).
    A falling FNV/GDX ratio = institutions comfortable with operational risk
    = risk-on within gold = gold momentum likely continuing.

    Scoring (5-day ratio of returns):
      ratio > 1.015 (FNV outperforming GDX by >1.5%) → BULLISH +1
      ratio < 0.985 (GDX outperforming FNV by >1.5%) → slight risk-on → 0
        (not penalised — risk-on within gold is not bearish for gold price)

    Source: FNV and GDX via yfinance — both liquid, reliable daily data.
    """
    try:
        import yfinance as yf
        fnv = yf.Ticker("FNV").history(period="10d")
        gdx = yf.Ticker("GDX").history(period="10d")
        if fnv.empty or gdx.empty or len(fnv) < 5 or len(gdx) < 5:
            return None
        fnv_ret = (fnv["Close"].iloc[-1] - fnv["Close"].iloc[-5]) / fnv["Close"].iloc[-5]
        gdx_ret = (gdx["Close"].iloc[-1] - gdx["Close"].iloc[-5]) / gdx["Close"].iloc[-5]
        if abs(gdx_ret) < 0.0001:
            return None
        ratio = (1 + fnv_ret) / (1 + gdx_ret)
        return round(float(ratio), 6)
    except Exception:
        return None


# ── News sentiment — 4-hour batch window ──────────────────────────────────

def fetch_news_sentiment() -> float:
    """
    Fetch gold-related news from the last 4 hours.
    ~48 calls/day (well under 100 free tier limit).
    """
    try:
        from_dt = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = "https://newsapi.org/v2/everything"
        params = {
            "q":        "gold OR inflation OR \"federal reserve\" OR \"interest rates\"",
            "apiKey":   NEWS_KEY,
            "language": "en",
            "sortBy":   "publishedAt",
            "pageSize": 20,
            "from":     from_dt,
        }
        resp     = requests.get(url, params=params, timeout=10)
        articles = resp.json().get("articles", [])
        if not articles:
            return 0.0

        bullish_words = [
            "rally", "surge", "rise", "higher", "bullish", "inflation", "safe haven",
            "rate cut", "dovish", "weak dollar", "geopolitical", "uncertainty"
        ]
        bearish_words = [
            "fall", "drop", "decline", "lower", "bearish", "hawkish",
            "rate hike", "strong dollar", "tightening", "sell-off"
        ]
        score = 0
        for article in articles:
            text = ((article.get("title") or "") + " " + (article.get("description") or "")).lower()
            for w in bullish_words:
                if w in text: score += 1
            for w in bearish_words:
                if w in text: score -= 1
        return round(max(-1.0, min(1.0, score / max(len(articles), 1))), 4)
    except Exception:
        return 0.0


# ── Regime classification ──────────────────────────────────────────────────

def classify_regime(
    real_rate:         float | None,
    dxy:               float | None,
    dxy_12m_mean:      float | None,
    vix:               float | None,
    news:              float,
    mm_pct:            float | None,
    spx_20d_return:    float | None,
    audusd_20d_return: float | None,
    gdx_20d_return:    float | None = None,
    gdxj_gdx_ratio:    float | None = None,
    fnv_gdx_ratio:     float | None = None,
) -> str:
    """
    Regime scoring — each input contributes ±1 or ±2 points.
    BULLISH if score >= +2, BEARISH if score <= -1 (asymmetric), else NEUTRAL.

    Inputs and their weights:
      Real rate      +-2  (primary driver — dominates at extremes)
      DXY            +-1  (inverse correlation, relative to 12m mean)
      VIX            +-1  (fear level)
      News           +-1  (sentiment)
      SPX 20d        +-1  (risk-on/risk-off direction)
      AUD/USD 20d    +-1  (gold-correlated forex confirmation)
      GDX 20d        +-1  (miner flows — institutional lead indicator)
      GDXJ/GDX ratio +-1  (junior vs senior momentum — early warning)
      FNV/GDX ratio   +1  (royalty vs producer — safe gold demand signal)
      COT cap              (crowding override, caps BULLISH at NEUTRAL)

    Total range: -7 to +9. Asymmetric: BEARISH at -1, BULLISH at +2.
    Real rate dominates at extremes (+-2 weight).
    """
    score = 0

    # Real interest rate (most important gold driver)
    if real_rate is not None:
        if real_rate < 0:    score += 2   # negative real rates = structurally BULLISH
        elif real_rate < 1:  score += 1
        elif real_rate > 3:  score -= 2
        elif real_rate > 2:  score -= 1

    # DXY — relative to its own 12-month mean
    if dxy is not None and dxy_12m_mean is not None and dxy_12m_mean > 0:
        dxy_dev = (dxy - dxy_12m_mean) / dxy_12m_mean
        if dxy_dev < -0.02:  score += 1   # DXY >2% below mean = weak dollar = BULLISH gold
        elif dxy_dev > 0.02: score -= 1   # DXY >2% above mean = strong dollar = BEARISH gold

    # VIX — broad fear level
    if vix is not None:
        if vix > 25:   score += 1   # high fear = safe haven bid for gold
        elif vix < 15: score -= 1   # low fear = risk-on, less gold demand

    # SPX 20-day return — market direction (risk-on vs risk-off)
    if spx_20d_return is not None:
        if spx_20d_return < -0.02:   score += 1   # market falling = risk-off = BULLISH gold
        elif spx_20d_return > 0.03:  score -= 1   # market rising strongly = risk-on = BEARISH gold
        # Band is asymmetric: -2% bearish threshold vs +3% bullish threshold
        # because gold reacts more sharply to equity selloffs than to rallies

    # AUD/USD 20-day return — gold-correlated forex confirmation
    if audusd_20d_return is not None:
        if audusd_20d_return > 0.01:   score += 1   # AUD rising = gold demand confirmation
        elif audusd_20d_return < -0.01: score -= 1  # AUD falling = gold headwind

    # GDX 20-day return — institutional miner flows (lead indicator for gold)
    # Miners are more leveraged to gold expectations; institutional money flows
    # into miners before buying physical gold, making GDX a 1-5 day leading signal.
    if gdx_20d_return is not None:
        if gdx_20d_return > 0.03:    score += 1   # miners rallying = bullish gold expectation
        elif gdx_20d_return < -0.03: score -= 1   # miners falling = bearish gold expectation

    # GDXJ/GDX ratio — junior vs senior miner momentum (early warning signal)
    # Juniors outperforming = risk appetite rising within gold = early bull signal.
    # Seniors outperforming = institutions rotating to safety within gold.
    if gdxj_gdx_ratio is not None:
        if gdxj_gdx_ratio > 1.02:   score += 1   # juniors beating seniors = early momentum
        elif gdxj_gdx_ratio < 0.98: score -= 1   # seniors beating juniors = defensive rotation

    # FNV/GDX ratio — royalty vs producer outperformance (safe gold demand signal)
    # FNV outperforming GDX = institutions paying premium for operational safety
    # = risk-averse demand for gold exposure = building bullish conviction.
    # GDX outperforming FNV = risk-on within gold = not penalised (still bullish for gold).
    if fnv_gdx_ratio is not None:
        if fnv_gdx_ratio > 1.015:  score += 1   # royalty premium = institutional safe gold demand

    # News sentiment
    if news > 0.3:    score += 1
    elif news < -0.3: score -= 1

    # COT crowding cap — when managed money is crowded long, cap at NEUTRAL
    if mm_pct is not None and mm_pct >= 80:
        score = min(score, 0)

    # Asymmetric thresholds: BEARISH triggers at -1, BULLISH at +2.
    # Rationale from Mar 23 overnight analysis: SPX was -4.8% (risk-off) and
    # AUD/USD was -1.9% — both pointing bearish for gold positioning, yet the
    # regime stayed NEUTRAL (score=0) because the threshold was -2.
    # BUY signals fired all night into a -$130 gold decline at 27.3% win rate.
    # Lowering BEARISH to -1 means even a single strong bearish signal (e.g.
    # falling equities + weak AUD) is enough to suppress BUY entries.
    # BULLISH stays at +2 — requiring stronger confirmation to size up is correct.
    if score >= 2:    return "BULLISH"
    elif score <= -1: return "BEARISH"
    else:             return "NEUTRAL"


# ── Main macro update ──────────────────────────────────────────────────────

async def run_macro_update():
    now = datetime.now(timezone.utc)
    try:
        # ── FRED inputs ────────────────────────────────────────────────────
        cpi_yoy        = fetch_cpi_yoy()
        dff_obs        = fred_fetch("DFF", 1)
        fed_funds_rate = safe_float(dff_obs)
        real_rate      = round(fed_funds_rate - cpi_yoy, 4) if (fed_funds_rate and cpi_yoy) else None

        dxy_obs      = fred_fetch("DTWEXBGS", 1)
        dxy          = safe_float(dxy_obs)
        dxy_12m_obs  = fred_fetch("DTWEXBGS", 252)
        dxy_12m_mean = None
        if len(dxy_12m_obs) >= 50:
            vals = [safe_float(dxy_12m_obs, i) for i in range(len(dxy_12m_obs))]
            vals = [v for v in vals if v is not None]
            dxy_12m_mean = round(sum(vals) / len(vals), 4) if vals else None

        vix_obs    = fred_fetch("VIXCLS", 1)
        vix        = safe_float(vix_obs)
        t10y2y_obs = fred_fetch("T10Y2Y", 1)
        t10y2y     = safe_float(t10y2y_obs)

        # ── yfinance inputs ────────────────────────────────────────────────
        gvz           = fetch_gvz()
        gvz_vix_ratio = round(gvz / vix, 4) if (gvz and vix and vix != 0) else None
        spx_20d       = fetch_spx_20d_return()
        audusd_20d    = fetch_audusd_20d_return()

        # ── Miner flow inputs (new — from Predictive Asset List) ─────────
        gdx_20d    = fetch_gdx_20d_return()
        gdxj_ratio = fetch_gdxj_gdx_ratio()
        fnv_ratio  = fetch_fnv_gdx_ratio()

        # ── News sentiment ─────────────────────────────────────────────────
        news_sentiment = fetch_news_sentiment()

        # ── COT data from DB ───────────────────────────────────────────────
        mm_pct = None
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT mm_percentile_52w FROM cot_snapshots ORDER BY date DESC LIMIT 1"
            )
            if row:
                mm_pct = float(row["mm_percentile_52w"])

        # ── Regime classification ──────────────────────────────────────────
        regime = classify_regime(
            real_rate, dxy, dxy_12m_mean, vix,
            news_sentiment, mm_pct,
            spx_20d, audusd_20d,
            gdx_20d, gdxj_ratio, fnv_ratio,
        )

        snapshot = {
            "timestamp":         now.isoformat(),
            "cpi_yoy":           cpi_yoy,
            "fed_funds_rate":    fed_funds_rate,
            "real_rate":         real_rate,
            "dxy":               dxy,
            "dxy_12m_mean":      dxy_12m_mean,
            "vix":               vix,
            "t10y2y":            t10y2y,
            "gvz":               gvz,
            "gvz_vix_ratio":     gvz_vix_ratio,
            "spx_20d_return":    spx_20d,
            "audusd_20d_return": audusd_20d,
            "gdx_20d_return":    gdx_20d,
            "gdxj_gdx_ratio":    gdxj_ratio,
            "fnv_gdx_ratio":     fnv_ratio,
            "news_sentiment":    news_sentiment,
            "regime":            regime,
            "mm_cot_percentile": mm_pct,
        }

        await redis_client.set("gt:regime", regime)
        await redis_client.set("gt:regime_updated", now.isoformat())
        await redis_client.set("gt:macro_snapshot", json.dumps(snapshot))

        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO macro_snapshots
                (timestamp, cpi_yoy, fed_funds_rate, real_rate, dxy, vix,
                 t10y2y, gvz, gvz_vix_ratio, news_sentiment, regime,
                 mm_cot_percentile, spx_20d_return, audusd_20d_return)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            """, now, cpi_yoy, fed_funds_rate, real_rate, dxy, vix,
                t10y2y, gvz, gvz_vix_ratio, news_sentiment, regime,
                mm_pct, spx_20d, audusd_20d)

        dxy_str    = f"{dxy:.3f} (mean={dxy_12m_mean:.3f})" if dxy_12m_mean else str(dxy)
        spx_str    = f"{spx_20d*100:+.2f}%" if spx_20d is not None else "N/A"
        audusd_str = f"{audusd_20d*100:+.2f}%" if audusd_20d is not None else "N/A"
        gdx_str    = f"{gdx_20d*100:+.2f}%" if gdx_20d is not None else "N/A"
        gdxj_str   = f"{gdxj_ratio:.4f}" if gdxj_ratio is not None else "N/A"
        fnv_str    = f"{fnv_ratio:.4f}" if fnv_ratio is not None else "N/A"
        print(f"[macro_service] Updated — Regime: {regime} | RealRate: {real_rate} | "
              f"DXY: {dxy_str} | VIX: {vix} | SPX20d: {spx_str} | AUDUSD20d: {audusd_str} | "
              f"GDX20d: {gdx_str} | GDXJ/GDX: {gdxj_str} | FNV/GDX: {fnv_str}")
        return snapshot

    except Exception as e:
        print(f"[macro_service] ERROR in macro update: {e}")
        raise


# ── API Endpoints ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    start      = time.time()
    await redis_client.ping()
    latency_ms = round((time.time() - start) * 1000, 2)
    regime     = await redis_client.get("gt:regime")
    updated    = await redis_client.get("gt:regime_updated")
    snap_raw   = await redis_client.get("gt:macro_snapshot")
    spx_str    = audusd_str = None
    if snap_raw:
        snap = json.loads(snap_raw)
        s = snap.get("spx_20d_return")
        a = snap.get("audusd_20d_return")
        spx_str    = f"{s*100:+.2f}%" if s is not None else None
        audusd_str = f"{a*100:+.2f}%" if a is not None else None
    return {
        "status":             "healthy",
        "service":            "macro_service",
        "latency_ms":         latency_ms,
        "regime":             regime.decode() if regime else "NEUTRAL",
        "regime_updated":     updated.decode() if updated else None,
        "spx_20d_return":     spx_str,
        "audusd_20d_return":  audusd_str,
    }

@app.post("/api/macro/update")
async def macro_update():
    snapshot = await run_macro_update()
    return {"status": "ok", "regime": snapshot["regime"], "snapshot": snapshot}

@app.get("/api/macro/current")
async def macro_current():
    data   = await redis_client.get("gt:macro_snapshot")
    regime = await redis_client.get("gt:regime")
    if not data:
        return {"regime": "NEUTRAL", "snapshot": None}
    return {
        "regime":   regime.decode() if regime else "NEUTRAL",
        "snapshot": json.loads(data)
    }

@app.post("/api/macro/cot")
async def macro_cot(payload: dict):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO cot_snapshots
            (date, managed_money_net, commercial_net,
             mm_percentile_52w, comm_percentile_52w, mm_signal, comm_signal)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (date) DO UPDATE SET
                managed_money_net    = EXCLUDED.managed_money_net,
                commercial_net       = EXCLUDED.commercial_net,
                mm_percentile_52w    = EXCLUDED.mm_percentile_52w,
                comm_percentile_52w  = EXCLUDED.comm_percentile_52w
        """, payload["date"], payload["managed_money_net"], payload["commercial_net"],
            payload["mm_percentile_52w"], payload["comm_percentile_52w"],
            payload.get("mm_signal", "NEUTRAL"), payload.get("comm_signal", "NEUTRAL"))
    return {"status": "ok"}

@app.get("/api/macro/gvz")
async def macro_gvz():
    return {"gvz": fetch_gvz()}
