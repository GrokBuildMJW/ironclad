# Ironclad launcher (Windows / PowerShell). Wired as the `ironclad` command by ironclad-install.ps1.
# Reads <project>\.ironclad\config.json, ensures the local engine is up (version-aware), runs the client.
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[ironclad] $m" }

$proj = (Get-Location).Path
$cfgPath = Join-Path $proj ".ironclad\config.json"
if (-not (Test-Path $cfgPath)) {
  # S9 (#1232): a new project dir must NOT demand a re-install (the runtime is already installed). If a global
  # runtime.json exists (written once by the installer), auto-bind THIS dir to it — mint a per-dir config.json —
  # and carry on. Only a genuine first-run (no runtime installed at all) still points at the installer.
  $runtime = Join-Path $HOME ".ironclad\runtime.json"
  if (Test-Path $runtime) {
    Say "no .ironclad in '$proj' — auto-binding to the installed runtime."
    New-Item -ItemType Directory -Force (Split-Path $cfgPath) | Out-Null
    Copy-Item $runtime $cfgPath -Force
    Set-Content (Join-Path $proj ".ironclad\.gitignore") "*" -Encoding ascii
  } else {
    Say "no .ironclad in '$proj' and no installed runtime — run install\ironclad-install.ps1 first."; exit 2
  }
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
if ($cfg.root -and (Test-Path (Join-Path $cfg.root ".install-incomplete"))) {
  Say "the installed runtime is incomplete (a previous install did not finish) — re-run the Ironclad installer to repair it."
  exit 2
}
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
function Workdir($url) { try { (Invoke-RestMethod -Uri $url -TimeoutSec 3).workdir } catch { $null } }
# #1252: resolve THIS folder EXACTLY as the engine does (pathlib.Path.resolve → what /health's `workdir`
# reports after its own resolve+chdir), so a directory junction/symlink or a case difference never spuriously
# restarts a healthy engine. Fall back to the raw path if the resolve call fails.
$projReal = try { (& $venvPy -c "import sys,pathlib;print(pathlib.Path(sys.argv[1]).resolve())" "$proj" 2>$null | Out-String).Trim() } catch { "" }
if (-not $projReal) { $projReal = $proj }

$stamp = (Get-Content "$engineDir\VERSION" -Raw -ErrorAction SilentlyContinue); if ($stamp) { $stamp = $stamp.Trim() } else { $stamp = "unknown" }
$started = $null; $reuse = $false
if (Probe "$base/health") {
  $rv = Version "$base/health"
  $wd = Workdir "$base/health"
  # #1252: reuse ONLY when it is THIS project's engine. An engine on the shared port bound to a DIFFERENT
  # workdir would silently serve the wrong project's vault/registry, so treat a workdir mismatch like a stale
  # version and restart for this project.
  if ($rv -eq $stamp -and $wd -and ("$wd".TrimEnd('\','/') -ieq $projReal.TrimEnd('\','/'))) { Say "engine already running on $base (version $stamp) — reusing."; $reuse = $true }
  else {
    # #47 / #1252: a stale-version OR wrong-project engine must not be reused; stop it (by listening port) and start fresh.
    if ($rv -ne $stamp) { Say "engine on $base is version '$rv', installed is '$stamp' — restarting." }
    else { Say "engine on $base is bound to a different project ('$wd') — restarting for '$proj'." }
    try {
      Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique |
        ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
      Start-Sleep -Milliseconds 800
    } catch {}
  }
}
if (-not $reuse) {
  # INSTALL-1 (#503): 'auto' lets the engine derive the topology from base_url at boot (loopback → server/
  # in-engine, remote → local), so a fresh default install BOOTS without baking a model host into the repo.
  $env:GX10_SETUP_TYPE = "auto"
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
  # #428: /exit ends the client → reliably stop the LOCAL engine it was using, whether THIS session
  # started it ($started) or reused a running/orphaned one ($reuse) — otherwise a background server.py
  # lingers on the port after /exit. Stop by the listening port (the same mechanism as the stale-engine
  # restart above). The spark path returned earlier (no local engine); single-tenant by design (one
  # engine per port).
  Say "stopping the engine on $base ..."
  try {
    Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty OwningProcess -Unique |
      ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
  } catch {}
}
