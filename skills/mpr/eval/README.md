# MPR `eval/` — A/B-Harness · Rubric · Judge · Eval-Sets (Spec 08)

Heimat der **Live-/Eval-Schicht** des MPR-Plugins (Spec 08 §3–§5, §10). Getrennt von den
deterministischen Unit-Tests unter `../tests/` (die laufen modell-/netzfrei im Merge-Gate); was hier
liegt, treibt **echte** Turns gegen den deployten Orchestrator und kostet Tokens → läuft **nicht** bei
jedem Commit, sondern vor dem Merge eines MPR-relevanten Changes (Gate-Stufe 4, Spec 08 §7).

## Layout

```
eval/
  README.md            # dieses Dokument
  harness.py           # A/B-Harness „MPR on/off" (ctx_harness-Stil, stdlib-only, --selftest)   [Ev-3]
  rubric.py            # Rubric als Daten (pure, unit-getestet)                                  [Ev-5]
  judge.py             # LLM-Judge-Panel (3 Stimmen, gestubbt im Gate; live nur im A/B-Report)   [Ev-5]
  gate.py              # Merge-Gate-Auswertung (liest gate.toml, prüft Schwellen)                [Ev-6]
  gate.toml            # Gate-Schwellen (coverage_floor/budget/epsilon/decline_rate), tunebar    [Ev-6]
  sets/                # kuratierte Eval-Sets je Domäne (jsonl) — KALIBRIERUNG, braucht User     [Ev-8]
  refs/                # Referenz-Dimensionslisten je Query (Ground-Truth-Achsen)                [Ev-8]
  recordings/          # Record/Replay-Manifeste (Test-Fixtures, generiert)                       [Ev-4]
```

Alle Module (`harness.py`/`rubric.py`/`judge.py`/`gate.py`/`gate.toml`) sind **gebaut** (Phase-2-Einheiten
Ev-3/5/6); die Datenordner `sets/`/`refs/`/`recordings/` sind mit kuratierten Eval-Daten/Fixtures gefüllt
(`.gitkeep` hält sie auch leer versionierbar).

## Reconcile gegen Spec 08 §1 (WICHTIG — die Spec beschreibt ein früheres Layout)

Spec 08 §1/§2 wurden gegen die **ursprünglich angenommene** Modul-Aufteilung geschrieben
(`mpr.py`, `registry.py`, `effort.py`, `test_router.py`, gx10-Global-Fixtures `_StubWorkers`/
`restore_flags`). Die **gebaute** Phase-1-Architektur weicht bewusst ab — die Garantien sind identisch,
die Form anders:

| Spec 08 §1 nimmt an | Ist (Phase 1) | warum |
|---|---|---|
| `mpr.py` (CASE+run) | `entry.py` + dünner `skills/mpr_research.py` | Loader scannt nur `**/skills/*.py`; Logik importierbar+stubbar |
| `registry.py` (flach) | `registry/`-Package | konsolidiert (schema/resolve/synthesis/loader/guards/adaptive/config) |
| `effort.py` | `registry/resolve.py` | Effort-/Policy-Resolution gehört zur Registry |
| `test_router.py` etc. | `test_router_*.py` (8 Dateien) + `test_registry_*.py` … | per-Unit gewachsen; **ein** Test-Root |
| gx10-Global-Fixtures (`_StubWorkers`, `restore_flags`) | **Injected-Deps** (`Deps`-Dataclass, Stubs als Argumente) | netzfrei OHNE gx10 auf sys.path; sauberer als Monolith-Globals |

Die vollständige **§2.1–§2.9 → bestehende-Tests Coverage-Map** liegt in der Ev-1+Ev-2-Quittung in
`vault/Plan/mpr/TASKS.md`. Der strukturelle Sammel-Gate (`tests/test_eval_coverage.py`) erzwingt die
Invarianten (kein eigener Dispatcher; §2-Komponenten-Testdateien vorhanden).

## P0-Dispatch-Wiring — GEBAUT (war: deferred)

Das „run_mpr → P0-Dispatch"-Wiring ist verdrahtet **und getestet**: `tests/test_p0_dispatch.py` (PW-1)
treibt die Perspektiven über den P0 `ProviderDispatcher` und prüft net-frei via injiziertem
`_StubDispatcher` die §2.4-Seam (`RouteRequest[]`/`DispatchPolicy`) und §2.6-Provenance. Damit sind die
zuvor deferreten Spec-08-Tests un-deferred (der vormalige Tracking-Skip-Stub existiert nicht mehr):

- §2.4 Dispatch-Seam (`_StubDispatcher` captured `RouteRequest[]`/`DispatchPolicy` via `ProviderDispatcher.dispatch`)
- §2.6 Provenance-aus-`DispatchResult` (Manifest trägt heute in-engine-Substrat, nicht Dispatch-Provenance)
- §2.3 Envelope/Governor (`ReasoningWorkers._plan_concurrency` über ein Panel)
- §2.7 argv-grep / §2.8 sealed-no-egress (kein externer Argv-/Egress-Pfad im in-engine-MVP)

Die **lasttragenden** Garantien dieser §§ (local-only nie extern, Policy-Passthrough, Effort-Clamp,
fail-closed) werden **heute** an der **Plan-Naht** (`plan_perspective_dispatch` → `ProviderChoice`) in
`tests/test_sovereignty.py` deterministisch bewiesen.
