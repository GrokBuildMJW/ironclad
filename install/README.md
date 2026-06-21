# Ironclad one-shot installer

Cross-platform, **secret-free** scripts that turn a fresh clone into a working desktop install in a single
command: a Python venv with the engine, the optional TypeScript client, a per-project config, and an
`ironclad` shell command. All endpoints default to **localhost** and are overridable — nothing about your
deployment is baked into the repo.

## Install (run once, from the project folder you want to drive)

**Linux / macOS**
```bash
bash install/ironclad-install.sh                       # localhost defaults
bash install/ironclad-install.sh --base-url http://HOST:8000/v1 --model MODEL
source ~/.bashrc   # or ~/.zshrc — activate the `ironclad` command in this shell
```

**Windows (PowerShell)**
```powershell
install\ironclad-install.ps1                            # localhost defaults
install\ironclad-install.ps1 -BaseUrl http://HOST:8000/v1 -Model MODEL
. $PROFILE   # activate the `ironclad` command in this shell
```

## Use

```
ironclad           # ensure the local engine is up, then open the client (current folder = codedir)
ironclad-doctor    # read-only status: engine version + endpoint reachability
```

## What each script does

| Script | Role |
|---|---|
| `ironclad-install.{sh,ps1}` | One-shot: prereq check → venv + `pip install -e .[engine]` → build the ink client (if Node present) → write `<project>/.ironclad/config.json` → wire the `ironclad` command into your shell profile. |
| `ironclad.{sh,ps1}` | Launcher (`ironclad`): ensure the engine is healthy (version-aware restart), then run the client against `http://127.0.0.1:<port>`. |
| `ironclad-doctor.{sh,ps1}` | Read-only status of the install and its endpoints. |

## Configuration

Defaults assume an OpenAI-compatible model endpoint at `http://127.0.0.1:8000/v1`. Override per install via
flags or environment variables — never by editing the scripts:

| Flag (`.sh` / `.ps1`) | Env | Default |
|---|---|---|
| `--base-url` / `-BaseUrl` | `GX10_BASE_URL` | `http://127.0.0.1:8000/v1` |
| `--memory-url` / `-MemoryUrl` | `GX10_MEMORY_URL` | *(empty → memory off)* |
| `--model` / `-Model` | `GX10_MODEL` | `qwen3.6-35b` |
| `--port` / `-Port` | `GX10_PORT` | `8100` |
| `--language` / `-Language` | `GX10_LANGUAGE` | `en` |
| `--connection` / `-ConnectionFile` | `GX10_CONNECTION_FILE` | *(none)* — optional JSON `{ "connection": { "base_url", "model" } }` |

To stand up a model endpoint first, see [`../SETUP.md`](../SETUP.md) (Track B) and
[`../scripts/spark-bootstrap.sh`](../scripts/spark-bootstrap.sh) for a DGX Spark.
