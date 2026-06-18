# Setup

> **Status: pre-release / in active development.** No tagged release yet; expect
> change. Ironclad is developed and tested on an **NVIDIA DGX Spark** (GB10,
> Blackwell `sm_121`, 128 GB unified memory) with a local **vLLM** server running
> **Qwen3.6-35B-A3B-NVFP4**. Any OpenAI-compatible endpoint works; the defaults just
> match that reference box.

Prefer to have an AI coding agent do this for you? See **[`AGENTS.md`](AGENTS.md)**.

---

## Quick start (copy-paste)

Have **Python 3.10+** and a running **model endpoint**? Install in three commands:

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git && cd ironclad
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"
```

Run it:

```bash
# defaults assume http://localhost:8000/v1 and model "qwen3.6-35b" — override if needed:
export GX10_BASE_URL=http://localhost:8000/v1 GX10_MODEL=your-model   # PowerShell: $env:GX10_BASE_URL="..."
python engine/server.py &              # the orchestrator
python engine/client.py --codedir .    # the client (drives it; code-tools run on your files)
```

- **Recommended interactive client** (purpose-built renderer: ghost-free resize, native
  scrollback/selection/copy, smooth streaming): the TypeScript client in
  [`clients/ink/`](clients/ink/) — see [§4 Run](#4-run--orchestrator--client). The
  `engine/client.py` above is the zero-Node quick path.
- **One-command launcher** (so you can just type `ironclad`): see
  [§8 Shell shortcuts](#8-shell-shortcuts-windows--macos--linux).
- **No endpoint yet?** [`docs/dgx-spark.md`](docs/dgx-spark.md) brings vLLM up in one shot.

The sections below explain each step in detail.

---

## 1. Prerequisites

- **Python 3.10+** — the orchestrator + engine.
- **Node.js ≥ 22** — for the **recommended** terminal client (`clients/ink/`). You only
  need it for that client; skip it if you'll use the legacy Python clients instead.
- An **OpenAI-compatible chat endpoint** (vLLM, llama.cpp server, vLLM-OpenAI, etc.)
  reachable over HTTP. The endpoint must support tool/function calling for the
  orchestration engine; the ACK library alone only needs the endpoint for emission.
- Optional: **`prompt_toolkit`** for the legacy full-screen Python TUI (installed by the
  `[engine]` extra). Without it the legacy Python client falls back to a plain line REPL.

## 2. Install

```bash
git clone https://github.com/GrokBuildMJW/ironclad.git
cd ironclad
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[engine]"           # ACK (pydantic) + engine extras (openai + TUI: prompt_toolkit, textual, rich)
```

`pip install -e .` (without the extra) installs just the **ACK library** (`import ack`)
— pydantic-only, for embedding the contract kernel in your own app.

> Once published to PyPI the distribution name is **`ironclad-ai`**
> (`pip install ironclad-ai`); the import package stays `ack`. (`ironclad` was already
> taken on PyPI by an unrelated project.)

## 3. Configure the model endpoint

Configuration precedence (low → high): **code defaults → config file/dir → `GX10_*`
env vars → CLI flags**. The simplest path is env vars:

| Variable          | Meaning                                  | Default                     |
|-------------------|------------------------------------------|-----------------------------|
| `GX10_BASE_URL`   | OpenAI-compatible base URL               | `http://localhost:8000/v1`  |
| `GX10_MODEL`      | served model name                        | `qwen3.6-35b`               |
| `GX10_API_KEY`    | API key (only if your endpoint needs one)| `not-needed`                |
| `GX10_WORKDIR`    | workspace (tasks/, summaries/, session)  | `.`                         |
| `GX10_MAX_TOKENS` | output token cap                         | `8192`                      |
| `GX10_THINKING`   | `auto` \| `first` \| `off` \| `all`      | `auto`                      |
| `GX10_LANGUAGE`   | reply language (`en`,`de`,`fr`,…)        | `en`                        |

Or drop a `gx10.config.json` next to where you run it:

```json
{
  "connection": { "base_url": "http://localhost:8000/v1", "model": "your-model",
                  "api_key_env": "GX10_API_KEY" },
  "generation": { "max_tokens": 8192, "thinking_mode": "auto" },
  "paths":      { "workdir": "./my-workspace" }
}
```

## 4. Run — orchestrator + client

