#!/usr/bin/env bash
# Ironclad doctor (Linux / macOS) — read-only status of the desktop install + its endpoints.
set -euo pipefail

say() { printf '[doctor] %s\n' "$*"; }
PROJ="$(pwd)"
CFG="$PROJ/.ironclad/config.json"
[ -f "$CFG" ] || { say "no .ironclad in '$PROJ' — run install/ironclad-install.sh in this project first."; exit 2; }

# unit-separator (\x1f) so empty fields + spaced paths survive (Tab is IFS-whitespace → read collapses them)
IFS=$'\x1f' read -r ROOT VENV ENGINE_DIR CLIENT_CLI BASE_URL MEMORY_URL MODEL PORT LANGUAGE < <(python3 - "$CFG" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
keys = [("root",""),("venv",""),("engineDir",""),("clientCli",""),("baseUrl",""),
        ("memoryUrl",""),("model",""),("port","8100"),("language","en")]
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

STAMP="unknown"; [ -f "$ENGINE_DIR/VERSION" ] && STAMP="$(tr -d '[:space:]' < "$ENGINE_DIR/VERSION")"
say "type=desktop  local engine version=$STAMP  model=$MODEL  language=$LANGUAGE"
say "engine   (http://127.0.0.1:$PORT): $(reach "http://127.0.0.1:$PORT/health")"
[ -n "$BASE_URL" ]   && say "model    ($BASE_URL): $(reach "$BASE_URL/models")"
[ -n "$MEMORY_URL" ] && say "memory   ($MEMORY_URL): $(reach "$MEMORY_URL")"
