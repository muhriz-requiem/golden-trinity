"""
update_training_data.py — Keep training CSVs fresh
===================================================
Appends only NEW bars since the last download.
Run weekly (or before any training run) to keep data current.

Usage:
    python update_training_data.py           # updates both 1min and 5min + exports live trades
    python update_training_data.py --1min    # only 1min bars
    python update_training_data.py --5min    # only 5min bars
    python update_training_data.py --no-live # skip live trade export

Why this matters:
  GLD at $460 moves differently than at $200.
  Your most recent bars are the most relevant to current trading.
  Live paper trade outcomes are ground truth — real fills, real exits,
  real market conditions — more valuable than synthetic historical labels.

How live trade data flows into training:
  1. This script exports GLD_live_trades.csv from TimescaleDB position_log
  2. train_lstm_v5.py reads it as supplementary high-quality recent data
  3. When you have >=200 live trades, they get weighted 2x during training
     because they reflect real current market behavior
"""
import os, sys, time
import pandas as pd
import numpy as np
import requests
import asyncpg
import asyncio
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

ALPACA_KEY    = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
ALPACA_BASE   = "https://data.alpaca.markets"
HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
}

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME", "goldentrinity")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

DATA_DIR   = r"D:\golden-trinity\data"
FILE_1MIN  = os.path.join(DATA_DIR, "GLD_1min.csv")
FILE_5MIN  = os.path.join(DATA_DIR, "GLD_5min.csv")
LIVE_FILE  = os.path.join(DATA_DIR, "GLD_live_trades.csv")


# ── Alpaca bar fetcher ─────────────────────────────────────────────────────

