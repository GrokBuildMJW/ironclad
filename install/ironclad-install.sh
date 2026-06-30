#!/usr/bin/env bash
# Ironclad one-shot desktop installer (Linux / macOS). SECRET-FREE.
#
# Run it once from a clone of the Ironclad repo, inside the project folder you want to drive:
#
#   bash install/ironclad-install.sh            # localhost endpoint defaults
#   bash install/ironclad-install.sh --base-url http://my-host:8000/v1 --model my-model
#   bash install/ironclad-install.sh --warm-url redis://host:6379   # bind the Valkey/Redis warm tier
#
# It builds a venv, installs the engine (incl. the warm-cache client so the warm tier works whenever
# GX10_WARM_URL / --warm-url is set), builds the optional TypeScript client, writes a project config
# (.ironclad/config.json) and wires an `ironclad` shell command. Defaults point at localhost; override
# any endpoint with a flag or a GX10_* env var. No host/IP/path is ever baked into the repo.
set -euo pipefail

say() { printf '[install] %s\n' "$*"; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

# --- locate the repo root: this script lives in <root>/install/ ----------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# layout-agnostic component discovery (flat OSS export OR core/ inside the private monorepo)
find_dir()  { for c in "$@"; do [ -d "$c" ] && { printf '%s' "$c"; return 0; }; done; return 1; }
find_file() { for c in "$@"; do [ -f "$c" ] && { printf '%s' "$c"; return 0; }; done; return 1; }
# venv interpreter: POSIX is bin/python; a venv created under Git-Bash on Windows uses Scripts/python.exe
venv_py()   { for p in "$1/bin/python" "$1/Scripts/python.exe"; do [ -x "$p" ] && { printf '%s' "$p"; return 0; }; done; return 1; }
ENGINE_DIR="$(find_dir "$ROOT/engine" "$ROOT/core/engine")" || die "engine/ not found under $ROOT"
PYPROJECT="$(find_file "$ROOT/pyproject.toml" "$ROOT/core/pyproject.toml")" || die "pyproject.toml not found under $ROOT"
PKG_ROOT="$(dirname "$PYPROJECT")"
INK_DIR="$(find_dir "$ROOT/clients/ink" "$ROOT/../clients/ink" "$PKG_ROOT/clients/ink" || true)"

# --- defaults (localhost; override via flags / GX10_* env) ---------------------------------------
BASE_URL="${GX10_BASE_URL:-http://127.0.0.1:8000/v1}"
MEMORY_URL="${GX10_MEMORY_URL:-}"          # empty → memory off (fail-soft)
WARM_URL="${GX10_WARM_URL:-}"              # Valkey/Redis warm cache; empty → warm off (fail-soft)
MODEL="${GX10_MODEL:-qwen3.6-35b}"
PORT="${GX10_PORT:-8100}"
LANGUAGE="${GX10_LANGUAGE:-en}"
PROJECT="$(pwd)"
CONNECTION="${GX10_CONNECTION_FILE:-}"     # optional private overlay (base_url/model); never in the export

# --- flags --------------------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --base-url)   BASE_URL="$2"; shift 2;;
    --memory-url) MEMORY_URL="$2"; shift 2;;
    --warm-url)   WARM_URL="$2"; shift 2;;
    --model)      MODEL="$2"; shift 2;;
    --port)       PORT="$2"; shift 2;;
    --language)   LANGUAGE="$2"; shift 2;;
    --project)    PROJECT="$(cd "$2" && pwd)"; shift 2;;
    --connection) CONNECTION="$2"; shift 2;;
    -h|--help)
      sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "unknown option: $1 (see --help)";;
  esac
done

# optional private overlay: pull endpoint defaults from a connection.json if one was passed and the
# value wasn't overridden on the CLI/env. Keeps the operator's host out of the repo.
if [ -n "$CONNECTION" ] && [ -f "$CONNECTION" ]; then
  say "reading endpoint defaults from $CONNECTION"
  _ov="$(python3 - "$CONNECTION" <<'PY'
import json, sys
try:
    c = json.load(open(sys.argv[1], encoding="utf-8")).get("connection", {})
    print((c.get("base_url") or "") + "\t" + (c.get("model") or ""))
except Exception:
    print("\t")
PY
)"
  _ob="${_ov%%$'\t'*}"; _om="${_ov##*$'\t'}"
  [ "${GX10_BASE_URL:-}" = "" ] && [ -n "$_ob" ] && BASE_URL="$_ob"
  [ "${GX10_MODEL:-}" = "" ] && [ -n "$_om" ] && MODEL="$_om"
