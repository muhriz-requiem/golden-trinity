"""
close_all_positions.py — Emergency / End-of-Day position closer
================================================================
Closes ALL open positions — both Alpaca (GLD/DUST) and simulated XAU/USD.
Clears Redis, updates DB.

Use cases:
  1. End of trading day — run before market close to avoid overnight GLD exposure
  2. Emergency — run any time to flatten everything immediately
  3. After a crash — positions stuck in Redis but market has moved

Run: python close_all_positions.py
     python close_all_positions.py --force   (skip market-hours check)
"""
import os, sys, json, asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
import redis.asyncio as aioredis
import asyncpg
import httpx

load_dotenv()

ALPACA_BASE   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_DATA   = "https://data.alpaca.markets"
ALPACA_KEY    = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
REDIS_HOST    = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT    = int(os.getenv("REDIS_PORT", 6379))
DB_HOST       = os.getenv("DB_HOST", "localhost")
DB_PORT       = int(os.getenv("DB_PORT", 5432))
DB_NAME       = os.getenv("DB_NAME", "goldentrinity")
DB_USER       = os.getenv("DB_USER")
DB_PASS       = os.getenv("DB_PASS")

HEADERS = {
    "APCA-API-KEY-ID":     ALPACA_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET,
    "Content-Type":        "application/json"
}


def is_market_open() -> bool:
    now = datetime.now(timezone.utc)
    h   = now.hour + now.minute / 60
    return now.weekday() < 5 and 13.5 <= h < 20.0


async def get_alpaca_price(symbol: str) -> float:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                f"{ALPACA_DATA}/v2/stocks/{symbol}/quotes/latest",
                headers=HEADERS
            )
            if r.status_code == 200:
                quote = r.json().get("quote", {})
                bid   = float(quote.get("bp", 0))
                ask   = float(quote.get("ap", 0))
                if bid > 0 and ask > 0:
                    return (bid + ask) / 2
    except Exception as e:
        print(f"  Price fetch error for {symbol}: {e}")
    return 0.0


