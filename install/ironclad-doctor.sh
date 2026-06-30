#!/usr/bin/env bash
# Ironclad doctor (Linux / macOS) — read-only status of the desktop install + its endpoints.
set -euo pipefail

say() { printf '[doctor] %s\n' "$*"; }
PROJ="$(pwd)"
CFG="$PROJ/.ironclad/config.json"
[ -f "$CFG" ] || { say "no .ironclad in '$PROJ' — run install/ironclad-install.sh in this project first."; exit 2; }

# unit-separator (\x1f) so empty fields + spaced paths survive (Tab is IFS-whitespace → read collapses them)
IFS=$'\x1f' read -r ROOT VENV ENGINE_DIR CLIENT_CLI BASE_URL MEMORY_URL MODEL PORT LANGUAGE TYPE SERVER_URL < <(python3 - "$CFG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
keys = [("root",""),("venv",""),("engineDir",""),("clientCli",""),("baseUrl",""),
        ("memoryUrl",""),("model",""),("port","8100"),("language","en"),("type","desktop"),("serverUrl","")]
print("\x1f".join(str(c.get(k, d)) for k, d in keys))
PY
)
PY=""; for p in "$VENV/bin/python" "$VENV/Scripts/python.exe"; do [ -x "$p" ] && { PY="$p"; break; }; done
[ -n "$PY" ] || PY="python3"

reach() { "$PY" - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=5); print("reachable")
except urllib.error.HTTPError: print("reachable")          # 4xx still means the service answered
except Exception: print("NOT reachable")
PY
}

# Read one field from the running engine's /health JSON. #385 reports the Cold (memory) and Warm (Valkey)
# tiers SEPARATELY so a silent warm outage cannot hide behind `memory: up`; surface both here (up/down/off).
hfield() { "$PY" - "$1" "$2" <<'PY' 2>/dev/null
import sys, json, urllib.request
try:
    print(json.load(urllib.request.urlopen(sys.argv[1], timeout=5)).get(sys.argv[2], "?"))
except Exception: print("?")
PY
}

# The /health `registry` block surfaces the #601 project-isolation binding (status / active project / home),
# otherwise invisible after boot — flagging `unisolated` when the registry fell back to the un-isolated mode.
hreg() { "$PY" - "$1" <<'PY' 2>/dev/null
import sys, json, urllib.request
try:
    r = json.load(urllib.request.urlopen(sys.argv[1], timeout=5)).get("registry") or {}
    print(f"status={r.get('status','?')}  active_project={r.get('active_project')}  home={r.get('home')}")
except Exception: print("?")
PY
}

if [ "$TYPE" = "spark" ]; then
  SERVER="${GX10_SERVER_URL:-$SERVER_URL}"
  say "type=spark (thin client, no local engine)."
  if [ -n "$SERVER" ]; then say "orchestrator ($SERVER): $(reach "$SERVER/health")"
  else say "no serverUrl in config — re-install."; fi
  exit 0
fi

STAMP="unknown"; [ -f "$ENGINE_DIR/VERSION" ] && STAMP="$(tr -d '[:space:]' < "$ENGINE_DIR/VERSION")"
say "type=desktop  local engine version=$STAMP  model=$MODEL  language=$LANGUAGE"
HEALTH="http://127.0.0.1:$PORT/health"
say "engine   ($HEALTH): $(reach "$HEALTH")"
say "memory tier (/health.memory): $(hfield "$HEALTH" memory)   |   warm tier (/health.warm): $(hfield "$HEALTH" warm)"
say "registry (/health.registry): $(hreg "$HEALTH")"
[ -n "$BASE_URL" ]   && say "model    ($BASE_URL): $(reach "$BASE_URL/models")"
[ -n "$MEMORY_URL" ] && say "memory   ($MEMORY_URL): $(reach "$MEMORY_URL")"
