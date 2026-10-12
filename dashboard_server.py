"""
dashboard_server.py — GoldenTrinity Dashboard Server
=====================================================
Serves the dashboard at http://localhost:9000
Acts as a proxy for all service API calls (fixes CORS completely).
Also provides a /cmd endpoint so the dashboard can run real commands.
Also provides /api/portfolio for the portfolio chart.

Run: python dashboard_server.py
Open: http://localhost:9000
"""
import os, asyncio, json, time, webbrowser
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from dotenv import load_dotenv
import httpx
import asyncpg
import uvicorn

load_dotenv()

BASE_DIR = r"D:\golden-trinity"

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5432))
DB_NAME = os.getenv("DB_NAME", "goldentrinity")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

SERVICES = {
    "macro":     8001,
    "risk":      8002,
    "execution": 8003,
    "signal":    8004,
    "xauusd":    8005,
}

ws_clients: list[WebSocket] = []
db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, database=DB_NAME,
            user=DB_USER, password=DB_PASS, min_size=1, max_size=3
        )
        print("[dashboard] DB connected")
    except Exception as e:
        print(f"[dashboard] DB connection failed: {e} — portfolio chart will be empty")
        db_pool = None
    await asyncio.sleep(1)
    webbrowser.open("http://localhost:9000")
    yield
    if db_pool:
        await db_pool.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Dashboard HTML ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    path = os.path.join(BASE_DIR, "dashboard.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Portfolio chart data ───────────────────────────────────────────────────
@app.get("/api/portfolio")
async def portfolio():
    """
    Returns every closed trade with cumulative P&L in dollars.
    Used by the portfolio chart in the dashboard.
    """
    if not db_pool:
        return JSONResponse({"trades": [], "summary": {}, "error": "DB not connected"})
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    closed_at,
                    opened_at,
                    asset,
                    direction,
                    fill_price,
                    exit_price,
                    size_pct,
                    pnl_pct,
                    exit_type,
                    session,
                    regime_at_entry,
                    confidence_at_entry
                FROM position_log
                WHERE pnl_pct IS NOT NULL
                  AND closed_at IS NOT NULL
                ORDER BY closed_at ASC
            """)

        # Use 100000 as base NAV (paper trading starting capital)
        NAV      = 100000.0
        cum      = 0.0
        trades   = []
        wins     = 0

        for r in rows:
            dollar_pnl = float(r["pnl_pct"]) * float(r["size_pct"] or 0.02) * NAV
            cum       += dollar_pnl
            won        = float(r["pnl_pct"]) > 0
            if won:
                wins += 1
            trades.append({
                "time":        r["closed_at"].isoformat(),
                "opened_at":   r["opened_at"].isoformat(),
                "asset":       r["asset"],
                "direction":   r["direction"],
                "pnl_pct":     round(float(r["pnl_pct"]) * 100, 4),
                "dollar_pnl":  round(dollar_pnl, 2),
                "cumulative":  round(cum, 2),
                "exit_type":   r["exit_type"],
                "session":     r["session"],
                "regime":      r["regime_at_entry"],
                "confidence":  round(float(r["confidence_at_entry"] or 0) * 100, 1),
                "won":         won,
            })

        n = len(trades)
        return JSONResponse({
            "trades": trades,
            "summary": {
                "total_dollars":  round(cum, 2),
                "total_trades":   n,
                "wins":           wins,
                "losses":         n - wins,
                "win_rate":       round(wins / n * 100, 1) if n > 0 else 0,
                "initial_nav":    NAV,
                "current_nav":    round(NAV + cum, 2),
            }
        })
    except Exception as e:
        return JSONResponse({"trades": [], "summary": {}, "error": str(e)}, status_code=500)


# ── N8N health ─────────────────────────────────────────────────────────────
@app.get("/proxy/n8n/health")
async def n8n_health_route():
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get("http://localhost:5678/healthz")
            healthy = r.status_code == 200
            return JSONResponse({"ok": healthy, "status": r.status_code})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


# ── Service proxy ──────────────────────────────────────────────────────────
@app.api_route("/proxy/{service}/{path:path}", methods=["GET","POST","DELETE"])
async def proxy(service: str, path: str, request: Request):
    port = SERVICES.get(service)
    if not port:
        return JSONResponse({"error": f"Unknown service: {service}"}, status_code=404)
    url  = f"http://localhost:{port}/{path}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.request(
                method=request.method, url=url, content=body,
                headers={"Content-Type": "application/json"} if body else {}
            )
            return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e), "offline": True}, status_code=503)


# ── WebSocket CMD ──────────────────────────────────────────────────────────
@app.websocket("/ws/cmd")
async def cmd_websocket(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            data  = await ws.receive_json()
            cmd   = data.get("cmd", "").strip()
            panel = data.get("panel", "exec")
            if not cmd:
                continue
            allowed = ["docker","curl","python","dir","cd","echo","type","ping",
                       "ipconfig","netstat","tasklist","venv\\Scripts\\python.exe","uvicorn"]
            if not any(cmd.lower().startswith(a) for a in allowed):
                await ws.send_json({"panel": panel, "line": f"[dashboard] Not allowed: {cmd}", "type": "err"})
                continue
            await ws.send_json({"panel": panel, "line": f"$ {cmd}", "type": "dim"})
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=BASE_DIR, shell=True
                )
                async for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        await ws.send_json({"panel": panel, "line": text, "type": ""})
                await proc.wait()
                await ws.send_json({"panel": panel, "line": f"[exit {proc.returncode}]", "type": "dim"})
            except Exception as e:
                await ws.send_json({"panel": panel, "line": f"Error: {e}", "type": "err"})
    except WebSocketDisconnect:
        ws_clients.remove(ws)


# ── Shutdown endpoint ─────────────────────────────────────────────────────
@app.post("/shutdown")
async def shutdown_all():
    """Close all open positions (GLD + XAU/USD) and halt the system."""
    results = {"gld": [], "xauusd": [], "errors": []}
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Get and close GLD positions
        try:
            r = await client.get("http://localhost:8003/api/execution/positions")
            positions = r.json().get("positions", [])
            for pos in positions:
                pid = str(pos.get("db_id") or pos.get("id", ""))
                try:
                    await client.post(f"http://localhost:8003/api/execution/close/{pid}")
                    results["gld"].append(pid)
                except Exception as e:
                    results["errors"].append(f"GLD {pid}: {e}")
        except Exception as e:
            results["errors"].append(f"GLD fetch: {e}")

        # Get and close XAU/USD positions
        try:
            r = await client.get("http://localhost:8005/api/xauusd/positions")
            xau_positions = r.json().get("positions", [])
            for pos in xau_positions:
                pid = pos.get("id", "")
                try:
                    await client.post("http://localhost:8005/api/xauusd/close",
                                      json={"position_id": pid})
                    results["xauusd"].append(pid)
                except Exception as e:
                    results["errors"].append(f"XAU {pid}: {e}")
        except Exception as e:
            results["errors"].append(f"XAU/USD fetch: {e}")

    # FIX: was missing closing } on dict literal — caused SyntaxError, shutdown button did nothing
    results["closed"] = {
        "gld":    len(results["gld"]),
        "xauusd": len(results["xauusd"]),
    }
    gld_closed = results["closed"]["gld"]
    xau_closed = results["closed"]["xauusd"]
    print(f"[shutdown] Closed {gld_closed} GLD + {xau_closed} XAU/USD positions")
    return JSONResponse(results)


@app.post("/stop_services")
async def stop_services():
    """Kill all 5 Python service processes by port."""
    import subprocess
    ports_killed = []
    for port in [8001, 8002, 8003, 8004, 8005]:
        try:
            result = subprocess.run(
                ["cmd", "/c", f"netstat -ano | findstr :{port} | findstr LISTENING"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
                        ports_killed.append(port)
        except Exception as e:
            print(f"[stop_services] Port {port} error: {e}")
    print(f"[stop_services] Killed services on ports: {ports_killed}")
    return JSONResponse({"stopped_ports": ports_killed, "status": "ok"})


@app.get("/status")
async def status():
    results = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, port in SERVICES.items():
            try:
                t0   = time.time()
                resp = await client.get(f"http://localhost:{port}/api/health")
                ms   = round((time.time() - t0) * 1000, 1)
                results[name] = {"ok": True, "ms": ms, **resp.json()}
            except Exception as e:
                results[name] = {"ok": False, "error": str(e)}
    return results


if __name__ == "__main__":
    print("=" * 50)
    print(" GoldenTrinity Dashboard — http://localhost:9000")
    print("=" * 50)
    uvicorn.run(app, host="localhost", port=9000, log_level="warning")
