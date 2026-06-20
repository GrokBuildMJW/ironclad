"""Audit identity + run-manifest SSOT (Spec 07 §2 + §3).

The manifest is THE truth (file-first, lossless, replayable); Memory is a lossy projection of it
(Audit ≠ Memory, §1). This module holds the MPR-own identity helpers (stdlib hashlib/uuid/time — no
core symbol) and the pydantic-v2 manifest schema. Recording/writing/indexing/retention build on this in
later units; here it is the typed contract + the hash/id helpers that make replay byte-stable.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Writer port — mirrors ironclad's ``_atomic_write(path, text)`` (gx10.py:939). Injected so audit.py
#: stays engine-free + tmp_path-testable; run() (1f) binds the real atomic writer.
Writer = Callable[[Path, str], None]

#: A perspective ran locally (no egress) iff its substrate is in-engine (parallel_reason/fanout)
#: AND its provider is a known local backend (the substrate tag alone is not trusted — §8 couples
#: provider↔substrate). Config-overridable later (mpr.local_providers).
_LOCAL_SUBSTRATE = "in-engine"
LOCAL_PROVIDERS = frozenset({"spark-vllm"})


def _as_text(payload: Any) -> str:
    """Coerce a payload to text for hashing/byte-count — never crash, and keep falsy non-None
    payloads distinct from a missing one (0/False ≠ '')."""
    if payload is None:
        return ""
    return payload if isinstance(payload, str) else str(payload)


# ── §2 identity ───────────────────────────────────────────────────────────────────────────────────
def new_run_id() -> str:
    """Sortable, collision-free run id: ``mpr-YYYYMMDDThhmmssZ-<8hex>`` (ISO-UTC prefix → lexical=chrono)."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"mpr-{ts}-{uuid.uuid4().hex[:8]}"


