"""MPR router (Phase-0 relevance/classification layer) — Spec 04.

``classify(inp, *, llm, registry)`` turns a query + optional hint + attached files into exactly one
validated ``RouterDecision`` (run | decline) via a deterministic pipeline around a SINGLE classifier
LLM call (§3.1). Everything before/after the call is deterministic → snapshot-/replay-testable.

Reiten, nicht duplizieren: the router brings no client, no fan-out, no store. It consumes the schema
(``.schema``) and the registry layer (panels/resolve/guards/synthesis). The ONE LLM call goes through
the injected ``ClassifierLLM`` port. Adhoc panels come straight from that one call's output (NOT a
second ``generate_adhoc_panel`` call — that would break the one-call invariant; §3.1 + §8 "Panel kommt
voll vom Klassifikator"), so ``classify`` stays fully synchronous.

Every error degrades to **decline** (the safe default), never to an uncontrolled fan-out (§7).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .registry.guards import COVERAGE_AXES, axes_covered, jaccard, lens_signature
from .registry.loader import PanelRegistry
from .registry.resolve import SovereigntyError, resolve_effort, resolve_policy
from .registry.schema import SynthesisTemplate
from .registry.synthesis import default_template_for_mode
from .schema import (
    ClassifierLLM,
    Decision,
    EvidenceSource,
    Mode,
    Perspective,
    ProviderPolicy,
    Route,
    RouterDecision,
    RouterInput,
)

# ── Tunables (module defaults; R-9 makes them config-overridable via mpr.router.*) ────────────────
MIN_PANEL = 3                 # editorial floor of distinct roles (kippt auf decline, §6.3)
MAX_PANEL = 7                 # cap (distinctness, §6.1)
DISTINCT_MAX_SIM = 0.6        # jaccard clone threshold (§6.1)
MIN_QUERY_CHARS = 12          # pre-check R1 (§4.1)
ROUTER_MAX_TOKENS = 768       # classifier call cap (§3.3)
ROUTER_TEMPERATURE = 0.2      # low — classification, not creativity (§3.2)
#: the only synthesis_template values the downstream SynthesisInput accepts — a free-text classifier
#: value outside this set is ignored (→ panel/default fallback), so a live model can't emit a template
#: that breaks synthesis (the canned-JSON tests never exercised an out-of-range value).
_VALID_TEMPLATES = frozenset(t.value for t in SynthesisTemplate)

_FILE_ROUTES = {Route.FILE_ONLY.value, Route.FILE_AUGMENTED.value}
_FREE_ROUTES = {Route.WIDE.value, Route.FOCUSED.value}


@dataclass(frozen=True)
class _Params:
    """Resolved router knobs for one classify() run — config overrides, else module defaults."""

    min_panel: int = MIN_PANEL
    max_panel: int = MAX_PANEL
    distinct_max_sim: float = DISTINCT_MAX_SIM
    min_query_chars: int = MIN_QUERY_CHARS
    max_tokens: int = ROUTER_MAX_TOKENS
    temperature: float = ROUTER_TEMPERATURE


def _params(config) -> _Params:
    """Build run params from a RouterConfig (duck-typed) or fall back to module defaults (config=None
    → byte-identical to the constant path)."""
    if config is None:
        return _Params()
    return _Params(
        min_panel=config.min_panel,
        max_panel=config.max_panel,
        distinct_max_sim=config.distinct_max_sim,
        min_query_chars=config.min_query_chars,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
    )

# ── Decline regexes (word-boundary, DE+EN, case-insensitive; §4.1) ───────────────────────────────
_FACT_RE = re.compile(
    r"\b(what\s+is|what\s+are|when\s+(is|was|did)|who\s+(is|was)|how\s+many|how\s+much|"
    r"convert|define|wie\s+viel(e)?|wann\s+(ist|war)|wer\s+(ist|war)|was\s+ist|was\s+sind|"
    r"was\s+kostet|what'?s)\b",
    re.IGNORECASE,
)
_YESNO_FACT_RE = re.compile(
    r"^\s*(is|are|was|were|does|do|did|can|has|have|will|ist|sind|war|hat|haben|kann|"
    r"gibt\s+es)\b",
    re.IGNORECASE,
)
_DELIBERATION_RE = re.compile(
    r"\b(should|vs\.?|versus|compare|comparison|trade-?offs?|risks?|evaluate|evaluation|best|"
    r"better|which|sollte|abw(ä|ae)gen|risiko|risiken|bewerten|bewerte|empfehl\w*|"
    r"vergleich\w*|welche[rs]?|am\s+besten|beste[rsn]?|besser(e[rsn]?)?|optimal\w*)\b",
    re.IGNORECASE,
)
_EXTRACTIVE_RE = re.compile(
    r"\b(summar(y|ize|ise)|extract|fass(e)?\s+\w*\s*zusammen|zusammenfass\w*|extrahier\w*|"
    r"list\s+(the|all)|gib\s+mir|what\s+does\s+(it|this|the\s+\w+)\s+say)\b",
    re.IGNORECASE,
)

_FENCE_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*\})\s*```", re.DOTALL)


class _ParseError(ValueError):
    """Classifier output could not be parsed as JSON (→ one re-ask, then decline §7.1)."""


class _SchemaError(ValueError):
    """Parsed JSON could not form a valid RUN RouterDecision (→ decline §7.2)."""


@dataclass(frozen=True)
class _Floor:
    """Deterministic route floor (§3.4): the allowed route set + an optional forced route."""

    allowed: frozenset
    forced: Optional[Route]
    notes: tuple = field(default_factory=tuple)


# ── §3.4 deterministic route floor ────────────────────────────────────────────────────────────────
def _route_from_files_and_hint(inp: RouterInput) -> _Floor:
    files_present = bool(inp.files)
    allowed = frozenset(_FILE_ROUTES if files_present else _FREE_ROUTES)
    hint = inp.route_hint
    if not hint:
        return _Floor(allowed=allowed, forced=None)  # P3: LLM chooses within the set
    if hint in allowed:
        return _Floor(allowed=allowed, forced=Route(hint))  # P2: in-set hint wins
    # P2 conflict: reconcile an out-of-set hint onto the floor + record it.
    recon = Route.FILE_AUGMENTED if files_present else Route.FOCUSED
    return _Floor(allowed=allowed, forced=recon,
                  notes=(f"route:hint-reconciled({hint}->{recon.value})",))


def _default_route(allowed: frozenset) -> Route:
    return Route.FILE_AUGMENTED if Route.FILE_AUGMENTED.value in allowed else Route.WIDE


# ── §4.1 cheap pre-check (before the LLM call) ────────────────────────────────────────────────────
def _decline(reason: str, *, classifier_raw: Optional[str] = None, guards=None) -> RouterDecision:
    return RouterDecision(
        decision=Decision.DECLINE,
        decline_reason=reason,
        classifier_raw=classifier_raw,
        guards_applied=list(guards or []),
    )


def _decline_precheck(inp: RouterInput, floor: _Floor, p: _Params) -> Optional[RouterDecision]:
    q = inp.query.strip()
    deliberative = bool(_DELIBERATION_RE.search(q))
    # R1 — trivially short, no files
    if len(q) <= p.min_query_chars and not inp.files:
        return _decline("too short for multi-lens analysis", guards=["precheck:R1"])
    # R2 — single-fact marker, not vetoed by a deliberation marker
    if not deliberative and _FACT_RE.search(q):
        return _decline("single-fact lookup", guards=["precheck:R2"])
    # R3 — single source, extractive ask
    if (len(inp.files) == 1 and inp.route_hint in (None, Route.FILE_ONLY.value)
            and _EXTRACTIVE_RE.search(q)):
        return _decline("single-source retrieval — no multi-perspective gain", guards=["precheck:R3"])
    # R4 — closed yes/no factual question, not deliberative
    if not deliberative and _YESNO_FACT_RE.search(q):
        return _decline("closed factual question", guards=["precheck:R4"])
    return None


# ── §3.2/3.3 the single classifier call ──────────────────────────────────────────────────────────
_SYSTEM = (
    "You are MPR's routing classifier. Output ONE JSON object matching the schema. No prose. "
    'Decline (decision="decline") when the question is a SINGLE FACT lookup, a SINGLE-SOURCE '
    "retrieval, or otherwise gains nothing from multiple distinct expert lenses. Otherwise classify "
    "route/domain/mode and propose a panel of 3..7 DISTINCT roles — each a genuinely different lens, "
    'not a paraphrase. Prefer a known domain; use domain="adhoc" only if none fits, then decompose '
    "the question into distinct role lenses yourself."
)


def _domain_catalog(registry: Optional[PanelRegistry]) -> str:
    if registry is None:
        return "(none)"
    lines = []
    for dom in registry.domains():
        panel = registry.resolve(dom)
        desc = (panel.description if panel else "") or ""
        lines.append(f"- {dom}: {desc}")
    return "\n".join(lines) if lines else "(none)"


def _classify_call(inp: RouterInput, floor: _Floor, llm: ClassifierLLM,
                   registry: Optional[PanelRegistry], p: _Params, *, strict: bool = False) -> str:
    allowed = sorted(floor.allowed)
    forced = floor.forced.value if floor.forced else None
    files = [{"path": f.path, "excerpt": (f.excerpt or "")[:400]} for f in inp.files]
    user = (
        f"route_floor: allowed={allowed} forced={forced}\n"
        f"known domains:\n{_domain_catalog(registry)}\n"
        f"files: {json.dumps(files, ensure_ascii=False)}\n"
        f"locale: {inp.locale or '-'}\n"
        f"hints (advisory, ignore if unfitting): domain={inp.domain_hint or '-'} "
        f"mode={inp.mode_hint or '-'}\n"
        f"QUESTION:\n{inp.query}\n\n"
        'Respond with ONE JSON object: {"decision","route","domain","mode","perspectives":'
        '[{"role","lens_prompt","effort","provider_policy"}],"synthesis_template",'
        '"evidence_source","decline_reason"}.'
    )
    if strict:
        user += "\n\nReturn ONLY the JSON object — no markdown fence, no prose, no trailing text."
    return llm.complete_json(_SYSTEM, user, max_tokens=p.max_tokens, temperature=p.temperature)


# ── §5 coerce, defaults, registry mapping ────────────────────────────────────────────────────────
def _extract_json(raw: str) -> dict:
    s = (raw or "").strip()
    for candidate in (s,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    m = _FENCE_RE.search(s)
    if m:
        try:
            obj = json.loads(m.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001
            pass
    raise _ParseError("classifier output is not valid JSON")


def _coerce_mode(value, panel) -> Mode:
    if value:
        try:
            return Mode(value)
        except ValueError:
            pass
    if panel is not None:
        try:
            return Mode(panel.mode)
        except ValueError:
            pass
    return Mode.EVIDENCE_RESEARCH


def _parse_perspectives(parsed: dict) -> list:
    out = []
    for p in parsed.get("perspectives") or []:
        if not isinstance(p, dict):
            continue
        role, lens = p.get("role"), p.get("lens_prompt")
        if not role or not lens:
            continue
        kwargs = {"role": role, "lens_prompt": lens}
        if p.get("effort"):
            kwargs["effort"] = p["effort"]
        if p.get("provider_policy"):
            kwargs["provider_policy"] = p["provider_policy"]
        try:
            out.append(Perspective(**kwargs))
        except Exception:  # noqa: BLE001 — drop a malformed perspective; min-panel guards the count
            continue
    return out


def _scaffold_from_panel(panel, mode: Mode) -> list:
    mode_val = mode.value
    scaffold = []
    for r in panel.roles:
        scaffold.append(Perspective(
            role=r.role,
            lens_prompt=r.lens_prompt,
            effort=resolve_effort(panel, r, mode_val).value,
            provider_policy=resolve_policy(panel, r).value,
        ))
    return scaffold


def _evidence_for(route: Route, panel, parsed_evidence) -> EvidenceSource:
    if route == Route.FILE_ONLY:
        return EvidenceSource.INTERNAL
    if route == Route.FILE_AUGMENTED:
        return EvidenceSource.MIXED
    if panel is not None:
        return EvidenceSource(panel.evidence_source)
    if parsed_evidence:
        try:
            return EvidenceSource(parsed_evidence)
        except ValueError:
            pass
    return EvidenceSource.EXTERNAL if route == Route.WIDE else EvidenceSource.MIXED


def _coerce_and_default(raw: str, inp: RouterInput, floor: _Floor,
                        registry: Optional[PanelRegistry]) -> RouterDecision:
    parsed = _extract_json(raw)
    notes = list(floor.notes)

    # Classifier decline → honoured (reason normalised in _apply_decline_rules).
    if str(parsed.get("decision", "")).lower() == Decision.DECLINE.value:
        return RouterDecision(
            decision=Decision.DECLINE,
            decline_reason=parsed.get("decline_reason") or "classifier declined",
            classifier_raw=raw,
            guards_applied=notes,
        )

    # RUN: route clamp to floor.
    if floor.forced is not None:
        route = floor.forced
    else:
        rv = parsed.get("route")
        route = Route(rv) if rv in floor.allowed else _default_route(floor.allowed)

    domain = parsed.get("domain") or "adhoc"
    panel = registry.resolve(domain) if registry is not None else None
    mode = _coerce_mode(parsed.get("mode"), panel)

    raw_tmpl = parsed.get("synthesis_template")
    raw_tmpl = raw_tmpl if raw_tmpl in _VALID_TEMPLATES else None   # drop an out-of-range classifier value
    if panel is not None:  # known domain → registry scaffold ∪ LLM additions (registry first)
        perspectives = _scaffold_from_panel(panel, mode) + _parse_perspectives(parsed)
        synthesis_template = raw_tmpl or panel.synthesis_template
    else:               # adhoc → panel comes wholly from the classifier (§8)
        domain = "adhoc"
        perspectives = _parse_perspectives(parsed)
        synthesis_template = raw_tmpl or default_template_for_mode(mode).value

    evidence = _evidence_for(route, panel, parsed.get("evidence_source"))

    # §5.5 sovereignty clamp (fail-closed): internal evidence → every perspective local-only.
    if evidence == EvidenceSource.INTERNAL:
        perspectives = [
            p.model_copy(update={"provider_policy": ProviderPolicy.LOCAL_ONLY}) for p in perspectives
        ]

    try:
        return RouterDecision(
            decision=Decision.RUN,
            route=route,
            domain=domain,
            mode=mode,
            perspectives=perspectives,
            synthesis_template=synthesis_template,
            evidence_source=evidence,
            classifier_raw=raw,
            guards_applied=notes,
        )
    except Exception as exc:  # noqa: BLE001 — a half-empty RUN must not escape (→ decline §7.2)
        raise _SchemaError(str(exc)) from exc


# ── §4.2 decline override ─────────────────────────────────────────────────────────────────────────
def _apply_decline_rules(cand: RouterDecision, inp: RouterInput) -> RouterDecision:
    if cand.decision == Decision.DECLINE:
        if not cand.decline_reason:
            # never let a reasonless decline through (the schema would reject it anyway).
            return _decline("classifier declined", classifier_raw=cand.classifier_raw,
                            guards=cand.guards_applied)
        return cand
    return cand  # never upgrade a decline to run; min-panel handles a too-thin RUN (§6.3)


# ── §6 guards ─────────────────────────────────────────────────────────────────────────────────────
def _distinctness_guard(cand: RouterDecision, p: _Params) -> RouterDecision:
    kept, sigs = [], []
    for persp in cand.perspectives:
        sig = lens_signature(persp)
        if all(jaccard(sig, s) < p.distinct_max_sim for s in sigs):
            kept.append(persp)
            sigs.append(sig)
        else:
            cand.guards_applied.append(f"distinctness:dropped({persp.role})")
    cand.perspectives = kept[:p.max_panel]
    return cand


def _registry_role_for_axis(panel, axis: str, mode: Mode) -> Optional[Perspective]:
    mode_val = mode.value
    for r in panel.roles:
        if axes_covered([f"{r.role} {r.lens_prompt}"], [axis]):
            return Perspective(
                role=r.role,
                lens_prompt=r.lens_prompt,
                effort=resolve_effort(panel, r, mode_val).value,
                provider_policy=resolve_policy(panel, r).value,
            )
    return None


def _coverage_guard(cand: RouterDecision, inp: RouterInput,
                    registry: Optional[PanelRegistry], p: _Params) -> RouterDecision:
    expected = COVERAGE_AXES.get(cand.domain or "", [])
    if not expected or registry is None:
        return cand  # adhoc / unknown → no reference axes (no-op)
    panel = registry.resolve(cand.domain)
    if panel is None:
        return cand
    present = {f"{persp.role} {persp.lens_prompt}" for persp in cand.perspectives}
    have = axes_covered(present, expected)
    for axis in expected:
        if axis in have or len(cand.perspectives) >= p.max_panel:
            continue
        role = _registry_role_for_axis(panel, axis, cand.mode)
        if role is not None and not any(p.role == role.role for p in cand.perspectives):
            if cand.evidence_source == EvidenceSource.INTERNAL:
                role = role.model_copy(update={"provider_policy": ProviderPolicy.LOCAL_ONLY})
            cand.perspectives.append(role)
            cand.guards_applied.append(f"coverage:added({axis})")
            have.add(axis)
    return cand


def _min_panel_guard(cand: RouterDecision, p: _Params) -> RouterDecision:
    n = len(cand.perspectives)
    if n >= p.min_panel:
        return cand
    reason = ("panel degenerates to a single lens" if n <= 1
              else f"insufficient distinct perspectives ({n}<{p.min_panel})")
    return _decline(reason, classifier_raw=cand.classifier_raw, guards=cand.guards_applied)


# ── §3.1 the pipeline ─────────────────────────────────────────────────────────────────────────────
def classify(inp: RouterInput, *, llm: ClassifierLLM,
             registry: Optional[PanelRegistry] = None, config=None) -> RouterDecision:
    """Deterministic relevance/classification gate around ONE classifier call (§3.1).

    Returns a validated ``RouterDecision``. ``config`` (a RouterConfig, duck-typed) overrides the
    module-default knobs; ``config=None`` is byte-identical to the constant path. Every failure
    degrades to **decline** (§7): a pre-check catch, a transport error, an unparsable reply (one
    re-ask first), a schema violation, or a panel that cannot carry ``min_panel`` distinct lenses.
    """
    p = _params(config)
    floor = _route_from_files_and_hint(inp)

    early = _decline_precheck(inp, floor, p)
    if early is not None:
        return early  # NO LLM call

    try:
        raw = _classify_call(inp, floor, llm, registry, p)
    except Exception:  # noqa: BLE001 — transport/auth error is not the model's fault (R7.3)
        return _decline("router-llm-unavailable", guards=list(floor.notes))

    try:
        cand = _coerce_and_default(raw, inp, floor, registry)
    except _ParseError:
        # R7.1 — one stricter re-ask, then decline.
        try:
            raw2 = _classify_call(inp, floor, llm, registry, p, strict=True)
            cand = _coerce_and_default(raw2, inp, floor, registry)
        except _SchemaError:
            return _decline("router-schema-invalid", classifier_raw=raw, guards=list(floor.notes))
        except SovereigntyError:
            return _decline("router-sovereignty-conflict", classifier_raw=raw, guards=list(floor.notes))
        except Exception:  # noqa: BLE001
            return _decline("router-classify-failed", classifier_raw=raw, guards=list(floor.notes))
    except _SchemaError:
        return _decline("router-schema-invalid", classifier_raw=raw, guards=list(floor.notes))
    except SovereigntyError:
        return _decline("router-sovereignty-conflict", classifier_raw=raw, guards=list(floor.notes))

    cand = _apply_decline_rules(cand, inp)
    if cand.decision == Decision.DECLINE:
        return cand

    # Guards are deterministic but _coverage_guard touches resolve_policy → a sovereignty conflict on a
    # future panel must degrade to decline, never escape classify (M1: structural fail-soft, not luck).
    try:
        cand = _distinctness_guard(cand, p)
        cand = _coverage_guard(cand, inp, registry, p)
        cand = _min_panel_guard(cand, p)
    except SovereigntyError:
        return _decline("router-sovereignty-conflict", classifier_raw=raw, guards=list(floor.notes))
    except Exception:  # noqa: BLE001 — a guard fault degrades to decline, never an uncontrolled run
        return _decline("router-guard-failed", classifier_raw=raw, guards=list(floor.notes))
    return cand


# ── §9 audit hook (provenance, file-first) ────────────────────────────────────────────────────────
#: The fields the run-manifest (Spec 07 / unit 1d) records from a router decision. The router writes
#: NO manifest itself — it only supplies these. ``classifier_raw is None`` ⇔ a pre-check decline (no
#: LLM call), which is the EXPECTED value there, not a missing field.
PROVENANCE_FIELDS = (
    "decision", "route", "domain", "mode", "perspectives", "synthesis_template",
    "evidence_source", "decline_reason", "classifier_raw", "guards_applied", "schema_version",
)


def provenance(decision: RouterDecision) -> dict:
    """The §9 provenance dict for the manifest — a JSON-safe dump of the decision (enums → values).

    Every ``PROVENANCE_FIELDS`` key is present; ``perspectives`` carry role/lens_prompt/effort/
    provider_policy. On a pre-check decline ``classifier_raw`` is ``None`` (no call happened).
    """
    return decision.model_dump(mode="json")
