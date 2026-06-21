# Ironclad launcher (Windows / PowerShell). Wired as the `ironclad` command by ironclad-install.ps1.
# Reads <project>\.ironclad\config.json, ensures the local engine is up (version-aware), runs the client.
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[ironclad] $m" }

$proj = (Get-Location).Path
$cfgPath = Join-Path $proj ".ironclad\config.json"
if (-not (Test-Path $cfgPath)) { Say "no .ironclad in '$proj' — run install\ironclad-install.ps1 in this project first."; exit 2 }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$type = if ($cfg.type) { "$($cfg.type)".Trim().ToLower() } else { "desktop" }

# spark: thin client → a remote orchestrator (no local engine/venv). 'desktop' (default) runs the engine locally.
if ($type -eq 'spark') {
  $server = if ($env:GX10_SERVER_URL) { $env:GX10_SERVER_URL } elseif ($cfg.serverUrl) { $cfg.serverUrl } else { "" }
  if (-not $server) { Say "(spark) no serverUrl in config — re-install."; exit 2 }
  if (-not ($cfg.clientCli -and (Get-Command node -ErrorAction SilentlyContinue))) { Say "(spark) needs the Node client — re-install with Node present."; exit 2 }
  Say "(spark) client -> $server  (codedir $proj)"
  node "$($cfg.clientCli)" --server $server --codedir $proj
  exit 0
}

$port      = if ($cfg.port) { [int]$cfg.port } else { 8100 }
$base      = "http://127.0.0.1:$port"
$venvPy    = "$($cfg.venv)\Scripts\python.exe"
$engineDir = $cfg.engineDir
if (-not (Test-Path $venvPy)) { Say "venv python missing ($venvPy) — re-run install\ironclad-install.ps1."; exit 2 }

function Probe($url)   { try { (Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing).StatusCode -eq 200 } catch { $false } }
function Version($url) { try { (Invoke-RestMethod -Uri $url -TimeoutSec 3).orchestrator_version } catch { $null } }

$stamp = (Get-Content "$engineDir\VERSION" -Raw -ErrorAction SilentlyContinue); if ($stamp) { $stamp = $stamp.Trim() } else { $stamp = "unknown" }
$started = $null; $reuse = $false
if (Probe "$base/health") {
  $rv = Version "$base/health"
  if ($rv -eq $stamp) { Say "engine already running on $base (version $stamp) — reusing."; $reuse = $true }
  else {
    # #47: a stale engine keeps serving the old code; stop it (by listening port) and start fresh.
    Say "engine on $base is version '$rv', installed is '$stamp' — restarting."
    try {
      Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
      Start-Sleep -Milliseconds 800
    } catch {}
  }
}
if (-not $reuse) {
  $env:GX10_SETUP_TYPE = "local"
  $env:GX10_BASE_URL   = $cfg.baseUrl
  $env:GX10_MEMORY_URL = $cfg.memoryUrl
  $env:GX10_MODEL      = $cfg.model
  $env:GX10_WORKDIR    = $proj
  $env:GX10_PLUGINS_DIR= "$($cfg.root)\skills"
  $env:GX10_LANGUAGE   = if ($cfg.language) { $cfg.language } else { "en" }
  $env:GX10_ORCHESTRATOR_VERSION = $stamp
  # optional, config-driven tuning (absent in a default install → engine defaults; a deployment may set them)
  if ($cfg.warmUrl)               { $env:GX10_WARM_URL = $cfg.warmUrl }
  if ($cfg.claudeBin)             { $env:GX10_CLAUDE_BIN = $cfg.claudeBin }
  if ($cfg.fanoutConcurrency)     { $env:GX10_FANOUT_CONCURRENCY = "$($cfg.fanoutConcurrency)" }
  if ($cfg.workersMaxTokens)      { $env:GX10_WORKERS_MAX_TOKENS = "$($cfg.workersMaxTokens)" }
  if ($cfg.workersMaxBatchTokens) { $env:GX10_WORKERS_MAX_BATCH_TOKENS = "$($cfg.workersMaxBatchTokens)" }
  Say "starting the engine ($base, version $stamp) ..."
  $svArgs = @("$engineDir\server.py","--host","127.0.0.1","--port","$port")
  if ($cfg.engineConfig -and (Test-Path $cfg.engineConfig)) { $svArgs += @("--config","$($cfg.engineConfig)") }
  $started = Start-Process -FilePath $venvPy -ArgumentList $svArgs -WindowStyle Hidden -PassThru
  for ($i=0; $i -lt 30; $i++) { if (Probe "$base/health") { break }; Start-Sleep -Milliseconds 700 }
  if (-not (Probe "$base/health")) { Say "ERROR: engine did not become healthy."; if ($started) { Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue }; exit 1 }
}

try {
  if ($cfg.clientCli -and (Get-Command node -ErrorAction SilentlyContinue)) {
    node "$($cfg.clientCli)" --server $base --codedir $proj
  } else {
    $env:GX10_SERVER_URL = $base
    & $venvPy "$engineDir\client.py" --codedir $proj
  }
} finally {
  if ($started) { Say "stopping the engine (pid $($started.Id))."; Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue }
}
