# Backup & restore (memory tiers + engine state) — #1062

The accumulated memory and engine state are the crown jewels of a self-learning Ironclad deployment — losing
them is **permanent**. `scripts/backup.sh` captures everything worth keeping; `scripts/restore.sh` puts it
back.

## What is backed up

| Source | Where it lives | Archive |
|--------|----------------|---------|
| Vector memory | `mem-qdrant` volume (`/qdrant/storage`) | `qdrant.tgz` |
| Graph memory | `mem-neo4j` volume (`/data`) | `neo4j.tgz` |
| Warm session/cache | `mem-valkey` volume (`/data`) | `valkey.tgz` |
| Engine state · vault artifacts · ACE playbooks · **audit ledger** | `./ironclad-workdir` bind mount | `workdir.tgz` |

Model weights (`hf-cache`) and container logs are **not** backed up — they are re-downloadable / disposable.
A tier whose container is not running is skipped (its data is not deployed here).

## Back up

```bash
bash scripts/backup.sh                       # → ./ironclad-backups/<UTC-stamp>/
IRONCLAD_BACKUP_DIR=/mnt/nas/ic bash scripts/backup.sh    # off-box target (recommended)
IRONCLAD_BACKUP_KEEP=14 bash scripts/backup.sh            # keep the newest 14 (default 7)
```

Each run writes a timestamped directory containing the archives + a `MANIFEST`. Retention (`keep newest N`)
is applied by `scripts/backup_retention.py` (unit-tested).

## Schedule it

Until the in-product scheduler ([#1064]) lands, drive it with cron / a systemd timer — e.g. daily at 03:00:

```cron
0 3 * * *  cd /path/to/ironclad && bash scripts/backup.sh >> ./ironclad-backups/backup.log 2>&1
```

## Restore (DESTRUCTIVE)

Restoring **replaces** the live data. Stop the stack first, restore, then bring it back up:

```bash
docker compose --profile memory down                 # stop the tiers (volumes are kept)
bash scripts/restore.sh ./ironclad-backups/<UTC-stamp> --yes
docker compose --profile model --profile memory up -d
```

`restore.sh` requires `--yes` to proceed and only touches tiers whose archive is present in the backup.

## Verify a backup

```bash
tar tzf ./ironclad-backups/<UTC-stamp>/qdrant.tgz | head   # list contents without extracting
cat ./ironclad-backups/<UTC-stamp>/MANIFEST
```
