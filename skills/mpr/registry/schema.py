"""MPR role-registry schema (SSOT) — ``Role`` + ``Panel`` (Spec 05 §3).

Dependency-light (pydantic-v2 + stdlib only, exactly like ``ack/case_spec.py``): a panel is a
declarative ACK-style case-spec — one domain = one ``Panel`` = one role garniture. Both models use
``ConfigDict(extra="forbid", use_enum_values=True)`` → closed objects (``additionalProperties:false``)
whose typos fail at emission.

Cardinality lives in **validators, never in JSON-Schema keywords**: ``minItems``/``maxItems``/
``uniqueItems`` *400* under XGrammar V1 (``case_spec.py:232``), so ``MIN_ROLES..MAX_ROLES`` and label
uniqueness are enforced in ``_roles_well_formed`` — ``panel_json_schema()`` stays lint-clean. (``ge=1``
on ``version`` emits ``minimum`` and ``extra="forbid"`` emits ``additionalProperties`` — both are
XGrammar-safe; the lint only flags array-cardinality keywords.)
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Build knobs (module-top so the Panel model_validator sees them at load time) ──────────────────
MIN_ROLES = 3    # below three lenses it is not a panel (distinctness guard, §7)
MAX_ROLES = 9    # governor/cost guard; the router may trim below this


class Effort(str, Enum):
    """Effort/token depth per role. In the reasoning-only path it bites ONLY via max_tokens
    (``fanout`` has no effort arg); the ``{effort}`` template column applies only to the optional
    code-CLI lane (a free string, no ironclad enum) — see Spec 05 §4."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class ProviderPolicy(str, Enum):
    """Sovereignty flag (spec 01, §E). ``local-only`` = NEVER dispatched externally."""

    LOCAL_ONLY = "local-only"
    OFFLOADABLE = "offloadable"


class EvidenceSource(str, Enum):
    INTERNAL = "internal"     # repo / local code / internal docs
    EXTERNAL = "external"     # web / public research
    MIXED = "mixed"


class SynthesisTemplate(str, Enum):
    DECISION_MATRIX = "decision-matrix"
    EVIDENCE_REPORT = "evidence-report"
    COMPARISON_MATRIX = "comparison-matrix"
    RISK_REGISTER = "risk-register"


class Mode(str, Enum):
    DECISION = "decision"
    EVIDENCE_RESEARCH = "evidence-research"
    COMPARISON = "comparison"


class Role(BaseModel):
    """One lens in a panel — a distinct reasoning standpoint."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    role: str = Field(description="Short, unique role label (e.g. 'SRE / Ops').")
    lens_prompt: str = Field(
        description="Role-specific prompt preamble; set before the query (→ a fanout item)."
    )
    effort: Optional[Effort] = Field(
        default=None,
        description="This role's effort; None ⇒ inherits panel.effort_defaults[default].",
    )
    provider_policy: Optional[ProviderPolicy] = Field(
        default=None,
        description="Sovereignty; None ⇒ inherited from evidence_source (internal→local-only else offloadable).",
    )

    @field_validator("role", "lens_prompt")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be a non-empty string")
        return v.strip()


class EffortDefaults(BaseModel):
    """Panel default effort + optional per-mode override."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    default: Effort = Field(description="Effort a role inherits when it carries no own field.")
    by_mode: dict[str, Effort] = Field(
        default_factory=dict,
        description="Optional Mode→Effort override (Mode enum value as key).",
    )

    @field_validator("by_mode")
    @classmethod
    def _keys_are_valid_modes(cls, v: dict) -> dict:
        # A typo key (e.g. "decison") would never match in resolve_effort (§4) → a silent
        # fall-through to default. A dict key can't carry a schema keyword, so close the drift
        # gap hard in the validator — same extra="forbid" philosophy as the rest of the model.
        valid = {m.value for m in Mode}
        unknown = [k for k in v if k not in valid]
        if unknown:
            raise ValueError(
                f"by_mode keys must be Mode values {sorted(valid)}, got unknown {unknown}"
            )
        return v


class Panel(BaseModel):
    """Domain role-panel — one case-spec, one domain. SSOT for a role set."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    domain: str = Field(description="Domain key = resolution key (slug, [a-z0-9-]).")
    mode: Mode = Field(description="Primary working mode of this panel.")
    roles: list[Role] = Field(description="The distinct lenses (>= MIN_ROLES, <= MAX_ROLES).")
    effort_defaults: EffortDefaults = Field(description="Default effort + per-mode override.")
    evidence_source: EvidenceSource = Field(description="Where this panel's evidence comes from.")
    synthesis_template: SynthesisTemplate = Field(description="Output format of the synthesis stage.")
    version: int = Field(default=1, ge=1, description="Schema/content version of this domain.")
    description: str = Field(
        default="",
        description="What this panel is for (flows into CASE.description / docs).",
    )

    # --- cardinality as VALIDATOR, never as a JSON-Schema keyword (XGrammar V1!) ---
    @field_validator("domain")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9-]*$", v or ""):
            raise ValueError(f"domain {v!r} must be a lowercase slug [a-z0-9-]")
        return v

    @model_validator(mode="after")
    def _roles_well_formed(self) -> "Panel":
        n = len(self.roles)
        if not (MIN_ROLES <= n <= MAX_ROLES):
            raise ValueError(f"panel needs {MIN_ROLES}..{MAX_ROLES} roles, got {n}")
        labels = [r["role"] if isinstance(r, dict) else r.role for r in self.roles]
        norm = [" ".join(label.lower().split()) for label in labels]
        if len(set(norm)) != len(norm):
            raise ValueError("role labels must be unique (case/space-insensitive)")
        return self


# ── Schema derivation & validate (analogous to case_spec.py) ──────────────────────────────────────
def panel_json_schema() -> dict:
    """The Panel JSON-Schema (shared shapes land in ``$defs``); lint-clean for XGrammar."""
    return Panel.model_json_schema()


def validate_panel_json(data: dict) -> Panel:
    """Validate raw data into a ``Panel`` — raises ``pydantic.ValidationError`` with the exact field."""
    return Panel.model_validate(data)