def now_iso() -> str:
    """ISO-UTC timestamp, same convention as TaskStore._now_iso() (gx10.py)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_prompt(rendered: Dict[str, Optional[str]]) -> str:
    """Canonical render form of a perspective for prompt_hash + replay byte-equality (§2).

    Exactly what the engine builds as messages: ``system + "\\n\\n" + user`` (workers.py:72-78).
    ``rendered`` = ``{"system": str|None, "user": str}``. Missing/empty system ⇒ user only (no blank lead).
    """
    sys_, usr = (rendered.get("system") or ""), (rendered.get("user") or "")
    return (sys_ + "\n\n" + usr) if sys_ else usr


def prompt_hash(text: str) -> str:
    """Stable content fingerprint of a (canonically rendered) prompt → replay key.

    sha256 over UTF-8 bytes, truncated to 16 hex (short, collision-rare). Feed ALWAYS via
    ``canonical_prompt(...)``, never raw, else the hash drifts against replay.
    """
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def content_hash(text: str) -> str:
    """sha256 fingerprint (16 hex) of arbitrary content — egress payloads, file refs, case-specs."""
    return "sha256:" + hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


# ── §3 run-manifest schema (pydantic-v2 SSOT) ─────────────────────────────────────────────────────
class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    route_hint: Optional[str] = None
    attached_files: List[str] = Field(default_factory=list)  # paths only, no content


class RouterDecisionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str                                   # run | decline
    decline_reason: Optional[str] = None
    route: Optional[str] = None
    domain: Optional[str] = None
    mode: Optional[str] = None
    synthesis_template: Optional[str] = None
    evidence_source: Optional[str] = None
    router_version: str = "1"
    guards: Dict[str, bool] = Field(default_factory=dict)


class ContextSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str                                       # rag | file | …
    ref: str
    query: Optional[str] = None
    n_hits: Optional[int] = None
    sha256: Optional[str] = None


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: Optional[int] = None
    completion: Optional[int] = None
    prompt_estimated: bool = True                   # fanout never passes prompt_tokens → always chars/4


class Cost(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = "USD"
    amount: float = 0.0
    estimated: bool = True


class PerspectiveEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int                                      # 1-based, = fanout input order
    role: str
    lens_prompt: str
    effort: str = "medium"
    provider: str
    model: Optional[str] = None
    substrate: str                                  # in-engine | pc-cli
    provider_policy: str                            # local-only | offloadable
    spilled: bool = False                           # P0 DispatchResult: retried locally after a failed offload
    route_reason: Optional[str] = None              # P0 DispatchResult: routing/spill reason (§2.6)
    prompt_hash: str
    context_sources: List[ContextSource] = Field(default_factory=list)
    max_tokens: Optional[int] = None
    tokens: Tokens = Field(default_factory=Tokens)
    latency_s: Optional[float] = None
    cost: Cost = Field(default_factory=Cost)
    ok: bool
    error: Optional[str] = None
    artifact: Optional[str] = None                  # perspective_NN.md or null


class EgressEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    perspective_index: int
    provider: str
    model: Optional[str] = None
    data_classification: str                        # public | internal | sensitive
    payload_hash: str                               # hash of what left — never plaintext
    bytes_out: int
    policy_allowed: bool


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sovereignty_ok: bool
    egress: List[EgressEntry] = Field(default_factory=list)
    violations: List[dict] = Field(default_factory=list)  # non-empty ⇒ status=error (§4)


class SynthInputRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    role: str
    prompt_hash: str


class ConflictPosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    stance: str


class ConflictRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    axis: str
    positions: List[ConflictPosition] = Field(default_factory=list)


class SynthesisBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: List[SynthInputRef] = Field(default_factory=list)
    conflicts: List[ConflictRef] = Field(default_factory=list)
    output: Optional[str] = None                    # synthesis.md or null
    synthesis_provider: Optional[str] = None
    synthesis_model: Optional[str] = None
    synthesis_tokens: Tokens = Field(default_factory=Tokens)
    synthesis_latency_s: Optional[float] = None


class RenderedPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    system: Optional[str] = None
    user: str
    prompt_hash: str


class RegistrySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: str
    roles: List[str] = Field(default_factory=list)
    case_spec_hash: Optional[str] = None


class Inputs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seed: str = "mpr-seed-1"
    rendered_prompts: List[RenderedPrompt] = Field(default_factory=list)
    registry_snapshot: Optional[RegistrySnapshot] = None


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_perspectives: int = 0
    n_ok: int = 0
    total_completion_tokens: int = 0
    wallclock_s: float = 0.0
    total_cost: Cost = Field(default_factory=Cost)
    n_written: int = 0                              # reducer-written items (0 = legit, §6)


class SecurityBlock(BaseModel):
    """Security context of the run (Spec 09 §7)."""
    model_config = ConfigDict(extra="forbid")
    profile: str                                    # open | token | sealed
    code_locality: str                              # local | mount
    permission_mode_effective: str                  # rendered (read-only) offload permission


class BudgetSummary(BaseModel):
    """Run budget snapshot (Spec 09 §7)."""
    model_config = ConfigDict(extra="forbid")
    max_cost_usd_per_run: float = 0.0
    max_tokens_per_run: int = 0
    spent_cost_usd: float = 0.0
    spent_tokens: int = 0
    per_provider_spent: Dict[str, dict] = Field(default_factory=dict)
    actions: List[dict] = Field(default_factory=list)   # degrade/truncate/abort actions
    cost_estimated: bool = True


class SovereigntySummary(BaseModel):
    """Machine-checkable per-run sovereignty proof (Spec 09 §7); ``violations`` must be 0 on success."""
    model_config = ConfigDict(extra="forbid")
    local_only_count: int = 0
    offloaded_count: int = 0
    external_egress_providers: List[str] = Field(default_factory=list)
    violations: int = 0


class Manifest(BaseModel):
    """The run manifest — the lossless truth + sovereignty/provenance proof (§3)."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1"
    run_id: str
    created_at: str
    finished_at: Optional[str] = None
    task_id: Optional[str] = None                   # TaskStore index (§5); null when audit off
    status: str = "ok"                              # ok | partial | declined | error
    audit_level: str = "manifest-only"              # manifest-only | full-per-perspective (§7)
    query: Query
    router_decision: RouterDecisionSnapshot
    perspectives: List[PerspectiveEntry] = Field(default_factory=list)
    provenance: Provenance
    synthesis: SynthesisBlock = Field(default_factory=SynthesisBlock)
    final_answer: str = ""
    inputs: Inputs = Field(default_factory=Inputs)
    metrics: Metrics = Field(default_factory=Metrics)
    # --- Spec 09 §7 config/security/sovereignty (optional; filled by run()/1f) ---
    security: Optional[SecurityBlock] = None
    budget: Optional[BudgetSummary] = None
    sovereignty_summary: Optional[SovereigntySummary] = None


# ── §8 perspective recorder (substrate-agnostic mirror) ──────────────────────────────────────────
def _perspective_md(run_id: str, idx: int, role: str, provider: str, model: Optional[str],
                    ph: str, content: str) -> str:
    header = (f"<!-- run:{run_id} index:{idx} role:{role} provider:{provider} "
              f"model:{model} prompt_hash:{ph} -->\n\n")
    return header + content


