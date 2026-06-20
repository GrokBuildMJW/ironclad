"""Router config keys (Spec 04 §10) — the typed ``mpr.router.*`` surface ``classify`` consumes.

Sibling to ``registry/config.py`` (Reg-8): defaults are imported from ``router`` so config and code
never drift, and ``classify(config=…)`` overrides the knobs while the module constants remain the
``config=None`` default (byte-identical path). Two-tier (Spec 04 §3.5): the ``RouterDecision``
@model_validator keeps the FIXED ``_PANEL_HARD_FLOOR``; ``min_panel`` here is the editorial threshold,
so it must stay ``>= _PANEL_HARD_FLOOR``. The global config precedence lives in Cfg (Spec 09 / 1e).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .router import (
    DISTINCT_MAX_SIM,
    MAX_PANEL,
    MIN_PANEL,
    MIN_QUERY_CHARS,
    ROUTER_MAX_TOKENS,
    ROUTER_TEMPERATURE,
)
from .schema import _PANEL_HARD_FLOOR


class RouterConfig(BaseModel):
    """Resolved router config — defaults mirror router.py's constants."""

    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = None          # classifier model (None ⇒ engine default); read by the adapter
    max_tokens: int = ROUTER_MAX_TOKENS
    temperature: float = ROUTER_TEMPERATURE
    min_panel: int = MIN_PANEL
    max_panel: int = MAX_PANEL
    distinct_max_sim: float = DISTINCT_MAX_SIM
    min_query_chars: int = MIN_QUERY_CHARS

    @field_validator("distinct_max_sim")
    @classmethod
    def _sim_in_unit_interval(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"distinct_max_sim must be in [0, 1], got {v}")
        return v

    @field_validator("temperature")
    @classmethod
    def _temp_non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"temperature must be >= 0, got {v}")
        return v

    @field_validator("max_tokens", "min_query_chars")
    @classmethod
    def _positive(cls, v: int) -> int:
        if isinstance(v, bool) or v <= 0:
            raise ValueError(f"must be a positive int, got {v!r}")
        return v

    @model_validator(mode="after")
    def _bounds_coherent(self) -> "RouterConfig":
        if self.min_panel < _PANEL_HARD_FLOOR:
            raise ValueError(f"min_panel must be >= {_PANEL_HARD_FLOOR} (panel hard floor), got {self.min_panel}")
        if self.min_panel > self.max_panel:
            raise ValueError(f"min_panel ({self.min_panel}) must be <= max_panel ({self.max_panel})")
        return self


def load_router_config(router_section: Optional[dict]) -> RouterConfig:
    """Build a ``RouterConfig`` from the (already-resolved) ``mpr.router`` section, tolerantly.

    Missing key → module default. Precedence (env vs file) is Cfg's concern (Spec 09); this loader is
    precedence-agnostic, mirroring ``registry.config.load_registry_config``.
    """
    rs = router_section or {}
    data: dict = {}
    for key in ("model", "max_tokens", "temperature", "min_panel", "max_panel",
                "distinct_max_sim", "min_query_chars"):
        if rs.get(key) is not None:
            data[key] = rs[key]
    return RouterConfig(**data)
