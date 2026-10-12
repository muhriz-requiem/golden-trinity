# GoldenTrinity — Auto Market Hours Launcher
# Runs at 9:00 AM ET (13:00 UTC) weekdays
# Starts Docker containers then Python services

# Prevent PC sleep during market hours
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0

# Start Docker Desktop if not running
$docker = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $docker) {
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    Write-Host "Waiting for Docker to start..."
    Start-Sleep -Seconds 30
}

# Wait for Docker engine to be ready
$ready = $false
$attempts = 0
while (-not $ready -and $attempts -lt 20) {
    try {
        docker ps 2>&1 | Out-Null
        $ready = $true
    } catch {
        Start-Sleep -Seconds 5
        $attempts++
    }
}

# Start containers
cd D:\golden-trinity
docker-compose up -d
Write-Host "Docker containers started."
Start-Sleep -Seconds 10

# Start all Python services
Start-Process "D:\golden-trinity\start_services.bat"
Write-Host "GoldenTrinity services launched. Market opens in 30 min."
