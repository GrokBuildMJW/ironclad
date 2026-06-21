"""MPR plugin orchestration (Spec 10 §3/§4 + Spec 02 §4 data-flow). The thin standalone entry
``skills/mpr/skills/mpr_research.py`` binds the real engine handles; the logic lives here so it is
importable + testable with injected stubs (no engine, no network).

``run_mpr`` ties the whole MPR layer together: classify (router) → decline? direct answer : build panel
(registry) → dispatch perspectives (sovereignty chokepoint + in-engine fanout, the MVP substrate) →
synthesize (templates) → write manifest + index (audit) → mirror insight to memory → return the report
between the sentinels. It **never raises** — every stage is caught and degrades to a clear,
plugin-prefixed string (Spec 10 §4: the dispatch has no plugin-side try/except).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from . import audit, i18n
from .registry.resolve import EFFORT_MAX_TOKENS
from .router import classify
from .schema import RouterInput
from .sovereignty import plan_perspective_dispatch
from .synthesis import PerspectiveResult, SynthesisInput, synthesize, write_back

REPORT_OPEN = "<<<MPR_REPORT>>>"
REPORT_CLOSE = "<<<END>>>"
_PANEL_DIRECT_TOKENS = 4096   # "direct" panel mode: flat budget for a substantive thinking-off analysis/lens
_VALID_AUDIT_LEVELS = ("full-per-perspective", "manifest-only")   # allowlist for the model-facing audit_level (§7)

# CASE-Dict (§3) — name == capability per Spec 10 §3 (packaging spec is authoritative; reconciles the
# 'mpr.research' form in Spec 09 §9.0). The description IS the layer-1 gate + the sentinel instruction.
_DESCRIPTION = (
    "Beleuchtet EINE Frage durch ein domänenspezifisches Experten-Rollen-Panel parallel und "
    "synthetisiert die Sichten zu EINEM begründeten Urteil/Report. Reasoning-only (Analyse, Suche, "
    "Datei-I/O) — KEINE Code-Mutation. WOFÜR: mehrdimensionale Entscheidungen (Architektur-Trade-offs), "
    "Evidenz-Recherche über mehrere Quellen/Jurisdiktionen, Wettbewerbs-/Risiko-Analysen — Fragen mit "
    "mehreren legitimen, widerstreitenden Blickwinkeln. NICHT FÜR: Ein-Fakt-Fragen, Single-Source-"
    "Lookups, einfache Definitionen, Code-Änderungen — in diesen Fällen NICHT aufrufen (der Router lehnt "
    "sonst ohnehin früh ab). Gib den MPR-Report zwischen den Sentinels "
    f"{REPORT_OPEN} … {REPORT_CLOSE} WÖRTLICH aus."
)


def build_case() -> dict:
    # The catalogue manifest fields (type/version/provenance, #90) ride on CASE so mpr — the
    # reference built-in — appears in ack.catalogue. Extra keys are ignored by the tool path
    # (discover_skills / derive_tool_schema), so this is additive + back-compatible.
    return {"name": "mpr_research", "capability": "mpr_research", "domain": "reasoning",
            "type": "capability", "version": "0.1.0", "provenance": "built-in",
            "description": _DESCRIPTION}


def mpr_enabled(env: Optional[dict] = None) -> bool:
    """A/B gate (§5): MPR registers a tool only when GX10_MPR is truthy (the env mirror of mpr.enabled).
    Off → the standalone entry exports no CASE/run → no tool → byte-identical single-pass turn."""
    import os
    env = env if env is not None else os.environ
    return str(env.get("GX10_MPR", "")).strip().lower() in ("1", "true", "yes", "on")


def _wrap(body: str) -> str:
    return f"{REPORT_OPEN}\n{(body or '').strip()}\n{REPORT_CLOSE}"


@dataclass
class Deps:
    """Injected handles. run()/standalone binds the real engine globals; tests pass stubs."""
    llm: Any = None                              # ClassifierLLM for the router (complete_json)
    registry: Any = None                         # PanelRegistry
    dispatcher: Any = None                       # P0 ProviderDispatcher (real routing/offload); None → in-engine fanout
    fanout: Optional[Callable] = None            # (prompts, *, system, max_tokens) -> list[worker dict]
    synth_llm: Optional[Callable] = None         # (prompt, *, system, max_tokens) -> str
    reducer: Optional[Callable] = None           # _reduce_worker_results
    store: Any = None                            # TaskStore
    writer: Optional[Callable] = None            # _atomic_write(path, text)
    store_delete: Optional[Callable] = None
    run_id: Optional[str] = None
    runs_dir: str = "runs/mpr"
    audit_level: str = "full-per-perspective"
    operator_permission: str = "acceptEdits"
    enabled: bool = True                         # runtime active-gate; live default off (cfg) → /config set mpr.enabled on
    panel_mode: str = "direct"                   # in-engine panel: "direct" (thinking-off) | "deep" (thinking-on + effort budgets)
    pool: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    default_offload: str = "claude-sonnet"
    sovereignty: dict = field(default_factory=dict)
    language: str = "en"                          # #18-4c: active output language (en source; de/… via locale overlay)
    index_runs: bool = True                       # #52: register the run in the TaskStore? off when #15 blocks the
                                                  # pipeline (mpr initiative) — runs/manifest+INDEX.md are the record there


_MPR_ROOT = Path(__file__).resolve().parent   # skills/mpr/ — panel discovery root


class _ClassifierAdapter:
    """Adapt the engine's OpenAI-compatible client onto the ClassifierLLM port (Spec 04 §3.3)."""

    def __init__(self, client: Any, model: Any):
        self._client, self._model = client, model

    def complete_json(self, system: str, user: str, *, max_tokens: int, temperature: float) -> str:
        # Classify is STRUCTURED EMISSION (compact decision JSON), not reasoning — disable qwen3 thinking
        # (mirrors workers._one's enable_thinking flag, the ACK structured-emission path). With thinking ON
        # a reasoning model burns the small ROUTER_MAX_TOKENS cap on <think> → empty/truncated content →
        # router-classify-failed (live-only gap the canned-JSON tests miss).
        resp = self._client.chat.completions.create(
            model=self._model, max_tokens=max_tokens, temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or ""


def _resolve_store(mod: Any) -> Any:
    """Bind ironclad's single shared TaskStore. The engine exposes it as the lazy accessor ``_store()``
    (gx10.py:1965) — calling it returns/creates the one shared instance; ``STORE`` is the underlying
    global (None until first access). NB: ``getattr(mod, "_store")`` yields the FUNCTION, so it must be
    CALLED — binding the function object instead made ``index_in_taskstore`` hit ``store.create`` on a
    non-store and silently no-op (task_id stayed None). Returns None on a minimal/old engine stub."""
    fn = getattr(mod, "_store", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001 — store build fault → no indexing (fail-soft)
            return None
    return getattr(mod, "STORE", None)


def _engine_deps() -> Deps:
    """Bind the real engine handles (Spec 04 §2 Weg 1) — lazy + fail-soft. Engine absent → minimal Deps
    (run_mpr then degrades to a clear ERROR string). Server-side glue; the orchestration is tested with
    stubs, this binding is verified on deploy."""
    d = Deps()
    try:  # noqa: BLE001 — every binding step is best-effort
        import gx10  # engine module (server-side; core/engine on sys.path at runtime)
        from .mpr_config import _apply_mpr_env, load_mpr_config
        from .registry.loader import get_registry

        # Read the LIVE config tree each call (so a runtime `/config set mpr.*` takes effect on the next
        # run — entry.py is re-entered per request). Seed the mpr.* section from GX10_MPR_* env ONCE into
        # the live tree (the engine core has no mpr awareness, so without this the env knobs are dead);
        # afterwards `/config set` mutations to _EFFECTIVE_CFG['mpr'] persist and override (env < runtime-set).
        tree = getattr(gx10, "_EFFECTIVE_CFG", None)
        if isinstance(tree, dict) and "mpr" not in tree:
            _apply_mpr_env(tree)
        cfg = load_mpr_config(tree if isinstance(tree, dict) else {})
        d.runs_dir, d.audit_level, d.panel_mode = cfg.runs_dir, cfg.audit_level, cfg.panel_mode
        d.language = getattr(gx10, "LANGUAGE", "en") or "en"   # #18-4c: role lens_prompts localize to this
        # B3 (STATE layout): MPR runs are initiative artifacts → route them under the ACTIVE initiative
        # (vault/<slug>/runs) instead of the WORKDIR root. Without an active initiative the config
        # default stays; mpr_research_run then gates the run fail-closed anyway, before anything is created.
        # getattr (fail-soft like the other bindings) — a minimal/old engine stub may not have the fn.
        _arootsoft = getattr(gx10, "artifact_root_soft", None)
        _vp = _arootsoft() if callable(_arootsoft) else None
        if _vp is not None:
            d.runs_dir = (_vp / "runs").as_posix()
        d.enabled = cfg.enabled                       # runtime active-gate (default off; /config set mpr.enabled on)
        d.pool = cfg.providers.pool
        d.routing = cfg.providers.routing.model_dump()
        d.default_offload = cfg.providers.default_offload
        d.sovereignty = cfg.sovereignty.model_dump()
        d.reducer = getattr(gx10, "_reduce_worker_results", None)
        d.writer = getattr(gx10, "_atomic_write", None)
        d.store = _resolve_store(gx10)
        # #52: only index the run in the TaskStore when the #15 contract allows the task pipeline in the
        # ACTIVE initiative — i.e. NOT in a reasoning-only mpr initiative (there the gate would refuse
        # create and task_id would silently stay None). #15 is the single source of truth; fail-soft → True.
        _blk = getattr(gx10, "_mpr_blocks_tasks", None)
        d.index_runs = not (callable(_blk) and _blk())
        d.registry = get_registry(_MPR_ROOT)
        workers = getattr(gx10, "_WORKERS", None)
        if workers is not None and getattr(workers, "client", None) is not None:
            client, model = workers.client, getattr(workers, "model", None)
            d.llm = _ClassifierAdapter(client, model)
            d.fanout = lambda prompts, *, system, max_tokens, think=True: workers.fanout(
                prompts, system=system, max_tokens=max_tokens, think=think)
            # think=False: synthesis emits a STRUCTURED decision-matrix/evidence JSON block (validated
            # against the template), not free reasoning — same structured-emission class as the classifier
            # and panel "direct" mode. With thinking ON, qwen3 writes a <think> block first → extract_json
            # fails / the budget is spent before the JSON closes → template-parse degrade (Live-Bug #4).
            d.synth_llm = lambda p, *, system, max_tokens: (
                workers.fanout([p], system=system, max_tokens=max_tokens, think=False)[0].get("content") or "")
        # P0 provider-router (real routing/offload) — OPT-IN, default OFF (LOK-8). The dispatch path does
        # NOT get MPR's LB-4 fixes (think=False + flat panel budget): it calls fanout with think=True +
        # est_max_tokens, which live produced EMPTY perspectives / 0 tokens in desktop mode. The dispatcher
        # is the deferred #2 (external agents) and is unverified for panel routing, so until it is verified
        # MPR stays on the in-engine fanout (the LB-4..7-verified path). Re-enable explicitly with
        # GX10_MPR_USE_DISPATCHER=1. No core edit — MPR only consumes it.
        dispatcher = getattr(gx10, "_DISPATCHER", None)
        if (dispatcher is not None and getattr(dispatcher, "active", lambda: False)()
                and os.environ.get("GX10_MPR_USE_DISPATCHER") == "1"):
            d.dispatcher = dispatcher
    except Exception:  # noqa: BLE001
        pass
    return d


def mpr_research_run(query: str, *, route_hint: str = "", domain_hint: str = "", mode_hint: str = "",
                     files: Optional[List[str]] = None, audit_level: str = "") -> str:
    """The public ``run`` (§4 signature → drives the tool schema via derive_tool_schema). Sync, never
    raises. Binds the engine handles and delegates to run_mpr."""
    # B3 fail-closed: an MPR run creates artifacts (runs/<id>/…) → requires an active initiative,
    # otherwise it would write into the project root. A clear note instead of writing to the root.
    _gx = None
    try:
        import gx10 as _gx  # type: ignore
        if _gx.artifact_root_soft() is None:
            return ("ERROR: mpr_research: kein aktives Initiative — Artefakte hätten kein Zuhause. "
                    "`/initiative new <name> --type mpr` (oder `--type software`) zuerst.")
    except Exception:  # noqa: BLE001 — no engine context (standalone) → normal run
        _gx = None
    out = run_mpr(query, route_hint=route_hint, domain_hint=domain_hint, mode_hint=mode_hint,
                  files=files, audit_level=audit_level, deps=_engine_deps())
    # C2: after a real run, keep the active initiative's INDEX.md fresh (fail-soft, index only;
    # not on ERROR/decline/disabled — no artifact was created there).
    if _gx is not None and out and not out.startswith(("ERROR", "MPR declined", "MPR ist deaktiviert")):
        try:
            _slug = _gx.active_slug()
            if _slug:
                _gx.reconcile_vault(_slug, links=False)
        except Exception:  # noqa: BLE001
            pass
    return out


def _render_lens(role: str, lens_prompt: str, query: str, lang: str = "en") -> dict:
    user = f"{lens_prompt}\n\n{i18n.label('question', lang)}: {query}"
    return {"system": None, "user": user}


def _execute(prompts, perspectives, choices, deps) -> List[dict]:
    """Run the panel: prefer the P0 ProviderDispatcher (real routing/offload) when bound+active, else the
    in-engine fanout MVP. Returns EXACTLY len(prompts) result dicts (1:1). DispatchResult rows additionally
    carry provenance keys (provider_id/provider_kind/model/real_cost_usd/spilled/route_reason); fanout rows
    do not (→ _write_audit records those as in-engine). The P0 path is fail-soft: any import/build fault
    falls back to fanout, and ``dispatch()`` itself never raises (dispatch.py). A raising fanout propagates
    to run_mpr's dispatch try/except (→ ERROR string) — preserving the §4 fail-soft contract."""
    n = len(prompts)
    results = None
    if deps.dispatcher is not None:
        try:
            # P0 engine types (NOT an own dispatcher — guard-allowed). Lazy + fail-soft: resolves to
            # engine/* which is on sys.path because MPR runs co-resident with the engine (import gx10
            # in _engine_deps succeeded to bind the dispatcher). An ImportError here → in-engine fallback.
            from router import RouteRequest
            from dispatch import DispatchPolicy
            reqs = [RouteRequest(index=i, effort=(ch.effort or "medium"), provider_policy=ch.policy,
                                 sensitivity=("sensitive" if ch.policy == "local-only" else "internal"))
                    for i, ch in enumerate(choices)]
            results = list(deps.dispatcher.dispatch(prompts, None, DispatchPolicy(reqs, system=None)))
        except Exception:  # noqa: BLE001 — P0 unavailable/build fault → in-engine fallback
            results = None
    if results is None:
        # panel mode (deps.panel_mode): "deep" → thinking-on + per-effort token budgets (deeper, the
        # governor throttles concurrency); else "direct" → thinking-OFF + a flat budget so each lens
        # writes a substantive analysis WITHOUT <think> eating the cap (the stable default — the live
        # 2048+thinking combo produced empty content → degraded synthesis).
        deep = getattr(deps, "panel_mode", "direct") == "deep"
        budget = (max((EFFORT_MAX_TOKENS.get(ch.effort, _PANEL_DIRECT_TOKENS) for ch in choices),
                      default=_PANEL_DIRECT_TOKENS) if deep else _PANEL_DIRECT_TOKENS)
        results = deps.fanout(prompts, system=None, max_tokens=budget, think=deep) if deps.fanout else \
            [{"ok": True, "content": "", "error": None, "completion_tokens": None, "latency": None}
             for _ in prompts]
    if len(results) != n:                            # length guard for ANY executor — never zip-truncate (MED-4)
        results = [results[i] if i < len(results) else
                   {"ok": False, "content": None, "error": "missing execution result",
                    "completion_tokens": None, "latency": None} for i in range(n)]
    # a non-dict row (a buggy/custom dispatcher; the P0 contract only emits dict rows) must degrade
    # cleanly at the dispatch stage, not crash the synthesis loop on r.get(...).
    return [r if isinstance(r, dict) else
            {"ok": False, "content": None, "error": "malformed execution result",
             "completion_tokens": None, "latency": None} for r in results]


def run_mpr(query: str, *, route_hint: str = "", domain_hint: str = "", mode_hint: str = "",
            files: Optional[List[str]] = None, audit_level: str = "", deps: Deps) -> str:
    """The orchestration (§4). Returns a string ALWAYS; never raises (§4 fail-soft)."""
    if not (query or "").strip():
        return "ERROR: mpr_research: 'query' darf nicht leer sein."

    # Runtime active-gate (§ feature flag): the plugin is LOADED (GX10_MPR) but can be paused live. Off →
    # a clear, sentinel-free note (the model answers directly). The load-time A/B gate (GX10_MPR off → no
    # tool at all = byte-identical) is separate; this is the in-session toggle via `/config set mpr.enabled`.
    if not deps.enabled:
        return "MPR ist deaktiviert — aktivieren mit:  /config set mpr.enabled on"

    # 1. classify (router, layer-2 gate) ----------------------------------------------------------
    try:
        inp = RouterInput(
            query=query,
            route_hint=route_hint if route_hint in ("wide", "focused", "file-only", "file-augmented")
            else None,
            domain_hint=(domain_hint or None), mode_hint=(mode_hint or None),  # advisory seeds (§4.2)
            files=[{"path": p} for p in (files or [])],
        )
        decision = classify(inp, llm=deps.llm, registry=deps.registry)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: mpr_research: router: {exc!r}"

    if decision.decision.value == "decline":
        # direct-answer signal — no fan-out, no report (a slim decline note, no sentinels).
        return (f"MPR declined ({decision.decline_reason}): "
                f"{i18n.localized('please answer directly.', deps.language, 'messages', 'decline')}")

    # audit_level is exposed as a tool param (Spec 10 §6), so the model can fill it — and it does so
    # WRONGLY (e.g. "high", confusing it with effort). An unrecognised value would silently fall through
    # the `== "full-per-perspective"` gate → no synthesis.md / perspective files written (the sovereignty
    # proof lost). Validate against the allowlist like route_hint; anything else → the configured default.
    level = audit_level if audit_level in _VALID_AUDIT_LEVELS else deps.audit_level
    run_id = deps.run_id or audit.new_run_id()
    run_dir = Path(deps.runs_dir) / run_id

    # 2. dispatch each perspective (sovereignty chokepoint → in-engine fanout MVP) -----------------
    try:
        perspectives = decision.perspectives
        # #18-4c: localize each role's lens_prompt to the active language (en source in panels;
        # de/… from locales/<lang>.json; missing → English fallback, so a run never breaks).
        rendered = [_render_lens(p.role,
                                 i18n.role_lens(decision.domain or "", p.role, p.lens_prompt, deps.language),
                                 query, deps.language)
                    for p in perspectives]
        choices = []
        for p in perspectives:
            try:
                ch = plan_perspective_dispatch(
                    role=p.role, role_policy=p.provider_policy.value, effort=p.effort.value,
                    evidence_source=(decision.evidence_source.value if decision.evidence_source else None),
                    reads_repo_context=False, pool=deps.pool, routing=deps.routing,
                    default_offload=deps.default_offload, operator_permission=deps.operator_permission,
                    default_policy=(deps.sovereignty.get("default_policy", "offloadable")),
                    internal_is_local_only=deps.sovereignty.get("internal_is_local_only", True),
                    fail_closed=deps.sovereignty.get("fail_closed", True),
                )
            except Exception:  # noqa: BLE001 — sovereignty violation/route fault → keep this lens local
                from .sovereignty import ProviderChoice
                ch = ProviderChoice(provider="spark-vllm", policy="local-only", permission="plan",
                                    effort=p.effort.value)
            choices.append(ch)
        prompts = [r["user"] for r in rendered]
        results = _execute(prompts, perspectives, choices, deps)   # P0 dispatch when active, else in-engine fanout
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: mpr_research: dispatch: {exc!r}"

    # 3. synthesize -------------------------------------------------------------------------------
    try:
        presults = [
            PerspectiveResult(
                role=p.role, ok=bool(r.get("ok")), content=r.get("content"), error=r.get("error"),
                provider=(r.get("provider_id") or ch.provider),   # the executed provider (P0) or the plan
                effort=ch.effort, provider_policy=ch.policy,
                completion_tokens=r.get("completion_tokens"), latency=r.get("latency"),
            )
            for p, r, ch in zip(perspectives, results, choices)
        ]
        synth_inp = SynthesisInput(
            run_id=run_id, query=query, mode=(decision.mode.value if decision.mode else "decision"),
            synthesis_template=(decision.synthesis_template or "decision-matrix"),
            domain=decision.domain or "adhoc",
            evidence_source=(decision.evidence_source.value if decision.evidence_source else "mixed"),
            perspectives=presults,
        )
        out = synthesize(synth_inp, llm_call=deps.synth_llm, lang=deps.language) if deps.synth_llm else None
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: mpr_research: synthesis: {exc!r}"
    if out is None:
        return "ERROR: mpr_research: synthesis: no synthesis client"

    # 4. audit (verbindlich) + 5. memory (best-effort) — never sink the run ------------------------
    try:
        _write_audit(run_dir, run_id, query, decision, perspectives, choices, rendered, results, out,
                     level, deps)
    except Exception:  # noqa: BLE001 — audit write failure must not sink the answer (§8)
        pass
    try:
        if deps.reducer is not None:
            write_back(out, synth_inp, deps.reducer)
    except Exception:  # noqa: BLE001
        pass

    # 6. return the report between sentinels (§6.1) ----------------------------------------------
    return _wrap(out.body)


def _write_audit(run_dir, run_id, query, decision, perspectives, choices, rendered, results, out,
                 level, deps):
    if deps.writer is None:
        return  # no file substrate injected (e.g. monolith CLI) → skip file-first audit
    entries = []
    metas = []
    ev = decision.evidence_source.value if decision.evidence_source else "internal"
    for i, (p, ch, rend, res) in enumerate(zip(perspectives, choices, rendered, results), 1):
        # The manifest is the sovereignty PROOF, so it records REAL execution (§2.6). A P0 DispatchResult
        # carries the executed provenance (provider_id/provider_kind/model/real_cost_usd/spilled/route_
        # reason); the in-engine fanout fallback carries none → the `.get(...) or default` resolves to
        # spark-vllm / in-engine / 0.0 (byte-identical to the prior MVP record). ch.policy stays the
        # recorded sovereignty classification (what the lens is ALLOWED), independent of where it ran.
        kind = res.get("provider_kind")
        exec_provider = res.get("provider_id") or "spark-vllm"
        exec_substrate = "in-engine" if kind in (None, "in-engine") else "pc-cli"
        persp = {"role": p.role, "lens_prompt": p.lens_prompt, "effort": ch.effort,
                 "provider": exec_provider, "model": res.get("model"), "substrate": exec_substrate,
                 "provider_policy": ch.policy, "rendered": rend, "context_sources": [],
                 "max_tokens": None, "cost": {"amount": float(res.get("real_cost_usd") or 0.0)},
                 "spilled": bool(res.get("spilled", False)), "route_reason": res.get("route_reason")}
        entries.append(audit.record_perspective(run_dir, i, persp, res, level, writer=deps.writer))
        metas.append({"index": i, "provider": exec_provider,
                      "substrate": exec_substrate, "provider_policy": ch.policy,
                      "data_classification": ("sensitive" if ch.policy == "local-only"
                                              else "public" if ev == "external" else "internal"),
                      "payload": audit.canonical_prompt(rend)})
    # local allowlist = the pool's local-only providers PLUS the in-engine execution identities (incl. P0's
    # 'spark-fallback' spill-back) — anything that ran in-engine on the local Spark genuinely never egressed.
    local_ids = {pid for pid, s in (deps.pool or {}).items() if (s or {}).get("policy_class") == "local-only"}
    local_ids |= {"spark-vllm", "spark-fallback"}
    prov = audit.build_provenance(metas, local_providers=local_ids)
    status = audit.compute_status(entries, prov, declined=False)
    if level == "full-per-perspective":
        audit.write_synthesis(run_dir, out.body, run_id=run_id, template=out.template, writer=deps.writer)
    # BUG-6/LOK-10: the manifest is the proof — aggregate REAL metrics + synthesis refs from the executed
    # perspectives. Without this, Metrics()/SynthesisBlock() stay at their zero defaults (n_ok=0,
    # total_completion_tokens=0, input=[]) even though the panel ran with real content + tokens.
    ok_entries = [e for e in entries if e.ok]
    metrics = audit.Metrics(
        n_perspectives=len(entries),
        n_ok=len(ok_entries),
        total_completion_tokens=sum(int(e.tokens.completion or 0) for e in entries),
    )
    synth_block = audit.SynthesisBlock(
        input=[audit.SynthInputRef(index=e.index, role=e.role, prompt_hash=e.prompt_hash) for e in ok_entries],
        output=(out.body or None),
    )
    manifest = audit.Manifest(
        run_id=run_id, created_at=audit.now_iso(), status=status, audit_level=level,
        query=audit.Query(text=query),
        router_decision=audit.RouterDecisionSnapshot(
            decision="run", route=(decision.route.value if decision.route else None),
            domain=decision.domain, mode=(decision.mode.value if decision.mode else None),
            synthesis_template=decision.synthesis_template,
            evidence_source=(decision.evidence_source.value if decision.evidence_source else None)),
        perspectives=entries, provenance=prov, synthesis=synth_block, metrics=metrics, final_answer=out.body,
        sovereignty_summary=audit.SovereigntySummary(
            local_only_count=sum(1 for c in choices if c.policy == "local-only"),
            # offloaded_count = REAL egress (substrate-based): 0 on the in-engine fanout fallback, and the
            # true count of externally-executed lenses once P0 dispatch routes offloadable ones out.
            offloaded_count=sum(1 for m in metas if m["substrate"] != "in-engine"),
            external_egress_providers=sorted({e.provider for e in prov.egress}),
            violations=len(prov.violations)),
    )
    audit.write_manifest(run_dir, manifest, writer=deps.writer)
    # #52: skip TaskStore indexing when #15 blocks the pipeline (mpr initiative) — don't attempt create()
    # only to have the gate raise + get swallowed; the run's record is runs/<id>/manifest.json + INDEX.md.
    if deps.store is not None and deps.index_runs:
        tid = audit.index_in_taskstore(run_id, manifest.query.text, manifest.router_decision.domain or "",
                                       status, store=deps.store)
        if tid:
            manifest.task_id = tid
            audit.write_manifest(run_dir, manifest, writer=deps.writer)  # idempotent index backfill
