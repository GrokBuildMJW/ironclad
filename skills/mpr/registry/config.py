"""Registry config keys (Spec 05 §8) — the typed, validated ``mpr.*`` surface the registry consumes.

Just the *registry* subset of the MPR config (panels dir, role bounds, effort table, distinctness
threshold, adaptive floor). The global config precedence (env vs file vs defaults) is Spec 09 / unit
1e (Cfg-*); this module is only the registry's schema + a tolerant loader, so there is no double-build —
Cfg passes the resolved ``mpr`` section here.

Defaults are imported from the modules that own them (``MIN_ROLES``/``MAX_ROLES``, ``EFFORT_MAX_TOKENS``,
``DISTINCTNESS_MAX_OVERLAP``) so config and code can never drift. Two-tier bound (like Spec 04 §3.5):
the ``Panel`` model_validator keeps a FIXED structural 3..9, while ``roles_min``/``roles_max`` here are
the *effective* thresholds the guards / adaptive generation use — so ``roles_max`` may never exceed the
structural ceiling ``MAX_ROLES``.

Inherited (NOT redefined here): ``GX10_CLAUDE_EFFORT`` / ``GX10_AGENT_CMD`` / ``GX10_PLUGINS_DIR`` —
ironclad effort/dispatch/loader keys the plugin inherits transitively.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .guards import DISTINCTNESS_MAX_OVERLAP
from .resolve import EFFORT_MAX_TOKENS
from .schema import MAX_ROLES, MIN_ROLES

_EFFORT_KEYS = ("low", "medium", "high", "xhigh")


class RegistryConfig(BaseModel):
    """Resolved registry config — defaults mirror the owning modules' constants."""

    model_config = ConfigDict(extra="forbid")

    panels_dir: Optional[str] = None  # None ⇒ caller uses <plugin_root>/panels (mpr.panels.dir)
    roles_min: int = MIN_ROLES        # effective distinct-role floor (guards/adaptive), not the schema floor
    roles_max: int = MAX_ROLES        # effective cap; must stay <= structural ceiling MAX_ROLES
    effort_max_tokens: dict[str, int] = Field(default_factory=lambda: dict(EFFORT_MAX_TOKENS))
    distinct_max_overlap: float = DISTINCTNESS_MAX_OVERLAP
    adaptive_min_roles: Optional[int] = None  # None ⇒ ties to roles_min (resolved in the validator)

    @field_validator("distinct_max_overlap")
    @classmethod
    def _overlap_in_unit_interval(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"distinct_max_overlap must be in [0, 1], got {v}")
        return v

    @field_validator("effort_max_tokens")
    @classmethod
    def _effort_complete_and_positive(cls, v: dict) -> dict:
        missing = [k for k in _EFFORT_KEYS if k not in v]
        if missing:
            raise ValueError(f"effort_max_tokens missing keys {missing}")
        bad = [k for k in _EFFORT_KEYS if isinstance(v[k], bool) or not isinstance(v[k], int) or v[k] <= 0]
        if bad:
            raise ValueError(f"effort_max_tokens values must be positive ints; offending keys {bad}")
        return v

    @model_validator(mode="after")
    def _bounds_coherent(self) -> "RegistryConfig":
        if self.roles_min < 2:
            raise ValueError(f"roles_min must be >= 2, got {self.roles_min}")
        if self.roles_max > MAX_ROLES:
            raise ValueError(f"roles_max must be <= {MAX_ROLES} (panel structural ceiling), got {self.roles_max}")
        if self.roles_min > self.roles_max:
            raise ValueError(f"roles_min ({self.roles_min}) must be <= roles_max ({self.roles_max})")
        if self.adaptive_min_roles is None:
            self.adaptive_min_roles = self.roles_min  # default ties to the effective floor
        elif not (2 <= self.adaptive_min_roles <= self.roles_max):
            raise ValueError(
                f"adaptive_min_roles must be in [2, {self.roles_max}], got {self.adaptive_min_roles}"
            )
        return self


def load_registry_config(mpr: Optional[dict]) -> RegistryConfig:
    """Build a ``RegistryConfig`` from the (already-resolved) ``mpr`` config section.

    Reads the nested registry keys tolerantly (missing → module default); merges a partial
    ``mpr.effort.max_tokens`` onto the default table so other tiers keep their defaults. Whatever
    precedence produced ``mpr`` is Cfg's concern (Spec 09); this loader is precedence-agnostic.
    """
    mpr = mpr or {}
    panels = mpr.get("panels") or {}
    roles = mpr.get("roles") or {}
    effort = mpr.get("effort") or {}
    distinct = mpr.get("distinctness") or {}
    adaptive = mpr.get("adaptive") or {}

    data: dict = {}
    if panels.get("dir") is not None:
        data["panels_dir"] = panels["dir"]
    if roles.get("min") is not None:
        data["roles_min"] = roles["min"]
    if roles.get("max") is not None:
        data["roles_max"] = roles["max"]
    if effort.get("max_tokens"):
        data["effort_max_tokens"] = {**EFFORT_MAX_TOKENS, **effort["max_tokens"]}
    if distinct.get("max_overlap") is not None:
        data["distinct_max_overlap"] = distinct["max_overlap"]
    if adaptive.get("min_roles") is not None:
        data["adaptive_min_roles"] = adaptive["min_roles"]
    return RegistryConfig(**data)
