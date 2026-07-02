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

import glob
import json
import os
import re
import shutil
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

# #449 (review B): the agent_id is a filename token matched by the ASCII-only regexes
# _HO_AGENT_RE/_FB_RE (r"_([A-Za-z]+)…"). Validate against the SAME ASCII class — `str.isalpha()`
# would accept non-ASCII letters (e.g. "ÉGENT") that pass here but can never round-trip a filename.
_AGENT_ID_RE = re.compile(r"[A-Za-z]+")


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

    @field_validator("max_effort")
    @classmethod
    def _norm_max_effort(cls, v: str) -> str:
        # ROUTER-1 (#503): a conf value outside the enum would raise KeyError on EFFORT_RANK[max_effort]
        # deep in route_one (violating never-raises-into-the-tool-loop). Normalize an unknown tier to the
        # conservative FLOOR "low" at load — so routing never raises AND a typo can never OVER-claim
        # capability (an unknown value only makes the provider eligible for low-effort requests).
        return v if v in ("low", "medium", "high", "xhigh") else "low"


class RateLimit(BaseModel):
    max_concurrent: int = 4         # concurrent in-flight calls/agents for this provider
    rpm: Optional[int] = None       # requests per minute (None = unlimited)
    tpm: Optional[int] = None       # tokens per minute (None = unlimited)


class ProviderSpec(BaseModel):
    provider_id: str                # unique key, e.g. "spark-vllm", "claude-sonnet", "kimi-cli"
    kind: ProviderKind
    model: str                      # model identifier (passed to the substrate; NEVER a literal in code)
    # #449: a provider that ALSO serves as a handover code-AGENT declares its agent identity here. The
    # agent_id is the FILENAME-SAFE token (ASCII letters only — must satisfy _HO_AGENT_RE/_FB_RE,
    # §C0R-1): e.g. OPUS/SONNET. The agent registry is fully config-driven (no agents hard-coded in
    # core/ logic) — Ironclad ships OPUS/SONNET as OVERRIDABLE config defaults; conf/ adds its own.
    agent_id: Optional[str] = None       # handover agent token (letters only); None = routing-only provider
    display: Optional[str] = None        # human label for /coders + logs; falls back to provider_id
    # exactly one set, depending on kind:
    endpoint_env: Optional[str] = None   # in-engine: ENV name holding the base_url (e.g. "GX10_BASE_URL")
    api_key_env: Optional[str] = None    # in-engine: ENV name holding the key (fallback "not-needed")
    cmd_template: Optional[str] = None   # cli: GX10_AGENT_CMD-style template ({bin}{model}{effort}{permission}{prompt})
    bin: Optional[str] = None            # cli: executable ({bin})
    bin_glob: Optional[str] = None       # cli: #451/FORK-A3 — private-layer glob to a rotating launcher
                                         #   path (e.g. a hashed AppData dir); resolved newest when `bin`
                                         #   is not on PATH. Lives in conf/ (never a literal path in core/).
    effort: Optional[str] = None         # cli: default effort ({effort}), per-item overridable
    permission_mode: Optional[str] = None  # cli: {permission}; None → inherits CLAUDE_PERMISSION_MODE
    mcp_template: Optional[str] = None   # cli: #480 — per-CLI read-only Memory MCP config args, injected
                                         #   into the {mcp} placeholder ONLY under the sealed profile. The
                                         #   {mcp_server} token renders to the python invocation of
                                         #   memory_mcp.py. Lives in conf/ (per-CLI flag shape).
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

    @field_validator("agent_id")
    @classmethod
    def _agent_token(cls, v: Optional[str]) -> Optional[str]:
        # #449/§C0R-1: the agent_id is the handover/feedback FILENAME token — must be ASCII LETTERS ONLY
        # so it round-trips _HO_AGENT_RE (r"_([A-Za-z]+)\.md$") and _FB_RE; no underscores/hyphens/digits/
        # non-ASCII (review B: `str.isalpha()` accepts e.g. "ÉGENT", which then strands the handover).
        if v is not None and not _AGENT_ID_RE.fullmatch(v):
            raise ValueError(f"agent_id must be ASCII letters only (filename-safe token): {v!r}")
        return v

    def agent_display(self) -> str:
        return self.display or self.provider_id

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


