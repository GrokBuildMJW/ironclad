# AGENTS.md — auto-setup runbook for an AI coding agent

This file is a **deterministic, verifiable runbook** for an AI coding agent (Claude
Code, Cursor, etc.) asked to set up and smoke-test Ironclad. Follow the steps in
order; each has an explicit **check** and a **stop condition**. Do not improvise past
a failed check — report the exact error and stop (fail-closed).

> Ironclad is **pre-release / in active development** and is developed on an **NVIDIA
> DGX Spark** with a local **vLLM** + **Qwen3.6-35B-A3B-NVFP4** endpoint. Any
> OpenAI-compatible endpoint works.

## Ground rules

- **Secret-free core.** Never hard-code a host, IP, key or path into the repo. Use
  env vars / flags. There is a CI boundary check that fails on private literals.
- **Verify each step before continuing.** If a check fails, stop and surface the
  command + its output. Do not mark setup "done" with a failing test.
- **Don't push or publish** unless explicitly asked.

> Just installing to **use** Ironclad (not develop it)? The fastest path is the bundled one-shot
> installer — see **Track D**. Track A below is the manual/developer setup.

## Track A — local development setup

1. **Python ≥ 3.10.** `python --version`. Stop if lower.
2. **Create + activate a venv.**
   `python -m venv .venv && . .venv/bin/activate` (Windows: `.venv\Scripts\Activate.ps1`).
3. **Install.** `pip install -e ".[engine]" pytest`.
   - Check: `python -c "import ack, pydantic; print('ack ok')"`.
4. **Run the test suite.** `python -m pytest ack/tests -q`.
   - Check: all tests pass. Stop on any failure; report the failing test names.
5. **Confirm the public boundary stays clean:** the repository root is the boundary surface; shipped
   code must not import from or hardcode any private path or literal. A boundary check enforces this in CI.

Done-A: `import ack` works and `pytest` is green.

6. **(Optional) Install the recommended client globally.** If **Node ≥ 22** is available and
   the user wants the TypeScript terminal client: `( cd clients/ink && npm install && npm install -g . )`
   → a global `ironclad` command (like claude / kimi), installed in the npm prefix, not the clone.
   - Check: `command -v ironclad` (POSIX) / `Get-Command ironclad` (PowerShell) resolves.
   Skip on no Node — the Python clients below work without it.
7. **(Optional) Install shell shortcuts** for the legacy clients. Detect the OS/shell and
   append the matching block from [§8 of `SETUP.md`](SETUP.md#8-shell-shortcuts-windows--macos--linux),
   substituting `IRONCLAD_HOME` with the absolute clone path (the `ironclad` command is the
   global bin from step 6; `ironclad-tui`/`ironclad-repl` are the legacy Python clients):
   - **Windows / PowerShell:** ensure `$PROFILE` exists, append the `function ironclad…`
     block, tell the user to run `. $PROFILE`.
   - **macOS / Linux:** append the `ironclad()…` block to `~/.zshrc` (zsh) or
     `~/.bashrc` (bash), tell the user to `source` it.
   - Check: in a fresh shell, the `ironclad` command resolves (`Get-Command ironclad` /
     `type ironclad`).
   Never hard-code the path into the repo — only into the user's own profile.

## Track B — connect to a model endpoint

1. **Have an OpenAI-compatible endpoint** reachable (e.g. vLLM). To stand one up on a
   DGX Spark, use Track C.
2. **Point Ironclad at it:**
   ```bash
   export GX10_BASE_URL=http://<host>:8000/v1
   export GX10_MODEL=<served-model-name>
   export GX10_API_KEY=...          # only if the endpoint needs one
   ```
   - Check: `curl -s "$GX10_BASE_URL/models"` lists your model.
3. **Smoke a turn.** Start the orchestrator, then drive it with the client:
   `python engine/server.py --port 8100 &` then
   `GX10_SERVER_URL=http://localhost:8100 python engine/client.py --codedir .` and type a
   short question. (Or, if you installed it in A.6, the recommended client:
   `GX10_SERVER_URL=http://localhost:8100 ironclad`.)
   - Check: a coherent answer + a `✓ DONE` line. Stop if the call errors.
   - A plain question needs nothing more; to drive an artefact-producing **build** task, first
     `/project new <name>` (fail-closed without one — see
     [`docs/state-and-initiative.md`](docs/state-and-initiative.md)).

Done-B: a real model turn returns through the engine.

## Track C — DGX Spark stack (one-shot)

If the task is "set up the Spark", use the bundled idempotent bootstrap instead of
hand-running Docker. It brings up vLLM (and optionally the orchestrator server). It is
**parameterized — pass the host/model, never bake them in.**

```bash
# Reference launch + what each piece is: see docs/dgx-spark.md
bash scripts/spark-bootstrap.sh --help
bash scripts/spark-bootstrap.sh \
     --model-dir ~/models/RedHatAI-Qwen3.6-35B-A3B-NVFP4 \
     --served-name qwen3.6-35b
```

- Check after vLLM start: `curl -s http://localhost:8000/v1/models` lists the model.
- Check after orchestrator start (if `--with-orchestrator`):
  `curl -s http://localhost:8100/health` returns `{"ok": true, ...}`.
- Stop conditions: GPU not visible to Docker (`--gpus all` fails) → report; model dir
  missing → report the path; port already in use → report.

Done-C: `/v1/models` and (if requested) `/health` both respond.

## Track D — one-shot desktop install (the bundled installer)

If the task is "install Ironclad on this machine" and the user wants the single-command path, use the
bundled installer instead of hand-running Track A.6–7. It is **cross-platform and secret-free** — pass the
endpoint, never bake it in.

1. **From the repo clone, in the project folder to drive**, run the installer for the OS:
   - macOS / Linux: `bash install/ironclad-install.sh` (add `--base-url http://<host>:8000/v1 --model <name>`
     if the endpoint is not the localhost default).
   - Windows / PowerShell: `install\ironclad-install.ps1` (add `-BaseUrl … -Model …`).
   - Check: it prints `done. Desktop install in <root>` and writes `<project>/.ironclad/config.json`.
     Stop on any prereq/venv/pip error and report it.
2. **Activate the command** in the current shell: `source ~/.bashrc` (or `~/.zshrc`) / `. $PROFILE`.
   - Check: `command -v ironclad` (POSIX) / `Get-Command ironclad` (PowerShell) resolves.
3. **Verify** with the read-only doctor: `ironclad-doctor`.
   - Check: it prints the engine version and `engine … reachable` once the engine has been started
     (the launcher starts it on first `ironclad`). Stop if the model endpoint shows `NOT reachable` —
     fix the endpoint (Track B/C) before declaring done.
4. **Smoke a turn:** run `ironclad`, ask a short question, confirm a coherent answer. (An artefact-producing
   build task first needs `/project new <name>`.)

Done-D: the installer completed, `ironclad-doctor` is green, and a turn returns. Never edit the scripts to
inject a host — pass `--base-url`/`-BaseUrl` or `GX10_BASE_URL`; the repo stays secret-free.

## Definition of done

- Track A green (import + tests), **and**
- Track B green (a real turn) **or** Track C green (endpoints up), per the task.
- No secrets written into the repo; boundary check still PASSED.

Report a short summary: what was installed, which endpoint/model is wired, test
result, and any check that failed with its exact output.
