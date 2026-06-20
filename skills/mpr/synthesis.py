"""Synthesis stage — the last MPR step (Spec 06 §1/§2/§5/§6/§7). Reasoning-only, no own primitive.

``synthesize(inp, *, llm_call)`` verdichtet die gelabelten Perspektiv-Ergebnisse zu EINEM Urteil über
GENAU EINEN governeten LLM-Call + deterministisches Rendering (Templates, Syn-4) + Cross-Verify
(conflicts, Syn-3). Es **wirft nie** für Modell-/Transport-/Memory-Fehler — es degradiert gestuft (§7).

Reiten, nicht duplizieren: der Degradations-Formatter (`degrade_format`) und der Memory-Reducer
(`write_back(reducer=…)`) werden INJIZIERT — run() (1f) reicht ironclads `_format_parallel`
(gx10.py:1709) bzw. `_reduce_worker_results` (gx10.py:1810) durch; hier sind sie stub-bar und der
Default-Formatter spiegelt `_format_parallel` minimal, ohne den Engine-Import in dieses Modul zu ziehen.
"""
from __future__ import annotations

import math
from typing import Any, Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from .conflicts import Conflict, detect_conflicts
from .templates import validate_template
from .templates._common import conflict_zones_md
from .templates.prompts import build_synthesis_prompt

SYNTH_MAX_TOKENS = 6144        # mpr.synth_max_tokens (§5 budget) — headroom so the structured JSON for a
                              # large panel fits when thinking is off (the whole budget is output, no <think>)
_SYNTH_BASE = 2048
_DISTILL_LIMIT = 1800         # ~1–2k chars, hard <= chunk_size (§6.2)


# ── §1 input/output contract ─────────────────────────────────────────────────────────────────────
class PerspectiveResult(BaseModel):
    role: str
    lens_prompt_hash: str = ""
    ok: bool
    content: Optional[str] = None
    error: Optional[str] = None
    provider: str = "spark-vllm"
    model: Optional[str] = None
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    completion_tokens: Optional[int] = None
    latency: Optional[float] = None
    provider_policy: Literal["local-only", "offloadable"] = "offloadable"


class SynthesisInput(BaseModel):
    run_id: str
    query: str
    mode: Literal["decision", "evidence-research", "comparison"]
    synthesis_template: Literal["decision-matrix", "evidence-report", "comparison-matrix", "risk-register"]
    domain: str
    evidence_source: Literal["internal", "external", "mixed"]
    perspectives: List[PerspectiveResult]
    cross_verify: bool = True
    # router seeds for the conflict detector (§3.1): subjects = options/entities being compared,
    # criteria = dimension-name hints. Kept as plain strings — the final weighted Criterion list is
    # the LLM's DecisionMatrix output (§4.1), deliberately separate.
    subjects: List[str] = Field(default_factory=list)
    criteria: List[str] = Field(default_factory=list)


class SynthesisOutput(BaseModel):
    run_id: str
    mode: str
    template: str
    status: Literal["full", "degraded"]
    body: str
    template_valid: bool
    conflicts: List[Conflict] = Field(default_factory=list)
    used: List[str] = Field(default_factory=list)
    dropped: List[dict] = Field(default_factory=list)


# ── §7 quorum + degradation ────────────────────────────────────────────────────────────────────
def _quorum(ok: List[PerspectiveResult], all_p: List[PerspectiveResult]) -> str:
    n, k = len(all_p), len(ok)
    # k<2 is insufficient FIRST (§7) — even k==n==1 must not become a pseudo-synthesis over one lens.
    if k < 2:
        return "insufficient"
    if k == n:
        return "full"
    floor = max(2, math.ceil(0.5 * n))   # degraded needs >= half AND >= 2; below → insufficient
    return "degraded" if k >= floor else "insufficient"


def _default_degrade_format(results: List[dict]) -> str:
    # minimal mirror of gx10._format_parallel (gx10.py:1709) — run() injects the real one.
    ok = sum(1 for r in results if r.get("ok"))
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {(r.get('content') or '').strip()}" if r.get("ok")
                     else f"[{i}] ERROR: {r.get('error')}")
    return f"[parallel_reason] {ok}/{len(results)} ok\n\n" + "\n\n".join(lines)


def _dropped(failed: List[PerspectiveResult]) -> List[dict]:
    return [{"role": p.role, "reason": p.error or "empty"} for p in failed]


def _degraded_output(inp: SynthesisInput, ok: List[PerspectiveResult],
                     failed: List[PerspectiveResult], *, reason: str,
                     conflicts: Optional[List[Conflict]] = None,
                     degrade_format: Optional[Callable[[List[dict]], str]] = None,
                     raw: Optional[str] = None) -> SynthesisOutput:
    conflicts = conflicts or []
    fmt = degrade_format or _default_degrade_format
    parts: List[str] = []
    if reason == "quorum":
        parts.append("⚠ Zu wenige Perspektiven für eine belastbare Synthese — Einzelsicht unten, "
                     "bitte Frage schärfen oder Run wiederholen.")
        parts += [f"### {p.role}\n{(p.content or '').strip()}" for p in ok]
    else:
        parts.append(f"⚠ Synthese degradiert ({reason}) — Roh-Konsolidierung unten.")
        parts.append(raw if raw is not None
                     else fmt([{"ok": p.ok, "content": p.content, "error": p.error} for p in ok]))
    if failed:
        parts.append("Ausgefallen: " + ", ".join(f"{p.role} ({p.error or 'leer'})" for p in failed))
    # raw (from validate_template) already embeds the conflict zones → don't append them twice.
    if raw is None:
        cz = conflict_zones_md(conflicts)
        if cz:
            parts.append(cz)
    return SynthesisOutput(
        run_id=inp.run_id, mode=inp.mode, template=inp.synthesis_template, status="degraded",
        body="\n\n".join(p for p in parts if p), template_valid=False, conflicts=conflicts,
        used=[p.role for p in ok], dropped=_dropped(failed),
    )


