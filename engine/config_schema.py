"""Typed configuration schema for the Ironclad engine.

This module is deliberately pure and stdlib-only.  It is the single source for
the code-default tree, boundary parsers, lifecycle metadata, and retired-key
metadata.  Runtime modules may import it; it never imports the engine or ACK.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional


RUNTIME = "runtime"
BOOT_ONLY = "boot_only"
SWITCH = "switch"
TUNING = "tuning"
PUBLIC = "public"
ENV_NAME = "env_name"
REDACT = "redact"

_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


class ConfigError(ValueError):
    """A clear, operator-facing configuration refusal."""


def parse_env_bool(raw: str) -> bool:
    """Parse the only supported environment-variable boolean vocabulary."""
    if not isinstance(raw, str):
        raise ConfigError("environment boolean must be text")
    word = raw.strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ConfigError(
        "expected one of true/false/1/0/yes/no/on/off (case-insensitive)"
    )


def _env_string(raw: str) -> str:
    return raw


def _env_int(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("expected an integer") from exc


def _env_float(raw: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("expected a number") from exc
    if not math.isfinite(value):
        raise ConfigError("expected a finite number")
    return value


def _env_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError("expected JSON") from exc


def _type_name(expected: "type | tuple[type, ...]") -> str:
    types = expected if isinstance(expected, tuple) else (expected,)
    return " or ".join("null" if t is type(None) else t.__name__ for t in types)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


Relationship = Callable[[Mapping[str, Any]], Optional[str]]
ValueValidator = Callable[[Any], Optional[str]]


@dataclass(frozen=True)
class LeafSpec:
    """Metadata and validation contract for one effective dotted leaf."""

    key: str
    python_type: "type | tuple[type, ...]"
    default: Any
    enum: tuple[Any, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    minimum_exclusive: bool = False
    maximum_exclusive: bool = False
    relationship: Optional[Relationship] = None
    validator: Optional[ValueValidator] = None
    secret_policy: str = PUBLIC
    lifecycle: str = RUNTIME
    classification: str = TUNING
    env_parser: Callable[[str], Any] = _env_string
    env_names: tuple[str, ...] = ()
    deprecated: Optional[str] = None


@dataclass(frozen=True)
class TombstoneSpec:
    key: str
    reason: str
    replacement: Optional[str] = None
    alias: bool = False


@dataclass(frozen=True)
class ExternalSeam:
    name: str
    kind: str
    meaning: str


EXTERNAL_SEAMS = (
    ExternalSeam(
        "conf/memory/memory.json",
        "file",
        "Component-owned MemoryManager overlay; tolerantly merged over the typed `memory.*` block.",
    ),
    ExternalSeam(
        "GX10_MEMORY_URL",
        "environment",
        "Overrides `memory.base_url` and enables the cold-memory seam when non-empty.",
    ),
    ExternalSeam(
        "GX10_MEMORY_AGENT",
        "environment",
        "Supplies the cold-memory base `agent_id` when `GX10_MEMORY_URL` activates the seam.",
    ),
    ExternalSeam(
        "conf/warm/warm.json",
        "file",
        "Component-owned WarmTier overlay; tolerantly merged over the typed `warm.*` block.",
    ),
    ExternalSeam(
        "GX10_WARM_URL",
        "environment",
        "Overrides `warm.url` and enables the warm tier when non-empty.",
    ),
    ExternalSeam(
        "GX10_SESSION_ID",
        "environment",
        "Selects the warm session key; an empty value resolves to `main`.",
    ),
)


def _get(config: Mapping[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def _trim_relationship(config: Mapping[str, Any]) -> Optional[str]:
    try:
        trim = _get(config, "context.trim_target_chars")
        maximum = _get(config, "context.max_ctx_chars")
    except KeyError:
        return None
    if trim >= maximum:
        return "context.trim_target_chars must be less than context.max_ctx_chars"
    return None


def _quality_relationship(config: Mapping[str, Any]) -> Optional[str]:
    try:
        consecutive = _get(config, "quality.min_consecutive")
        window = _get(config, "quality.window")
    except KeyError:
        return None
    if consecutive > window:
        return "quality.min_consecutive must not exceed quality.window"
    return None


def _worker_relationship(config: Mapping[str, Any]) -> Optional[str]:
    try:
        per_worker = _get(config, "workers.max_tokens")
        batch = _get(config, "workers.max_batch_tokens")
    except KeyError:
        return None
    if per_worker > batch:
        return "workers.max_tokens must not exceed workers.max_batch_tokens"
    return None


def _memory_chunk_relationship(config: Mapping[str, Any]) -> Optional[str]:
    try:
        size = _get(config, "memory.chunk_size")
        overlap = _get(config, "memory.chunk_overlap")
    except KeyError:
        return None
    if overlap >= size:
        return "memory.chunk_overlap must be less than memory.chunk_size"
    return None


def _timeout_relationship(config: Mapping[str, Any]) -> Optional[str]:
    try:
        request = _get(config, "connection.request_timeout_s")
        connect = _get(config, "connection.connect_timeout_s")
    except KeyError:
        return None
    # Only the unambiguous ordering is enforced: a TCP connect budget cannot exceed the whole-request
    # budget. request/idle/first-token bound DIFFERENT things (per-request read vs idle-watchdog vs
    # time-to-first-token) and are independently tuned per deployment (see #1131/#1397), so the schema
    # does not invent a cross-ordering between them — that would refuse valid long-prefill tunings.
    if connect > request:
        return "connection.connect_timeout_s must not exceed connection.request_timeout_s"
    return None


def _relative_code_subdir(value: Any) -> Optional[str]:
    if not value:
        return None
    normalized = value.replace("\\", "/").strip("/")
    if ":" in normalized or ".." in normalized.split("/") or value.startswith(("/", "\\")):
        return "must be a relative path without a drive or '..' traversal"
    return None


def _multi_tenant(value: Any) -> Optional[str]:
    if value is True:
        return (
            "cannot be enabled until request-path authorization and tenant namespacing "
            "are fail-closed"
        )
    return None


def _leaf(
    key: str,
    python_type: "type | tuple[type, ...]",
    default: Any,
    *,
    enum: tuple[Any, ...] = (),
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    minimum_exclusive: bool = False,
    maximum_exclusive: bool = False,
    relationship: Optional[Relationship] = None,
    validator: Optional[ValueValidator] = None,
    secret_policy: str = PUBLIC,
    lifecycle: str = RUNTIME,
    classification: str = TUNING,
    env_names: tuple[str, ...] = (),
    env_parser: Optional[Callable[[str], Any]] = None,
) -> LeafSpec:
    if env_parser is None:
        types = python_type if isinstance(python_type, tuple) else (python_type,)
        if bool in types:
            env_parser = parse_env_bool
        elif int in types and float not in types:
            env_parser = _env_int
        elif float in types or int in types:
            env_parser = _env_float
        elif list in types or dict in types:
            env_parser = _env_json
        else:
            env_parser = _env_string
    return LeafSpec(
        key=key,
        python_type=python_type,
        default=default,
        enum=enum,
        minimum=minimum,
        maximum=maximum,
        minimum_exclusive=minimum_exclusive,
        maximum_exclusive=maximum_exclusive,
        relationship=relationship,
        validator=validator,
        secret_policy=secret_policy,
        lifecycle=lifecycle,
        classification=classification,
        env_parser=env_parser,
        env_names=env_names,
    )


_CODE_AGENT_POOL = [
    {
        "provider_id": "claude-opus", "kind": "cli", "agent_id": "OPUS",
        "display": "Claude Opus 4.8", "model": "claude-opus-4-8", "bin": "claude",
        "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
        "effort": "xhigh", "permission_mode": "default",
        "cost_per_1k_in": 0.015, "cost_per_1k_out": 0.075,
    },
    {
        "provider_id": "claude-sonnet", "kind": "cli", "agent_id": "SONNET",
        "display": "Claude Sonnet 5", "model": "claude-sonnet-5", "bin": "claude",
        "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
        "effort": "high", "permission_mode": "default",
        "cost_per_1k_in": 0.003, "cost_per_1k_out": 0.015,
    },
]

_PLANNING_KEYWORDS = [
    "erstell", "plane", "plan ", "zerleg", "analysier", "entscheid", "review", "architekt",
    "design", "warum", "weshalb", "vergleich", "refactor", "implementier", "konzept", "proposal",
    "handover", "bewerte", "strateg", "evaluier", "optimier", "begründ", "schlag vor", "entwirf",
]
_ROUTINE_KEYWORDS = [
    "welche", "was ist offen", "offen", "status", "liste", "list ", "zeig", "übersicht", "überblick",
    "wie viele", "show", "open task", "lies ", "cat ", "ls ", "gib mir", "welcher", "welches",
    "etwas zu tun", "zu tun", "steht an", "todo", "to-do", "idle", "anything to do", "was liegt an",
    "liegt was an",
]


_SPECS = [
    _leaf("connection.base_url", str, "http://localhost:8000/v1", env_names=("GX10_BASE_URL",)),
    _leaf("connection.model", str, "qwen3.6-35b", env_names=("GX10_MODEL",)),
    _leaf("connection.api_key_env", str, "GX10_API_KEY", secret_policy=ENV_NAME),
    _leaf("connection.request_timeout_s", (int, float), 120.0, minimum=0, minimum_exclusive=True,
          maximum=3600, relationship=_timeout_relationship, env_names=("GX10_LLM_TIMEOUT_S",),
          env_parser=_env_float),
    _leaf("connection.connect_timeout_s", (int, float), 10.0, minimum=0, maximum=120,
          minimum_exclusive=True,
          relationship=_timeout_relationship, env_names=("GX10_LLM_CONNECT_TIMEOUT_S",), env_parser=_env_float),
    _leaf("connection.first_token_timeout_s", (int, float), 600.0, minimum=0, maximum=1800,
          minimum_exclusive=True,
          relationship=_timeout_relationship, env_names=("GX10_LLM_FIRST_TOKEN_TIMEOUT_S",), env_parser=_env_float),
    _leaf("connection.max_retries", int, 1, minimum=0, env_names=("GX10_LLM_MAX_RETRIES",)),
    _leaf("search.enabled", bool, False, lifecycle=BOOT_ONLY, classification=SWITCH,
          env_names=("GX10_SEARCH_ENABLED",)),
    _leaf("search.adapter", str, "cli", enum=("cli", "brave", "mock"), lifecycle=BOOT_ONLY,
          classification=SWITCH, env_names=("GX10_SEARCH_ADAPTER",)),
    _leaf("search.api_key_env", str, "GX10_SEARCH_API_KEY", lifecycle=BOOT_ONLY, secret_policy=ENV_NAME),
    _leaf("search.count", int, 10, minimum=1, maximum=100, env_names=("GX10_SEARCH_COUNT",)),
    _leaf("search.max_output_chars", int, 100_000, minimum=1,
          env_names=("GX10_SEARCH_MAX_OUTPUT_CHARS",)),
    _leaf("forge.enabled", bool, False, classification=SWITCH, env_names=("GX10_FORGE_ENABLED",)),
    _leaf("forge.repo", str, "", env_names=("GX10_FORGE_REPO",)),
    _leaf("forge.adapter", str, "cli", enum=("cli", "native", "mock"), classification=SWITCH,
          env_names=("GX10_FORGE_ADAPTER",)),
    _leaf("forge.token_env", str, "GX10_FORGE_TOKEN", secret_policy=ENV_NAME,
          env_names=("GX10_FORGE_TOKEN_ENV",)),
    _leaf("review.agent", str, "", env_names=("GX10_REVIEW_AGENT",)),
    _leaf("review.timeout_s", (int, float), 180.0, minimum=0, minimum_exclusive=True, maximum=3600,
          env_names=("GX10_REVIEW_TIMEOUT_S",), env_parser=_env_float),
    _leaf("notify.webhook", str, "", secret_policy=REDACT, env_names=("GX10_NOTIFY_WEBHOOK",)),
    _leaf("audit.scope", str, "mutating", enum=("mutating", "all"), classification=SWITCH,
          env_names=("GX10_AUDIT_SCOPE",)),
    _leaf("metrics.window_s", int, 3600, minimum=1),
    _leaf("metrics.slo_error_rate", (int, float), 0.2, minimum=0, maximum=1),
    _leaf("metrics.slo_p95_latency_s", (int, float), 60.0, minimum=0, minimum_exclusive=True),
    _leaf("alert.enabled", bool, False, lifecycle=BOOT_ONLY, classification=SWITCH,
          env_names=("GX10_ALERT_ENABLED",)),
    _leaf("alert.interval_s", (int, float), 300, minimum=0, minimum_exclusive=True, lifecycle=BOOT_ONLY),
    _leaf("platform.mode", str, "auto", enum=("auto", "windows", "linux"), classification=SWITCH,
          env_names=("GX10_PLATFORM",)),
    _leaf("tasks.dedup_threshold", (int, float), 0.8, minimum=0, maximum=1),
    _leaf("tasks.id_prefix", str, "KGC", lifecycle=BOOT_ONLY),
    _leaf("lodestar.enabled", bool, False, classification=SWITCH),
    _leaf("loop_profiles.default", dict, {}),
    _leaf("loop_profiles.by_type", dict, {}),
    _leaf("quality.threshold", (int, float), 0.5, minimum=0, maximum=1),
    _leaf("quality.min_consecutive", int, 3, minimum=1, relationship=_quality_relationship),
    _leaf("quality.window", int, 20, minimum=1, relationship=_quality_relationship),
    _leaf("process.hints_enabled", bool, False, classification=SWITCH),
    _leaf("process.max_hints", int, 3, minimum=1),
    _leaf("framing_notes.enabled", bool, False, classification=SWITCH),
    _leaf("ace.max_bullets", int, 200, minimum=0),
    _leaf("ace.rounds", int, 1, minimum=1),
    _leaf("ace.top_k", int, 8, minimum=1),
    _leaf("ace.cost", int, 1, minimum=1),
    _leaf("ace.embed_url", str, ""),
    _leaf("ace.fork_mpr.enabled", bool, False, classification=SWITCH),
    _leaf("verify.grounding_threshold", (int, float), 0.5, minimum=0, maximum=1),
    _leaf("strategy.budget", int, 3, minimum=1, maximum=3),
    _leaf("security.profile", str, "open", enum=("open", "token", "sealed"), lifecycle=BOOT_ONLY,
          classification=SWITCH, env_names=("GX10_PROFILE",)),
    _leaf("security.allow_unauthenticated_bind", bool, False, lifecycle=BOOT_ONLY,
          classification=SWITCH, env_names=("GX10_ALLOW_UNAUTHENTICATED_BIND",)),
    _leaf("security.token_env", str, "GX10_SERVER_TOKEN", lifecycle=BOOT_ONLY, secret_policy=ENV_NAME),
    _leaf("security.session_heartbeat_s", int, 30, minimum=5, lifecycle=BOOT_ONLY,
          env_names=("GX10_SESSION_HEARTBEAT",)),
    _leaf("security.code_locality", str, "mount", enum=("mount", "local"), lifecycle=BOOT_ONLY,
          classification=SWITCH, env_names=("GX10_CODE_LOCALITY",)),
    _leaf("security.web_in_sealed", bool, False, lifecycle=BOOT_ONLY, classification=SWITCH),
    _leaf("security.sandbox", str, "auto", enum=("auto", "bwrap", "firejail"), classification=SWITCH,
          env_names=("GX10_SANDBOX",)),
    _leaf("security.multi_tenant", bool, False, classification=SWITCH, validator=_multi_tenant,
          env_names=("GX10_MULTI_TENANT",)),
    _leaf("security.tooling_envelope.allow_list", (list, type(None)), None, lifecycle=BOOT_ONLY),
    # "auto" is a first-class value (INSTALL-1 #503): the desktop installer ships GX10_SETUP_TYPE=auto and
    # resolve_offload_topology derives server/local from the base_url at boot. Dropping it would silently
    # degrade the auto-install to `server` (env) or crash boot (file). See gx10._VALID_SETUP_TYPES.
    _leaf("setup.type", str, "server", enum=("server", "local", "auto"), lifecycle=BOOT_ONLY,
          classification=SWITCH, env_names=("GX10_SETUP_TYPE",)),
    _leaf("server.host", str, "127.0.0.1", lifecycle=BOOT_ONLY,
          env_names=("GX10_SERVER_HOST",)),
    _leaf("workers.concurrency", int, 4, minimum=1, maximum=64, lifecycle=BOOT_ONLY,
          env_names=("GX10_FANOUT_CONCURRENCY",)),
    _leaf("workers.max_tokens", int, 1024, minimum=1, lifecycle=BOOT_ONLY,
          relationship=_worker_relationship, env_names=("GX10_WORKERS_MAX_TOKENS",)),
    _leaf("workers.max_batch_tokens", int, 8192, minimum=1, lifecycle=BOOT_ONLY,
          relationship=_worker_relationship, env_names=("GX10_WORKERS_MAX_BATCH_TOKENS",)),
    _leaf("workers.memory_read", bool, True, classification=SWITCH, env_names=("GX10_WORKER_MEMORY",)),
    _leaf("workers.memory_write", bool, True, classification=SWITCH, env_names=("GX10_WORKER_WRITE",)),
    _leaf("workers.write_mode", str, "reducer", enum=("reducer", "direct"), classification=SWITCH,
          env_names=("GX10_WORKER_WRITE_MODE",)),
    _leaf("providers.default_id", (str, type(None)), None, lifecycle=BOOT_ONLY,
          env_names=("GX10_PROVIDERS_DEFAULT",)),
    _leaf("providers.max_agents", int, 3, minimum=1, lifecycle=BOOT_ONLY,
          env_names=("GX10_PROVIDERS_MAX_AGENTS",)),
    _leaf("providers.cli_timeout_s", (int, float), 900.0, minimum=0, maximum=3600,
          minimum_exclusive=True,
          lifecycle=BOOT_ONLY, env_names=("GX10_PROVIDERS_CLI_TIMEOUT_S",), env_parser=_env_float),
    _leaf("providers.effort_max_tokens", dict,
          {"low": 512, "medium": 1024, "high": 2048, "xhigh": 4096}, lifecycle=BOOT_ONLY),
    _leaf("providers.budget.usd_cap", (int, float, type(None)), None, minimum=0,
          env_names=("GX10_PROVIDERS_BUDGET_USD",), env_parser=_env_float),
    _leaf("providers.pool", list, [], lifecycle=BOOT_ONLY),
    _leaf("code_agents.pool", list, _CODE_AGENT_POOL, lifecycle=BOOT_ONLY),
    _leaf("code_agents.pinned", (str, type(None)), None, classification=SWITCH),
    _leaf("code_agents.timeout_s", (int, float), 1800.0, minimum=0, minimum_exclusive=True, maximum=7200,
          env_names=("GX10_CODE_AGENTS_TIMEOUT_S",), env_parser=_env_float),
    _leaf("code_agents.exhausted.stderr_patterns", list, [
        r"(?i)\b(quota|usage limit|rate limit|insufficient (credit|balance|quota))\b",
        r"(?i)\b(out of|exceeded)\b.{0,24}\b(quota|credit|budget|tokens?)\b",
        r"(?i)\b429\b.{0,20}too many requests",
    ], lifecycle=BOOT_ONLY),
    _leaf("code_agents.exhausted.exit_codes", list, [], lifecycle=BOOT_ONLY),
    _leaf("code_agents.exhausted.json_event_types", list, [], lifecycle=BOOT_ONLY),
    _leaf("code_agents.classes.complex", list, ["OPUS"], lifecycle=BOOT_ONLY),
    _leaf("code_agents.classes.standard", list, ["SONNET", "OPUS"], lifecycle=BOOT_ONLY),
    _leaf("code_agents.classes.routine", list, ["SONNET"], lifecycle=BOOT_ONLY),
    _leaf("code_agents.classes.analysis", list, ["SONNET"], lifecycle=BOOT_ONLY),
    _leaf("memory.enabled", bool, True, lifecycle=BOOT_ONLY, classification=SWITCH),
    _leaf("memory.base_url", str, "", lifecycle=BOOT_ONLY, env_names=("GX10_MEMORY_URL",)),
    _leaf("memory.agent_id", str, "ironclad", lifecycle=BOOT_ONLY, env_names=("GX10_MEMORY_AGENT",)),
    _leaf("memory.user_id", (str, type(None)), None, lifecycle=BOOT_ONLY),
    _leaf("memory.add_timeout", (int, float), 120.0, minimum=0, minimum_exclusive=True,
          lifecycle=BOOT_ONLY),
    _leaf("memory.read_timeout", (int, float), 15.0, minimum=0, minimum_exclusive=True,
          lifecycle=BOOT_ONLY),
    _leaf("memory.deep_timeout", (int, float), 40.0, minimum=0, minimum_exclusive=True,
          lifecycle=BOOT_ONLY),
    _leaf("memory.health_ttl", (int, float), 10.0, minimum=0, minimum_exclusive=True,
          lifecycle=BOOT_ONLY),
    _leaf("memory.chunk_long_artifacts", bool, True, lifecycle=BOOT_ONLY, classification=SWITCH,
          env_names=("GX10_MEMORY_CHUNKING",)),
    _leaf("memory.chunk_size", int, 6000, minimum=1, relationship=_memory_chunk_relationship,
          lifecycle=BOOT_ONLY),
    _leaf("memory.chunk_overlap", int, 400, minimum=0, relationship=_memory_chunk_relationship,
          lifecycle=BOOT_ONLY),
    _leaf("memory.recency_tiebreak", bool, True, lifecycle=BOOT_ONLY, classification=SWITCH,
          env_names=("GX10_MEMORY_RECENCY",)),
    _leaf("warm.enabled", bool, True, lifecycle=BOOT_ONLY, classification=SWITCH),
    _leaf("warm.url", str, "", lifecycle=BOOT_ONLY, env_names=("GX10_WARM_URL",)),
    _leaf("warm.session_ttl", int, 86_400, minimum=1, lifecycle=BOOT_ONLY),
    _leaf("warm.cache_ttl", int, 180, minimum=1, lifecycle=BOOT_ONLY),
    _leaf("warm.timeout", (int, float), 0.5, minimum=0, minimum_exclusive=True, lifecycle=BOOT_ONLY),
    _leaf("onboarding.enabled", bool, False, classification=SWITCH, env_names=("GX10_ONBOARDING",)),
    _leaf("autopilot.enabled", bool, False, classification=SWITCH, env_names=("GX10_AUTOPILOT",)),
    _leaf("autopilot.claude_bin", str, "claude"),
    _leaf("autopilot.extra_args", list, []),
    _leaf("autopilot.default_effort", str, "medium", enum=("low", "medium", "high", "xhigh"),
          classification=SWITCH),
    _leaf("autopilot.logs_dir", str, "logs"),
    _leaf("autopilot.max_concurrent", int, 1, minimum=1, maximum=16),
    _leaf("autopilot.stream", bool, False, classification=SWITCH, env_names=("GX10_AUTOPILOT_STREAM",)),
    _leaf("autopilot.terminate_on_advance", bool, False, classification=SWITCH,
          env_names=("GX10_AUTOPILOT_TERMINATE",)),
    _leaf("autopilot.autoplan", bool, False, classification=SWITCH,
          env_names=("GX10_AUTOPILOT_AUTOPLAN",)),
    _leaf("autopilot.autoplan_max_tasks", int, 20, minimum=1, maximum=100,
          env_names=("GX10_AUTOPILOT_MAX_TASKS",)),
    _leaf("autopilot.log_terminal", bool, False, classification=SWITCH,
          env_names=("GX10_AUTOPILOT_LOG_TERMINAL",)),
    _leaf("paths.system_prompt", str, "prompts/GX10_Orchestrator_SystemPrompt.md",
          env_names=("GX10_PROMPT",)),
    _leaf("paths.workdir", str, ".", lifecycle=BOOT_ONLY, env_names=("GX10_WORKDIR",)),
    _leaf("paths.state_root", str, ".ironclad", lifecycle=BOOT_ONLY),
    _leaf("paths.vault_root", str, "vault", lifecycle=BOOT_ONLY),
    _leaf("paths.session_file", str, "session.json", lifecycle=BOOT_ONLY),
    _leaf("paths.code_root", str, "", lifecycle=BOOT_ONLY),
    _leaf("paths.code_subdir", str, "", lifecycle=BOOT_ONLY, validator=_relative_code_subdir),
    _leaf("paths.plugins_dir", str, "", lifecycle=BOOT_ONLY, env_names=("GX10_PLUGINS_DIR",)),
    _leaf("paths.post_advance_hooks", list, [], lifecycle=BOOT_ONLY),
    _leaf("paths.active_capability_backlog", (str, type(None)), None),
    _leaf("generation.temperature", (int, float), 0.3, minimum=0, maximum=2),
    _leaf("generation.max_tokens", int, 8192, minimum=1, env_names=("GX10_MAX_TOKENS",)),
    _leaf("generation.finalize_on_truncation", bool, False, classification=SWITCH,
          env_names=("GX10_FINALIZE_ON_TRUNCATION",)),
    _leaf("generation.thinking_mode", str, "auto", classification=SWITCH, env_names=("GX10_THINKING",)),
    _leaf("generation.stream", bool, True, classification=SWITCH),
    _leaf("generation.retry_backoff", (int, float), 1.5, minimum=0),
    _leaf("generation.language", str, "en", env_names=("GX10_LANGUAGE",)),
    _leaf("context.max_iterations", int, 20, minimum=1),
    _leaf("context.max_ctx_chars", int, 80_000, minimum=1, relationship=_trim_relationship,
          env_names=("GX10_MAX_CTX_CHARS",)),
    _leaf("context.trim_target_chars", int, 48_000, minimum=1, relationship=_trim_relationship,
          env_names=("GX10_TRIM_TARGET_CHARS",)),
    _leaf("context.max_model_len", int, 32_768, minimum=1,
          env_names=("IRONCLAD_MAX_MODEL_LEN", "GX10_MAX_MODEL_LEN")),
    _leaf("context.token_budget", bool, True, classification=SWITCH, env_names=("GX10_TOKEN_BUDGET",)),
    _leaf("context.chars_per_token", (int, float), 2.6, minimum=0, minimum_exclusive=True,
          env_names=("GX10_CHARS_PER_TOKEN",), env_parser=_env_float),
    _leaf("context.thinking_reserve", int, 4000, minimum=0, env_names=("GX10_THINKING_RESERVE",)),
    _leaf("context.min_output_tokens", int, 1024, minimum=1, env_names=("GX10_MIN_OUTPUT_TOKENS",)),
    _leaf("context.overflow_safety_tokens", int, 1536, minimum=0, env_names=("GX10_OVERFLOW_SAFETY",)),
    _leaf("context.turn_idle_timeout_s", (int, float), 240.0, minimum=0, minimum_exclusive=True,
          relationship=_timeout_relationship, env_names=("GX10_TURN_IDLE_TIMEOUT_S",), env_parser=_env_float),
    _leaf("context.memory_brief_tokens", int, 1200, minimum=1, env_names=("GX10_MEMORY_BRIEF_TOKENS",)),
    _leaf("context.max_file_chars", int, 24_000, minimum=1),
    _leaf("context.list_dir_hard_cap", int, 200, minimum=1),
    _leaf("context.summarize_evicted", bool, True, classification=SWITCH,
          env_names=("GX10_CONTEXT_SUMMARY",)),
    _leaf("context.summary_max_tokens", int, 512, minimum=1, env_names=("GX10_SUMMARY_MAX_TOKENS",)),
    _leaf("context.emergency_summarize", bool, False, classification=SWITCH,
          env_names=("GX10_EMERGENCY_SUMMARIZE",)),
    _leaf("context.proactive_roll", bool, False, classification=SWITCH, env_names=("GX10_PROACTIVE_ROLL",)),
    _leaf("context.ingest_soft_frac", (int, float), 0.7, minimum=0, maximum=1, minimum_exclusive=True,
          env_names=("GX10_INGEST_SOFT_FRAC",), env_parser=_env_float),
    _leaf("context.max_summaries_per_turn", int, 0, minimum=0,
          env_names=("GX10_MAX_SUMMARIES_PER_TURN",)),
    _leaf("context.rag_enabled", bool, True, classification=SWITCH, env_names=("GX10_CONTEXT_RAG",)),
    _leaf("context.rag_top_k", int, 5, minimum=1, env_names=("GX10_RAG_TOP_K",)),
    _leaf("context.rag_max_tokens", int, 1024, minimum=1, env_names=("GX10_RAG_MAX_TOKENS",)),
    _leaf("thinking_auto.planning_keywords", list, _PLANNING_KEYWORDS),
    _leaf("thinking_auto.routine_keywords", list, _ROUTINE_KEYWORDS),
    _leaf("workspace.dirs", list, ["vault"]),
    _leaf("workspace.idle_marker", str, "# Workflow — idle\n\nNo active handover.\n"),
    _leaf("watcher.feedback_dir", str, "feedback"),
    _leaf("watcher.interval", (int, float), 3.0, minimum=0, minimum_exclusive=True, lifecycle=BOOT_ONLY),
    _leaf("automation.decoupled", bool, False, classification=SWITCH),
    _leaf("heartbeat.stall_seconds", (int, float), 900, minimum=0, minimum_exclusive=True),
    _leaf("heartbeat.claim_lease_seconds", (int, float), 120, minimum=0, minimum_exclusive=True),
    _leaf("ui.max_lines", int, 5000, minimum=1),
    _leaf("ui.refresh_interval", (int, float), 0.1, minimum=0, minimum_exclusive=True),
    _leaf("ui.spinner_frames", str, "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"),
]

LEAVES = {spec.key: spec for spec in _SPECS}
if len(LEAVES) != len(_SPECS):  # pragma: no cover - import-time schema authoring guard
    raise RuntimeError("duplicate config schema leaf")
SCHEMA = LEAVES


_TOMBSTONE_DATA = {
    "ack.enabled": "task validation is always on",
    "design_gate.enabled": "design lifecycle protection is always on",
    "advance_gate.enabled": "completion authority is always on",
    "security.tooling_envelope.enabled": "tooling authorization is always on",
    "audit.enabled": "mutating-action audit is always on",
    "security.injection_defense": "injection fencing is always on",
    "security.egress_analysis.enabled": "egress enforcement is always on",
    "ace.safe_promote": "learned-state safety is always on",
    "verify.enabled": "handover verification is always on",
    "quality.enabled": "the output-quality breaker is always on",
    "safety.ambiguity_detect": "the no-guessing ambiguity gate is always on",
    "strategy.enabled": "finite failure strategy is always on",
    "providers.enabled": "setup.type is the single provider-topology authority",
    "lessons.enabled": "ACE is always on through the PlaybookStore provider",
    "lessons.max_per_scope": "use ace.max_bullets instead",
    "safety.constraint_conflict_detect": "product constraint-conflict detection remains retired",
    "providers.scoring": "router scoring uses fixed built-in constants until a live policy is implemented",
    "watcher.enabled": "/auto on|off is the single watcher authority",
}
TOMBSTONES = {key: TombstoneSpec(key, reason) for key, reason in _TOMBSTONE_DATA.items()}
TOMBSTONES.update({
    "constraint_gate.enabled": TombstoneSpec(
        "constraint_gate.enabled", "renamed configuration key",
        replacement="framing_notes.enabled", alias=True,
    ),
    "process.enabled": TombstoneSpec(
        "process.enabled", "renamed configuration key",
        replacement="process.hints_enabled", alias=True,
    ),
})
DEPRECATIONS = TOMBSTONES

BOOT_ONLY_KEYS = frozenset(spec.key for spec in LEAVES.values() if spec.lifecycle == BOOT_ONLY)
boot_only_keys = BOOT_ONLY_KEYS
CONTAINER_DEFAULTS = {"safety": {}}
ENV_BINDINGS = {
    env_name: spec
    for spec in LEAVES.values()
    for env_name in spec.env_names
}


def defaults_tree() -> dict[str, Any]:
    """Build a fresh nested defaults tree exclusively from schema leaf defaults."""
    root: dict[str, Any] = copy.deepcopy(CONTAINER_DEFAULTS)
    for dotted, spec in LEAVES.items():
        node = root
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = copy.deepcopy(spec.default)
    return root


def validate_leaf(key: str, value: Any) -> Any:
    """Validate one already-parsed value and return it unchanged."""
    spec = LEAVES.get(key)
    if spec is None:
        tombstone = tombstone_for(key)
        if tombstone is not None:
            raise ConfigError(f"{key}: retired configuration key ({tombstone.reason})")
        raise ConfigError(f"{key}: unknown configuration leaf")
    expected = spec.python_type if isinstance(spec.python_type, tuple) else (spec.python_type,)
    if type(value) not in expected:
        raise ConfigError(
            f"{key}: expected {_type_name(spec.python_type)}, got {type(value).__name__}"
        )
    if spec.enum and value not in spec.enum:
        choices = ", ".join(str(choice) for choice in spec.enum)
        raise ConfigError(f"{key}: expected one of {choices}, got {value!r}")
    if value is not None and (spec.minimum is not None or spec.maximum is not None):
        if not _number(value):
            raise ConfigError(f"{key}: expected a finite number")
        number = float(value)
        if spec.minimum is not None and (
            number < spec.minimum or (spec.minimum_exclusive and number == spec.minimum)
        ):
            op = ">" if spec.minimum_exclusive else ">="
            raise ConfigError(f"{key}: must be {op} {spec.minimum:g}")
        if spec.maximum is not None and (
            number > spec.maximum or (spec.maximum_exclusive and number == spec.maximum)
        ):
            op = "<" if spec.maximum_exclusive else "<="
            raise ConfigError(f"{key}: must be {op} {spec.maximum:g}")
    if spec.validator is not None:
        problem = spec.validator(value)
        if problem:
            raise ConfigError(f"{key}: {problem}")
    return value


def tombstone_for(key: str) -> Optional[TombstoneSpec]:
    """Return exact/subtree tombstone metadata for *key*, if retired."""
    if key in TOMBSTONES:
        return TOMBSTONES[key]
    return next((spec for retired, spec in TOMBSTONES.items() if key.startswith(retired + ".")), None)


def validate(
    config: Mapping[str, Any], *, plugin_roots: tuple[str, ...] = ()
) -> Mapping[str, Any]:
    """Validate a complete merged core projection and return it unchanged.

    Known mapping/list leaves are opaque typed values: their internal provider,
    loop-profile, or tooling policy schemas are owned by their dedicated loaders.
    Unknown core roots/leaves and malformed containers are refused.
    """
    if not isinstance(config, Mapping):
        raise ConfigError(f"configuration root: expected dict, got {type(config).__name__}")
    required_roots = set(defaults_tree())
    missing = sorted(required_roots - set(config))
    if missing:
        raise ConfigError("configuration is missing required root(s): " + ", ".join(missing))

    prefixes = {
        ".".join(key.split(".")[:i])
        for key in LEAVES
        for i in range(1, key.count(".") + 1)
    } | set(CONTAINER_DEFAULTS)

    def walk(node: Any, prefix: str = "") -> None:
        if not isinstance(node, Mapping):
            label = prefix or "configuration root"
            raise ConfigError(f"{label}: expected dict, got {type(node).__name__}")
        for name, value in node.items():
            if not isinstance(name, str):
                raise ConfigError(f"{prefix or 'configuration root'}: keys must be strings")
            if name.startswith("_"):
                continue
            dotted = f"{prefix}.{name}" if prefix else name
            if not prefix and dotted in plugin_roots:
                if not isinstance(value, Mapping):
                    raise ConfigError(f"plugin root {dotted}: expected dict, got {type(value).__name__}")
                continue
            if dotted in LEAVES:
                validate_leaf(dotted, value)
            elif dotted in prefixes:
                walk(value, dotted)
            else:
                tombstone = tombstone_for(dotted)
                if tombstone is not None:
                    raise ConfigError(f"{dotted}: retired configuration key ({tombstone.reason})")
                raise ConfigError(f"{dotted}: unknown configuration leaf")

    walk(config)
    seen: set[Relationship] = set()
    for spec in LEAVES.values():
        check = spec.relationship
        if check is None or check in seen:
            continue
        seen.add(check)
        problem = check(config)
        if problem:
            raise ConfigError(problem)
    return config


__all__ = [
    "BOOT_ONLY", "BOOT_ONLY_KEYS", "CONTAINER_DEFAULTS", "ConfigError", "DEPRECATIONS", "ENV_BINDINGS", "ENV_NAME",
    "EXTERNAL_SEAMS", "ExternalSeam", "LEAVES", "LeafSpec", "PUBLIC", "REDACT", "RUNTIME", "SCHEMA", "SWITCH", "TOMBSTONES", "TUNING",
    "TombstoneSpec", "defaults_tree", "parse_env_bool", "tombstone_for", "validate", "validate_leaf",
    "boot_only_keys",
]