# --------------------------------------------------------------------------- #
# Code-AGENT registry (#449, C0R-9) — the handover code-agent identity map. A SEPARATE,
# ALWAYS-ON config surface (``config.code_agents.pool``), INDEPENDENT of the fan-out
# ``providers.pool`` / ``providers.enabled`` (which is True in local-mode and would otherwise turn
# default agents into fan-out reasoning substrates). Each entry is a ProviderSpec carrying an
# ``agent_id``. Ironclad ships OPUS/SONNET as OVERRIDABLE defaults; ``conf/`` adds CODEX/KIMI.
# Unknown agent id fails closed (rejected, never silently defaulted) — replaces the six OPUS/SONNET
# allowlists, ``_MODEL_BY_AGENT`` and the legacy KIMI→SONNET normalization.
# --------------------------------------------------------------------------- #
class CodeAgentRegistry(BaseModel):
    agents: List[ProviderSpec]

    def by_agent(self) -> Dict[str, ProviderSpec]:
        """agent_id (UPPER) → spec, enabled entries only."""
        return {a.agent_id.upper(): a for a in self.agents if a.agent_id and a.enabled}

    def names(self) -> List[str]:
        """Canonical agent ids (UPPER), declaration order — the dynamic schema enum source."""
        out: List[str] = []
        for a in self.agents:
            if a.agent_id and a.enabled:
                aid = a.agent_id.upper()
                if aid not in out:
                    out.append(aid)
        return out

    def has(self, agent_id: str) -> bool:
        return (agent_id or "").upper() in self.by_agent()

    def resolve(self, agent_id: str) -> Optional[ProviderSpec]:
        """Spec for an agent id, or None if unknown (caller fails closed). ENABLED entries only — a
        disabled (onboarded) agent resolves to None, so it is never launched/probed/failed-over to."""
        return self.by_agent().get((agent_id or "").upper())

    def all_ids(self) -> List[str]:
        """#460: ALL configured agent ids (UPPER), declaration order, INCLUDING disabled/onboarded ones.
        ``names()`` is enabled-only (the launch/schema surface); this is for operator VISIBILITY — an
        onboarded-but-disabled agent (e.g. KIMI pending exhausted-signal calibration) is inert but should
        be shown as registered."""
        out: List[str] = []
        for a in self.agents:
            if a.agent_id:
                aid = a.agent_id.upper()
                if aid not in out:
                    out.append(aid)
        return out

    def spec_of(self, agent_id: str) -> Optional[ProviderSpec]:
        """The spec for an agent id INCLUDING disabled entries (``resolve`` is enabled-only). Visibility
        only — never use this to launch (a disabled agent must stay inert)."""
        aid = (agent_id or "").upper()
        return next((a for a in self.agents if (a.agent_id or "").upper() == aid), None)

    def validate_loud(self) -> "CodeAgentRegistry":
        seen = set()
        for a in self.agents:
            if not a.agent_id:
                raise ValueError(f"code_agents entry {a.provider_id!r} has no agent_id")
            aid = a.agent_id.upper()
            if aid in seen:                              # dupe guard (fail-loud)
                raise ValueError(f"duplicate code-agent agent_id: {aid}")
            seen.add(aid)
            err = a.kind_consistent()
            if err:
                raise ValueError(err)
            # #449 (review B): a code-agent must ship a COMPLETE launch spec — BOTH bin and cmd_template.
            # The generic provider lane tolerates one-or-the-other (kind_consistent), but a partial agent
            # would silently mix a configured bin with the client's Claude fallback template (or vice
            # versa) and emit a broken command. Fail loud here instead.
            if not (a.bin and a.cmd_template):
                raise ValueError(f"code-agent {aid} must define BOTH bin and cmd_template "
                                 f"(got bin={a.bin!r}, cmd_template={'set' if a.cmd_template else None})")
        return self


