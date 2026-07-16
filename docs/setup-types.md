# Setup types — boot-fixed operating mode (`setup.type`)

`setup.type` is a **boot-only** config key that selects **where ironclad runs**. The orchestrator and the
code agents are **always co-located** on one machine — there is no cross-machine offload. It is read **once
at startup** and wires the provider dispatcher's runner; it is **not** runtime-switchable (a
[frozen key](config-runtime.md) — `/config set setup.type` is refused).

Pick **one** value at deploy time — set it in the **config file** (`setup.type`) or via the
**`GX10_SETUP_TYPE`** env var (there is no CLI flag for it). It is a [frozen config key](config-runtime.md):
`/config get setup.type` reads it, `/config set setup.type …` is refused. There are **three** values; the
dispatcher code is the same for all — only *which runner closure* is injected at boot differs (`auto` is not
its own runner: it **resolves to `server` or `local`** at boot from the `base_url`).

| `setup.type` | Where the engine + agents run | Model + memory | Code agents | Effect |
|--------------|-------------------------------|----------------|-------------|--------|
| **`server`** (engine code default) | the model host (e.g. a GPU server, containerized) | local to that host | — (in-engine only; external agents deferred) | **byte-identical to a no-provider deployment** |
| **`local`** | natively on the user's machine (the engine + agents run here) | **remote** over the network (the model host keeps the GPU model) | local subprocess (`claude --print`) | engine + agents co-located on the user's machine |
| **`auto`** (desktop launcher default) | derived at boot from `base_url` | — | — | a **loopback** `base_url` ⇒ `server` (fully in-box), a **remote** `base_url` ⇒ `local`; so a fresh default install boots without baking a model host into the repo |

**Two fixed poles, regardless of the value:** the **model and the _Cold_ memory (Mem0) always live on the
model host** (both are GPU-/LLM-coupled — Mem0's embedder/graph extraction run there), and the **terminal
client always runs on the user's machine** — it connects to wherever the orchestrator lives (remote for
`server`, loopback for `local`). The **_Warm_ cache (Valkey) follows the orchestrator**, NOT the model: a
loopback hit is the performance *ideal*, but a LAN hop to the model host is acceptable (a ~1 ms warm hit
still shortcuts a far costlier Cold vector+graph query). In `local`, Valkey runs on the model host and is
reached over the LAN (Valkey has no native Windows build); on a `local` Linux box it can be loopback.
Valkey ships **loopback-only by default** — reaching it over the LAN is a deliberate operator step (bind on
the LAN **and** set `--requirepass` + firewall-pin; see `docker-compose.yml`), so until then `/health`
reports `warm: off`/`down`, fail-soft. The
engine block
(orchestrator + agents) is strictly **on one machine, never both** — that single choice is what `setup.type`
makes. A deployment may surface these values under its own install-time names; the engine knows
`server`, `local`, and `auto` (the last derives one of the first two at boot).

## Semantics & validation (fail-closed)

At startup the engine derives the wiring from `setup.type` and **aborts with a clear message** rather than
silently degrading when the mode can't be honored:

- `server` → dispatcher inactive; in-engine only; byte-identical to running without any provider pool.
- `local` → requires a **remote** `base_url` (a loopback endpoint fails closed — the model lives elsewhere;
  the engine sits with the CLIs) **and** a reachable agent CLI. The boot probe resolves
  **`GX10_CLAUDE_BIN`** (default `claude`) on `PATH` via `shutil.which`; if you point the agent at a
  different binary through the `GX10_AGENT_CMD` template, make sure that binary is the one on `PATH`.
  Missing remote URL or missing CLI → the server **fails closed** (no silent degrade).
- `auto` → resolves to `server` when `base_url` is loopback (a fully in-box desktop) and to `local` when it
  is remote, then applies that mode's rules above (so an `auto`→`local` install still needs a reachable
  CLI). This is the desktop launcher's default: it ships a loopback `base_url`, so a fresh install boots
  in-engine, and pointing `GX10_BASE_URL` at a remote model switches it to the LAN-offload `local` topology
  — no host baked into the repo.
- An unknown value → fails closed.

**`setup.type` is the single boot control for dispatcher activation.** Whether the provider dispatcher
runs is derived from `setup.type` alone (`server` → inactive, `local` → active). The retired
`providers.enabled` / `GX10_PROVIDERS` inputs are warning-only tombstones, not a second gate. Configure the
topology with `setup.type`, not with a separate provider on/off switch.

**Security override:** with `security.profile = sealed` (no egress) the engine is forced to `server`
(in-engine only) regardless of `setup.type`. Items classified local-only/sensitive are never sent to an
external agent.

## Why boot-only

The setup type wires the runner at startup; changing it mid-process would leave the running dispatcher
pointed at the wrong substrate. So it is a [frozen config key](config-runtime.md): readable with
`/config get setup.type`, but `/config set setup.type …` is refused — set it in the deploy and restart.
