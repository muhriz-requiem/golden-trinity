# GoldenTrinity -- Full System Status Check
# Run: powershell -ExecutionPolicy Bypass -File check_status.ps1

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  GoldenTrinity System Status" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm') UTC" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# 1. Docker Containers
Write-Host ""
Write-Host "[ DOCKER CONTAINERS ]" -ForegroundColor Yellow
$containers = @("golden-trinity-timescaledb-1","golden-trinity-redis-1","golden-trinity-n8n-1","golden-trinity-grafana-1")
foreach ($c in $containers) {
    $status = docker inspect --format="{{.State.Status}}" $c 2>$null
    if ($status -eq "running") {
        Write-Host "  OK    $c" -ForegroundColor Green
    } else {
        Write-Host "  DOWN  $c" -ForegroundColor Red
    }
}

# 2. Python Services
Write-Host ""
Write-Host "[ PYTHON SERVICES ]" -ForegroundColor Yellow
$services = @(
    @{name="macro_service";     port=8001},
    @{name="risk_service";      port=8002},
    @{name="execution_service"; port=8003},
    @{name="signal_service";    port=8004},
    @{name="xauusd_service";    port=8005}
)
foreach ($svc in $services) {
    try {
        $resp  = Invoke-RestMethod "http://localhost:$($svc.port)/api/health" -TimeoutSec 3
        $lat   = $resp.latency_ms
        $extra = ""
        if ($svc.name -eq "signal_service") {
            $c = if ($resp.collapse_alert) { " COLLAPSE!" } else { "" }
            $extra = " | model=$($resp.model) buf=$($resp.buffer_size)/60$c"
        }
        if ($svc.name -eq "execution_service") {
            $pnlPct = [math]::Round([double]$resp.daily_pnl_pct * 100, 3)
            $extra  = " | nav=`$$($resp.nav) pos=$($resp.open_positions) pnl=${pnlPct}%"
        }
        if ($svc.name -eq "risk_service") {
            $extra = " | cb_samples=$($resp.cb_samples)"
        }
        if ($svc.name -eq "xauusd_service") {
            $extra = " | XAU=`$$($resp.last_price) pos=$($resp.open_positions) training=$($resp.training_active)"
        }
        Write-Host "  OK    $($svc.name) :$($svc.port) ${lat}ms$extra" -ForegroundColor Green
    } catch {
        Write-Host "  DOWN  $($svc.name) :$($svc.port)" -ForegroundColor Red
    }
}

# 3. Redis State
Write-Host ""
Write-Host "[ REDIS STATE ]" -ForegroundColor Yellow

$keys = @("gt:regime","gt:nav","gt:daily_pnl_pct","gt:system_halted","gt:halt_reason","gt:collapse_alert")
foreach ($key in $keys) {
    $val = docker exec golden-trinity-redis-1 redis-cli GET $key 2>$null
    if ($val) {
        $color = "White"
        if ($key -eq "gt:system_halted"  -and $val -eq "true") { $color = "Red"   }
        if ($key -eq "gt:collapse_alert" -and $val -eq "true") { $color = "Red"   }
        if ($key -eq "gt:regime" -and $val -eq "BULLISH")      { $color = "Green" }
        if ($key -eq "gt:regime" -and $val -eq "BEARISH")      { $color = "Red"   }
        if ($key -eq "gt:daily_pnl_pct") {
            $pct = [math]::Round([double]$val * 100, 3)
            $color = if ($pct -lt -2) { "Red" } elseif ($pct -gt 0) { "Green" } else { "White" }
            Write-Host "  $key = ${pct}%" -ForegroundColor $color
            continue
        }
        Write-Host "  $key = $val" -ForegroundColor $color
    } else {
        Write-Host "  $key = (not set)" -ForegroundColor DarkGray
    }
}

# Latest GLD signal
$sig = docker exec golden-trinity-redis-1 redis-cli GET gt:signal:current 2>$null
if ($sig) {
    $s = $sig | ConvertFrom-Json
    $c = if ($s.passed_gate) { "Green" } else { "DarkGray" }
    Write-Host "  gt:signal = dir=$($s.direction) P(UP)=$($s.p_up) P(DN)=$($s.p_down) passed=$($s.passed_gate) price=`$$($s.price)" -ForegroundColor $c
} else {
    Write-Host "  gt:signal = none (buffer filling)" -ForegroundColor DarkGray
}