def load_code_agents(cfg: Dict) -> "CodeAgentRegistry":
    """Build the code-agent registry from ``config.code_agents.pool``. Always returns a registry
    (never None): the public defaults supply OPUS/SONNET even with no ``conf/``. Lists replace on
    config merge, so ``conf/`` re-lists the agents it wants (OPUS/SONNET/CODEX/…)."""
    block = (cfg or {}).get("code_agents") or {}
    pool = block.get("pool") or []
    reg = CodeAgentRegistry(agents=[ProviderSpec(**p) for p in pool])
    return reg.validate_loud()


# --------------------------------------------------------------------------- #
# Boot probe (#451) — resolve each code-agent's executable WITHOUT spending a prompt. Path-resolution
# IS the liveness signal: a resolvable executable means the agent can run; this stays filesystem-only
# (no subprocess, no token spend) so boot is fast and never flaky on a CLI's `--version` quirks.
# --------------------------------------------------------------------------- #
def resolve_agent_bin(spec: Optional[ProviderSpec]) -> Optional[str]:
    """Resolve a code-agent's executable (FORK-A3, charter): a PATH entry wins (``shutil.which`` —
    covers a stable PATH shim, option B); else, when the spec declares a private-layer ``bin_glob``,
    the NEWEST match by mtime (option C — the hashed launcher path rots on update). ``None`` ⇒ not
    resolvable. Env vars / ``~`` in ``bin_glob`` are expanded; the private path lives in ``conf/``."""
    if spec is None or not spec.bin:
        return None
    found = shutil.which(spec.bin)
    if found:
        return found
    if spec.bin_glob:
        # Normalize separators (a conf glob may use `/` while %LOCALAPPDATA% expands to `\`) and pick the
        # NEWEST match. stat() inside the try (review A): a file that vanishes between glob and stat is
        # skipped, so the probe returns None rather than raising — it never breaks boot.
        pat = os.path.expandvars(os.path.expanduser(spec.bin_glob)).replace("/", os.sep)
        best, best_mtime = None, -1.0
        for p in glob.glob(pat):
            try:
                if not os.path.isfile(p):
                    continue
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt > best_mtime:
                best, best_mtime = p, mt
        return best
    return None


def probe_code_agents(registry: "CodeAgentRegistry") -> Dict[str, Optional[str]]:
    """Per-enabled-agent liveness probe: ``agent_id`` → resolved bin path (or ``None``). The boot
    caller treats ``any(resolved)`` as cli-available and fails closed only when ZERO agents resolve."""
    return {aid: resolve_agent_bin(registry.resolve(aid)) for aid in registry.names()}


# Result classes the result classifier returns (#455).
RESULT_OK = "ok-feedback"            # the agent produced feedback → advance
RESULT_FAILED = "task-failed"        # the agent ran but produced no usable result (not a budget signal)
RESULT_UNAVAILABLE = "agent-unavailable"   # budget/quota exhausted → trip the breaker + fail over


