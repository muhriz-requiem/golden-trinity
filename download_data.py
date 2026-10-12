"""
download_data.py — Download historical GLD data for LSTM training
Uses Alpaca historical API (free with paper account)
Run: python download_data.py
Output: D:\golden-trinity\data\GLD_1min.csv
"""
import os, time
import pandas as pd
import requests
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

OUTPUT_DIR  = "D:\\golden-trinity\\data"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "GLD_1min.csv")

def download_bars(symbol="GLD", timeframe="1Min", years=2):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * years)

    print(f"Downloading {symbol} {timeframe} bars from {start.date()} to {end.date()}...")

    all_bars = []
    page_token = None
    page = 0

    while True:
        params = {
            "symbols":    symbol,
            "timeframe":  timeframe,
            "start":      start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":        end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":      10000,
            "feed":       "iex",
            "adjustment": "all",
        }
        if page_token:
            params["page_token"] = page_token

        r = requests.get(f"{ALPACA_BASE}/v2/stocks/bars", headers=HEADERS, params=params, timeout=30)

        if r.status_code != 200:
            print(f"Error {r.status_code}: {r.text}")
            break

        data = r.json()
        bars = data.get("bars", {}).get(symbol, [])
        if not bars:
            break

        all_bars.extend(bars)
        page += 1
        print(f"  Page {page}: {len(bars)} bars (total: {len(all_bars)})")

        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.3)  # Rate limit

    if not all_bars:
        print("No data downloaded. Check API keys.")
        return None

    df = pd.DataFrame(all_bars)
    df["t"] = pd.to_datetime(df["t"])
    df = df.rename(columns={"t":"timestamp","o":"open","h":"high","l":"low","c":"close","v":"volume"})
    df = df.set_index("timestamp").sort_index()
    df = df[["open","high","low","close","volume"]]

    # Remove pre/post market — keep only 9:30-16:00 ET
    df.index = df.index.tz_convert("America/New_York")
    df = df.between_time("09:30","16:00")
    df.index = df.index.tz_convert("UTC")

    df.to_csv(OUTPUT_FILE)
    print(f"\nSaved {len(df)} bars to {OUTPUT_FILE}")
    print(f"Date range: {df.index[0]} → {df.index[-1]}")
    print(f"Columns: {list(df.columns)}")
    return df

if __name__ == "__main__":
    df = download_bars("GLD", "1Min", years=2)
    if df is not None:
        print(f"\nStats:")
        print(f"  Days of data: {df.index.normalize().nunique()}")
        print(f"  Avg bars/day: {len(df) / df.index.normalize().nunique():.0f}")
        print(f"  Close range:  ${df['close'].min():.2f} – ${df['close'].max():.2f}")