# XAU/USD price
$xp = docker exec golden-trinity-redis-1 redis-cli GET gt:xauusd_price 2>$null
if ($xp) { Write-Host "  gt:xauusd_price = `$$([math]::Round([double]$xp,2))" -ForegroundColor Cyan }

# Open positions
$gldPos = docker exec golden-trinity-redis-1 redis-cli GET gt:open_positions 2>$null
if ($gldPos -and $gldPos -ne "[]") {
    $arr = $gldPos | ConvertFrom-Json
    Write-Host "  gt:open_positions = $($arr.Count) open" -ForegroundColor Cyan
    foreach ($p in $arr) {
        Write-Host "    $($p.direction) $($p.asset) qty=$($p.qty) fill=`$$($p.fill_price) SL=`$$($p.stop_loss) TP=`$$($p.take_profit)" -ForegroundColor Cyan
    }
} else {
    Write-Host "  gt:open_positions = empty" -ForegroundColor DarkGray
}

$xauPos = docker exec golden-trinity-redis-1 redis-cli GET gt:xauusd_positions 2>$null
if ($xauPos -and $xauPos -ne "[]") {
    $arr = $xauPos | ConvertFrom-Json
    Write-Host "  gt:xauusd_positions = $($arr.Count) open" -ForegroundColor Cyan
} else {
    Write-Host "  gt:xauusd_positions = empty" -ForegroundColor DarkGray
}

# 4. Database
Write-Host ""
Write-Host "[ DATABASE ]" -ForegroundColor Yellow
$tables = @("macro_snapshots","signal_log","position_log","risk_alert_log")
foreach ($t in $tables) {
    $n = docker exec golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity -t -c "SELECT COUNT(*) FROM $t" 2>$null
    $n = $n.Trim()
    $c = if ([int]$n -gt 0) { "Green" } else { "DarkGray" }
    Write-Host "  $t = $n rows" -ForegroundColor $c
}
$today = docker exec golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity -t -c "SELECT COUNT(*) FROM position_log WHERE opened_at >= CURRENT_DATE AND pnl_pct IS NOT NULL" 2>$null
Write-Host "  trades today (closed) = $($today.Trim())" -ForegroundColor Cyan

# 5. Alpaca Quote Test
Write-Host ""
Write-Host "[ ALPACA QUOTE TEST ]" -ForegroundColor Yellow
$envVars = @{}
Get-Content "D:\golden-trinity\.env" | ForEach-Object {
    if ($_ -match "^([^#][^=]*)=(.+)$") { $envVars[$matches[1].Trim()] = $matches[2].Trim() }
}
try {
    $h = @{ "APCA-API-KEY-ID"=$envVars["ALPACA_KEY"]; "APCA-API-SECRET-KEY"=$envVars["ALPACA_SECRET"] }
    $q = Invoke-RestMethod "https://data.alpaca.markets/v2/stocks/GLD/quotes/latest?feed=iex" -Headers $h -TimeoutSec 5
    $bid = $q.quote.bp; $ask = $q.quote.ap
    if ($ask -gt 0) {
        $spread = [math]::Round((($ask-$bid)/(($ask+$bid)/2))*100,4)
        $ok     = if ($spread -lt 0.05) { "within limit" } else { "ABOVE LIMIT" }
        Write-Host "  GLD bid=`$$bid ask=`$$ask spread=${spread}% ($ok)" -ForegroundColor Green
    } else {
        Write-Host "  GLD prices = 0 (market closed)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  FAILED: $_" -ForegroundColor Red
}

# 6. Summary
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
$tc = [int](docker exec golden-trinity-timescaledb-1 psql -U gt_user -d goldentrinity -t -c "SELECT COUNT(*) FROM position_log WHERE pnl_pct IS NOT NULL" 2>$null).Trim()
$pct = [math]::Min(100, [math]::Round($tc / 200.0 * 100))
$tradeColor = if ($tc -ge 200) { "Green" } else { "White" }
Write-Host "  Trades to go-live eval: $tc / 200 (${pct}%)" -ForegroundColor $tradeColor
Write-Host "  buffer=60 + trades in DB = WORKING" -ForegroundColor White
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