def classify_agent_result(*, exit_code: Optional[int], stderr: str,
                          has_feedback: bool, patterns: Optional[Dict] = None) -> str:
    """#455: classify a code-agent's RAW run result into ``RESULT_OK`` | ``RESULT_FAILED`` |
    ``RESULT_UNAVAILABLE`` (budget/quota exhausted).

    A run that produced FEEDBACK is ``ok-feedback`` — period. The feedback is the agent's task result
    (semantically arbitrary: a coding answer may legitimately contain "rate limit"/"quota"), so it is
    NEVER pattern-matched (review B: scanning it caused false breaker trips + discarded good work). The
    exhausted signal lives only in the RAW process channel — stderr + exit code — which is what we scan,
    and only when there is NO feedback. LAYERED, most specific first: a structured JSON error-event
    ``type`` (one JSON object per stderr line) → a stderr regex → an exit code. Patterns come from conf
    (``code_agents.exhausted``); none ⇒ no exhausted signal. CONSERVATIVE: only an EXPLICIT exhausted
    match yields ``agent-unavailable`` — an unknown failure is ``task-failed`` (NOT unavailable), so a
    normal failure never triggers a wasteful failover. Pure + never raises (a bad conf regex is
    skipped); runs on the SERVER, so the patterns + agent literals stay in ``conf/``."""
    if has_feedback:
        return RESULT_OK                                  # a real result is never re-judged as exhausted
    pats = patterns or {}
    text = stderr or ""                                   # ONLY the raw process stderr — not the feedback
    # 1) structured JSON error-event types (most specific) — one JSON object per line
    evtypes = {str(t).lower() for t in (pats.get("json_event_types") or [])}
    if evtypes:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = str(ev.get("type") or ev.get("event") or "").lower()
            if t and t in evtypes:
                return RESULT_UNAVAILABLE
    # 2) stderr regex (a bad conf regex must never crash classification)
    for rx in (pats.get("stderr_patterns") or []):
        try:
            if re.search(rx, text):
                return RESULT_UNAVAILABLE
        except re.error:
            continue
    # 3) exit code
    if exit_code is not None and int(exit_code) in {int(c) for c in (pats.get("exit_codes") or [])}:
        return RESULT_UNAVAILABLE
    return RESULT_FAILED


def result_failure_class(result: str):
    """Re-map a code-agent run result onto the shared :class:`~ack.failure_class.FailureClass`
    taxonomy (#602 S602-3) — keeping a SINGLE failure vocabulary across the engine.

    The three run results stay the wire contract (``classify_agent_result`` + the server
    reconciler depend on the exact strings); this is the *bridge* the reflection layer
    (the Strategy Revisor / Quality breaker, #602 SUB-7/SUB-9) reads instead of re-deriving
    a taxonomy: ``RESULT_UNAVAILABLE`` → ``UNAVAILABLE`` (budget/quota exhausted),
    ``RESULT_FAILED`` → ``INCOMPLETE_OUTPUT`` (ran but produced no usable result),
    ``RESULT_OK`` → ``None`` (not a failure). Lazy-imports ``ack`` so engine import order
    and the clean-room export stay unaffected. Pure; an unknown result → ``None``.
    """
    from ack.failure_class import FailureClass  # lazy: engine→ack one-way, no import cycle

    return {
        RESULT_UNAVAILABLE: FailureClass.UNAVAILABLE,
        RESULT_FAILED: FailureClass.INCOMPLETE_OUTPUT,
    }.get(result)


def code_agent_strategy(result: str, *, attempt: int = 1, budget: int = 3):
    """Engine-side application of the Strategy Revisor to a code-agent run result (#602 S602-7).

    Maps the run result onto the shared :class:`~ack.failure_class.FailureClass` (:func:`result_failure_class`)
    then through the pure SSOT policy (:func:`ack.strategy.revise`) to a :class:`~ack.strategy.Strategy` — so
    the failover / retry path consults ONE policy instead of re-deriving its own (``RESULT_UNAVAILABLE`` →
    ``FAIL_OVER``, etc.). ``RESULT_OK`` (not a failure) → ``None``. The default ``budget=3`` keeps a single
    classification NON-terminal (the targeted action, not an immediate human-escalation); pass the real
    attempt/budget to drive escalation. Pure + additive; lazy-imports ``ack``."""
    fc = result_failure_class(result)
    if fc is None:
        return None
    from ack.strategy import revise  # lazy: engine→ack one-way, no import cycle
    return revise(fc, attempt, budget)
