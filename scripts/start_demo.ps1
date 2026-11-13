# One-shot demo launcher for Windows.
#
# What it does, in order:
#   1. verify docker is reachable
#   2. start any Nexent container that exists but is not running
#   3. wait for the northbound API health endpoint
#   4. read the northbound access key from postgres
#   5. start the workbench web server (skipped if the port is already serving)
#   6. open the portal page
#
# Idempotent: running it twice is safe. Docker is required for steps 2-4 only;
# if the containers are already up, steps 2-4 are no-ops.
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI and would
# garble any non-ASCII text saved without a BOM.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1
#   ... -Port 18765 -NoBrowser
#   ... -CheckOnly      # print status, change nothing

param(
    [int]$Port = 18765,
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
# Invoke-WebRequest draws a progress bar on PowerShell 5.1 which repaints over the
# lines we just printed, making the whole run look garbled. Turn it off.
$ProgressPreference = 'SilentlyContinue'
$Repo = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo '.venv\Scripts\python.exe'

# Core containers. supabase-* and *-mini variants are part of the platform
# install but the agent pipeline runs without them, so they are listed separately.
$CoreContainers = @(
    'nexent-postgresql', 'nexent-elasticsearch', 'nexent-minio',
    'nexent-config', 'nexent-runtime', 'nexent-northbound', 'nexent-web',
    'nexent-mcp', 'nexent-data-process', 'urban-twin-mcp'
)
$OptionalContainers = @('nexent-redis', 'nexent-runtime-sandbox')

function Write-Step([string]$Text) { Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "   [ok]   $Text" -ForegroundColor Green }
function Write-Bad([string]$Text)  { Write-Host "   [fail] $Text" -ForegroundColor Red }
function Write-Note([string]$Text) { Write-Host "   $Text" -ForegroundColor DarkGray }

# --- 1. docker -------------------------------------------------------------
Write-Step 'docker'
$dockerOut = & docker version --format '{{.Server.Version}}' 2>$null
if (-not $?) {
    Write-Bad 'docker is not reachable. Start Docker Desktop, then re-run this script.'
    exit 2
}
Write-Ok "docker engine $dockerOut"

# --- 2. containers ---------------------------------------------------------
Write-Step 'containers'
$toStart = @()
foreach ($name in ($CoreContainers + $OptionalContainers)) {
    $exists = & docker ps -a --filter "name=^/$name$" --format '{{.Names}}' 2>$null
    if (-not $exists) {
        if ($CoreContainers -contains $name) { Write-Bad "$name missing (not created on this machine)" }
        else { Write-Note "$name missing (optional)" }
        continue
    }
    $running = & docker inspect -f '{{.State.Running}}' $name 2>$null
    if ($running -eq 'true') { Write-Ok "$name running" }
    else { Write-Note "$name stopped - will start"; $toStart += $name }
}
if ($toStart.Count -gt 0 -and -not $CheckOnly) {
    foreach ($name in $toStart) {
        & docker start $name | Out-Null
        if ($?) { Write-Ok "$name started" } else { Write-Bad "$name failed to start" }
    }
}

# --- 3. northbound health --------------------------------------------------
Write-Step 'northbound api (http://localhost:5013)'
$healthy = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri 'http://localhost:5013/nb/v1/health' -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if ($healthy) { Write-Ok 'health 200' }
else {
    Write-Bad 'northbound api did not come up in 120s'
    Write-Note 'check: docker logs nexent-northbound --tail 50'
    exit 3
}

# --- 4. access key ---------------------------------------------------------
Write-Step 'access key'
$key = ''
try {
    $key = (& docker exec nexent-postgresql psql -U root -d nexent -t -A `
        -c "select access_key from nexent.user_token_info_t where token_id=2 and delete_flag='N';" 2>$null | Out-String).Trim()
} catch { $key = '' }
if ($key) { Write-Ok "key ${key}" }
else {
    Write-Bad 'no access key found in nexent.user_token_info_t (token_id=2)'
    Write-Note 'the workbench chat needs it; portal and map pages still work without it'
}

# --- 5. web server ---------------------------------------------------------
Write-Step "workbench web server (port $Port)"
$already = $false
try {
    $probe = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
    if ($probe.StatusCode -eq 200) { $already = $true }
} catch { $already = $false }

if ($already) {
    Write-Ok "already serving on $Port - left as is"
} elseif ($CheckOnly) {
    Write-Note "not running (check-only mode, nothing started)"
} else {
    if (-not (Test-Path $Python)) {
        Write-Bad "python not found: $Python"
        Write-Note 'run: uv sync --locked'
        exit 4
    }
    # Start-Process inherits the environment of this shell, so set it here.
    $env:PYTHONPATH             = Join-Path $Repo 'src'
    $env:NEXENT_BASE_URL        = 'http://localhost:5013'
    $env:NEXENT_AGENT_NAME      = 'workbench_main'
    $env:NEXENT_API_KEY         = $key
    $env:NEXENT_WEB_BASE        = 'http://localhost:3000'
    Start-Process -FilePath $Python `
        -ArgumentList @('-m', 'decision_poc.web_demo', '--port', "$Port") `
        -WorkingDirectory $Repo -WindowStyle Hidden
    $up = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 700
        try {
            $probe = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
            if ($probe.StatusCode -eq 200) { $up = $true; break }
        } catch { }
    }
    if ($up) { Write-Ok "serving on $Port" }
    else { Write-Bad "web server did not answer on $Port"; exit 5 }
}

# --- 6. open ---------------------------------------------------------------
$portal = "http://127.0.0.1:$Port/"
Write-Step 'entries'
Write-Note "portal     $portal"
Write-Note "workbench  ${portal}workbench"
Write-Note "2.5d map   ${portal}map2d"
Write-Note "twin 3d    ${portal}twin3d"
Write-Note "platform   http://localhost:3000/zh/workbench  (needs a browser login)"
if (-not $NoBrowser -and -not $CheckOnly) {
    Start-Process $portal
}
Write-Host 'done.' -ForegroundColor Green