The **orchestrator** (reasoning + state) runs next to the model; the **client** runs where
your code lives and drives it. Plain client-initiated HTTP. Code stays on your machine: the
orchestrator's file/command tools are **passed through to the client** and run on your
local files (and code-agents launch locally).

```bash
# 1) the orchestrator (next to the model; localhost is fine on a single machine):
python engine/server.py --host 0.0.0.0 --port 8100

# 2) on your machine — the RECOMMENDED terminal client (TypeScript). Install it ONCE as a
#    global command, then run `ironclad` from any folder (that folder becomes the codedir):
( cd clients/ink && npm install && npm install -g . )      # global `ironclad`, like claude / kimi
ironclad --server http://<server-host>:8100               # or set the URL once (config file, below)

# ...or a LEGACY Python client (no Node needed) — full-screen TUI or plain line REPL:
GX10_SERVER_URL=http://<server-host>:8100 python engine/tui.py    --codedir .   # legacy
GX10_SERVER_URL=http://<server-host>:8100 python engine/client.py --codedir .   # legacy
```

The recommended client is the TypeScript terminal client in **[`clients/ink/`](clients/ink/)**
(ghost-free resize, native scrollback/selection/copy, smooth streaming). Install it once
globally (`npm install -g .` in `clients/ink/`) so `ironclad` is on your PATH; it talks the
same HTTP contract as the Python clients, so
the server is identical. The Python `tui.py`/`client.py` stay as legacy fallbacks.

`engine/gx10.py` is now the **engine library** (imported by the server), not a CLI.

Server endpoints: `GET /health /tasks /pending /doctor` · `POST /chat /chat/stream
/tool-result /feedback /fanout /cancel /session/*`. The client pulls staged handovers from
`/pending`, runs code-agents locally, and posts results to `/feedback`. `--max-agents`
bounds how many run in parallel.

## 5. Reference vLLM launch (DGX Spark)

> **One-shot:** on a DGX Spark, instead of the manual command below, run
> `bash scripts/spark-bootstrap.sh --model-dir <weights> --served-name qwen3.6-35b
> --with-orchestrator` (idempotent, waits for readiness). Full reference stack and
> rationale: [`docs/dgx-spark.md`](docs/dgx-spark.md).

The endpoint Ironclad is developed against — a single NVFP4 MoE on one GB10. Adjust
paths/flags to your box; nothing here is required by Ironclad:

```bash
docker run -d --name vllm --restart unless-stopped --gpus all --ipc host \
  -v ~/models:/models -p 8000:8000 \
  vllm/vllm-openai:cu130-nightly \
  /models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name qwen3.6-35b \
  --quantization compressed-tensors --kv-cache-dtype fp8 \
  --attention-backend flashinfer --moe-backend flashinfer_cutlass \
  --tensor-parallel-size 1 --gpu-memory-utilization 0.6 \
  --max-model-len 32768 --max-num-seqs 8 \
  --enable-chunked-prefill --enable-prefix-caching \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

Notes from this hardware: GB10 is decode-bandwidth-limited, so an **MoE in NVFP4**
beats a dense model; structured/constrained decoding (XGrammar) works on the **CUDA 13
nightly** image but crashed on older ones; per-request **thinking-off** is what makes
structured emission reliable (the ACK emitter sets it; normal chat keeps thinking on).

## 6. Verify

```bash
pip install pytest
python -m pytest ack/tests -q          # contract kernel + engine + split tests

