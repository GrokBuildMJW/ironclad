# Ironclad one-shot desktop installer (Windows / PowerShell). SECRET-FREE.
#
# Run once from a clone of the Ironclad repo, inside the project folder you want to drive:
#
#   install\ironclad-install.ps1                                  # localhost endpoint defaults
#   install\ironclad-install.ps1 -BaseUrl http://host:8000/v1 -Model my-model
#   install\ironclad-install.ps1 -WarmUrl redis://host:6379          # bind the Valkey/Redis warm tier
#
# Builds a venv, installs the engine (incl. the warm-cache client so the warm tier works whenever
# GX10_WARM_URL / -WarmUrl is set), builds the optional TypeScript client, writes a project config
# (.ironclad\config.json) and wires an `ironclad` command into your PowerShell profile. Defaults point at
# localhost; override any endpoint via a flag or a GX10_* env var. No host/IP/path is baked into the repo.
param(
  [string]$BaseUrl    = $(if ($env:GX10_BASE_URL)   { $env:GX10_BASE_URL }   else { "http://127.0.0.1:8000/v1" }),
  [string]$MemoryUrl  = $(if ($env:GX10_MEMORY_URL) { $env:GX10_MEMORY_URL } else { "" }),
  [string]$WarmUrl    = $(if ($env:GX10_WARM_URL)   { $env:GX10_WARM_URL }   else { "" }),
  [string]$Model      = $(if ($env:GX10_MODEL)      { $env:GX10_MODEL }      else { "qwen3.6-35b" }),
  [int]$Port          = $(if ($env:GX10_PORT)       { [int]$env:GX10_PORT }  else { 8100 }),
  [string]$Language   = $(if ($env:GX10_LANGUAGE)   { $env:GX10_LANGUAGE }   else { "en" }),
  [string]$Project    = (Get-Location).Path,
  [string]$ConnectionFile = $(if ($env:GX10_CONNECTION_FILE) { $env:GX10_CONNECTION_FILE } else { "" })
)
$ErrorActionPreference = "Stop"
function Say($m) { Write-Host "[install] $m" }

$ScriptDir = $PSScriptRoot
$Root = (Resolve-Path "$ScriptDir\..").Path

function FindDir  { param($cands) foreach ($c in $cands) { if (Test-Path $c -PathType Container) { return (Resolve-Path $c).Path } } return $null }
function FindFile { param($cands) foreach ($c in $cands) { if (Test-Path $c -PathType Leaf) { return (Resolve-Path $c).Path } } return $null }
$EngineDir = FindDir @("$Root\engine","$Root\core\engine"); if (-not $EngineDir) { throw "engine/ not found under $Root" }
$PyProject = FindFile @("$Root\pyproject.toml","$Root\core\pyproject.toml"); if (-not $PyProject) { throw "pyproject.toml not found under $Root" }
$PkgRoot = Split-Path $PyProject
$InkDir = FindDir @("$Root\clients\ink","$Root\..\clients\ink","$PkgRoot\clients\ink")

# optional private overlay: pull endpoint defaults from a connection.json (never in the export) when the
# value wasn't set via CLI/env. Keeps the operator's host out of the repo.
if ($ConnectionFile -and (Test-Path $ConnectionFile)) {
  Say "reading endpoint defaults from $ConnectionFile"
  try {
    $conn = (Get-Content $ConnectionFile -Raw | ConvertFrom-Json).connection
    if (-not $env:GX10_BASE_URL -and $conn.base_url) { $BaseUrl = $conn.base_url }
    if (-not $env:GX10_MODEL    -and $conn.model)    { $Model   = $conn.model }
  } catch { Say "WARN: could not read $ConnectionFile — using defaults." }
}

# --- prerequisites ---
$py = (Get-Command python -ErrorAction SilentlyContinue); if (-not $py) { throw "python not found (need >= 3.10)" }
$okver = & python -c "import sys; print(1 if sys.version_info[:2] >= (3,10) else 0)"
if ($okver.Trim() -ne "1") { throw "Python >= 3.10 required" }
Say "root=$Root  project=$Project  model=$Model  base_url=$BaseUrl"

