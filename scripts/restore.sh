#!/usr/bin/env bash
# #1062: restore the memory tiers + engine state from a backup dir made by scripts/backup.sh.
#
# DESTRUCTIVE — this REPLACES the current memory + engine state with the backup's. Bring the stack DOWN
# first (or at least the memory profile), restore, then bring it back up. Requires --yes to proceed.
#
#   docker compose --profile memory down            # stop the tiers (keep the volumes)
#   bash scripts/restore.sh ./ironclad-backups/<UTC-stamp> --yes
#   docker compose --profile model --profile memory up -d
#
# See docs/backup-restore.md.
set -euo pipefail

SRC="${1:-}"
CONFIRM="${2:-}"
WORKDIR="${IRONCLAD_WORKDIR:-./ironclad-workdir}"
[ -n "$SRC" ] || { echo "usage: restore.sh <backup-dir> --yes"; exit 2; }
[ -d "$SRC" ] || { echo "not a directory: $SRC"; exit 2; }
SRC_ABS="$(cd "$SRC" && pwd)"
if [ "$CONFIRM" != "--yes" ]; then
  echo "This will REPLACE the live memory + engine state from $SRC."
  echo "Stop the stack first, then re-run with --yes to proceed."
  exit 1
fi

echo "restore ← $SRC"

_restore_volume() {   # $1=container-or-volume  $2=path-in-container  $3=archive-name
  local arc="$SRC_ABS/$3.tgz"
  [ -f "$arc" ] || { echo "  skip: $3.tgz not in backup"; return 0; }
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; then
    docker run --rm --volumes-from "$1" -v "$SRC_ABS:/backup" alpine sh -c \
      "rm -rf ${2:?}/* ${2}/..?* ${2}/.[!.]* 2>/dev/null; tar xzf /backup/$3.tgz -C $2" \
      && echo "  restored $3.tgz → $1:$2" || echo "  WARN: could not restore $1"
  else
    echo "  WARN: container $1 not found — create the stack (docker compose ... up --no-start) first"
  fi
}

_restore_volume mem-qdrant /qdrant/storage qdrant
_restore_volume mem-neo4j  /data           neo4j
_restore_volume mem-valkey /data           valkey

if [ -f "$SRC_ABS/workdir.tgz" ]; then
  mkdir -p "$WORKDIR"
  tar xzf "$SRC_ABS/workdir.tgz" -C "$WORKDIR" && echo "  restored workdir.tgz → $WORKDIR"
fi

echo "restore complete — bring the stack back up."