def record_perspective(run_dir: Path, idx: int, persp: Dict[str, Any], result: Dict[str, Any],
                       audit_level: str, *, writer: Writer) -> PerspectiveEntry:
    """Normalise ONE perspective result (in-engine OR pc-cli — same {ok,content,error,
    completion_tokens,latency} dict) into a PerspectiveEntry, and on ``full-per-perspective`` write the
    raw ``perspective_NN.md`` via the injected *writer*. Input-order preserved via *idx* (§8)."""
    rendered = canonical_prompt(persp.get("rendered") or {})
    ph = prompt_hash(rendered)
    est_prompt = max(1, len(rendered) // 4)   # fanout never passes prompt_tokens → always chars/4 (§3)
    content = result.get("content")
    artifact: Optional[str] = None
    if audit_level == "full-per-perspective" and content:
        fn = f"perspective_{idx:02d}.md"
        writer(run_dir / fn, _perspective_md(run_dir.name, idx, persp.get("role", ""),
                                             persp.get("provider", ""), persp.get("model"), ph, content))
        artifact = fn
    return PerspectiveEntry(
        index=idx, role=persp["role"], lens_prompt=persp.get("lens_prompt", ""),
        effort=persp.get("effort", "medium"), provider=persp["provider"], model=persp.get("model"),
        substrate=persp.get("substrate", _LOCAL_SUBSTRATE),
        provider_policy=persp.get("provider_policy", "offloadable"),
        spilled=bool(persp.get("spilled", False)), route_reason=persp.get("route_reason"),
        prompt_hash=ph, context_sources=persp.get("context_sources") or [],
        max_tokens=persp.get("max_tokens"),
        tokens=Tokens(prompt=est_prompt, completion=result.get("completion_tokens"), prompt_estimated=True),
        latency_s=result.get("latency"), cost=Cost(**(persp.get("cost") or {})),
        ok=bool(result.get("ok")), error=result.get("error"), artifact=artifact,
    )


# ── §4 provenance (sovereignty proof, fail-closed) ───────────────────────────────────────────────
def build_provenance(metas: List[Dict[str, Any]], *,
                     local_providers: Optional[set] = None) -> Provenance:
    """Compute the sovereignty proof from per-perspective dispatch metas — fail-closed.

    Truly local = ``substrate=in-engine`` AND provider in the local allowlist → no egress. The
    substrate tag alone is NOT trusted: a ``substrate=in-engine`` claim with a non-local provider is a
    **tag-mismatch violation** AND still recorded as egress (data went to a non-local backend). A
    non-local perspective is an egress: ``policy_allowed = (provider_policy == offloadable)``; a
    ``local-only`` perspective that went external is a VIOLATION (never an allowed egress). The raw
    payload is hashed, never stored. ``sovereignty_ok == (violations == [])``.
    """
    local = set(local_providers) if local_providers is not None else LOCAL_PROVIDERS
    egress: List[EgressEntry] = []
    violations: List[dict] = []
    for m in metas:
        provider = m.get("provider", "?")
        claims_local = m.get("substrate") == _LOCAL_SUBSTRATE
        if claims_local and provider in local:
            continue  # genuinely local → no data left the box
        if claims_local and provider not in local:
            violations.append({
                "perspective_index": m["index"], "provider": provider,
                "provider_policy": m.get("provider_policy"),
                "reason": "substrate=in-engine but provider is not a known local backend (tag mismatch)",
            })
        payload = _as_text(m.get("payload"))
        allowed = m.get("provider_policy") == "offloadable"
        egress.append(EgressEntry(
            perspective_index=m["index"], provider=provider, model=m.get("model"),
            data_classification=m.get("data_classification", "internal"),
            payload_hash=content_hash(payload), bytes_out=len(payload.encode("utf-8")),
            policy_allowed=allowed,
        ))
        if not allowed:
            violations.append({
                "perspective_index": m["index"], "provider": provider,
                "provider_policy": m.get("provider_policy"),
                "reason": "local-only perspective dispatched externally",
            })
    return Provenance(sovereignty_ok=(not violations), egress=egress, violations=violations)


def compute_status(perspectives: List[PerspectiveEntry], provenance: Provenance, *,
                   declined: bool = False, write_error: bool = False) -> str:
    """Run status (§4): error on a sovereignty violation OR a write/infra error; declined; partial when
    a perspective failed but synthesis ran; else ok."""
    if write_error or provenance.violations:
        return "error"
    if declined:
        return "declined"
    if any(not p.ok for p in perspectives):
        return "partial"
    return "ok"


# ── §5.1 file-first writers (run-dir layout) ─────────────────────────────────────────────────────
def write_synthesis(run_dir: Path, text: str, *, run_id: str, template: str,
                    conflicts: Optional[List[str]] = None, writer: Writer) -> str:
    """Write ``synthesis.md`` (raw synthesis text). Returns the filename."""
    cz = (", ".join(conflicts) if conflicts else "—")
    header = f"<!-- run:{run_id} template:{template} conflicts:{cz} -->\n\n"
    writer(run_dir / "synthesis.md", header + (text or ""))
    return "synthesis.md"


def write_manifest(run_dir: Path, manifest: Manifest, *, writer: Writer) -> str:
    """Write ``manifest.json`` (the commit point, §5.1). Returns the filename."""
    writer(run_dir / "manifest.json", manifest.model_dump_json(indent=2))
    return "manifest.json"


# ── §5.2 TaskStore index (MPR run = task) ─────────────────────────────────────────────────────────
def index_in_taskstore(run_id: str, query_text: str, domain: str, status: str, *,
                       store: Any) -> Optional[str]:
    """Register the MPR run as a TaskStore entry (force=True — runs are never duplicates, §5.2) and
    transition it to ``done`` (a reasoning run is finished at index time). Returns the task_id (backfilled
    into the manifest). ``store is None`` → None (audit off / no store); fail-soft, never raises."""
    if store is None:
        return None
    fields = {
        "type": "mpr-run", "priority": "normal",
        "title": f"MPR: {query_text[:80]}", "description": domain or "reasoning",
        "mpr_run_id": run_id, "manifest_path": f"runs/{run_id}/manifest.json", "mpr_status": status,
    }
    try:
        task = store.create(fields, force=True)   # force: distinct audit events, never Jaccard-deduped
        tid = task.get("id")
        if tid:
            store.transition(tid, "done")         # archive, not work-queue (project_active skips done)
        return tid
    except Exception:  # noqa: BLE001 — indexing is best-effort; the file-first trail is the truth
        return None


# ── §6 memory mirror (only distilled insight, via the single-writer reducer) ──────────────────────
def mirror_to_memory(insight: str, domain: str, query_text: str, *, reducer: Any) -> int:
    """Hand ONE distilled insight to ironclad's single-writer reducer (``_reduce_worker_results``).

    NEVER raw perspectives/prompts/provenance — only the synthesis essence (§6). Returns ``n_written``
    for ``metrics.n_written``; ``reducer is None`` (or globally flag-gated off) → 0 (legit, not error).
    """
    if reducer is None or not (insight or "").strip():
        return 0
    items = [{"ok": True, "content": insight}]   # exactly one consolidated entry
    try:
        n = reducer(items, topic=f"MPR/{domain}: {query_text[:120]}")
        return int(n) if isinstance(n, int) else 0
    except Exception:  # noqa: BLE001 — memory write-back is fail-soft (§6)
        return 0


# ── §9 retention (prune run-dirs + their TaskStore entry) ─────────────────────────────────────────
def _iso_minus_days(now_s: str, days: int) -> str:
    dt = datetime.strptime(now_s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (dt - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_runs(runs_root: Any, *, keep_runs: int = 500, keep_days: Optional[int] = 90,
               protect_violations: bool = True, store_delete: Optional[Callable[[str], None]] = None,
               now: Optional[str] = None) -> List[str]:
    """Prune old run-dirs (+ their TaskStore entry via injected ``store_delete``). Idempotent + fail-soft.

    LRU by run_id prefix (= time) past ``keep_runs``; also drop dirs older than ``keep_days`` (by
    ``created_at``). ``protect_violations`` keeps any run with ``provenance.violations != []`` forever
    (proof obligation, §9). Returns the deleted run_ids. TaskStore has no delete → ``store_delete`` is
    injected (run()/1f binds a locked delete or a named TaskStore.delete core-change).
    """
    root = Path(runs_root)
    if not root.is_dir():
        return []
    dirs = sorted(d for d in root.iterdir() if d.is_dir() and d.name.startswith("mpr-"))
    infos: List[dict] = []
    for d in dirs:
        created_at, violated, task_id = None, False, None
        mf = d / "manifest.json"
        if mf.is_file():
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
                created_at = m.get("created_at")
                violated = bool((m.get("provenance") or {}).get("violations"))
                task_id = m.get("task_id")
            except Exception:  # noqa: BLE001 — a broken manifest never aborts the prune
                pass
        infos.append({"dir": d, "name": d.name, "created_at": created_at,
                      "violated": violated, "task_id": task_id})

    deletable = [i for i in infos if not (protect_violations and i["violated"])]
    to_delete: set = set()
    if keep_days is not None:
        cutoff = _iso_minus_days(now or now_iso(), keep_days)
        for i in deletable:
            if i["created_at"] and i["created_at"] < cutoff:
                to_delete.add(i["name"])
    excess = max(0, len(infos) - keep_runs)   # keep_runs caps total; protected ones count but survive
    if excess:
        for i in sorted(deletable, key=lambda x: x["name"])[:excess]:
            to_delete.add(i["name"])

    deleted: List[str] = []
    for i in infos:
        if i["name"] not in to_delete:
            continue
        try:
            shutil.rmtree(i["dir"], ignore_errors=True)
            if store_delete and i["task_id"]:
                try:
                    store_delete(i["task_id"])
                except Exception:  # noqa: BLE001 — store delete is best-effort
                    pass
            deleted.append(i["name"])
        except Exception:  # noqa: BLE001 — per-dir fail-soft
            continue
    return deleted
