"""
health_monitor.py — Standalone health monitor
Runs every 5 minutes, checks all 5 services, posts to Discord.
Start with: python health_monitor.py
Timestamps displayed in ET (EST/EDT). Internals always UTC.
"""
import time, requests, os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

SERVICES = [
    ("macro_service",     "http://localhost:8001/api/health"),
    ("risk_service",      "http://localhost:8002/api/health"),
    ("execution_service", "http://localhost:8003/api/health"),
    ("signal_service",    "http://localhost:8004/api/health"),
    ("xauusd_service",    "http://localhost:8005/api/health"),
]

DISCORD_HEALTH = os.getenv("DISCORD_WEBHOOK_HEALTH")
DISCORD_ALERTS = os.getenv("DISCORD_WEBHOOK_ALERTS")

def et_now():
    """
    Return current time as ET string (handles EST/EDT automatically).
    Uses manual UTC offset — zoneinfo on Windows often fails with
    'No time zone found with key America/New_York' even when the
    module is present, because the Windows timezone database path
    differs from the IANA path zoneinfo expects.
    """
    utc    = datetime.now(timezone.utc)
    offset = -4 if 3 <= utc.month <= 11 else -5   # EDT = -4, EST = -5
    et     = utc + timedelta(hours=offset)
    label  = "EDT" if offset == -4 else "EST"
    return et.strftime(f"%Y-%m-%d %I:%M %p {label}")

def check_and_post():
    results = []
    for name, url in SERVICES:
        try:
            r       = requests.get(url, timeout=5)
            data    = r.json()
            ok      = data.get("status") == "healthy"
            latency = data.get("latency_ms", "?")
            extra   = ""
            if name == "execution_service":
                nav = data.get("nav")
                pnl = data.get("daily_pnl_pct", 0)
                if nav:
                    extra = f" | NAV=${nav:.2f} PnL={pnl:.2%}"
            elif name == "risk_service":
                cb    = data.get("cb_samples", 0)
                extra = f" | CB samples={cb}"
            elif name == "signal_service":
                model   = data.get("model", "?")
                buf     = data.get("buffer_size", 0)
                collapse = data.get("collapse_alert", False)
                extra   = f" | {model} buf={buf}" + (" ⚠️COLLAPSE" if collapse else "")
            elif name == "xauusd_service":
                price = data.get("last_price", 0)
                pos   = data.get("open_positions", 0)
                exp   = data.get("combined_exp_pct", 0)
                extra = f" | XAU=${price:.1f} pos={pos} exp={exp:.1f}%" if price else ""
            results.append((name, ok, latency, extra))
        except Exception:
            results.append((name, False, None, ""))

    all_ok  = all(r[1] for r in results)
    failed  = [r[0] for r in results if not r[1]]
    lines   = [
        ("✅" if ok else "❌") + f" {name}" +
        (f" — {lat}ms{extra}" if lat else f" — DOWN{extra}")
        for name, ok, lat, extra in results
    ]
    ts      = et_now()
    emoji   = "🟢" if all_ok else "🔴"
    message = f"{emoji} **GoldenTrinity Health — {ts}**\n" + "\n".join(lines)

    if DISCORD_HEALTH:
        requests.post(DISCORD_HEALTH, json={"content": message}, timeout=5)

    if not all_ok and DISCORD_ALERTS:
        alert = f"@everyone 🚨 **GoldenTrinity SERVICE DOWN**: {', '.join(failed)}\n{ts}"
        requests.post(DISCORD_ALERTS, json={"content": alert}, timeout=5)
        print(f"[health_monitor] ALERT — DOWN: {failed}")
    else:
        print(f"[health_monitor] All healthy — {ts}")

if __name__ == "__main__":
    print("[health_monitor] Starting — checking every 5 minutes (5 services)")
    while True:
        try:
            check_and_post()
        except Exception as e:
            print(f"[health_monitor] Error: {e}")
        time.sleep(300)
