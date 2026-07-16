# Operate-phase scheduler

The orchestrator's `execute_command` is single-shot (and its deny-list refuses `schtasks` / `start-job`), so
there is no *in-product* way to run periodic operate-phase jobs — backups, run-artifact pruning, drift
checks. `scripts/scheduler.py` is the minimal, testable primitive that fills the gap.

## How it works

- **Jobs config** (`scripts/scheduler.jobs.json`) — a list of `{name, command, interval_s}`. Copy
  `scripts/scheduler.jobs.example.json` and adjust.
- **Last-run state** — one JSON file (`--state`, default under the workdir) records each job's last run.
- **`--run-due`** — runs every job whose `interval_s` has elapsed since its last run, then stamps the time.
  A failed job is retried only after its interval (never in a hot loop).

Drive it with **one** host cron entry / systemd timer that fires every minute — it fans out to all configured
jobs, so you never hand-maintain a cron line per job:

```cron
* * * * *  cd /path/to/ironclad && python3 scripts/scheduler.py --run-due >> ./scheduler.log 2>&1
```

## Inspect

```bash
python3 scripts/scheduler.py                 # list the schedule + when each job is next due
python3 scripts/scheduler.py --run-due       # run whatever is due now
```

## Default jobs (example)

| Job | Command | Interval |
|-----|---------|----------|
| `backup` | `bash scripts/backup.sh` | daily (86400s) |
| `prune-runs` | `python3 scripts/prune_runs.py ./ironclad-workdir --keep-days 30 --apply` | weekly (604800s) |

The due logic and last-run state are unit-tested; the actual job commands are deploy configuration.
