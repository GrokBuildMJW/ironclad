"""MPR router schema — the pydantic SSOT for the Phase-0 relevance/classification layer (Spec 04).

This module is dependency-light on purpose (pydantic + stdlib only, no registry, no engine import):
it defines *what* the router consumes (``RouterInput``/``FileRef``, §2) and *what* it emits (the
``RouterDecision`` object plus its enums and the ``Perspective`` panel entry, §3.5), and the thin
``ClassifierLLM`` port the router calls (§3.3). All routing *behaviour* lives in ``router.py``; this
file is just the typed contract so the decision object is snapshot-/replay-testable.

The enum *values* are deliberately byte-aligned with the P0 provider-router
(``engine/router.py``): ``provider_policy`` ∈ {local-only, offloadable} and the effort tiers
{low, medium, high, xhigh} hand off to P0 by string, so a MPR ``Perspective`` maps onto a P0
``RouteRequest`` without a translation table.
"""
from __future__ import annotations

from enum import Enum
from typing import List, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── Panel floor ───────────────────────────────────────────────────────────────────────────────────
# _PANEL_HARD_FLOOR is the FIXED structural minimum the @model_validator enforces: a RUN with fewer
# than two lenses is not a panel at all. The *editorial* minimum MIN_PANEL (default 3) and the cap
# MAX_PANEL (default 7) are config-overridable and enforced only in the deterministic guards
# (router.py §6.3 / §6.1) — the validator never sees runtime config, so it carries the hard floor and
# the guard carries the configurable threshold (Spec 04 §3.5). Config validation enforces
# min_panel >= _PANEL_HARD_FLOOR, so the two never contradict.
_PANEL_HARD_FLOOR = 2


# ── Input contract (§2) ─────────────────────────────────────────────────────────────────────────
class FileRef(BaseModel):
    """One attached file as seen by the router — a reference + a short excerpt, never the full body."""

    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: Optional[str] = None
    excerpt: Optional[str] = None  # first ~2k chars, classifier context only (never the whole file)
    bytes: Optional[int] = None


class RouterInput(BaseModel):
    """The validated input ``mpr_research.run()`` builds from the model-supplied tool args (§2)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)  # the user question (required)
    route_hint: Optional[Literal["wide", "focused", "file-only", "file-augmented"]] = None
    # Soft seeds (§4.2) — advisory ONLY: handed to the classifier as context, never trusted by the
    # deterministic guards and never a hard override. Free-form (any value fail-soft, the LLM ignores
    # nonsense); the resolved domain/mode still comes from classification + the registry.
    domain_hint: Optional[str] = None
    mode_hint: Optional[str] = None
    files: List[FileRef] = Field(default_factory=list)  # attached files (path + short excerpt/hash)
    locale: Optional[str] = None  # answer language (passed through, never router logic)


# ── Classifier port (§3.3) ──────────────────────────────────────────────────────────────────────
class ClassifierLLM(Protocol):
    """Minimal port over the engine's OpenAI-compatible client — no second stack, injectable for tests.

    ``mpr_research.py`` adapts ``engine._WORKERS.client`` (workers.py:56) onto this; tests inject a
    ``FakeClassifierLLM`` that returns recorded JSON, so ``classify`` is net-free and deterministic.
    """

    def complete_json(
        self, system: str, user: str, *, max_tokens: int, temperature: float
    ) -> str: ...


# ── Output schema (§3.5) ────────────────────────────────────────────────────────────────────────
class Decision(str, Enum):
    RUN = "run"
    DECLINE = "decline"


class Route(str, Enum):
    WIDE = "wide"
    FOCUSED = "focused"
    FILE_ONLY = "file-only"
    FILE_AUGMENTED = "file-augmented"


class Mode(str, Enum):
    DECISION = "decision"
    EVIDENCE = "evidence-research"
    COMPARISON = "comparison"


class Effort(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ProviderPolicy(str, Enum):
    LOCAL_ONLY = "local-only"      # sovereignty: NEVER external (hands to P0 ProviderPolicy by value)
    OFFLOADABLE = "offloadable"    # may be offloaded to an external CLI provider


class EvidenceSource(str, Enum):
    INTERNAL = "internal"          # repo/private context → forces local-only (§5 sovereignty clamp)
    EXTERNAL = "external"          # public world knowledge
    MIXED = "mixed"


class Perspective(BaseModel):
    """One distinct expert lens in the panel — the unit the fan-out turns into a reasoning worker."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1)         # the role (e.g. "SRE/Ops")
    lens_prompt: str = Field(min_length=1)  # the concrete lens, as an instruction to the worker
    effort: Effort = Effort.MEDIUM          # effort-matrix entry (→ Spec 05 §4 max_tokens mapping)
    provider_policy: ProviderPolicy = ProviderPolicy.OFFLOADABLE  # sovereignty (→ P0)


class RouterDecision(BaseModel):
    """The single validated object the router emits; consumed by synthesis (Spec 06) + P0 routing."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    route: Optional[Route] = None          # None ⇔ decision=decline
    domain: Optional[str] = None           # registry key OR "adhoc"; None ⇔ decline
    mode: Optional[Mode] = None
    perspectives: List[Perspective] = Field(default_factory=list)  # empty ⇔ decline
    synthesis_template: Optional[str] = None  # e.g. "decision-matrix" | "evidence-report"
    evidence_source: Optional[EvidenceSource] = None
    # --- Audit/Provenance (file-first manifest, Overview §10 / Spec 04 §9) ---
    decline_reason: Optional[str] = None   # set ⇔ decision=decline (for the direct answer)
    classifier_raw: Optional[str] = None   # raw LLM output (manifest, replay); None on pre-check decline
    guards_applied: List[str] = Field(default_factory=list)  # e.g. ["distinctness:dropped(...)"]
    schema_version: str = "mpr.router/1"

    @model_validator(mode="after")
    def _coherence(self) -> "RouterDecision":
        if self.decision == Decision.DECLINE:
            # Decline is self-supporting: no route/panel, but a reason is mandatory.
            if not self.decline_reason:
                raise ValueError("decline requires decline_reason")
            return self
        # RUN: required fields + hard panel floor (FIXED, not config-driven — the validator sees only
        # module globals; the config-overridable MIN_PANEL is enforced in _min_panel_guard, §6.3).
        if not (self.route and self.domain and self.mode and self.perspectives):
            raise ValueError("run requires route, domain, mode and a non-empty panel")
        if len(self.perspectives) < _PANEL_HARD_FLOOR:
            raise ValueError(f"run panel below hard floor ({_PANEL_HARD_FLOOR})")
        return self
