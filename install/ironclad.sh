#!/usr/bin/env bash
# Ironclad launcher (Linux / macOS). Wired as the `ironclad` command by ironclad-install.sh.
# Reads <project>/.ironclad/config.json, ensures the local engine is up (version-aware), runs the client.
set -euo pipefail

say() { printf '[ironclad] %s\n' "$*"; }
PROJ="$(pwd)"
CFG="$PROJ/.ironclad/config.json"
[ -f "$CFG" ] || { say "no .ironclad in '$PROJ' — run install/ironclad-install.sh in this project first."; exit 2; }

# read the (install-written) config — unit-separator (\x1f) so empty fields (e.g. clientCli) and paths
# with spaces both survive (a Tab is IFS-whitespace → read would collapse empty fields and shift values).
IFS=$'\x1f' read -r ROOT VENV ENGINE_DIR CLIENT_CLI BASE_URL MEMORY_URL MODEL PORT LANGUAGE < <(python3 - "$CFG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
keys = [("root",""),("venv",""),("engineDir",""),("clientCli",""),("baseUrl",""),
        ("memoryUrl",""),("model",""),("port","8100"),("language","en")]
print("\x1f".join(str(c.get(k, d)) for k, d in keys))
PY
)
BASE="http://127.0.0.1:${PORT}"
# venv interpreter: bin/python (POSIX) or Scripts/python.exe (venv made under Git-Bash on Windows)
PY=""; for p in "$VENV/bin/python" "$VENV/Scripts/python.exe"; do [ -x "$p" ] && { PY="$p"; break; }; done
[ -n "$PY" ] || { say "venv python missing under $VENV — re-run install/ironclad-install.sh."; exit 2; }

# health probe + running-version read (urllib → no curl dependency)
probe()   { "$PY" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=3); print("ok")
except Exception: pass
PY
}
version() { "$PY" - "$1" <<'PY' 2>/dev/null
import sys, json, urllib.request
try:
    print(json.load(urllib.request.urlopen(sys.argv[1], timeout=3)).get("orchestrator_version",""))
except Exception: pass
PY
}

STAMP="unknown"; [ -f "$ENGINE_DIR/VERSION" ] && STAMP="$(tr -d '[:space:]' < "$ENGINE_DIR/VERSION")"
STARTED_PID=""
REUSE=0
if [ -n "$(probe "$BASE/health")" ]; then
  RV="$(version "$BASE/health")"
  if [ "$RV" = "$STAMP" ]; then
    say "engine already running on $BASE (version $STAMP) — reusing."
    REUSE=1
  else
    # #47: a stale engine keeps serving the old code; stop it (by listening port) and start fresh.
    say "engine on $BASE is version '$RV', installed is '$STAMP' — restarting."
    if command -v fuser >/dev/null 2>&1; then fuser -k "${PORT}/tcp" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then kill $(lsof -t -i ":${PORT}" 2>/dev/null) 2>/dev/null || true; fi
    sleep 1
  fi
fi

if [ "$REUSE" -eq 0 ]; then
  say "starting the engine ($BASE, version $STAMP) ..."
  GX10_SETUP_TYPE=local GX10_BASE_URL="$BASE_URL" GX10_MEMORY_URL="$MEMORY_URL" GX10_MODEL="$MODEL" \
  GX10_WORKDIR="$PROJ" GX10_PLUGINS_DIR="$ROOT/skills" GX10_MPR=1 GX10_LANGUAGE="$LANGUAGE" \
  GX10_ORCHESTRATOR_VERSION="$STAMP" \
    nohup "$PY" "$ENGINE_DIR/server.py" --host 127.0.0.1 --port "$PORT" >"$PROJ/.ironclad/engine.log" 2>&1 &
  STARTED_PID=$!
  for _ in $(seq 1 30); do [ -n "$(probe "$BASE/health")" ] && break; sleep 0.7; done
  [ -n "$(probe "$BASE/health")" ] || { say "ERROR: engine did not become healthy — see $PROJ/.ironclad/engine.log"; [ -n "$STARTED_PID" ] && kill "$STARTED_PID" 2>/dev/null; exit 1; }
fi

cleanup() { [ -n "$STARTED_PID" ] && { say "stopping the engine (pid $STARTED_PID)."; kill "$STARTED_PID" 2>/dev/null || true; }; }
trap cleanup EXIT

if [ -n "$CLIENT_CLI" ] && command -v node >/dev/null 2>&1; then
  node "$CLIENT_CLI" --server "$BASE" --codedir "$PROJ"
else
  GX10_SERVER_URL="$BASE" "$PY" "$ENGINE_DIR/client.py" --codedir "$PROJ"
fi