def _synth_budget(n_ok: int) -> int:
    return min(SYNTH_MAX_TOKENS, _SYNTH_BASE + 512 * n_ok)


# ── §2 pipeline ───────────────────────────────────────────────────────────────────────────────────
def synthesize(inp: SynthesisInput, *, llm_call: Callable[..., str],
               degrade_format: Optional[Callable[[List[dict]], str]] = None) -> SynthesisOutput:
    """Deterministic synthesis around ONE ``llm_call(prompt, *, system, max_tokens) -> str``.

    Never raises for model/transport errors — degrades stepwise (§7): quorum gate → cross-verify →
    one synthesis call → template validate (one repair re-ask) → degraded fallback if anything fails.
    """
    ok = [p for p in inp.perspectives if p.ok and (p.content or "").strip()]
    failed = [p for p in inp.perspectives if not (p.ok and (p.content or "").strip())]

    status = _quorum(ok, inp.perspectives)
    if status == "insufficient":
        return _degraded_output(inp, ok, failed, reason="quorum", degrade_format=degrade_format)

    conflicts: List[Conflict] = []
    if inp.cross_verify:
        try:
            conflicts = detect_conflicts(ok, subjects=(inp.subjects or None), mode=inp.mode,
                                         query=inp.query)
        except Exception:  # noqa: BLE001 — conflicts are optional; never sink the stage (§7)
            conflicts = []

    prompt, system = build_synthesis_prompt(inp, ok, conflicts)
    budget = _synth_budget(len(ok))
    try:
        body = llm_call(prompt, system=system, max_tokens=budget)
    except Exception as exc:  # noqa: BLE001 — synth call failed → best-effort degrade
        return _degraded_output(inp, ok, failed, reason=f"synth-call: {exc!r}",
                                conflicts=conflicts, degrade_format=degrade_format)

    rendered, valid = validate_template(inp.synthesis_template, body, conflicts)
    if not valid:
        # §4.4 step 2: exactly ONE repair re-ask, then degrade (keeping conflict zones).
        repair = (prompt + "\n\nDeine vorige Ausgabe war NICHT formvalide. Re-emittiere den FULL "
                  "```json-Block exakt nach Schema, behebe nur die Formfehler, ändere sonst nichts.")
        try:
            body2 = llm_call(repair, system=system, max_tokens=budget)
            rendered2, valid2 = validate_template(inp.synthesis_template, body2, conflicts)
        except Exception:  # noqa: BLE001
            return _degraded_output(inp, ok, failed, reason="template-parse",
                                    conflicts=conflicts, degrade_format=degrade_format, raw=rendered)
        if valid2:
            rendered, valid = rendered2, valid2
        else:
            return _degraded_output(inp, ok, failed, reason="template-parse",
                                    conflicts=conflicts, degrade_format=degrade_format, raw=rendered2)

    out_status = "full" if status == "full" else "degraded"
    if out_status == "degraded" and failed:
        rendered = (f"⚠ Panel unvollständig: Rolle(n) {', '.join(p.role for p in failed)} ausgefallen "
                    f"— diese Dimensionen sind nicht abgedeckt.\n\n" + rendered)
    return SynthesisOutput(
        run_id=inp.run_id, mode=inp.mode, template=inp.synthesis_template, status=out_status,
        body=rendered, template_valid=valid, conflicts=conflicts,
        used=[p.role for p in ok], dropped=_dropped(failed),
    )


# ── §6.2 memory write-back (single-writer, dedup) — reducer INJECTED ──────────────────────────────
def _distill(out: SynthesisOutput, *, limit: int = _DISTILL_LIMIT) -> str:
    """Compact insight (recommendation/top-findings + conflict zones), hard-truncated <= limit."""
    keep = [ln for ln in out.body.splitlines()
            if any(m in ln for m in ("Empfehlung", "Rückzugsoption", "Befund", "Konfliktzonen",
                                     "⚠", "- ", "Lücken", "Chancen"))]
    text = "\n".join(keep) if keep else out.body
    return text[:limit]


def write_back(out: SynthesisOutput, inp: SynthesisInput, reducer: Optional[Callable]) -> Any:
    """Hand EXACTLY ONE distilled insight to the injected single-writer reducer (→ run() binds it to
    ``_reduce_worker_results``). No-op-safe: ``reducer is None`` → skip; reducer errors swallowed (§6.2
    fail-soft). Never raises."""
    if reducer is None:
        return None
    entry = [{"ok": True, "content": _distill(out)}]
    try:
        return reducer(entry, topic=f"MPR {inp.mode}: {inp.query[:120]}")
    except Exception:  # noqa: BLE001 — memory write-back is fail-soft
        return None
