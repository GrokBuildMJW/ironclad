# MPR — Multi-Perspective Reasoning (privates ironclad-Plugin)

> **PRIVAT.** Dieses Plugin liegt unter `skills/mpr/` **ausserhalb von `core/`** und wird **nie**
> exportiert/veröffentlicht. Es konsumiert nur die stabilen ironclad-Primitive (P0-Dispatcher,
> `_WORKERS.fanout`, `_reduce_worker_results`, `_atomic_write`, `TaskStore`) über die öffentliche
> Plugin-Grenze — **kein Core-Fork, kein Core-Edit**.

MPR ersetzt für eine Anfrage den Single-Pass durch ein **Panel** unabhängiger Perspektiven
(Rollen aus der Registry), führt sie parallel aus und **synthetisiert** ein Ergebnis. Reasoning-only:
keine zusätzlichen Tools, kein Memory-Schreibpfad, voller Audit-Trail pro Run.

---

## Gates — Laden vs. Aktiv (WICHTIG)

MPR is a **core built-in** (ADR-0002 #115) — **always loaded**, no load gate. A single **runtime**
switch controls whether it runs; the tool is always registered, but it can be paused live.

| Gate | Source | Default | Effect when OFF | Toggle |
|------|--------|---------|-----------------|--------|
| **RUNTIME** | `mpr.enabled` in the config tree | **on** | `run_mpr` returns a short "MPR is disabled" note (0 LLM calls, no run dir) | **in the CLI: `/config set mpr.enabled off`** (no redeploy) |

- `mpr.enabled` **on** (default) → the panel runs. **off** → loaded but paused (each call returns the note).
- Deploy override: set `GX10_MPR_ENABLED=0` to make the runtime default off at boot.

> **Deprecation:** the legacy `GX10_MPR` *load* gate was removed (MPR is a core built-in now). Use
> the runtime `mpr.enabled` (`/config set mpr.enabled on|off`) or `GX10_MPR_ENABLED` at deploy.

---

## Laufzeit-Umschaltung im CLI (`/config set`)

`/config set` / `/config get` sind **generische, plugin-agnostische** Core-Befehle (siehe
[`docs/config-runtime.md`](../../docs/config-runtime.md)). Sie schreiben einen
gepunkteten Schlüssel in die laufende Config; MPR liest seine `mpr.*`-Sektion **bei jedem Aufruf neu**
(`entry._engine_deps`), also greift die Änderung ab dem **nächsten** `run_mpr` — ohne Neustart.

```
/config get mpr.enabled                 # aktuellen Wert anzeigen
/config set mpr.enabled on              # Panel scharf schalten (off|on)
/config set mpr.panel_mode deep         # Tiefe umschalten (direct|deep)
/config get mpr.panel_mode
```

Werte-Coercion: `on|true|yes → True`, `off|false|no → False`, sonst Zahl (int/float) oder String.

---

## Panel-Modus (`mpr.panel_mode`)

Die in-engine-Panel-Ausführung kennt zwei abgestimmte Pfade. Hintergrund: qwen3.6-35b ist ein
**Reasoning-Modell** — mit aktivem Thinking frisst der `<think>`-Block bei knappem Budget die ganze
Completion (Live-Bug #3: leere `perspective_NN.md`). Deshalb der schaltbare Modus:

| `panel_mode` | Thinking | Token-Budget je Perspektive | Wann |
|--------------|----------|-----------------------------|------|
| **`direct`** (Default, stabil) | **aus** | flach `4096` | Analyse geht direkt aufs Budget, keine `<think>`-Starvation, volle Fan-out-Concurrency, schnell |
| **`deep`** | **an** | per-Effort (low 2048 … xhigh 16384) | tieferes Reasoning; der Governor drosselt die Concurrency |

Der Classifier-/Router-Pfad läuft **immer** thinking-off (fester 768-Token-Cap; Live-Bug #1).

---

## Alle `mpr.*`-Config-Schlüssel

SSOT der Defaults: `skills/mpr/mpr_config.py` (`MprConfig`), abgestimmt auf Spec 09 §2.1.
Globale Präzedenz ist ironclads: **code-defaults < datei/conf < env < CLI (`/config set`)**.

| Schlüssel | Typ | Default | Bedeutung |
|-----------|-----|---------|-----------|
| `mpr.enabled` | bool | `false` | **RUNTIME-Gate** (s.o.) |
| `mpr.panel_mode` | `direct`\|`deep` | `direct` | Panel-Ausführungstiefe (s.o.) |
| `mpr.audit_level` | str | `full-per-perspective` | Audit-Granularität (`full-per-perspective`\|`manifest-only`) |
| `mpr.runs_dir` | str | `runs/mpr` | Config-Fallback. **STATE-Layout (B3):** ein Run routet ans aktive Initiative → `vault/<slug>/runs/<run_id>/`; ohne aktives Initiative ist `mpr_research` fail-closed (kein Schreiben in den Root). |
| `mpr.sovereignty.default_policy` | str | `offloadable` | Default-Datenpolitik je Item (`offloadable`\|`local-only`) |
| `mpr.sovereignty.internal_is_local_only` | bool | `true` | interne/sensible Daten nie auslagern |
| `mpr.sovereignty.fail_closed` | bool | `true` | im Zweifel **lokal** halten (nie spillen) |
| `mpr.budget.max_cost_usd_per_run` | float | `2.00` | Kosten-Cap je Run |
| `mpr.budget.max_tokens_per_run` | int | `200000` | Token-Cap je Run |
| `mpr.budget.per_provider` | dict | `{}` | engere Caps je Provider (tighter wins) |
| `mpr.budget.on_exceed` | str | `degrade` | `degrade`\|`truncate`\|`abort` |
| `mpr.providers.default_offload` | str | `claude-sonnet` | Default-Offload-Provider |
| `mpr.providers.pool` | dict | `DEFAULT_POOL` | Provider-Katalog (secret-frei; Endpunkte aus `connection.*`) |
| `mpr.providers.routing.spill_when_spark_busy` | bool | `true` | bei ausgelastetem Spark auslagern |
| `mpr.providers.routing.effort_to_provider` | dict | s. `DEFAULT_ROUTING` | Effort→Provider-Mapping |
| `mpr.router.*` | — | s. `config.py` | Router-Subconfig (z. B. `min_panel`) |
| `mpr.roles` / `mpr.registry.*` | — | see `registry/config.py` | **Reserved** — role/registry sub-config (`roles.max`, effort table, distinctness): loaded + validated but **not yet read** by the resolver (#503 MPR-REG-1) |

> **Boundary:** Der Pool enthält **keine** privaten Literale (keine Spark-IP, kein Hostname).
> Endpunkte kommen aus `connection.*`, Secrets nur als `*_api_key_env`-**Namen** (nicht der Wert).

---

## Env-Knöpfe (`GX10_MPR_*`)

Werden in `entry._engine_deps` einmal pro Prozess auf die `mpr`-Sektion gelegt
(`mpr_config._apply_mpr_env`) — der Deploy-Default-Pfad. Danach gewinnt `/config set` zur Laufzeit.

| Env | wirkt auf | Beispiel |
|-----|-----------|----------|
| `GX10_MPR` | **LOAD-Gate** (Tool registrieren) | `GX10_MPR=1` |
| `GX10_MPR_ENABLED` | `mpr.enabled` (RUNTIME-Default beim Deploy) | `GX10_MPR_ENABLED=1` |
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

## Deploy auf den Spark

```bash
# Standard: MPR geladen, Runtime AUS (per /config set zuschaltbar):
bash deploy/spark/deploy-mpr.sh

# Beim Deploy schon scharf + deep:
GX10_MPR_ENABLED=1 GX10_MPR_PANEL_MODE=deep bash deploy/spark/deploy-mpr.sh

# P0-Provider-Router live (externer Offload-Lane läuft am PC-Client, nicht am Spark):
GX10_PROVIDERS=1 bash deploy/spark/deploy-mpr.sh
```

Das Skript lässt das OSS-Image **unangetastet** und injiziert das Plugin per Host-Volume-Mount
(`-v skills:/skills`) + `GX10_PLUGINS_DIR=/skills`. Kein Core-Edit, kein Image-Rebuild.

---

## Operator-Test im CLI (Rezept)

```bash
# 1) Verbinden (Client → Orchestrator)
ironclad --server http://<your-server-host>:8100 --codedir .

# 2) Geladen, aber aus? → erwartet den Deaktiviert-Hinweis
/config get mpr.enabled          # → mpr.enabled = False
<eine Reasoning-Frage>           # → run_mpr antwortet „MPR ist deaktiviert …" (Single-Pass bleibt)

# 3) Scharf schalten + dieselbe Frage → Panel läuft (Initiative muss aktiv sein, sonst fail-closed)
/initiative new Architektur-Frage --type mpr
/config set mpr.enabled on
<dieselbe Frage>                 # → Panel, Run-Verzeichnis unter vault/<slug>/runs/<run_id>/

# 4) Tiefe vergleichen
/config set mpr.panel_mode deep
<dieselbe Frage>                 # → tiefere Perspektiven (thinking-on, per-Effort-Budget)

# 5) Wieder pausieren
/config set mpr.enabled off
```

Artefakte je Run (`vault/<slug>/runs/<run_id>/`): `manifest.json` (Provenance/Budget/Sovereignty),
`perspective_NN.md` (je Rolle), `synthesis.md`. Bei deaktiviertem Runtime-Gate entsteht **kein**
Verzeichnis und es gibt **0 LLM-Calls**.

---

## Tests

```bash
python -m pytest skills/mpr/tests -q          # Plugin-Suite (deterministisch, Stub-Dispatcher)
python scripts/ci/check_core_boundary.py      # core/ bleibt grenzrein
```

Status-SSOT der Bauarbeit: `vault/Plan/mpr/TASKS.md`. Spezifikationen: `vault/Plan/mpr/`.
