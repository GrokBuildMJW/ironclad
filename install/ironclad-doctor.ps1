# Ironclad doctor (Windows / PowerShell) — read-only status of the desktop install + its endpoints.
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[doctor] $m" }

$proj = (Get-Location).Path
$cfgPath = Join-Path $proj ".ironclad\config.json"
if (-not (Test-Path $cfgPath)) {
  # S9 (#1232): no local bind yet — fall back to the recorded runtime (read-only; the doctor never writes).
  $runtime = Join-Path $HOME ".ironclad\runtime.json"
  if (Test-Path $runtime) { Say "no .ironclad in '$proj' — reporting the installed runtime."; $cfgPath = $runtime }
  else { Say "no .ironclad in '$proj' and no installed runtime — run install\ironclad-install.ps1 first."; exit 2 }
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$type = if ($cfg.type) { "$($cfg.type)".Trim().ToLower() } else { "desktop" }
$port = if ($cfg.port) { [int]$cfg.port } else { 8100 }

# a service that answers (even 4xx) counts as reachable; only a failed connect is "NOT reachable".
function Reach($url) { try { Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing | Out-Null; return $true }
                       catch { if ($_.Exception.Response) { return $true } else { return $false } } }

if ($type -eq 'spark') {
  $server = if ($env:GX10_SERVER_URL) { $env:GX10_SERVER_URL } elseif ($cfg.serverUrl) { $cfg.serverUrl } else { "" }
  Say "type=spark (thin client, no local engine)."
  if ($server) {
    try { $sh = Invoke-RestMethod -Uri "$server/health" -TimeoutSec 5
          Say "orchestrator ($server): version=$($sh.orchestrator_version)  memory=$($sh.memory)  warm=$($sh.warm)  language=$($sh.language)"; Say "OK reachable." }
    catch { Say "WARN: orchestrator not reachable ($server)." }
  } else { Say "no serverUrl in config — re-install." }
  return
}

$stamp = (Get-Content "$($cfg.engineDir)\VERSION" -Raw -ErrorAction SilentlyContinue); if ($stamp) { $stamp = $stamp.Trim() } else { $stamp = "unknown" }
Say "type=desktop  installed engine version=$stamp  model=$($cfg.model)  language=$($cfg.language)"
# Report the RUNNING engine's version too, and flag an installed-vs-running drift (#255): ironclad-install
# re-stamps the VERSION file + re-copies core/, but does NOT restart the live engine — the `ironclad`
# launcher restarts a stale engine on next start. The on-disk stamp alone can therefore hide a still-running
# old process; orchestrator_version is frozen at the engine's boot, so /health is the running truth.
$base = "http://127.0.0.1:$port"
$running = ""
try { $sh = Invoke-RestMethod -Uri "$base/health" -TimeoutSec 5; if ($sh.orchestrator_version) { $running = "$($sh.orchestrator_version)".Trim() } } catch {}
if ($running) {
  if ($running -eq $stamp) { Say "engine   ($base): reachable — running version $running (matches installed)." }
  else { Say "engine   ($base): reachable — WARN running version '$running' != installed '$stamp' — the live engine has NOT picked up this install; run 'ironclad' to restart it." }
} else {
  Say "engine   ($base): $(if (Reach "$base/health") { 'reachable (version unavailable)' } else { 'NOT reachable' })"
}
# #385: the Cold (memory) and Warm (Valkey) tiers are reported separately by /health so a silent warm
# outage cannot hide behind `memory: up`; surface both (up/down/off) when the engine answered.
if ($sh) { Say "memory tier (/health.memory): $($sh.memory)   |   warm tier (/health.warm): $($sh.warm)" }
# #601 isolation binding (status / active project / home) — surfaces an `unisolated` registry fallback.
if ($sh -and $sh.registry) { Say "registry (/health.registry): status=$($sh.registry.status)  active_project=$($sh.registry.active_project)  home=$($sh.registry.home)" }
if ($cfg.baseUrl)   { Say "model    ($($cfg.baseUrl)): $(if (Reach "$($cfg.baseUrl)/models") { 'reachable' } else { 'NOT reachable' })" }
if ($cfg.memoryUrl) { Say "memory   ($($cfg.memoryUrl)): $(if (Reach "$($cfg.memoryUrl)") { 'reachable' } else { 'NOT reachable' })" }