# --- venv + engine ---
$Venv = "$Root\.venv"
if (-not (Test-Path "$Venv\Scripts\python.exe")) { Say "creating venv ($Venv) ..."; & python -m venv $Venv }
$VenvPy = "$Venv\Scripts\python.exe"
Say "installing the engine (pip install -e .[engine,memory]) ..."
& $VenvPy -m pip install --quiet --upgrade pip
# Install the warm-cache client (the `memory` extra -> redis>=5) alongside the engine so the warm tier
# works whenever GX10_WARM_URL is set (via env or -WarmUrl), matching the Docker image; warm stays OFF
# at runtime until a URL is configured. ".[extra]" from the pkg dir -- pip rejects "C:\abs\path[extra]".
Push-Location $PkgRoot
& $VenvPy -m pip install --quiet -e ".[engine,memory]"
Pop-Location
& $VenvPy -c "import ack, pydantic" | Out-Null

# --- optional TypeScript client ---
$ClientCli = ""
if ($InkDir -and (Get-Command node -ErrorAction SilentlyContinue)) {
  # The ink client needs Node >= 22 (clients/ink package.json engines); npm does NOT enforce `engines` by
  # default, so gate it here and skip with a clear message on older Node rather than emit a cryptic build error.
  $nodeMajor = 0; try { $nodeMajor = [int]((& node -p "process.versions.node.split('.')[0]") 2>$null) } catch {}
  if ($nodeMajor -ge 22) {
    Say "building the ink client ..."
    Push-Location $InkDir; npm install --silent | Out-Null; npm run build --silent | Out-Null; Pop-Location
    $ClientCli = "$InkDir\dist\cli.js"
  } else { Say "skipping ink client — Node >= 22 required (have $(node -v)); the legacy Python client still works." }
} else { Say "skipping ink client (no Node or clients/ink absent) — the legacy Python client still works." }

# --- project config ---
$dot = "$Project\.ironclad"; New-Item -ItemType Directory -Force $dot | Out-Null
$cfg = [ordered]@{ type="desktop"; root=$Root; venv=$Venv; engineDir=$EngineDir; clientCli=$ClientCli;
                   baseUrl=$BaseUrl; memoryUrl=$MemoryUrl; warmUrl=$WarmUrl; model=$Model; port=$Port; language=$Language }
$cfg | ConvertTo-Json | Set-Content "$dot\config.json" -Encoding utf8
Set-Content "$dot\.gitignore" "*" -Encoding ascii
Say "bound project: $dot\config.json"

# --- wire the `ironclad` command into the PowerShell profile ---
$prof = $PROFILE; $pdir = Split-Path $prof
if (-not (Test-Path $pdir)) { New-Item -ItemType Directory -Force $pdir | Out-Null }
$m0 = '# >>> ironclad commands >>>'; $m1 = '# <<< ironclad commands <<<'
$block = "$m0`nfunction global:ironclad        { & `"$ScriptDir\ironclad.ps1`"        @args }`nfunction global:ironclad-doctor { & `"$ScriptDir\ironclad-doctor.ps1`" @args }`n$m1"
$cur = if (Test-Path $prof) { Get-Content $prof -Raw } else { '' }
if ($cur -match [regex]::Escape($m0)) {
  $cur = [regex]::Replace($cur, "(?s)$([regex]::Escape($m0)).*?$([regex]::Escape($m1))", $block)
} else { $cur = (($cur.TrimEnd() + "`n`n" + $block + "`n")).TrimStart("`n") }
Set-Content -Path $prof -Value $cur -Encoding utf8
Say "wired 'ironclad' + 'ironclad-doctor' into $prof"

Write-Host ""
Write-Host "[install] done. Desktop install in $Root."
Write-Host "[install] activate in THIS shell:  . `$PROFILE"
Write-Host "[install] then, from your project:  ironclad   ·   ironclad-doctor"
Write-Host "[install] endpoint: $BaseUrl  (override: re-run with -BaseUrl / -Model, or set GX10_BASE_URL)"