# Smoke the endpoint wiring (expects your model to answer):
python engine/server.py --port 8100 &
GX10_SERVER_URL=http://localhost:8100 python engine/client.py --codedir .
# then type:  was ist 17 mal 23?
```

## 7. Troubleshooting

- **`prompt_toolkit` missing / "NoConsoleScreenBufferError".** Install the `[engine]`
  extra, and run the TUI in a real terminal (PowerShell / Windows Terminal), not a
  bare pipe. The client auto-falls back to the line REPL if prompt_toolkit is absent.
- **`UnicodeEncodeError` (cp1252) on Windows.** Run with `PYTHONIOENCODING=utf-8`.
- **Engine can't reach the model.** Check `GX10_BASE_URL`/`GX10_MODEL` and that your
  endpoint serves `/v1/models`.
- **Constrained decoding crashes the server.** Some vLLM/GPU combos can't run the
  grammar bitmask kernel; use a recent vLLM build, or rely on the soft validate→reask
  path (ACK works either way).

## 8. Shell shortcuts (Windows / macOS / Linux)

The recommended client installs as a **global command**, exactly like `claude` / `kimi`:

```bash
cd clients/ink
npm install
npm install -g .         # builds + installs the global `ironclad` bin on your PATH
```

Now `ironclad` works from **any folder** — that folder becomes the working directory
(`codedir`), the same way Claude Code adopts the directory you launch it in. Nothing lives in
the project clone; the command sits in your npm prefix. (No global install? Run
`node clients/ink/dist/cli.js` after `npm run build`, or wrap that in a shell function.)

> **PowerShell note:** PowerShell does not always sync a child process's working directory to
> `Set-Location`, so on Windows wrap `ironclad` in the tiny function below that pins `--codedir
> (Get-Location).Path`. bash/zsh don't need this — there the bare global bin is enough.

**Set the orchestrator URL once** so you don't pass `--server` every time — a JSON config file
at `%APPDATA%\ironclad\config.json` (Windows) or `~/.config/ironclad/config.json` (macOS/Linux):

```json
{ "serverUrl": "http://<server-host>:8100" }
```

Precedence: **config file < `GX10_SERVER_URL` env < `ironclad --server <url>` flag**. The URL is
the orchestrator (`:8100`), **not** the model (`:8000`).

**Legacy Python clients** (optional) are plain shell functions — `ironclad-tui` (full-screen),
`ironclad-repl` (line REPL), `ironclad-server` (start the server). Add them only if you still
want them; the engine lives under `engine\` in your clone — adjust the path to match.

### Windows (PowerShell)

Open your profile (`notepad $PROFILE`; create it first if missing:
`if (!(Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force }`), paste,
then reload with `. $PROFILE`:

```powershell
$env:IRONCLAD_HOME = "C:\path\to\ironclad"

# `ironclad` is the global npm bin; this wrapper pins the working dir to the shell's current folder
# (PowerShell can otherwise hand a stale cwd to the child process).
function ironclad {
    $bin = (Get-Command ironclad.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($bin) { & $bin --codedir (Get-Location).Path @args }
    else { Write-Host "ironclad not installed — run 'npm install -g .' in $env:IRONCLAD_HOME\clients\ink" -ForegroundColor Yellow }
}

# Legacy Python clients + the server:
$IRONCLAD_PY = "$env:IRONCLAD_HOME\.venv\Scripts\python.exe"
function ironclad-tui    { & $IRONCLAD_PY "$env:IRONCLAD_HOME\engine\tui.py"    --codedir (Get-Location).Path @args }
function ironclad-repl   { & $IRONCLAD_PY "$env:IRONCLAD_HOME\engine\client.py" --codedir (Get-Location).Path @args }
function ironclad-server { & $IRONCLAD_PY "$env:IRONCLAD_HOME\engine\server.py" --host 0.0.0.0 --port 8100 @args }
```

### macOS / Linux (bash or zsh)

Append to `~/.zshrc` (zsh, macOS default) or `~/.bashrc` (bash), then
`source ~/.zshrc` / `source ~/.bashrc`:

```bash
export IRONCLAD_HOME="$HOME/ironclad"
IRONCLAD_PY="$IRONCLAD_HOME/.venv/bin/python"

# `ironclad` is the global npm bin (npm install -g .) — no function needed. Legacy clients + server:
ironclad-tui()    { "$IRONCLAD_PY" "$IRONCLAD_HOME/engine/tui.py"    --codedir "$(pwd)" "$@"; }
ironclad-repl()   { "$IRONCLAD_PY" "$IRONCLAD_HOME/engine/client.py" --codedir "$(pwd)" "$@"; }
ironclad-server() { "$IRONCLAD_PY" "$IRONCLAD_HOME/engine/server.py" --host 0.0.0.0 --port 8100 "$@"; }
```

`ironclad` (the recommended client) is the global bin and runs from any folder; `ironclad-tui`
/ `ironclad-repl` are the legacy Python clients and `ironclad-server` starts the server. All
need a real terminal (Windows Terminal / Terminal.app / any TTY); the legacy TUI falls back to
a line REPL if `prompt_toolkit` isn't installed.