def fetch_bars_since(symbol, timeframe, since_dt):
    end   = datetime.now(timezone.utc)
    start = since_dt

    if (end - start).total_seconds() < 300:
        print(f"  Less than 5 minutes of new data — skipping")
        return None

    print(f"  Fetching {symbol} {timeframe} bars from "
          f"{start.strftime('%Y-%m-%d %H:%M')} to {end.strftime('%Y-%m-%d %H:%M')} UTC...")

    all_bars   = []
    page_token = None

    while True:
        params = {
            "symbols":    symbol,
            "timeframe":  timeframe,
            "start":      start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":      10000,
            "feed":       "iex",
            "sort":       "asc",
        }
        if page_token:
            params["page_token"] = page_token

        try:
            r = requests.get(
                f"{ALPACA_BASE}/v2/stocks/bars",
                headers=HEADERS, params=params, timeout=30
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"  Fetch error: {e}")
            return None

        bars = data.get("bars", {}).get(symbol, [])
        all_bars.extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.3)

    if not all_bars:
        print(f"  No new bars returned")
        return None

    df = pd.DataFrame(all_bars)
    df = df.rename(columns={"t": "timestamp", "o": "open", "h": "high",
                             "l": "low",  "c": "close", "v": "volume"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
    df = df.sort_index()
    print(f"  Got {len(df)} new bars")
    return df


def update_csv(filepath, timeframe_str, alpaca_tf):
    print(f"\n{'─'*50}")
    print(f"Updating {os.path.basename(filepath)} ({timeframe_str})")

    if not os.path.exists(filepath):
        print(f"  File not found — run download_data.py first")
        return False

    existing = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
    if existing.index.tz is None:
        existing.index = existing.index.tz_localize("UTC")

    last_ts     = existing.index.max()
    rows_before = len(existing)
    age_hours   = (datetime.now(timezone.utc) - last_ts).total_seconds() / 3600
    print(f"  Existing: {rows_before:,} bars | Last bar: {last_ts.strftime('%Y-%m-%d %H:%M')} UTC "
          f"({age_hours:.1f}h ago)")

    since    = last_ts + timedelta(minutes=1)
    new_bars = fetch_bars_since("GLD", alpaca_tf, since)

    if new_bars is None or len(new_bars) == 0:
        print(f"  Already up to date")
        return True

    new_bars = new_bars[new_bars.index > last_ts]
    combined = pd.concat([existing, new_bars])
    combined = combined[~combined.index.duplicated(keep="last")]
    combined = combined.sort_index()
    combined.to_csv(filepath)

    added = len(combined) - rows_before
    print(f"  Added {added:,} bars → total {len(combined):,} bars")
    print(f"  Coverage: {combined.index.min().date()} → {combined.index.max().date()}")
    return True


# ── Live trade exporter ────────────────────────────────────────────────────

async def export_live_trades():
    """
    Export completed GLD paper trades from TimescaleDB.

    These are more valuable than historical bars for training because:
    - They are recent (reflect current GLD price level and regime)
    - They passed your confidence gate (signal_service already filtered them)
    - They have real outcome labels (pnl_pct > 0 = UP was correct, < 0 = wrong)
    - They capture real market microstructure at the times you actually trade

    When you accumulate >=200 trades, train_lstm_v5.py will weight them
    2x during training to bias the model toward recent behavior.
    """
    print(f"\n{'─'*50}")
    print("Exporting live paper trade outcomes from TimescaleDB...")

    try:
        pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASS, min_size=1, max_size=2
        )
    except Exception as e:
        print(f"  DB connection failed: {e}")
        print(f"  (Is Docker running? Start with: docker-compose up -d)")
        return False

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                opened_at,
                direction,
                fill_price,
                exit_price,
                pnl_pct,
                exit_type,
                regime_at_entry,
                session,
                confidence_at_entry,
                time_in_trade_min
            FROM position_log
            WHERE closed_at IS NOT NULL
              AND pnl_pct IS NOT NULL
              AND asset = 'GLD'
            ORDER BY opened_at ASC
        """)
    await pool.close()

    if not rows:
        print(f"  No completed GLD trades yet")
        return False

    trades = [{
        "opened_at":         r["opened_at"].isoformat(),
        "direction":         r["direction"],
        "fill_price":        float(r["fill_price"] or 0),
        "exit_price":        float(r["exit_price"] or 0),
        "pnl_pct":           float(r["pnl_pct"] or 0),
        "exit_type":         r["exit_type"],
        "regime":            r["regime_at_entry"],
        "session":           r["session"],
        "confidence":        float(r["confidence_at_entry"] or 0),
        "time_in_trade_min": float(r["time_in_trade_min"] or 0),
        "won":               1 if float(r["pnl_pct"] or 0) > 0 else 0,
    } for r in rows]

    df = pd.DataFrame(trades)
    df.to_csv(LIVE_FILE, index=False)

    total  = len(df)
    wins   = df["won"].sum()
    avg_pnl = df["pnl_pct"].mean() * 100
    print(f"  {total} trades exported | Win rate: {wins/total:.1%} | Avg PnL: {avg_pnl:.3f}%")
    print(f"  Saved to: {LIVE_FILE}")

    if total < 200:
        remaining = 200 - total
        print(f"\n  ⏳ Need {remaining} more trades before live data feeds into LSTM training")
        print(f"     At current pace (~{total} trades/day), "
              f"that's ~{remaining//max(total,1)} more trading days")
    else:
        print(f"\n  ✅ {total} trades — enough for live data to feed into LSTM training")
        print(f"     train_lstm_v5.py will weight these 2x during training")

    return True


# ── Summary ────────────────────────────────────────────────────────────────

def print_summary():
    print(f"\n{'='*50}")
    print("TRAINING DATA STATUS")
    print(f"{'='*50}")

    for filepath, label in [(FILE_1MIN, "1-min bars"),
                             (FILE_5MIN, "5-min bars"),
                             (LIVE_FILE, "live trades")]:
        if not os.path.exists(filepath):
            print(f"  {label:14s}: NOT FOUND")
            continue

        if filepath == LIVE_FILE:
            df = pd.read_csv(filepath)
            status = "✅ feeds into training" if len(df) >= 200 else f"⏳ need {200-len(df)} more"
            print(f"  {label:14s}: {len(df)} trades  {status}")
        else:
            df = pd.read_csv(filepath, index_col="timestamp", parse_dates=True)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            age_days = (datetime.now(timezone.utc) - df.index.max()).days
            freshness = "✅" if age_days <= 7 else f"⚠️  {age_days}d old"
            print(f"  {label:14s}: {len(df):,} bars | "
                  f"{df.index.min().date()} → {df.index.max().date()} | {freshness}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 50)
    print("GoldenTrinity — Training Data Updater")
    print(f"Run at: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    args     = sys.argv[1:]
    do_1min  = "--5min"    not in args
    do_5min  = "--1min"    not in args
    do_live  = "--no-live" not in args

    if do_1min:
        update_csv(FILE_1MIN, "1-minute", "1Min")
    if do_5min:
        update_csv(FILE_5MIN, "5-minute", "5Min")
    if do_live:
        asyncio.run(export_live_trades())

    print_summary()
    print()
    print("Run train_lstm_v5.py when ready to train")
    print("Recommended: run this updater weekly before each training run")


if __name__ == "__main__":
    main()
