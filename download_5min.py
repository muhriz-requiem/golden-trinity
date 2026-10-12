import os, time, requests, pandas as pd
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()

KEY    = os.getenv("ALPACA_KEY")
SECRET = os.getenv("ALPACA_SECRET")
BASE   = "https://data.alpaca.markets"
HEADERS = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET}
OUTPUT  = r"D:\golden-trinity\data\GLD_5min.csv"

end   = datetime.now(timezone.utc)
start = end - timedelta(days=365*2)
print(f"Downloading GLD 5Min bars {start.date()} to {end.date()}...")

all_bars, page_token, page = [], None, 0
while True:
    params = {
        "symbols": "GLD", "timeframe": "5Min",
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 10000, "feed": "iex", "adjustment": "all"
    }
    if page_token:
        params["page_token"] = page_token
    r = requests.get(f"{BASE}/v2/stocks/bars", headers=HEADERS, params=params, timeout=30)
    data = r.json()
    bars = data.get("bars", {}).get("GLD", [])
    if not bars:
        break
    all_bars.extend(bars)
    page += 1
    print(f"  Page {page}: {len(bars)} bars (total: {len(all_bars)})")
    page_token = data.get("next_page_token")
    if not page_token:
        break
    time.sleep(0.3)

df = pd.DataFrame(all_bars)
df["t"] = pd.to_datetime(df["t"])
df = df.rename(columns={"t":"timestamp","o":"open","h":"high","l":"low","c":"close","v":"volume"})
df = df.set_index("timestamp").sort_index()
df = df[["open","high","low","close","volume"]]
df.index = df.index.tz_convert("America/New_York")
df = df.between_time("09:30","16:00")
df.index = df.index.tz_convert("UTC")
df.to_csv(OUTPUT)
print(f"Saved {len(df)} bars to {OUTPUT}")
print(f"Range: {df.index[0].date()} to {df.index[-1].date()}")
