#!/usr/bin/env bash
# #1062: back up the crown-jewel memory tiers + engine state to a timestamped archive.
#
# The accumulated memory (vectors, graph, warm session) + the engine state (vault artifacts, the ACE
# playbooks, the audit ledger) are IRREPLACEABLE for a self-learning deployment — data loss is permanent.
# This dumps each running memory-tier volume (via a throwaway helper container so it works whether or not
# the DB is quiesced) plus the ./ironclad-workdir bind mount, then applies retention.
#
#   bash scripts/backup.sh                 # back up to ./ironclad-backups/<UTC-stamp>/
#   IRONCLAD_BACKUP_DIR=/mnt/nas/ic bash scripts/backup.sh
#   IRONCLAD_BACKUP_KEEP=14 bash scripts/backup.sh
#
# Schedule it (until the in-product scheduler #1064) with cron, e.g. daily at 03:00:
#   0 3 * * *  cd /path/to/ironclad && bash scripts/backup.sh >> ./ironclad-backups/backup.log 2>&1
#
# Restore with scripts/restore.sh <backup-dir>. See docs/backup-restore.md.
set -euo pipefail

BACKUP_DIR="${IRONCLAD_BACKUP_DIR:-./ironclad-backups}"
KEEP="${IRONCLAD_BACKUP_KEEP:-7}"
WORKDIR="${IRONCLAD_WORKDIR:-./ironclad-workdir}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$BACKUP_DIR/$STAMP"
mkdir -p "$DEST"
DEST_ABS="$(cd "$DEST" && pwd)"

echo "backup → $DEST"

_dump_volume() {   # $1=container  $2=path-in-container  $3=out-name
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$1"; then
    if docker run --rm --volumes-from "$1" -v "$DEST_ABS:/backup" alpine \
         tar czf "/backup/$3.tgz" -C "$2" . 2>/dev/null; then
      echo "  backed up $1:$2 → $3.tgz"
    else
      echo "  WARN: could not back up $1 (is docker available?)"
    fi
  else
    echo "  skip: container $1 not running (its tier is not deployed here)"
  fi
}

# The three memory tiers (their container names are the public compose defaults).
_dump_volume mem-qdrant /qdrant/storage qdrant
_dump_volume mem-neo4j  /data           neo4j
_dump_volume mem-valkey /data           valkey

# Engine state + vault artifacts + ACE playbooks + the audit ledger (a host bind mount → tar directly).
if [ -d "$WORKDIR" ]; then
  tar czf "$DEST/workdir.tgz" -C "$WORKDIR" . && echo "  backed up $WORKDIR → workdir.tgz"
else
  echo "  skip: workdir $WORKDIR not found"
fi

printf 'ironclad-backup %s\n' "$STAMP" > "$DEST/MANIFEST"
echo "  wrote MANIFEST"

# Retention: keep the newest $KEEP backups (the one piece with real logic → unit-tested).
python3 "$HERE/backup_retention.py" "$BACKUP_DIR" --keep "$KEEP" --apply || true

echo "backup complete → $DEST"
