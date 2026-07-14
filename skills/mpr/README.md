# MPR — Multi-Perspective Reasoning (ironclad core built-in)

> **A public core built-in.** MPR ships under `skills/mpr/` and is exported with the framework as the
> flagship example skill (ADR-0002 #115). It consumes only the stable ironclad primitives (the P0
> dispatcher, `_WORKERS.fanout`, `_reduce_worker_results`, `_atomic_write`, `TaskStore`) over the public
> plugin boundary — **no core fork, no core edit**.

For one request, MPR replaces the single pass with a **panel** of independent perspectives (roles from the
registry), runs them in parallel, and **synthesizes** a result. Reasoning-only: no extra tools, no
memory write path, a full audit trail per run.

---

## Gates — loaded vs. active (IMPORTANT)

MPR is a **core built-in** (ADR-0002 #115) — **always loaded**, no load gate. A single **runtime**
switch controls whether it runs; the tool is always registered, but it can be paused live.

| Gate | Source | Default | Effect when OFF | Toggle |
|------|--------|---------|-----------------|--------|
| **RUNTIME** | `mpr.enabled` in the config tree | **on** | `run_mpr` returns a short "MPR is disabled" note (0 LLM calls, no run dir) | **in the CLI: `/config set mpr.enabled off`** (no redeploy) |
| **MPR-AT-FORK** | `ace.fork_mpr.enabled` in the config tree | **off** | a declared architecture `ForkSignal` never triggers MPR — the STOP-and-ask is byte-identical to today (0 LLM calls) | **`/config set ace.fork_mpr.enabled on`** (no redeploy) |

- `mpr.enabled` **on** (default) → the panel runs. **off** → loaded but paused (each call returns the note).
- Deploy override: set `GX10_MPR_ENABLED=0` to make the runtime default off at boot.
- `ace.fork_mpr.enabled` **off** (default) → ACE's MPR-at-fork option is dormant. **on** → when the dev-loop
  declares an architecture fork (a `ForkSignal` on the ledger, epic #855 / M5), the engine runs MPR's
  `architecture-decision` panel **off the hot path** (a background worker), pre-informed by the playbook's
  prior fork decisions, and produces a decision-matrix as a well-founded proposal for the human ask. Requires
  `mpr.enabled` **on** + an active project; any failure degrades to a no-op (the ask still surfaces). MPR
  only *proposes* — the operator still decides.

> **Deprecation:** the legacy `GX10_MPR` *load* gate was removed (MPR is a core built-in now). Use
> the runtime `mpr.enabled` (`/config set mpr.enabled on|off`) or `GX10_MPR_ENABLED` at deploy.

---

## Runtime toggle in the CLI (`/config set`)

`/config set` / `/config get` are **generic, plugin-agnostic** core commands (see
[`docs/config-runtime.md`](../../docs/config-runtime.md)). They write a dotted key into the
running config; MPR re-reads its `mpr.*` section **on every call** (`entry._engine_deps`), so the change
takes effect from the **next** `run_mpr` — no restart.

```
/config get mpr.enabled                 # show the current value
/config set mpr.enabled on              # arm the panel (off|on)
/config set mpr.panel_mode deep         # switch the depth (direct|deep)
/config get mpr.panel_mode
```

Value coercion: `on|true|yes → True`, `off|false|no → False`, else a number (int/float) or a string.

---

## Panel mode (`mpr.panel_mode`)

The in-engine panel execution has two tuned paths. Background: qwen3.6-35b is a **reasoning model** — with
thinking on, the `<think>` block eats the whole completion under a tight budget (live bug #3: empty
`perspective_NN.md`). Hence the switchable mode:

| `panel_mode` | Thinking | Token budget per perspective | When |
|--------------|----------|-------------------------------|------|
| **`direct`** (default, stable) | **off** | flat `4096` | analysis goes straight at the budget, no `<think>` starvation, full fan-out concurrency, fast |
| **`deep`** | **on** | per-effort (low 2048 … xhigh 16384) | deeper reasoning; the governor throttles the concurrency |

The classifier/router path always runs thinking-off (a fixed 768-token cap; live bug #1).

---

## All `mpr.*` config keys

SSOT of the defaults: `skills/mpr/mpr_config.py` (`MprConfig`), aligned with spec 09 §2.1.
The global precedence is ironclad's: **code defaults < file/conf < env < CLI (`/config set`)**.

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `mpr.enabled` | bool | `true` | **RUNTIME gate** (see above) — on by default; `/config set mpr.enabled off` pauses it |
| `mpr.panel_mode` | `direct`\|`deep` | `direct` | panel execution depth (see above) |
| `mpr.audit_level` | str | `full-per-perspective` | audit granularity (`full-per-perspective`\|`manifest-only`) |
| `mpr.runs_dir` | str | `runs/mpr` | config fallback. **STATE layout (B3):** a run routes to the active project → `vault/<slug>/runs/<run_id>/`; with no active project `mpr_research` is fail-closed (no write into the root). |
| `mpr.sovereignty.default_policy` | str | `offloadable` | default data policy per item (`offloadable`\|`local-only`) |
| `mpr.sovereignty.internal_is_local_only` | bool | `true` | never offload internal/sensitive data |
| `mpr.sovereignty.fail_closed` | bool | `true` | when in doubt keep it **local** (never spill) |
| `mpr.budget.max_cost_usd_per_run` | float | `2.00` | cost cap per run |
| `mpr.budget.max_tokens_per_run` | int | `200000` | token cap per run |
| `mpr.budget.per_provider` | dict | `{}` | tighter caps per provider (tighter wins) |
| `mpr.budget.on_exceed` | str | `degrade` | `degrade`\|`truncate`\|`abort` |
| `mpr.providers.default_offload` | str | `claude-sonnet` | default offload provider |
| `mpr.providers.pool` | dict | `DEFAULT_POOL` | provider catalogue (secret-free; endpoints from `connection.*`) |
| `mpr.providers.routing.spill_when_spark_busy` | bool | `true` | offload when the Spark is busy |
| `mpr.providers.routing.effort_to_provider` | dict | see `DEFAULT_ROUTING` | effort→provider mapping |
| `mpr.router.*` | — | see `config.py` | router sub-config (e.g. `min_panel`) |
| `mpr.roles` / `mpr.registry.*` | — | see `registry/config.py` | **Reserved** — role/registry sub-config (`roles.max`, effort table, distinctness): loaded + validated but **not yet read** by the resolver (#503 MPR-REG-1) |

> **Boundary:** the pool holds **no** private literals (no Spark IP, no hostname). Endpoints come from
> `connection.*`; secrets only as `*_api_key_env` **names** (never the value).

---

## Env knobs (`GX10_MPR_*`)

Applied once per process in `entry._engine_deps` onto the `mpr` section (`mpr_config._apply_mpr_env`) —
the deploy-default path. After that, `/config set` wins at runtime.

| Env | affects | Example |
|-----|---------|---------|
| `GX10_MPR_ENABLED` | `mpr.enabled` (RUNTIME default at boot; on by default) | `GX10_MPR_ENABLED=0` to start paused |
| `GX10_MPR_PANEL_MODE` | `mpr.panel_mode` | `GX10_MPR_PANEL_MODE=deep` |
| `GX10_MPR_AUDIT_LEVEL` | `mpr.audit_level` | `manifest-only` |
| `GX10_MPR_RUNS_DIR` | `mpr.runs_dir` | `/work/runs/mpr` |
| `GX10_MPR_DEFAULT_POLICY` | `mpr.sovereignty.default_policy` | `local-only` |
| `GX10_MPR_FAIL_CLOSED` | `mpr.sovereignty.fail_closed` | `0` |
| `GX10_MPR_MAX_COST_USD` | `mpr.budget.max_cost_usd_per_run` | `0.5` |
| `GX10_MPR_MAX_TOKENS` | `mpr.budget.max_tokens_per_run` | `100000` |
| `GX10_MPR_ON_EXCEED` | `mpr.budget.on_exceed` | `truncate` |
| `GX10_MPR_DEFAULT_OFFLOAD` | `mpr.providers.default_offload` | `claude-opus` |

---

## Configuration

MPR is a **core built-in** — it ships with the framework, is always loaded, and runs **on by default**.
There is nothing to deploy separately. Configure it two ways:

```bash
# Boot default (env, applied once per process before the runtime toggle can override it):
GX10_MPR_ENABLED=0        # start paused (default is on)
GX10_MPR_PANEL_MODE=deep  # start in deep mode (default is direct)

# Runtime (no restart) from inside the CLI:
/config set mpr.enabled off      # pause the panel
/config set mpr.panel_mode deep  # switch depth
```

---

## Try it in the CLI (recipe)

```bash
# 1) Connect (client → orchestrator)
ironclad --server http://<your-server-host>:8100 --codedir .

# 2) On by default → inside an active project, a reasoning question runs the panel
/config get mpr.enabled          # → mpr.enabled = True
/project new architecture-question
<a reasoning question>           # → panel; a run directory is created under the active project (else fail-closed)

# 3) Compare depth
/config set mpr.panel_mode deep
<the same question>              # → deeper perspectives (thinking-on, per-effort budget)

# 4) Pause it → each call returns the short "MPR is disabled" note (single pass, 0 panel LLM calls, no run dir)
/config set mpr.enabled off
<the same question>
```

Artifacts per run (`vault/<slug>/runs/<run_id>/`): `manifest.json` (provenance/budget/sovereignty),
`perspective_NN.md` (per role), `synthesis.md`. With the runtime gate off, **no** directory is created
and there are **0 LLM calls**.

---

## Tests

```bash
python -m pytest skills/mpr/tests -q          # plugin suite (deterministic, stub dispatcher)
```