fi

# --- prerequisites ------------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 not found (need >= 3.10)"
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' \
  || die "Python >= 3.10 required (have $(python3 -V 2>&1))"

say "root=$ROOT  project=$PROJECT  model=$MODEL  base_url=$BASE_URL"

# --- venv + engine ------------------------------------------------------------------------------
VENV="$ROOT/.venv"
venv_py "$VENV" >/dev/null 2>&1 || { say "creating venv ($VENV) ..."; python3 -m venv "$VENV"; }
VENV_PY="$(venv_py "$VENV")" || die "venv python not found after creation"
say "installing the engine (pip install -e .[engine,memory]) ..."
"$VENV_PY" -m pip install --quiet --upgrade pip
# Install the warm-cache client (the `memory` extra → redis>=5) alongside the engine so the warm tier
# works whenever GX10_WARM_URL is set (via env or --warm-url), matching the Docker image; warm stays
# OFF at runtime until a URL is configured. ".[extra]" from the pkg dir — pip rejects "/abs/path[extra]".
( cd "$PKG_ROOT" && "$VENV_PY" -m pip install --quiet -e ".[engine,memory]" )
"$VENV_PY" -c 'import ack, pydantic' || die "engine import check failed"

# --- optional TypeScript client -----------------------------------------------------------------
CLIENT_CLI=""
if [ -n "${INK_DIR:-}" ] && command -v node >/dev/null 2>&1; then
  # The ink client needs Node >= 22 (clients/ink package.json engines); npm does NOT enforce `engines` by
  # default, so gate it here and skip with a clear message on older Node rather than emit a cryptic build error.
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "${NODE_MAJOR:-0}" -ge 22 ]; then
    say "building the ink client ..."
    ( cd "$INK_DIR" && npm install --silent && npm run build --silent )
    CLIENT_CLI="$INK_DIR/dist/cli.js"
  else
    say "skipping ink client — Node >= 22 required (have $(node -v 2>/dev/null)); the legacy Python client still works."
  fi
else
  say "skipping ink client (no Node or clients/ink absent) — the legacy Python client still works."
fi

# --- project config -----------------------------------------------------------------------------
mkdir -p "$PROJECT/.ironclad"
cat > "$PROJECT/.ironclad/config.json" <<JSON
{
  "type": "desktop",
  "root": "$ROOT",
  "venv": "$VENV",
  "engineDir": "$ENGINE_DIR",
  "clientCli": "$CLIENT_CLI",
  "baseUrl": "$BASE_URL",
  "memoryUrl": "$MEMORY_URL",
  "warmUrl": "$WARM_URL",
  "model": "$MODEL",
  "port": $PORT,
  "language": "$LANGUAGE"
}
JSON
printf '*\n' > "$PROJECT/.ironclad/.gitignore"
say "bound project: $PROJECT/.ironclad/config.json"

# --- wire the `ironclad` shell command ----------------------------------------------------------
LAUNCHER="$SCRIPT_DIR/ironclad.sh"
DOCTOR="$SCRIPT_DIR/ironclad-doctor.sh"
RC="$HOME/.zshrc"; [ -n "${ZSH_VERSION:-}" ] || case "${SHELL:-}" in */zsh) RC="$HOME/.zshrc";; *) RC="$HOME/.bashrc";; esac
M0='# >>> ironclad commands >>>'; M1='# <<< ironclad commands <<<'
BLOCK="$M0
ironclad()        { ( cd \"\$PWD\" && bash \"$LAUNCHER\" \"\$@\" ); }
ironclad-doctor() { ( cd \"\$PWD\" && bash \"$DOCTOR\" \"\$@\" ); }
$M1"
touch "$RC"
if grep -qF "$M0" "$RC" 2>/dev/null; then
  python3 - "$RC" "$M0" "$M1" "$BLOCK" <<'PY'
import re, sys
rc, m0, m1, block = sys.argv[1:5]
t = open(rc, encoding="utf-8").read()
t = re.sub(re.escape(m0) + ".*?" + re.escape(m1), block, t, flags=re.DOTALL)
open(rc, "w", encoding="utf-8").write(t)
PY
else
  printf '\n%s\n' "$BLOCK" >> "$RC"
fi
say "wired 'ironclad' + 'ironclad-doctor' into $RC"

cat <<DONE

[install] done. Desktop install in $ROOT.
[install] activate in THIS shell:  source "$RC"
[install] then, from your project:  ironclad        ·  ironclad-doctor
[install] endpoint: $BASE_URL  (override: re-run with --base-url / --model, or set GX10_BASE_URL)
DONE
