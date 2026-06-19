"""Provider registry (P0) — declarative, config-driven backend pool for the provider router.

Agent-neutral, secret-free: this module is pure pydantic schema + loading/validation, no
subprocess and no network. It describes WHICH reasoning backends exist (the local in-engine
Spark substrate and any headless code-CLI substrate) so the routing-policy engine (router.py)
can pick one per item and the dispatcher (dispatch.py) can run it.

Boundary: ``endpoint_env``/``api_key_env`` only name ENV variables — the values (base URL, keys)
come from the environment at runtime, never as a literal here (like ``gx10.API_KEY_ENV``).
``model``/``bin``/``cost_*`` come from ``conf/`` via config, never hard-coded in ``core/``.

Empty/missing config → ``load_registry`` returns ``None`` and the caller falls back byte-identically
to direct ``_WORKERS.fanout`` (the router stays inert when no pool is configured).
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class ProviderKind(str, Enum):
    IN_ENGINE = "in-engine"   # OpenAI-compatible API call via the existing client (Spark-vLLM etc.)
    CLI = "cli"               # headless code-CLI via a GX10_AGENT_CMD-style template (PC-pool lane)


class Capabilities(BaseModel):
    """What a backend can do — router input for the suitability check."""
    reasoning: bool = True          # MPR P0 is reasoning-only
    web_search: bool = False        # CLI with search tools (public research)
    file_io: bool = False           # may read local files (PC pool)
    local: bool = False             # runs on sovereign infra (Spark/loopback) → local-only capable
    max_effort: str = "xhigh"       # highest effort tier it can serve (low|medium|high|xhigh)


class RateLimit(BaseModel):
    max_concurrent: int = 4         # concurrent in-flight calls/agents for this provider
    rpm: Optional[int] = None       # requests per minute (None = unlimited)
    tpm: Optional[int] = None       # tokens per minute (None = unlimited)


class ProviderSpec(BaseModel):
    provider_id: str                # unique key, e.g. "spark-vllm", "claude-sonnet", "kimi-cli"
    kind: ProviderKind
    model: str                      # model identifier (passed to the substrate; NEVER a literal in code)
    # exactly one set, depending on kind:
    endpoint_env: Optional[str] = None   # in-engine: ENV name holding the base_url (e.g. "GX10_BASE_URL")
    api_key_env: Optional[str] = None    # in-engine: ENV name holding the key (fallback "not-needed")
    cmd_template: Optional[str] = None   # cli: GX10_AGENT_CMD-style template ({bin}{model}{effort}{permission}{prompt})
    bin: Optional[str] = None            # cli: executable ({bin})
    effort: Optional[str] = None         # cli: default effort ({effort}), per-item overridable
    permission_mode: Optional[str] = None  # cli: {permission}; None → inherits CLAUDE_PERMISSION_MODE
    cost_per_1k_in: float = 0.0          # $/1k input tokens (routing cost axis; local = 0.0)
    cost_per_1k_out: float = 0.0         # $/1k output tokens
    rate_limit: RateLimit = Field(default_factory=RateLimit)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    weight: int = 100                    # tie-break preference at equal suitability (higher = preferred)
    enabled: bool = True

    @field_validator("provider_id")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not v or " " in v:
            raise ValueError(f"provider_id must be a non-empty slug: {v!r}")
        return v

    def kind_consistent(self) -> Optional[str]:
        """fail-loud: cli needs cmd_template+bin; in-engine needs endpoint_env."""
        if self.kind == ProviderKind.CLI and not (self.cmd_template or self.bin):
            return f"{self.provider_id}: kind=cli requires cmd_template/bin"
        if self.kind == ProviderKind.IN_ENGINE and not self.endpoint_env:
            return f"{self.provider_id}: kind=in-engine requires endpoint_env"
        return None


class ProviderRegistry(BaseModel):
    providers: List[ProviderSpec]
    default_id: Optional[str] = None     # provider when the policy forces none (else weight-max of locals)

    def by_id(self) -> Dict[str, ProviderSpec]:
        return {p.provider_id: p for p in self.providers if p.enabled}

    def validate_loud(self) -> "ProviderRegistry":
        seen = set()
        for p in self.providers:
            if p.provider_id in seen:               # dupe guard analogous to the ACK registry (fail-loud)
                raise ValueError(f"duplicate provider_id: {p.provider_id}")
            seen.add(p.provider_id)
            err = p.kind_consistent()
            if err:
                raise ValueError(err)
        if self.default_id and self.default_id not in {p.provider_id for p in self.providers}:
            raise ValueError(f"default_id {self.default_id!r} not in pool")
        return self


def load_registry(cfg: Dict) -> Optional["ProviderRegistry"]:
    """Build from the config block ``providers`` (see spec §7). Empty/missing → ``None`` (router
    inert; caller falls back byte-identically to direct ``_WORKERS.fanout``)."""
    block = (cfg or {}).get("providers") or {}
    pool = block.get("pool") or []
    if not pool:
        return None
    reg = ProviderRegistry(
        providers=[ProviderSpec(**p) for p in pool],
        default_id=block.get("default_id"),
    )
    return reg.validate_loud()
