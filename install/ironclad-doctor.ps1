# Ironclad doctor (Windows / PowerShell) — read-only status of the desktop install + its endpoints.
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[doctor] $m" }

$proj = (Get-Location).Path
$cfgPath = Join-Path $proj ".ironclad\config.json"
if (-not (Test-Path $cfgPath)) { Say "no .ironclad in '$proj' — run install\ironclad-install.ps1 in this project first."; exit 2 }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$port = if ($cfg.port) { [int]$cfg.port } else { 8100 }

# a service that answers (even 4xx) counts as reachable; only a failed connect is "NOT reachable".
function Reach($url) { try { Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing | Out-Null; return $true }
                       catch { if ($_.Exception.Response) { return $true } else { return $false } } }

$stamp = (Get-Content "$($cfg.engineDir)\VERSION" -Raw -ErrorAction SilentlyContinue); if ($stamp) { $stamp = $stamp.Trim() } else { $stamp = "unknown" }
Say "type=desktop  local engine version=$stamp  model=$($cfg.model)  language=$($cfg.language)"
Say "engine   (http://127.0.0.1:$port): $(if (Reach "http://127.0.0.1:$port/health") { 'reachable' } else { 'NOT reachable' })"
if ($cfg.baseUrl)   { Say "model    ($($cfg.baseUrl)): $(if (Reach "$($cfg.baseUrl)/models") { 'reachable' } else { 'NOT reachable' })" }
if ($cfg.memoryUrl) { Say "memory   ($($cfg.memoryUrl)): $(if (Reach "$($cfg.memoryUrl)") { 'reachable' } else { 'NOT reachable' })" }
