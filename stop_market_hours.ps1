# GoldenTrinity — Market Close Shutdown
# Runs at 4:15 PM ET (20:15 UTC) weekdays
# Gracefully stops services and optionally re-enables sleep

Write-Host "Market closed. Stopping GoldenTrinity services..."

# Kill Python service windows
Get-Process | Where-Object { $_.MainWindowTitle -match "GT-" } | Stop-Process -Force

# Stop Docker containers (keeps data intact)
cd D:\golden-trinity
docker-compose stop
Write-Host "Containers stopped (data preserved)."

# Re-enable sleep (optional — comment out if you want PC to stay on)
powercfg /change standby-timeout-ac 30
powercfg /change monitor-timeout-ac 10

Write-Host "GoldenTrinity shutdown complete."
