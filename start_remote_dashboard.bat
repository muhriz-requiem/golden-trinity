@echo off
REM GoldenTrinity — Remote Dashboard Tunnel
REM Creates a public URL you can open on any device, any network.
REM Uses Cloudflare Quick Tunnels — completely free, no account, no credit card.
REM The URL changes each time you run this script (that is normal).
REM
REM HOW TO USE:
REM   1. Make sure start_services.bat is already running (dashboard must be on :9000)
REM   2. Double-click this file
REM   3. A public URL appears in the window — e.g. https://abc-def-ghi.trycloudflare.com
REM   4. Open that URL on your phone, other PC, anywhere
REM   5. Close this window to shut off the tunnel (other devices lose access)
REM
REM SECURITY NOTE:
REM   Anyone with the URL can view the dashboard while the tunnel is open.
REM   The URL is random and unguessable, but do not share it publicly.
REM   The dashboard is read-only — no one can halt the system through it.
REM   Close this window when done.

cd /d D:\golden-trinity
echo.
echo ============================================================
echo   GoldenTrinity Remote Dashboard
echo ============================================================
echo.
echo Starting tunnel to http://localhost:9000 ...
echo.
echo When the URL appears below, open it on any device.
echo Keep this window open to maintain the connection.
echo Close this window to disconnect remote access.
echo.
echo ============================================================
echo.

D:\golden-trinity\cloudflared.exe tunnel --url http://localhost:9000

echo.
echo Tunnel closed.
pause