async def get_xauusd_price() -> float:
    """Get current XAU/USD price from xauusd_service."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("http://localhost:8005/api/xauusd/price")
            if r.status_code == 200:
                return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0.0


async def place_alpaca_sell(symbol: str, qty) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{ALPACA_BASE}/v2/orders",
                headers=HEADERS,
                json={
                    "symbol":        symbol,
                    "qty":           str(qty),
                    "side":          "sell",
                    "type":          "market",
                    "time_in_force": "day"
                }
            )
            if r.status_code in (200, 201):
                return True
            print(f"  Order rejected ({r.status_code}): {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  Order error: {e}")
        return False


async def cancel_all_pending_orders():
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{ALPACA_BASE}/v2/orders", headers=HEADERS)
            if r.status_code == 207:
                cancelled = r.json()
                if cancelled:
                    print(f"  Cancelled {len(cancelled)} pending orders")
    except Exception as e:
        print(f"  Cancel orders error: {e}")


async def main():
    force = "--force" in sys.argv

    print("=" * 55)
    print("GoldenTrinity — Close All Positions")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 55)

    market_open = is_market_open()

    if not market_open and not force:
        print()
        print("⚠️  Market is currently CLOSED.")
        print()
        print("Options:")
        print("  1. Run again when market opens (9:30 AM ET)")
        print("  2. Use --force to queue Alpaca orders for next open")
        print("     (XAU/USD positions close immediately — 24/5 market)")
        print()
        answer = input("Proceed anyway? (y/n): ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return

    # Connect
    redis_client = aioredis.from_url(f"redis://{REDIS_HOST}:{REDIS_PORT}")
    db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT, database=DB_NAME,
        user=DB_USER, password=DB_PASS, min_size=1, max_size=3
    )

    # Cancel pending Alpaca orders
    print("\nCancelling pending Alpaca orders...")
    await cancel_all_pending_orders()

    # ── PART 1: Alpaca positions (GLD / DUST) ─────────────────────────────
    positions_raw    = await redis_client.get("gt:open_positions")
    alpaca_positions = json.loads(positions_raw) if positions_raw else []

    # Also catch any DB-only orphans
    async with db_pool.acquire() as conn:
        db_rows = await conn.fetch(
            "SELECT asset FROM position_log "
            "WHERE closed_at IS NULL AND pnl_pct IS NULL AND broker IN ('alpaca_paper','simulated_gld')"
        )

    print(f"\nAlpaca positions (GLD/DUST):")
    print(f"  Redis: {len(alpaca_positions)} | DB orphans: {len(db_rows)}")

    alpaca_closed = 0
    alpaca_failed = 0
    total_pnl     = 0.0

    if alpaca_positions:
        print(f"\n{'─'*55}")
        for pos in alpaca_positions:
            symbol = pos.get("asset", "GLD")
            qty    = pos.get("qty", 1)
            entry  = pos.get("fill_price", 0)
            size   = pos.get("size_pct", 0)
            print(f"  Closing {symbol} qty={qty} entry=${entry:.2f} ...", end=" ")

            price = await get_alpaca_price(symbol)
            ok    = await place_alpaca_sell(symbol, qty)

            if ok:
                pnl = (price - entry) / entry if price > 0 and entry > 0 else 0
                total_pnl += pnl * size
                print(f"✅  est. PnL: {pnl:+.2%}")
                alpaca_closed += 1

                opened_at = datetime.fromisoformat(pos.get("opened_at", datetime.now(timezone.utc).isoformat()))
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=timezone.utc)
                mins = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60

                try:
                    async with db_pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE position_log SET
                                closed_at = NOW(),
                                exit_price = $1,
                                exit_type = 'MANUAL_CLOSE',
                                time_in_trade_min = $2,
                                pnl_pct = $3
                            WHERE broker IN ('alpaca_paper','simulated_gld')
                              AND asset = $4
                              AND closed_at IS NULL
                            ORDER BY opened_at DESC
                            LIMIT 1
                        """, price if price > 0 else entry, round(mins, 2), round(pnl, 6), symbol)
                except Exception as e:
                    print(f"    DB update error: {e}")
            else:
                print("❌ FAILED")
                alpaca_failed += 1

            await asyncio.sleep(0.3)

    # Mark DB-only orphans as closed
    if db_rows:
        print(f"\n  Marking {len(db_rows)} DB orphan rows as closed...")
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE position_log SET
                    closed_at = NOW(),
                    exit_type = 'MANUAL_CLOSE'
                WHERE closed_at IS NULL AND pnl_pct IS NULL
                  AND broker IN ('alpaca_paper','simulated_gld')
            """)

    # ── PART 2: XAU/USD simulated positions ───────────────────────────────
    xauusd_raw       = await redis_client.get("gt:xauusd_positions")
    xauusd_positions = json.loads(xauusd_raw) if xauusd_raw else []

    print(f"\nXAU/USD simulated positions:")
    print(f"  Redis: {len(xauusd_positions)}")

    xauusd_closed = 0

    if xauusd_positions:
        print(f"\n{'─'*55}")
        xau_price = await get_xauusd_price()
        if xau_price == 0:
            # Try last known price from Redis
            raw = await redis_client.get("gt:xauusd_price")
            xau_price = float(raw) if raw else 0

        for pos in xauusd_positions:
            direction = pos.get("direction", "BUY")
            entry     = pos.get("fill_price", 0)
            units     = pos.get("units", 0)
            print(f"  Closing {direction} {units:.4f}oz XAU/USD entry=${entry:.2f} ...", end=" ")

            if xau_price > 0 and entry > 0:
                pnl = (xau_price - entry) / entry if direction == "BUY" else (entry - xau_price) / entry
                print(f"✅  est. PnL: {pnl:+.2%} @ ${xau_price:.2f}")
            else:
                pnl = 0
                print(f"⚠️  no live price — logging at entry price")

            opened_at = datetime.fromisoformat(pos.get("opened_at", datetime.now(timezone.utc).isoformat()))
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            mins = (datetime.now(timezone.utc) - opened_at).total_seconds() / 60

            try:
                async with db_pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE position_log SET
                            closed_at = NOW(),
                            exit_price = $1,
                            exit_type = 'MANUAL_CLOSE',
                            time_in_trade_min = $2,
                            pnl_pct = $3
                        WHERE broker = 'simulated'
                          AND asset = 'XAU/USD'
                          AND closed_at IS NULL
                        ORDER BY opened_at DESC
                        LIMIT 1
                    """, xau_price if xau_price > 0 else entry, round(mins, 2), round(pnl, 6))
            except Exception as e:
                print(f"    DB update error: {e}")

            xauusd_closed += 1

    # ── Clear Redis ────────────────────────────────────────────────────────
    await redis_client.set("gt:open_positions",   "[]")
    await redis_client.set("gt:xauusd_positions", "[]")
    await redis_client.set("gt:daily_pnl_pct",    "0.0")
    print(f"\n  Redis cleared (positions + daily P&L)")

    await redis_client.close()
    await db_pool.close()

    total_closed = alpaca_closed + xauusd_closed
    print(f"\n{'='*55}")
    print(f"Done:")
    print(f"  Alpaca:  {alpaca_closed} closed, {alpaca_failed} failed")
    print(f"  XAU/USD: {xauusd_closed} closed")
    print(f"  Total:   {total_closed} positions closed")
    if alpaca_failed > 0:
        print(f"  ⚠️  {alpaca_failed} Alpaca orders failed — run again when market opens")
    if not market_open:
        print("  ℹ️  Alpaca orders queued — execute at next market open (9:30 AM ET)")
    print()


if __name__ == "__main__":
    asyncio.run(main())
