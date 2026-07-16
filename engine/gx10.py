#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ironclad orchestration engine **library** — agent loop, deterministic TaskStore,
fail-closed macros (advance_pipeline / stage_handover), config-tree loader. Runs
against any OpenAI-compatible endpoint; every model-emitted task_json is validated
against the ACK contract at the stage_handover boundary.

The standalone monolithic CLI was **removed** — the system is now a headless
server (`server.py`) plus a thin client (`client.py` / the TS `clients/ink/`).
This module is imported by the server; running it directly exits with a pointer
(see `_REMOVED_MSG`).

Key design points:
  - Macro tools collapse multi-step workflows into a single deterministic call
    (advance_pipeline for completion, stage_handover for creation) — far fewer
    LLM round-trips than step-by-step file ops.
  - Streaming + incremental output; hysteresis context-trimming (prefix-cache
    friendly); per-generation perf instrumentation (TTFT, tokens/s).
  - Thinking is decided per turn (planning thinks; routine lookups don't); the ACK
    emitter turns it off per-request for reliable structured output.

Default system prompt: prompts/GX10_Orchestrator_SystemPrompt.md.
Start the system via the server: see SETUP.md.
"""

import os
import tempfile
import re
import sys
import json
import inspect
import time
import shlex
import shutil
import subprocess
import threading
import queue as _q
import contextvars
from contextlib import contextmanager
import argparse
import math
import copy
import logging
import urllib.request
import urllib.error
import urllib.parse
import config_schema
import proc_tree
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import List, Dict, Any, Optional, Tuple, Callable, Mapping

logger = logging.getLogger(__name__)

# Note: the earlier watchdog-based feedback watcher was replaced by a
# polling reconciler (more reliable, no dependency required).

try:
    from openai import OpenAI
except ImportError:
    # Soft: the module stays importable WITHOUT openai (e.g. the thin client loads only
    # the UI primitives). Only GX10 construction (which needs a client) then fails
    # with a clear message — see GX10.__init__.
    OpenAI = None  # type: ignore[assignment,misc]

try:
    from memory import MemoryManager as _MemoryManager
except ImportError:
    _MemoryManager = None

try:
    from warm import WarmTier as _WarmTier
except ImportError:
    _WarmTier = None

try:
    import project_context as _pc            # ADR-0011 AD-1: the request-scoped ProjectContext seam (S3)
except ImportError:
    _pc = None                              # absent → path accessors fall back to the legacy globals

try:
    import lifecycle_projector as _lifecycle_projector   # S13b: pure transition→evidence projector (AD-7)
except ImportError:
    _lifecycle_projector = None             # absent → the /lifecycle gate reports fail-closed BLOCKED

try:
    import project_registry as _pr           # ADR-0011 AD-6: installation-global Project Registry SSOT (S2)
except ImportError:
    _pr = None                              # absent → no registry; engine runs un-isolated (legacy)

try:
    import project_switch as _ps             # ADR-0011 AD-1: the quiesced switch core (S5a)
except ImportError:
    _ps = None

# ack.devprocess.api (the curated dev-process facade, ADR-0011 AD-3 / S6) is imported LATE — in
# _register_devprocess_driver() below, AFTER the package root is placed on sys.path — so the real launch
# (only this engine dir on the path at import time) still resolves it. Until set, the tools call the impls directly.
_devapi = None

try:
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import HSplit, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.formatted_text import ANSI
    HAS_PT = True
except ImportError:
    HAS_PT = False
    # prompt_toolkit is missing (e.g. headless server mode): still provide `Application`
    # as a name, otherwise the module annotation `Optional[Application]` crashes on
    # import. Any is correct here — the real app is only built under HAS_PT.
    Application = Any  # type: ignore[assignment,misc]

# ─── Installation location (code, read-only) ─────────────────────
# SCRIPT_DIR = where gx10_v3.py + prompts/ live. Separate from this: WORKDIR
# (where the orchestrator works) — see the config loader / main().
SCRIPT_DIR = Path(__file__).resolve().parent
# Original process cwd, where boot-relative conf seams live. The server imports gx10 before bootstrap
# changes cwd to paths.workdir, so every later config derivation must keep resolving against this anchor.
_BOOT_CWD = Path.cwd().resolve()

# The package root (this engine dir's parent) goes on sys.path so the ACK package
# (a sibling of this engine dir) is importable when the engine runs as a script.
_CORE_DIR = SCRIPT_DIR.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

# ─── Configuration (code defaults) ──────────────────────────
# These module constants are the weakest level of value precedence
# (code defaults < config file < env). At startup
# `_apply_config()` overrides them from the loaded config — so all
# existing references (run_tool, macros, _trim_context …) stay unchanged.
DEFAULT_BASE_URL = "http://localhost:8000/v1"   # generic default; real endpoint via config (connection.base_url)
DEFAULT_API_KEY  = "not-needed"
DEFAULT_MODEL    = "qwen3.6-35b"   # current orchestrator model; real endpoint via conf/connection
DEFAULT_PROMPT   = "prompts/GX10_Orchestrator_SystemPrompt.md"
_ORCH_VERSION: Optional[str] = None


def orchestrator_version() -> str:
    """The orchestrator build identity (surfaced in /health + at boot). Read once, then cached.
    Source order: env GX10_ORCHESTRATOR_VERSION → a sibling VERSION file (written by the deploy/install
    stamp) → "unknown". Pure read — NO git/SHA logic in core for the version (the deploy stamps it); generic
    + secret-free. (The ONE deliberate, scoped git call in core is ``_git_head_tree`` (#933) — a fail-soft,
    read-only delivery-tree DEFAULT for ``/lifecycle gate`` when ``--tree`` is omitted, never a version.)"""
    global _ORCH_VERSION
    if _ORCH_VERSION is None:
        v = os.environ.get("GX10_ORCHESTRATOR_VERSION")
        if not v:
            try:
                v = (Path(__file__).resolve().parent / "VERSION").read_text(encoding="utf-8").strip()
            except OSError:
                v = ""
        _ORCH_VERSION = v or "unknown"
    return _ORCH_VERSION


def registry_health() -> dict:
    """Read-only observability of the project-isolation binding for /health + the doctor (NEVER raises).
    ``status`` is ``"ok"`` when the installation-global Project Registry is wired, or ``"unisolated"`` when
    the engine fell back to the un-isolated mode at boot (a fallback only logged at boot today); the
    ``active_project`` id is None when un-isolated; ``home`` is the installation-global ``GX10_HOME``.
    Generic + secret-free; surfaces the otherwise-invisible #601 isolation binding."""
    try:
        active = _ACTIVE_PROJECT.id if _ACTIVE_PROJECT is not None else None
    except Exception:  # noqa: BLE001 — observability must never raise
        active = None
    try:
        import project_registry as _pr      # lazy: keep the module-top import surface unchanged
        home = str(_pr.ironclad_home())
    except Exception:  # noqa: BLE001
        home = None
    try:
        status = "ok" if _REGISTRY is not None else "unisolated"
    except Exception:  # noqa: BLE001
        status = "unknown"
    return {"status": status, "active_project": active, "home": home}
DEFAULT_WORKDIR  = "."           # WORKDIR: work location (CWD behaviour as before)
CODE_ROOT        = ""            # optional code root for the handover path guard
                                 # (vessel-specific, e.g. a service subfolder
                                 # in the repo); empty = check repo root only. Via paths.code_root.
MAX_ITERATIONS   = 20
MAX_CTX_CHARS    = 80_000        # high-water: trimming starts only here (char-based; derived from MAX_MODEL_LEN under TOKEN_BUDGET)
TRIM_TARGET_CHARS = 48_000       # PERF-06: low-water after the trim (60 %)
MAX_TOKENS       = 8192          # output (generation) token reserve. PERF-10: raised 4096→8192 (4096
                                 # truncated long handovers). It permanently subtracts from the usable
                                 # window; the token budget (#371/#372) reserves it accurately. Tunable —
                                 # generation.max_tokens / GX10_MAX_TOKENS: raise for longer single
                                 # outputs, lower for more context headroom (#379, default kept at 8192).
FINALIZE_ON_TRUNCATION = False  # generation.finalize_on_truncation / GX10_FINALIZE_ON_TRUNCATION:
                                 # opt-in salvage for a length-truncated reasoning-only answer.
# #366: the SMALLEST output budget still worth proceeding with. The output reserve above is a CEILING,
# not a fixed floor: when the full reserve would push the prompt over the window, `_preflight_context`
# reserves LESS output — down to this minimum — so the turn proceeds LOSSLESSLY (all context kept, a
# shorter answer) instead of failing. Only when even this minimum will not fit do we trim/raise. Tunable
# via context.min_output_tokens / GX10_MIN_OUTPUT_TOKENS.
MIN_OUTPUT_TOKENS = 1024
# #366: extra headroom the pre-flight guard keeps BELOW the model window, on top of the output + tools +
# thinking reserves, to absorb the gap between the engine's token ESTIMATE and vLLM's EXACT rendered-prompt
# count — the chat-template framing (role markers, tool-call wrapping) and the tools-schema serialization
# that `_count_prompt_tokens` cannot see token-exactly. Without it the adaptive clamp targets the wall to
# the token and any undercount slips a raw vLLM 400 through; a generous margin is cheap. Tunable via
# context.overflow_safety_tokens / GX10_OVERFLOW_SAFETY.
OVERFLOW_SAFETY_TOKENS = 1536
# MEM-9 / §3-mechanism 3 — token-accurate budgeting: couple the trim working set to the MODEL WINDOW
# instead of fixed chars. When TOKEN_BUDGET=True, _apply_config derives MAX_CTX_CHARS/
# TRIM_TARGET_CHARS from MAX_MODEL_LEN (minus reserve for output+RAG+summary, 10 % headroom). The
# LIVE trim measures REAL tokens via the served model's tokenizer (the vLLM /tokenize endpoint,
# Epic #366) and only falls back to the CHARS_PER_TOKEN estimate when that endpoint is unreachable.
# OFF = fixed char thresholds as today (then context.max_ctx_chars / GX10_MAX_CTX_CHARS apply).
MAX_MODEL_LEN    = 32768         # hard per-request token window (vLLM --max-model-len); GX10_MAX_MODEL_LEN/IRONCLAD_MAX_MODEL_LEN
TOKEN_BUDGET     = True          # default ON (06-18); off via context.token_budget=false / GX10_TOKEN_BUDGET=0
# CALIBRATED chars/token FALLBACK — used ONLY when the live tokenizer is unavailable. Real agent
# content (code/JSON/CJK) is ~2–2.6 c/t, NOT 4; a too-high ratio under-counts tokens and overflows
# the 32 768-token wall (the #366 live HTTP 400). 2.6 is conservative (under-counts c/t ⇒ over-counts
# tokens ⇒ trims earlier ⇒ never overflows). Tunable via context.chars_per_token / GX10_CHARS_PER_TOKEN.
CHARS_PER_TOKEN  = 2.6
# #366 D5: output headroom reserved for the model's thinking budget, applied ONLY when think=True for
# the call (the pre-flight guard #372 counts it so a thinking turn doesn't overflow the wall). Tunable
# via context.thinking_reserve / GX10_THINKING_RESERVE.
THINKING_RESERVE = 4000
# B1 — rolling summarization: on trim, roll the evicted rounds into a compact
# summary block directly BELOW the system prompt AND archive the raw text losslessly
# to Mem0 (/add_bulk, vector-only). Flag-gated, fail-soft, off-critical-
# path; FLAG OFF = byte-identical to today's trim (no model call, no block).
SUMMARIZE_EVICTED  = True        # B1 switch (default ON, 06-18 decision); off via context.summarize_evicted=false / GX10_CONTEXT_SUMMARY=0
SUMMARY_MAX_TOKENS = 512         # capped output of the eviction summary
_SUMMARY_MARKER    = "## Conversation so far (rolling summary)"   # stable block marker (find-and-update instead of duplicating)
# #1225 (S3): the per-turn AUTHORITATIVE steering-state block folded onto the user turn (like rag/steer) so
# the orchestrator model never GUESSES its state (the observed bug: it probed `ls vault/` + a non-existent
# sentinel → "no active project" + a fabricated vault path, while the engine held the real active project).
_STEERING_MARKER   = "## Steering state (authoritative — this is what the engine is bound to)"
# #1050 (L3): the emergency rung ALWAYS cold-archives the slice it discards (source="fragment_trim"), and an
# optional summarize-not-truncate replaces the raw drop with a bounded summary — DEFAULT OFF (raw head+tail
# truncation stays byte-identical), guarded by a hard timeout + a skip when a generation this turn errored.
EMERGENCY_SUMMARIZE           = False   # off via context.emergency_summarize=false / GX10_EMERGENCY_SUMMARIZE=0
EMERGENCY_SUMMARIZE_TIMEOUT_S = 8.0     # hard wall-clock cap on the recovery-path summarize (daemon-thread, win32-safe)
# #1051 (L3): the proactive cumulative-ingestion accountant + the shared per-turn summarize rate-limit.
PROACTIVE_ROLL         = False   # default OFF (byte-identical). ON → proactively roll the oldest tool rounds via a
                                 #   query-aware summary once cumulative ingestion crosses the soft mark
INGEST_SOFT_FRAC       = 0.7     # soft mark as a fraction of the model window that triggers a proactive roll
MAX_SUMMARIES_PER_TURN = 0       # shared per-turn cap across ALL summarize triggers (roll/emergency/proactive);
                                 #   0 ⇒ unlimited (byte-identical to today); >0 ⇒ degrade to a plain drop past the cap
# B2 — auto-retrieval assembly: per user turn ONE vector-only search (graph=false) on the
# user message, dedup against the window, a token-budgeted context block BEFORE the user message
# (at the tail → prefix cache stays). Warm cache (B0) in front (cache-aside). Flag-gated, fail-soft;
# FLAG OFF = user message appended verbatim → byte-identical to today's behaviour.
RAG_ENABLED     = True            # B2 switch (default ON, 06-18 decision); off via context.rag_enabled=false / GX10_CONTEXT_RAG=0
RAG_TOP_K       = 5               # hits per retrieval
RAG_MAX_TOKENS  = 1024            # token budget of the injected block (enforced in real tokens, #366)
_RAG_MARKER     = "## Relevant context (retrieved)"
# #458 (D1): token budget of the richer Memory BRIEF appended to a handover (warm rolling summary +
# body-keyed vector hits + optional relational hits). Enforced in real tokens. Config context.memory_brief_tokens.
MEMORY_BRIEF_TOKENS = 1200
LANGUAGE         = "en"          # the orchestrator's reply language (OSS default en; via GX10_LANGUAGE/config)
MAX_FILE_CHARS   = 24_000        # PERF-05: read_file cap (head+tail) — the CEILING; the live budget-aware
                                 # cap (#994-S16) may lower it per-turn so one read can't overflow the window.
# #1488: filesystem reads must be bounded BEFORE allocating/decoding, but this is the ALLOCATION ceiling
# (OOM protection), NOT the model-output cap — the returned text is still capped by MAX_FILE_CHARS (head+tail)
# or a ranged/pattern slice. It must be high enough to load a normal repo file (sources, docs — the engine's
# own gx10.py is ~800 KB) so #1047 ranged/pattern reads of large files keep working, yet low enough to refuse
# a truly-huge (multi-GB) file that would OOM the always-on server. 16 MiB is a safe momentary allocation.
_MAX_FILE_BYTES  = 16 * 1024 * 1024
_READ_FLOOR_CHARS = 2_000        # #994-S16: always allow at least a small excerpt (emergency-trim backstops)
#: #994-S16: the per-turn safe char cap for a file/tool read, set by the session before each tool dispatch
#: from the live remaining window budget. None ⇒ read_file uses the fixed MAX_FILE_CHARS (convenience callers,
#: no live budget). A contextvar so concurrent workers never clobber each other's per-turn cap.
_READ_BUDGET_CV: "contextvars.ContextVar[Optional[int]]" = contextvars.ContextVar(
    "_ironclad_read_budget", default=None)


def _read_char_cap() -> int:
    """The active char cap for a file/tool read: the live per-turn budget (#994-S16) if the session set one,
    else the fixed ``MAX_FILE_CHARS``. So a single read can never by itself overflow the model window."""
    b = _READ_BUDGET_CV.get()
    return b if b is not None else MAX_FILE_CHARS


#: #1046 (L1-choke, epic #1043): the ingestion tools whose result is INGESTED into the model context and
#: must be capped to the live per-turn budget at the SINGLE run-loop choke point. `read_file` caps itself,
#: but `search_files`/`list_directory`/`execute_command` do NOT, and the local-tool bridge returns before
#: read_file's cap — so ALL of them are capped here. This set controls only the destructive character cap;
#: already-budgeted or structured web/provider/plugin/memory results stay out of it.
_INGESTION_TOOLS = frozenset({"read_file", "list_directory", "search_files", "execute_command", "fetch_url", "view_issue", "pr_status", "review"})
_INGEST_MARKER_SLACK = 512   # a result read_file already capped (cap + its own marker) must pass through here

#: #1464 F3b: every result in this class crosses the mandatory injection fence before model ingestion.
#: This is deliberately distinct from `_INGESTION_TOOLS`: structured provider/plugin/memory payloads must
#: be fenced but must not be corrupted by the head/tail character cap.
_UNTRUSTED_RESULT_TOOLS = frozenset({
    *_INGESTION_TOOLS,
    "web_search", "parallel_reason", "query_memory", "deep_query_memory",
})


def _cap_ingested_result(name: str, result: str, cap_chars: int) -> str:
    """Cap an INGESTION tool's result to ``cap_chars`` (head+tail + a steering marker) at the ONE run-loop
    choke point, so a single tool result can never overflow the window — for EVERY ingestion tool, not just
    read_file, and including the local-bridge path (which returns before read_file's own cap). Idempotent
    with read_file's internal cap (its cap+marker fits within the slack) and a no-op for non-ingestion tools
    / short results, so a web_search/parallel_reason/MPR/memory payload is never touched (#366/#1046)."""
    if (name not in _INGESTION_TOOLS or not isinstance(result, str)
            or len(result) <= cap_chars + _INGEST_MARKER_SLACK):
        return result
    head_n = cap_chars * 2 // 3
    tail_n = cap_chars - head_n
    omitted = len(result) - head_n - tail_n
    return (
        result[:head_n]
        + f"\n\n... [Ironclad: {omitted} chars omitted — the {name} result was {len(result)} chars, capped "
          f"at {cap_chars} to fit the context window. Narrow it: use search_files to locate the relevant "
          f"lines, then read only those.] ...\n\n"
        + result[-tail_n:]
    )


def _is_untrusted_result(name: str) -> bool:
    """Whether a serialized tool result is untrusted model input, including every dynamic plugin/MPR."""
    return name in _UNTRUSTED_RESULT_TOOLS or name in _PLUGIN_TOOLS


def _fence_untrusted_result(name: str, result: str) -> str:
    """Apply the one mandatory post-serialization fence, failing closed without exposing raw content."""
    if not _is_untrusted_result(name):
        return result
    try:
        from ack import injection as _inj
        return _inj.wrap_untrusted(result, source=name)
    except Exception:  # noqa: BLE001 — raw untrusted bytes must never bypass a broken fence
        return (f"ERROR: {name} result withheld because mandatory injection fencing failed; "
                "the raw result was not added to model context.")


# #1084: per-action audit — the mutating/outward tool surface whose actions are recorded (content-free) into
# the mandatory tamper-evident audit ledger. The minimal first step of the audit-log epic (#1067).
_AUDIT_TOOLS = frozenset({"write_file", "write_last_reply", "edit_file", "execute_command", "create_issue", "create_pr", "comment_on_issue"})


def _audit_detail(name: str, args: "Dict[str, Any]") -> str:
    """A short, CONTENT-FREE descriptor of an action for the audit trail — the target (path/command/title/
    query/url), never the file body or command output (an audit records WHAT was done, not the payload)."""
    a = args or {}
    if name in ("write_file", "write_last_reply", "edit_file", "read_file"):
        return str(a.get("path", ""))
    if name == "execute_command":
        return str(a.get("command", ""))
    if name in ("create_issue", "create_pr"):
        return str(a.get("title", ""))
    if name == "comment_on_issue":
        return str(a.get("number", ""))
    if name in ("search_files", "query_memory", "deep_query_memory", "web_search", "remember"):
        return str(a.get("query", a.get("pattern", a.get("text", ""))))
    if name == "list_directory":
        return str(a.get("path", "."))
    if name == "fetch_url":
        return str(a.get("url", ""))
    return name   # fallback (full-surface scope): at least record which tool ran


def _audit_principal() -> str:
    """WHO — the acting principal. Single-tenant today (the orchestrator on the operator's behalf);
    per-principal identity + RBAC is #1071, which will make this the authenticated caller."""
    return "orchestrator"


def _audit_reason() -> str:
    """WHY — the context the action served: the active project/track scope. Coarse today (a per-task reason
    is a follow-up); best-effort, never raises."""
    try:
        return _active_mem_ns() or "default"
    except Exception:   # noqa: BLE001
        return ""


def _is_audit_path(p: "Any") -> bool:
    """#1067 tamper-RESISTANCE: True iff *p* resolves under the active audit directory — the agent's own
    write tools refuse it, so an autonomous agent can't edit/delete its own audit trail (beyond the
    hash-chain's tamper-EVIDENCE). Never raises."""
    try:
        audit_dir = (state_root() / "audit").resolve()
        target = Path(p).resolve()
        return target == audit_dir or audit_dir in target.parents
    except Exception:   # noqa: BLE001
        return False


def _authorize_action(role: str, tier: str) -> bool:
    """#1071: RBAC gate a server can call for a resolved principal. Single-tenant (MULTI_TENANT off) ⇒ every
    action allowed (byte-identical to today's model). Multi-tenant ⇒ delegate to `ack.authz.authorize`
    (deny-by-default). Fail-OPEN on a wiring error — the foundation is default-off and not yet the sole gate,
    so a bug must never lock out the operator; full request-path enforcement is remaining scope (ADR-0014)."""
    if not MULTI_TENANT:
        return True
    try:
        from ack import authz   # lazy: never import ack at gx10 top-level (S6b lesson)
        return authz.authorize(role, tier)
    except Exception:   # noqa: BLE001
        return True


def _tenant_mem_scope(scope: str, tenant: str = "default") -> str:
    """#1071: namespace a memory scope by *tenant* when multi-tenant is on (else byte-identical). Fail-soft."""
    if not MULTI_TENANT:
        return scope
    try:
        from ack import authz
        return authz.tenant_scope(scope, tenant)
    except Exception:   # noqa: BLE001
        return scope


def _maybe_audit(name: str, args: "Dict[str, Any]", result: str) -> None:
    """Append a result record for the configured audit surface; failures propagate."""
    if name in _AUDIT_TOOLS or AUDIT_SCOPE == "all":
        _append_audit(name, args, "result", ok=not str(result or "").startswith("ERROR"))


def _audit_ledger_path() -> Path:
    """Return the project-scoped mandatory audit-ledger path."""
    return state_root() / "audit" / "ledger.jsonl"


def _append_audit(name: str, args: "Dict[str, Any]", phase: str, *, ok: bool) -> None:
    """Append one mandatory audit record."""
    import audit_ledger
    audit_ledger.record_action(
        _audit_ledger_path(), name, _audit_detail(name, args), ok=ok,
        ts=time.time(), actor=_audit_principal(), reason=_audit_reason(), phase=phase,
    )


def _metrics_report() -> "Dict[str, Any]":
    """#1060: the GET /metrics payload — all-time + recent-window rolling telemetry (turns, error rate,
    latency p50/p95, token cost) + the SLO verdict + a recent-vs-baseline anomaly signal. Thresholds are
    config-tunable (`metrics.window_s` / `slo_error_rate` / `slo_p95_latency_s`). Fail-soft."""
    try:
        import telemetry as _tel
        cfg = (_EFFECTIVE_CFG or {}).get("metrics") or {}
        window_s = float(cfg.get("window_s", 3600) or 3600)
        err_slo = float(cfg.get("slo_error_rate", 0.2) or 0.2)
        lat_slo = float(cfg.get("slo_p95_latency_s", 60.0) or 60.0)
        now = time.time()
        all_time = _tel.snapshot(now=now)
        window = _tel.snapshot(now=now, window_s=window_s)
        return {"all_time": all_time, "window": window, "window_s": window_s,
                "slo": _tel.slo_status(window, max_error_rate=err_slo, max_p95_latency_s=lat_slo),
                "anomaly": _tel.anomaly(window, all_time)}
    except Exception as ex:   # noqa: BLE001 — /metrics must never 500
        return {"error": repr(ex)}


def _notify_alert(alert: "Dict[str, Any]") -> bool:
    """#1061: page one alert to the configured webhook (#1083). No-op (False) when no webhook. Fail-soft."""
    if not NOTIFY_WEBHOOK:
        return False
    try:
        import alerting as _alerting
        import notify as _notify
        return _notify.notify_webhook(NOTIFY_WEBHOOK, _alerting.format_alert(alert),
                                      extra={"kind": alert.get("kind"), "severity": alert.get("severity")})
    except Exception:   # noqa: BLE001 — a paging failure must never break a scan/receive
        return False


def _alert_scan() -> "List[Dict[str, Any]]":
    """#1061: evaluate the telemetry SLO/anomaly (#1060) against the alert rules + page each alert (correlated
    with the running deploy version). Returns the alerts fired. Fail-soft."""
    try:
        import alerting as _alerting
        alerts = _alerting.evaluate(_metrics_report(), version=orchestrator_version())
        for a in alerts:
            _notify_alert(a)
        return alerts
    except Exception:   # noqa: BLE001
        return []


def _receive_alert(payload: "Any") -> "Dict[str, Any]":
    """#1061: inbound alert receiver — normalize an EXTERNAL alert + page it (correlated with the running
    deploy version). Returns ``{ok, notified, severity}`` or ``{ok: False, error}``. Fail-soft."""
    try:
        import alerting as _alerting
        alert, err = _alerting.normalize_inbound(payload)
        if err:
            return {"ok": False, "error": err}
        alert["version"] = orchestrator_version()
        sent = _notify_alert(alert)
        return {"ok": True, "notified": bool(sent), "severity": alert["severity"]}
    except Exception as ex:   # noqa: BLE001
        return {"ok": False, "error": repr(ex)}


LIST_DIR_HARD_CAP = 200          # HV-B: hard cap in list_directory
# #1488: search is bounded independently of read_file. It scans up to _SEARCH_MAX_FILES candidates (one at a
# time, each discarded after scanning), so the per-file read cap is DECOUPLED from read_file's 16 MiB
# single-file ceiling and kept small — a source file's matchable lines fit easily; a huge data file is
# skipped past the cap. This keeps the whole search bounded in both files-scanned and per-file bytes.
_SEARCH_MAX_FILES = LIST_DIR_HARD_CAP * 5
_SEARCH_MAX_FILE_BYTES = 1024 * 1024
_SEARCH_HIT_CAP = 50
TEMPERATURE      = 0.3
RETRY_BACKOFF    = 1.5           # OPT-4: wait time (s) before 1× retry on an API error
# Guard 1 (#1131/epic #1130): per-request LLM bound. Without it a hung completion (a stalled stream, a wedged
# nested MPR/worker call) holds the turn — and the server agent lock — for the OpenAI SDK default (~600s) ×
# retries = a silent multi-minute stall. Applied at EVERY OpenAI() construction (the agent client + the ACE
# reflector; workers/MPR reuse the agent client). Tunable via connection.request_timeout_s / GX10_LLM_TIMEOUT_S.
LLM_REQUEST_TIMEOUT_S = 120.0    # seconds per LLM request (connect+read); a slow LAN 35B first-token stays well under
LLM_CONNECT_TIMEOUT_S: "Optional[float]" = float(config_schema.LEAVES["connection.connect_timeout_s"].default)
LLM_FIRST_TOKEN_TIMEOUT_S: "Optional[float]" = float(config_schema.LEAVES["connection.first_token_timeout_s"].default)
LLM_MAX_RETRIES       = 1        # SDK client retries; kept low so it can't compound _make_completion's own retry
# Guard 1 / S2 (#1132/epic #1130): per-turn IDLE watchdog. If a turn makes NO progress (no generation chunk, no
# completed generation, no tool result) for this long it is aborted AND SURFACED ("⏱ TURN ABORTED — model
# stalled"), never a silent indefinite hold of the agent lock. A backstop ABOVE the per-request LLM timeout,
# reset on every progress signal so a slow-but-progressing deep turn (e.g. an MPR panel) is never killed. 0 ⇒ off.
TURN_IDLE_TIMEOUT_S   = 240.0    # seconds of NO progress before a turn is declared stalled (GX10_TURN_IDLE_TIMEOUT_S)
_REASONING_FINALIZE_NUDGE = (
    "Your previous reply exhausted the token budget on reasoning and produced no answer. "
    "Give your final answer now, directly and concisely, with NO further reasoning."
)


def _opt_float(v):
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "auto", "none")):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _decoupled() -> bool:
    return LLM_FIRST_TOKEN_TIMEOUT_S is not None and LLM_FIRST_TOKEN_TIMEOUT_S > 0


def _is_timeout_error(e) -> bool:
    try:
        import httpx
        if isinstance(e, httpx.TimeoutException):
            return True
    except Exception:
        pass
    try:
        import openai
        if isinstance(e, openai.APITimeoutError):
            return True
    except Exception:
        pass
    return False


def _client_timeout():
    if not _decoupled():
        return LLM_REQUEST_TIMEOUT_S
    import httpx
    connect = LLM_CONNECT_TIMEOUT_S or LLM_REQUEST_TIMEOUT_S
    return httpx.Timeout(connect=connect,
                         read=LLM_FIRST_TOKEN_TIMEOUT_S,
                         write=LLM_REQUEST_TIMEOUT_S,
                         pool=LLM_REQUEST_TIMEOUT_S)


def _total_request_deadline_s() -> float:
    """The hard WHOLE-request wall-clock budget (#1544). Decoupled mode gives the prefill its
    first_token budget PLUS the request budget for the rest; non-decoupled uses the request budget.
    0/negative disables the cap (byte-identical to before)."""
    req = float(LLM_REQUEST_TIMEOUT_S)
    if req <= 0:
        return 0.0
    if _decoupled():
        return float(LLM_FIRST_TOKEN_TIMEOUT_S) + req
    return req


def _idle_limit(first_token_seen: bool) -> float:
    if _decoupled() and not first_token_seen:
        # Backstop above the httpx read deadline so the named first-token
        # timeout fires first, and pre-HTTP work cannot consume that budget.
        return max(TURN_IDLE_TIMEOUT_S, float(LLM_FIRST_TOKEN_TIMEOUT_S) + TURN_IDLE_TIMEOUT_S)
    return TURN_IDLE_TIMEOUT_S


def _generation_error_outcome(stream: bool, err, first_token_seen: bool) -> Dict[str, Any]:
    if stream and _decoupled() and _is_timeout_error(err) and not first_token_seen:
        return {"kind": "error", "detail": f"first-token timeout after {float(LLM_FIRST_TOKEN_TIMEOUT_S):.0f}s (model still prefilling -- raise connection.first_token_timeout_s)"}
    return {"kind": "error", "detail": f"API: {err}"}


def _finalize_outcome(outcome: Dict[str, Any], watchdog_tripped: bool, first_token_seen: bool) -> Dict[str, Any]:
    if watchdog_tripped and outcome.get("kind") != "error":
        return {"kind": "stalled", "detail": f"no progress for {_idle_limit(first_token_seen):.0f}s"}
    return outcome


def _should_persist_partial(
    decoupled: bool, watchdog_tripped: bool, first_token_seen: bool, in_think: Optional[bool]
) -> bool:
    return decoupled and watchdog_tripped and first_token_seen and in_think is False


def _partial_assistant_message(content: str) -> Optional[Dict[str, str]]:
    cleaned = TOOLCALL_RE.sub("", clean(content))
    tool_call_start = cleaned.find("<tool_call>")
    if tool_call_start != -1 and cleaned.find("</tool_call>", tool_call_start) == -1:
        cleaned = cleaned[:tool_call_start]
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return {"role": "assistant", "content": cleaned}
# Engine machinery lives hidden under STATE_ROOT (initiative-independent): session.json, the
# local warm cache (memory/), config.json/active (ITYPE). Relative to WORKDIR (after chdir = CWD),
# overridable via cfg["paths"]["state_root"] (default ".ironclad") — absolute too. Boundary
# clean (no private literal). Helpers: state_root() / session_path().
STATE_ROOT       = ".ironclad"
SESSION_FILE     = "session.json"   # basename, resolved under STATE_ROOT (was ".gx10_session.json" at the root)
# Visible knowledge root (Obsidian-navigable): vault/<slug>/ per initiative. Engine machinery
# is STATE_ROOT, KNOWLEDGE is VAULT_ROOT — strictly separated. Overridable via cfg["paths"]["vault_root"].
VAULT_ROOT       = "vault"
# S? (#1237): the software tree lives under this subdir of the project so the product sources are ISOLATED
# from the ironclad control-plane (vault/, .ironclad/, tasks/). Governs MODEL-driven execution only (code-
# tools, execute_command, the launched code-agent — via _exec_cwd); the control-plane keeps resolving to the
# project root. Empty ⇒ off / byte-identical (execution at the project root). Overridable via
# cfg["paths"]["code_subdir"] (DEV-1 sets "src").
CODE_SUBDIR      = ""
SPINNER_FRAMES   = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
UI_REFRESH_INTERVAL = 0.1        # prompt_toolkit Application refresh

# Platform mode: determines shell + command syntax in execute_command.
# PLATFORM_MODE is the config value ("auto" is resolved at startup);
# PLATFORM is the EFFECTIVE mode ("windows" | "linux"), never "auto".
PLATFORM_MODE = "auto"           # "auto" | "windows" | "linux"
PLATFORM      = "windows" if os.name == "nt" else "linux"

# Task management (TaskStore): threshold for deterministic topic dedup.
TASKS_DEDUP_THRESHOLD = 0.8      # Jaccard over title+description

# Task ID prefix (vessel-configurable via tasks.id_prefix). IDs are
# {prefix}-N (monotonic). Default "KGC" keeps the existing behaviour; the
# example IDs in the tool descriptions still name the default prefix.
TASK_PREFIX = "KGC"

# ─── ACK (Agent-Contract-Kernel) integration ──────────────────
# Validates every model-emitted task_json at the stage_handover boundary against the
# ACK contract (ack.case_spec). On a violation the exact error is
# returned → the agent loop hands it back to the model as a tool result (reask),
# nothing is created. LODESTAR_ENABLED → CapabilityTaskSpec (capability mandatory for
# buildable types). Lodestar is an optional stricter schema selector; base ACK
# validation is always on.
LODESTAR_ENABLED = False
# #1073: forge (code-host) issue-filing. Explicitly enabled and capability-detected — offered only when
# forge.enabled=true and the selected transport is usable. It is blocked under the sealed profile (no
# autonomous outbound writes) — see _forge_available().
# FORGE_REPO is optional (empty ⇒ the gh CLI's default repo for the cwd); never a repo literal baked into core.
FORGE_ENABLED    = bool(config_schema.LEAVES["forge.enabled"].default)
FORGE_REPO       = ""
# #1213 (epic #1212): the forge adapter seam — `cli` (default, the ambient `gh` CLI, byte-identical) |
# `native` (a stdlib-urllib GitHub client, so the forge tools work with NO `gh` on the box, e.g. the Spark) |
# `mock`. The native token is read name-indirectly from the env var NAMED here (never a secret literal in core).
FORGE_ADAPTER    = "cli"
FORGE_TOKEN_ENV  = "GX10_FORGE_TOKEN"
# #1221: generic cross-model second-opinion review. CAPABILITY-DETECTED — offered when a code-agent
# binary resolves on this box (the reviewer runs via client.default_cli_runner; no new backend).
# Config: review.agent (default reviewer; empty ⇒ anti-affinity pick) + review.timeout_s.
REVIEW_AGENT     = ""            # agent_id from code_agents.pool; empty ⇒ distinct-peer pick (#457 SOFT)
REVIEW_TIMEOUT_S = 180.0         # bounded synchronous call (single agent-lock; never a watch/poll)
_REVIEW_MATERIAL_CAP = 80_000    # char cap on assembled material before the reviewer prompt
# #1083: outbound escalation notification. A HUMAN_ESCALATION fires the `escalation` hook; when a webhook is
# configured (deploy secret via GX10_NOTIFY_WEBHOOK / notify.webhook — NEVER a URL literal in core) the
# notifier POSTs it to an off-duty human. Empty ⇒ no consumer registered (byte-identical default-off).
NOTIFY_WEBHOOK   = ""
# #1084: mandatory per-action audit ledger. Mutations append hash-chained intent and result records under
# STATE_ROOT/audit/ledger.jsonl. Boundary-clean (a core-owned ledger, not the private dev-process one).
AUDIT_SCOPE      = "mutating"   # #1067: "mutating" (write/exec/create — #1084) | "all" (every tool call)
_AUDIT_DEGRADED  = False
SANDBOX          = "auto"       # #1464: mandatory OS exec sandbox policy — auto | bwrap | firejail
MULTI_TENANT     = False        # #1071: per-principal RBAC + tenant memory isolation — default OFF (single-tenant)
# #1061: alerting pipeline. When enabled AND a notify webhook (#1083) is configured, a periodic self-scan
# pages the telemetry SLO/anomaly (#1060) to the webhook, and an inbound POST /alert receiver pages external
# alerts. Default OFF (paging is a deliberate opt-in). No endpoint literal in core (the webhook is #1083's).
ALERT_ENABLED    = False

# Onboarding mode: proactive duplicate pre-check BEFORE the (expensive) handover.
# Default off (store dedup guarantees correctness anyway). Helpful when
# migrating from another CLI / with many legacy tasks. When active, the
# `check_task_exists` tool is offered and the prompt instructs to pre-check.
ONBOARDING_MODE = False

# Autopilot (Path B): for pending tasks with a handover the reconciler
# automatically starts `claude --print` (API-free execution) and moves pending →
# in_progress. Default OFF; any permission bypass is an explicit per-agent capability.
AUTOPILOT_ENABLED        = False
AUTOPILOT_CLAUDE_BIN     = "claude"
AUTOPILOT_EXTRA_ARGS     = list(config_schema.LEAVES["autopilot.extra_args"].default)
AUTOPILOT_DEFAULT_EFFORT = "medium"
AUTOPILOT_LOGS_DIR       = "logs"     # resolved under state_root() (.ironclad/logs); absolute path verbatim
AUTOPILOT_MAX_CONCURRENT = int(config_schema.LEAVES["autopilot.max_concurrent"].default)
AUTOPILOT_STREAM         = False        # live log streaming (claude --verbose --output-format stream-json); default OFF
AUTOPILOT_TERMINATE_ON_ADVANCE = False  # terminate the associated claude session on advance; default OFF
AUTOPILOT_AUTOPLAN       = False   # after an empty queue, have GX10 automatically plan the next task; default OFF
AUTOPILOT_MAX_TASKS      = int(config_schema.LEAVES["autopilot.autoplan_max_tasks"].default)
_AUTOPLAN_DONE           = 0       # session counter (touched only in the agent_thread → no lock needed)
# ROUTE-2 (#503): removed the dead `_TURN_DID_ADVANCE` guard — it was only reset, never set or read, so it
# never blocked anything (its comment claimed otherwise). The actual auto-plan control is AUTOPILOT_AUTOPLAN.
AUTOPILOT_LOG_TERMINAL   = False        # on every autopilot start open a new terminal with Get-Content -Wait; default OFF
# Kimi was replaced by Sonnet on 2026-06-15. "KIMI" remains only as a
# legacy alias and is transparently normalized to SONNET everywhere
# (Claude Code CLI + claude-sonnet-5). No Kimi CLI plumbing anymore.
WATCHER_FEEDBACK_DIR = "feedback"   # name of the feedback inbox under <initiative>/.work/ (B3); overridable via watcher.feedback_dir
API_KEY_ENV      = "GX10_API_KEY"             # secrets only from env, never from a file

# Workspace structure (created by _ensure_dirs at WORKDIR). B3: the functional artefact
# directories (tasks/, handovers/feedback, reviews …) are NO LONGER here — they live per
# initiative under vault/<slug>/ (skeleton via initiative_new). At WORKDIR only the visible
# knowledge root vault/ remains; engine machinery lives hidden under state_root(). Overridable
# via config (workspace.dirs).
WORKSPACE_DIRS = [
    "vault",
]
# Note: the local warm cache (memory/) is NO LONGER an artefact directory — it is
# engine machinery and lives under state_root()/"memory" (created by _ensure_dirs).


def _project_root() -> Optional[Path]:
    """The active project's root (ADR-0011 AD-1 / S3), or None when no project is active — in which case
    the path accessors fall back to the legacy WORKDIR-relative globals (byte-unchanged pre-switch)."""
    pc = _pc.current() if _pc is not None else None
    return Path(pc.root) if (pc is not None and pc.root) else None


_TRACK_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_track(t: object) -> bool:
    """A track id may name a directory segment — reject traversal / separators (defence in depth;
    the binding is trusted, but the active track reaches the filesystem via ``vault_root``). Crash-safe
    for a non-str input (→ False), mirroring ``project_context._safe_track`` so the vault subtree and the
    memory sub-scope agree on the effective track for EVERY input."""
    return isinstance(t, str) and t not in (".", "..") and _TRACK_RE.match(t) is not None


def _active_track() -> str:
    """The active track within the project (ADR-0011 AD-2'), from the ProjectContext, or ``"main"``
    when no project is active or the bound track is unsafe. ``"main"`` is the default track and
    resolves byte-identically to the pre-track vault layout (no surprise relocation)."""
    pc = _pc.current() if _pc is not None else None
    t = (pc.track if pc is not None else "") or "main"
    return t if _is_safe_track(t) else "main"


def state_root() -> Path:
    """Root of the hidden engine machinery (initiative-independent): session.json, the local
    warm cache (memory/), config.json/active. Relative to WORKDIR (after chdir), overridable via
    cfg["paths"]["state_root"] (default ``.ironclad``); absolute overrides are taken
    unchanged. Boundary clean — no private literal. Under an active ProjectContext a RELATIVE
    state_root is resolved against the project root (S3); an absolute override is still taken as-is."""
    p = Path(STATE_ROOT)
    root = _project_root()
    if root is not None and not p.is_absolute():
        return root / p
    return p


def session_path() -> Path:
    """Path of the session file: ``state_root()/SESSION_FILE``. An absolutely configured
    SESSION_FILE is used unchanged (backward compatibility)."""
    p = Path(SESSION_FILE)
    return p if p.is_absolute() else state_root() / p


def vault_root() -> Path:
    """Visible knowledge root (initiative-centric): ``vault/<slug>/`` per initiative. Relative to
    WORKDIR (after chdir), overridable via cfg["paths"]["vault_root"] (default ``vault``). Under an
    active ProjectContext a RELATIVE vault_root is resolved against the project root (S3).

    Per-track (ADR-0011 AD-2'): a non-``"main"`` active track is isolated under a hidden
    ``.tracks/<track>/`` subtree of the project vault, so each track gets its own first-class vault
    subtree. The default ``"main"`` track resolves byte-identically to the pre-track layout (and a
    single-track install is unchanged). Track isolation needs no exclusion logic: every vault op is
    slug-scoped (``vault_root()/<slug>``) or a one-level ``*/meta.md`` scan, and the ``.tracks`` dir
    carries no ``meta.md`` of its own."""
    p = Path(VAULT_ROOT)
    root = _project_root()
    if root is not None and not p.is_absolute():
        p = root / p
    track = _active_track()
    if track != "main":
        p = p / ".tracks" / track
    return p


def _display_doc_path(rel: str) -> str:
    """#1276: render a vault-root-relative doc path in the OPERATOR's frame — project-root-relative
    (e.g. ``vault/<slug>/decisions/design.md``), so it resolves from where the operator's shell runs. The
    internal vault-root-relative value stays for the design gate's own resolution; only what the operator
    SEES is reframed. Falls back to the input on any failure (a display reframe must never break a caller)."""
    try:
        vr = vault_root()
        root = _project_root()
        if root is not None and vr.is_absolute():
            prefix = vr.relative_to(root)          # e.g. `vault` (or `vault/.tracks/<track>`)
        elif not vr.is_absolute():
            prefix = vr                            # default project: vault_root is already workdir-relative
        else:
            return rel
        return (prefix / rel).as_posix()
    except Exception:  # noqa: BLE001
        return rel


_vault_lock_tl = threading.local()


def _resolve_vault_lock():
    """A ``FileLock`` for the active project+track's vault, or ``None`` when locking is unavailable.
    Uses the registry's per-project+track ``vault_lock`` (under the installation home ``ironclad_home()``
    = the ``GX10_HOME`` env var or a per-user default, AD-6) when a project is active; otherwise (legacy /
    no-ctx single-project) a lock under the project-scoped
    state root. Distinct from the dev-loop ``project_lock`` so a quick reconcile is never mistaken for
    an in-flight dev unit. Fail-soft: any resolution hiccup → ``None`` (no lock, write proceeds)."""
    track = _active_track()
    pc = _pc.current() if _pc is not None else None
    if _REGISTRY is not None and pc is not None and getattr(pc, "project_id", ""):
        try:
            return _REGISTRY.vault_lock(pc.project_id, track)
        except Exception:   # noqa: BLE001 — locking is best-effort, never blocks a write
            return None
    try:
        from project_registry import FileLock   # lazy: keep the module-top import surface unchanged
        lp = state_root() / "locks" / f"vault__{track}.lock"
        return FileLock(lp)
    except Exception:   # noqa: BLE001
        return None


@contextmanager
def _vault_lock():
    """Serialize vault mutation for the active project+track (Codex S3 / #601 S12b). **Reentrant**
    within a call stack — a nested writer (e.g. ``initiative_new`` → ``reconcile_vault``) does not
    re-acquire, so there is no self-deadlock; cross-process / cross-thread it serializes via the OS
    file lock. **Fail-soft**: a locking-infra hiccup never blocks a vault write (best-effort
    serialization, not a hard gate)."""
    if getattr(_vault_lock_tl, "depth", 0) > 0:
        yield
        return
    lk = _resolve_vault_lock()
    acquired = False
    if lk is not None:
        try:
            lk.acquire()
            acquired = True
        except Exception:   # noqa: BLE001 — best-effort; proceed unserialized rather than fail the write
            lk = None
    _vault_lock_tl.depth = 1
    try:
        yield
    finally:
        _vault_lock_tl.depth = 0
        if acquired:
            try:
                lk.release()
            except Exception:   # noqa: BLE001
                pass


# ─── Initiative (initiative-centric vault) ────────────────────
# An initiative = one visible knowledge/work unit under vault/<slug>/. meta.md (flat
# frontmatter) is the SSOT; the ONE active initiative is stored as a slug in state_root()/active.
# Artefact-producing ops (tasks, handovers, decisions, MPR runs) route relative to the active
# initiative (B3) — fail-closed without an active initiative. Pure conversation turns need none.
INITIATIVE_TYPES = ("software",)

# Hidden machine plumbing per initiative (hybrid layout, 06-20): active.md + handover/
# feedback inbox + history live under <initiative>/.work/ (out of sight); the visible
# artefacts (decisions/ proposals/ reviews/ runs/ tasks/) stay navigable on top.
WORKFLOW_DIR = ".work"

# Skeleton directories per type (relative to vault/<slug>/). software = task pipeline +
# file-communication plumbing + a runs/ home for the EMBEDDED MPR (the off-hot-path
# architecture-decision panel writes its run artefacts here — MPR is a dev-process function,
# never a project type of its own).
_INITIATIVE_SKELETON: Dict[str, List[str]] = {
    "software": ["tasks/pending", "tasks/in_progress", "tasks/done",
                 "decisions", "proposals", "reviews", "runs",
                 f"{WORKFLOW_DIR}/handovers", f"{WORKFLOW_DIR}/feedback",
                 f"{WORKFLOW_DIR}/archive/handovers", f"{WORKFLOW_DIR}/archive/feedback"],
}

# Umlaut folding for readable ASCII slugs (ä→ae …). Pure convenience; Unicode slugs would work too.
_SLUG_UMLAUT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}

# The "no active initiative" message is localized at call time (engine/messages.py: init.no_active) —
# a module constant would freeze the language at import; use _msg("init.no_active") at each use site.


def _slugify(name: str) -> str:
    """Kebab-case slug from an initiative name (LLM-free, deterministic). German umlauts
    are folded (ä→ae …); every run of non-[a-z0-9] (spaces, path separators, other
    punctuation, non-folded accents) becomes a SINGLE ``-``. Never empty."""
    s = (name or "").strip().lower()
    s = "".join(_SLUG_UMLAUT.get(c, c) for c in s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "initiative"


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """Minimal, flat YAML frontmatter parser (``key: value`` between ``---`` lines).
    LLM-free; shared by initiative meta AND reconcile_vault (Unit C). Values stay strings
    (incl. raw lists like ``[a, b]``); empty/missing block → ``{}``."""
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: Dict[str, str] = {}
    for s in lines[1:]:
        if s.strip() == "---":
            break
        if ":" in s and not s.lstrip().startswith("#"):
            k, _, v = s.partition(":")
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def _msg(key: str, **fmt: object) -> str:
    """Localized engine chrome (engine/messages.py): English is the source/default; the active
    language is ``gx10.LANGUAGE`` (config ``generation.language`` / ``GX10_LANGUAGE``), so the public
    export defaults to English with a DE overlay (no hardcoded German in core/). Lazy sibling import."""
    from messages import msg   # noqa: PLC0415 — sibling engine module; lazy keeps import order clean
    return msg(key, **fmt)


class Initiative:
    """Metadata + paths of an initiative. Persistence lives in vault/<slug>/meta.md (SSOT)."""

    __slots__ = ("slug", "type", "title", "created", "status")

    def __init__(self, slug: str, type: str, title: str, created: str, status: str = "active"):
        self.slug     = slug
        self.type      = type
        self.title    = title
        self.created = created
        self.status   = status

    @property
    def path(self) -> Path:
        return vault_root() / self.slug

    @property
    def meta_path(self) -> Path:
        return self.path / "meta.md"

    def to_meta(self) -> str:
        return (
            "---\n"
            f"type: {self.type}\n"
            f"title: {self.title}\n"
            f"created: {self.created}\n"
            f"status: {self.status}\n"
            "---\n\n"
            f"# {self.title}\n\n"
            f"_{_msg('init.meta_body', type=self.type, path=f'{vault_root().as_posix()}/{self.slug}')}_\n"
        )

    @classmethod
    def from_meta(cls, slug: str, meta_path: Optional[Path] = None) -> "Initiative":
        p = Path(meta_path) if meta_path else (vault_root() / slug / "meta.md")
        fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
        # Forward-safety (#984): degrade an unknown/legacy type (e.g. a pre-#984 `type: mpr`) to the
        # single supported type so an old vault loads without a KeyError on the skeleton lookup.
        _t = (fm.get("type") or "").strip().lower()
        return cls(
            slug=slug,
            type=_t if _t in INITIATIVE_TYPES else "software",
            title=fm.get("title", slug),
            created=fm.get("created", ""),
            status=fm.get("status", "active"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"slug": self.slug, "type": self.type, "title": self.title,
                "created": self.created, "status": self.status,
                "path": self.path.as_posix()}


def _active_path() -> Path:
    return state_root() / "active"


def active_slug() -> Optional[str]:
    """Slug of the active initiative from state_root()/active, or None."""
    p = _active_path()
    try:
        return (p.read_text(encoding="utf-8").strip() or None) if p.exists() else None
    except Exception:
        return None


def set_active_slug(slug: str) -> None:
    p = _active_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((slug or "").strip() + "\n", encoding="utf-8", newline="\n")


def initiative_exists(slug: str) -> bool:
    return bool(slug) and (vault_root() / slug / "meta.md").is_file()


def initiative_get(slug: str) -> Optional[Initiative]:
    if not initiative_exists(slug):
        return None
    try:
        return Initiative.from_meta(slug)
    except Exception:
        return None


def initiative_list() -> List[Initiative]:
    """All initiatives (sorted by slug). Broken meta.md files are silently skipped."""
    root = vault_root()
    if not root.exists():
        return []
    out: List[Initiative] = []
    for meta in sorted(root.glob("*/meta.md")):
        try:
            out.append(Initiative.from_meta(meta.parent.name, meta))
        except Exception:
            continue
    return out


def initiative_new(name: str, type: str = "software") -> Initiative:
    """Creates a new initiative (meta.md + type skeleton), sets it active. Colliding slugs
    get a -N suffix. Unknown type / empty name → ValueError. ``software`` is the only type
    (MPR is an embedded dev-process function, not a project type)."""
    t = (type or "").strip().lower()
    if t not in INITIATIVE_TYPES:
        raise ValueError(_msg("init.unknown_type", type=type, allowed=", ".join(INITIATIVE_TYPES)))
    title = (name or "").strip()
    if not title:
        raise ValueError(_msg("init.needs_name"))
    with _vault_lock():        # serialize the collision scan + creation (Codex S3 / S12b)
        base = _slugify(title)
        slug, n = base, 2
        while (vault_root() / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        v = Initiative(slug=slug, type=t, title=title,
                     created=time.strftime("%Y-%m-%d", time.localtime()), status="active")
        v.path.mkdir(parents=True, exist_ok=True)
        for d in _INITIATIVE_SKELETON[t]:
            (v.path / d).mkdir(parents=True, exist_ok=True)
        v.meta_path.write_text(v.to_meta(), encoding="utf-8", newline="\n")
        set_active_slug(slug)
        _reconcile_active_soft()   # reentrant: already holds the vault lock
    return v


def initiative_use(slug: str) -> Initiative:
    """Sets an existing initiative active. Unknown slug → ValueError."""
    v = initiative_get((slug or "").strip())
    if v is None:
        raise ValueError(_msg("init.unknown_slug", slug=slug, root=vault_root().as_posix()))
    set_active_slug(v.slug)
    return v


def initiative_active() -> Optional[Initiative]:
    """The active initiative (or None, even when the active marker points at a deleted one)."""
    slug = active_slug()
    return initiative_get(slug) if slug else None


def active_initiative_path() -> Path:
    """Path of the active initiative — fail-closed (RuntimeError) without a valid active initiative.
    Source of artefact routing (B3): tasks/handovers/decisions/reviews/MPR runs land under it."""
    v = initiative_active()
    if v is None:
        raise RuntimeError(_msg("init.no_active"))
    return v.path


# ─── Artefact routing (B3) ────────────────────────────────────
# "file communication" (tasks/handovers/feedback/decisions/proposals/reviews/MPR runs) lives under
# the ACTIVE initiative instead of WORKDIR. Producing ops are fail-closed (active_initiative_path); background
# scanners (reconciler/watcher/autopilot) use the *_soft variant (None instead of an error → the daemon never
# crashes). Visible directly under <initiative>/, machine plumbing under <initiative>/.work/.
def artifact_root_soft() -> Optional[Path]:
    """Soft variant of active_initiative_path(): None without an active initiative."""
    v = initiative_active()
    return v.path if v else None


def _work(soft: bool = False) -> Optional[Path]:
    base = artifact_root_soft() if soft else active_initiative_path()
    return (base / WORKFLOW_DIR) if base is not None else None


def handovers_dir(soft: bool = False) -> Optional[Path]:
    """Handover inbox <initiative>/.work/handovers (staged by the orchestrator, pulled by the client)."""
    w = _work(soft=soft)
    return (w / "handovers") if w is not None else None


def feedback_dir(soft: bool = False) -> Optional[Path]:
    """Feedback inbox <initiative>/.work/<feedback> (filled by the local code agent, read by the reconciler).
    The inbox name is configurable via watcher.feedback_dir (default ``feedback``)."""
    w = _work(soft=soft)
    return (w / WATCHER_FEEDBACK_DIR) if w is not None else None


def active_md_path(soft: bool = False) -> Optional[Path]:
    """Active handover <initiative>/.work/active.md (pure projection, never maintain by hand)."""
    w = _work(soft=soft)
    return (w / "active.md") if w is not None else None


def archive_handovers_dir(soft: bool = False) -> Optional[Path]:
    """Handover history <initiative>/.work/archive/handovers."""
    w = _work(soft=soft)
    return (w / "archive" / "handovers") if w is not None else None


def archive_feedback_dir(soft: bool = False) -> Optional[Path]:
    """Feedback history <initiative>/.work/archive/feedback."""
    w = _work(soft=soft)
    return (w / "archive" / "feedback") if w is not None else None


# ─── Self-maintaining vault (Unit C): reconcile_vault, LLM-free ──
# Scans vault/<slug>/**/*.md (excluding INDEX.md and the hidden .work/), parses frontmatter and
# builds an AUTO-maintained INDEX.md (grouped by category/date, Obsidian [[links]]) plus —
# optionally — an idempotent "Verwandt (auto)" (related) block in the curated docs (shared tags /
# title reference in the text). Deterministic, no model call. Like the MEMORY.md pattern.
_INDEX_AUTO_START = "<!-- ironclad:index:auto START -->"   # #1265: English + description-less, consistent with the board/lifecycle/related markers
_INDEX_AUTO_END   = "<!-- ironclad:index:auto END -->"
# #1265: an INDEX.md written before #1265 carries the legacy (German, descriptive) START marker. reconcile_vault
# normalizes ANY prior `ironclad:index:auto START …` marker to the current one BEFORE the managed-block rewrite,
# so an existing vault's block is rewritten IN PLACE (never duplicated); it also matches the new marker → idempotent.
_INDEX_LEGACY_START_RE = re.compile(r"<!-- ironclad:index:auto START.*?-->")
_LINKS_AUTO_START = "<!-- ironclad:related:auto START -->"
_LINKS_AUTO_END   = "<!-- ironclad:related:auto END -->"
# S12c: a machine-readable typed-edge graph (GRAPH.json) + a human LIFECYCLE.md view, both generated
# next to INDEX.md. The HTML markers above stay FROZEN; LIFECYCLE.md adds its own managed-block markers.
_LIFECYCLE_AUTO_START = "<!-- ironclad:lifecycle:auto START -->"
_LIFECYCLE_AUTO_END   = "<!-- ironclad:lifecycle:auto END -->"
# S6 (#1228 / R5): the central task board — a human-readable, LLM-free projection of the TaskStore (all units
# grouped pending/in_progress/done), managed block next to INDEX/LIFECYCLE. Frozen markers.
_BOARD_AUTO_START  = "<!-- ironclad:board:auto START -->"
_BOARD_AUTO_END    = "<!-- ironclad:board:auto END -->"
GRAPH_FILENAME     = "GRAPH.json"
LIFECYCLE_FILENAME = "LIFECYCLE.md"
BOARD_FILENAME     = "BOARD.md"
# Doc categories (first path segment) that get a "Verwandt" (related) block — curated knowledge,
# NOT the auto-generated MPR runs/ and not the meta.md.
_LINK_CATEGORIES  = {"decisions", "proposals", "reviews", "(root)"}
# Typed frontmatter edges (allowlist). Each value is a flat list (``[a, b]`` or ``a, b``) of doc
# targets (a relpath, a stem, or a bare filename stem). Unknown keys are ignored; an unresolvable
# target is recorded as a *dangling* edge (honest, not dropped).
_EDGE_TYPES = ("depends_on", "refines", "supersedes", "relates_to", "implements", "blocks")
# Composable lifecycle stages (S12d), in canonical order. A doc declares its stage via a flat
# ``stage:`` frontmatter field; the initiative's lifecycle is COMPOSED from its docs' stages
# (``lifecycle_state``). The order defines the transition guard (``can_advance_stage``) and the
# completeness notion the DELIVER leg (AD-7) / the ``/lifecycle`` command (S16) consume.
LIFECYCLE_STAGES = ("idea", "design", "adr", "spec", "tests", "proposals", "reviews", "delivery")
_STAGE_INDEX = {s: i for i, s in enumerate(LIFECYCLE_STAGES)}


def is_lifecycle_stage(s: object) -> bool:
    """True iff *s* is a known lifecycle stage. Crash-safe for non-str / unhashable input (→ False)."""
    return isinstance(s, str) and s in _STAGE_INDEX


def can_advance_stage(frm: object, to: object, *, allow_regress: bool = False) -> bool:
    """Transition guard (fully fail-closed): may a unit move from stage *frm* to stage *to*?
    *to* must be a known stage; only the **empty string** *frm* (no stage yet) admits any valid *to*;
    a non-str / unknown *frm* or *to* is refused (so ``None``/``[]`` can never slip through). Otherwise
    the move must be forward (``to`` at or after ``frm`` in :data:`LIFECYCLE_STAGES`) unless
    *allow_regress*. The reusable primitive the ``/lifecycle`` command (S16) and the DELIVER-leg
    completeness gate (AD-7 / S17) build on."""
    if not is_lifecycle_stage(to):
        return False
    if frm == "":
        return True
    if not is_lifecycle_stage(frm):
        return False
    return allow_regress or _STAGE_INDEX[to] >= _STAGE_INDEX[frm]


def lifecycle_state(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The COMPOSED lifecycle of an initiative, derived deterministically from its docs' ``stage`` fields
    (S12d). ``present`` = the known stages that have at least one artifact (in canonical order);
    ``current`` = the furthest-along present stage; ``gaps`` = required earlier stages (before
    ``current``) with no artifact; ``complete`` = a current stage with no gaps; ``unknown`` = declared
    stages that are not in the allowlist. LLM-free, no timestamp (idempotent)."""
    counts: Dict[str, int] = {}
    unknown: List[str] = []
    for d in docs:
        s = (d.get("stage") or "").strip()
        if not s:
            continue
        if s in _STAGE_INDEX:
            counts[s] = counts.get(s, 0) + 1
        elif s not in unknown:
            unknown.append(s)
    present = [s for s in LIFECYCLE_STAGES if counts.get(s)]
    current = present[-1] if present else ""
    gaps = [s for s in LIFECYCLE_STAGES[:_STAGE_INDEX[current]] if not counts.get(s)] if current else []
    return {
        "stages": list(LIFECYCLE_STAGES),
        "present": present,
        "current": current,
        "gaps": gaps,
        "complete": bool(current) and not gaps,
        "counts": {s: counts[s] for s in present},
        "unknown": sorted(unknown),
    }


def _parse_tags(raw: str) -> set:
    """Tags from a flat frontmatter value: ``[a, b]`` or ``a, b`` → {a, b} (lowercase)."""
    s = (raw or "").strip().strip("[]")
    return {t.strip().strip("'\"").lower() for t in re.split(r"[,\s]+", s) if t.strip()}


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.*\S)", line)
        if m:
            return m.group(1).strip()
    return ""


def _set_managed_block(text: str, start: str, end: str, block: Optional[str]) -> str:
    """Idempotent, marked block: ``block`` replaces an existing block (or is appended);
    ``block is None`` removes it. Replacement via a function → no backref interpretation in the replacement."""
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    if block is None:
        new = pat.sub("", text)
        return (new.rstrip() + "\n") if new.strip() else ""
    if pat.search(text):
        return pat.sub(lambda _m: block, text)
    return (text.rstrip() + "\n\n" + block + "\n") if text.strip() else (block + "\n")


def _parse_edge_targets(raw: str) -> List[str]:
    """Doc targets from a flat frontmatter value: ``[a, b]`` or ``a, b`` → ['a', 'b'] (comma-separated,
    order-preserving, case-insensitively deduped). Targets may be relpaths (with ``/``), so do NOT split
    on whitespace."""
    s = (raw or "").strip().strip("[]")
    out: List[str] = []
    seen: set = set()
    for t in s.split(","):
        t = t.strip().strip("'\"")
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _doc_edges(fm: Dict[str, str]) -> Dict[str, List[str]]:
    """Typed edges declared in a doc's frontmatter (only the allowlisted, non-empty ones)."""
    out: Dict[str, List[str]] = {}
    for et in _EDGE_TYPES:
        targets = _parse_edge_targets(fm.get(et, ""))
        if targets:
            out[et] = targets
    return out


def _vault_docs(vdir: Path) -> List[Dict[str, Any]]:
    """Indexable docs under the initiative (excluding the generated INDEX.md / LIFECYCLE.md and the
    hidden .work/), with metadata + typed frontmatter edges."""
    out: List[Dict[str, Any]] = []
    for p in sorted(vdir.rglob("*.md")):
        rel = p.relative_to(vdir)
        if p.name in ("INDEX.md", LIFECYCLE_FILENAME, BOARD_FILENAME) or (rel.parts and rel.parts[0] == WORKFLOW_DIR):
            continue
        try:
            text, _size = _read_text_capped(p)
        except OSError:
            continue
        if text is None:
            continue
        fm = _parse_frontmatter(text)
        out.append({
            "rel": rel, "stem": rel.with_suffix("").as_posix(), "path": p,
            "title": fm.get("title") or _first_heading(text) or p.stem,
            "type": fm.get("type", ""), "status": fm.get("status", ""),
            "date": fm.get("created") or fm.get("date") or "",
            "tags": _parse_tags(fm.get("tags", "")),
            "category": rel.parts[0] if len(rel.parts) > 1 else "(root)",
            "stage": (fm.get("stage", "") or "").strip(),
            "approved": (fm.get("approved", "") or "").strip(),       # S5 (#1227): design-gate approval flag
            "tree_sha": (fm.get("tree_sha", "") or "").strip(),       # S13: evidence-doc provenance
            "edges": _doc_edges(fm),
            "text": text,
        })
    return out


def _index_block(slug: str, docs: List[Dict[str, Any]]) -> str:
    lines = [_INDEX_AUTO_START,
             f"_{_msg('vault.index_auto', n=len(docs))}_", ""]
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for d in docs:
        by_cat.setdefault(d["category"], []).append(d)
    for cat in sorted(by_cat):
        lines.append(f"## {cat}")
        items = sorted(by_cat[cat], key=lambda x: x["title"].lower())
        items = sorted(items, key=lambda x: x["date"], reverse=True)   # newest first, stable by title
        for d in items:
            bits = " · ".join(b for b in (d["type"], d["status"], d["date"]) if b)
            lines.append(f"- [[{d['stem']}|{d['title']}]]" + (f"  ({bits})" if bits else ""))
        lines.append("")
    lines.append(_INDEX_AUTO_END)
    return "\n".join(lines)


def _related_docs(d: Dict[str, Any], docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Related = shared tags OR a title reference in the text. Self-reference + already-injected
    related blocks excluded (otherwise the set would grow on every run → not idempotent)."""
    clean = _set_managed_block(d["text"], _LINKS_AUTO_START, _LINKS_AUTO_END, None).lower()
    rel: List[Dict[str, Any]] = []
    for o in docs:
        if o["stem"] == d["stem"]:
            continue
        shared = bool(d["tags"] & o["tags"])
        title_ref = bool(o["title"]) and len(o["title"]) >= 4 and o["title"].lower() in clean
        if shared or title_ref:
            rel.append(o)
    return rel


def build_graph(slug: str, docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The machine SSOT graph (S12c): nodes keyed by **full relpath**, plus the typed edges declared in
    each doc's frontmatter. Deterministic + LLM-free: nodes are emitted in relpath order, tags sorted,
    edges sorted, and there is **no timestamp** (so a re-run with unchanged docs is byte-identical →
    idempotent). An edge target is resolved (against relpath / stem / bare filename stem) to the target's
    relpath; an unresolvable target is kept verbatim and flagged ``resolved=false`` (dangling)."""
    by_key: Dict[str, str] = {}
    for d in sorted(docs, key=lambda x: x["rel"].as_posix()):
        rp = d["rel"].as_posix()
        for key in (rp, d["stem"], d["rel"].stem):       # first (relpath-sorted) doc wins a shared key
            by_key.setdefault(key, rp)
    nodes: Dict[str, Any] = {}
    edges: List[Dict[str, Any]] = []
    seen: set = set()
    for d in sorted(docs, key=lambda x: x["rel"].as_posix()):
        rp = d["rel"].as_posix()
        nodes[rp] = {"title": d["title"], "type": d["type"], "status": d["status"],
                     "date": d["date"], "category": d["category"], "stage": d.get("stage", ""),
                     "tags": sorted(d["tags"])}
        for et in _EDGE_TYPES:
            for tgt in d.get("edges", {}).get(et, []):
                resolved = by_key.get(tgt)
                to = resolved or tgt
                key = (rp, et, to)
                if key in seen:        # aliases of the same target (e.g. stem + relpath) collapse to one edge
                    continue
                seen.add(key)
                edges.append({"from": rp, "type": et, "to": to, "resolved": resolved is not None})
    edges.sort(key=lambda e: (e["from"], e["type"], e["to"]))
    return {"version": 1, "slug": slug, "generator": "reconcile_vault",
            "lifecycle": lifecycle_state(docs), "nodes": nodes, "edges": edges}


def _graph_json(graph: Dict[str, Any]) -> str:
    """Deterministic JSON for GRAPH.json (sorted keys + trailing newline) → stable diffs / idempotent."""
    import json
    return json.dumps(graph, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _lifecycle_block(graph: Dict[str, Any]) -> str:
    """A human-readable LIFECYCLE.md view of the graph (managed block, deterministic). Lists each node
    with its outgoing typed edges; a dangling edge is marked. The HTML markers are frozen."""
    nodes = graph["nodes"]
    edges = graph["edges"]
    lc = graph.get("lifecycle") or {}
    by_from: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges:
        by_from.setdefault(e["from"], []).append(e)
    lines = [_LIFECYCLE_AUTO_START,
             f"_{_msg('vault.lifecycle_auto', nodes=len(nodes), edges=len(edges))}_", ""]
    # S12d: the composed lifecycle summary (current stage + gaps + present-stage progression)
    if lc:
        current = lc.get("current") or "(none)"
        complete = "complete" if lc.get("complete") else "incomplete"
        lines.append(f"## Lifecycle — {current} ({complete})")
        present = lc.get("present") or []
        if present:
            lines.append("- present: " + ", ".join(f"{s} ({lc['counts'][s]})" for s in present))
        if lc.get("gaps"):
            lines.append("- gaps: " + ", ".join(lc["gaps"]))
        if lc.get("unknown"):
            lines.append("- unknown stages: " + ", ".join(lc["unknown"]))
        lines.append("")
    lines.append("## Graph")
    for rp in sorted(nodes):
        n = nodes[rp]
        bits = " · ".join(b for b in (n.get("stage"), n.get("type"), n.get("status"), n.get("date")) if b)
        lines.append(f"- `{rp}` — {n.get('title') or rp}" + (f"  ({bits})" if bits else ""))
        for e in by_from.get(rp, []):
            mark = "" if e["resolved"] else "  (dangling)"
            lines.append(f"    - {e['type']} -> `{e['to']}`{mark}")
    lines.append("")
    lines.append(_LIFECYCLE_AUTO_END)
    return "\n".join(lines)


def _render_board(slug: "Optional[str]" = None) -> str:
    """S6 (#1228 / R5): a human-readable, LLM-free task board — every unit grouped pending/in_progress/done,
    the central steering view. Deterministic + timestamp-free (idempotent re-render). Reads the TaskStore for
    *slug* (the active unit by default; a non-active slug via a slug-scoped store). Managed block, frozen
    markers. Rows: ``id`` · type · title · labels · parent · created_at."""
    slug = (slug or active_slug() or "").strip()
    store = _store() if slug and slug == active_slug() else TaskStore(root=str(vault_root() / slug))
    by_status = {st: sorted(store.list(st), key=lambda t: (t.get("created_at", ""), t.get("id", "")))
                 for st in TaskStore.STATUSES}
    counts = {st: len(by_status[st]) for st in TaskStore.STATUSES}
    # #1296: per-epic unit progress ("3/7 units done") — computed once over all rows so every
    # epic row can show how far its decomposition is.
    _all_rows = [t for rows in by_status.values() for t in rows]
    _epic_prog: Dict[str, str] = {}
    for t in _all_rows:
        if str(t.get("type") or "").lower() != "epic":
            continue
        eid = str(t.get("id") or "")
        kids = [k for k in _all_rows if str(k.get("parent") or "") == eid]
        if kids:
            n_done = sum(1 for k in kids if k.get("status") == "done")
            _epic_prog[eid] = f"units: {n_done}/{len(kids)} done"
    lines = [_BOARD_AUTO_START,
             "_" + f"{sum(counts.values())} task(s) — "
             + " · ".join(f"{st} {counts[st]}" for st in TaskStore.STATUSES) + "_", ""]
    for st in TaskStore.STATUSES:
        lines.append(f"## {st} ({counts[st]})")
        for t in by_status[st]:
            # Coerce EVERY field to str — a legacy pre-ACK task can carry non-string values
            # (labels:[1,2], type:7); a bare str.join would then raise
            # and silently kill /board. Defensive: str() each bit so a malformed unit renders instead of crashing.
            labels = t.get("labels") or []
            if isinstance(labels, str):
                labels = [x.strip() for x in labels.split(",") if x.strip()]
            elif not isinstance(labels, (list, tuple)):
                labels = [str(labels)]
            # S7 (#1229): a blocked/stalled in_progress task shows a ⚠ marker so the operator sees the stall on
            # the board instead of a healthy-looking row. Conditional on the field → byte-identical when absent.
            blocked_bit = ""
            if t.get("blocked"):
                blocked_bit = "⚠ " + str(t.get("blocked_kind") or "blocked").upper()
                if str(t.get("blocked_reason") or "").strip():
                    blocked_bit += f": {str(t.get('blocked_reason')).strip()}"
            bits = " · ".join(b for b in (
                str(t.get("type") or ""),
                str(t.get("title") or ""),
                (f"labels: {', '.join(str(x) for x in labels)}" if labels else ""),
                (f"parent: {t['parent']}" if t.get("parent") else ""),
                _epic_prog.get(str(t.get("id") or ""), ""),   # #1296: epic rows show unit progress
                blocked_bit,
                str(t.get("created_at") or ""),
            ) if b)
            tid = t.get("id", "?")
            lines.append(f"- `{tid}` · {bits}" if bits else f"- `{tid}`")
        lines.append("")
    lines.append(_BOARD_AUTO_END)
    return "\n".join(lines)


def _write_board(slug: "Optional[str]" = None) -> None:
    """Write/refresh ``<slug>/BOARD.md`` from :func:`_render_board` (idempotent — writes only on a real
    change). Vault-locked, FAIL-SOFT: never raises, never blocks a caller (a board hiccup must not fail a
    task op — the S6 backstop is folded into the existing soft-reconcile, adding no new hot-path failure)."""
    try:
        s = (slug or active_slug() or "").strip()
        if not s:
            return
        vdir = vault_root() / s
        if not (vdir / "meta.md").is_file():
            return
        with _vault_lock():
            doc = vdir / BOARD_FILENAME
            existing = doc.read_text(encoding="utf-8") if doc.is_file() else f"# {s} — task board\n\n"
            new = _set_managed_block(existing, _BOARD_AUTO_START, _BOARD_AUTO_END, _render_board(s))
            if new != existing:
                doc.write_text(new, encoding="utf-8", newline="\n")
    except Exception:   # noqa: BLE001 — the board is an aid; never let it break a write
        pass


def _board_command(arg: "Optional[str]" = None) -> str:
    """S6 (#1228): render the active (or named) unit's task board to BOARD.md and return it for display.
    Deterministic, model-free. Friendly message when there is no unit; fail-closed on an unknown slug."""
    slug = (arg or active_slug() or "").strip()
    if not slug:
        return "No active unit — create or switch to one first (/project), then /board."
    if not (vault_root() / slug / "meta.md").is_file():
        return f"ERROR: no unit {slug!r}."
    _write_board(slug)
    return _render_board(slug)


def reconcile_vault(slug: str, *, links: bool = True) -> str:
    """Maintains INDEX.md (always) + optionally the "Verwandt (auto)" (related) blocks (``links=True``) of an initiative.
    Deterministic, idempotent, LLM-free. ``links=False`` (auto-trigger) only keeps the index fresh and
    does not touch doc bodies (no conflict with an open editor)."""
    vdir = vault_root() / slug
    if not (vdir / "meta.md").is_file():
        return _msg("vault.no_initiative", slug=slug, root=vault_root().as_posix())
    with _vault_lock():        # serialize the index/related writes (Codex S3 / S12b)
        docs = _vault_docs(vdir)

        index_path = vdir / "INDEX.md"
        # Seed the H1 from the initiative's title (meta.md is guaranteed present above), not the slug —
        # keeps the overview header consistent with meta.md and the wikilink label. Fallback: slug.
        try:
            seed_title = Initiative.from_meta(slug).title or slug
        except Exception:
            seed_title = slug
        existing = index_path.read_text(encoding="utf-8") if index_path.exists() else f"# {seed_title} — INDEX\n"
        existing = _INDEX_LEGACY_START_RE.sub(_INDEX_AUTO_START, existing)   # #1265: migrate any legacy START marker in place
        new_index = _set_managed_block(existing, _INDEX_AUTO_START, _INDEX_AUTO_END, _index_block(slug, docs))
        if new_index != existing:
            index_path.write_text(new_index, encoding="utf-8", newline="\n")

        # S12c: the machine SSOT graph (GRAPH.json) + the human LIFECYCLE.md view — generated next to
        # INDEX.md in BOTH modes (they are generated files, never curated bodies). Idempotent: written
        # only when the content actually changes (GRAPH.json carries no timestamp).
        graph = build_graph(slug, docs)
        graph_path = vdir / GRAPH_FILENAME
        new_graph = _graph_json(graph)
        if (not graph_path.exists()) or graph_path.read_text(encoding="utf-8") != new_graph:
            graph_path.write_text(new_graph, encoding="utf-8", newline="\n")
        life_path = vdir / LIFECYCLE_FILENAME
        life_existing = (life_path.read_text(encoding="utf-8") if life_path.exists()
                         else f"# {seed_title} — LIFECYCLE\n")
        new_life = _set_managed_block(life_existing, _LIFECYCLE_AUTO_START, _LIFECYCLE_AUTO_END,
                                      _lifecycle_block(graph))
        if new_life != life_existing:
            life_path.write_text(new_life, encoding="utf-8", newline="\n")

        linked = 0
        if links:
            for d in docs:
                if d["category"] not in _LINK_CATEGORIES or d["rel"].name == "meta.md":
                    continue
                related = _related_docs(d, docs)
                if related:
                    items = sorted(related, key=lambda x: x["title"].lower())
                    body = "\n".join([_LINKS_AUTO_START, "", "## " + _msg("vault.related_heading"),
                                      *[f"- [[{o['stem']}|{o['title']}]]" for o in items],
                                      "", _LINKS_AUTO_END])
                    new = _set_managed_block(d["text"], _LINKS_AUTO_START, _LINKS_AUTO_END, body)
                else:   # no related docs → remove any existing block (tidy)
                    new = _set_managed_block(d["text"], _LINKS_AUTO_START, _LINKS_AUTO_END, None)
                if new != d["text"]:
                    d["path"].write_text(new, encoding="utf-8", newline="\n")
                    linked += 1
        suffix = (_msg("vault.related_suffix", n=linked) if links
                  else _msg("vault.index_only_suffix"))
        return _msg("vault.indexed", slug=slug, n=len(docs), suffix=suffix)


def _reconcile_active_soft(*, links: bool = False) -> None:
    """Auto-reconcile of the active initiative after a write (fail-soft, never raises).
    Default ``links=False`` → only keep INDEX.md fresh, doc bodies untouched."""
    try:
        s = active_slug()
        if s:
            reconcile_vault(s, links=links)
    except Exception:   # noqa: BLE001 — reconcile must never make a write fail
        pass
    _write_board()      # S6 (#1228): keep BOARD.md current on disk — own fail-soft guard, never disturbs the reconcile


# ─── Evidence projection + lifecycle-completeness gate (S13a / AD-7) ──────────
# A deterministic, append-only EVIDENCE projector: dev-process transitions are projected (by the
# PRIVATE DELIVER leg, S13b) into stage-tagged vault docs bound to a `tree_sha` + a `content_hash`,
# via the index-only reconcile path — NEVER rewriting curated bodies. The lifecycle-completeness gate
# (run in the engine DELIVER leg, not monorepo CI) verifies the required evidence stages are present
# AND all bound to the delivery tree_sha. Builds on the public S12 vault (stage frontmatter,
# lifecycle_state, _vault_lock).
EVIDENCE_TYPE = "evidence"
EVIDENCE_DIR = "evidence"


def project_evidence(stage: str, title: str, body: str, *, tree_sha: str,
                     content_hash: "Optional[str]" = None, slug: "Optional[str]" = None) -> str:
    """Append an evidence doc for *stage* into the active (or *slug*) initiative's vault, bound to
    *tree_sha* + a content hash (AD-7). The doc carries ``type: evidence`` + the lifecycle ``stage`` +
    ``tree_sha`` + ``content_hash`` frontmatter; it is written under ``<slug>/evidence/`` with a
    **deterministic** filename ``<stage>-<tree_sha[:12]>-<hash[:12]>.md``, so re-projecting the same
    (stage, tree_sha, body) is a **no-op** (idempotent, no timestamp). Append-only: it only ever writes
    NEW evidence files and never touches curated bodies. Fail-closed: an unknown *stage*, an empty
    *tree_sha*, or no resolvable initiative raises ``ValueError``. Returns the doc path (posix)."""
    import hashlib
    if not is_lifecycle_stage(stage):
        raise ValueError(f"unknown lifecycle stage: {stage!r}")
    if not isinstance(tree_sha, str) or not tree_sha.strip():
        raise ValueError("evidence requires a non-empty tree_sha")
    tree_sha = tree_sha.strip()
    # tree_sha + content_hash land in the on-disk FILENAME → must be hex-only (no separators / traversal).
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", tree_sha):
        raise ValueError("evidence requires a git-like hex tree_sha (7-64 hex chars)")
    target = (slug or active_slug() or "").strip()
    if not target:
        raise ValueError("no active initiative for evidence projection")
    # Normalize line endings so the rendered bytes match what read_text() returns on a re-run (the
    # comparison/hash would otherwise churn on CRLF input → broken idempotency).
    body = str(body).replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    chash = (content_hash or hashlib.sha256(body.encode("utf-8")).hexdigest()).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{12,64}", chash):
        raise ValueError("evidence content_hash must be hex (12-64 chars)")
    with _vault_lock():
        vdir = vault_root() / target
        if not (vdir / "meta.md").is_file():
            raise ValueError(_msg("vault.no_initiative", slug=target, root=vault_root().as_posix()))
        edir = vdir / EVIDENCE_DIR
        edir.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f"type: {EVIDENCE_TYPE}\n"
            f"stage: {stage}\n"
            f"title: {title}\n"
            f"tree_sha: {tree_sha}\n"
            f"content_hash: {chash}\n"
            "---\n\n"
            f"# {title}\n\n"
            f"{body}"
        )
        # Filename identity = the hash of the FULL rendered doc (incl. title) → any content change yields a
        # distinct file (strict append-only); identical content yields the same file (idempotent no-op).
        ident = hashlib.sha256(content.encode("utf-8")).hexdigest()
        doc = edir / f"{stage}-{tree_sha[:12]}-{ident[:12]}.md"
        if doc.exists():
            if doc.read_text(encoding="utf-8") != content:   # defensive: never rewrite an evidence doc
                raise ValueError(f"evidence collision with differing content: {doc.name}")
        else:
            doc.write_text(content, encoding="utf-8", newline="\n")
        try:
            reconcile_vault(target, links=False)   # index-only refresh (reentrant under the vault lock)
        except Exception:   # noqa: BLE001 — projection must not fail on a reconcile hiccup
            pass
        return (doc.relative_to(vault_root())).as_posix()


def lifecycle_completeness(slug: str, *, required_stages: "List[str]", tree_sha: str) -> "tuple[bool, List[str]]":
    """The lifecycle-completeness gate (S13a / AD-7), consumed by the engine DELIVER leg. Verifies that
    the *slug* initiative's vault carries an **evidence** doc for **every** stage in *required_stages*
    AND that every evidence doc for those stages is bound to the delivery *tree_sha* (evidence tree_sha
    == delivery tree_sha). Returns ``(ready, reasons)`` — ``ready`` iff there are no reasons. Fully
    fail-closed: an empty *tree_sha*, an unknown required stage, or a missing/mismatched evidence doc is
    a reason. Pure + deterministic."""
    reasons: List[str] = []
    if not isinstance(tree_sha, str) or not tree_sha.strip():
        return False, ["no delivery tree_sha"]
    tree_sha = tree_sha.strip()
    vdir = vault_root() / slug
    if not (vdir / "meta.md").is_file():
        return False, [f"no initiative {slug!r}"]
    evidence = [d for d in _vault_docs(vdir) if d.get("type") == EVIDENCE_TYPE]
    by_stage: Dict[str, List[Dict[str, Any]]] = {}
    for d in evidence:
        by_stage.setdefault(d.get("stage", ""), []).append(d)
    for stage in required_stages:
        if not is_lifecycle_stage(stage):
            reasons.append(f"unknown required stage: {stage!r}")
            continue
        docs = by_stage.get(stage, [])
        if not docs:
            reasons.append(f"missing evidence for stage {stage!r}")
            continue
        if not any(d.get("tree_sha") == tree_sha for d in docs):
            reasons.append(f"stage {stage!r} evidence not bound to delivery tree_sha {tree_sha[:12]}")
    return (not reasons), reasons


# ─── S5 (#1227): fail-closed design→impl approval lifecycle (no blind coding, R2/R3) ──────────────
# Design recording, approval, injection, and build-boundary anti-drift are always on. The retired
# `design_gate.enabled` config leaf is consumed only as a deprecated tombstone.
# Framing-note capture is opt-in, default OFF -> byte-identical off. Config `framing_notes.enabled` controls
# only the non-gating record_constraints tool exposure; design authority and implementation checks stay on.
FRAMING_NOTES_ENABLED = False
# S2 (#1224/#1463): completion authority is always on. An advance to `done` requires a readable,
# non-empty feedback artifact with an explicit normalized `status: done`; no other content state advances.
# S7 (#1229): disentangle /watcher (feedback-advance) from /autopilot (launch) — two orthogonal concerns over
# ONE reconciler loop. Opt-in (default OFF → byte-identical: the loop is gated on _WATCHER_ENABLED only, launch
# needs watcher on, and `autopilot on` points the operator at `/auto on`). When ON: autopilot is self-sufficient
# (the loop runs if EITHER is on; the feedback side stays _WATCHER-gated) and the contradictory double message
# is suppressed. DEV-1 turns it on via `automation.decoupled`.
AUTOMATION_DECOUPLED = False
# S7 (#1229): task-scoped detect-progress heartbeat (distinct from the per-turn idle-watchdog #1132). Seconds
# without a progress signal (coder log / feedback mtime) before an in_progress task that has shown progress is
# flagged stalled. A task with no signal ever is deliberately excluded so manually managed work is not
# false-flagged. Positive finite tuning only; the protection has no off state.
HEARTBEAT_STALL_S = 900.0
# Client-run claims expire unless the thin client renews this lease through /claim.
CLAIM_LEASE_TTL_S = 120.0
DESIGN_STAGE = "design"
# Task types that PRODUCE CODE — a stage_handover of one of these is REFUSED until the active unit has an
# APPROVED design. Design/analysis types (architecture, concept, research, documentation, verification,
# smoke-test, cleanup) are the stage that PRODUCES the design → never gated. (Not `_task_class`, which lumps
# docs into "coding".)
_IMPLEMENTATION_TASK_TYPES = frozenset({
    "implementation", "feature", "backend", "frontend", "fullstack", "integration",
    "refactoring", "bugfix", "optimization", "security", "security-audit",
    "deployment", "infrastructure",
})


def _fm_is_true(v: object) -> bool:
    """Frontmatter values arrive as strings (via `_parse_frontmatter`). Coerce an approval flag."""
    return str(v or "").strip().lower() in ("true", "yes", "1", "approved")


UNCAPTURED = "UNCAPTURED"
CAPTURED_NONE = "CAPTURED_NONE"
CAPTURED = "CAPTURED"
_CONSTRAINT_MARKERS = ("<!-- IRONCLAD:CONSTRAINTS -->", "<!-- /IRONCLAD:CONSTRAINTS -->")


class GateRefusal(Exception):
    """Typed deterministic gate refusal. The message intentionally has no output prefix."""


def _trim_constraint_body(body: str) -> str:
    """Trim blank lines at the edges without changing content lines or their line endings."""
    if not body or not body.strip():
        return ""
    body = re.sub(r"\A(?:[ \t]*(?:\r\n|\r|\n))+", "", body)
    return re.sub(r"(?:(?:\r\n|\r|\n)[ \t]*)+\Z", "", body)


def _constraint_status(slug: "Optional[str]") -> "tuple[str, Optional[str]]":
    """Tri-state framing-note status for *slug* from ``notes/framing.md``.

    One bounded file read, pure and fail-soft: malformed, inconsistent, poisoned, oversized, or unreadable
    documents are indistinguishable from a missing capture and return ``UNCAPTURED``.
    """
    if not slug:
        return (UNCAPTURED, None)
    doc = vault_root() / slug / "notes" / "framing.md"
    try:
        if not doc.is_file():
            return (UNCAPTURED, None)
        raw = doc.read_bytes()
        if len(raw) > 65536:
            return (UNCAPTURED, None)
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return (UNCAPTURED, None)
        closing = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if closing is None:
            return (UNCAPTURED, None)
        fm = _parse_frontmatter(text)
        if not fm:
            return (UNCAPTURED, None)
        body = "".join(lines[closing + 1:])
        if any(marker in body for marker in _CONSTRAINT_MARKERS):
            return (UNCAPTURED, None)
        body = _trim_constraint_body(body)
        declared_none = _fm_is_true(fm.get("declared_none"))
        if declared_none and not body:
            return (CAPTURED_NONE, None)
        if not declared_none and body:
            return (CAPTURED, body)
        return (UNCAPTURED, None)
    except Exception:  # noqa: BLE001 -- a constraint-status probe must never raise
        return (UNCAPTURED, None)


class DesignMigrationRefusal(RuntimeError):
    """A legacy design cannot be reconciled without risking operator-authored bytes."""


_DESIGN_MIGRATION_BLOCKED: "Dict[str, Path]" = {}


def _fsync_directory(path: Path) -> None:
    """Durably commit a contained replace where directory fsync is supported."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _atomic_design_write(path: Path, content: str) -> None:
    """Crash-safe UTF-8 design write: unique temp, file fsync, replace, directory fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _numbered_design_proposals(slug: str) -> "List[Path]":
    """Return numbered proposal paths without invoking legacy migration recursively."""
    pdir = vault_root() / slug / "proposals"
    if not pdir.is_dir():
        return []
    numbered: "List[tuple[int, Path]]" = []
    for path in pdir.glob("design-*.md"):
        suffix = path.stem[len("design-"):]
        if suffix.isdigit():
            numbered.append((int(suffix), path))
    return [path for _number, path in sorted(numbered, key=lambda item: item[0])]


def _migration_refusal(slug: str, reason: str) -> DesignMigrationRefusal:
    return DesignMigrationRefusal(
        f"legacy design migration refused for unit {slug!r}: {reason}. "
        "Reconcile decisions/design.md manually and retry; nothing was overwritten or deleted."
    )


def _blocked_design_reconciled(decision: Path, blocked: Path) -> bool:
    """Return whether an operator replaced the blocked partial with one valid decision."""
    if blocked.exists() or not decision.is_file():
        return False
    try:
        raw = decision.read_bytes()
        if len(raw) > 65536:
            return False
        fm = _parse_frontmatter(raw.decode("utf-8"))
        return bool(fm) and str(fm.get("approved") or "").strip().lower() in {"true", "false"}
    except Exception:  # noqa: BLE001 -- an inconsistent operator state must remain blocked
        return False


def _migrate_legacy_design(slug: "Optional[str]") -> None:
    """Lazily reconcile one unit's legacy design layout under the project/track vault lock."""
    target = str(slug or "").strip()
    if not target:
        return
    with _vault_lock():
        vdir = vault_root() / target
        decision = vdir / "decisions" / "design.md"
        blocked_key = str(vdir.resolve())
        blocked = _DESIGN_MIGRATION_BLOCKED.get(blocked_key)
        if blocked is not None:
            if _blocked_design_reconciled(decision, blocked):
                _DESIGN_MIGRATION_BLOCKED.pop(blocked_key, None)
            else:
                raise _migration_refusal(target, f"prior recovery is incomplete at {blocked.as_posix()}")
        if not decision.exists():
            return
        if not decision.is_file():
            raise _migration_refusal(target, "decisions/design.md is not a regular file")
        try:
            raw = decision.read_bytes()
        except Exception as ex:  # noqa: BLE001 -- unreadable operator state is a protected refusal
            raise _migration_refusal(target, f"decisions/design.md is unreadable ({ex!r})") from ex
        if len(raw) > 65536:
            raise _migration_refusal(target, "decisions/design.md exceeds the 65536-byte limit")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as ex:
            raise _migration_refusal(target, "decisions/design.md is not valid UTF-8") from ex
        fm = _parse_frontmatter(text)
        approved_raw = str(fm.get("approved") or "").strip().lower() if fm else ""
        if approved_raw not in {"true", "false"}:
            raise _migration_refusal(target, "decisions/design.md has missing or malformed frontmatter")
        if approved_raw == "true":
            return

        proposals = _numbered_design_proposals(target)
        numbers = [int(path.stem[len("design-"):]) for path in proposals]
        proposal = vdir / "proposals" / f"design-{max(numbers, default=0) + 1}.md"
        proposal.parent.mkdir(parents=True, exist_ok=True)
        normalized = _set_frontmatter_flag(text, "type", "proposal")
        normalized = _set_frontmatter_flag(normalized, "approved", "false")
        if normalized == text and (str(fm.get("type") or "").strip().lower() != "proposal"):
            raise _migration_refusal(target, "decisions/design.md frontmatter cannot be normalized")
        try:
            os.replace(decision, proposal)
            _fsync_directory(decision.parent)
            _fsync_directory(proposal.parent)
            _atomic_design_write(proposal, normalized)
        except Exception as ex:  # noqa: BLE001 -- restore the sole authoritative path whenever possible
            if proposal.exists() and not decision.exists():
                try:
                    decision.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(proposal, decision)
                    _fsync_directory(proposal.parent)
                    _fsync_directory(decision.parent)
                except Exception:  # noqa: BLE001 -- retain exactly one recoverable design path and block
                    _DESIGN_MIGRATION_BLOCKED[blocked_key] = proposal
            raise _migration_refusal(target, f"atomic migration failed ({ex!r})") from ex


def _design_typed(slug: "Optional[str]") -> "Dict[str, Any]":
    """Return typed build-standard fields from the approved ``decisions/design.md``.

    S2 (#1415): build-boundary anti-drift reads the approved design, not the intent-time
    approved design metadata. Interim scope is ``language`` only; dependency/egress enforcement is
    deferred. Pure/fail-soft: missing, unapproved, malformed, unreadable, or invalid values -> ``{}``.
    """
    target = (slug or "").strip()
    if not target:
        return {}
    _migrate_legacy_design(target)
    doc = vault_root() / target / "decisions" / "design.md"
    try:
        if not doc.is_file():
            return {}
        raw = doc.read_bytes()
        if len(raw) > 65536:
            return {}
        text = raw.decode("utf-8")
        fm = _parse_frontmatter(text)
        if not fm or not _fm_is_true(fm.get("approved")):
            return {}
        raw_lang = str(fm.get("language") or "").strip()
        if not raw_lang:
            return {}
        from ack.ace.constraint_types import normalize_language  # lazy: never import ack at top-level
        lang = normalize_language(raw_lang)
        return {"language": lang} if lang else {}
    except Exception:  # noqa: BLE001 -- a typed-design probe must never raise
        return {}


def _approved_design_build_policy_section(slug: "Optional[str]") -> "Optional[str]":
    """Return the approved design's ``## Build policy`` body, or ``None`` when absent."""
    target = (slug or "").strip()
    if not target:
        return None
    _migrate_legacy_design(target)
    doc = vault_root() / target / "decisions" / "design.md"
    if not doc.is_file():
        return None
    raw = doc.read_bytes()
    if len(raw) > 65536:
        return None
    text = raw.decode("utf-8")
    fm = _parse_frontmatter(text)
    if not fm or not _fm_is_true(fm.get("approved")):
        return None
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^##\s+Build policy\s*$", line.strip(), flags=re.IGNORECASE):
            start = i
            break
    if start is None:
        return None
    body: "List[str]" = []
    for line in lines[start + 1:]:
        if re.match(r"^##\s+", line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _design_build_policy(slug: "Optional[str]") -> str:
    """Return the approved design's ``## Build policy`` body, using ``""`` when absent."""
    return _approved_design_build_policy_section(slug) or ""


def _split_egress_packages(raw: str) -> "List[str]":
    return [p.strip().lower() for p in re.split(r"[\s,]+", raw or "") if p.strip()]


def _design_egress_policy(slug: "Optional[str]") -> "Dict[str, Any]":
    """Return machine-readable egress policy from the approved design build policy."""
    try:
        policy = _approved_design_build_policy_section(slug)
        if policy is None:
            return {"network": "absent", "allow": [], "deny": []}
        out = {"network": "invalid", "allow": [], "deny": []}
        network_seen = False
        for line in policy.splitlines():
            m = re.match(r"^\s*(network|allow|deny)\s*:\s*(.*?)\s*$", line, flags=re.IGNORECASE)
            if not m:
                continue
            key = m.group(1).lower()
            value = m.group(2).strip()
            if key == "network":
                network_seen = True
                network = value.lower()
                out["network"] = network if network in {"none", "declared", "open"} else "invalid"
            elif key in {"allow", "deny"}:
                out[key] = _split_egress_packages(value)
        if not network_seen:
            out["network"] = "invalid"
        return out
    except DesignMigrationRefusal:
        raise
    except Exception:  # noqa: BLE001 -- a declared-policy read/parse failure is restrictive fail-closed input
        return {"network": "invalid", "allow": [], "deny": []}


def _design_build_check(slug: "Optional[str]", task_typed: "Optional[Dict[str, Any]]") -> "Optional[str]":
    """Build-boundary anti-drift check against the approved design's typed standard."""
    try:
        dt = _design_typed(slug)
        if not dt:
            return None
        from ack.ace.constraint_conflict import hardcheck  # lazy: never import ack at top-level
        v = hardcheck(dt, task_typed or {}, require_present=True)
        if v is None:
            return None
        if v.kind == "missing":
            return (
                f"ERROR: approved design requires {v.category}={v.required!r} but the task typed "
                f"field is missing — declare {v.category!r} on the task to match the approved "
                "design. Nothing changed."
            )
        return (
            f"ERROR: approved design requires {v.category}={v.required!r} but the task provides "
            f"{v.provided!r} — align the typed {v.category!r} field to the approved design. "
            "Nothing changed."
        )
    except Exception:  # noqa: BLE001 -- fail-closed while the design gate is enforcing
        return ("ERROR: design build-check failed (internal) — refuse (fail-closed). "
                "Nothing changed.")


def _egress_finding_message(finding: "Dict[str, Any]") -> str:
    subject = (
        finding.get("package")
        or finding.get("symbol")
        or finding.get("command")
        or finding.get("step")
        or finding.get("file")
        or "finding"
    )
    reason = str(finding.get("reason") or "egress-capable operation").strip()
    loc = ""
    if finding.get("file"):
        loc = str(finding["file"])
        if finding.get("line"):
            loc += f":{finding['line']}"
        loc = f" ({loc})"
    if finding.get("package"):
        return f"package {subject}: {reason}"
    if finding.get("command"):
        return f"build step {subject}: {reason}"
    return f"{subject}{loc}: {reason}"


def _egress_advance_findings(root: "Path", pol: "Dict[str, Any]") -> "tuple[List[str], List[str]]":
    """Run best-effort egress analyzers for the post-coder advance hook."""
    block_msgs: "List[str]" = []
    advisory_msgs: "List[str]" = []

    from ack.egress import analyze_dependencies
    from ack.egress.staticscan import scan_source_tree
    from engine import egress_runner

    for result in (
        analyze_dependencies(root, pol, rust_feature_resolver=egress_runner.rust_feature_resolver),
        scan_source_tree(root),
        egress_runner.run_hermetic(root, network=str(pol.get("network") or "open")),
    ):
        for finding in list((result or {}).get("findings") or []):
            msg = _egress_finding_message(finding)
            if finding.get("severity") == "block":
                block_msgs.append(msg)
            else:
                advisory_msgs.append(msg)
    return block_msgs, advisory_msgs


def _egress_advance_check_log() -> "tuple[Optional[str], List[str]]":
    """Return the always-on egress refusal plus advisory log lines for advance."""
    try:
        pol = _design_egress_policy(active_slug())
    except DesignMigrationRefusal as ex:
        return (
            f"ERROR: egress analysis refused advance; the approved design vault is unreadable ({ex}). "
            "Reconcile decisions/design.md and retry. Task stays in_progress.",
            [],
        )
    net = str(pol.get("network") or "invalid")
    if net in ("absent", "open"):
        return None, []
    if net == "invalid":
        return (
            "ERROR: egress analysis refused advance; the approved design `## Build policy` is present but its "
            "`network:` posture is missing or invalid (declare exactly one of network: none|declared|open). "
            "Task stays in_progress.",
            [],
        )
    root = _project_root() or Path(_exec_cwd() or ".")
    if not root or not Path(root).is_dir():
        return (
            "ERROR: egress analysis refused advance; a restrictive network posture requires a code root to "
            "analyze but none is available. Task stays in_progress.",
            [],
        )
    try:
        block_msgs, advisory_msgs = _egress_advance_findings(root, pol)
    except Exception:  # noqa: BLE001 -- restrictive posture: analyzer failure must refuse, not skip
        return (
            "ERROR: egress analysis refused advance; the analyzers could not complete under a restrictive "
            "network posture (fail-closed). Task stays in_progress.",
            [],
        )

    log = [f"egress advisory: {msg}" for msg in advisory_msgs]
    if not block_msgs:
        return None, log
    bullets = "\n".join(f"  - {msg}" for msg in block_msgs)
    return (
        "ERROR: egress analysis refused advance; blocking findings:\n"
        f"{bullets}\n"
        "Resolve by allow-listing the dependency in the approved design `## Build policy` "
        "(`allow: <package>`) or by removing the egress-capable dependency/import/build step. "
        "Task stays in_progress.",
        log,
    )


def _task_typed_fields(fields: "Optional[Dict[str, Any]]") -> "Dict[str, Any]":
    """Normalize optional TaskSpec ``language`` / ``network`` from a task dict (fail-soft ``{}``)."""
    if not fields:
        return {}
    try:
        from ack.ace.constraint_types import parse_typed  # lazy
        return parse_typed(fields)
    except Exception:  # noqa: BLE001
        return {}


def _design_proposals(slug: "Optional[str]") -> "List[Path]":
    """All recorded design proposal variants for *slug*, sorted by their integer index (ascending).

    ADR-0006 D5 (S3): a recorded design is a non-destructive *variant* at
    ``vault_root()/<slug>/proposals/design-<n>.md`` where ``<n>`` is a pure decimal integer. Files whose
    suffix after ``design-`` is not a pure int are ignored. Pure/fail-soft: no unit / no folder / a read
    hiccup → ``[]`` (never raises after migration succeeds)."""
    target = (slug or "").strip()
    if not target:
        return []
    _migrate_legacy_design(target)
    try:
        return _numbered_design_proposals(target)
    except Exception:  # noqa: BLE001 — a proposal probe must never raise into a write/steering path
        return []


def _next_design_proposal_path(slug: str) -> Path:
    """The path for the NEXT design proposal variant: ``proposals/design-<max+1>.md`` (starts at 1)."""
    nums: "List[int]" = []
    for p in _design_proposals(slug):
        try:
            nums.append(int(p.stem[len("design-"):]))
        except Exception:  # noqa: BLE001
            continue
    nxt = (max(nums) + 1) if nums else 1
    return vault_root() / slug / "proposals" / f"design-{nxt}.md"


def _effective_design_doc(slug: "Optional[str]") -> "Optional[Path]":
    """The design doc the gate/steering should read: the approved decision ``decisions/design.md`` when it
    exists, else the highest-index proposal variant, else ``None``. Pure/fail-soft.

    Legacy single-doc state is migrated before lookup."""
    target = (slug or "").strip()
    if not target:
        return None
    _migrate_legacy_design(target)
    try:
        decision = vault_root() / target / "decisions" / "design.md"
        if decision.is_file():
            return decision
        proposals = _design_proposals(target)
        if proposals:
            return proposals[-1]
        return None
    except Exception:  # noqa: BLE001
        return None


def _resolve_design_proposal(slug: str, design_id: "Optional[str]") -> "Optional[Path]":
    """Resolve a proposal id (``'2'`` or ``'design-2'``) to its ``proposals/design-<n>.md`` path if it
    exists, else ``None``. Pure/fail-soft."""
    raw = str(design_id or "").strip().lower()
    if raw.startswith("design-"):
        raw = raw[len("design-"):]
    if not raw or not raw.isdigit():
        return None
    try:
        p = vault_root() / slug / "proposals" / f"design-{int(raw)}.md"
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def _unit_design_status(slug: "Optional[str]") -> "tuple[bool, bool, Optional[str]]":
    """(has_design, approved, doc_rel) for *slug*. ``has_design`` is true for a recorded proposal OR an
    approved/pending decision; ``approved`` keys ONLY on ``decisions/design.md`` (a proposal is never
    approved-in-place — S3 promotes it). Order: the approved-or-pending decision doc wins (today's
    behaviour), else the latest proposal variant, else no design. Cheap: at most ONE file read + one dir
    scan. Pure/fail-soft (no unit / read hiccup → ``(False, False, None)``).

    Legacy single-doc state is migrated before lookup."""
    if not slug:
        return (False, False, None)
    _migrate_legacy_design(slug)
    doc = vault_root() / slug / "decisions" / "design.md"
    try:
        if doc.is_file():
            fm = _parse_frontmatter(doc.read_text(encoding="utf-8"))
            return (True, _fm_is_true(fm.get("approved")), doc.relative_to(vault_root()).as_posix())
        proposals = _design_proposals(slug)
        if proposals:
            return (True, False, proposals[-1].relative_to(vault_root()).as_posix())
    except DesignMigrationRefusal:
        raise
    except Exception:  # noqa: BLE001 — a design-status probe must never raise (it runs every turn)
        return (False, False, None)
    return (False, False, None)


def _design_gate(task_type: str, slug: "Optional[str]") -> "Optional[str]":
    """R2/R3 fail-closed PRE-code gate: an implementation handover is refused until the active unit has a
    recorded + APPROVED design. Design/analysis/docs handovers are unaffected. Returns an ERROR string
    (refusal) or ``None`` (allow). Pure/deterministic — no model call."""
    if (task_type or "").strip().lower() not in _IMPLEMENTATION_TASK_TYPES:
        return None
    if not slug:
        return ("ERROR: blind-coding refused (R2) — no active unit. Record + approve a design "
                "(record_design → /approve) before an implementation handover.")
    try:
        has_design, approved, rel = _unit_design_status(slug)
    except DesignMigrationRefusal as ex:
        return f"ERROR: {ex}"
    if not has_design:
        return (f"ERROR: blind-coding refused (R2) — unit {slug!r} has no design on record. Call "
                f"record_design to persist the design (idea/approach/architecture), get it approved "
                f"(/approve), THEN stage the implementation handover.")
    if not approved:
        return (f"ERROR: design for unit {slug!r} is recorded ({_display_doc_path(rel)}) but NOT approved. The operator must "
                f"approve it — run /approve — before an "
                f"implementation handover.")
    return None


def record_design(title: str, body: str, *, slug: "Optional[str]" = None,
                  language: str = "", network: str = "") -> str:
    """S5 (#1227): persist a DESIGN doc for the active (or *slug*) unit — the pre-code lifecycle artifact the
    design→impl gate reads. Writes ``<slug>/decisions/design.md`` with contract-guaranteed frontmatter
    (``type: proposal`` · ``stage: design`` · ``approved: false``), so the model cannot forget the fields the
    gate needs, then reconciles the vault (cross-links, R4). A fresh recording resets approval to ``false`` (a
    changed design must be re-approved). Fail-closed: no resolvable unit raises ``ValueError``.

    Recording is **non-destructive**: it writes a retained variant to ``proposals/design-<n>.md`` and does
    not overwrite or unapprove an existing ``decisions/design.md``. ``/approve design [<id>]`` promotes the
    chosen variant into ``decisions/`` as the decision. Returns the doc path (posix).

    Optional typed ``language`` is normalized and persisted as design frontmatter. The compatibility
    ``network`` argument is never persisted or passed to the design hard-check; egress posture belongs only
    in the approved design's ``## Build policy`` section.

    """
    target = (slug or active_slug() or "").strip()
    if not target:
        raise ValueError("no active unit for record_design")
    title = (title or "").strip() or "Design"
    body = str(body).replace("\r\n", "\n").replace("\r", "\n")
    if not body.endswith("\n"):
        body += "\n"
    # #1341: optional typed design fields (fail-closed when a value is provided but not allow-listed).
    typed_fm_lines = _typed_frontmatter_lines(language, "", refuse_invalid=True)
    with _vault_lock():
        vdir = vault_root() / target
        if not (vdir / "meta.md").is_file():
            raise ValueError(_msg("vault.no_initiative", slug=target, root=vault_root().as_posix()))
        _migrate_legacy_design(target)
        # #1267: the model's body frequently already opens with its own top-level heading; injecting
        # `# {title}` on top of that produces a DUPLICATE H1. Inject the title heading only when the body
        # does not already lead with one (the frontmatter `title:` stays canonical either way).
        heading = "" if body.lstrip().startswith("# ") else f"# {title}\n\n"
        content = (
            "---\n"
            "type: proposal\n"
            f"stage: {DESIGN_STAGE}\n"
            "approved: false\n"
            f"title: {title}\n"
            f"{typed_fm_lines}"
            "---\n\n"
            f"{heading}"
            f"{body}"
        )
        pdir = vdir / "proposals"
        pdir.mkdir(parents=True, exist_ok=True)
        doc = _next_design_proposal_path(target)
        _atomic_design_write(doc, content)
        try:
            reconcile_vault(target, links=False)
        except Exception:  # noqa: BLE001 — projection must not fail on a reconcile hiccup
            pass
        return _display_doc_path((doc.relative_to(vault_root())).as_posix())   # #1276: navigable path


def _typed_frontmatter_lines(language: str = "", network: str = "", *,
                             refuse_invalid: bool = True) -> str:
    """Normalize optional typed params to frontmatter lines (``language:`` / ``network:``).

    Empty / whitespace = not provided (no line). A non-empty value that fails the allow-list raises
    ``GateRefusal`` when *refuse_invalid* (capture/design fail-closed). Never invents keys.
    """
    from ack.ace.constraint_types import (  # lazy: never import ack at gx10 top-level
        normalize_language,
        normalize_network,
    )
    lines = ""
    lang_raw = str(language or "").strip()
    if lang_raw:
        lang = normalize_language(lang_raw)
        if lang is None:
            if refuse_invalid:
                raise GateRefusal(f"invalid language value: {lang_raw!r}")
        else:
            lines += f"language: {lang}\n"
    if isinstance(network, bool):
        lines += f"network: {'true' if network else 'false'}\n"
    else:
        net_raw = str(network or "").strip()
        if net_raw:
            net = normalize_network(net_raw)
            if net is None:
                if refuse_invalid:
                    raise GateRefusal(f"invalid network value: {net_raw!r}")
            else:
                lines += f"network: {'true' if net else 'false'}\n"
    return lines


def record_constraints(title: str, body: str, *, slug: "Optional[str]" = None,
                       language: str = "", network: str = "", source: str = "") -> str:
    """Persist non-gating framing notes for the active (or *slug*) unit.

    S1 (#1414): product constraints are no longer a hard floor and this tool no
    longer writes ``decisions/constraints.md``. The note is optional context under
    ``notes/framing.md``; design approval and implementation gates do not read it.
    Typed parameters are accepted for compatibility but only invalid values refuse.
    """
    target = (slug or active_slug() or "").strip()
    if not target:
        raise ValueError("no active unit for record_constraints")
    title = (title or "").strip() or "Framing"
    body = str(body)
    if any(marker in body for marker in _CONSTRAINT_MARKERS):
        raise GateRefusal("framing body may not contain the reserved IRONCLAD:CONSTRAINTS marker")
    declared_none = not body.strip() or body.strip().lower() == "none"
    captured_body = "" if declared_none else _trim_constraint_body(body)
    typed_fm_lines = "" if declared_none else _typed_frontmatter_lines(language, network, refuse_invalid=True)
    with _vault_lock():
        vdir = vault_root() / target
        if not (vdir / "meta.md").is_file():
            raise ValueError(_msg("vault.no_initiative", slug=target, root=vault_root().as_posix()))
        ndir = vdir / "notes"
        ndir.mkdir(parents=True, exist_ok=True)
        doc = ndir / "framing.md"
        content = (
            "---\n"
            "type: note\n"
            "stage: framing\n"
            f"declared_none: {'true' if declared_none else 'false'}\n"
            f"title: {title}\n"
            f"{typed_fm_lines}"
            "---\n"
            f"{captured_body}"
        )
        doc.write_text(content, encoding="utf-8", newline="")
        try:
            reconcile_vault(target, links=False)
        except Exception:  # noqa: BLE001 -- projection must not fail on a reconcile hiccup
            pass
        return _display_doc_path(doc.relative_to(vault_root()).as_posix())


def _approve_command(arg: "Optional[str]") -> str:
    """Route design approval only.

    Accepted forms: bare ``/approve``, ``/approve design``, and
    ``/approve design <proposal-id>`` (the token is a design proposal id in the
    active unit). Product constraint approval was retired in S1 (#1414).
    """
    parts = (arg or "").split()
    if not parts:
        return _approve_design()
    head = parts[0].lower()
    if head != "design":
        return "ERROR: usage: /approve [design [<proposal-id>]]"
    token = parts[1] if len(parts) > 1 else None
    return _approve_design(design_id=token)


def _approve_design(slug: "Optional[str]" = None, *, design_id: "Optional[str]" = None) -> str:
    """S5 (#1227): promote the active (or *slug*) unit's ``stage: design`` proposal to a decision and stamp
    ``approved: true`` — the operator's influence/approval point that unblocks implementation handovers.
    File-based (R1). Returns a human message. Fail-closed: no unit / no design doc → a clear ERROR, nothing
    changed.

    Approval promotes the chosen proposal variant (``design_id`` = ``'2'`` / ``'design-2'``, else the sole
    proposal) into ``decisions/design.md``.
    """
    target = (slug or active_slug() or "").strip()
    if not target:
        return "ERROR: no active unit — nothing to approve."
    try:
        _migrate_legacy_design(target)
        return _promote_design_proposal(target, design_id)
    except DesignMigrationRefusal as ex:
        return f"ERROR: {ex}"


def _proposal_matches_decision(proposal: Path, decision_text: str) -> bool:
    """True when *proposal* is the variant currently ratified as the decision — i.e. promoting it (stamp
    ``approved: true`` + ``type: decision``, mirroring :func:`_promote_design_proposal`) reproduces
    *decision_text* exactly (``reconcile_vault(links=False)`` leaves the body untouched, so the match is
    reliable). Used to keep an ALREADY-promoted proposal out of the 'switch to a newer variant' hint.
    Fail-soft ``False`` on a read hiccup (the proposal is then treated as genuinely newer — safe)."""
    try:
        ptext = proposal.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return False
    promoted = _set_frontmatter_flag(ptext, "approved", "true")
    promoted = _set_frontmatter_flag(promoted, "type", "decision")
    return promoted == decision_text


def _promote_design_proposal(target: str, design_id: "Optional[str]") -> str:
    """ADR-0006 D5 (#1416 / S3) ON-path design approval: PROMOTE the chosen proposal variant into
    ``decisions/design.md`` as the decision (``approved: true`` · ``type: decision``), preserving the body
    verbatim — including any ``## Build policy`` section (dep/egress guidance the build honours). The
    promoted proposal file is RETAINED (variant provenance); ``decisions/`` keeps only the approved decision.

    Selection: explicit *design_id* → that proposal (else a ``no such design proposal`` ERROR); no id →
    the sole proposal, or (already-approved decision) an idempotent note (+ switch hint when newer proposals
    exist), or (multiple proposals) a pick-one ERROR, or (legacy unapproved ``decisions/design.md``) stamp it
    in place, else the no-design ERROR.
    """
    with _vault_lock():
        _migrate_legacy_design(target)
        decision_doc = vault_root() / target / "decisions" / "design.md"
        decision_exists = decision_doc.is_file()
        decision_approved = False
        if decision_exists:
            try:
                decision_approved = _fm_is_true(
                    _parse_frontmatter(decision_doc.read_text(encoding="utf-8")).get("approved"))
            except Exception:  # noqa: BLE001 — migration already rejected unreadable/malformed state
                decision_approved = False
        proposals = _design_proposals(target)
        ids_hint = ", ".join(p.stem for p in proposals)
        already_approved = False

        raw_id = str(design_id or "").strip()
        if raw_id:
            chosen = _resolve_design_proposal(target, raw_id)
            if chosen is None:
                tail = f" (recorded: {ids_hint})" if ids_hint else " — record one first (record_design)"
                return f"ERROR: no such design proposal {raw_id!r} for {target!r}{tail}. Nothing changed."
        elif decision_exists and decision_approved:
            chosen = decision_doc
            already_approved = True
        elif len(proposals) == 1:
            chosen = proposals[0]
        elif len(proposals) > 1:
            return (f"ERROR: multiple design proposals for {target!r} — pick one: "
                    f"`/approve design <id>` ({ids_hint}). Nothing changed.")
        else:
            return (f"ERROR: unit {target!r} has no design to approve — record one first "
                    f"(record_design). Nothing changed.")

        if already_approved:
            rel = _display_doc_path(decision_doc.relative_to(vault_root()).as_posix())
            try:
                decision_text = decision_doc.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                decision_text = ""
            newer = [p for p in proposals
                     if not (decision_text and _proposal_matches_decision(p, decision_text))]
            newer_hint = ", ".join(p.stem for p in newer)
            hint = (f" Newer proposal(s) recorded ({newer_hint}) — `/approve design <id>` to switch the decision."
                    if newer else "")
            return f"Design for {target!r} is already approved ({rel})." + hint
        vdir = vault_root() / target
        if not (vdir / "meta.md").is_file():
            return f"ERROR: no unit {target!r}."
        try:
            text = chosen.read_text(encoding="utf-8")
        except Exception as ex:  # noqa: BLE001
            return f"ERROR: could not read the design proposal for {target!r} ({ex!r}). Nothing changed."
        new = _set_frontmatter_flag(text, "approved", "true")
        new = _set_frontmatter_flag(new, "type", "decision")
        rel = _display_doc_path((decision_doc.relative_to(vault_root())).as_posix())
        if new == text:
            return f"ERROR: could not stamp approval on the design doc for {target!r} ({rel})."
        try:
            _atomic_design_write(decision_doc, new)
        except Exception as ex:  # noqa: BLE001 -- promotion must not expose a partial ratified decision
            return f"ERROR: could not atomically promote the design for {target!r} ({ex!r}). Nothing changed."
        try:
            reconcile_vault(target, links=False)
        except Exception:  # noqa: BLE001
            pass
    return (f"OK: approved the design for {target!r} ({rel}) — implementation handovers are now unblocked.\n"
            f"👉 Next: ask the model to break the approved design into units — it creates ONE epic plus ALL "
            f"implementation units via plan_units (no handovers yet). Then `/auto on [N]` drains them: the "
            f"engine stages, launches and advances unit after unit until the epic is done; `/auto off` keeps "
            f"it guided (the engine recommends each next unit, you drive).")


def _set_frontmatter_flag(text: str, key: str, value: str) -> str:
    """Set/insert a scalar ``key: value`` inside the leading ``---`` frontmatter block. If the key exists it
    is replaced; else it is appended just before the closing ``---``. Returns the doc with no frontmatter
    unchanged (defensive)."""
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*(?:\n|$))", text, re.DOTALL)
    if not m:
        return text
    head, block, tail = m.group(1), m.group(2), m.group(3)
    lines = block.split("\n")
    kre = re.compile(rf"^(\s*){re.escape(key)}\s*:.*$")
    for i, ln in enumerate(lines):
        if kre.match(ln):
            lines[i] = f"{key}: {value}"
            break
    else:
        lines.append(f"{key}: {value}")
    return head + "\n".join(lines) + tail + text[m.end():]


def _project_vault_base() -> Path:
    """The project vault root WITHOUT the active-track suffix (the ``main`` track location). Cross-track
    enumeration + reconcile work off this base. Derived from the :func:`vault_root` accessor (not the raw
    global): for a non-``main`` active track ``vault_root`` appended ``.tracks/<track>``, so strip those
    two trailing segments back to the base; for ``main`` it already IS the base."""
    base = vault_root()
    if _active_track() != "main":
        base = base.parent.parent
    return base


def _project_tracks() -> List[str]:
    """All tracks of the active project: ``main`` plus each safe directory under
    ``<project_vault>/.tracks/`` (sorted, deterministic)."""
    tracks = ["main"]
    td = _project_vault_base() / ".tracks"
    if td.is_dir():
        tracks += sorted(d.name for d in td.iterdir() if d.is_dir() and _is_safe_track(d.name))
    return tracks


def reconcile_active_project(*, links: bool = False) -> List[str]:
    """Project-scoped reconcile (S12e): reconcile EVERY initiative in the **current track** (not just the
    single active initiative). Fail-closed per initiative — one that errors is reported and the rest
    proceed. Idempotent (delegates to the vault-locked :func:`reconcile_vault`)."""
    out: List[str] = []
    for v in initiative_list():
        try:
            out.append(reconcile_vault(v.slug, links=links))
        except Exception as e:   # noqa: BLE001 — one bad initiative must not abort the sweep
            out.append(f"{v.slug}: ERROR {e!r}")
    return out


def reconcile_all_tracks(*, links: bool = False) -> Dict[str, List[str]]:
    """The scheduled **cross-track reconciler** (S12e): reconcile every initiative in every track of the
    active project. **Fail-closed per track** — a track that raises is recorded and the others continue.
    Returns ``{track: [per-initiative results]}``. Idempotent; the scheduled reconcile tick / the
    ``/initiative reconcile all`` command call this. With no active project only ``main`` is swept."""
    pcur = _pc.current() if _pc is not None else None
    has_project = pcur is not None and bool(getattr(pcur, "project_id", ""))
    # With no active project there is nothing to bind a non-main track to, so only ``main`` is swept
    # (a stray legacy ``.tracks/`` subtree must NOT surface as empty entries — contract-honest).
    tracks = _project_tracks() if has_project else ["main"]
    out: Dict[str, List[str]] = {}
    for track in tracks:
        try:
            if has_project:
                ctx = _pc.ProjectContext(pcur.project_id, pcur.root, pcur.mem_ns, track=track)
                with _pc.use(ctx):
                    out[track] = reconcile_active_project(links=links)
            else:
                out[track] = reconcile_active_project(links=links)   # main only
        except Exception as e:   # noqa: BLE001 — fail-closed per track
            out[track] = [f"track {track!r}: ERROR {e!r}"]
    return out


# Memory layer — module-level singleton, initialized in GX10.__init__()
_MEMORY_CONFIG: Dict[str, Any] = {}
_MEMORY: Optional[Any] = None
# Warm tier (Valkey, B0) — optional cache-aside layer in front of the cold vector store (B2 retrieval)
# + session state. Singleton, initialized in GX10.__init__(); without a url it stays None (no-op).
_WARM_CONFIG: Dict[str, Any] = {}
_WARM: Optional[Any] = None
# Epic #366 — the per-engine token counter (vLLM /tokenize + calibrated char fallback). Created in
# GX10.__init__; module-global so the module-level budgeters (_rag_block / _count_tokens / the trim)
# can reach it. None ⇒ pure calibrated char fallback.
_TOKENS: Optional[Any] = None
#: Phase-e reasoning fan-out (engine/workers.py). Set by the server; stays None in the
#: monolithic CLI, so the parallel tool is offered only where the governed workers exist.
_WORKERS: Optional[Any] = None
#: P0 provider router — set at server boot beside _WORKERS (server.py). None or inactive ⇒
#: parallel_reason uses today's _WORKERS.fanout path, byte-identically.
_DISPATCHER: Optional[Any] = None
#: Web-search adapter seam (epic #505) — set at server boot from the `search` config block,
#: standalone from the provider dispatcher so a native-search deployment with no CLI provider
#: still offers web_search. None ⇒ no adapter wired (offer/exec gates fail closed).
_WEBSEARCH: Optional[Any] = None
#: §3c MAP — when True, each fan-out worker gets its own per-item retrieved context (memory
#: read-citizen) PLUS the shared rolling-summary floor. Default ON (06-18); set False /
#: GX10_WORKER_MEMORY=0 for the stateless fan-out. Config workers.memory_read.
WORKER_MEMORY = True
#: §3c REDUCE — when True, the OK fan-out outputs are consolidated by a SINGLE writer (the main
#: loop) into ONE cold write (no parallel /add races / duplicates). Default ON (06-18); set
#: False / GX10_WORKER_WRITE=0 to stop persisting fan-out outputs. Config workers.memory_write.
WORKER_WRITE = True
#: Write strategy: "reducer" (default, single-writer consolidation — the only mode for the
#: stateless reasoning fan-out) | "direct" (reserved for long-lived autonomous agents that write
#: idempotently themselves; the reducer steps back). Config workers.write_mode / GX10_WORKER_WRITE_MODE.
WORKER_WRITE_MODE = "reducer"
#: Warm-tier session key for the shared rolling summary + worker scratch (single-tenant default).
#: Config GX10_SESSION_ID. ``session:{id}:summary`` survives restart and is read by the workers.
WARM_SESSION_ID = "main"


def _active_warm_session() -> str:
    """The warm-tier session key, scoped to the active ProjectContext's track-composed memory partition
    (``mem_scope()`` = ``<mem_ns>::track::<tid>`` for a non-``main`` track; S14-1) when one is set
    (ADR-0011 AD-1 / S3b — so a project's, and a track's, rolling summary is isolated), else the global
    ``WARM_SESSION_ID`` (today's behaviour; dormant until the switch sets a ctx). Evaluate in the request
    thread — a fan-out worker that does not carry the ctx is bound in S3b(b2)."""
    pc = _pc.current() if _pc is not None else None
    if pc is not None and pc.mem_ns:
        return pc.mem_scope()
    return WARM_SESSION_ID


def _active_mem_ns(default: str = "") -> str:
    """The active cold-memory partition (ADR-0011 AD-1 / S3b; S14-1 adds the per-track sub-scope): the
    ProjectContext's ``mem_scope()`` (``<mem_ns>::track::<tid>`` for a non-``main`` track) when a project
    is active, else *default*. Used to scope the warm retrieval cache + the launched read-only Memory MCP
    to the SAME partition the orchestrator's memory uses."""
    pc = _pc.current() if _pc is not None else None
    if pc is not None and pc.mem_ns:
        return pc.mem_scope()
    return default


def _forget_scope(scope: str) -> Dict[str, Any]:
    """Scope-aware forget endpoint (ADR-0011 D5 / #601 S14-5): drop every trace of partition *scope* across
    all three substrate layers — the cold store (Mem0 ``agent_id``), the warm tier (session state + the
    retrieval cache), and the lesson backend. *scope* is an opaque partition string (typically a
    ``mem_scope`` from :func:`_active_mem_ns`). **Fail-closed on an empty scope** (returns immediately — a
    forget must never wipe the shared base partition); each layer is **fail-soft** (a down/absent tier never
    breaks the call). Returns a per-layer summary: ``{scope, cold: bool, warm: int, lessons: bool}``."""
    scope = (scope or "").strip()
    out: Dict[str, Any] = {"scope": scope, "cold": False, "warm": 0, "lessons": False}
    if not scope:
        return out
    if _MEMORY is not None:
        try:
            out["cold"] = bool(_MEMORY.forget(scope))
        except Exception:  # noqa: BLE001 — fail-soft: a forget never breaks the call
            pass
    if _WARM is not None:
        try:
            out["warm"] = int(_WARM.forget_scope(scope))
        except Exception:  # noqa: BLE001
            pass
    try:
        from ack import lessons as _lessons   # lazy: never import ack at gx10 top-level (S6b lesson)
        out["lessons"] = bool(_lessons.forget(scope))
    except Exception:  # noqa: BLE001
        pass
    return out


def _orphan_scopes(present: "List[str]", registered: "List[str]") -> "List[str]":
    """The memory partitions present in the store with **no registered project** — orphans eligible for GC
    (ADR-0011 AD-4 / #601 S15). An orphan is a present scope whose project key is a **minted** ``mem_ns``
    (``valid_mem_ns``) that is not in *registered*. This precisely targets partitions left behind by a
    removed project, and by construction never flags the base partition (e.g. ``ironclad``) or a curated /
    human-named scope (neither is a minted ``mem_ns``). A per-track sub-scope (``<mem_ns>::track::<tid>``)
    is judged by its project ``mem_ns``, so a live project's tracks are kept and an orphan project's tracks
    are collected with it."""
    if _pr is None:
        return []
    reg = set(registered)
    orphans: List[str] = []
    for s in present:
        if not isinstance(s, str) or not s:
            continue
        base_ns = s.split("::track::", 1)[0]
        if _pr.valid_mem_ns(base_ns) and base_ns not in reg:
            orphans.append(s)
    return orphans


def _reconcile_orphan_memory(*, dry_run: bool = True) -> Dict[str, Any]:
    """Registry-keyed orphan GC (ADR-0011 AD-4 / #601 S15): list the partitions present in the memory store,
    diff against the registered projects' ``mem_ns``, and **forget** any minted partition with no project
    (cold + warm + lessons, via :func:`_forget_scope`). **Destructive → ``dry_run=True`` by default** (it
    returns the orphans without deleting; a caller opts in to the delete). Fail-soft throughout. Returns
    ``{present, registered, orphans, forgotten, dry_run}``."""
    out: Dict[str, Any] = {"present": [], "registered": [], "orphans": [], "forgotten": [], "dry_run": dry_run}
    if _MEMORY is None or _REGISTRY is None:
        return out
    try:
        present = list(_MEMORY.list_scopes())
    except Exception:  # noqa: BLE001 — store unreachable → no present scopes → nothing to GC (safe)
        present = []
    out["present"] = present
    try:
        registered = [p.mem_ns for p in _REGISTRY.list() if getattr(p, "mem_ns", "")]
    except Exception:  # noqa: BLE001 — can't enumerate registered projects → REFUSE to GC (never mass-delete)
        return out
    out["registered"] = registered
    orphans = _orphan_scopes(present, registered)
    out["orphans"] = orphans
    if dry_run:
        return out
    for s in orphans:
        try:
            _forget_scope(s)
            out["forgotten"].append(s)
        except Exception:  # noqa: BLE001 — fail-soft: one orphan's failure never aborts the sweep
            pass
    return out


#: #1317: a per-call override of the exec cwd. A bridged client (Ink / thin CLI) receives the server-shipped
#: active-project exec cwd and sets this around ``run_tool`` so its LOCAL tool execution (relative-path
#: resolution + ``execute_command`` cwd) targets the active project, not the client's frozen boot workdir.
_EXEC_CWD_OVERRIDE: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "_EXEC_CWD_OVERRIDE", default=None)
#: A bridged client receives the server's validated sandbox preference with the internal tool frame. The
#: context-local override keeps concurrent requests isolated and is never model-controlled.
_SANDBOX_POLICY_OVERRIDE: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar(
    "_SANDBOX_POLICY_OVERRIDE", default=None)


def _exec_cwd() -> "Optional[str]":
    """The filesystem working directory for MODEL-DRIVEN execution — the code-tools (read/write/list/…),
    ``execute_command``, and the launched code-agent — under the active ProjectContext (ADR-0011 AD-1 / S9c).
    Returns the active project's ``root`` ONLY when a genuinely non-default project is bound; otherwise
    ``None`` so the caller keeps the process workdir (``_BOOT_WORKDIR``, set by the boot ``os.chdir``) —
    BYTE-IDENTICAL to the pre-isolation engine. A ``/switch`` does NOT chdir the process (a global chdir under
    the daemons/fan-out threads is unsafe), so this is the seam that points a switched project's file ops at
    its own tree.

    NB: when a local-tool bridge is active the code-tools run on the CLIENT's tree (``run_tool`` returns
    early), so this governs only SERVER-side execution — exactly where the project root must be honoured.

    S? (#1237): when ``CODE_SUBDIR`` is configured (``paths.code_subdir``, opt-in, default empty), execution
    runs under ``<root>/<CODE_SUBDIR>`` (created on demand) so the software tree is isolated from the control-
    plane (vault/, .ironclad/ keep resolving to the project root via ``_project_root``). Empty ⇒ the pre-
    isolation behaviour, byte-identical."""
    _ov = _EXEC_CWD_OVERRIDE.get()      # #1317: a bridged client's run_tool sets the server-shipped active-
    if _ov:                            # project exec cwd for the call → its LOCAL tool ops resolve there.
        return _ov
    pc = _pc.current() if _pc is not None else None
    root: "Optional[str]" = None
    if pc is not None and pc.root and not (_BOOT_WORKDIR is not None and Path(pc.root) == _BOOT_WORKDIR):
        root = pc.root                           # a genuinely non-default project (default == boot workdir → None)
    if CODE_SUBDIR:
        base = Path(root) if root is not None else (_BOOT_WORKDIR or Path.cwd())
        sub = base / CODE_SUBDIR
        try:
            sub.mkdir(parents=True, exist_ok=True)
            return str(sub)
        except Exception:   # noqa: BLE001 — mkdir failed (e.g. a plain file sits at <root>/<subdir>): fall
            return root     # back to the project root rather than hand a caller a non-directory cwd (Popen)
    return root


def _resolve_exec_path(path: str) -> Path:
    """Resolve a model-supplied tool path against the active project's exec cwd (``_exec_cwd``). An absolute
    path is taken verbatim; a relative path under a non-default project is anchored at the project root; with
    no active (non-default) project it is returned unchanged (relative to the process workdir — byte-identical
    to before)."""
    p = Path(path)
    cwd = _exec_cwd()
    if cwd is None or p.is_absolute():
        return p
    return Path(cwd) / p


# ─── Project Registry integration (ADR-0011 AD-1/AD-6 / S5b) ──────────────────
# The installation-global Registry is the SSOT of registered, isolated projects + the PERSISTED continuity
# pointer (`active`). The engine's CURRENT project, by contrast, is PER-PROCESS (single-active-per-engine):
# it is read from the registry's `active` ONCE at boot (continuity) and cached in `_ACTIVE_PROJECT`; a
# `/switch` updates that cache AND persists `active`. Threads that don't inherit the boot contextvar
# (daemons, request handlers) call `bind_active()`, which binds the cached project — it does NOT re-read the
# registry, so a second engine process sharing the same home can never yank a running process onto another
# project (and there is no per-tick file I/O).
#
# Until a non-default project is activated this is BYTE-IDENTICAL to the pre-isolation engine: the `default`
# project binds THIS process's boot workdir (`_BOOT_WORKDIR`) — never a root some other workdir's boot
# persisted — so state_root()/vault_root() resolve exactly as before, and its bound mem_ns is EMPTY (memory
# + warm fall back to the legacy/base partition). Distinct per-project partitions begin only on activation.
_REGISTRY = None        # type: ignore[assignment]   # the live Registry (None when project_registry absent)
_BASE_CFG: Optional[Dict[str, Any]] = None            # the deployment base config (pre project-overlay)
_ACTIVE_PROJECT = None  # type: ignore[assignment]    # this process's active Project (cached; not re-read per tick)
_BOOT_WORKDIR: "Optional[Path]" = None                # this process's workdir — the `default` project's root


def _engine_ctx_for(project) -> "Any":
    """Build the ProjectContext to bind for *project*, applying the engine's binding policy. The implicit
    ``default`` project binds THIS process's boot workdir and an EMPTY ``mem_ns`` (it shares the
    legacy/base memory partition — backward-compatible, and process-local so a shared registry can't
    re-point it); every explicitly-registered project binds its own fixed root + minted ``mem_ns`` (true
    isolation). Shared by boot, the daemons, and the ``/switch`` path so the binding is consistent."""
    default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default") if _pr is not None else "default"
    if project.id == default_id:
        root = str(_BOOT_WORKDIR) if _BOOT_WORKDIR is not None else project.root
        mem_ns = ""
    else:
        # S14-2 / C0 cond.5 — fail-closed: a REGISTERED (non-default) project MUST carry a valid minted
        # mem_ns. A malformed persisted entry with an empty/low-entropy mem_ns would otherwise bind here
        # and silently fall back to the BASE partition (a cross-project memory leak). Refuse instead; the
        # switch path rolls back, and boot degrades to the safe `default` project (see init_registry).
        _valid = getattr(_pr, "valid_mem_ns", None) if _pr is not None else None
        if _valid is not None and not _valid(project.mem_ns):
            raise ValueError(f"project {project.id!r} has an invalid mem_ns "
                             f"({project.mem_ns!r}) — refusing to bind it to the base partition; "
                             f"run a registry reconcile")
        root, mem_ns = project.root, project.mem_ns
    return _pc.ProjectContext(project.id, root, mem_ns,
                              getattr(project, "active_track", "main") or "main")


def _set_active_project(project) -> None:
    """Set this process's cached active project (used by boot and the ``/switch`` wrapper). The caller is
    responsible for persisting ``registry.active`` when the change should survive a reboot."""
    global _ACTIVE_PROJECT
    _ACTIVE_PROJECT = project


def init_registry(workdir: "Path | str") -> None:
    """Instantiate the installation-global Registry, ensure the implicit ``default`` project, cache this
    process's active project (the registry's persisted ``active`` — continuity), and bind it on the boot
    thread. No-op when the registry/context seams are absent (the engine then runs un-isolated, exactly as
    before)."""
    global _REGISTRY, _BOOT_WORKDIR, _ACTIVE_PROJECT
    if _pr is None or _pc is None:
        return
    _BOOT_WORKDIR = Path(workdir).resolve()
    _REGISTRY = _pr.Registry()
    try:
        _REGISTRY.ensure_default(_BOOT_WORKDIR)
        _ACTIVE_PROJECT = _REGISTRY.active()    # continuity: the last-active project (default after a fresh ensure)
    except Exception as e:   # noqa: BLE001 — a registry hiccup must never block boot (fall back to legacy)
        _ui_print(col(f"[WARN] project registry unavailable, running un-isolated: {e!r}", C.YELLOW))
        _REGISTRY = None
        _ACTIVE_PROJECT = None
        _pc.set_current(None)   # never leave a stale ctx behind the "un-isolated" claim
        return
    # #1263: an active project whose root vanished out-of-band (e.g. the operator deleted the workdir tree)
    # must NOT be silently re-scaffolded + re-bound as an empty dir — warn and fall back to `default`, so a
    # project the operator believes gone does not reappear. (A completed `/project delete` already drops the
    # entry; this guards the manual out-of-band deletion path.)
    _default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default")
    if (_ACTIVE_PROJECT is not None and _ACTIVE_PROJECT.id != _default_id
            and _ACTIVE_PROJECT.root and not Path(_ACTIVE_PROJECT.root).exists()):
        _ui_print(col(f"[WARN] active project {_ACTIVE_PROJECT.id!r} root is gone "
                      f"({_ACTIVE_PROJECT.root}); falling back to the default project", C.YELLOW))
        _ACTIVE_PROJECT = _REGISTRY.get(_default_id)
        try:
            _REGISTRY.set_active(_default_id)
        except Exception:  # noqa: BLE001
            pass
    try:
        bind_active()
    except Exception as e:   # noqa: BLE001 — S14-2: a corrupt active entry must NEVER bind to the base
        # partition. Degrade to the legitimate `default` project (which uses the base by design — safe),
        # rather than leaking the corrupt project's memory into the base partition.
        _ui_print(col(f"[WARN] active project unbindable ({e!r}); binding the default project (safe base)",
                      C.YELLOW))
        try:
            default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default")
            dflt = _REGISTRY.get(default_id)
            _ACTIVE_PROJECT = dflt
            _pc.set_current(_engine_ctx_for(dflt) if dflt is not None else None)
        except Exception:   # noqa: BLE001
            _ACTIVE_PROJECT = None
            _pc.set_current(None)


def bind_active() -> None:
    """Bind THIS thread's ProjectContext to this process's cached active project. Called at boot and at the
    top of each daemon/request section so a background thread — which does not inherit the boot thread's
    contextvar — operates on the active project (and follows a ``/switch`` that updated the cache). No-op
    (leaves the ctx untouched) when there is no active project."""
    if _pc is None or _ACTIVE_PROJECT is None:
        return
    _pc.set_current(_engine_ctx_for(_ACTIVE_PROJECT))


# ─── Dev-process facade driver (ADR-0011 AD-3 / S6b) ──────────────────────────
# Make the curated public ``ack.devprocess.api`` facade LIVE in-engine by registering a driver that wraps
# the engine's own verbs. Only the verbs the engine OWNS in-process are wired: ``stage_handover`` and
# ``advance`` (their impls live here). ``select_unit`` (the deterministic selection policy) and ``deliver``
# (the supervised delivery leg) live in the private dev-loop substrate that ``core/`` must not import
# (boundary), and ``record_feedback`` is the server-side reconciler inbox — those three raise a clear
# ``SubstrateUnavailable`` rather than pretend. The engine impls are referenced by name (late binding), so
# a runtime reload / test monkeypatch of ``_stage_handover`` / ``_advance_pipeline`` is honoured.
class _EngineDevProcessDriver:
    """The in-engine driver registered into ``ack.devprocess.api`` at import — wires the two engine-owned
    verbs; the other three are not in-engine and say so."""

    def stage_handover(self, agent, handover_md, *, task_id=None, task_json=None,
                       set_active=True, force=False):
        return _stage_handover(task_id, agent, handover_md, task_json, set_active, force)

    def advance(self, task_id, agent, *, next_task_id=None):
        return _advance_pipeline(task_id, agent, next_task_id)

    def record_feedback(self, task_id, agent, content):
        raise _devapi.SubstrateUnavailable(
            "record_feedback is the server-side reconciler inbox (POST /feedback), not the in-engine facade")

    def select_unit(self, candidates, *, skip=()):
        raise _devapi.SubstrateUnavailable(
            "select_unit is provided by the dev-loop extension, not the in-engine facade")

    def deliver(self, unit, *, go, operator, secret, tree_sha, version, release_index,
                ledger_path, dial_config=None):
        raise _devapi.SubstrateUnavailable(
            "deliver is provided by the dev-loop extension, not the in-engine facade")


def _register_devprocess_driver() -> None:
    """Make ``ack.devprocess.api`` LIVE in-engine: import the facade (LATE — core/ is on sys.path by now,
    so the real launch resolves it) and register the in-engine driver, but ONLY if no driver is already
    registered — a richer driver a private/composite extension wired earlier must win (the engine driver is
    the minimal fallback). Idempotent + fail-soft (a missing facade ⇒ the tools call the impls directly)."""
    global _devapi
    try:
        from ack.devprocess import api as _devapi
    except Exception:   # noqa: BLE001 — facade absent ⇒ legacy direct path
        _devapi = None
        return
    try:
        if _devapi.get_driver() is None:
            _devapi.set_driver(_EngineDevProcessDriver())
    except Exception:   # noqa: BLE001 — registration must never break the import
        pass


_register_devprocess_driver()
#: Tools that operate on the *code* — they run where the code is. When a client is driving
#: a turn and has offered local execution, the server routes these THROUGH the client (it
#: runs them on the local filesystem and returns the result). Set by the server per turn.
LOCAL_TOOL_NAMES = frozenset({
    "read_file", "write_file", "list_directory", "execute_command", "search_files",
    "move_file", "delete_file", "copy_file", "create_directory",
})
#: The active client-tool bridge (callable ``(name, args) -> str``) or None. When set, the
#: tools in LOCAL_TOOL_NAMES are delegated to it instead of running server-side.
_LOCAL_TOOL_BRIDGE: Optional[Any] = None
#: Plugins discovered from the configured plugins dir (name → {"schema", "handler"}).
#: The OPEN extension surface: a skill (a ``.py`` under a ``skills/`` dir with a module
#: ``CASE`` dict + a ``run(...)`` function) becomes an agent tool — no core change. Empty
#: unless ``paths.plugins_dir`` / ``GX10_PLUGINS_DIR`` is set. See docs/plugin-api.md.
_PLUGIN_TOOLS: Dict[str, Dict[str, Any]] = {}
#: Playbook skills (the second kind, ADR-0001): ``SKILL.md`` packages discovered from the
#: plugins dir, exposed to the model via the ``use_skill`` tool with progressive disclosure
#: (list metadata → load body → load a reference on demand). capability → Playbook. Empty
#: unless playbooks are present. See docs/skill-packaging.md.
_PLAYBOOKS: Dict[str, Any] = {}
#: Prompt-library items (``kind: prompt``, ADR-0003): declarative MD prompts discovered as core
#: built-ins, exposed via the ``use_prompt`` tool (list → guided elicitation → multilingual
#: assemble). capability → ack.prompt.Prompt. Empty unless prompts are present.
_PROMPTS: Dict[str, Any] = {}

# ─── Colors ──────────────────────────────────────────────────
class C:
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    GRAY    = "\033[90m"
    MAGENTA = "\033[95m"
    BOLD    = "\033[1m"
    RESET   = "\033[0m"

#: Whether ANSI color is appropriate for the current sink. Finalized by
#: ``_setup_output()`` at startup; default True so interactive use is colored, but the
#: headless server (non-TTY) drops to plain so escape codes never leak into the HTTP API.
_COLOR_ENABLED = True


def col(text: str, c: str) -> str:
    return f"{c}{text}{C.RESET}" if _COLOR_ENABLED else text


def _color_supported() -> bool:
    """Runtime decision: emit ANSI only where it renders. NO_COLOR disables, FORCE_COLOR
    forces, a dumb/absent TTY disables (so piped/captured output stays clean)."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001 — no usable stdout → no color
        return False


def _setup_output() -> None:
    """Make output robust across runtimes (call once at startup):
    force UTF-8 on stdout/stderr so non-ASCII never raises (the Windows cp1252
    ``UnicodeEncodeError`` class), enable ANSI on Windows consoles, and decide whether
    color is appropriate for this sink. Idempotent and failure-tolerant."""
    global _COLOR_ENABLED
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # py3.7+ text streams
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        os.system("")  # turn on ANSI/VT processing in legacy Windows consoles
    _COLOR_ENABLED = _color_supported()

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>", re.I)
TOOLCALL_RE = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)

def clean(text: str) -> str:
    return THINK_RE.sub("", text).strip() if text else ""


def _answer_is_empty(content: object) -> bool:
    if not content:
        return True
    txt = clean(str(content))
    m = _THINK_OPEN_RE.search(txt)
    if m:
        txt = txt[:m.start()]
    return not txt.strip()


def _should_finalize_truncation(flag: bool, finalized: bool, tool_calls: object,
                                finish_reason: object, content: object) -> bool:
    return (
        bool(flag)
        and not finalized
        and not tool_calls
        and finish_reason == "length"
        and _answer_is_empty(content)
    )


def _accumulate_generation_metrics(perf: Dict[str, Any], turn: Dict[str, Any],
                                   metrics: Optional[Dict[str, Any]]) -> bool:
    if not metrics:
        return False
    perf["gens"]       += 1
    perf["prompt"]     += metrics.get("prompt_tokens") or 0
    perf["completion"] += metrics.get("completion_tokens") or 0
    perf["wall"]       += metrics.get("total") or 0.0
    turn["gens"]       += 1
    turn["prompt"]     += metrics.get("prompt_tokens") or 0
    turn["completion"] += metrics.get("completion_tokens") or 0
    return True

# ─── Thinking auto-classification ────────────────────────────
# Safe failure mode: when in doubt, THINK. Thinking is only switched off for clear
# routine (status/lookup/done) WITHOUT a planning verb.
_PLANNING_KW = (
    "erstell", "plane", "plan ", "zerleg", "analysier", "entscheid", "review",
    "architekt", "design", "warum", "weshalb", "vergleich", "refactor",
    "implementier", "konzept", "proposal", "handover", "bewerte", "strateg",
    "evaluier", "optimier", "begründ", "schlag vor", "entwirf",
)
_ROUTINE_KW = (
    "welche", "was ist offen", "offen", "status", "liste", "list ", "zeig",
    "übersicht", "überblick", "wie viele", "show", "open task", "lies ",
    "cat ", "ls ", "gib mir", "welcher", "welches",
    # Routine status queries (kept narrow — no broad "gibt es" / "liegt an",
    # so that diagnostic "woran liegt das?" still thinks):
    "etwas zu tun", "zu tun", "steht an", "todo", "to-do", "idle",
    "anything to do", "was liegt an", "liegt was an",
)
#: Smalltalk / identity / greetings — short conversational turns that need NO planning
#: round. Front-stops the "auto = think on doubt" default so trivial questions answer
#: in ~1s instead of running the reasoner up to the token cap. Length-gated so a long
#: task that merely contains a greeting word still thinks.
_SMALLTALK_KW = (
    "wer bist du", "wer bis du", "wie heißt du", "wie heisst du", "was bist du",
    "who are you", "what are you", "stell dich vor", "stell dich kurz vor",
    "hallo", "hi ", "hey", "moin", "servus", "guten morgen", "guten tag",
    "guten abend", "danke", "thanks", "thank you", "wie geht", "alles klar",
)


# ─── Streaming think filter (PERF-01 + PERF-02 display) ──────
class _ThinkFilter:
    """Incremental filter: suppresses everything between <think> and
    </think> across chunk boundaries. Holds back a possible partial
    tag at the end of the buffer so no tag is cut apart."""
    OPEN  = "<think>"
    CLOSE = "</think>"

    def __init__(self, open_tag: "Optional[str]" = None, close_tag: "Optional[str]" = None):
        # #1266: parameterizable so the SAME incremental suppressor also hides a model-emitted
        # <tool_call>…</tool_call> block (a TEXT tool call, not a native tool_calls delta) from the LIVE
        # render; the defaults keep the <think> behaviour (back-compat). The raw content still reaches
        # `parts` in the stream loop, so the post-turn text→tool_call recovery is UNAFFECTED.
        self.OPEN     = open_tag or self.OPEN
        self.CLOSE    = close_tag or self.CLOSE
        self.in_think = False
        self.buf      = ""
        self.entered  = False   # flips once on entering a suppressed block → drives a one-time render hint

    @staticmethod
    def _safe_cut(s: str, tag: str) -> int:
        """Index up to which s may be safely emitted/discarded;
        holds back a suffix that could be a prefix of tag."""
        maxk = min(len(tag) - 1, len(s))
        for k in range(maxk, 0, -1):
            if s.endswith(tag[:k]):
                return len(s) - k
        return len(s)

    def feed(self, text: str) -> str:
        self.buf += text
        out: List[str] = []
        while True:
            if not self.in_think:
                i = self.buf.find(self.OPEN)
                if i == -1:
                    cut = self._safe_cut(self.buf, self.OPEN)
                    out.append(self.buf[:cut])
                    self.buf = self.buf[cut:]
                    break
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(self.OPEN):]
                self.in_think = True
                self.entered  = True   # #1266: record entering a suppressed block (upstream one-time hint)
            else:
                j = self.buf.find(self.CLOSE)
                if j == -1:
                    cut = self._safe_cut(self.buf, self.CLOSE)
                    self.buf = self.buf[cut:]   # discard, keep partial tag
                    break
                self.buf = self.buf[j + len(self.CLOSE):]
                self.in_think = False
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.in_think else self.buf
        self.buf = ""
        return rest


# ─── Table-aware line output (code-rendered tables) ──
class _TableLineRenderer:
    """Takes text line by line, buffering pipe-table rows so a separator-less table (the model is told to omit
    the `|---|` row) is re-emitted as a PROPER GFM table (pipes + the separator inserted) for the
    markdown-rendering client to render as a box; every other line passes through UNCHANGED so bold/code/etc.
    reach the client. #1154 (epic #1144): was collapsing tables to pipe-less aligned columns + stripping `**`,
    a pre-markdown-client leftover the Ink client (marked-terminal) then showed as flat text."""

    def __init__(self, emit_line):
        self.emit_line = emit_line     # callable(str): emits ONE finished line
        self.buf   = ""
        self.table = []                # collected raw table rows (without separators)

    @staticmethod
    def _is_row(line: str) -> bool:
        s = line.strip()
        return s.startswith("|") and s.count("|") >= 2

    @staticmethod
    def _is_separator(line: str) -> bool:
        s = line.strip()
        core = s.replace("|", "").replace(":", "").replace("-", "").replace(" ", "")
        return s.startswith("|") and "-" in s and core == ""

    def feed(self, text: str):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self._line(line)

    def _line(self, line: str):
        if self._is_row(line):
            if not self._is_separator(line):
                self.table.append(line)
            return
        self._flush_table()
        self.emit_line(line)   # #1154: pass markdown through unchanged — the client renders bold/code/tables

    def _flush_table(self):
        if not self.table:
            return
        # #1154: re-emit the buffered rows as a PROPER GFM table (pipes kept + a `|---|` separator inserted,
        # since the model is told to omit it) so the markdown-rendering client renders a box.
        header = self.table[0]
        ncol = header.strip().strip("|").count("|") + 1
        self.emit_line(header)
        self.emit_line("|" + " --- |" * ncol)
        for r in self.table[1:]:
            self.emit_line(r)
        self.table = []

    def flush(self):
        if self.buf:
            self._line(self.buf)
            self.buf = ""
        self._flush_table()


# ─── Tool-call display (#1146/#1147: Claude-Code-style header + full result under a ⎿ corner) ──
# name → (human label, the arg key whose value is the subject). The internal tool name
# (`execute_command`, …) never reaches the user; the command/target does.
_TOOL_LABELS = {
    "execute_command":  ("Bash",   "command"),
    "read_file":        ("Read",   "path"),
    "write_file":       ("Write",  "path"),
    "write_last_reply": ("Write",  "path"),
    "search_files":     ("Search", "query"),
    "list_directory":   ("List",   "path"),
    "create_issue":     ("Issue",  "title"),
    "create_pr":        ("PR",     "title"),
    "comment_on_issue": ("Comment", "number"),
    "pr_status":        ("PR-checks", "number"),
    "web_search":       ("Search", "query"),
    "review":           ("Review", "focus"),
}


def _tool_display(name: str, args: dict) -> str:
    """A human, Claude-Code-style header for a tool call — the command / target, NOT the internal tool name
    (`execute_command(command='…')`). Falls back to ``name(<first meaningful arg>)`` then bare ``name``."""
    label, key = _TOOL_LABELS.get(name, (name, None))
    if key is None:
        for k in ("path", "query", "title", "task_id", "name", "url"):
            if k in (args or {}):
                key = k
                break
    val = ""
    if key and args:
        val = str(args.get(key, "")).replace("\n", " ").strip()
        if len(val) > 120:
            val = val[:120] + "…"
    return f"{label}({val})" if key else label


def _tool_result_lines(result_t: str, max_lines: int = 60) -> list:
    """The FULL tool result indented under a ``⎿`` corner (no 70-char cut). Output longer than *max_lines* is
    capped with an EXPLICIT ``… (+N more lines)`` — never a silent mid-line truncation."""
    lines = (result_t or "").splitlines() or [""]
    out = [("  ⎿ " if i == 0 else "     ") + ln for i, ln in enumerate(lines[:max_lines])]
    if len(lines) > max_lines:
        out.append(f"     … (+{len(lines) - max_lines} more lines)")
    return out


# ─── Global UI state ────────────────────────────────────────
_UI_MAX_LINES                 = 5000
_UI_LINES:   "deque[str]"     = deque(maxlen=_UI_MAX_LINES)
_UI_PARTIAL: str              = ""
_UI_LOCK                      = threading.Lock()
_UI_APP: Optional[Application] = None

# Headless capture hook: set by the server mode (server.py)
# when NO prompt_toolkit UI is running (_UI_APP is None). A callable(text:str)->None
# that taps the output (e.g. into a thread-local request buffer) instead of printing
# to stdout. Stays None in normal CLI/REPL operation → behaviour unchanged.
_UI_SINK: Optional[Callable[[str], None]] = None

_INPUT_QUEUE: _q.Queue        = _q.Queue()
_CANCEL_EVENT                 = threading.Event()
_EXEC_COMMAND_TIMEOUT_S       = 30
_COMMAND_CANCEL_POLL_S        = 0.05
_POST_KILL_DRAIN_S            = 2.0   # #1489: bound the reap after a tree kill so a survivor can't hang the tool
_SANDBOX_BEST_EFFORT_WARNED   = False
_RELOAD_FLAG                  = False
_WATCHER_ENABLED              = False   # auto-advance via reconciler; enabled only by /auto on
RECONCILER_INTERVAL           = 3.0     # polling interval (s)
_ADVANCE_CMD                  = "\x00advance\x00"   # internal structured reconciler command
_LAUNCH_CMD                   = "\x00launch\x00"    # internal autopilot launch command

# Autopilot: counter of reserved/running claude processes (concurrency gate)
_AUTOPILOT_ACTIVE             = 0
_AUTOPILOT_LOCK               = threading.Lock()
_AUTOPILOT_PROCS: Dict[str, Any] = {}   # task_id -> Popen (for targeted termination on advance)
_LOG_CAP_BYTES                = 8 * 1024 * 1024   # Coder logs are diagnostic; cap endless output (#1548).

_status = {"thinking": False, "label": "ready"}

# Effectively loaded config + source (set in main()) — for the `config` command.
_EFFECTIVE_CFG: Optional[Dict[str, Any]] = None
_CONFIG_LOCK = threading.RLock()
TOOLING_ENVELOPE_POLICY = None
_CFG_SOURCE: Optional[Path] = None

def _ui_print(*args, sep: str = " ", end: str = "\n", flush: bool = False):
    """Universal output: Application window or fallback stdout."""
    global _UI_PARTIAL
    text = sep.join(str(a) for a in args)
    if _UI_APP is not None:
        with _UI_LOCK:
            _UI_PARTIAL += text + end
            if "\n" in _UI_PARTIAL:
                parts       = _UI_PARTIAL.split("\n")
                _UI_PARTIAL = parts.pop()
                _UI_LINES.extend(parts)
        _UI_APP.invalidate()
    elif _UI_SINK is not None:
        # Headless server mode: send the output to the capture hook instead of stdout.
        _UI_SINK(text + end)
    else:
        print(*args, sep=sep, end=end, flush=flush)

_ANSI_LEN_RE = re.compile(r"\x1b\[[0-9;]*m")

#: #1196: ANSI escapes to strip from a tool result before it enters the model context / the ingestion cap
#: — the model must read clean text (escape bytes are noise and skew the char count), while the DISPLAY
#: stream keeps the colour. Covers CSI (SGR colour `…m`, cursor/erase), OSC (`…]…BEL/ST`, e.g. title-set),
#: and a bare two-byte Fe escape, so a crafted `ls`/shell output can't smuggle a non-CSI escape past the
#: strip into the model view.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC …  BEL | ST
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"          # CSI …
    r"|\x1b[@-Z\\-_]"                      # a bare 2-byte Fe escape
)


def _strip_ansi(s: str) -> str:
    """Remove ANSI escapes (CSI colour/control + OSC + bare Fe) from *s* — for the model-facing copy of a
    tool result. The DISPLAY keeps the raw bytes (the renderer sandboxes them)."""
    return _ANSI_ESCAPE_RE.sub("", s) if s else s


def _has_ansi(s: str) -> bool:
    """True iff *s* carries an ANSI escape (e.g. `ls --color` output) — the display streams it as-is."""
    return "\x1b[" in s


def _visual_rows(line: str, width: int) -> int:
    """How many screen rows a (possibly wrapping) line occupies —
    ANSI color codes do not count toward the width."""
    n = len(_ANSI_LEN_RE.sub("", line))
    return max(1, -(-n // width))   # ceil(n/width)

def _get_output():
    # IMPORTANT: take the size from prompt_toolkit's OWN source when the app
    # is running — otherwise it can diverge from shutil (until the first resize), and
    # the tail budget doesn't match the actual window height → the bottom
    # lines (perf, ✓ DONE) get clipped until you move the terminal.
    term_rows = term_cols = None
    if _UI_APP is not None:
        try:
            sz = _UI_APP.output.get_size()
            term_rows, term_cols = sz.rows, sz.columns
        except Exception:
            term_rows = term_cols = None
    if term_rows is None:
        s = shutil.get_terminal_size((80, 24))
        term_rows, term_cols = s.lines, s.columns
    # Bottom fixed UI: separator(1) + input(1) + separator(1) + toolbar(3) = 6.
    rows  = max(1, term_rows - 6)
    width = max(1, term_cols)
    with _UI_LOCK:
        lines = list(_UI_LINES)
        if _UI_PARTIAL:
            lines.append(_UI_PARTIAL)
    # Collect from the END until the window (in VISIBLE, wrapped lines)
    # is full — this guarantees the newest line (✓ DONE) stays visible at the bottom.
    visible: List[str] = []
    used = 0
    for ln in reversed(lines):
        vis = _visual_rows(ln, width)
        if visible and used + vis > rows:
            break
        visible.append(ln)
        used += vis
    visible.reverse()
    return ANSI("\n".join(visible))

def _toolbar():
    cwd   = str(Path.cwd())
    frame = SPINNER_FRAMES[int(time.time() * 8) % len(SPINNER_FRAMES)]

    # Status indicators — always visible, even during thinking
    w_color = "fg:ansigreen bold" if _WATCHER_ENABLED    else "fg:ansired bold"
    a_color = "fg:ansigreen bold" if AUTOPILOT_ENABLED   else "fg:ansigray"
    r_color = "fg:ansigreen bold" if (AUTOPILOT_ENABLED and AUTOPILOT_AUTOPLAN) else "fg:ansigray"
    r_label = (" Autoplan  " if not (AUTOPILOT_AUTOPLAN and AUTOPILOT_MAX_TASKS > 0)
               else f" Autoplan({_AUTOPLAN_DONE}/{AUTOPILOT_MAX_TASKS})  ")
    t_color = "fg:ansigreen bold" if (AUTOPILOT_ENABLED and AUTOPILOT_LOG_TERMINAL) else "fg:ansigray"
    _mem_ok = _MEMORY is not None and _MEMORY.is_available()
    m_color = "fg:ansigreen bold" if _mem_ok else "fg:ansigray"
    w_dot   = "●" if _WATCHER_ENABLED  else "○"
    a_dot   = "●" if AUTOPILOT_ENABLED else "○"
    r_dot   = "●" if (AUTOPILOT_ENABLED and AUTOPILOT_AUTOPLAN)  else "○"
    t_dot   = "●" if (AUTOPILOT_ENABLED and AUTOPILOT_LOG_TERMINAL) else "○"
    m_dot   = "●" if _mem_ok else "○"

    if _status["thinking"]:
        return [
            ("fg:ansiblue bold", " ██ "),
            ("bold",             "Ironclad"),
            ("",                 "  powered by "),
            ("fg:ansiblue bold", "MJWC-AI-LAB"),
            ("",                 "\n"),
            ("fg:ansiblue bold", " ██ "),
            ("",                 f"  {frame}  {_status['label']}...   Ctrl+C = cancel   "),
            (w_color,            w_dot),
            ("",                 " Watcher  "),
            (a_color,            a_dot),
            ("",                 " Autopilot  "),
            (r_color,            r_dot),
            ("",                 r_label),
            (t_color,            t_dot),
            ("",                 " LogTerm  "),
            (m_color,            m_dot),
            ("",                 " Memory\n"),
            ("fg:ansigray",      f"     {cwd}"),
        ]
    return [
        ("fg:ansiblue bold", " ██ "),
        ("bold",             "Ironclad"),
        ("",                 "  powered by "),
        ("fg:ansiblue bold", "MJWC-AI-LAB"),
        ("",                 "\n"),
        ("fg:ansiblue bold", " ██ "),
        ("",                 "  Orchestrator Engine  ·  streaming  |  exit = Beenden   "),
        (w_color,            w_dot),
        ("",                 " Watcher  "),
        (a_color,            a_dot),
        ("",                 " Autopilot  "),
        (r_color,            r_dot),
        ("",                 r_label),
        (t_color,            t_dot),
        ("",                 " LogTerm  "),
        (m_color,            m_dot),
        ("",                 " Memory\n"),
        ("fg:ansigray",      f"     {cwd}"),
    ]


# ─── Spinner ─────────────────────────────────────────────────
class Spinner:
    def __init__(self, label: str = "Qwen thinking"):
        self._label = label

    def start(self):
        _status["thinking"] = True
        _status["label"]    = self._label
        if _UI_APP:
            _UI_APP.invalidate()

    def stop(self):
        _status["thinking"] = False
        if _UI_APP:
            _UI_APP.invalidate()

# ─── Tool definitions ────────────────────────────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": ("Read a file — prefer a TARGETED read. For a large or unknown file, use "
                            "search_files first to locate the relevant lines, then read only that range: pass "
                            "start/end (1-based inclusive line numbers) or a regex `pattern` (reads a window "
                            "around the first match). Omitting the range reads the file (head+tail-capped if "
                            "large). Good for handovers, task JSONs, feedback, CLAUDE.md."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "1-based first line to read (inclusive)"},
                    "end": {"type": "integer", "description": "1-based last line to read (inclusive)"},
                    "max_chars": {"type": "integer", "description": "cap the returned slice to this many chars"},
                    "pattern": {"type": "string", "description": "regex: read a window of lines around the first match"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file. Creates missing parent directories. "
                "For LARGE content, prefer write_last_reply (small models mis-escape a big JSON 'content'). "
                "Set mode='append' to add to an existing file (build a large file in chunks). "
                "Handover naming: KGC-XXX_OPUS.md, KGC-XXX_SONNET.md. "
                "Feedback: KGC-XXX_OPUS-feedback.md, KGC-XXX_SONNET-feedback.md. "
                "IMPORTANT: If a conflicting ID exists, use move_file to rename first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"},
                    "mode":    {"type": "string", "enum": ["write", "append"],
                                "description": "write (default: replace) or append to the end"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_last_reply",
            "description": (
                "Write your PREVIOUS message's text to a file — ESCAPE-FREE authoring for LARGE content. "
                "Produce the full file body as your normal reply text, THEN call write_last_reply(path); "
                "do NOT stuff large content into write_file's JSON 'content' (small models mis-escape it and "
                "the write is dropped). Use mode='append' to extend a file built in chunks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "mode": {"type": "string", "enum": ["write", "append"]}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an EXACT string in a file — a targeted edit, cheaper and safer than rewriting the "
                "whole file with write_file. old_string must match EXACTLY (whitespace/indentation included) "
                "and appear ONCE (include surrounding context to make it unique) unless replace_all is true. "
                "Fails if old_string is absent or (without replace_all) not unique."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":        {"type": "string"},
                    "old_string":  {"type": "string"},
                    "new_string":  {"type": "string"},
                    "replace_all": {"type": "boolean", "description": "replace every occurrence (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
            }
        }
    },
    # #1200: list_directory is deliberately NOT offered to the model — a listing must always run
    # through the shell (execute_command: bash `ls` / PowerShell), so the transcript look never
    # flips between `$ ls -la` output and a `[D]/[F]` list per sampled tool choice. The handler,
    # the bridge case and LOCAL_TOOL_NAMES keep the tool alive for `/ls` (manual_ls) + API callers.
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": "Execute a shell command. Use for: docker compose config, git status, validation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 30}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Move, rename, or resolve file conflicts. "
                "ID conflict resolution: rename existing file before writing new one. "
                "For task transitions and handover archiving DO NOT move files by hand — use "
                "advance_pipeline (it routes everything under the active initiative deterministically)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source":      {"type": "string"},
                    "destination": {"type": "string"}
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file. Use to clean up handovers after task completion.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": (
                "Copy a file without removing the original. For the task/handover/feedback "
                "workflow use the deterministic tools (stage_handover / advance_pipeline) instead — "
                "they route under the active initiative; copying by hand bypasses that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source":      {"type": "string"},
                    "destination": {"type": "string"}
                },
                "required": ["source", "destination"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": ("Search files with a REGEX (case-insensitive; e.g. "
                            "'vLLM|rate.limit'). An invalid pattern falls back to a literal "
                            "substring. For task JSONs set file_pattern='*.json'."),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":      {"type": "string"},
                    "directory":    {"type": "string", "default": "."},
                    "file_pattern": {"type": "string", "default": "*.md"}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "Create a directory and all parent directories.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "advance_pipeline",
            "description": (
                "Advance the workflow pipeline for ONE completed task in a single "
                "deterministic step (everything under the ACTIVE initiative): archive the active "
                "handover, archive the feedback, set the task JSON to status=done and move it to "
                "tasks/done/, delete the handover from the .work/handovers inbox, and optionally "
                "activate the next task. "
                "On 'done' ALWAYS use this tool instead of individual "
                "move_file/copy_file/delete_file calls. Fail-closed: aborts if the "
                "feedback file is missing OR no initiative is active. Never touches code/ or the audit chain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id":      {"type": "string", "description": "e.g. KGC-315"},
                    "agent":        {"type": "string", "enum": ["OPUS", "SONNET"]},
                    "next_task_id": {"type": "string", "description": "optional — the next task to activate"}
                },
                "required": ["task_id", "agent"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stage_handover",
            "description": (
                "Create a NEW task+handover in ONE step. The system (TaskStore) assigns "
                "the ID, stamps created_at and deterministically checks for TOPIC "
                "DUPLICATES — so do NOT pass an id or a created_at yourself (they are "
                "ignored/overwritten). If a task on the same topic already exists, NOTHING "
                "is created and the existing task is named — then use that one, do not "
                "force a new one. When creating/handing off ALWAYS use this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent":       {"type": "string", "enum": ["OPUS", "SONNET"]},
                    "handover_md": {"type": "string", "description": "the full handover markdown"},
                    "task_json":   {"type": "string", "description": "task JSON as a string (title, description, type, priority required; omit id/created_at — the store assigns them)"},
                    "task_id":     {"type": "string", "description": "optional — only for a pure handover WITHOUT task_json"},
                    "set_active":  {"type": "boolean", "description": "optional, default true"},
                    "force":       {"type": "boolean", "description": "optional — override dedup (ONLY on an explicit operator instruction)"}
                },
                "required": ["agent", "handover_md"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "plan_units",
            "description": (
                "Materialize the APPROVED design's FULL decomposition in ONE call: creates one "
                "'epic' task plus ALL implementation units as pending tasks linked to it "
                "(parent=epic id) — deliberately WITHOUT handovers (each unit's handover is "
                "authored later, when the loop selects that unit). Use this ONCE after design "
                "approval instead of staging tasks one by one; the engine then works the units "
                "off in deterministic order (priority, then creation) until the epic is done. "
                "The store assigns ids/created_at and checks topic duplicates (fail-closed, "
                "atomic: on any refusal NOTHING is created). To add units to an existing open "
                "epic later (plan change), pass epic_id instead of epic_json. Within the batch, "
                "a unit may depend on a sibling via 'unit:<n>' (its 1-based position)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "epic_json":  {"type": "string", "description": "epic task JSON as a string (title, description, priority required; type is fixed to 'epic'; omit id/created_at)"},
                    "units_json": {"type": "string", "description": "JSON ARRAY of unit task objects (each: type, priority, title, description required; optional dependencies — real Task-IDs or 'unit:<n>' sibling refs)"},
                    "epic_id":    {"type": "string", "description": "optional — add the units under this EXISTING open epic instead of creating one (omit epic_json then)"},
                    "force":      {"type": "boolean", "description": "optional — override dedup (ONLY on an explicit operator instruction)"}
                },
                "required": ["units_json"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "launch_coder",
            "description": ("Start the coding agent for the CURRENT staged handover NOW (the one you just "
                            "created via stage_handover). Resolves the newest pending task that has a "
                            "handover, launches its agent, and flips the task to in_progress. Use this right "
                            "after stage_handover when the session must start now — you are the one steering "
                            "author, so you trigger it (autopilot stays off by default). Fail-closed: a clear "
                            "message if nothing is staged, a coder is already running, or no agent is "
                            "configured on this box. Never double-launches."),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string",
                                "description": "optional — a specific task to launch; default = the current "
                                               "(newest) pending handover"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "record_design",
            "description": ("Persist the DESIGN for the active unit BEFORE any implementation. Call this "
                            "after your analysis: `title` = the design's title, `body` = the design (goal, "
                            "the chosen approach/technology + WHY, architecture, the facets to cover). It "
                            "writes a design proposal (proposals/design-N.md) the engine reads, then you STOP — an "
                            "implementation stage_handover is REFUSED until the operator approves the design "
                            "(/approve). This is the no-blind-coding contract (R2)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "the design's title"},
                    "body": {"type": "string",
                             "description": "the design content — approach/technology + why, architecture, facets"},
                    # #1341: optional machine-checkable typed fields (allow-listed; invalid → refusal).
                    "language": {"type": "string",
                                 "description": "optional — design language token (e.g. python, rust, go)"},
                    "network": {"type": "string",
                                "description": "optional — design network stance (none/forbidden or allowed)"},
                },
                "required": ["title", "body"]
            }
        }
    }
]

# #1338: conditional L1 constraint-capture tool. It is deliberately separate from static ``TOOLS`` so the
# default-off model surface remains byte-identical.
CONSTRAINT_TOOL = {
    "type": "function",
    "function": {
        "name": "record_constraints",
        "description": ("Record optional framing notes for the active unit. These notes are context only: "
                        "they do not gate design approval, implementation, or fork decisions, and the "
                        "engine stores them under notes/framing.md. Use `none` when there is no framing "
                        "context worth preserving."),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "the framing note title"},
                "body": {"type": "string", "description": "framing context, taboos, preferences, or `none`"},
                "language": {"type": "string", "description": "optional compatibility field; validated if present"},
                "network": {"type": "string", "description": "optional compatibility field; validated if present"},
                "source": {"type": "string", "description": "ignored compatibility field"},
            },
            "required": ["title", "body"],
        },
    },
}

# query_memory is offered as soon as memory is CONFIGURED (any mode, NOT onboarding-only):
# _effective_tools() adds [MEMORY_TOOL, DEEP_MEMORY_TOOL] when `_MEMORY is not None`. (The
# onboarding-only duplicate pre-check is a different tool, check_task_exists in ONBOARDING_TOOLS.)
MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "query_memory",
        "description": (
            "Search the persistent agent memory for relevant context: past task "
            "patterns, architecture decisions, known gotchas and solution approaches. "
            "Call before stage_handover for complex tasks to find relevant past "
            "decisions. Also useful for research."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query (natural language)"},
                "limit": {"type": "integer", "description": "max number of results (default 8)"}
            },
            "required": ["query"]
        }
    }
}

# §3-Mechanismus 5 / MEM-10: opt-in RELATIONAL memory query over the graph. Slower, off the hot
# path; offered only when memory is configured. The hot read (query_memory) stays vector-only.
DEEP_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_query_memory",
        "description": (
            "RELATIONAL / multi-hop memory query over the knowledge graph (e.g. 'what depends on "
            "task X', 'how are A and B connected'). Slower than query_memory and off the hot path "
            "— use it only when a plain query_memory (vector) isn't enough for a connection or "
            "dependency question, NOT for routine lookups."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "relational query (natural language)"},
                "limit": {"type": "integer", "description": "max number of results (default 5)"}
            },
            "required": ["query"]
        }
    }
}

# #1076 (epic #1043 quick-win): DELIBERATE memory write — the model persists a durable fact/decision so it
# survives the session and is retrieved later (query_memory / RAG). Offered only when memory is configured;
# scope-aware (the active project partition) + fail-soft (best-effort, like the eviction archive).
MEMORY_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Deliberately persist a durable fact / decision / gotcha into the project's long-term memory so "
            "it survives THIS session and is retrieved later via query_memory / RAG. Use for cross-session "
            "knowledge worth keeping (a resolved gotcha, a chosen approach, a key constraint) — NOT for "
            "transient turn state. Confirm you stored it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the fact/decision to remember (self-contained)"},
            },
            "required": ["text"],
        },
    },
}

# Phase-e: server-side parallel reasoning. Offered only when the governed fan-out
# workers are present (i.e. running under the server). The model uses it to process
# many INDEPENDENT items in one concurrent batch instead of a serial tool loop.
PARALLEL_TOOL = {
    "type": "function",
    "function": {
        "name": "parallel_reason",
        "description": (
            "Reason over several INDEPENDENT items at once — they run concurrently "
            "against the local model and come back together, far faster than one at a "
            "time. Use for batch analysis / classification / review of many items, or a "
            "multi-candidate planning panel. NOT for steps that depend on each other "
            "(there is no shared state between items). Concurrency is governed for GPU "
            "safety, so passing many items is fine — overflow just queues."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "the independent units to reason over — one model call each",
                },
                "instruction": {
                    "type": "string",
                    "description": "shared instruction applied to every item (e.g. 'Classify the sentiment as pos/neg/neutral')",
                },
                "max_tokens": {"type": "integer", "description": "optional per-item token cap"},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"],
                           "description": "optional reasoning effort per item (routes to a token budget; default medium)"},
            },
            "required": ["items"],
        },
    },
}

# #459 (epic #440 P6, §4 / FORK-H): first-class web search. Offered ONLY when a web-capable provider is
# configured (see _effective_tools). Runs SERVER-side through the provider lane via the captured CLI
# runner → structurally immune to the console-write scaling break. This is the tool the model must use
# for current/latest/today information INSTEAD of improvising a shell web fetch (execute_command).
# (epic #505 R2: the spec's `deferByDefault` is N/A here — tools are offered wholesale via
# _effective_tools with plain function-JSON schemas; there is no defer / lazy-context tool registry.)
WEBSEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for CURRENT / LATEST / real-time information (today's news, recent events, "
            "live prices, 'what is the latest …'). ALWAYS use this for anything time-sensitive or "
            "post-training-cutoff — do NOT try to fetch the web with execute_command (a shell web "
            "request is blocked and corrupts the display). Optionally scope the search with "
            "allowDomains/blockDomains (mutually exclusive, concrete domains, no wildcards). Returns "
            "the search result text; after searching, cite the relevant sources as Markdown links."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "the search query (natural language)"},
                # Optional domain filters. Kept grammar-clean (plain array of strings, no
                # minLength/pattern/minItems) so the structured-outputs path never 400s; the
                # real rules (>=2-char query, allow XOR block, normalization, wildcard reject)
                # are enforced in the websearch validator at the tool boundary, not here.
                "allowDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("optional: restrict the search to ONLY these domains "
                                    "(e.g. ['docs.python.org']); mutually exclusive with "
                                    "blockDomains; concrete domains, no wildcards"),
                },
                "blockDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("optional: exclude these domains from the search; mutually "
                                    "exclusive with allowDomains; concrete domains, no wildcards"),
                },
            },
            "required": ["query"],
        },
    },
}

# #1073 (epic #1043 quick-win): let the orchestrator FILE its own tracker issues instead of falling back to
# writing a body file it cannot submit. Gated (default OFF — forge.enabled / GX10_FORGE_ENABLED), secret-free
# (uses the ambient `gh` CLI auth — no token on the wire, no repo literal in core), and ESCAPE-FREE (the body
# comes from a FILE the model already wrote via write_last_reply, never a giant JSON arg). Registered in
# _effective_tools ONLY when enabled → byte-identical when off.
CREATE_ISSUE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_issue",
        "description": (
            "Create a tracker issue in the project's code forge (GitHub, via the `gh` CLI). The body comes "
            "from a FILE (escape-free): FIRST write the issue body with write_last_reply / write_file, THEN "
            "pass its path as body_file (do NOT inline a large body). Optional comma-separated `labels` — these "
            "must ALREADY EXIST in the repo (an unknown label is rejected with the valid set; do not invent "
            "labels). Optional `milestone` (existing title) and `parent` (an epic issue number — links this "
            "issue as a native sub-issue of that epic). Returns the created issue URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title":     {"type": "string"},
                "body_file": {"type": "string", "description": "path to a file holding the issue body (escape-free)"},
                "labels":    {"type": "string", "description": "optional comma-separated labels — must be labels that already exist in the repo"},
                "milestone": {"type": "string", "description": "optional milestone title"},
                "parent":    {"type": "string", "description": "optional parent epic issue number — links this issue as a native sub-issue"},
            },
            "required": ["title", "body_file"],
        },
    },
}

# #1208: the READ counterpart to create_issue — the first-class path for resolving a `#NNN` reference.
# The agent flailed on "check #1207" because it had NO issue-read tool: it fell back to the generic shell and
# grepped git history (which only ever cites issues a merged PR CLOSED, so an OPEN issue is invisible there),
# then falsely concluded "does not exist". view_issue queries the tracker directly. Capability-detected +
# trust-gated exactly like create_issue (offered together via _forge_available), so the forge surface stays
# uniform. Offered in _effective_tools ONLY when available → byte-identical when off.
VIEW_ISSUE_TOOL = {
    "type": "function",
    "function": {
        "name": "view_issue",
        "description": (
            "Read a tracker issue from the project's code forge (GitHub, via the `gh` CLI) by its NUMBER. This "
            "is the CORRECT way to check/resolve a `#NNN` reference (e.g. the operator says 'check #1207') — "
            "NEVER search git history or branches for it (commit messages only cite issues a merged PR closed, "
            "so an open issue is invisible there). Returns the issue's number, state, title, labels, milestone, "
            "url and body. A non-existent number returns an authoritative 'NOT_FOUND' (the tracker WAS queried) "
            "— never conclude an issue does not exist from the absence of a commit."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "the issue number (with or without a leading '#')"},
            },
            "required": ["number"],
        },
    },
}

# #1215 (epic #1212): open a PR through the forge adapter — the WRITE-sibling of create_issue for the
# Issue→Branch→PR→Merge dev loop. Escape-free (body from a FILE), capability-detected + sealed-gated like
# create_issue, and OPEN-ONLY (it never merges — merge stays a CI/review gate). Offered only when available.
CREATE_PR_TOOL = {
    "type": "function",
    "function": {
        "name": "create_pr",
        "description": (
            "Open a pull request in the project's code forge (GitHub). The body comes from a FILE "
            "(escape-free): FIRST write the PR body with write_last_reply / write_file, THEN pass its path as "
            "body_file. Include 'Closes #<N>' in the body to link the issue. Optional `base` (target branch; "
            "default the repo's default branch), `head` (source branch; the cli path infers the current branch, "
            "the native path REQUIRES it), and `draft`. Returns the PR URL. Does NOT merge — merge stays a "
            "CI/review gate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title":     {"type": "string"},
                "body_file": {"type": "string", "description": "path to a file holding the PR body (escape-free)"},
                "base":      {"type": "string", "description": "optional target branch (default: repo default)"},
                "head":      {"type": "string", "description": "optional source branch (cli infers it; native requires it)"},
                "draft":     {"type": "string", "description": "optional 'true' to open as a draft PR"},
            },
            "required": ["title", "body_file"],
        },
    },
}

# #1217 (epic #1212): append a comment to an existing tracker issue through the forge adapter — the third leg
# of create/read/comment. NARROW (comment only, never close/relabel — close is policy-sensitive). Escape-free
# (body from a FILE), capability-detected + sealed-gated like create_issue. Offered only when available.
COMMENT_ISSUE_TOOL = {
    "type": "function",
    "function": {
        "name": "comment_on_issue",
        "description": (
            "Append a comment to an existing tracker issue in the project's code forge (GitHub) by NUMBER. The "
            "body comes from a FILE (escape-free): FIRST write the comment with write_last_reply / write_file, "
            "THEN pass its path as body_file — NEVER shell out to `gh issue comment`. Returns the posted "
            "comment URL; a non-existent number returns an authoritative 'NOT_FOUND'. Comment-ONLY — it does "
            "NOT close, reopen, or relabel the issue."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "number":    {"type": "string", "description": "the issue number (with or without a leading '#')"},
                "body_file": {"type": "string", "description": "path to a file holding the comment body (escape-free)"},
            },
            "required": ["number", "body_file"],
        },
    },
}

# #1219 (epic #1212): read a PR's CI/mergeability SNAPSHOT — the merge-readiness gate of the dev loop. A
# NON-BLOCKING read (never waits/watches — the engine runs one agent turn behind a single lock; re-poll
# across turns). Capability-detected + sealed-gated. Offered only when available.
PR_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "pr_status",
        "description": (
            "Read the CI + mergeability status of a pull request in the project's code forge (GitHub) by "
            "NUMBER — the correct way to judge if a PR is mergeable. Returns a deterministic per-check summary "
            "(name -> pass/fail/pending) plus an overall verdict (ALL PASSING / N FAILING / N PENDING) and the "
            "mergeable / mergeStateStatus / reviewDecision. A SNAPSHOT of the current state, NOT a wait — "
            "re-call on a LATER turn to poll; NEVER scrape a shell table for merge-readiness. A non-existent "
            "number returns an authoritative 'NOT_FOUND'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "number": {"type": "string", "description": "the PR number (with or without a leading '#')"},
            },
            "required": ["number"],
        },
    },
}

# #1221 (epic #1212): generic cross-model second-opinion review — ANY configured code-agent (KIMI/SONNET/
# CODEX/OPUS/…), not codex-only; works in EVERY area (a git diff by default, or named paths for docs/
# decisions/plans/artifacts). Mechanism: `_code_agent_registry()` + `client.default_cli_runner` (existing
# synchronous hardened-env CLI runner) — no new reviewer backend. Capability-detected (offered only when a
# reviewer agent is runnable on this box). A READ of untrusted reviewer text → `_INGESTION_TOOLS`. Bounded
# synchronous call (`review.timeout_s`), never a watch/poll.
REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "review",
        "description": (
            "Get an INDEPENDENT cross-model second opinion from a configured code-agent reviewer (KIMI / "
            "SONNET / CODEX / OPUS / … — any agent in the registry). Use this to review a working git diff "
            "(default), named files/docs/decisions/plans (`paths`), or any artifact you produced or are "
            "weighing — NEVER self-review. Optional `focus` steers the review; optional `agent` selects the "
            "reviewer (default: config `review.agent`, else a distinct peer from the producer via anti-affinity). "
            "Returns the reviewer's structured findings (Summary / Findings / Recommendations / Verdict). "
            "Bounded and synchronous — not a watch/poll."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "focus": {
                    "type": "string",
                    "description": "optional review focus (e.g. 'security', 'API contract', 'doc completeness')",
                },
                "agent": {
                    "type": "string",
                    "description": ("optional reviewer agent_id from the code-agent registry; default is "
                                    "config review.agent or a distinct peer (never self-review when a peer exists)"),
                    "enum": ["OPUS", "SONNET"],
                },
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("optional files/docs/artifacts to review; omit to review the working "
                                    "git diff (staged + unstaged vs HEAD)"),
                },
            },
            "required": [],
        },
    },
}

# #1074 (epic #1043 quick-win): read a specific http(s) page verbatim (RFCs/standards/API specs/docs) —
# web_search FINDS pages, fetch_url READS one. Offered only when the trust profile allows outbound
# (blocked under sealed unless security.web_in_sealed). Bounded: a hard byte cap + a basic SSRF guard
# (http/https only, no loopback/private/link-local target) + the ingestion choke-point char cap.
_FETCH_MAX_BYTES = 2_000_000


def _fetch_url_blocked(url: str) -> Optional[str]:
    """Return a BLOCKED reason if *url* is not plain http(s) or resolves to a loopback/private/link-local
    address (an autonomous agent must not pivot to internal services via fetch_url), else None. A DNS
    failure is NOT treated as blocked — the fetch itself then fails with a clear error. Never raises."""
    try:
        u = urllib.parse.urlparse(url)
    except Exception:  # noqa: BLE001
        return "malformed URL"
    if u.scheme not in ("http", "https"):
        return f"scheme {u.scheme or '(none)'} not allowed — http/https only"
    host = (u.hostname or "").lower()
    if not host:
        return "no host in URL"
    if host == "localhost" or host.endswith(".localhost"):
        return "loopback host blocked (SSRF guard)"
    try:
        import ipaddress
        import socket
        port = u.port or (443 if u.scheme == "https" else 80)
        for _fam, _t, _p, _c, sockaddr in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                return f"host resolves to a non-public address ({ip}) — blocked (SSRF guard)"
    except Exception:  # noqa: BLE001 — resolution failure: let the fetch fail with its own error
        pass
    return None


FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch the raw text of an http(s) URL verbatim (RFCs, standards, API specs, docs), size-capped. "
            "Use web_search to FIND pages; use fetch_url to READ a specific one. Returns the decoded body "
            "(truncated to fit the window). Blocked under the sealed trust profile and for non-public hosts."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "the http(s) URL to fetch"}},
            "required": ["url"],
        },
    },
}

ONBOARDING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_task_exists",
            "description": (
                "Cheaply check BEFORE writing a handover whether a task on the same "
                "topic already exists (same logic as the stage_handover dedup gate). "
                "Returns 'EXISTS: KGC-XXX' or 'NONE'. In onboarding mode ALWAYS call this "
                "first to avoid expensive handover generation for duplicates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title":       {"type": "string"},
                    "description": {"type": "string", "description": "optional — sharpens the similarity check"}
                },
                "required": ["title"]
            }
        }
    }
]

def _code_agent_registry():
    """The active code-agent registry (#449, config-driven, always-on). Rebuilt from the current
    effective config each call (cheap: a handful of specs) so /config edits take effect. The
    handover/launch lane resolves agent_id → spec through this; an unknown agent fails closed."""
    from providers import load_code_agents
    cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
    return load_code_agents(cfg)

def _agent_names() -> List[str]:
    """Configured handover-agent tokens (e.g. OPUS/SONNET[/CODEX/KIMI]) — the dynamic schema enum."""
    return _code_agent_registry().names()

def _is_sealed_profile() -> bool:
    """#480: True iff the configured trust profile is ``sealed`` (env GX10_PROFILE > config > 'open')."""
    prof = (os.environ.get("GX10_PROFILE")
            or ((_EFFECTIVE_CFG or {}).get("security") or {}).get("profile") or "open")
    return str(prof).strip().lower() == "sealed"

def _mcp_for_launch(spec) -> Tuple[str, Dict[str, str]]:
    """#480: the (mcp_args, mcp_env) to inject into a code agent's launch — the read-only Memory MCP,
    ALWAYS ON (#994-S10) when a memory service is configured + the agent's mcp_template — the read-only Memory
    MCP is no longer sealed-gated. ("", {}) when memory is unconfigured (the agent launches byte-identically).
    Fail-soft."""
    try:
        import memory_mcp
        cfg = _MEMORY_CONFIG or {}
        return memory_mcp.render_mcp_launch(
            getattr(spec, "mcp_template", None),
            memory_url=str(cfg.get("base_url") or ""),
            namespace=_active_mem_ns(default=str(cfg.get("agent_id") or "ironclad")),  # S3b: the active project's partition
        )
    except Exception:  # noqa: BLE001 — never let the MCP seam break a launch
        return "", {}

def _code_agent_pin() -> Optional[str]:
    """The runtime operator pin (`/coders use <id>`), upper-cased, or None. Only honoured when it
    names a CONFIGURED agent — an unknown/disabled pin fails closed (treated as no pin)."""
    pin = ((_EFFECTIVE_CFG or {}).get("code_agents") or {}).get("pinned")
    pin = (pin or "").strip().upper()
    return pin if pin and _code_agent_registry().has(pin) else None

def _code_agent_timeout_s() -> float:
    """Live per-coder wall-clock shipped to clients for their next local launch."""
    cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
    return float(((cfg.get("code_agents") or {}).get("timeout_s") or 1800.0))

# #455: process-lifetime circuit-breaker — agents whose last run reported budget/quota EXHAUSTED
# (classified `agent-unavailable`). Server-side, in-memory (resets on restart). Tripped agents are
# excluded from execution/failover so we never burn a zero-budget agent on retry (turns Kimi's
# infinite-retry into a clean failover). reason is for the /coders view + logs.
_CODE_AGENT_BREAKER: Dict[str, str] = {}

def _breaker_trip(agent: str, reason: str = "") -> None:
    a = (agent or "").upper()
    if a:
        _CODE_AGENT_BREAKER[a] = reason or "agent-unavailable"

def _breaker_reset(agent: str) -> None:
    _CODE_AGENT_BREAKER.pop((agent or "").upper(), None)

def _breaker_tripped(agent: str) -> bool:
    return (agent or "").upper() in _CODE_AGENT_BREAKER

def _breaker_snapshot() -> Dict[str, str]:
    return dict(_CODE_AGENT_BREAKER)

# epic #602 SUB-9: a SEPARATE per-task output-QUALITY breaker (an ``ack.quality.QualityBreaker`` or None) —
# distinct from the per-peer availability breaker above (folding quality in would corrupt failover). It is
# always built from the ``quality`` tuning block; a latched trip is an always-on, fail-closed pre-write staging
# hold until a passing-quality submission or explicit operator reset clears it.
_QUALITY_BREAKER = None
_QUALITY_LOCK = threading.Lock()

# #456 (FORK-D): task_class is derived DETERMINISTICALLY from task_json.type — never from model output.
# #1287 (operator role model, 2026-07-08 — REVERSES the 2026-06-25 "staged pick is authoritative" rule):
# every task TYPE maps to a cost TIER, and the coder is chosen DETERMINISTICALLY as the cheapest CAPABLE
# agent for that tier (`_route_code_agent`), so the model's `to:` no longer decides which coder runs. Tiers:
#   complex  → the strongest/priciest coder (OPUS): security/architecture/optimization.
#   standard → the mid coders (SONNET/CODEX): ordinary implementation/features/refactors (the DEFAULT).
#   routine  → the cheapest coders (KIMI/GROK): mechanical scaffolding/docs/cleanup/build.
#   analysis → the broad/cheap set (SONNET/KIMI): verification/research.
# The class→coders matrix (which coder serves each tier, cheapest-capable first by cost_per_1k) lives in
# code_agents.classes (public default = OPUS/SONNET; conf/ adds the private CODEX/KIMI/GROK). The operator
# pin still overrides at launch (`_effective_code_agent`).
_TASK_CLASS_BY_TYPE = {
    "security": "complex", "security-audit": "complex",
    "architecture": "complex", "optimization": "complex",
    "documentation": "routine", "concept": "routine", "cleanup": "routine", "smoke-test": "routine",
    "verification": "analysis", "research": "analysis",
}

def _task_class(task: Dict[str, Any]) -> str:
    t = str((task or {}).get("type") or "").strip().lower()
    return _TASK_CLASS_BY_TYPE.get(t, "standard")

# #500/#1287: auto-tier the handover reasoning effort by the derived task_class (cost tier) — complex gets
# xhigh, standard/analysis get high, routine (mechanical) gets medium. An UNMAPPED class returns None ⇒
# fail-open: the effort chain is left unchanged (a future class cannot silently force an effort until mapped).
_EFFORT_BY_CLASS = {"complex": "xhigh", "standard": "high", "routine": "medium", "analysis": "high"}

def _effort_for_class(task_class: Optional[str]) -> Optional[str]:
    return _EFFORT_BY_CLASS.get(task_class or "")

def _resolve_handover_effort(explicit: Optional[str], task_class: Optional[str],
                             spec_effort: Optional[str]) -> str:
    """#500: the handover reasoning-effort precedence. An explicit handover ``effort:`` (the operator's /
    method's per-handover override) ALWAYS wins; else auto-tier by ``task_class`` (``_effort_for_class``);
    else the agent's configured ``spec.effort``; else the global ``AUTOPILOT_DEFAULT_EFFORT``. Deterministic
    and fail-open — an unmapped/None class falls straight through to the pre-#500 spec/default chain."""
    return explicit or _effort_for_class(task_class) or spec_effort or AUTOPILOT_DEFAULT_EFFORT

def _class_capable_agents(task_class: Optional[str]) -> Optional[List[str]]:
    """The configured capable agents (UPPER) for a task_class. Returns None when the class is
    unknown/unmapped ⇒ NO restriction (fail-open, byte-identical to #455); returns the (possibly EMPTY)
    list when the class IS mapped. An explicit empty list is an operator statement "no agent may serve
    this class" → it must scope the failover to nothing (fail-CLOSED: keep the chosen agent), NOT
    collapse to fail-open — so a `classes.security: []` never leaks a security task to a cheaper peer.
    Reads code_agents.classes from the config."""
    if not task_class:
        return None
    classes = ((_EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults())
               .get("code_agents") or {}).get("classes") or {}
    caps = classes.get(task_class)
    if caps is None:                       # class not mapped → no restriction (fail-open)
        return None
    return [str(a).upper() for a in caps]  # mapped (incl. empty []) → restrict (empty ⇒ fail-closed)

def _cheapest_available_peer(exclude_tripped: bool = True, task_class: Optional[str] = None) -> Optional[str]:
    """#455/#456: the cheapest agent that is not breaker-tripped AND (when a task_class is given and
    mapped) CAPABLE of that task_class (cost = cost_per_1k_in+out, USD soft ordering per C0R-2; ties →
    declaration order). Unmapped/unknown class ⇒ the whole pool (fail-open). None ⇒ nothing available."""
    reg = _code_agent_registry()
    capable = _class_capable_agents(task_class)          # None ⇒ no class restriction
    cands = []
    for i, aid in enumerate(reg.names()):
        if exclude_tripped and _breaker_tripped(aid):
            continue
        if capable is not None and aid not in capable:   # #456: not capable of this task_class
            continue
        spec = reg.resolve(aid)
        if spec is not None:
            cost = (spec.cost_per_1k_in or 0.0) + (spec.cost_per_1k_out or 0.0)
            cands.append((cost, i, aid))
    cands.sort()
    return cands[0][2] if cands else None

def _effective_code_agent(staged: str, task_class: Optional[str] = None) -> str:
    """#454/#455/#456: the agent that ACTUALLY runs a handover. The operator pin overrides the
    orchestrator's task-chosen (staged) agent (the runtime switch, #454); no/invalid pin ⇒ the staged
    agent. #455: if that chosen agent is breaker-tripped (budget exhausted), FAIL OVER to the cheapest
    non-tripped peer — #456 restricts the failover to agents CAPABLE of the task_class (so a security
    handover never falls back to a non-security agent); if none qualify, keep the chosen one
    (fail-closed). The staged agent stays authoritative — task_class only scopes the failover."""
    chosen = _code_agent_pin() or (staged or "")
    if not chosen or not _breaker_tripped(chosen):
        return chosen
    return _cheapest_available_peer(exclude_tripped=True, task_class=task_class) or chosen

def _route_code_agent(task: Dict[str, Any]) -> Optional[str]:
    """#1287: the DETERMINISTIC primary coder for a task — the cheapest CAPABLE agent for its cost tier
    (`_task_class`), so a routine scaffold/doc lands on a cheap coder and OPUS is reserved for the complex
    tier, regardless of the orchestrator model's pick. Returns None when nothing is routable (unmapped class
    or none available) ⇒ the caller keeps the model's agent as a fail-soft fallback. The operator pin still
    overrides at launch (`_effective_code_agent`)."""
    return _cheapest_available_peer(exclude_tripped=True, task_class=_task_class(task))

def _tools_with_agent_enum(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """#449: replace the static ``agent`` enum (OPUS/SONNET) in the handover tool schemas with the
    LIVE registry names, so a config-added agent (CODEX/KIMI) is offerable + the model can't emit an
    unconfigured one. Only the agent-bearing tools are deep-copied (cheap); the rest pass through."""
    names = _agent_names()
    out: List[Dict[str, Any]] = []
    for t in tools:
        props = (((t or {}).get("function") or {}).get("parameters") or {}).get("properties") or {}
        agent = props.get("agent")
        if isinstance(agent, dict) and "enum" in agent:
            t = copy.deepcopy(t)
            t["function"]["parameters"]["properties"]["agent"]["enum"] = names
        out.append(t)
    return out

def _all_tool_names(include_plugins: bool = True) -> frozenset:
    """Every tool name the model might know — independent of whether it is offered this turn — used
    to catch a tool invoked as a shell command via execute_command (S12). Defensive over globals so a
    conditionally-absent tool constant never raises."""
    g = globals()
    names = set()
    for ln in ("TOOLS", "ONBOARDING_TOOLS"):
        for t in (g.get(ln) or []):
            n = ((t or {}).get("function") or {}).get("name")
            if n:
                names.add(n)
    for sn in ("CONSTRAINT_TOOL", "MEMORY_TOOL", "DEEP_MEMORY_TOOL", "MEMORY_WRITE_TOOL", "PARALLEL_TOOL", "WEBSEARCH_TOOL",
               "CREATE_ISSUE_TOOL", "VIEW_ISSUE_TOOL", "CREATE_PR_TOOL", "COMMENT_ISSUE_TOOL", "PR_STATUS_TOOL",
               "REVIEW_TOOL", "FETCH_URL_TOOL", "USE_SKILL_TOOL", "USE_PROMPT_TOOL"):
        t = g.get(sn)
        n = ((t or {}).get("function") or {}).get("name") if isinstance(t, dict) else None
        if n:
            names.add(n)
    if include_plugins:   # ROUTE-4 (#503): include_plugins=False → the BUILT-IN tool names only (collision check)
        names.update((g.get("_PLUGIN_TOOLS") or {}).keys())
    return frozenset(names)


def _forge_transport():
    """Select the active forge adapter from the vendor-neutral `forge.adapter` config (#1213/epic #1212):
    `cli` (default — the ambient `gh` CLI, byte-identical to before) | `native` (a stdlib-`urllib` GitHub
    client that works with NO `gh` on the box — the Spark `server` topology — keyed by a name-indirected
    token + `forge.repo`) | `mock`. Reads the LIVE globals so a reconfigure/monkeypatch takes effect per
    call. Never raises."""
    try:
        from forge_adapters import build_forge_adapter
        token = os.environ.get(FORGE_TOKEN_ENV, "") if FORGE_ADAPTER == "native" else ""
        return build_forge_adapter(adapter=FORGE_ADAPTER, repo=FORGE_REPO, token=token)
    except Exception:  # noqa: BLE001 — a builder hiccup must never break the turn
        from forge_adapters import UnavailableForgeAdapter
        return UnavailableForgeAdapter("error", "forge adapter unavailable")


def _forge_available() -> bool:
    """Central capability check for the forge tools (create_issue/view_issue, #1073/#1208) — offered when a
    forge TRANSPORT is usable: the `gh` CLI on PATH for the default `cli` adapter, OR a native token+repo for
    the `native` adapter (#1213/#1212 — so the tools are general IN ironclad, not gh-on-the-box). Gated by
    forge.enabled; blocked under the sealed profile (no autonomous outbound writes, like web_search). Mirrors
    _web_search_available() (uniformly capability-detected). Never raises."""
    try:
        return FORGE_ENABLED and not _is_sealed_profile() and _forge_transport().available()
    except Exception:  # noqa: BLE001 — a flaky capability probe must never break the turn
        return False


def _review_available() -> bool:
    """#1221: capability check for the `review` tool — offered when AT LEAST ONE code-agent binary
    resolves on this box (the reviewer runs via ``default_cli_runner``). Mirrors ``_forge_available()``
    (uniformly capability-detected; lights up on the desktop/local topology where coder CLIs live).
    Never raises."""
    try:
        from providers import probe_code_agents
        return any(probe_code_agents(_code_agent_registry()).values())
    except Exception:  # noqa: BLE001 — a flaky probe must never break the turn
        return False


try:
    _MODEL_PROBE_TIMEOUT_S = float(os.environ.get("GX10_MODEL_PROBE_TIMEOUT_S") or 8.0)
except Exception:  # noqa: BLE001
    _MODEL_PROBE_TIMEOUT_S = 8.0
_MODEL_CHECK_CACHE: Dict[str, "ModelCheck"] = {}


def _probe_agent_model(spec, bin_path):
    """Run an opt-in code-agent models probe and return a pure providers.ModelCheck, fail-soft.

    This diagnostic probe is intentionally outside the tooling-envelope launch scope: it spends no prompt,
    executes only an operator-declared models-list command against a bin that already survived registry
    filtering and boot resolution, and never performs a coder handover.
    """
    try:
        if spec is None or not getattr(spec, "models_probe", None) or not bin_path:
            return None
        from providers import validate_model
        argv = [bin_path] + shlex.split(spec.models_probe)
        cp = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=_MODEL_PROBE_TIMEOUT_S,
        )
        merged = (cp.stdout or "") + ("\n" if cp.stdout and cp.stderr else "") + (cp.stderr or "")
        if not merged.strip():
            return None
        return validate_model(spec, merged)
    except Exception:  # noqa: BLE001
        return None


def _validate_code_agent_models() -> list:
    """Populate the opt-in model-check cache and return cached mismatches. Never raises into boot."""
    out = []
    try:
        from providers import resolve_agent_bin
        reg = _code_agent_registry()
        for aid in reg.names():
            spec = reg.resolve(aid)
            if spec is None or not getattr(spec, "models_probe", None):
                continue
            check = _probe_agent_model(spec, resolve_agent_bin(spec))
            if check is None:
                continue
            _MODEL_CHECK_CACHE[aid.upper()] = check
            if check.ok is False:
                out.append(check)
    except Exception:  # noqa: BLE001
        return []
    return out


def _cached_model_mismatch(agent) -> Optional["ModelCheck"]:
    """Return a cached opt-in mismatch for an agent, or None when unprobed/ok. Never raises."""
    try:
        mm = _MODEL_CHECK_CACHE.get((agent or "").upper())
        return mm if mm is not None and mm.ok is False else None
    except Exception:  # noqa: BLE001
        return None


def _pick_reviewer(requested: Optional[str] = None) -> Optional[str]:
    """#1221: resolve the reviewer agent_id. Explicit ``agent`` arg wins (fail-closed if unknown /
    unrunnable) — a deliberate request is honored even if it equals the producer. Config-default /
    no-arg path: prefer config ``review.agent`` when runnable **and** not a self-review against a
    runnable peer; else a SOFT distinct-peer pick that excludes the producer pin (#457 anti-affinity —
    waive and keep the only capable agent when exclusion would empty the set). Returns None when
    nothing is runnable."""
    from providers import resolve_agent_bin
    reg = _code_agent_registry()

    def runnable(aid: str) -> bool:
        if not aid or not reg.has(aid):
            return False
        return bool(resolve_agent_bin(reg.resolve(aid)))

    req = (requested or "").strip().upper()
    if req:
        return req if runnable(req) else None

    # SOFT distinct-reviewer anti-affinity (#457): never self-review when a peer exists.
    # Applies to the config-default / no-arg path only (explicit arg is already honored above).
    producer = _code_agent_pin()
    candidates = [a for a in reg.names() if runnable(a)]
    if not candidates:
        return None
    peers = [a for a in candidates if a != producer] if producer else list(candidates)

    cfg_agent = (REVIEW_AGENT or "").strip().upper()
    if cfg_agent and runnable(cfg_agent):
        # Config default is fine unless it would self-review while a peer is runnable.
        if not producer or cfg_agent != producer or not peers:
            return cfg_agent
        return peers[0]

    if peers:
        return peers[0]
    return candidates[0]


def _normalize_review_paths(paths_arg) -> List[str]:
    """Accept an array of strings OR a comma-separated string (models sometimes emit either)."""
    if paths_arg is None or paths_arg == "":
        return []
    if isinstance(paths_arg, (list, tuple)):
        return [str(p).strip() for p in paths_arg if str(p).strip()]
    return [p.strip() for p in str(paths_arg).split(",") if p.strip()]


def _assemble_review_material(paths_arg) -> Tuple[str, str]:
    """Assemble the review payload. Default = working ``git diff HEAD``; with ``paths`` = named files
    (docs/decisions/plans/artifacts). Returns ``(mode, material)`` where mode is ``diff`` or ``paths``.
    Material is char-capped. Never raises."""
    paths = _normalize_review_paths(paths_arg)
    if paths:
        parts: List[str] = []
        usable = 0
        for rel in paths:
            p = _resolve_exec_path(rel)
            if not p.exists():
                parts.append(f"### {rel}\nERROR: not found\n")
                continue
            if p.is_dir():
                parts.append(f"### {rel}/\n(directory — pass specific files, not a directory)\n")
                continue
            try:
                text, size = _read_text_capped(p)
            except Exception as e:  # noqa: BLE001
                parts.append(f"### {rel}\nERROR: read failed: {e!r}\n")
                continue
            if text is None:
                parts.append(f"### {rel}\nERROR: file too large — {size} bytes, cap {_MAX_FILE_BYTES} bytes\n")
                continue
            usable += 1
            parts.append(f"### {rel}\n```\n{text}\n```\n")
        if usable == 0:
            return "paths", "ERROR: no readable files among the given paths."
        material = "\n".join(parts)
        mode = "paths"
    else:
        cwd = str(_exec_cwd() or Path.cwd())
        try:
            proc = subprocess.run(
                ["git", "-C", cwd, "diff", "HEAD"],
                capture_output=True, text=True, timeout=30,
            )
            material = proc.stdout or ""
            if proc.returncode != 0 and not material.strip():
                err = (proc.stderr or "").strip()[:500]
                return "diff", f"ERROR: git diff failed: {err or f'exit {proc.returncode}'}"
            if not material.strip():
                material = "(empty working-tree diff — no uncommitted changes vs HEAD)"
            mode = "diff"
        except Exception as e:  # noqa: BLE001
            return "diff", f"ERROR: git diff failed: {e!r}"
    if len(material) > _REVIEW_MATERIAL_CAP:
        material = (material[:_REVIEW_MATERIAL_CAP]
                    + f"\n\n... [Ironclad: review material truncated at {_REVIEW_MATERIAL_CAP} chars] ...")
    return mode, material


def _review_prompt(focus: str, mode: str, material: str) -> str:
    """Build the independent-reviewer prompt (structured findings; author-blind)."""
    focus_line = (focus or "").strip() or "general correctness, risks, missing pieces, and contract fit"
    return (
        "You are an independent cross-model reviewer. You did NOT author the material below.\n"
        "Review it critically and return STRUCTURED findings only — no code rewrites.\n\n"
        f"Focus: {focus_line}\n"
        f"Material mode: {mode}\n\n"
        "Output format (use exactly these headings):\n"
        "## Summary\n(1-3 sentences)\n"
        "## Findings\n- [severity: high|medium|low] <finding> — <evidence/location>\n"
        "## Recommendations\n- <actionable next step>\n"
        "## Verdict\nAPPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION\n\n"
        f"--- BEGIN MATERIAL ---\n{material}\n--- END MATERIAL ---\n"
    )


def _forge_labels() -> Optional[set]:
    """create_issue label vocabulary (#1130 follow-up): the repo's ACTUAL labels, for validate→reask on the
    `labels` arg — the model must use existing labels, not invent them. Returns None on any error (fail-soft:
    validation is then skipped, never blocking a create over a label-lookup hiccup). Routed through the active
    forge adapter (#1213), so it works on both the `cli` and `native` paths."""
    try:
        return _forge_transport().list_labels()
    except Exception:  # noqa: BLE001
        return None


def _effective_tools() -> List[Dict[str, Any]]:
    """Tool list depending on the mode — onboarding tools only when active."""
    # Offer the tool only when memory is CONFIGURED (not just the module present) —
    # otherwise the tool would be offered even though every call would return "unavailable".
    mem = [MEMORY_TOOL, DEEP_MEMORY_TOOL, MEMORY_WRITE_TOOL] if _MEMORY is not None else []
    par = [PARALLEL_TOOL] if _WORKERS is not None else []
    # #459 / epic #505: offer web_search only when a usable search adapter is configured (else every
    # call would return "unavailable"). Adapter-aware (cli / brave / mock) — not dispatcher-only.
    web = [WEBSEARCH_TOOL] if _web_search_available() else []
    iss = ([CREATE_ISSUE_TOOL, VIEW_ISSUE_TOOL, CREATE_PR_TOOL, COMMENT_ISSUE_TOOL, PR_STATUS_TOOL]
           if _forge_available() else [])   # #1073/#1208/#1215/#1217/#1219: capability-detected forge surface
    # #1221: cross-model second-opinion review — route through `_tools_with_agent_enum` so the
    # model-facing `agent` enum is LIVE from the registry (CODEX/KIMI/…), not the static OPUS/SONNET.
    rev = _tools_with_agent_enum([REVIEW_TOOL]) if _review_available() else []
    fet = [FETCH_URL_TOOL] if _web_search_trust_ok() else []   # #1074: outbound fetch, blocked under sealed
    plug = [t["schema"] for t in _PLUGIN_TOOLS.values()]
    skl = [USE_SKILL_TOOL] if _PLAYBOOKS else []
    prm = [USE_PROMPT_TOOL] if _PROMPTS else []
    con = [CONSTRAINT_TOOL] if FRAMING_NOTES_ENABLED else []
    return (_tools_with_agent_enum(TOOLS) + con + mem + par + web + iss + rev + fet + plug + skl + prm
            + (ONBOARDING_TOOLS if ONBOARDING_MODE else []))

# ─── Macro tool: deterministic pipeline (HV-A) ─────────────
_TASK_ID_RE = re.compile(rf"^{re.escape(TASK_PREFIX)}-[A-Za-z0-9_]+$")
_IDLE_ACTIVE = "# Workflow — idle\n\nNo active handover.\n"

def _atomic_write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    # Windows: os.replace fails with [WinError 5] when the target is held open by
    # another process (e.g. Obsidian on active.md). The lock is usually
    # transient → retry briefly; if it persists, overwrite directly
    # (non-atomic) instead of letting the whole pipeline fail.
    for attempt in range(8):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if attempt < 7:
                time.sleep(0.25)
                continue
            p.write_text(content, encoding="utf-8", newline="\n")   # fallback: direct (non-atomic) write
            try:
                tmp.unlink()
            except OSError:
                pass
            return

def _normalize_handover_id(md: str, tid: str) -> str:
    """Sets ALL `task_id:` lines in the handover (frontmatter + feedback template)
    to the ID assigned by the store. count=0 = replace all occurrences, so
    the feedback template in the body doesn't keep KGC-XXX (reconciler fallback)."""
    return re.sub(r"(?m)^(task_id:\s*).*$", rf"\g<1>{tid}", md, count=0)


def _normalize_handover_recipient(md: str, agent: str) -> str:
    """#1311: the frontmatter ``to:`` is the authoritative recipient (the resolved agent, also the
    handover filename), but the model authors the free-form body Meta block and can name a DIFFERENT
    agent there (e.g. ``Recipient: CODEX`` on a SONNET handover) — confusing for the coder that reads it.
    Rewrite a body ``Recipient:`` line to the resolved agent, but ONLY when it names another configured
    code-agent — NEVER a legitimate payload value (an email/header fixture the task is about, a person),
    so task content the coder must produce is left untouched. Matches plain / bulleted / bold forms with
    the colon inside (``**Recipient:**``) or outside (``**Recipient**:``) the bold. No-op with no agent."""
    if not agent:
        return md
    known = {a.upper() for a in _agent_names()} | {agent.upper()}   # configured code-agent tokens
    pat = re.compile(r"(?i)^(?P<pre>\s*(?:[-*]\s+)?\*{0,2}\s*Recipient\s*\*{0,2}\s*:\s*\*{0,2}\s*)(?P<val>.*)$")
    # Scope to the FIRST Recipient line OUTSIDE any fenced code block (the Meta recipient) — never rewrite a
    # Recipient in task payload / a fenced example (a later line, or code), and only when it names another
    # configured agent. This is the precise `Recipient: CODEX`-on-a-SONNET-handover fix, nothing else.
    out: List[str] = []
    in_fence = done = False
    for line in md.splitlines(keepends=True):
        s = line.lstrip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not done and not in_fence:
            content = line.rstrip("\r\n")
            m = pat.match(content)
            if m:
                done = True   # the FIRST non-fenced Recipient line IS the Meta one — scan no further, so a
                              # later `Recipient: <agent>` in task payload is never rewritten, whether or
                              # not this line needed a fix.
                cur = m.group("val").strip().strip("*").strip().upper()
                if cur in known and cur != agent.upper():
                    out.append(f"{m.group('pre')}{agent}{line[len(content):]}")   # keep the line ending
                    continue
        out.append(line)
    return "".join(out)


def _inject_code_root_note(md: str) -> str:
    """#1328: when model-driven execution is rooted below the project, tell the coder that its cwd is
    already that code root. The orchestrator plans from the project root and can otherwise repeat the
    configured subdir in the handover, producing ``src/src``-style trees. Insert the note after leading
    YAML frontmatter when present, keep re-hands idempotent, and fail soft like the other enrichments."""
    if not CODE_SUBDIR:
        return md
    try:
        marker = "<!-- ironclad-code-root-note -->"
        if marker in md:
            return md
        nl = "\r\n" if "\r\n" in md else "\n"
        note = nl.join((
            marker,
            "> [!IMPORTANT]",
            f"> **Code root:** Your working directory is already the project's code root (`{CODE_SUBDIR}`).",
            "> Create the package and `pyproject.toml` directly in this working directory. Do not add another",
            f"> `{CODE_SUBDIR}/` prefix; that would double-nest the tree.",
        ))
        insert_at = 0
        lines = md.splitlines(keepends=True)
        if lines and lines[0].strip() == "---":
            offset = len(lines[0])
            for line in lines[1:]:
                offset += len(line)
                if line.strip() == "---":
                    insert_at = offset
                    break
        if not insert_at:
            return note + nl + nl + md
        before, after = md[:insert_at], md[insert_at:]
        if not before.endswith(("\n", "\r")):
            before += nl
        return before + nl + note + nl + nl + after.lstrip("\r\n")
    except Exception:   # noqa: BLE001 — a staging hint must never break handover enrichment
        return md


# ── #602 SUB-2 (#690): the Loop-Intelligence Hook-Bus publish seam ───────────────────────────────
# The reflection consumers (#602 C2: Verifier / Quality / Process-SC / Lessons) subscribe via
# ``ack.hooks``; the engine PUBLISHES the boundary events through this seam. Lazy-imported + cached
# (never import ack at gx10 top-level — S6b lesson); fail-soft and an O(1) no-op when no hook is
# registered, so with NO subscriber (the default) the loop is byte-identical. Observer-only: a hook's
# return value is ignored, and the engine's cancel flag is threaded in so a cancelled turn stops dispatch.
_HOOKS_MOD = None


def _emit_hook(event: str, ctx: "Any" = None) -> None:
    global _HOOKS_MOD
    try:
        if _HOOKS_MOD is None:
            from ack import hooks as _h   # lazy: never import ack at gx10 top-level (S6b lesson)
            _HOOKS_MOD = _h
        _HOOKS_MOD.dispatch(event, ctx, should_cancel=_CANCEL_EVENT.is_set)
    except Exception:   # noqa: BLE001 — the hook bus must never break a turn
        pass


def _advance_pipeline(task_id: str, agent: str, next_task_id: Optional[str] = None) -> str:
    """Serialized wrapper (#601 S12b): run the deterministic advance under the per-project+track vault
    lock so a concurrent vault mutation (a parallel ``initiative_new`` / handover) can't interleave —
    e.g. the active slug being re-pointed mid-advance. Reentrant: the impl's inner reconcile does not
    re-acquire."""
    _emit_hook("pre_advance", {"task_id": task_id, "agent": (agent or "").upper(),
                               "next_task_id": next_task_id})
    with _vault_lock():
        result = _advance_pipeline_impl(task_id, agent, next_task_id)
    # post_feedback = task completion boundary, published OUTSIDE the vault lock. BOTH completion-writes are
    # re-homed here as bus subscribers — Process-SC (#803) + Lessons (#804) — so the reflection consumers share
    # one consistent wiring path. `result` lets a consumer gate on a FRESH completion ("OK: pipeline advanced …")
    # vs an already-done re-advance / error.
    _emit_hook("post_feedback", {"task_id": task_id, "agent": (agent or "").upper(), "result": result})
    return result


def _feedback_status(text: str) -> str:
    """Dev-loop stabilization: extract the coder's completion STATUS token, tolerant to WHERE it was placed.
    The engine's own coder prompt dictates a leading ``status:`` line, but a coder may instead put it inside
    the ``---`` frontmatter — a bare leading ``status: done`` (before the fence) was invisible to
    ``_parse_frontmatter`` (which needs ``lines[0]=='---'``), so a COMPLETED task stalled forever. Try the
    frontmatter first; else a BOUNDED head-scan of the first ~20 lines (so a ``status:`` buried deep in prose
    is NOT matched — refuse-on-uncertain stays). Returns the FIRST value token, lowercased, or ``""``."""
    fm = _parse_frontmatter(text or "")
    raw = next((v for k, v in fm.items() if k.strip().lower() == "status"), "")
    if not raw:
        head = "\n".join((text or "").splitlines()[:20])
        m = re.search(r"(?im)^\s*status:\s*(\S+)", head)
        raw = m.group(1) if m else ""
    toks = raw.strip().split()
    if not toks:
        return ""
    token = toks[0]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        token = token[1:-1]
    return token.rstrip(".,;:!?").lower()


def _stamp_done_if_clean(text: str, exit_code) -> str:
    """Stamp status only for non-empty, status-less feedback from a confirmed clean exit."""
    content = text or ""
    # A confirmed clean exit is the integer 0 ONLY — reject bool `False` (`False == 0` in Python)
    # and any non-int (e.g. a JSON string/null) so a malformed exit_code can never stamp `done`.
    clean_exit = isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code == 0
    if clean_exit and content.strip() and not _feedback_status(content):
        return "status: done\n" + content
    return content


def _advance_gate(fb_text: str) -> "Optional[str]":
    """Return ``None`` only for explicit normalized ``status: done`` completion evidence."""
    # #1463: strict done is forced by the fail-closed transition invariant. It deliberately reconciles the
    # old presence-wins stall fix with "no signal != done"; this is not an operator-selectable policy.
    status = _feedback_status(fb_text or "")
    if status == "done":
        return None
    shown = repr(status) if status else "missing"
    return (f"ERROR: not advancing — the feedback status is {shown}, not done. Resolve it first; the "
            f"task stays in_progress (no blind advance).")


def _advance_pipeline_impl(task_id: str, agent: str, next_task_id: Optional[str] = None) -> str:
    """Advances the 'done' pipeline for ONE task deterministically.
    Status transitions go through the TaskStore (directory = truth),
    active.md is projected. Fail-closed: no completion without a feedback
    file. Touches neither code/ nor the audit chain."""
    if not task_id or not _TASK_ID_RE.match(task_id):
        return f"ERROR: invalid task_id: {task_id!r} (expected e.g. KGC-315)"
    agent = (agent or "").upper()
    if not _code_agent_registry().has(agent):   # #449: config-driven membership, fail-closed (no KIMI-norm)
        return f"ERROR: unknown agent {agent!r} (configured: {', '.join(_agent_names()) or 'none'})"
    if next_task_id and not _TASK_ID_RE.match(next_task_id):
        return f"ERROR: invalid next_task_id: {next_task_id!r}"

    if artifact_root_soft() is None:
        return f"ERROR: {_msg('init.no_active')}"     # B3: fail-closed — artefacts route to the active initiative
    _blk = _internal_target_blocks_normal()           # #979: normal pipeline is off on an internal target
    if _blk:
        return f"ERROR: {_blk}"

    store = _store()
    log: List[str] = []

    # Idempotency gate: task already done → no re-advance needed
    existing = store.get(task_id)
    if existing and existing.get("status") == "done":
        return (f"OK: task {task_id} is already done — no re-advance needed. "
                f"feedback is in {(archive_feedback_dir() / f'{task_id}_{agent}-feedback.md').as_posix()}")

    # 0. Fail-closed gate: feedback MUST exist. Dev-loop stabilization (Fix 4): key the match on the TASK ID
    #    and derive the TRUE agent from the matched FILENAME (via _FB_RE) — never reconstruct the name from the
    #    caller-supplied agent, which routing (#1287/#1292) can skew into a permanent miss. Prefer the exact
    #    caller agent (newest), else the newest matching file; keep the configured-agent fail-closed filter.
    def _fb_agent(p: "Path") -> str:
        m = _FB_RE.search(p.name)
        return m.group(1).upper() if m else ""
    _cands = sorted(
        [p for p in (list(feedback_dir().glob(f"{task_id}_*-feedback.md"))
                     + list(archive_feedback_dir().glob(f"{task_id}_*-feedback.md")))
         if _code_agent_registry().has(_fb_agent(p))],
        key=lambda p: p.stat().st_mtime)
    if not _cands:
        return (f"ERROR: feedback missing: no {task_id}_*-feedback.md in "
                f"{feedback_dir().as_posix()} nor its archive — the task is NOT complete. Pipeline not advanced.")
    _exact = [p for p in _cands if _fb_agent(p) == agent]
    fb = _exact[-1] if _exact else _cands[-1]      # exact caller agent (newest), else newest matching feedback
    agent = _fb_agent(fb)                          # the TRUE runner, from the filename (authoritative for the rest)
    log.append(f"feedback found: {fb} (agent {agent})")
    try:
        _fbtext = fb.read_text(encoding="utf-8")
    except Exception as exc:   # noqa: BLE001 — unreadable evidence cannot authorize completion
        _fbtext = ""
        _gate_err = f"ERROR: not advancing — feedback is unreadable ({exc}); the task stays in_progress."
    else:
        _gate_err = (_advance_gate(_fbtext) if _fbtext.strip() else
                     "ERROR: not advancing — feedback is empty; the task stays in_progress.")
    if _gate_err:
        # S7 (#1229): the refused task stays in_progress — mark it BLOCKED so the operator sees the stall on
        # the board/steering instead of a healthy-looking in_progress (transition() clears it on advance).
        _st = _feedback_status(_fbtext or "")   # dev-loop stab: same tolerant parse as the gate
        try:
            store.mark_blocked(task_id, reason=_gate_err.replace("ERROR: ", "")[:200],
                               kind=_st if _st in ("blocked", "clarification_needed") else "blocked")
        except Exception:   # noqa: BLE001 — a marking hiccup must not change the refusal outcome
            pass
        return _gate_err

    _egress_err, _egress_log = _egress_advance_check_log()
    log.extend(_egress_log)
    if _egress_err:
        return _egress_err

    try:
        # 1. archive the current active.md handover (before the switch)
        active  = active_md_path()
        archive = archive_handovers_dir() / f"{task_id}_{agent}.md"
        if active.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(active), str(archive))
            log.append(f"active.md archived → {archive}")
        else:
            log.append("active.md not present (skip archive)")

        # 2. move feedback into the archive AND remove the original — otherwise
        #    old feedbacks pile up in the inbox and match again on ID reuse
        #    (stale trigger). If fb already comes from the archive (fallback), no copy+delete.
        vfb = archive_feedback_dir() / fb.name
        vfb.parent.mkdir(parents=True, exist_ok=True)
        if fb.resolve() != vfb.resolve():
            shutil.copy2(str(fb), str(vfb))
            try:
                fb.unlink()
                log.append(f"feedback archived → {vfb} (original removed)")
            except OSError:
                log.append(f"feedback → {vfb}")
        else:
            log.append(f"feedback already archived: {vfb} (no copy needed)")

        # 3. status transition → done (via the store)
        try:
            store.transition(task_id, "done")
            log.append(f"task {task_id} → tasks/done (status=done)")
        except KeyError:
            log.append("task-json not found (skip)")

        # 3x. #1296: epic auto-complete — a tracker record has no coder feedback, so its completion
        # is DERIVED, not advanced: when the just-advanced task was the LAST open child of an epic,
        # the engine transitions the epic to done itself (deterministic, ledgered here). Fail-soft:
        # a missing/foreign parent id or a check hiccup never breaks the completed advance.
        _parent = str((existing or {}).get("parent") or "").strip()
        if _parent:
            try:
                _ptask = store.get(_parent)
                if _ptask is None:
                    log.append(f"parent {_parent} not found (skip epic auto-complete)")
                elif (str(_ptask.get("type", "")).lower() == "epic"
                        and _ptask.get("status") != "done"):
                    _sibs = [t for t in store.list() if str(t.get("parent") or "") == _parent]
                    if _sibs and all(t.get("status") == "done" for t in _sibs):
                        store.transition(_parent, "done")
                        log.append(f"epic {_parent} auto-completed (all {len(_sibs)} units done)")
                    else:
                        _left = sum(1 for t in _sibs if t.get("status") != "done")
                        log.append(f"epic {_parent}: {_left} unit(s) still open")
            except Exception as _e:  # noqa: BLE001
                log.append(f"epic auto-complete check failed (skip): {_e}")

        # 3a. Memory: store the task completion as an episode (fail-soft)
        if _MEMORY is not None and _MEMORY.is_available():
            try:
                fb_text = vfb.read_text(encoding="utf-8") if vfb.exists() else ""
                _MEMORY.store_task_completion(task_id, existing or {}, fb_text)
            except Exception:
                pass

        # 3b. ACE consumes the completion through its always-on post_feedback hook outside the vault lock.
        # Legacy lesson files remain migration input; process.hints_enabled controls only pre-turn reads.

        # 4. delete the handover in the inbox (.work/handovers)
        deleted = False
        _hod = handovers_dir()
        for cand in (_hod / f"{task_id}_{agent}.md",
                     _hod / f"{task_id}_{agent.capitalize()}.md"):
            if cand.exists():
                cand.unlink()
                log.append(f"handover deleted: {cand}")
                deleted = True
                break
        if not deleted:
            log.append("no handover in .work/handovers (skip)")

        # 5. activate the next task (store) — active.md follows from the projection
        if next_task_id:
            try:
                store.transition(next_task_id, "in_progress")
                log.append(f"next task {next_task_id} → in_progress")
            except KeyError:
                log.append(f"WARN: next task {next_task_id} not found")

        # 6. project active.md (newest non-done handover, or idle)
        store.project_active()
        log.append("active.md projected")

        # 7. Optional: terminate the associated autopilot session (task is done)
        if AUTOPILOT_TERMINATE_ON_ADVANCE:
            _terminate_autopilot(task_id)
            log.append("autopilot session terminated (if active)")

        # 8. regenerate the vault projections DETERMINISTICALLY — mechanically, NOT
        #    dependent on GX10's step-6 discipline (prevents a stale backlog →
        #    otherwise autoplan plans from outdated data → duplicate). Idempotent +
        #    fail-soft: a script error does NOT abort the already-completed advance.
        #    UTF-8 env so emoji output doesn't crash on cp1252 stdout.
        _env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # ROUTE-1 (#503): the post-advance regen hooks are a DEPLOYMENT detail (vessel-specific scripts,
        # absent from the boundary-clean export) — config-driven via paths.post_advance_hooks (default
        # empty ⇒ NO subprocess; each absent script is skipped). Without this, core unconditionally spawned
        # 3 hardcoded scripts/*.py that don't exist in the export (3 fail-soft WARN procs per advance).
        _hooks = ((_EFFECTIVE_CFG or {}).get("paths") or {}).get("post_advance_hooks") or []
        for _script in _hooks:
            if not os.path.isfile(_script):
                log.append(f"regen {_script}: skipped (absent)")
                continue
            try:
                _r = subprocess.run([sys.executable, _script],
                                    cwd=".", capture_output=True, text=True,
                                    timeout=60, env=_env)
                log.append(f"regen {_script}: {'ok' if _r.returncode == 0 else 'WARN rc=' + str(_r.returncode)}")
            except Exception as _e:  # noqa: BLE001
                log.append(f"regen {_script}: WARN {_e!r}")

    except Exception as e:
        return f"ERROR: pipeline step failed: {e}\nso far:\n" + "\n".join(f"  - {l}" for l in log)

    _reconcile_active_soft()   # C2: keep the active initiative's INDEX.md fresh (fail-soft, index only)
    result = f"OK: pipeline advanced for {task_id} ({agent})\n" + "\n".join(f"  - {l}" for l in log)
    # #1296 (guided mode): with the continuation OFF nothing follows automatically — so the advance
    # RESULT itself names the deterministically selected next unit and the exact step to proceed
    # (the same recommendation the steering state carries). Fail-soft, advisory only.
    if not AUTOPILOT_AUTOPLAN:
        try:
            _nxt, _elig, _n_open = _select_next_unit(store)
            if _nxt is not None:
                result += (f"\n👉 Next open unit: {_nxt['id']} ({str(_nxt.get('title') or '')!r}) — stage its "
                           f"handover via stage_handover (task_id='{_nxt['id']}', no task_json), or `/auto on` "
                           f"to drain all open units automatically.")
            elif _n_open > 0:
                result += (f"\n⚠ {_n_open} open unit(s) but NONE selectable (blocked / unsatisfied "
                           f"dependencies) — inspect /board.")
        except Exception:  # noqa: BLE001 — the recommendation must never break a completed advance
            pass
    return result


# ─── Path guard: detect invented codebase paths in the handover ───
# The orchestrator sometimes guesses non-existent "current codebase state"
# paths, which lures the code agent into rebuilding instead of extending
# (duplication risk). This check reports code-like paths that exist neither
# relative to the repo root nor under the optional, vessel-specific CODE_ROOT
# (paths.code_root; empty = off).
_HANDOVER_PATH_RE = re.compile(
    r"(?<![\w@.-])((?:code|core|routers|config|tests|scripts|services|docker|frontend)/[\w./-]+(?:\.\w{1,6}|/))"
)

def _handover_path_warnings(handover_md: str) -> List[str]:
    api_base = Path(CODE_ROOT) if CODE_ROOT else None
    seen, missing = set(), []
    for tok in _HANDOVER_PATH_RE.findall(handover_md or ""):
        key = tok.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        if Path(key).exists() or (api_base is not None and (api_base / key).exists()):
            continue
        missing.append(tok)
    return missing


# ─── Macro tool: publish a handover (OPT-2, store-backed) ──
def _approved_design_standard_snapshot(slug: "Optional[str]") -> "List[str]":
    """Read the authoritative approved design material before a staging mutation."""
    standard: "List[str]" = []
    design_typed = _design_typed(slug)
    if "language" in design_typed:
        standard.append(f"- language: {design_typed['language']}")
    policy = _design_build_policy(slug)
    if policy:
        standard.append("## Build policy\n\n" + policy)
    return standard


def _inject_approved_design_standard(handover_md: str, standard: "List[str]", log: "List[str]") -> str:
    """Strip stale injected context and add the preflighted authoritative design standard."""
    open_m, close_m = _CONSTRAINT_MARKERS
    enriched = re.sub(
        rf"{re.escape(open_m)}.*?{re.escape(close_m)}\n*",
        "",
        handover_md,
        flags=re.DOTALL,
    )
    if not standard:
        return enriched
    standard_body = "\n\n".join(standard)
    log.append("Approved design standard injected")
    return (
        f"{open_m}\n"
        f"## Approved design standard (authoritative — honour verbatim; do not override)\n"
        f"\n"
        f"{standard_body}\n"
        f"{close_m}\n"
        f"\n"
        f"{enriched}"
    )


def _enrich_handover(tid: str, handover_md: str, fields: Dict[str, Any], log: List[str],
                     agent: str = "",
                     constraint_snapshot: "Optional[tuple[str, Optional[str]]]" = None,
                     approved_design_standard: "Optional[List[str]]" = None,
                     approved_design_preinjected: bool = False) -> str:
    """Shared handover enrichment (#1296 parity — used by BOTH staging paths, create and re-hand):
    normalize the embedded task id, inject captured constraints (S2 #1339, single-snapshot, idempotent
    strip-then-add), append the token-budgeted Memory brief (#458 D1, fail-soft) and the advisory
    ACE/lesson context (#863/#877/#880). ``fields`` supplies title/type (the creation payload on the
    create path; the STORED task on the re-hand path). The approved-design snapshot is protected and is
    preflighted by both callers; later advisory enrichments remain fail-soft."""
    ho_md = _normalize_handover_id(handover_md, tid)
    ho_md = _normalize_handover_recipient(ho_md, agent)   # #1311: body Recipient agrees with frontmatter `to:`
    ho_md = _inject_code_root_note(ho_md)                 # #1328: coder cwd already IS the configured code root
    # S2 (#1415): every handover receives the approved design standard when one is present.
    if constraint_snapshot is not None and not approved_design_preinjected:
        ho_md = _inject_approved_design_standard(ho_md, approved_design_standard or [], log)
    # The richer token-budgeted Memory brief from past patterns (#458 D1, fail-soft):
    # body-keyed search + optional relational hits + the shared warm rolling summary.
    if _MEMORY is not None and _MEMORY.is_available():
        try:
            warm_summary = ""
            if _WARM is not None:
                try:
                    warm_summary = (_WARM.get_session(_active_warm_session(), "summary") or "").strip()
                except Exception:
                    warm_summary = ""
            mem_ctx = _MEMORY.brief(
                body=ho_md,
                task_type=fields.get("type", ""),
                title=fields.get("title", ""),
                warm_summary=warm_summary,
                budget_tokens=MEMORY_BRIEF_TOKENS,
                count_tokens=_count_tokens,
            )
            if mem_ctx:
                ho_md = ho_md.rstrip() + "\n\n---\n\n" + mem_ctx
                log.append("Memory context injected")
        except Exception:
            pass
    # Advisory loop-lessons for the active scope (ADR-0011 AD-10 / S14-4) — appended to the handover
    # the way the Memory brief above is, but INDEPENDENT of Mem0 (a lesson backend may be wired even
    # when Mem0 is not). With NO provider registered ``lessons.brief`` returns "" → ``ho_md`` is
    # unchanged and no I/O happens, so this is byte-identical to the pre-seam engine.
    try:
        from ack import lessons as _lessons   # lazy: never import ack at gx10 top-level (S6b lesson)
        ns = _active_mem_ns()
        # ACE (#863): the always-on PlaybookStore exposes a query-aware relevant-bullet read
        # (`context_for`, keyed by the task title + handover body) — the 32k-safe Generator read that
        # injects only the most relevant subset of a large playbook (#366). Any other provider (or a
        # foreign extension) keeps the string-only `brief`. Duck-typed so the seam stays generic.
        prov = _lessons.get_provider()
        if hasattr(prov, "context_for"):
            q = (fields.get("title", "") + "\n" + ho_md).strip()
            lesson_ctx = prov.context_for([ns], query=q)
            # M4-0 (#877): remember WHICH bullets were injected into this task's handover so the
            # post_feedback consumer can rate them helpful/harmful (E-004/H-002). Advisory + fail-soft.
            _ids = _ace_bullet_ids(lesson_ctx)
            _ace_record_injected(tid, _ids)
            # M4-3 (#880): also DURABLY record the injected ids keyed by the task id + any issue# the
            # handover references (the standard `Closes #N` linkage), so the per-UNIT dev-process
            # ledger scan (M4-2) can populate Trajectory.used_bullet_ids (E-004 for the dev-loop unit).
            _ace_persist_injected(_ace_unit_keys(tid, fields, ho_md), _ids)
        else:
            lesson_ctx = _lessons.brief([ns])
        if lesson_ctx:
            ho_md = ho_md.rstrip() + "\n\n---\n\n## Lessons\n\n" + lesson_ctx
            log.append("Lesson context injected")
    except Exception:   # noqa: BLE001 — advisory: a lesson read must never break a turn
        pass
    return ho_md


def _ack_validate(fields: Dict[str, Any], *, stored: bool = False) -> Optional[str]:
    """Validate a staged task against the mandatory ACK contract.

    Returns the exact Pydantic validation error for a schema violation. An unavailable validator is an
    internal refusal rather than a bypass. Lodestar remains an optional stricter schema selector.
    """
    try:
        from ack.case_spec import TaskSpec
        spec_cls = TaskSpec
        if LODESTAR_ENABLED:
            from ack.lodestar.spec import CapabilityTaskSpec
            spec_cls = CapabilityTaskSpec
        from pydantic import ValidationError
    except Exception:
        return "ACK validator unavailable (internal) — refuse (fail-closed). Nothing created."
    try:
        runtime_fields = {
            "id", "status", "created_at", "started_at", "completed_at", "updated_at", "assigned_to",
            *_BLOCKED_ANNOTATION_KEYS,
        }
        payload = ({key: value for key, value in fields.items() if key not in runtime_fields}
                   if stored else fields)
        spec_cls.model_validate(payload)
        return None
    except ValidationError as e:
        return str(e)


def _required_verifier_gate(fields: Dict[str, Any]) -> Optional[str]:
    """Run the always-on deterministic handover rules and refuse any failed required rule."""
    try:
        from ack.verify import verify_rules
    except Exception:
        return "ERROR: required verifier unavailable (internal) — refuse (fail-closed). Nothing created."
    rules = [
        ("description_substantive", lambda f: len(str((f or {}).get("description", "")).strip()) >= 40),
        ("title_specific", lambda f: len(str((f or {}).get("title", "")).split()) >= 3),
    ]
    verdict = verify_rules(fields, rules, verifier="required_rules")
    _set_last_verdict(verdict)
    if verdict.passed:
        return None
    failed = verdict.reason.removeprefix("failed: ")
    return (f"ERROR: required handover verifier rule(s) failed: {failed} — fix the task fields and retry. "
            "Nothing created.")


def _record_advisory_grounding(fields: Dict[str, Any], handover_md: str) -> None:
    """Combine the required-rules score with advisory grounding, explicitly preserving unavailable state."""
    rules_verdict = _last_verdict()
    if rules_verdict is None:
        return
    try:
        from ack.verify import VerdictResult, verify_grounding
    except Exception:
        return

    try:
        evs = tuple(_loop_profile(fields.get("type")).eval_verifiers or ())
    except Exception:
        evs = ()
    run_grounding = ("grounding" in evs) if evs else True
    grounding = None
    unavailable = "grounding not selected"
    if run_grounding:
        unavailable = "grounding unavailable (no memory tier)"
        try:
            if _MEMORY is not None and _MEMORY.is_available():
                claims = [ln.strip() for ln in handover_md.splitlines()
                          if len(ln.strip()) >= 30 and not ln.lstrip().startswith("#")][:12]
                if claims:
                    retrieved_any = False
                    retrieval_error = False

                    def retrieve(claim: str) -> bool:
                        nonlocal retrieved_any, retrieval_error
                        try:
                            hits = _MEMORY.search(claim, limit=3)
                        except Exception:
                            retrieval_error = True
                            return False
                        if hits:
                            retrieved_any = True
                            return True
                        return False

                    candidate = verify_grounding(
                        claims,
                        retrieve,
                        threshold=_VERIFY_GROUNDING_THRESHOLD,
                    )
                    if retrieval_error:
                        unavailable = "grounding unavailable (memory error)"
                    elif retrieved_any:
                        grounding = candidate
                    else:
                        unavailable = "grounding unavailable (no grounding hits)"
                else:
                    unavailable = "grounding unavailable (no substantive claims)"
        except Exception:
            unavailable = "grounding unavailable (memory error)"

    if grounding is None:
        _set_last_verdict(VerdictResult(
            rules_verdict.passed,
            rules_verdict.score,
            f"rules {rules_verdict.score:.2f}; {unavailable}",
            "handover",
        ))
        return
    score = (rules_verdict.score + grounding.score) / 2
    _set_last_verdict(VerdictResult(
        rules_verdict.passed and grounding.passed,
        score,
        f"rules {rules_verdict.score:.2f}; grounding {grounding.score:.2f}",
        "handover",
    ))


def _ambiguity_gate(handover_md: str, unit: str) -> Optional[str]:
    """Refuse an ambiguous handover with the detector's halt-to-ask question and options."""
    try:
        from ack.ace.fork import detect_ambiguity
        signal = detect_ambiguity(handover_md, unit=unit)
    except Exception:
        return "ERROR: ambiguity detector unavailable (internal) — refuse (fail-closed). Nothing created."
    if signal is None or signal.is_empty():
        return None
    options = " | ".join(signal.options) or "Ask the operator to clarify"
    return (f"ERROR: ambiguous handover refused — {signal.question}\n"
            f"Options: {options}\nNothing created; clarify the fork and retry.")


def _quality_hold() -> Optional[str]:
    """Hold the next protected staging write while the output-quality breaker is tripped."""
    snapshot = _quality_tripped()
    if snapshot is None:
        return None
    reason = str(getattr(snapshot, "reason", "sustained degradation") or "sustained degradation")
    message = (f"ERROR: output-quality breaker tripped ({reason}) — staging held until a passing-quality "
               "submission clears the breaker or the operator runs `/quality reset`. Nothing created.")
    _ui_print(col(f"  [quality] escalation — {message}", C.YELLOW))
    _emit_hook("escalation", {"kind": "output_quality", "reason": reason, "action": "staging_hold"})
    return message


def _mandatory_staging_gates(fields: Dict[str, Any], handover_md: str, unit: str, *, stored: bool = False) -> Optional[str]:
    """Shared F5a pre-write boundary for both task creation and re-handing."""
    _set_last_verdict(None)
    ack_err = _ack_validate(fields, stored=stored)
    if ack_err:
        return ("ERROR: task_json violates the ACK contract (nothing created):\n"
                + ack_err + "\n→ fix the fields and call stage_handover again.")
    # The required verifier rules validate a NEWLY AUTHORED task_json's quality. A pure re-hand (stored=True)
    # carries no new task_json — the task was authored + validated at create — so re-checking the stored
    # fields would wrongly refuse an already-created task. Ambiguity + quality-hold still gate both paths.
    if not stored:
        verifier_err = _required_verifier_gate(fields)
        if verifier_err:
            return verifier_err
    _record_advisory_grounding(fields, handover_md)
    ambiguity_err = _ambiguity_gate(handover_md, unit)
    if ambiguity_err:
        return ambiguity_err
    return _quality_hold()


def _stage_handover(task_id: Optional[str], agent: str, handover_md: str,
                    task_json: Optional[str] = None, set_active: bool = True,
                    force: bool = False) -> str:
    """Serialized wrapper (#601 S12b): publish the task+handover under the per-project+track vault lock
    so the id scan + task/handover/active.md writes can't interleave with a concurrent vault mutation
    (the in-process ``TaskStore`` RLock alone does not serialize across processes). Reentrant: the
    impl's inner reconcile does not re-acquire."""
    _emit_hook("pre_handover", {"task_id": task_id, "agent": (agent or "").upper(),
                                "handover_md": handover_md, "task_json": task_json})
    with _vault_lock():
        result = _stage_handover_impl(task_id, agent, handover_md,
                                      task_json=task_json, set_active=set_active, force=force)
    _emit_hook("post_handover", {"task_id": task_id, "agent": (agent or "").upper(),
                                 "handover_md": handover_md, "result": result})
    return result


def _stage_handover_impl(task_id: Optional[str], agent: str, handover_md: str,
                         task_json: Optional[str] = None, set_active: bool = True,
                         force: bool = False) -> str:
    """Publishes a NEW task+handover in ONE step via the
    TaskStore: ID assignment, created_at stamp, schema and topic dedup are
    deterministic (no AI involvement). On a topic duplicate, fail-closed —
    nothing is created, the existing task is named."""
    agent = (agent or "").upper()
    if not _code_agent_registry().has(agent):   # #449: config-driven membership, fail-closed (no KIMI-norm)
        return f"ERROR: unknown agent {agent!r} (configured: {', '.join(_agent_names()) or 'none'})"
    if not handover_md or not handover_md.strip():
        return "ERROR: handover_md is empty — the full handover text is required."
    _blk = _internal_target_blocks_normal()           # #979: normal pipeline is off on an internal target
    if _blk:
        return f"ERROR: {_blk}"

    store = _store()
    log: List[str] = []
    task_type = ""
    try:
        if task_json:
            # parse task fields
            if isinstance(task_json, dict):
                fields = dict(task_json)
            else:
                try:
                    fields = json.loads(task_json)
                except json.JSONDecodeError as e:
                    return f"ERROR: task_json is not valid JSON: {e} — nothing created."
                if not isinstance(fields, dict):
                    return "ERROR: task_json must be a JSON object — nothing created."
            task_type = str(fields.get("type", "")).lower()
            validation_err = _mandatory_staging_gates(fields, handover_md, active_slug() or "")
            if validation_err:
                return validation_err
            # S5 (#1227): fail-closed design→impl gate — refuse an IMPLEMENTATION handover until the active
            # unit has a recorded + APPROVED design (no blind coding, R2). Runs BEFORE store.create, so a
            # refusal mutates nothing. `force` does NOT bypass it (the approval file is the intended override).
            gate_err = _design_gate(task_type, active_slug())
            if gate_err:
                return gate_err
            csnap = (_constraint_status(active_slug()) if FRAMING_NOTES_ENABLED else (UNCAPTURED, None))
            try:
                design_standard = _approved_design_standard_snapshot(active_slug())
                protected_handover = _inject_approved_design_standard(handover_md, design_standard, log)
            except Exception as ex:  # noqa: BLE001 -- protected injection preflight must fail closed
                return (f"ERROR: approved design standard injection failed ({ex!r}) — refuse "
                        "(fail-closed). Nothing changed.")
            # #1342 (S6): L3 typed hard-check on the REAL task object (TaskSpec language/network).
            # IMPLEMENTATION types only; PRE-write; `force` does NOT bypass. Flag off → no-op.
            if task_type in _IMPLEMENTATION_TASK_TYPES:
                hc_err = _design_build_check(active_slug(), _task_typed_fields(fields))
                if hc_err:
                    return hc_err
            # Store: dedup + ID + created_at + schema, writes the pending JSON
            try:
                task = store.create(fields, force=bool(force))
            except DuplicateTaskError as e:
                if e.exact:
                    return (f"ERROR: duplicate — an EXACT-title task already exists as "
                            f"{e.existing_id}. Re-hand it with `task_id={e.existing_id}` "
                            f"(no task_json); `force` does NOT create an exact-title duplicate.")
                return (f"ERROR: duplicate — a task on the same topic already exists as "
                        f"{e.existing_id}. No new task created. Use the existing task "
                        f"or (only when instructed) set force=true.")
            except ValueError as e:
                return f"ERROR: {e} — no task created."
            tid = task["id"]
            log.append(f"task created: {tid} (pending, created_at={task['created_at']})")
            ho_md = _enrich_handover(
                tid, protected_handover, fields, log, agent, constraint_snapshot=csnap,
                approved_design_standard=design_standard,
                approved_design_preinjected=True,
            )
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, ho_md)
            log.append(f"handover written: {ho} ({len(ho_md)} chars)")
        else:
            # Pure handover without task JSON — requires a valid, EXISTING task_id.
            if not task_id or not _TASK_ID_RE.match(task_id):
                return f"ERROR: without task_json a valid task_id is required (was: {task_id!r})"
            existing = store.get(task_id)
            if existing is None:
                return f"ERROR: no such task {task_id!r} — create it with task_json first (nothing written)."
            validation_err = _mandatory_staging_gates(existing, handover_md, active_slug() or "", stored=True)
            if validation_err:
                return validation_err
            # S5 (#1227): re-handing an IMPLEMENTATION task still needs an approved design — a blank
            # re-handover of an impl-typed task cannot slip past the design gate.
            gate_err = _design_gate(str(existing.get("type", "")), active_slug())
            if gate_err:
                return gate_err
            csnap = (_constraint_status(active_slug()) if FRAMING_NOTES_ENABLED else (UNCAPTURED, None))
            try:
                design_standard = _approved_design_standard_snapshot(active_slug())
                protected_handover = _inject_approved_design_standard(handover_md, design_standard, log)
            except Exception as ex:  # noqa: BLE001 -- protected injection preflight must fail closed
                return (f"ERROR: approved design standard injection failed ({ex!r}) — refuse "
                        "(fail-closed). Nothing changed.")
            # #1342 (S6): re-hand of an IMPLEMENTATION task still hard-checks the stored typed fields.
            if str(existing.get("type", "")).strip().lower() in _IMPLEMENTATION_TASK_TYPES:
                hc_err = _design_build_check(active_slug(), _task_typed_fields(existing))
                if hc_err:
                    return hc_err
            tid = task_id
            # #1296 parity: the lazily staged unit handover (the continuation's [NEXT-UNIT] path)
            # gets the SAME enrichment as a created one — id normalization, memory brief, lessons.
            ho_md = _enrich_handover(
                tid, protected_handover, existing, log, agent, constraint_snapshot=csnap,
                approved_design_standard=design_standard,
                approved_design_preinjected=True,
            )
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, ho_md)
            log.append(f"handover written: {ho} ({len(ho_md)} chars)")
            # Canonical identity (#1294 fix 3, extended to the re-hand path): the staged agent IS
            # the assignment — stamp assigned_to so filename == assigned_to == body `to:` and the
            # reconciler's first guess matches what actually runs. Fail-soft: a stamping hiccup
            # never breaks the staging.
            if str(existing.get("assigned_to") or "").strip().upper() != agent:
                try:
                    p, s = store._find(tid)
                    if p:
                        data = json.loads(p.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            data["assigned_to"] = agent
                            _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=2))
                            log.append(f"assigned_to stamped: {agent}")
                except Exception:  # noqa: BLE001
                    pass

        if set_active:
            store.project_active()
            log.append("active.md projected (= newest non-done handover)")

    except Exception as e:
        return f"ERROR: stage_handover failed: {e}\nSo far:\n" + "\n".join(f"  - {l}" for l in log)

    result = f"OK: handover {tid} ({agent}) staged\n" + "\n".join(f"  - {l}" for l in log)
    # Path guard only for code tasks: with type=documentation (memory seed, docs)
    # the agent builds no code → no duplication risk, the check would only be noise.
    bad = [] if task_type == "documentation" else _handover_path_warnings(handover_md)
    if bad:
        result += (
            "\n\n⚠ PATH CHECK — these code paths in the handover do NOT exist "
            "(neither relative to the repo root nor under CODE_ROOT):\n"
            + "\n".join(f"    - {p}" for p in bad[:10])
            + "\n  → if they reference EXISTING code, they are WRONG — "
              "fix them, else the agent builds anew instead of extending (a duplicate). "
              "Files to be newly created are fine."
        )
    _reconcile_active_soft()   # C2: keep the active initiative's INDEX.md fresh (fail-soft, index only)
    return result


# ─── Macro tool: plan_units — epic decomposition in ONE deterministic step (#1296) ──
_UNIT_DEP_PLACEHOLDER_RE = re.compile(r"^unit:(\d+)$")


def _plan_units(epic_json: "Optional[Any]" = None, units_json: "Optional[Any]" = None,
                epic_id: "Optional[str]" = None, force: bool = False) -> str:
    """Serialized wrapper (mirrors ``_stage_handover``): publish the epic + its units under the
    per-project+track vault lock so the id scan + batch writes can't interleave with a concurrent
    vault mutation."""
    with _vault_lock():
        return _plan_units_impl(epic_json, units_json, epic_id=epic_id, force=force)


def _plan_units_impl(epic_json: "Optional[Any]", units_json: "Optional[Any]",
                     epic_id: "Optional[str]" = None, force: bool = False) -> str:
    """#1296: materialize an approved design's FULL decomposition as tracker records in ONE call —
    one ``epic`` task + N child units (``parent`` = the epic id), all ``pending`` and deliberately
    WITHOUT handovers: each unit's handover is authored lazily when the continuation selects it
    (anchors stay real, the plan-change duty stays possible). Deterministic + fail-closed:
    every unit is ACK-validated and topic-deduped BEFORE anything is written (atomic — on any
    refusal nothing is created; on a mid-write error the created files are rolled back).
    ``epic_id`` targets an EXISTING open epic instead (plan-change: add units mid-run); a done
    epic is refused. In-batch ``dependencies`` may reference sibling units as ``unit:<n>``
    (1-based order in *units_json*) — resolved to the minted Task-IDs after creation."""
    _blk = _internal_target_blocks_normal()           # #979: normal pipeline is off on an internal target
    if _blk:
        return f"ERROR: {_blk}"
    store = _store()

    # ── Parse inputs ────────────────────────────────────────────
    def _as_obj(raw: "Any", what: str) -> "tuple[Optional[Any], Optional[str]]":
        if raw is None or isinstance(raw, (dict, list)):
            return raw, None
        try:
            return json.loads(raw), None
        except (TypeError, json.JSONDecodeError) as e:
            return None, f"ERROR: {what} is not valid JSON: {e} — nothing created."
    epic_fields, err = _as_obj(epic_json, "epic_json")
    if err:
        return err
    units, err = _as_obj(units_json, "units_json")
    if err:
        return err
    if not isinstance(units, list) or not units or not all(isinstance(u, dict) for u in units):
        return "ERROR: units_json must be a non-empty JSON array of unit objects — nothing created."

    # ── Resolve the epic: existing (epic_id) or to-be-created (epic_json) ──
    existing_epic: "Optional[Dict[str, Any]]" = None
    if epic_id:
        if not _TASK_ID_RE.match(epic_id):
            return f"ERROR: invalid epic_id: {epic_id!r} (expected e.g. KGC-3)"
        existing_epic = store.get(epic_id)
        if existing_epic is None:
            return f"ERROR: no such epic {epic_id!r} — nothing created."
        if str(existing_epic.get("type", "")).lower() != "epic":
            return (f"ERROR: task {epic_id} has type {existing_epic.get('type')!r}, not 'epic' — "
                    f"units can only be added under an epic. Nothing created.")
        if existing_epic.get("status") == "done":
            return (f"ERROR: epic {epic_id} is already done — a completed epic does not take new "
                    f"units. Plan a NEW epic (plan_units with epic_json) instead. Nothing created.")
    else:
        if not isinstance(epic_fields, dict):
            return "ERROR: epic_json (object) or epic_id is required — nothing created."
        etype = str(epic_fields.get("type", "epic")).lower()
        if etype != "epic":
            return f"ERROR: epic_json.type must be 'epic' (was {etype!r}) — nothing created."
        epic_fields = dict(epic_fields)
        epic_fields["type"] = "epic"

    # ── Pre-validate EVERY record before anything is written (atomic) ──
    checked: "List[Dict[str, Any]]" = []
    for i, u in enumerate(units, 1):
        f = dict(u)
        utype = str(f.get("type", "")).lower()
        if utype == "epic":
            return f"ERROR: unit {i} has type 'epic' — epics nest exactly one level (units under one epic). Nothing created."
        # Split sibling placeholders (unit:<n>) from real Task-ID dependencies; placeholders are
        # resolved AFTER the ids are minted, real ids must validate against the contract now.
        deps = f.get("dependencies") or []
        if not isinstance(deps, list):
            return f"ERROR: unit {i}: dependencies must be a list. Nothing created."
        placeholders: "List[tuple[int, int]]" = []      # (dep-slot, referenced unit ordinal)
        real_deps: "List[str]" = []
        for d in deps:
            m = _UNIT_DEP_PLACEHOLDER_RE.match(str(d).strip())
            if m:
                ref = int(m.group(1))
                if not (1 <= ref <= len(units)) or ref == i:
                    return (f"ERROR: unit {i}: dependency {d!r} references unit {ref}, which is not "
                            f"another unit of this batch (1..{len(units)}, not itself). Nothing created.")
                placeholders.append((len(real_deps) + len(placeholders), ref))
            else:
                real_deps.append(str(d))
        f["dependencies"] = real_deps
        ack_err = _ack_validate(f)
        if ack_err:
            return (f"ERROR: unit {i} violates the ACK contract (nothing created):\n" + ack_err
                    + "\n→ fix the fields and call plan_units again.")
        gate_err = _design_gate(utype, active_slug())
        if gate_err:
            return gate_err
        # #1342 (S6): L3 hard-check on IMPLEMENTATION children only (atomic PRE-write; force no-bypass).
        if utype in _IMPLEMENTATION_TASK_TYPES:
            hc_err = _design_build_check(active_slug(), _task_typed_fields(f))
            if hc_err:
                return hc_err
        f["__placeholders"] = placeholders
        checked.append(f)
    if existing_epic is None:
        ack_err = _ack_validate({k: v for k, v in epic_fields.items()})
        if ack_err:
            return ("ERROR: epic_json violates the ACK contract (nothing created):\n" + ack_err
                    + "\n→ fix the fields and call plan_units again.")

    # ── Dedup: against the store (all statuses, incl. done) AND within the batch ──
    if not force:
        if existing_epic is None:
            dup = store.find_duplicate(epic_fields.get("title", ""), epic_fields.get("description", ""))
            if dup:
                return (f"ERROR: duplicate — a task on the epic's topic already exists as {dup}. "
                        f"Add units to it via plan_units(epic_id='{dup}', ...) if it is the same epic, "
                        f"or (only when instructed) set force=true. Nothing created.")
        for i, f in enumerate(checked, 1):
            dup = store.find_duplicate(f.get("title", ""), f.get("description", ""))
            if dup:
                return (f"ERROR: duplicate — unit {i} ({f.get('title')!r}) matches existing task {dup}. "
                        f"Nothing created; drop or rename the unit (force only on instruction).")
            tok_i = store._tokens(f"{f.get('title','')} {f.get('description','')}")
            for j, g in enumerate(checked[:i - 1], 1):
                same_key = store._title_key(f.get("title", "")) == store._title_key(g.get("title", ""))
                if same_key or store._jaccard(
                        tok_i, store._tokens(f"{g.get('title','')} {g.get('description','')}")) >= store.dedup_threshold:
                    return (f"ERROR: units {j} and {i} are topic duplicates of each other "
                            f"({g.get('title')!r} vs {f.get('title')!r}). Nothing created.")

    # ── Create (epic first, then units); roll back everything on a mid-write error ──
    created: "List[Dict[str, Any]]" = []
    log: "List[str]" = []
    try:
        if existing_epic is None:
            epic = store.create(epic_fields, force=bool(force))
            created.append(epic)
            eid = epic["id"]
            log.append(f"epic created: {eid} ({epic_fields.get('title')!r})")
        else:
            eid = existing_epic.get("id") or epic_id
            log.append(f"epic: {eid} (existing, {existing_epic.get('title')!r})")
        minted: "List[Dict[str, Any]]" = []
        for f in checked:
            placeholders = f.pop("__placeholders")
            f["parent"] = eid
            task = store.create(f, force=bool(force))
            created.append(task)
            task["__placeholders"] = placeholders
            minted.append(task)
        # Resolve sibling placeholders now that every unit id exists (write-through, atomic per file).
        for task in minted:
            placeholders = task.pop("__placeholders")
            if not placeholders:
                continue
            deps = list(task.get("dependencies") or [])
            for _slot, ref in placeholders:
                deps.append(minted[ref - 1]["id"])
            task["dependencies"] = deps
            _atomic_write(store._path(task["id"], "pending"),
                          json.dumps(task, ensure_ascii=False, indent=2))
            log.append(f"{task['id']}: sibling dependencies resolved → {deps}")
    except DuplicateTaskError as e:
        for t in created:
            p, _s = store._find(t["id"])
            if p:
                p.unlink(missing_ok=True)
        return (f"ERROR: duplicate — a task on the same topic already exists as {e.existing_id}. "
                f"Nothing created (batch rolled back).")
    except Exception as e:  # noqa: BLE001 — atomicity: never leave a half-created decomposition
        for t in created:
            p, _s = store._find(t["id"])
            if p:
                p.unlink(missing_ok=True)
        return f"ERROR: plan_units failed: {e} — batch rolled back, nothing created."

    unit_ids = [t["id"] for t in created if str(t.get("type", "")).lower() != "epic"]
    for t in created:
        if str(t.get("type", "")).lower() != "epic":
            log.append(f"unit created: {t['id']} [{t.get('type')}/{t.get('priority')}] {t.get('title')!r}")
    _write_board()
    _reconcile_active_soft()
    nxt, _elig, _open = _select_next_unit(store)
    # #1310: the engine honours declared `dependencies` topologically, but a small model tends to omit them
    # — then the selector orders by priority (not build order) and can start a module before its scaffolding.
    _new_unit_ids = {t["id"] for t in created if str(t.get("type", "")).lower() != "epic"}
    # #1310 (Codex): "no build order" = no NEWLY-MINTED unit depends on ANOTHER newly-minted unit (a real
    # sibling `unit:<n>` edge). A dependency on a pre-existing task id does not order the new units among
    # themselves, so it must not suppress the note.
    _no_dep_multi = len(_new_unit_ids) > 1 and not any(
        d in _new_unit_ids
        for t in created if str(t.get("type", "")).lower() != "epic"
        for d in (t.get("dependencies") or []))
    _dep_warn = ("\n\nNOTE: no inter-unit dependencies were declared — the units are selected by priority "
                 "then order, NOT build order. Stage the FOUNDATIONAL unit (the one that must build first, "
                 "e.g. scaffolding) yourself, and declare build-order `dependencies` (`unit:<n>`: scaffolding "
                 "before modules, a module after the models/utils it imports, tests after the code) whenever "
                 "you plan — the loop then runs units in dependency order.") \
        if _no_dep_multi else ""
    if nxt and AUTOPILOT_AUTOPLAN:
        # Continuation already armed: this same turn is the bootstrap — the model authors the first
        # unit's handover right away (the post-advance turns take over from there). #1309: the launch
        # instruction depends on WHO launches — the loop (defer) only when it actually owns launching;
        # in an autoplan-only run (continuation without an active launcher) the model must still launch.
        _launch_note = ("and do NOT call launch_coder — the automation loop launches the staged handover "
                        "itself" if _auto_owns_launching() else
                        "then launch it with launch_coder (the continuation advances to the next unit "
                        "after this one completes)")
        if _no_dep_multi:
            # #1310: with NO declared order do NOT auto-point the bootstrap at the priority-selected unit
            # (it may be a module before its scaffolding) — have the model pick the foundational unit, so
            # the automation loop never blindly starts a mis-ordered build.
            tail = (f"\nAUTOMATION ARMED — author the FOUNDATIONAL unit's handover NOW (the one that must "
                    f"build FIRST, e.g. scaffolding — with no declared dependencies the selector picked "
                    f"{nxt['id']} by PRIORITY, which may be the wrong order): ONE stage_handover call with "
                    f"its task_id and NO task_json, {_launch_note}.")
        else:
            tail = (f"\nAUTOMATION ARMED — author the FIRST unit's handover NOW: ONE stage_handover call "
                    f"with task_id='{nxt['id']}' ({nxt.get('title')!r}) and NO task_json, {_launch_note}. "
                    f"The loop advances + continues from there by itself.")
    elif nxt and _no_dep_multi:
        # #1310 (Codex): no declared build order → do NOT point the guided recommendation at the priority
        # pick either; ask for the foundational unit, matching the armed branch.
        tail = (f"\nNo build order is declared, so the selector picked {nxt['id']} by PRIORITY — which may "
                f"not be the right first unit. In guided mode, stage the FOUNDATIONAL unit (the one that "
                f"must build first, e.g. scaffolding) via stage_handover (no task_json); `/auto on [N]` "
                f"drains the units once build-order `dependencies` are declared.")
    elif nxt:
        tail = (f"\nNext open unit: {nxt['id']} ({nxt.get('title')!r}) — `/auto on [N]` drains all "
                f"units automatically; in guided mode, stage its handover via stage_handover "
                f"(task_id='{nxt['id']}', no task_json).")
    else:
        tail = ""
    return (f"OK: epic {eid} planned with {len(unit_ids)} unit(s) — all pending, handover-less "
            f"(each handover is authored when the unit is selected)\n"
            + "\n".join(f"  - {l}" for l in log) + _dep_warn + tail)


# ─── #1296: pure next-unit selection (the select-unit leg of the contract loop) ──
_UNIT_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "normal": 3, "low": 4}


def _unit_sort_key(task: "Dict[str, Any]") -> "tuple":
    """Deterministic selection order: priority → created_at → numeric id. Mirrors the GitHub
    selector ('highest-priority open sub-issue first'); an unknown priority ranks LAST (fail-soft,
    never ahead of an explicit one)."""
    prio = _UNIT_PRIORITY_ORDER.get(str(task.get("priority", "")).strip().lower(), len(_UNIT_PRIORITY_ORDER))
    m = re.search(r"-(\d+)$", str(task.get("id", "")))
    return (prio, task.get("created_at", ""), int(m.group(1)) if m else 1 << 30)


def _select_next_unit(store: "TaskStore") -> "tuple[Optional[Dict[str, Any]], int, int]":
    """The DETERMINISTIC select-unit policy (#1296, pure code — no model): among the OPEN units
    (pending, not an epic, no handover staged yet) pick the one to work next. Eligible = not
    ``blocked`` and every ``dependencies`` entry is done (an unknown dep id counts as UNSATISFIED —
    fail-closed). Order: priority → created_at → numeric id. Returns ``(winner|None,
    eligible_count, open_count)`` — ``open_count > 0`` with no winner is a dependency/blocked
    deadlock the caller must surface (never a silent idle)."""
    open_units = [t for t in store.list("pending")
                  if str(t.get("type", "")).lower() != "epic"
                  and _find_handover(t.get("id") or "") is None]
    if not open_units:
        return None, 0, 0
    done_ids = {t.get("id") for t in store.list("done")}
    eligible = [t for t in open_units
                if not t.get("blocked")
                and all(d in done_ids for d in (t.get("dependencies") or []))]
    if not eligible:
        return None, 0, len(open_units)
    return sorted(eligible, key=_unit_sort_key)[0], len(eligible), len(open_units)


def _work_in_flight(store: "TaskStore") -> bool:
    """#1296: is the pipeline actively working? True on any ``in_progress`` task or any pending
    task WITH a staged handover (launchable — the launcher's job, not the planner's). Handover-less
    pending units (an epic's open backlog) and epic records do NOT count — they are exactly what
    the continuation exists to drain."""
    for t in store.list("in_progress"):
        if str(t.get("type", "")).lower() != "epic":
            return True
    for t in store.list("pending"):
        if str(t.get("type", "")).lower() == "epic":
            continue
        if _find_handover(t.get("id") or "") is not None:
            return True
    return False


# ─── TaskStore: deterministic task truth (model 3) ─────
# Single truth: tasks/<status>/KGC-NNN.json. The DIRECTORY is the
# status authority; the status field is updated by the store; active.md
# is a projection of the in_progress handover. All mutations go
# through this API, serialized (single-writer). NO AI involvement:
# ID assignment, created_at, schema, double-ID and topic dedup are code.

_BLOCKED_ANNOTATION_KEYS = ("blocked", "blocked_reason", "blocked_kind", "blocked_at")


def _task_is_escalated(task: Any) -> bool:
    """Return whether *task* carries the durable terminal retry-budget annotation."""
    return bool(isinstance(task, dict) and task.get("blocked") and task.get("blocked_kind") == "escalated")


class DuplicateTaskError(Exception):
    """Raised when a task on the same topic already exists."""
    def __init__(self, existing_id: str, exact: bool = False):
        super().__init__(f"duplicate of {existing_id}")
        self.existing_id = existing_id
        self.exact = exact


class ContextOverflowError(RuntimeError):
    """Epic #366 (#372): raised by the pre-flight guard when a single turn cannot be made to fit the
    model window — even after an emergency whole-round trim. A clear, actionable error (prompt /
    output / window sizes) instead of letting vLLM return a raw HTTP 400 (`maximum context length`)."""


class TaskStore:
    STATUSES = ("pending", "in_progress", "done")
    REQUIRED = ("type", "priority", "title", "description")
    # Generic function words (de/en) — domain terms are kept.
    _STOP = {
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
        "einer", "eines", "und", "oder", "fur", "für", "zu", "mit", "von",
        "im", "in", "an", "auf", "aus", "bei", "nach", "uber", "über", "vor",
        "ist", "sind", "als", "wie", "the", "a", "an", "and", "or", "for",
        "to", "of", "with", "on", "at",
    }

    def __init__(self, root: Optional[str] = None, dedup_threshold: Optional[float] = None):
        # B3: root=None → the paths route dynamically to the ACTIVE initiative (production singleton);
        # an explicit root (tests/special cases) keeps the legacy behaviour (root-relative). The
        # dedup_threshold default is read at instantiation time from the (possibly config-set)
        # global — not frozen as a param default.
        self.root            = Path(root) if root is not None else None
        self.dedup_threshold = float(dedup_threshold if dedup_threshold is not None
                                     else TASKS_DEDUP_THRESHOLD)
        self._lock           = threading.RLock()

    # ── Paths (B3: dynamic to the active initiative, soft for read/scan paths) ──
    def _base(self) -> Optional[Path]:
        """Routing root: explicit root OR the active initiative (soft → None without an active one,
        so read/scan paths never crash a daemon)."""
        return self.root if self.root is not None else artifact_root_soft()

    def _require_base(self) -> Path:
        """Like _base, but fail-closed (RuntimeError) — for producing ops (create/transition)."""
        b = self._base()
        if b is None:
            raise RuntimeError(_msg("init.no_active"))
        if self.root is None:                          # #979: only gate when routed to the ACTIVE project
            _blk = _internal_target_blocks_normal()    # normal pipeline is off on an internal target
            if _blk:
                raise RuntimeError(_blk)
        return b

    def _dir(self, status: str) -> Optional[Path]:
        b = self._base()
        return (b / "tasks" / status) if b is not None else None

    def _path(self, task_id: str, status: str) -> Optional[Path]:
        d = self._dir(status)
        return (d / f"{task_id}.json") if d is not None else None

    def _find(self, task_id: str) -> Tuple[Optional[Path], Optional[str]]:
        for s in self.STATUSES:
            p = self._path(task_id, s)
            if p is not None and p.exists():
                return p, s
        return None, None

    def _handover_path(self, task_id: str) -> Optional[Path]:
        b = self._base()
        if b is None:
            return None
        d = b / WORKFLOW_DIR / "handovers"
        if not d.exists():
            return None
        hits = sorted(d.glob(f"{task_id}_*.md"))
        return hits[0] if hits else None

    # ── Identity ────────────────────────────────────────────
    def next_id(self) -> str:
        """Next free ID across ALL statuses (monotonic, never reused).
        Read the prefix from the (possibly config-set) TASK_PREFIX global at
        runtime — don't freeze it."""
        pref = TASK_PREFIX
        id_re = re.compile(rf"^{re.escape(pref)}-(\d+)$")
        with self._lock:
            mx = 0
            for s in self.STATUSES:
                d = self._dir(s)
                if d is None or not d.exists():
                    continue
                for f in d.glob(f"{pref}-*.json"):
                    m = id_re.match(f.stem)
                    if m:
                        mx = max(mx, int(m.group(1)))
            return f"{pref}-{mx + 1}"

    # ── Dedup (purely deterministic) ─────────────────────────
    @classmethod
    def _tokens(cls, text: str) -> List[str]:
        t = re.sub(r"[^\w\s]", " ", (text or "").lower(), flags=re.UNICODE)
        return [w for w in t.split() if w and w not in cls._STOP]

    @classmethod
    def _title_key(cls, title: str) -> str:
        return " ".join(sorted(set(cls._tokens(title))))

    @staticmethod
    def _jaccard(a: List[str], b: List[str]) -> float:
        sa, sb = set(a), set(b)
        if not sa and not sb:
            return 1.0
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    def find_exact_duplicate(self, title: str,
                             exclude_id: Optional[str] = None) -> Optional[str]:
        with self._lock:
            key = self._title_key(title)
            for task in self.list():
                if exclude_id and task.get("id") == exclude_id:
                    continue
                if key and self._title_key(task.get("title", "")) == key:
                    return task.get("id")
            return None

    def find_duplicate(self, title: str, description: str = "",
                       exclude_id: Optional[str] = None) -> Optional[str]:
        with self._lock:
            key = self._title_key(title)
            tok = self._tokens(f"{title} {description}")
            for task in self.list():
                if exclude_id and task.get("id") == exclude_id:
                    continue
                if key and self._title_key(task.get("title", "")) == key:
                    return task.get("id")
                other = self._tokens(f"{task.get('title','')} {task.get('description','')}")
                if self._jaccard(tok, other) >= self.dedup_threshold:
                    return task.get("id")
            return None

    # ── Schema ───────────────────────────────────────────────
    def _validate(self, fields: Dict[str, Any]):
        missing = [k for k in self.REQUIRED if not str(fields.get(k, "")).strip()]
        if missing:
            raise ValueError(f"required fields missing: {', '.join(missing)}")

    # ── Reading ────────────────────────────────────────────────
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p, s = self._find(task_id)
            if not p:
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["status"] = s          # directory is authority
                data.setdefault("id", task_id)
            return data

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for s in ((status,) if status else self.STATUSES):
                d = self._dir(s)
                if d is None or not d.exists():
                    continue
                for f in sorted(d.glob("KGC-*.json")):
                    try:
                        data = json.loads(f.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    if isinstance(data, dict):
                        data["status"] = s
                        data.setdefault("id", f.stem)
                        out.append(data)
            return out

    # ── Mutations ───────────────────────────────────────────
    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def create(self, fields: Dict[str, Any], *, force: bool = False,
               now_iso: Optional[str] = None) -> Dict[str, Any]:
        """Creates a pending task. Assigns the ID, stamps created_at,
        validates, rejects an exact-title duplicate, and rejects a fuzzy topic
        duplicate unless forced. Model-supplied id/created_at/status are
        IGNORED/overwritten."""
        with self._lock:
            self._validate(fields)
            self._require_base()   # B3: fail-closed — no writing to the root without an active initiative
            exact = self.find_exact_duplicate(fields["title"])
            if exact:
                raise DuplicateTaskError(exact, exact=True)
            if not force:
                dup = self.find_duplicate(fields["title"], fields.get("description", ""))
                if dup:
                    raise DuplicateTaskError(dup, exact=False)
            tid = self.next_id()
            task = dict(fields)
            task["id"]         = tid
            task["status"]     = "pending"
            task["created_at"] = now_iso or self._now_iso()
            self._dir("pending").mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path(tid, "pending"),
                          json.dumps(task, ensure_ascii=False, indent=2))
            return task

    def transition(self, task_id: str, to_status: str) -> Dict[str, Any]:
        """Moves the task JSON between status folders (atomically), updates
        the status field and re-projects active.md."""
        if to_status not in self.STATUSES:
            raise ValueError(f"invalid status: {to_status!r}")
        with self._lock:
            self._require_base()   # B3: fail-closed
            p, s = self._find(task_id)
            if not p:
                raise KeyError(f"task not found: {task_id}")
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"id": task_id}
            data["id"]     = task_id
            data["status"] = to_status
            if to_status != "in_progress":
                data.pop("claimed_at", None)
            # S7 (#1229): a task that advances is no longer blocked — drop any blocked annotation. No-op (byte-
            # identical) for a task that never carried one.
            for _bk in _BLOCKED_ANNOTATION_KEYS:
                data.pop(_bk, None)
            self._dir(to_status).mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path(task_id, to_status),
                          json.dumps(data, ensure_ascii=False, indent=2))
            if s != to_status:
                p.unlink()
            self.project_active()
            return data

    def stamp_claim(self, task_id: str, claimed_at: float) -> None:
        """Persist the client-run claim lease timestamp in place."""
        with self._lock:
            p, s = self._find(task_id)
            if not p:
                raise KeyError(f"task not found: {task_id}")
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"id": task_id}
            data["id"] = task_id
            data["status"] = s
            data["claimed_at"] = float(claimed_at)
            _atomic_write(self._path(task_id, s), json.dumps(data, ensure_ascii=False, indent=2))

    def mark_blocked(self, task_id: str, reason: str = "", kind: str = "blocked") -> None:
        """S7 (#1229): annotate a task blocked/stalled IN PLACE (no folder move) so the 3 directory states
        (STATUSES) stay untouched, but the board/steering can show a stuck task instead of a healthy-looking
        in_progress. Additive JSON keys → a task that never gets marked is byte-identical."""
        with self._lock:
            p, s = self._find(task_id)
            if not p:
                return
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:   # noqa: BLE001 — a bad task file is not worth crashing a reconcile tick
                return
            if not isinstance(data, dict):
                return
            data["id"] = task_id
            data["blocked"] = True
            data["blocked_reason"] = (reason or "").strip()
            data["blocked_kind"] = kind
            data["blocked_at"] = self._now_iso()
            _atomic_write(self._path(task_id, s), json.dumps(data, ensure_ascii=False, indent=2))

    def clear_blocked(self, task_id: str) -> None:
        """S7 (#1229): drop the blocked annotation (progress resumed). No-op if the task is not marked."""
        with self._lock:
            p, s = self._find(task_id)
            if not p:
                return
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:   # noqa: BLE001
                return
            if not isinstance(data, dict) or not data.get("blocked"):
                return
            for _bk in _BLOCKED_ANNOTATION_KEYS:
                data.pop(_bk, None)
            _atomic_write(self._path(task_id, s), json.dumps(data, ensure_ascii=False, indent=2))

    def project_active(self):
        """active.md = handover of the newest NON-done task (in_progress before
        pending at the same timestamp), otherwise idle. Pure projection — never
        to be maintained by hand."""
        with self._lock:
            b = self._base()
            if b is None:
                return                       # no active initiative → no projection (soft)
            active = b / WORKFLOW_DIR / "active.md"
            # in_progress ranks before pending; within, by created_at/id. #1296: pending units may
            # be handover-less (an epic's open backlog) — walk newest-first and project the first
            # task that actually HAS a handover, so a staged handover is never shadowed into idle
            # by a newer, not-yet-staged unit.
            cands = [(0, t) for t in self.list("pending")] + \
                    [(1, t) for t in self.list("in_progress")]
            cands.sort(key=lambda it: (it[0], it[1].get("created_at", ""), it[1].get("id", "")))
            for _rank, t in reversed(cands):
                ho = self._handover_path(t.get("id", ""))
                if ho and ho.exists():
                    _atomic_write(active, ho.read_text(encoding="utf-8"))
                    return
            _atomic_write(active, _IDLE_ACTIVE)


# Single, shared store (one lock → serialized mutations across macros AND reconciler).
# B3: root=None → paths route dynamically to the active initiative (vault/<slug>/), resolved late.
STORE: Optional["TaskStore"] = None

def _store() -> "TaskStore":
    global STORE
    if STORE is None:
        STORE = TaskStore(root=None)
    return STORE


def claim_task(task_id: str, agent: str) -> str:
    """Atomically acquire or renew a client-run task lease without moving a later state backward."""
    agent_u = (agent or "").strip().upper()
    registry = _code_agent_registry()
    if not registry.has(agent_u):
        raise ValueError(f"unknown agent {agent_u!r} (configured: {', '.join(registry.names()) or 'none'})")
    store = _store()
    with store._lock:
        task = store.get((task_id or "").strip())
        if task is None:
            return "not_found"
        status = str(task.get("status") or "not_found")
        if _task_is_escalated(task):
            return status
        if status == "pending":
            status = str(store.transition(task["id"], "in_progress")["status"])
            store.stamp_claim(task["id"], time.time())
            return status
        if status == "in_progress":
            store.stamp_claim(task["id"], time.time())
        return status


def unclaim_task(task_id: str) -> str:
    """Atomically release a failed client-run task without reopening a terminal escalation."""
    store = _store()
    with store._lock:
        task = store.get((task_id or "").strip())
        if task is None:
            return "not_found"
        status = str(task.get("status") or "not_found")
        if _task_is_escalated(task):
            return status
        if status == "in_progress":
            return str(store.transition(task["id"], "pending")["status"])
        return status


# ─── Platform mode (shell + syntax guidance from ONE source) ──
def _resolve_platform(mode: Optional[str]) -> str:
    """Resolves 'auto' at startup to a concrete mode. Invalid values
    fall back safely to OS detection."""
    m = (mode or "auto").strip().lower()
    if m in ("windows", "win", "nt"):
        return "windows"
    if m in ("linux", "posix", "unix", "mac", "darwin"):
        return "linux"
    # "auto" or unknown → auto-detect
    return "windows" if os.name == "nt" else "linux"


# #1183: per-command shell on Windows — a PowerShell cmdlet runs in PowerShell, a POSIX/bash command in Git
# Bash when it's installed; so BOTH shells work, neither is forced. Mirrors clients/ink/src/tools/shell.ts.
_PS_CMDLET_RE = re.compile(
    r"(?:^|[\s|;&(])(?:Get|Set|New|Remove|Select|Where|ForEach|Write|Add|Copy|Move|Rename|Test|Invoke|"
    r"Start|Stop|Out|Format|Sort|Measure|Import|Export|ConvertTo|ConvertFrom|Join|Split|Compare|Group|"
    r"Resolve|Clear|Push|Pop)-[A-Z]\w+"
)
_PS_SYNTAX_RE = re.compile(
    r"\$env:|\$PSItem|\$_(?:\.|\s|\)|$)|-Recurse\b|-Filter\b|-ErrorAction\b|\|\s*(?:Where|Select|ForEach|Sort|Measure)-",
    re.IGNORECASE,
)


def _detect_shell(command: str) -> str:
    """Which shell a command is written for: 'powershell' for PS cmdlets/syntax, else 'bash'."""
    c = command or ""
    return "powershell" if (_PS_CMDLET_RE.search(c) or _PS_SYNTAX_RE.search(c)) else "bash"


_GIT_BASH: "Optional[str]" = None
_GIT_BASH_RESOLVED = False


def _git_bash() -> "Optional[str]":
    """The Git Bash executable to prefer on Windows (``GX10_BASH`` override / Program Files / Scoop / PATH),
    or None → PowerShell. Cached. Skips WSL's ``System32\\bash.exe`` (runs in the WSL filesystem)."""
    global _GIT_BASH, _GIT_BASH_RESOLVED
    if _GIT_BASH_RESOLVED:
        return _GIT_BASH
    _GIT_BASH_RESOLVED = True
    if os.name != "nt":
        _GIT_BASH = None
        return None
    home = os.environ.get("USERPROFILE", "")
    candidates = [
        os.environ.get("GX10_BASH"),
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    if home:
        candidates += [
            os.path.join(home, r"scoop\apps\git\current\bin\bash.exe"),
            os.path.join(home, r"scoop\apps\git\current\usr\bin\bash.exe"),
            os.path.join(home, r"scoop\shims\bash.exe"),
        ]
    for c in candidates:
        if c and os.path.isfile(c):
            _GIT_BASH = c
            return c
    w = shutil.which("bash")
    if w and "system32" not in w.lower():
        _GIT_BASH = w
        return w
    _GIT_BASH = None
    return None


# ─── Deterministic listing count (#1193) — a shell listing carries the same authoritative count as
# list_directory, computed from the FILESYSTEM (not by parsing output), so the model copies the number
# instead of counting the listing (LLMs miscount). ──
def _fmt_count(n_dirs: int, n_files: int) -> str:
    """`N directories, M files` with correct singular/plural."""
    return f"{n_dirs} director{'y' if n_dirs == 1 else 'ies'}, {n_files} file{'' if n_files == 1 else 's'}"


def _directory_entry_names(path) -> "Optional[tuple]":
    """The (dir_names, file_names) of a directory (same hidden-entry policy as list_directory) —
    None if the path is not a readable directory. ONE O(n) pass, one snapshot."""
    try:
        p = Path(path)
        if not p.is_dir():
            return None
        items = list(p.iterdir())
    except OSError:
        return None
    dirs, files = [], []
    for i in items:
        (dirs if i.is_dir() else files).append(i.name)
    return dirs, files


def _directory_count_header(path) -> "Optional[str]":
    """Deterministic `N directories, M files` for a directory — None if not a readable directory."""
    names = _directory_entry_names(path)
    return None if names is None else _fmt_count(len(names[0]), len(names[1]))


def _safe_listing_name(n: str) -> str:
    """A name rendered into the copied-verbatim Answer sentence: a backtick would corrupt the inline-code
    spans, and any line/paragraph separator could FORGE extra lines in the reply the model copies —
    prompt injection via filename. Each such char renders as '?' (visibly altered, never executed). The
    class matches Python str.splitlines() line boundaries (C0 controls + DEL + NEL U+0085 + LS/PS
    U+2028/U+2029) plus the backtick, so no name can introduce a second line or a stray code span."""
    return re.sub("[`\x00-\x1f\x7f\x85\u2028\u2029]", "?", n)


def _listing_answer_sentence(dir_names, file_names, lang: str) -> str:
    """#1202: the COMPLETE, final listing reply, built from the filesystem in the reply language (en/de;
    anything else falls back to English). The model copies this sentence verbatim instead of composing a
    summary — composition drops names and breaks the one-sentence format, copying is reliable. Every name
    is sanitized and backtick-wrapped so the markdown clients render it as coloured inline code —
    deterministically, not only when the model happens to add the backticks itself."""
    d = sorted(dir_names, key=str.lower)
    f = sorted(file_names, key=str.lower)

    def _part(names, one, many):
        label = f"{len(names)} {one if len(names) == 1 else many}"
        return f"{label} ({', '.join(f'`{_safe_listing_name(n)}`' for n in names)})" if names else label

    if (lang or "en").lower().startswith("de"):
        return (f"Das Verzeichnis enthält {_part(d, 'Verzeichnis', 'Verzeichnisse')} "
                f"und {_part(f, 'Datei', 'Dateien')}.")
    return (f"The directory contains {_part(d, 'directory', 'directories')} "
            f"and {_part(f, 'file', 'files')}.")


_LISTING_VERBS = {"ls", "dir", "get-childitem", "gci"}


def _listing_count_header_for_command(command: str) -> "Optional[str]":
    """The deterministic count header for a SIMPLE listing command — see _listing_target_for_command."""
    target = _listing_target_for_command(command)
    return None if target is None else _directory_count_header(target)


_LISTING_HEADER_RE = re.compile(r"\d+ director(?:y|ies), \d+ files?")


def _localize_listing_answer(text: str, command: str = "") -> str:
    """#1202: render the machine ``AnswerData: {json}`` line a listing result carries into the final,
    localized ``Answer:`` sentence — SERVER-side, so ONE authoritative ``LANGUAGE`` governs every topology
    (bridged clients ship data, never templates or languages). **Command-gated**: only the result of a
    genuine listing command is transformed, so arbitrary output (e.g. ``cat file`` whose first lines
    coincidentally or maliciously match the shape) is NEVER rewritten. Additionally anchored to line 2
    directly under a line-1 count header. Malformed data drops the machine line (never leaves it, and
    never fabricates)."""
    if _listing_target_for_command(command) is None:
        return text
    lines = (text or "").split("\n", 2)
    if len(lines) < 2 or not lines[1].startswith("AnswerData: ") or not _LISTING_HEADER_RE.fullmatch(lines[0]):
        return text
    rest = lines[2:]   # the raw command body (line 3+), preserved verbatim
    try:
        data = json.loads(lines[1][len("AnswerData: "):])
        dirs, files = data["dirs"], data["files"]
        if not isinstance(dirs, list) or not isinstance(files, list):   # no str/scalar → char-splitting
            raise ValueError("dirs/files must be lists")
        dirs = [str(n) for n in dirs]
        files = [str(n) for n in files]
    except Exception:   # noqa: BLE001 — malformed data must never break a tool result: drop the machine line
        return "\n".join([lines[0]] + rest)
    if len(dirs) + len(files) > LIST_DIR_HARD_CAP:
        # the CONFIGURED cap governs at the render site too (a bridged client only knows the default
        # transport bound) — over the cap the large-folder prompt rule applies, no Answer line
        return "\n".join([lines[0]] + rest)
    return "\n".join([lines[0], f"Answer: {_listing_answer_sentence(dirs, files, LANGUAGE)}"] + rest)


def _listing_target_for_command(command: str) -> "Optional[str]":
    """If *command* is a SIMPLE directory listing (`ls`/`dir`/`Get-ChildItem`, optionally one leading
    `cd <path> &&`), return its resolved target directory path. None (no guess) for anything ambiguous:
    pipes, redirects, subshells, globs, `-R`/recursive, or more than one path operand."""
    cmd = (command or "").strip()
    if not cmd or "||" in cmd or any(t in cmd for t in ("|", ">", "<", "$(", "`", ";", "\n")):
        return None
    parts = cmd.split("&&")
    if len(parts) > 2:
        return None
    base = _exec_cwd() or os.getcwd()
    if len(parts) == 2:
        try:
            cd_tok = shlex.split(parts[0].strip(), posix=True)
        except ValueError:
            return None
        if len(cd_tok) != 2 or cd_tok[0] != "cd":
            return None
        base = cd_tok[1] if os.path.isabs(cd_tok[1]) else os.path.join(base, cd_tok[1])
    list_part = parts[-1].strip()
    if "&" in list_part:  # a stray background '&'
        return None
    try:
        tok = shlex.split(list_part, posix=True)
    except ValueError:
        return None
    if not tok or tok[0].lower() not in _LISTING_VERBS:
        return None
    # PowerShell cmdlets are case-INSENSITIVE and take VALUE-bearing named params (-Filter *.txt,
    # -Exclude x) whose value would be misread as the path operand; we can't tell a switch from a
    # value-taker without the cmdlet model, so a PS-style listing with ANY named parameter is
    # ambiguous → no header (no guess). `ls`/`dir` (POSIX/coreutils) keep clustered short flags.
    ps_style = tok[0].lower() in ("get-childitem", "gci")
    for t in tok[1:]:
        if not t.startswith("-"):
            continue
        low = t.lower()
        if low in ("--recursive", "-recurse", "-r") or (not t.startswith("--") and "R" in t):
            return None  # recursive (any case) — the header would no longer describe ONE directory
        if ps_style:
            return None  # a named parameter on a PS cmdlet takes a value → ambiguous target
    operands = [t for t in tok[1:] if not t.startswith("-")]
    if len(operands) > 1:
        return None
    target = operands[0] if operands else "."
    if any(ch in target for ch in "*?[]{}<>|;$`"):
        return None
    if not os.path.isabs(target):
        target = os.path.join(base, target)
    return target


_LANG_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
}


# MEM-20: map a file extension to a fenced-code language tag, so `/cat` output renders as preserved,
# syntax-highlighted code in markdown clients instead of being reflowed as prose. Unknown → "".
_EXT_LANG = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "tsx",
    ".js": "javascript", ".jsx": "jsx", ".mjs": "javascript", ".cjs": "javascript",
    ".json": "json", ".md": "markdown", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".ini": "ini",
    ".cfg": "ini", ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".css": "css", ".scss": "scss", ".html": "html", ".xml": "xml", ".sql": "sql",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".lua": "lua", ".r": "r",
    ".dockerfile": "dockerfile", ".tf": "hcl", ".proto": "proto", ".graphql": "graphql",
    ".vue": "vue", ".svelte": "svelte", ".dart": "dart", ".scala": "scala",
}


def _lang_for_path(path: str) -> str:
    """Fenced-code language for a path's extension (MEM-20); "" when unknown."""
    base = os.path.basename(path).lower()
    if base in ("dockerfile", "makefile"):
        return "dockerfile" if base == "dockerfile" else "makefile"
    return _EXT_LANG.get(os.path.splitext(base)[1], "")


def _language_guidance(lang: str) -> str:
    """Runtime note that pins the orchestrator's reply language. Deterministic: the
    configured language wins regardless of the input language. ``en`` is the OSS
    default; set ``GX10_LANGUAGE`` (or generation.language) to override."""
    code = (lang or "en").strip().lower()
    name = _LANG_NAMES.get(code, lang)
    return ("## Response language\n"
            f"Always respond to the user in {name}, regardless of the language of the "
            "input, tools, or context. Keep code identifiers and file paths unchanged.")


def _platform_guidance(platform: str) -> str:
    """Dynamically injected runtime note — keeps the prompt file neutral."""
    if platform == "windows":
        return (
            "## Runtime environment\n"
            "Operating system: **Windows**. The model `execute_command` shell tool is **unavailable** here: "
            "command isolation is mandatory and no supported sandbox backend (bwrap/firejail) exists on Windows, "
            "so every `execute_command` call fails closed with a refusal. Use the `list_directory` tool for "
            "directory listings, and `read_file`/`write_file`/`edit_file`/`search_files` for file work — those are "
            "unaffected. A direct, unrestricted shell is operator-only via `/sh` and is not a model tool."
        )
    return (
        "## Runtime environment\n"
        "Operating system: **Linux**. For `execute_command` use POSIX/bash syntax (e.g. `date`, `ls`, `cat`, "
        "`grep`). Model command execution runs inside a mandatory sandbox (bwrap/firejail); on a host without a "
        "supported backend `execute_command` fails closed with a refusal, so fall back to the `list_directory` "
        "tool for listings."
    )


# #459 (epic #440 P6, §4): current-info intent classifier + execute_command shell guardrail. Three
# parts jointly fix the verified scaling-break bug (#447): the model asked for current info improvised a
# PowerShell Invoke-WebRequest, whose progress bar drew into the renderer-owned conhost and corrupted it.
_CURRENT_INFO_KW = (
    # Bilingual EN+DE: these match the USER's input language, not code — a documented exception to the
    # english-only rule (epic #505 R1: keep the DE markers; this is deliberate language-aware INPUT
    # matching, like the language=de output exemption, NOT a German identifier/string in the codebase).
    # EN — explicit recency markers (conservative; a hint, not a hard route). Deliberately NOT bare
    # "current"/"currently" — those are everywhere in coding context ("current branch/directory/value")
    # and would mis-steer (review A S3). Require an unambiguous time/news marker.
    "latest", "today", "right now", "as of now", "breaking news", "this week", "recent news",
    "real-time", "realtime", "up to date", "up-to-date", "live news", "current news", "current events",
    # DE — likewise NOT bare "aktuell*" (review A: "der aktuelle Branch/Wert/Stand" is everywhere in
    # coding context, the same trap as bare EN "current"); require an unambiguous recency phrase/marker.
    "aktuelle lage", "aktuelle nachrichten", "aktuelle entwicklungen", "neueste", "neuesten", "heute",
    "in echtzeit", "gerade eben", "letzte woche",
    # S12: news/headline markers surfaced by operator testing ("aktuelle meldungen zum …").
    "aktuelle meldungen", "schlagzeilen", "headlines",
)

def _is_current_info_query(text: str) -> bool:
    """True when the user asks for CURRENT / real-time information (#459) — used to PROACTIVELY steer the
    model toward web_search instead of a shell web fetch. Pure EN+DE keyword heuristic; conservative
    (matches explicit recency markers, not every question). Only a hint — the model still decides."""
    t = (text or "").lower()
    return any(k in t for k in _CURRENT_INFO_KW)


def _web_search_trust_ok() -> bool:
    """Trust gate for web_search (epic #505 S7): outbound web search is BLOCKED under the ``sealed``
    (sovereign/loopback) trust profile unless the operator opts in via ``security.web_in_sealed``;
    ``open``/``token`` allow it. Never raises. Without this gate a sealed deployment would still
    egress web search — the dispatcher pins Sensitivity.PUBLIC and the sovereignty filter only forces
    local for SENSITIVE/LOCAL_ONLY routes."""
    if not _is_sealed_profile():
        return True
    return bool(((_EFFECTIVE_CFG or {}).get("security") or {}).get("web_in_sealed", False))


def _web_search_available() -> bool:
    """Central capability check for web_search (epic #505) — the adapter-aware replacement for the
    dispatcher-only ``has_web_provider`` gate. True iff a usable search adapter is wired (cli → a
    web-capable CLI provider; mock → always; brave → key present) AND the trust profile permits it
    (S7). Never raises."""
    ws = _WEBSEARCH
    try:
        return ws is not None and bool(ws.available()) and _web_search_trust_ok()
    except Exception:  # noqa: BLE001 — a flaky availability probe must never break the turn
        return False


def _websearch_steer(user_input: str) -> str:
    """The per-turn proactive nudge (#459): a one-line hint steering a CURRENT-info request to web_search,
    but ONLY when a search adapter is actually available (else the hint would point at a missing tool).
    "" otherwise. Pure over the input + the live adapter state."""
    if _is_current_info_query(user_input) and _web_search_available():
        return ("[note: this looks like a request for CURRENT / real-time information — use the "
                "`web_search` tool for it, not `execute_command`.]")
    return ""


def _steering_state_block() -> str:
    """#1225 (S3): the per-turn AUTHORITATIVE steering state — active project · unit · lifecycle stage ·
    N pending/M in_progress · watcher/autopilot — read from the SAME globals the plumbing acts on. The caller
    keeps EXACTLY ONE current copy (dropping stale ones), placed after the stable system prefix (KV-cache-safe),
    so the model never has to GUESS its state (the #1225 bug). Returns "" when nothing is bound (no project
    AND no unit) → a plain-chat/unisolated turn stays byte-identical. Compact, secret-free; effectively
    read-only (may lazily init the shared task-store singleton); NEVER raises (like registry_health)."""
    try:
        health  = registry_health()                       # {status, active_project, home} — never raises
        project = health.get("active_project")
        unit    = active_slug()                            # one small file read, fail-soft → None
        if not project and not unit:
            return ""                                      # nothing bound → byte-identical plain-chat turn
        stage = ""
        if unit:
            try:                                           # cheap cached projection — never re-scan the vault
                gp = vault_root() / unit / GRAPH_FILENAME
                if gp.is_file():
                    stage = (json.loads(gp.read_text(encoding="utf-8"))
                             .get("lifecycle", {}).get("current") or "")
            except Exception:  # noqa: BLE001 — advisory; omit the stage on any hiccup
                stage = ""
        try:
            store = _store()                               # the same singleton the plumbing uses
            n_pending = len(store.list("pending"))          # best-effort snapshot ([] when no unit; per-file errors swallowed)
            n_prog    = len(store.list("in_progress"))
        except Exception:  # noqa: BLE001
            n_pending = n_prog = 0
        lines = [
            _STEERING_MARKER,
            f"- active project: {project or '(none)'}"
            + ("  [engine running un-isolated]" if health.get("status") == "unisolated" else ""),
            f"- active unit (initiative): {unit or '(none — no vault/<slug> unit is active)'}",
        ]
        if stage:
            lines.append(f"- lifecycle stage: {stage}")
        if unit:
            try:
                _hd, _ap, _rel = _unit_design_status(unit)
            except DesignMigrationRefusal as ex:
                lines.append(f"- design gate: BLOCKED — {ex}")
                _hd = _ap = False
                _rel = None
                design_migration_blocked = True
            else:
                design_migration_blocked = False
            if design_migration_blocked:
                pass
            elif not _hd:
                lines.append("- design gate: no design on record — implementation handovers are BLOCKED "
                             "— if you have just researched/analysed a design, CALL record_design NOW to "
                             "persist it (a prose proposal is not enough); then wait for /approve.")
                lines.append("- design options: when the design space is genuinely open, recommend that "
                             "the operator run `/design --options [N]`; never fan out automatically.")
            elif not _ap:
                # ADR-0006 D5 (#1416 / S3): with >1 recorded proposal variants, promotion needs an explicit
                # id. The approved design may carry a `## Build policy` section honoured at build.
                _props = _design_proposals(unit)
                _pick = (f" — {len(_props)} proposal variants recorded ({', '.join(p.stem for p in _props)}); "
                         f"`/approve design <id>` to promote one" if len(_props) > 1 else "")
                lines.append(f"- design gate: design recorded ({_display_doc_path(_rel)}) but NOT approved — implementation "
                             f"handovers BLOCKED until /approve{_pick}. (An approved design may carry a "
                             f"`## Build policy` section for dep/egress guidance.)")
            else:
                lines.append("- design gate: design approved — implementation handovers allowed.")
        lines.append(f"- tasks: {n_pending} pending · {n_prog} in_progress")
        # #1296: the select-unit recommendation — the SAME deterministic policy the continuation
        # uses, surfaced per turn so guided mode (auto off) can drive the loop by hand.
        try:
            _unit, _elig, _n_open = _select_next_unit(store)
            if _unit is not None:
                _pp = str(_unit.get("parent") or "").strip()
                lines.append(f"- next open unit: {_unit['id']} ({str(_unit.get('title') or '')!r})"
                             + (f" under epic {_pp}" if _pp else "")
                             + " — stage its handover via stage_handover (task_id, no task_json); "
                               "/auto on drains all open units automatically.")
            elif _n_open > 0:
                lines.append(f"- next open unit: NONE selectable — {_n_open} open unit(s) blocked or "
                             f"dependency-gated (see /board).")
        except Exception:  # noqa: BLE001 — the recommendation is advisory, never break the turn
            pass
        _full_auto = _WATCHER_ENABLED and AUTOPILOT_ENABLED and AUTOPILOT_AUTOPLAN
        _no_auto   = not (_WATCHER_ENABLED or AUTOPILOT_ENABLED or AUTOPILOT_AUTOPLAN)
        lines.append(f"- watcher: {'on' if _WATCHER_ENABLED else 'off'} · "
                     f"autopilot: {'on' if AUTOPILOT_ENABLED else 'off'} · "
                     f"continuation: {'on' if AUTOPILOT_AUTOPLAN else 'off'}"
                     + ("  [auto: FULL]" if _full_auto else "  [auto: GUIDED]" if _no_auto else "  [auto: MIXED]"))
        lines.append("Trust these fields over any filesystem probe; do NOT invent a vault path.")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — a per-turn hint must never break a turn
        return ""


def _parse_design_options_args(raw: str) -> "tuple[Optional[int], Optional[str]]":
    """Parse ``/design --options [N]``. Deterministic and intentionally narrow: no model-judged
    openness, no implicit fan-out from prose."""
    parts = (raw or "").split()
    if not parts:
        return (None, "usage: /design --options [N]")
    if parts[0] != "--options":
        return (None, "usage: /design --options [N]")
    if len(parts) > 2:
        return (None, "usage: /design --options [N]")
    if len(parts) == 1:
        return (2, None)
    try:
        n = int(parts[1])
    except ValueError:
        return (None, f"ERROR: invalid design option count {parts[1]!r}. Use /design --options [N].")
    if n < 2:
        return (None, "ERROR: /design --options requires N >= 2.")
    if n > 8:
        return (None, "ERROR: /design --options caps N at 8.")
    return (n, None)


def _design_options_prompt(n: int) -> str:
    return (
        f"Generate {n} distinct design proposal variants for the active unit.\n\n"
        "Requirements:\n"
        f"- Call the `record_design` tool exactly {n} times, once per proposal variant.\n"
        "- Each proposal body MUST include a `## Trade-offs` section.\n"
        "- Under `## Trade-offs`, include explicit `Pros` and `Cons` subsections or bullet labels.\n"
        "- Keep the variants comparable: each proposal should name the approach, why it fits, core "
        "architecture, and build-time standards/policy if relevant.\n"
        "- Do not choose for the operator. After recording the variants, stop and tell the operator to run "
        "`/approve design <id>` for the proposal they pick.\n"
    )


def _design_command(agent: "Optional[GX10]", raw: str) -> str:
    """Operator-triggered S5 design-proposal fan-out."""
    n, err = _parse_design_options_args(raw)
    if err:
        return err
    if not active_slug():
        return "ERROR: /design --options needs an active unit. Create/select one with /project first."
    if agent is None:
        return "ERROR: /design --options needs a running orchestrator agent."
    proposals_dir = vault_root() / active_slug() / "proposals"
    before = {p.name for p in proposals_dir.glob("design-*.md")} if proposals_dir.exists() else set()
    agent.run(_design_options_prompt(n or 2))
    after = {p.name for p in proposals_dir.glob("design-*.md")} if proposals_dir.exists() else set()
    recorded = len(after - before)
    if recorded < (n or 2):
        return (
            f"WARN: recorded only {recorded} of {n or 2} requested design variants under proposals/. "
            "Check proposals/ and re-run /design --options if needed."
        )
    return (
        f"OK: recorded {recorded} of {n or 2} design proposal variants under proposals/. "
        "Pick one with /approve design <id>."
    )

# Fail-closed deny-list for execute_command (§4 / operator 2026-06-25): a shell command that fetches the
# web / a remote host (that is what web_search is for, and a PowerShell web fetch draws a progress bar into
# the renderer-owned console — the verified scaling break) or that runs unbounded / streams progress
# (sleep loops, follow/tail, watchers, schedulers) is REFUSED before it runs.
_SHELL_DENY = (
    # unambiguous web/remote APIs — match anywhere (these tokens have no innocuous use)
    (re.compile(r"(?i)\b(invoke-webrequest|invoke-restmethod|start-bitstransfer|net\.webclient|"
                r"downloadstring|downloadfile|system\.net\.(http|webclient))\b"),
     "a remote/web fetch"),
    # bare fetch commands — only at a COMMAND position (start / after a separator), so a filename or a
    # search string that merely CONTAINS "curl"/"wget" (e.g. `Select-String 'wget'`, `Get-Content
    # curl.txt`) is not wrongly blocked (review A S3). The model emits the fetch in command position.
    (re.compile(r"(?i)(?:^|[\n;|&({`])\s*(curl|wget|iwr|irm)\b"),
     "a remote/web fetch"),
    # long-running / progress-emitting processes
    (re.compile(r"(?i)(\bstart-sleep\b|while\s*\(\s*\$?true\s*\)|for\s*\(\s*;\s*;|\bping\s+-t\b|"
                r"\bping\s+-n\s*\d{3,}|\bstart-job\b|\bstart-process\b|\bregister-scheduledtask\b|"
                r"\bget-content\b[^\n|]*\s-wait\b|\btail\s+-f\b|\bwatch-\w|\btcpdump\b|\bhtop\b)"),
     "a long-running / progress-emitting process"),
)

def _shell_guard(command: str) -> Optional[str]:
    """Refuse (fail-closed) a shell command that fetches the web/a remote host or runs unbounded — the
    verified scaling-break guardrail (#459). Returns the block reason (a short phrase) to refuse with, or
    None when the command is allowed. Pure + side-effect-free.

    Scope: this guards the MODEL's own improvisation (and is paired with the PowerShell $ProgressPreference
    hardening that is the actual scaling-break fix), NOT an adversarial sandbox. Nested shell wrappers
    (``pwsh -Command iwr …``, ``cmd /c curl …``) and ``-EncodedCommand`` payloads are NOT decoded — the
    model emits the direct forms (which ARE caught); a deliberately-obfuscated fetch is out of scope."""
    cmd = command or ""
    for pat, why in _SHELL_DENY:
        if pat.search(cmd):
            return why
    return None


def _onboarding_guidance() -> str:
    """Injected into the context only in onboarding mode."""
    return (
        "## Onboarding mode (active)\n"
        "Before EVERY `stage_handover` for a NEW task, first call "
        "`check_task_exists(title=…, description=…)`. If it returns "
        "`EXISTS: KGC-XXX`, do NOT generate a handover — name the existing task. "
        "Only on `NONE` write the handover and call `stage_handover`. This avoids "
        "expensive generation for duplicates."
    )


# ─── Tool execution ──────────────────────────────────────────
def _tool_param_schema(name: str) -> Optional[Dict[str, Any]]:
    """The JSON-schema ``parameters`` block for a tool by name (or None if unknown)."""
    for t in _effective_tools():
        fn = t.get("function") or {}
        if fn.get("name") == name:
            return fn.get("parameters") or {}
    return None


_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict,
}


def _validate_tool_args(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Optional[str]:
    """Lightweight schema check against a tool's ``parameters``: required presence +
    top-level types. Returns an error string or None. Deliberately lenient (no deep
    nesting) — enough to drive a reask, not a full JSON-Schema validator."""
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties") or {}
    missing = [r for r in (schema.get("required") or []) if r not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    for k, v in args.items():
        spec = props.get(k)
        if not isinstance(spec, dict):
            continue
        t = spec.get("type")
        py = _JSON_TYPES.get(t)
        # bool is a subclass of int → reject it where a number/integer is expected.
        if t in ("integer", "number") and isinstance(v, bool):
            return f"argument '{k}' must be {t}, got boolean"
        if py and not isinstance(v, py):
            return f"argument '{k}' must be {t}, got {type(v).__name__}"
    return None


def _parse_tool_args(name: str, raw: Optional[str]) -> "tuple[Optional[Dict[str, Any]], Optional[str]]":
    """Parse a tool call's raw arguments and validate against its schema. Returns
    ``(args, None)`` on success, or ``(None, error)`` — the error is fed back as the tool
    result so the model RE-EMITS the call instead of us silently swallowing it (the
    Validate→Reask contract, applied at the tool boundary, not just at stage_handover)."""
    try:
        args = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        return None, (f"malformed JSON arguments for '{name}': {e}. "
                      f"Re-emit the tool call with valid JSON.")
    if not isinstance(args, dict):
        return None, f"arguments for '{name}' must be a JSON object, got {type(args).__name__}."
    schema_err = _validate_tool_args(args, _tool_param_schema(name))
    if schema_err:
        return None, f"invalid arguments for '{name}': {schema_err}. Re-emit with corrected arguments."
    return args, None


def _valid_tool_args_json(raw: Optional[str]) -> str:
    """The ``arguments`` stored on an assistant ``tool_call`` in the history MUST be valid JSON. On the NEXT
    request vLLM's tool-call rendering ``json.loads()`` them, so a model's MALFORMED arguments string (a
    small model emitting a huge escaped ``content`` for ``write_file`` gets it wrong) would hard-400 the
    reask (``Expecting ',' delimiter``) and DEFEAT Validate→Reask — the turn dies before the model can
    re-emit. Keep a parseable string; replace an unparseable one with ``{}``. The parse error is still fed
    back as the tool result (see ``_parse_tool_args``), so the model re-emits — but the request always
    renders. Never raises, idempotent."""
    raw = raw if (isinstance(raw, str) and raw) else "{}"
    try:
        json.loads(raw)
        return raw
    except (json.JSONDecodeError, ValueError):
        return "{}"


_TOOLCALL_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
_FENCED_JSON_RE = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.DOTALL)


def _coerce_extracted(obj: Any, tool_names: set) -> Optional[Dict[str, Any]]:
    """A parsed JSON object → a native-shaped tool_call dict, but ONLY if it names a known
    tool (so a legitimate JSON answer is never mistaken for a call). ``arguments`` may be
    given as ``arguments`` or ``parameters``; normalised to a JSON string."""
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or (obj.get("function") or {}).get("name")
    if not name or name not in tool_names:
        return None
    raw_args = obj.get("arguments")
    if raw_args is None:
        raw_args = obj.get("parameters")
    if raw_args is None and isinstance(obj.get("function"), dict):
        raw_args = obj["function"].get("arguments")
    if isinstance(raw_args, str):
        arguments = raw_args
    elif raw_args is None:
        arguments = "{}"
    else:
        arguments = json.dumps(raw_args, ensure_ascii=False)
    return {"id": "", "name": name, "arguments": arguments}


def _extract_tool_calls_from_text(content: str, tool_names: set) -> List[Dict[str, Any]]:
    """Recover tool calls a model emitted as TEXT (no native ``tool_calls``) — the model-
    agnostic fallback for OpenAI-compatible endpoints without a server-side tool parser.

    Recognised, in priority order (first that matches wins, to avoid double extraction):
      1. ``<tool_call>{…}</tool_call>`` blocks (Qwen/Nous style)
      2. fenced ```json/```tool_call blocks containing a call object

    **Only EXPLICIT markers count.** A bare top-level JSON object is deliberately NOT
    treated as a call: a legitimate answer that happens to be JSON (e.g. the model was
    asked to emit a tool *spec* or a data record with a ``name``/``arguments`` shape)
    must never be silently re-interpreted into a destructive tool call. A model invoking
    a tool without native support signals it with the tag/fence; data does not.
    Each candidate is additionally gated to a known tool name. Returns [] otherwise."""
    if not content or not tool_names:
        return []
    for pat in (_TOOLCALL_TAG_RE, _FENCED_JSON_RE):
        found: List[Dict[str, Any]] = []
        for m in pat.finditer(content):
            try:
                obj = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            call = _coerce_extracted(obj, tool_names)
            if call:
                found.append(call)
        if found:
            return found
    return []


#: Fixed core location for **built-in** skills/prompts — always scanned at startup, independent
#: of ``GX10_PLUGINS_DIR`` (which stays the additive surface for 3rd-party skills). ADR-0002 #114.
_BUILTIN_DIR = Path(__file__).resolve().parents[1] / "skills"   # skills/


def _discover_tools_into(root: str, *, forbidden_caps: "set | None" = None,
                         record_caps: "set | None" = None, into: "dict | None" = None,
                         skip_scaffolds: bool = False) -> int:
    """Discover typed ``CASE``+``run`` skills under *root* and ADD them to *into* (default ``_PLUGIN_TOOLS``).
    No clear — additive. Returns how many this root contributed. Fail-soft.

    ``record_caps`` (when given) accumulates the capabilities this root added; ``forbidden_caps`` (when
    given) skips a tool whose capability is already in it — the cross-root CAPABILITY guard the project
    library is loaded under (S11 / #630). ``into`` lets the build-then-swap loader (S11a-2) accumulate into a
    FRESH dict before swapping it live. ``skip_scaffolds`` (S11b-3a, the project-library root only) drops an
    UNFILLED scaffold — a generated stub still carrying the ``ACK-SCAFFOLD-SENTINEL`` marker is never offered
    as a real tool (enforces the generation gate cheaply at load; the full hermetic check is the
    ``ack.gate.library_items_complete`` invariant — operator / S17 acceptance, not auto-scheduled). All
    default None/False ⇒ byte-identical to the pre-S11 loader."""
    target = into if into is not None else _PLUGIN_TOOLS
    try:
        from ack.registry import Registry, derive_tool_schema
    except Exception as e:  # noqa: BLE001 — no registry → no tools, never fatal
        _ui_print(col(f"  [skills] registry unavailable: {e!r}", C.YELLOW))
        return 0
    _scaffold_check = None
    if skip_scaffolds:
        try:
            from ack.gate import has_scaffold_sentinel as _scaffold_check
        except Exception:  # noqa: BLE001 — no gate → no scaffold filter (never blocks loading)
            _scaffold_check = None
    try:
        added = Registry().discover_skills(root)
    except Exception as e:  # noqa: BLE001
        _ui_print(col(f"  [skills] tool discovery failed in {root!r}: {e!r}", C.YELLOW))
        return 0
    n = 0
    for r in added:
        if r.handler is None:
            continue
        name = str(r.name)
        cap = str(getattr(r, "capability", "") or "")
        src = str(getattr(r, "source", "") or "")
        # ROUTE-4 (#503): a plugin/skill tool named like a BUILT-IN tool is shadowed by run_tool's built-in
        # dispatch → it would be registered + offered to the model but NEVER callable (silently). Reject it.
        if name in _all_tool_names(include_plugins=False):
            _ui_print(col(f"  [skills] tool {name!r} collides with a built-in tool — skipped (undispatchable)", C.YELLOW))
            continue
        if _scaffold_check is not None and src and _scaffold_check(src):
            _ui_print(col(f"  [skills] {name!r} is an unfilled scaffold — not offered (#630; implement it)", C.YELLOW))
            continue
        # S11 (#630): cross-root CAPABILITY guard for a later root (the project library) — a fresh Registry
        # per root only dedups capability WITHIN a root, and the name check below is per-name; without this a
        # project-library tool with a built-in's capability but a different name would load as an extra tool.
        # forbidden_caps holds the capabilities already loaded from the earlier (global) roots.
        if forbidden_caps and cap and cap in forbidden_caps:
            _ui_print(col(f"  [skills] tool {name!r} shadows built-in/plugin capability {cap!r} — skipped", C.YELLOW))
            continue
        if name in target:               # tool names must be unique — otherwise silent shadowing
            _ui_print(col(f"  [skills] duplicate tool name {name!r} — first kept, rest skipped", C.YELLOW))
            continue
        if record_caps is not None and cap:
            record_caps.add(cap)
        target[name] = {
            "schema": {"type": "function", "function": {
                "name": name,
                "description": str(r.description or f"skill {name}"),
                "parameters": derive_tool_schema(r.handler),
            }},
            "handler": r.handler,
        }
        n += 1
    return n


def _load_plugins(plugins_dir: Optional[str]) -> int:
    """Load typed-tool skills from a single dir (clears first). Back-compat single-dir entry;
    startup uses :func:`_load_skills` (built-ins always + plugins additive)."""
    _PLUGIN_TOOLS.clear()
    if not plugins_dir:
        return 0
    _discover_tools_into(plugins_dir)
    if _PLUGIN_TOOLS:
        _ui_print(col(f"  [plugins] {len(_PLUGIN_TOOLS)} tool(s) from {plugins_dir}: "
                      f"{', '.join(sorted(_PLUGIN_TOOLS))}", C.GRAY))
    return len(_PLUGIN_TOOLS)


#: The single tool that exposes playbook skills with progressive disclosure: no/empty
#: capability → the metadata catalogue; capability → that playbook's body; capability+reference
#: → one reference doc. Offered only when playbooks are discovered (see _effective_tools).
USE_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "use_skill",
        "description": ("Access a playbook skill (SKILL.md). Call with no capability to LIST "
                        "available skills (metadata only); with a capability to load that "
                        "skill's instructions; add a reference name to load one reference doc. "
                        "Progressive disclosure — list first, then load only what you need."),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "description": "Skill capability id (omit to list all)."},
                "reference": {"type": "string", "description": "Optional reference doc name to load."},
            },
        },
    },
}


def _discover_playbooks_into(root: str, *, into: "dict | None" = None) -> int:
    """Discover ``SKILL.md`` playbooks under *root* and ADD them to *into* (default _PLAYBOOKS; no clear —
    additive; first capability wins). ``into`` lets the build-then-swap loader (S11a-2) accumulate into a
    fresh dict. Returns how many this root contributed. Fail-soft."""
    target = into if into is not None else _PLAYBOOKS
    try:
        from ack.registry import Registry
        found = Registry.discover_playbooks(root)
    except Exception as e:  # noqa: BLE001 — no registry/discovery → no playbooks, never fatal
        _ui_print(col(f"  [skills] playbook discovery failed in {root!r}: {e!r}", C.YELLOW))
        return 0
    n = 0
    for pb in found:
        if pb.capability in target:
            continue
        target[pb.capability] = pb
        n += 1
    return n


def _load_playbooks(plugins_dir: Optional[str]) -> int:
    """Load playbook skills from a single dir (clears first). Back-compat single-dir entry;
    startup uses :func:`_load_skills`."""
    _PLAYBOOKS.clear()
    if not plugins_dir:
        return 0
    _discover_playbooks_into(plugins_dir)
    if _PLAYBOOKS:
        _ui_print(col(f"  [skills] {len(_PLAYBOOKS)} playbook(s) from {plugins_dir}: "
                      f"{', '.join(sorted(_PLAYBOOKS))}", C.GRAY))
    return len(_PLAYBOOKS)


def _discover_prompts_into(root: str, *, into: "dict | None" = None) -> int:
    """Discover ``kind: prompt`` items under *root* and ADD them to *into* (default _PROMPTS; no clear —
    additive; first capability wins). ``into`` lets the build-then-swap loader (S11a-2) accumulate into a
    fresh dict. Returns how many this root contributed. Fail-soft."""
    target = into if into is not None else _PROMPTS
    try:
        from ack.prompt import discover_prompts
        found = discover_prompts(root)
    except Exception as e:  # noqa: BLE001 — no discovery → no prompts, never fatal
        _ui_print(col(f"  [skills] prompt discovery failed in {root!r}: {e!r}", C.YELLOW))
        return 0
    n = 0
    for p in found:
        if p.capability in target:
            continue
        target[p.capability] = p
        n += 1
    return n


#: Entry-point group a *packaged* plugin advertises (ADR-0004 #136). A pip-installed plugin
#: (3rd-party or internal) is discovered by this group name alone — dependency inversion: the
#: engine never imports a concrete plugin, and the only coupling is this generic string.
_PLUGIN_ENTRYPOINT_GROUP = "ironclad.plugins"


def _iter_plugin_entry_points() -> list[Any]:
    """The entry points in the ``ironclad.plugins`` group (fail-soft; isolated for testing)."""
    try:
        from importlib.metadata import entry_points
    except Exception:  # noqa: BLE001 — no metadata API → no entry-point plugins
        return []
    try:
        return list(entry_points(group=_PLUGIN_ENTRYPOINT_GROUP))   # selectable API (py3.10+)
    except TypeError:  # pragma: no cover — very old API shape
        return list(entry_points().get(_PLUGIN_ENTRYPOINT_GROUP, []))


def _resolve_plugin_root(obj: Any) -> Optional[str]:
    """Resolve an entry-point target to a discoverable plugins **root dir**. Accepts a dir path
    (str/Path), a package/module (its dir), or a zero-arg callable returning one of those."""
    if callable(obj) and not hasattr(obj, "__path__") and not hasattr(obj, "__file__"):
        try:
            obj = obj()
        except Exception:  # noqa: BLE001
            return None
    if isinstance(obj, (str, Path)):
        p = Path(obj)
        return str(p) if p.is_dir() else None
    path = getattr(obj, "__path__", None)        # a package
    if path:
        return next(iter(path), None)
    f = getattr(obj, "__file__", None)           # a module
    if f:
        return str(Path(f).parent)
    return None


def _entrypoint_plugin_roots() -> list[str]:
    """Discover plugin root dirs advertised via the ``ironclad.plugins`` entry-point group.
    Each entry point is loaded + resolved independently; a bad one is skipped, never fatal."""
    roots: list[str] = []
    for ep in _iter_plugin_entry_points():
        name = getattr(ep, "name", "?")
        try:
            root = _resolve_plugin_root(ep.load())
        except Exception as e:  # noqa: BLE001 — a broken plugin entry point never aborts startup
            _ui_print(col(f"  [skills] entry-point plugin {name!r} failed to load: {e!r}", C.YELLOW))
            continue
        if root:
            roots.append(root)
        else:
            _ui_print(col(f"  [skills] entry-point plugin {name!r} resolved to no plugins dir", C.YELLOW))
    return roots


def _load_skills(plugins_dir: Optional[str] = None) -> tuple[int, int, int]:
    """Load the skill/prompt registries (ADR-0002 #114): **always** the core built-ins from ``_BUILTIN_DIR``,
    then **additively** 3rd-party/internal skills — from *plugins_dir* (a dir, dev), packaged plugins
    (``ironclad.plugins`` entry points, ADR-0004 #136), and (S11 #630) the ACTIVE project's library, LAST.

    BUILD-THEN-SWAP (S11a-2): discovery runs into FRESH dicts, then the live registries are swapped in
    (``clear()+update()`` keeps the dict OBJECTS so any held reference stays valid). A failed/slow build
    never leaves the live registries empty or half-populated — so this is safe to call on a ``/switch`` to
    reload the new project's library. Returns (n_tools, n_playbooks, n_prompts)."""
    ep_roots = _entrypoint_plugin_roots()
    # S11 (#630): the ACTIVE project's library is the LAST (additive) root — generated per-project items are
    # discovered alongside the built-ins/plugins. It comes last so a name/capability collision keeps the
    # built-in (discovery is first-kept); a generated item can't shadow a built-in anyway (the S10a generate
    # guard refuses that). Only added when the dir EXISTS, so a project with no library is byte-identical.
    _lib = _project_library_root()
    _lib_in = _lib.is_dir()
    global_roots = [str(_BUILTIN_DIR)] + ([plugins_dir] if plugins_dir else []) + ep_roots
    _global_caps: "set | None" = set() if _lib_in else None   # only track when guarding the lib root
    tools: Dict[str, Dict[str, Any]] = {}
    playbooks: Dict[str, Any] = {}
    prompts: Dict[str, Any] = {}
    for root in global_roots:
        _discover_tools_into(root, record_caps=_global_caps, into=tools)
        _discover_playbooks_into(root, into=playbooks)
        _discover_prompts_into(root, into=prompts)
    if _lib_in:
        # the project library is loaded LAST + capability-guarded against the global roots, so a generated
        # item can never displace a built-in/plugin (playbooks/prompts are already first-kept by capability).
        # S11b-3a: an UNFILLED scaffold tool (still carrying the sentinel) is dropped — never offered.
        _discover_tools_into(str(_lib), forbidden_caps=_global_caps, into=tools, skip_scaffolds=True)
        _discover_playbooks_into(str(_lib), into=playbooks)
        _discover_prompts_into(str(_lib), into=prompts)
    # swap the fully-built registries in, keeping the dict objects (held references stay valid)
    _PLUGIN_TOOLS.clear(); _PLUGIN_TOOLS.update(tools)
    _PLAYBOOKS.clear(); _PLAYBOOKS.update(playbooks)
    _PROMPTS.clear(); _PROMPTS.update(prompts)
    if _PLUGIN_TOOLS or _PLAYBOOKS or _PROMPTS:
        srcs = ("built-ins" + (f" + {plugins_dir}" if plugins_dir else "")
                + (f" + {len(ep_roots)} entry-point(s)" if ep_roots else "")
                + (" + project library" if _lib_in else ""))
        _ui_print(col(f"  [skills] {len(_PLUGIN_TOOLS)} tool(s) + {len(_PLAYBOOKS)} playbook(s) "
                      f"+ {len(_PROMPTS)} prompt(s) ({srcs})", C.GRAY))
    return len(_PLUGIN_TOOLS), len(_PLAYBOOKS), len(_PROMPTS)


def _use_skill(capability: str = "", reference: str = "") -> str:
    """Dispatch for the ``use_skill`` tool — progressive disclosure over _PLAYBOOKS."""
    if not capability:
        if not _PLAYBOOKS:
            return "No playbook skills are available."
        lines = ["Available playbook skills (call use_skill with a capability to load one):"]
        for cap in sorted(_PLAYBOOKS):
            pb = _PLAYBOOKS[cap]
            lines.append(f"- {cap}: {pb.description}")
        return "\n".join(lines)
    pb = _PLAYBOOKS.get(capability)
    if pb is None:
        return (f"ERROR: no playbook skill {capability!r}. "
                f"Call use_skill with no capability to list available skills.")
    if reference:
        try:
            return pb.reference(reference)
        except Exception as e:  # noqa: BLE001 — surfaced as a tool result, never raises
            avail = ", ".join(pb.references()) or "(none)"
            return f"ERROR: {e}. Available references: {avail}."
    refs = pb.references()
    suffix = f"\n\nReferences (load with use_skill('{capability}', '<name>')): {', '.join(refs)}" if refs else ""
    return pb.body + suffix


#: Prompt-library tool (ADR-0003 #110): list prompts → guided elicitation → multilingual assemble.
USE_PROMPT_TOOL = {
    "type": "function",
    "function": {
        "name": "use_prompt",
        "description": ("Use a prompt-library item to produce a finished prompt. Call with no "
                        "capability to LIST prompts. Call with a capability + a `values` JSON of "
                        "the variables collected so far: if a required variable is still missing, "
                        "the tool returns the next question to ASK the user; once all required "
                        "values are present it returns the assembled prompt. `lang` picks the "
                        "target language."),
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "description": "Prompt id (omit to list all)."},
                "values": {"type": "string", "description": "JSON object of variable→value collected so far."},
                "lang": {"type": "string", "description": "Target language code (e.g. en, de)."},
            },
        },
    },
}


def _use_prompt(capability: str = "", values: str = "", lang: str = "") -> str:
    """Dispatch for ``use_prompt`` — list → guided elicitation → multilingual assemble (#110)."""
    if not capability:
        if not _PROMPTS:
            return "No prompt-library items are available."
        lines = ["Available prompts (call use_prompt with a capability + values JSON):"]
        for cap in sorted(_PROMPTS):
            lines.append(f"- {cap}: {_PROMPTS[cap].description}")
        return "\n".join(lines)
    p = _PROMPTS.get(capability)
    if p is None:
        return (f"ERROR: no prompt {capability!r}. Call use_prompt with no capability to list.")
    try:
        vals = json.loads(values) if values.strip() else {}
        if not isinstance(vals, dict):
            return "ERROR: `values` must be a JSON object of variable→value."
    except ValueError as e:
        return f"ERROR: `values` is not valid JSON: {e}"
    from ack.promptgen import run_prompt
    step = run_prompt(p, {k: str(v) for k, v in vals.items()}, lang=(lang or None))
    if step["status"] == "ask":
        return (f"NEXT QUESTION (variable '{step['variable']}'): {step['question']}\n"
                f"(still needed: {', '.join(step['missing'])}) — ask the user, then call use_prompt "
                f"again with this value added to `values`.")
    return f"ASSEMBLED PROMPT ({step['lang']}):\n\n{step['prompt']}"


def _emit_agent_frames(results: List[Dict[str, Any]]) -> None:
    """#453: surface routing provenance as `[agent]` control frames (the `[perf]` pattern) so the
    client shows which coder is being called. ONE frame per DISTINCT routed provider, with its
    route reason (+ `spilled`). Emitted via `_ui_print`, so it flows through the chat stream and is
    parsed out by the client (cli.py `_stream_turn.route` / ink `stream/route.ts`) into the footer.
    Fail-soft: a malformed result never breaks the fan-out (the frame is cosmetic)."""
    try:
        seen: set = set()
        for r in results:
            if not isinstance(r, dict):
                continue
            pid = r.get("provider_id")
            if not pid or pid in seen:                   # no provenance (byte-identical fanout) or dup
                continue
            seen.add(pid)
            reason = (r.get("route_reason") or "").strip()
            tag = f"{pid} · {reason}" if reason else str(pid)
            if r.get("spilled"):
                tag += " · spilled"
            _ui_print(col(f"  [agent] {tag}", C.GRAY))
    except Exception:
        pass


def _format_parallel(results: List[Dict[str, Any]]) -> str:
    """Render governed fan-out results back into the turn — numbered, in input order,
    failures isolated so a single bad item never sinks the batch."""
    ok = sum(1 for r in results if r.get("ok"))
    lines: List[str] = []
    for i, r in enumerate(results, 1):
        if r.get("ok"):
            lines.append(f"[{i}] {(r.get('content') or '').strip()}")
        else:
            lines.append(f"[{i}] ERROR: {r.get('error')}")
    return f"[parallel_reason] {ok}/{len(results)} ok\n\n" + "\n\n".join(lines)


# ── retrieval helpers (shared by the per-turn RAG (B2) and the fan-out worker read (§3c)) ──
def _retrieve_hits(query: str, top_k: int) -> List[str]:
    """Cache-aside hit list for *query*: the optional warm tier (B0) in front of the cold vector
    store. Fail-soft → ``[]``. Assumes the caller already gated on ``_MEMORY`` availability."""
    if _MEMORY is None or not (query or "").strip():
        return []
    hits: Optional[List[str]] = None
    warm = _WARM
    _ns = _active_mem_ns()                                  # S3b: scope the cache to the active project's partition
    if warm is not None:
        try:
            hits = warm.cache_get(query, _ns)
        except Exception:  # noqa: BLE001
            hits = None
    if hits is None:
        try:
            hits = _MEMORY.search(query, top_k)
        except Exception:  # noqa: BLE001
            return []
        if warm is not None and hits:
            try:
                warm.cache_set(query, hits, _ns)
            except Exception:  # noqa: BLE001
                pass
    return list(hits or [])


# ── Epic #366 — token-accurate counting against the served model ─────────────
# Primary: the vLLM `/tokenize` endpoint (exact, no bundled tokenizer dependency — keeps the wheel
# lean). Fallback: the CALIBRATED chars/token estimate (CHARS_PER_TOKEN). Replaces the fixed chars/4
# guess everywhere it gates the 32 768-token wall (_derive_ctx_budget / _rag_block / the trim).
_TOKENIZE_TIMEOUT = 4.0


def _tokenize_url(base_url: str) -> str:
    """Derive the vLLM tokenize endpoint from the OpenAI base_url. The real route is ``/tokenize``
    at the server ROOT (NOT ``/v1/tokenize``, which 404s — verified live 2026-06-24), so a trailing
    ``/v1`` is stripped. Empty base_url ⇒ "" (the counter stays in fallback)."""
    b = (base_url or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-len("/v1")].rstrip("/")
    return (b + "/tokenize") if b else ""


def _host_is_probeable(base_url: str) -> bool:
    """Auto-probe the tokenize endpoint only for a real remote/LAN deployment host — never a
    loopback or a bare single-label stub. Keeps the offline unit suite hermetic; a server-mode
    loopback deployment (orchestrator co-located with vLLM) opts in explicitly via GX10_TOKENIZE=1."""
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    return "." in host          # a dotted name or an IPv4 ⇒ a real deployment endpoint


def _discover_max_model_len(base_url: str, model: str, timeout: float = 4.0) -> Optional[int]:
    """#377 (Epic #366 P2): read the LIVE model window from ``GET /v1/models`` at boot — the served
    ``max_model_len`` of the entry matching *model* (else the first). Returns None on any failure
    (fail-soft) so the caller keeps the configured ``MAX_MODEL_LEN``. Prevents budget drift when the
    Spark is relaunched with a different ``--max-model-len``."""
    try:
        url = (base_url or "").rstrip("/") + "/models"
        with urllib.request.urlopen(url, timeout=float(timeout)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        entries = data.get("data") or []
        chosen = next((e for e in entries if e.get("id") == model), None) or (entries[0] if entries else {})
        mml = chosen.get("max_model_len")
        return int(mml) if mml else None
    except Exception:  # noqa: BLE001 — fail-soft: never let boot discovery break startup
        return None


def _char_token_estimate(text: str) -> int:
    """The calibrated chars/token fallback count (conservative; rounds up)."""
    ratio = float(CHARS_PER_TOKEN) if CHARS_PER_TOKEN else 1.0
    if ratio < 1.0:
        ratio = 1.0
    return int(math.ceil(len(text or "") / ratio))


def _message_text(m: Dict[str, Any]) -> str:
    """The countable text of a chat message: content + any tool-call names/arguments."""
    parts = [str(m.get("content") or "")]
    for tc in (m.get("tool_calls") or []):
        fn = tc.get("function") or {}
        parts.append(str(fn.get("name", "")))
        parts.append(str(fn.get("arguments", "")))
    return "\n".join(p for p in parts if p)


def _count_tokens(text: str) -> int:
    """Best-available token count of *text*: the live tokenizer when reachable (Epic #366), else the
    calibrated chars/token estimate. Never raises, never None — for budgeting that always needs a
    number."""
    c = _TOKENS
    if c is not None:
        n = c.count_text(text)
        if n is not None:
            return n
    return _char_token_estimate(text)


def _live_token_counter():
    """The engine token counter iff it can still produce EXACT counts (live endpoint), else None →
    the caller uses the calibrated char path. A dead/inert counter returns None."""
    c = _TOKENS
    return c if (c is not None and c.usable()) else None


def _tools_schema_tokens() -> int:
    """Token cost of the tools schema vLLM serializes into EVERY prompt (Epic #366 — the trim must
    reserve it, else a dense tool set + memory/plugin tools overflows the wall even when the message
    trim 'fits'). Best-available count (live tokenizer or calibrated estimate); 0 on any error."""
    try:
        tools = _effective_tools()
        if not tools:
            return 0
        return _count_tokens(json.dumps(tools, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — never let the reserve computation break the trim
        return 0


def _count_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    """Best-available token count of a whole message list (live tokenizer or the calibrated
    estimate, never None), summed per message with the same per-message framing overhead as
    ``_TokenCounter.count_prompt`` — for the pre-flight guard (#372)."""
    return sum(_count_tokens(_message_text(m)) + 4 for m in messages)


def _bound_text_tail(text: str, budget_tokens: int) -> Tuple[str, bool]:
    """Keep the TAIL (most recent end) of *text* within ``budget_tokens`` — the live tokenizer when
    available, else the calibrated estimate (Epic #366 #373: bound the summarizer input so a large
    evicted transcript can't itself overflow the window and get silently truncated). Snaps to a
    paragraph boundary so a round isn't cut mid-way. Returns ``(bounded_text, truncated)``."""
    text = text or ""
    if budget_tokens <= 0 or _count_tokens(text) <= budget_tokens:
        return text, False
    approx = max(1, int(budget_tokens * float(CHARS_PER_TOKEN)))
    tail = text[-approx:]
    while tail and _count_tokens(tail) > budget_tokens:
        tail = tail[max(1, len(tail) // 10):]        # drop ~10 % from the head each pass (terminates)
    # Snap to a round boundary so we don't start mid-round — but ONLY when it discards a SMALL leading
    # fragment (≤20 %). A large round leading the slice has its first "\n\n" at the boundary to the
    # NEXT round, so an unconditional snap would throw the whole budgeted tail away (#373 review S3).
    nl = tail.find("\n\n")
    if 0 <= nl and (nl + 2) <= len(tail) * 0.2:
        tail = tail[nl + 2:]
    return tail, True


def _derive_token_budget(max_model_len: int, max_tokens: int, rag_tokens: int,
                         summary_tokens: int) -> Tuple[int, int]:
    """Epic #366 — the trim watermark in TOKENS (not chars): the model window minus the reserves it
    must leave free (output + RAG + summary), with 10 % headroom. Returns ``(high_tok, low_tok)``,
    low = 60 % of high (mirrors the legacy hysteresis). Floored so a tiny window can't yield ≤0."""
    reserve = max(0, int(max_tokens) + int(rag_tokens) + int(summary_tokens))
    high = max(2048, int((int(max_model_len) - reserve) * 0.9))
    return high, int(high * 0.6)


class _TokenCounter:
    """Token counting against the served model (Epic #366). Primary: the vLLM ``/tokenize``
    endpoint — exact, no bundled tokenizer dependency. Fallback: the calibrated chars/token
    estimate. The endpoint is probed lazily and disabled for the session on the first failure
    (fail-soft → the engine never 400s because counting broke). Per-text counts are cached.
    ``usable()`` stays True until a failure makes the counter inert/dead."""
    _MAX_CACHE = 4096

    def __init__(self, base_url: str, model: str, *, enabled: bool = True,
                 force_probe: bool = False, fallback_ratio: Optional[float] = None,
                 timeout: float = _TOKENIZE_TIMEOUT):
        self.model = model
        self.url = _tokenize_url(base_url)
        self.timeout = float(timeout)
        self.ratio = float(fallback_ratio) if fallback_ratio else float(CHARS_PER_TOKEN)
        self._cache: Dict[str, int] = {}
        self.live = False                       # flips True after the first successful tokenize
        probeable = bool(force_probe) or _host_is_probeable(base_url)
        # _dead ⇒ never touch the network; pure calibrated fallback (keeps the unit suite offline)
        self._dead = not (bool(enabled) and probeable and bool(self.url))

    def usable(self) -> bool:
        return not self._dead

    def _post(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self._dead:
            return None
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(self.url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            self.live = True
            return out
        except Exception:  # noqa: BLE001 — any failure ⇒ permanent fallback for the session
            self._dead = True
            return None

    def count_text(self, text: str) -> Optional[int]:
        """Exact token count of *text* (cached), or None when the endpoint is unavailable."""
        text = text or ""
        if not text:
            return 0
        if self._dead:
            return None
        hit = self._cache.get(text)
        if hit is not None:
            return hit
        out = self._post({"model": self.model, "prompt": text, "add_special_tokens": False})
        if out is None:
            return None
        n = out.get("count")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            # a 200 without a valid integer `count` (a proxy, a different service on the port, a
            # future/older route shape) must NOT be read as 0 tokens — that would leave the counter
            # "usable", never trim, and re-introduce the #366 overflow. Treat it as endpoint-dead so
            # the engine drops to the conservative calibrated char path.
            self._dead = True
            return None
        if len(self._cache) < self._MAX_CACHE:
            self._cache[text] = n
        return n

    def count_prompt(self, messages: List[Dict[str, Any]], *, per_msg_overhead: int = 4
                     ) -> Optional[int]:
        """Best-available exact prompt token count: the sum of per-message text counts plus a small
        per-message framing overhead (role/template tokens). Returns None only when the endpoint is
        unavailable (so the caller falls back to the calibrated char path wholesale)."""
        if self._dead:
            return None
        total = 0
        for m in messages:
            c = self.count_text(_message_text(m))
            if c is None:
                return None
            total += c + max(0, int(per_msg_overhead))
        return total


def _rag_block(hits: List[str], budget_tokens: int, in_window: str = "") -> str:
    """Format hits into a token-budgeted, deduped ``## Relevant context (retrieved)`` block (or
    ""). The budget is enforced in REAL tokens (the live tokenizer when available, else the
    calibrated chars/token estimate — Epic #366), not a chars/4 guess. Dedups within the block and
    against *in_window* (already-visible context; "" ⇒ skip)."""
    budget = max(0, int(budget_tokens))
    lines: List[str] = []
    seen: set = set()
    used = 0
    for h in hits:
        h = str(h).strip()
        if not h:
            continue
        key = " ".join(h.split())[:200].lower()
        if key in seen or (in_window and h in in_window):
            continue
        line = f"- {h}"
        cost = _count_tokens(line + "\n")
        if used + cost > budget:
            break
        lines.append(line)
        seen.add(key)
        used += cost
    if not lines:
        return ""
    return _RAG_MARKER + "\n" + "\n".join(lines)


def _worker_contexts(items: List[str]) -> Optional[List[Optional[str]]]:
    """§3c MAP: per-item vector retrieval so each fan-out worker gets its OWN focused foreground
    (not just the shared instruction). Returns a per-item list (a block or None per item), or
    ``None`` for the whole batch when disabled / memory unavailable → ``fanout`` stays
    byte-identical to today. Fail-soft; respects the same token budget as the per-turn RAG."""
    if not WORKER_MEMORY or _MEMORY is None:
        return None
    try:
        if not _MEMORY.is_available():
            return None
    except Exception:  # noqa: BLE001
        return None
    out: List[Optional[str]] = []
    any_ctx = False
    for it in items:
        try:
            hits = _retrieve_hits(it, RAG_TOP_K)
            block = _rag_block(hits, RAG_MAX_TOKENS) if hits else ""
        except Exception:  # noqa: BLE001 — one item's retrieval never sinks the batch
            block = ""
        out.append(block or None)
        if block:
            any_ctx = True
    return out if any_ctx else None


def _worker_shared_floor() -> str:
    """§3c MAP floor: the shared rolling summary (from the warm tier) that every fan-out worker
    gets on top of its per-item context — the common ground the main loop has. "" when disabled /
    warm not configured / no summary yet. Fail-soft."""
    if not WORKER_MEMORY or _WARM is None:
        return ""
    try:
        s = _WARM.get_session(_active_warm_session(), "summary")
    except Exception:  # noqa: BLE001
        return ""
    s = (s or "").strip()
    return (_SUMMARY_MARKER + "\n" + s) if s else ""


def _reduce_worker_results(results: List[Dict[str, Any]], topic: str = "") -> int:
    """§3c REDUCE: consolidate the OK fan-out outputs into ONE cold write via a SINGLE writer —
    workers never write Mem0 directly (parallel /add ⇒ duplicates + the slow LLM path on the hot
    fan-out). Dedups, then one ``add_bulk`` (or ``chunk_and_store`` when large). Flag-gated
    (WORKER_WRITE, ``reducer`` mode), fail-soft, off-critical-path (the writes are themselves
    fire-and-forget). Returns the number of consolidated items written (0 when disabled/empty).

    ``direct`` mode is reserved for long-lived autonomous agents that write idempotently on their
    own; for the stateless reasoning fan-out the reducer steps back (returns 0)."""
    if not WORKER_WRITE or WORKER_WRITE_MODE != "reducer" or _MEMORY is None:
        return 0
    try:
        if not _MEMORY.is_available():
            return 0
    except Exception:  # noqa: BLE001
        return 0
    seen: set = set()
    parts: List[str] = []
    for i, r in enumerate(results, 1):
        if not r.get("ok"):
            continue
        c = (r.get("content") or "").strip()
        if not c:
            continue
        key = " ".join(c.split())[:200].lower()
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"[{i}] {c}")
    if not parts:
        return 0
    blob = (f"Parallel reasoning on: {topic}\n\n" if topic else "") + "\n\n".join(parts)
    md: Dict[str, Any] = {"source": "worker_reduce"}
    if topic:
        md["topic"] = topic[:200]
    try:
        # one consolidated write; chunk if it exceeds the artifact cap (B3), else a single add_bulk
        if len(blob) > getattr(_MEMORY, "chunk_size", 6000):
            _MEMORY.chunk_and_store(blob, md, source="worker_reduce")
        else:
            _MEMORY.add_bulk(blob, md)
    except Exception:  # noqa: BLE001 — best effort, never break the turn
        return 0
    return len(parts)


def _derive_ctx_budget(max_model_len: int, max_tokens: int, rag_tokens: int,
                       summary_tokens: int, chars_per_token: float = CHARS_PER_TOKEN) -> tuple:
    """MEM-9 / §3-Mechanismus 3 — the CHAR-fallback trim watermark (used when the live tokenizer is
    unreachable; ``_trim_context`` then measures chars). Derived from the model window minus the
    reserves it must leave free — output (``max_tokens``) + the RAG block + the summary block — via
    the CALIBRATED chars/token estimate with 10 % headroom. Returns ``(high_chars, low_chars)`` with
    low = 60 % of high (mirrors the legacy 80k→48k hysteresis). At the calibrated ratio it stays
    conservatively UNDER the token wall (the old default of 4 c/t was the #366 overflow); the live
    path (``_derive_token_budget`` + the tokenizer) is exact. Floored so a tiny window can't yield ≤0."""
    reserve = max(0, int(max_tokens) + int(rag_tokens) + int(summary_tokens))
    budget_tok = max(2048, int((int(max_model_len) - reserve) * 0.9))
    high = int(budget_tok * max(1.0, float(chars_per_token)))
    return high, int(high * 0.6)


def _read_text_capped(p: Path, max_bytes=_MAX_FILE_BYTES) -> tuple[Optional[str], int]:
    """Read and decode a complete text file without allocating beyond ``max_bytes`` plus one."""
    size = p.stat().st_size
    if size > max_bytes:
        return None, size
    with p.open("rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        try:
            size = max(size, p.stat().st_size)
        except OSError:
            size = len(raw)
        return None, size
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    return text, size


def _read_file_ranged(text: str, *, start=None, end=None, max_chars=None, pattern=None) -> Optional[str]:
    """#1047: return a TARGETED slice of a file's text — a regex `pattern` (a window of lines around the
    first match) OR a 1-based inclusive line range `start`/`end`, capped by `max_chars` (else the live
    read cap). Returns None on a bad/empty range or an unmatched/invalid pattern so the caller falls back
    to the existing head+tail cap. Never raises. Mirrored in the ink client (clients/ink runTool.ts) so a
    local-topology read applies the same slice."""
    try:
        # No keepends + a "\n" join, so the line model (count, indices, regex `$`) matches the ink client's
        # (JS `$` anchors only at line end without a trailing newline, and its keepends split would add a
        # phantom trailing empty line) — the returned slice normalises endings to "\n", which is fine for an
        # excerpt the model reads.
        lines = text.splitlines()
        n = len(lines)
        if n == 0:
            return None
        if pattern:
            rx = re.compile(pattern)
            hit = next((i for i, ln in enumerate(lines) if rx.search(ln)), None)
            if hit is None:
                return None                              # no match → fall back to the head+tail cap
            ctx = 20                                     # a window of lines around the first match
            lo, hi = max(0, hit - ctx), min(n, hit + ctx + 1)
        elif start is not None or end is not None:
            s = int(start) if start is not None else 1
            e = int(end) if end is not None else n
            if s < 1 or s > n or e < s:
                return None                              # bad range → fall back
            lo, hi = s - 1, min(n, e)
        else:
            return None                                  # no ranged args → the caller uses the normal path
        body = "\n".join(lines[lo:hi])
        cap = int(max_chars) if max_chars else _read_char_cap()
        if cap > 0 and len(body) > cap:
            head_n = cap * 2 // 3
            tail_n = cap - head_n
            omitted = len(body) - head_n - tail_n
            body = (body[:head_n]
                    + f"\n\n... [Ironclad: {omitted} chars omitted from the slice — capped at {cap}] ...\n\n"
                    + body[-tail_n:])
        return f"[Ironclad: lines {lo + 1}-{hi} of {n}]\n{body}"
    except Exception:  # noqa: BLE001 — any bad arg → the caller falls back to the head+tail cap
        return None


def run_tool(name: str, args: Dict[str, Any], exec_cwd: "Optional[str]" = None,
             sandbox_policy: "Optional[str]" = None) -> str:
    """#1202: the SINGLE structural site that renders a listing's machine ``AnswerData`` into the localized
    ``Answer:`` sentence. It wraps the tool dispatch and is **command-gated**, so EVERY caller (the model
    run loop, ``/tool``, ``/ls``, the API) and EVERY topology (native + bridged client) gets the localized
    reply, the machine line NEVER leaks to a user, and a non-listing command's output is never rewritten.

    #1317: a BRIDGED client (Ink / thin CLI) that runs a passed-through code-tool LOCALLY passes the
    server-shipped active-project ``exec_cwd`` so its relative-path resolution + ``execute_command`` cwd
    target the active project, not the client's frozen boot workdir. Honoured only when the path exists on
    THIS host (mount) — a remote/sealed client or an older server (no cwd) falls back to the process workdir,
    byte-identical. ``exec_cwd=None`` (every non-bridge caller) is byte-identical."""
    if (exec_cwd and os.path.isdir(exec_cwd)) or sandbox_policy is not None:
        _tok = _EXEC_CWD_OVERRIDE.set(exec_cwd if exec_cwd and os.path.isdir(exec_cwd) else None)
        _stok = _SANDBOX_POLICY_OVERRIDE.set(sandbox_policy)
        try:
            return _run_tool_localized(name, args)
        finally:
            _SANDBOX_POLICY_OVERRIDE.reset(_stok)
            _EXEC_CWD_OVERRIDE.reset(_tok)
    return _run_tool_localized(name, args)


def _run_tool_localized(name: str, args: Dict[str, Any]) -> str:
    result = _run_tool_dispatch(name, args)
    if name == "execute_command":
        result = _localize_listing_answer(result, (args or {}).get("command", ""))
    return result


def _run_tool_dispatch(name: str, args: Dict[str, Any]) -> str:
    """Audit-gated dispatch shared by local, bridged, and server tool lanes."""
    global _AUDIT_DEGRADED
    protected = name in _AUDIT_TOOLS
    selected = protected or AUDIT_SCOPE == "all"
    if selected:
        # The intent append is the sole fail-closed enforcer. _AUDIT_DEGRADED is the surfaced health
        # signal from a post-mutation result failure; it stays latched until a protected intent recovers.
        was_degraded = protected and _AUDIT_DEGRADED
        try:
            _append_audit(name, args, "intent", ok=True)
        except Exception as exc:  # noqa: BLE001 — fail closed before the protected action
            if was_degraded:
                return f"ERROR: audit intent append failed; audit health is degraded; {name} was refused: {exc}"
            return f"ERROR: audit intent append failed; {name} was refused: {exc}"
        if protected:
            _AUDIT_DEGRADED = False
    result = _run_tool_dispatch_impl(name, args)
    if selected:
        try:
            _maybe_audit(name, args, result)
        except Exception as exc:  # noqa: BLE001 — mutation may already have happened
            if protected:
                _AUDIT_DEGRADED = True
            return (f"ERROR: audit result append failed after {name}; audit health is degraded: {exc}. "
                    f"Action result: {result}")
    return result


def _sandbox_model_command(command: str) -> "Tuple[Optional[str], Optional[str]]":
    """Prepare one model-issued command for execution, or return an actionable fail-closed refusal."""
    global _SANDBOX_BEST_EFFORT_WARNED
    if PLATFORM == "windows":
        return None, (
            "ERROR: execute_command refused: no supported model-command sandbox backend is available "
            "on Windows. Ironclad fails closed; use a Linux host with bwrap/firejail, or use the separate "
            "operator /sh channel for an explicitly unrestricted operator command."
        )
    preference = _SANDBOX_POLICY_OVERRIDE.get() or SANDBOX
    try:
        import sandbox as _sbx
        prepared = _sbx.sandbox_command(command, preference)
        if isinstance(prepared, _sbx.SandboxRefusal):
            return None, (
                "ERROR: execute_command refused: no supported sandbox backend is available "
                f"for policy '{preference}' ({prepared.reason}). Ironclad fails closed; install bwrap "
                "or firejail on this Linux host."
            )
        run_cmd, backend = prepared
        if not run_cmd or not backend:
            return None, (
                "ERROR: execute_command refused: sandbox preparation returned no isolated command. "
                "Ironclad fails closed; install bwrap or firejail on this Linux host."
            )
        if _sbx.is_best_effort_teardown(backend) and not _SANDBOX_BEST_EFFORT_WARNED:
            _SANDBOX_BEST_EFFORT_WARNED = True
            logger.warning(
                "sandbox: firejail tree teardown is best-effort-only; bwrap is preferred for complete "
                "namespace teardown"
            )
        return run_cmd, None
    except Exception:  # noqa: BLE001 — import/wrapper failures must never fall back to the raw command
        return None, (
            "ERROR: execute_command refused: mandatory sandbox preparation failed. Ironclad fails closed; "
            "verify that bwrap or firejail is installed and usable on this Linux host."
        )


class _CommandCancelled(Exception):
    """Internal control flow for a cancelled model command."""


def _run_plugin_handler_bounded(name: str, handler, args: Dict[str, Any]):
    """Run a plugin handler without letting a hung run() hold the turn and agent lock forever (#1545).

    The handler runs in a daemon thread while the caller observes turn cancellation and the idle-watchdog
    budget. A handler still running after either condition is abandoned so the turn can terminate.
    """
    box: Dict[str, Any] = {}

    def _worker():
        try:
            box["result"] = handler(**args)
        except BaseException as exc:  # noqa: BLE001 — surface any handler failure to the caller
            box["error"] = exc

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    cap = float(TURN_IDLE_TIMEOUT_S) if TURN_IDLE_TIMEOUT_S and TURN_IDLE_TIMEOUT_S > 0 else 0.0
    deadline = time.monotonic() + cap if cap > 0 else None
    while thread.is_alive():
        if _CANCEL_EVENT.is_set():
            raise _CommandCancelled()
        if deadline is not None and time.monotonic() > deadline:
            return (f"ERROR: plugin tool '{name}' exceeded its {cap:.0f}s execution budget and was "
                    "abandoned (a hung run() cannot hold the turn).")
        thread.join(timeout=0.2)
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _kill_command_process_tree(proc, *, windows: "Optional[bool]" = None) -> None:
    """Kill *proc* and every descendant; the caller remains responsible for reaping *proc*."""
    proc_tree.kill_process_tree(proc, windows=windows)


def _drain_after_kill(proc) -> "tuple":
    """Reap *proc* and drain buffered output after a tree kill, but NEVER block forever. On the bwrap path
    --unshare-pid guarantees every pipe writer dies (EOF), so this returns at once; the bounded wait is a
    belt-and-suspenders for a weaker backend (e.g. firejail) where a descendant might survive the group kill
    and hold the pipe open — the idle watchdog cannot unblock an in-flight communicate()."""
    return proc_tree.drain_after_kill(proc, _POST_KILL_DRAIN_S)


def _run_model_command_process(run_cmd: str, timeout: float, cwd: "Optional[str]"):
    """Run a model command in its own process tree, observing timeout and turn cancellation."""
    popen_args = dict(
        shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", cwd=cwd,
    )
    if os.name == "nt":
        popen_args["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_args["start_new_session"] = True
    proc = subprocess.Popen(run_cmd, **popen_args)
    deadline = time.monotonic() + timeout
    while True:
        if _CANCEL_EVENT.is_set():
            _kill_command_process_tree(proc)
            _drain_after_kill(proc)
            raise _CommandCancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_command_process_tree(proc)
            stdout, stderr = _drain_after_kill(proc)
            raise subprocess.TimeoutExpired(run_cmd, timeout, output=stdout, stderr=stderr)
        try:
            stdout, stderr = proc.communicate(timeout=min(remaining, _COMMAND_CANCEL_POLL_S))
        except subprocess.TimeoutExpired:
            continue
        return subprocess.CompletedProcess(run_cmd, proc.returncode, stdout, stderr)


def _run_tool_dispatch_impl(name: str, args: Dict[str, Any]) -> str:
    try:
        # #459 (§4, review A S2): the shell guardrail fires SERVER-SIDE here, BEFORE the local-tool bridge
        # dispatches execute_command to a client — otherwise a thin/Ink client would run the blocked
        # command on its own console (re-breaking #447). This is the authoritative guard for every client;
        # the per-execution-site PowerShell hardening below (and in the Ink client) is the 2nd layer.
        if name == "execute_command":
            # S12: catch a TOOL invoked as a shell command — the model sometimes types e.g.
            # `web_search "…"` into execute_command (especially when the tool is not currently
            # offered), which the shell rejects ("not recognized"). Redirect to the tool instead of
            # letting the shell error. None of our tool names is a real shell executable, so this is
            # safe.
            _cmd = (args.get("command") or "").strip()
            _first = _cmd.split(None, 1)[0].strip("\"'").lower() if _cmd else ""
            if _first in _all_tool_names():
                extra = (" If it is not offered, configure it (web_search needs search.adapter + "
                         "GX10_SEARCH_API_KEY on a local setup, or a web-capable provider)."
                         if _first == "web_search" else "")
                return (f"BLOCKED: '{_first}' is a tool, not a shell command — call the {_first} tool "
                        f"directly, do not run it via execute_command.{extra}")
            blocked = _shell_guard(args.get("command", ""))
            if blocked is not None:
                hint = ""
                if "remote" in blocked and _web_search_available():
                    hint = " Use the `web_search` tool for current/online information instead."
                return (f"BLOCKED: execute_command refuses {blocked} — it can corrupt the display or hang "
                        f"the session (#459).{hint}")
        # Pass code-tools THROUGH to the driving client (runs them on the local fs) when a
        # bridge is active; otherwise they fall through and run server-side as before.
        if _LOCAL_TOOL_BRIDGE is not None and name in LOCAL_TOOL_NAMES:
            return _LOCAL_TOOL_BRIDGE(name, args)
        if name == "read_file":
            p = _resolve_exec_path(args["path"])
            if not p.exists():
                return f"ERROR: Not found: {args['path']}"
            text, size = _read_text_capped(p)
            if text is None:
                return (f"ERROR: read_file refused: file too large — {size} bytes, "
                        f"cap {_MAX_FILE_BYTES} bytes")
            # #1047: a targeted ranged/pattern read returns only the relevant slice; a bad range/pattern
            # returns None → fall through to the existing head+tail cap below.
            if any(args.get(k) is not None for k in ("start", "end", "max_chars", "pattern")):
                ranged = _read_file_ranged(text, start=args.get("start"), end=args.get("end"),
                                           max_chars=args.get("max_chars"), pattern=args.get("pattern"))
                if ranged is not None:
                    return ranged
            # PERF-05 + #994-S16: don't load a large file uncapped into the context; the cap is the live
            # per-turn window budget (so one read can't overflow), falling back to the fixed ceiling.
            cap = _read_char_cap()
            if len(text) > cap:
                head_n = cap * 2 // 3
                tail_n = cap - head_n
                omitted = len(text) - head_n - tail_n
                return (
                    text[:head_n]
                    + f"\n\n... [Ironclad: {omitted} chars omitted — file {len(text)} "
                      f"chars, capped at {cap}. For targeted excerpts, use search_files to "
                      f"locate the relevant lines, then read only those.] ...\n\n"
                    + text[-tail_n:]
                )
            return text

        elif name == "write_file":
            p   = _resolve_exec_path(args["path"])
            if _is_audit_path(p):                                     # #1067 tamper-resistance
                return "ERROR: refusing to write into the audit directory (tamper-resistant audit trail)."
            p.parent.mkdir(parents=True, exist_ok=True)
            content = args["content"]
            if args.get("mode") == "append":                          # #1048: build a large file in chunks
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(content)
                return f"OK: Appended {len(content)} chars to {args['path']}"
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(p)
            return f"OK: Written {len(content)} chars to {args['path']}"

        elif name == "edit_file":
            # #1075: targeted string edit — cheaper/safer than a whole-file write_file. Exact match, unique
            # unless replace_all; atomic write with the retry-on-lock helper.
            p = _resolve_exec_path(args["path"])
            if _is_audit_path(p):                                     # #1067 tamper-resistance
                return "ERROR: refusing to edit the audit directory (tamper-resistant audit trail)."
            if not p.exists():
                return f"ERROR: Not found: {args['path']}"
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            if not old:
                return "ERROR: edit_file needs a non-empty old_string (use write_file to create a file)."
            text, size = _read_text_capped(p)
            if text is None:
                return (f"ERROR: edit_file refused: file too large — {size} bytes, "
                        f"cap {_MAX_FILE_BYTES} bytes")
            hits = text.count(old)
            if hits == 0:
                return f"ERROR: old_string not found in {args['path']} — it must match EXACTLY (whitespace included)."
            if hits > 1 and not args.get("replace_all"):
                return (f"ERROR: old_string is not unique in {args['path']} ({hits} occurrences) — add "
                        f"surrounding context to make it unique, or set replace_all=true.")
            updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
            # #1317: a no-op edit (old_string == new_string, or already applied) must NOT masquerade as a
            # successful write — surface it as an ERROR the model receives, so "OK: edited" always means the
            # bytes actually changed (edit_file runs synchronously; the ERROR is returned verbatim).
            if updated == text:
                return (f"ERROR: no change to {args['path']} — old_string equals new_string (or the edit "
                        f"was already applied); nothing written.")
            _atomic_write(p, updated)
            return f"OK: edited {args['path']} ({hits if args.get('replace_all') else 1} replacement(s))"

        elif name == "create_issue":
            # #1073/#1213: file a forge issue through the active forge ADAPTER (cli=gh | native=urllib | mock),
            # so it works with OR without a `gh` CLI on the box (the Spark `server` topology). Capability-
            # detected (offered + accepted when a forge transport is usable and the profile permits an outbound
            # write), secret-free, escape-free (body from a file). Same gate as the offer (_forge_available).
            if not _forge_available():
                if not FORGE_ENABLED:
                    return "ERROR: create_issue is force-disabled by the operator (config forge.enabled=false)."
                if _is_sealed_profile():
                    return "ERROR: create_issue is blocked under the sealed trust profile (no autonomous outbound writes)."
                if FORGE_ADAPTER == "native":
                    return (f"ERROR: create_issue (native forge) needs a token in ${FORGE_TOKEN_ENV} and "
                            f"forge.repo (owner/repo).")
                return "ERROR: create_issue needs the GitHub CLI ('gh') on PATH + authenticated (gh auth login)."
            bf = _resolve_exec_path(args["body_file"])
            if not bf.exists():
                return (f"ERROR: body_file not found: {args['body_file']} — write the issue body to a FILE "
                        f"first (write_last_reply / write_file), then pass its path.")
            # A) label validate→reask: the model must use EXISTING repo labels, not invent them. An unknown
            # label is rejected with the valid set (+ did-you-mean) so the model re-emits — instead of the forge
            # hard-failing the whole create on the first bad label, and instead of silently dropping it.
            # Fail-soft: if the label list can't be fetched, validation is skipped (never block on a hiccup).
            req_labels = [l.strip() for l in str(args.get("labels", "") or "").split(",") if l.strip()]
            if req_labels:
                valid = _forge_labels()
                if valid is not None:
                    unknown = [l for l in req_labels if l not in valid]
                    if unknown:
                        import difflib
                        parts = []
                        for u in unknown:
                            near = difflib.get_close_matches(u, valid, n=3, cutoff=0.5)
                            parts.append(f"'{u}'" + (f" (did you mean: {', '.join(near)}?)" if near else ""))
                        return ("ERROR: unknown label(s): " + "; ".join(parts) + ". Use existing labels ONLY "
                                "(do not invent labels). Valid labels: " + ", ".join(sorted(valid)) +
                                ". Re-call create_issue with valid labels (or omit `labels`).")
            _forge = _forge_transport()
            cst, cres = _forge.create_issue(str(args.get("title", "")), bf, req_labels,
                                            str(args["milestone"]) if args.get("milestone") else None)
            if cst != "ok":
                return f"ERROR: {cres}"
            new_url = (cres or {}).get("url", "")
            # B) parent linking: link the new issue as a NATIVE sub-issue — so the model links in-tool, not via
            # ad-hoc execute_command. Fail-soft: the issue already exists, so a link failure is reported
            # alongside it, not raised.
            parent = str(args.get("parent", "") or "").strip().lstrip("#")
            if parent and new_url:
                lst, lres = _forge.link_sub_issue(parent, cres)
                if lst != "ok":
                    return (f"OK: created issue {new_url}, but linking to parent #{parent} failed: "
                            f"{str(lres)[:200]}")
                return f"OK: created issue {new_url} and linked it as a sub-issue of #{parent}"
            return f"OK: created issue {new_url}"

        elif name == "view_issue":
            # #1208/#1213: read a tracker issue through the active forge ADAPTER (cli=gh | native=urllib) — the
            # first-class path for resolving a `#NNN` reference (never git-history grepping). A non-existent
            # issue returns an authoritative NOT_FOUND (the tracker WAS queried), so the model never falls back
            # to inferring non-existence from a missing commit.
            if not _forge_available():
                if not FORGE_ENABLED:
                    return "ERROR: view_issue is force-disabled by the operator (config forge.enabled=false)."
                if _is_sealed_profile():
                    return "ERROR: view_issue is blocked under the sealed trust profile (no autonomous outbound calls)."
                if FORGE_ADAPTER == "native":
                    return (f"ERROR: view_issue (native forge) needs a token in ${FORGE_TOKEN_ENV} and "
                            f"forge.repo (owner/repo).")
                return "ERROR: view_issue needs the GitHub CLI ('gh') on PATH + authenticated (gh auth login)."
            num = str(args.get("number", "")).strip().lstrip("#")
            if not num.isdigit():
                return f"ERROR: view_issue needs a numeric issue number (got {args.get('number')!r})."
            vst, vres = _forge_transport().view_issue(int(num))
            if vst == "not_found":
                where = f" in {FORGE_REPO}" if FORGE_REPO else ""
                return f"NOT_FOUND: issue #{num} does not exist{where} (the tracker was queried — authoritative)."
            if vst != "ok":
                return f"ERROR: {vres}"
            data = vres if isinstance(vres, dict) else {}
            labels = ", ".join(l.get("name", "") for l in (data.get("labels") or [])) or "-"
            milestone = (data.get("milestone") or {}).get("title") or "-"
            body = (data.get("body") or "").strip()
            if len(body) > 4000:   # bound a huge body so it can't blow the window (belt-and-suspenders)
                body = body[:4000].rstrip() + "\n… [body truncated]"
            return (f"#{data.get('number')} [{data.get('state')}] {data.get('title')}\n"
                    f"labels: {labels} · milestone: {milestone}\n"
                    f"url: {data.get('url')}\n\n{body}")

        elif name == "create_pr":
            # #1215: open a PR through the active forge adapter (cli=gh | native=urllib). OPEN-ONLY — it never
            # merges (merge stays a CI/review gate). Escape-free (body from a file), capability-detected +
            # sealed-gated exactly like create_issue.
            if not _forge_available():
                if not FORGE_ENABLED:
                    return "ERROR: create_pr is force-disabled by the operator (config forge.enabled=false)."
                if _is_sealed_profile():
                    return "ERROR: create_pr is blocked under the sealed trust profile (no autonomous outbound writes)."
                if FORGE_ADAPTER == "native":
                    return (f"ERROR: create_pr (native forge) needs a token in ${FORGE_TOKEN_ENV} and "
                            f"forge.repo (owner/repo).")
                return "ERROR: create_pr needs the GitHub CLI ('gh') on PATH + authenticated (gh auth login)."
            bfp = str(args.get("body_file", "") or "").strip()
            if not bfp:   # empty/omitted must not silently resolve to the cwd dir (Path('.').exists() is True)
                return ("ERROR: create_pr needs body_file — write the PR body to a FILE first "
                        "(write_last_reply / write_file), then pass its path.")
            bf = _resolve_exec_path(bfp)
            if not bf.exists():
                return (f"ERROR: body_file not found: {args.get('body_file')} — write the PR body to a FILE "
                        f"first (write_last_reply / write_file), then pass its path.")
            draft = str(args.get("draft", "") or "").strip().lower() in ("1", "true", "yes")
            pst, pres = _forge_transport().create_pr(
                str(args.get("title", "")), bf,
                str(args.get("base", "") or "") or None, str(args.get("head", "") or "") or None, draft)
            if pst != "ok":
                return f"ERROR: {pres}"
            return f"OK: opened PR {pres}"

        elif name == "comment_on_issue":
            # #1217: append a comment to an issue through the active forge adapter (cli=gh | native=urllib).
            # NARROW — comment only (never close/relabel). Escape-free, capability-detected + sealed-gated.
            if not _forge_available():
                if not FORGE_ENABLED:
                    return "ERROR: comment_on_issue is force-disabled by the operator (config forge.enabled=false)."
                if _is_sealed_profile():
                    return "ERROR: comment_on_issue is blocked under the sealed trust profile (no autonomous outbound writes)."
                if FORGE_ADAPTER == "native":
                    return (f"ERROR: comment_on_issue (native forge) needs a token in ${FORGE_TOKEN_ENV} and "
                            f"forge.repo (owner/repo).")
                return "ERROR: comment_on_issue needs the GitHub CLI ('gh') on PATH + authenticated (gh auth login)."
            num = str(args.get("number", "")).strip().lstrip("#")
            if not num.isdigit():
                return f"ERROR: comment_on_issue needs a numeric issue number (got {args.get('number')!r})."
            bfp = str(args.get("body_file", "") or "").strip()
            if not bfp:
                return ("ERROR: comment_on_issue needs body_file — write the comment to a FILE first "
                        "(write_last_reply / write_file), then pass its path.")
            bf = _resolve_exec_path(bfp)
            if not bf.exists():
                return (f"ERROR: body_file not found: {args.get('body_file')} — write the comment to a FILE "
                        f"first (write_last_reply / write_file), then pass its path.")
            cst, cres = _forge_transport().comment_on_issue(int(num), bf)
            if cst == "not_found":
                where = f" in {FORGE_REPO}" if FORGE_REPO else ""
                return f"NOT_FOUND: issue #{num} does not exist{where} (the tracker was queried — authoritative)."
            if cst != "ok":
                return f"ERROR: {cres}"
            return f"OK: commented on #{num}: {cres}"

        elif name == "pr_status":
            # #1219: read a PR's CI/mergeability SNAPSHOT through the active forge adapter. NON-BLOCKING — one
            # snapshot, never a watch/poll (the engine runs one agent turn behind a single lock).
            if not _forge_available():
                if not FORGE_ENABLED:
                    return "ERROR: pr_status is force-disabled by the operator (config forge.enabled=false)."
                if _is_sealed_profile():
                    return "ERROR: pr_status is blocked under the sealed trust profile (no autonomous outbound calls)."
                if FORGE_ADAPTER == "native":
                    return (f"ERROR: pr_status (native forge) needs a token in ${FORGE_TOKEN_ENV} and "
                            f"forge.repo (owner/repo).")
                return "ERROR: pr_status needs the GitHub CLI ('gh') on PATH + authenticated (gh auth login)."
            num = str(args.get("number", "")).strip().lstrip("#")
            if not num.isdigit():
                return f"ERROR: pr_status needs a numeric PR number (got {args.get('number')!r})."
            sst, sres = _forge_transport().pr_status(int(num))
            if sst == "not_found":
                where = f" in {FORGE_REPO}" if FORGE_REPO else ""
                return f"NOT_FOUND: PR #{num} does not exist{where} (the forge was queried — authoritative)."
            if sst != "ok":
                return f"ERROR: {sres}"
            data = sres if isinstance(sres, dict) else {}
            checks = data.get("checks") or []
            n_fail = sum(1 for c in checks if str((c or {}).get("bucket", "")).lower() in ("fail", "cancel"))
            n_pend = sum(1 for c in checks if str((c or {}).get("bucket", "")).lower() == "pending")
            verdict = ("no checks reported" if not checks
                       else f"{n_fail} FAILING" if n_fail
                       else f"{n_pend} PENDING" if n_pend
                       else "ALL PASSING")
            merge_line = (f"mergeable: {data.get('mergeable') or '?'} · "
                          f"state: {data.get('mergeStateStatus') or data.get('state') or '?'} · "
                          f"review: {data.get('reviewDecision') or '-'}")
            lines = [f"{str((c or {}).get('bucket', '?')).upper():9} {(c or {}).get('name', '')}" for c in checks]
            return (f"PR #{num} — {verdict} ({len(checks)} checks):\n{merge_line}"
                    + ("\n" + "\n".join(lines) if lines else ""))

        elif name == "review":
            # #1221: independent cross-model second opinion via a configured code-agent + default_cli_runner.
            # Capability-detected (offered + accepted only when a reviewer is runnable). A READ of reviewer
            # text → ingestion-fenced. Bounded synchronous call — never a watch/poll.
            if not _review_available():
                return ("ERROR: review is unavailable — no code-agent binary resolves on this box "
                        "(configure code_agents.pool and install a coder CLI).")
            agent = _pick_reviewer(args.get("agent"))
            if agent is None:
                requested = (args.get("agent") or "").strip()
                if requested:
                    return (f"ERROR: review agent {requested!r} is unknown or not runnable "
                            f"(configured: {', '.join(_agent_names()) or 'none'}).")
                return ("ERROR: no runnable reviewer agent "
                        "(configure code_agents.pool and install a coder CLI).")
            mode, material = _assemble_review_material(args.get("paths"))
            if material.startswith("ERROR:"):
                return material
            spec = _code_agent_registry().resolve(agent)
            if spec is None:   # belt-and-suspenders (pick already resolved)
                return f"ERROR: review agent {agent!r} is not in the code-agent registry."
            try:
                from tooling_envelope_runtime import _envelope_authorize_spec
                refused = _envelope_authorize_spec(spec)
            except Exception:
                refused = "tooling envelope refused malformed coder command"
            if refused:
                return f"ERROR: review by {agent} failed: {refused}"
            prompt = _review_prompt(str(args.get("focus") or ""), mode, material)
            try:
                from client import default_cli_runner
            except Exception as e:  # noqa: BLE001
                return f"ERROR: review runner import failed: {e!r}"
            effort = getattr(spec, "effort", None) or "high"
            try:
                timeout = float(REVIEW_TIMEOUT_S) if REVIEW_TIMEOUT_S is not None else None
            except (TypeError, ValueError):
                timeout = 180.0
            res = default_cli_runner(spec, prompt, effort=str(effort), timeout=timeout)
            if not res.get("ok"):
                err = res.get("error") or "reviewer failed"
                return f"ERROR: review by {agent} failed: {err}"
            content = (res.get("content") or "").strip()
            if not content:
                return f"ERROR: review by {agent} returned empty output."
            return f"[review by {agent} · {mode}]\n{content}"

        elif name == "fetch_url":
            # #1074: verbatim, size-capped http(s) fetch. Trust-gated (sealed) + SSRF-guarded + byte-capped;
            # the run-loop choke point (fetch_url ∈ _INGESTION_TOOLS) additionally caps the returned chars.
            if not _web_search_trust_ok():
                return ("BLOCKED: fetch_url is disabled under the sealed trust profile "
                        "(operator opt-in: security.web_in_sealed).")
            url = str(args.get("url", "")).strip()
            reason = _fetch_url_blocked(url)
            if reason:
                return f"BLOCKED: fetch_url refuses {url!r} — {reason}."
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Ironclad-fetch_url/1.0"})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    raw = resp.read(_FETCH_MAX_BYTES + 1)
                    ctype = resp.headers.get("Content-Type", "")
            except Exception as ex:   # noqa: BLE001 — network/HTTP error is a tool error, not a crash
                return f"ERROR: fetch_url failed for {url}: {ex!r}"
            over = len(raw) > _FETCH_MAX_BYTES
            text = raw[:_FETCH_MAX_BYTES].decode("utf-8", errors="replace")
            tail = f"\n\n... [Ironclad: response exceeded {_FETCH_MAX_BYTES} bytes, truncated] ..." if over else ""
            return f"[fetch_url {url}{(' · ' + ctype) if ctype else ''}]\n{text}{tail}"

        elif name == "list_directory":
            p = _resolve_exec_path(args.get("path", "."))
            if not p.exists():
                return f"ERROR: Not found: {args.get('path', '.')}"
            # #1488: cap-plus-one detects overflow without materialising a hostile directory. Exact totals
            # remain available for normal directories; an overflow is deliberately reported as "many".
            items = []
            overflow = False
            for item in p.iterdir():
                items.append(item)
                if len(items) > LIST_DIR_HARD_CAP:
                    overflow = True
                    break
            total = len(items)
            n_dirs_total = sum(1 for i in items if i.is_dir())
            if args.get("sort") == "time":
                items.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            else:
                items.sort(key=lambda x: (x.is_file(), x.name.lower()))

            # HV-B: optional limit + hard cap against context bombs
            limit = args.get("limit")
            try:
                limit = int(limit) if limit is not None else None
            except (TypeError, ValueError):
                limit = None
            if limit and limit > 0:
                items = items[:limit]

            capped = False
            if len(items) > LIST_DIR_HARD_CAP:
                items = items[:LIST_DIR_HARD_CAP]
                capped = True

            lines = [f"{'[D]' if i.is_dir() else '[F]'} {i.name}" for i in items]
            # #1183: a deterministic count header of the FULL set — LLMs miscount a list, so state the exact
            # numbers and let the model report them verbatim instead of re-counting.
            n_dirs = n_dirs_total
            n_files = total - n_dirs
            count = (("At least " if overflow else "") + _fmt_count(n_dirs, n_files))
            out = f"{count}\n" + "\n".join(lines) if lines else "(empty)"
            shown = len(lines)
            if overflow:
                # #1488 M1: on an overflowing dir the sample is the first LIST_DIR_HARD_CAP entries in
                # FILESYSTEM order, so a sort/limit ranks only this partial sample — NOT the true newest
                # across the whole dir (that would need a full walk, the DoS this cap avoids). Steer the
                # model to NARROW the path, not to sort='time' (which was misleadingly reliable-sounding).
                out += (f"\n... [GX10v3: first {shown} entries (filesystem order) of many"
                        + (f"; hard cap {LIST_DIR_HARD_CAP} — narrow the path for a complete listing" if capped else f" (limit={limit})")
                        + "; a sort/limit ranks only this partial sample, not the whole directory]")
            elif shown < total:
                out += (f"\n... [GX10v3: showing {shown} of {total} entries"
                        + (f" (hard cap {LIST_DIR_HARD_CAP} — use sort='time'+limit)" if capped else f" (limit={limit})")
                        + "]")
            return out

        elif name == "execute_command":
            timeout_arg = args.get("timeout", _EXEC_COMMAND_TIMEOUT_S)
            timeout = int(timeout_arg) if "timeout" in args else float(timeout_arg)
            command = args["command"]
            # #459: the fail-closed shell guardrail already ran at the top of run_tool (server-side, before
            # any bridge), so a blocked command never reaches here.
            # Platform mode determines the interpreter — consistent with the
            # syntax guidance injected into the model.
            # stdin=DEVNULL: interactive commands (e.g. cmd `date` without an arg)
            # get EOF immediately instead of blocking for the full timeout.
            # encoding/errors explicit: decode command output as UTF-8 lossily, so a
            # non-locale byte (cp1252 on Windows) never raises decoding the result.
            run_cmd, refusal = _sandbox_model_command(command)
            if refusal is not None:
                return refusal
            r = _run_model_command_process(
                run_cmd, timeout, _exec_cwd()          # S9c: active project's root (None → process workdir)
            )
            # #1196: BSD/macOS `ls` rejects the GNU-only `--color=always` (exit != 0), which would drop
            # the fs-computed header/Answer (gated on exit 0). Retry the LISTING without the colour flag
            # so it still works on a non-coreutils host. The retry is independently sandbox-prepared.
            if (r.returncode != 0 and "--color=always" in command
                    and _listing_target_for_command(command) is not None):
                _fb = re.sub(r"\s*--color=always\b", "", command)
                _fb_cmd, refusal = _sandbox_model_command(_fb)
                if refusal is not None:
                    return refusal
                r = _run_model_command_process(_fb_cmd, timeout, _exec_cwd())
            out = (r.stdout + r.stderr).strip()
            if r.returncode == 0 and out:   # #1193: prepend a deterministic listing count (from the fs)
                target = _listing_target_for_command(command)
                names = _directory_entry_names(target) if target is not None else None
                if names is not None:
                    # ONE snapshot feeds header AND answer data (no self-contradicting TOCTOU pair).
                    # #1202: the machine AnswerData line becomes the localized ready-made `Answer:`
                    # sentence SERVER-side (_localize_listing_answer at the run-loop choke point) —
                    # the model copies it verbatim instead of composing a summary. Above the hard cap
                    # only the header ships (the large-folder prompt rule governs there).
                    header = _fmt_count(len(names[0]), len(names[1]))
                    data = ""
                    if len(names[0]) + len(names[1]) <= LIST_DIR_HARD_CAP:
                        payload = json.dumps({"dirs": names[0], "files": names[1]},
                                             ensure_ascii=False, separators=(",", ":"))
                        data = f"AnswerData: {payload}\n"
                    out = f"{header}\n{data}{out}"
            return out or f"(exit {r.returncode}, no output)"

        elif name == "move_file":
            # Guard empty/missing source|destination BEFORE resolving: an empty path normalises to "."
            # (str(Path("")) == ".") which under a project root would target the root itself — a
            # destructive move. The pre-isolation code passed the raw "" to shutil.move (a plain error),
            # so this both preserves that no-op and closes the destructive edge.
            if not args.get("source") or not args.get("destination"):
                return "ERROR: move_file requires a non-empty source and destination"
            dst = _resolve_exec_path(args["destination"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(_resolve_exec_path(args["source"])), str(dst))
            return f"OK: Moved {args['source']} → {args['destination']}"

        elif name == "delete_file":
            _resolve_exec_path(args["path"]).unlink()
            return f"OK: Deleted {args['path']}"

        elif name == "copy_file":
            src = _resolve_exec_path(args["source"])
            dst = _resolve_exec_path(args["destination"])
            if not src.exists():
                return f"ERROR: Source not found: {args['source']}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            return f"OK: Copied {args['source']} → {args['destination']}"

        elif name == "search_files":
            raw          = args["pattern"]
            directory    = args.get("directory", ".")
            file_pattern = args.get("file_pattern", "*.md")
            # Real regex (case-insensitive); on an invalid pattern, safe
            # fallback to a literal substring match.
            try:
                rx = re.compile(raw, re.IGNORECASE)
                def _hit(line: str) -> bool:
                    return rx.search(line) is not None
            except re.error:
                needle = raw.lower()
                def _hit(line: str) -> bool:
                    return needle in line.lower()
            hits = []
            files_scanned = 0
            byte_truncated = False
            budget_truncated = False
            for fp in _resolve_exec_path(directory).rglob(file_pattern):
                if fp.is_file():
                    if files_scanned >= _SEARCH_MAX_FILES:
                        budget_truncated = True
                        break
                    files_scanned += 1
                    try:
                        with fp.open("rb") as fh:
                            raw_bytes = fh.read(_SEARCH_MAX_FILE_BYTES + 1)
                        if len(raw_bytes) > _SEARCH_MAX_FILE_BYTES:
                            byte_truncated = True
                        text = raw_bytes[:_SEARCH_MAX_FILE_BYTES].decode("utf-8", errors="replace")
                        for i, line in enumerate(text.splitlines(), 1):
                            if _hit(line):
                                hits.append(f"{fp}:{i}: {line.strip()}")
                                if len(hits) >= _SEARCH_HIT_CAP:
                                    notes = [f"stopped at the {_SEARCH_HIT_CAP}-hit cap"]
                                    if byte_truncated:
                                        notes.append(f"one or more files exceeded the {_SEARCH_MAX_FILE_BYTES}-byte read cap")
                                    return ("\n".join(hits) + "\n... [Ironclad: search truncated — "
                                            + "; ".join(notes) + "] ...")
                    except Exception:
                        pass
            notes = []
            if budget_truncated:
                notes.append(f"stopped after the {_SEARCH_MAX_FILES}-file scan budget")
            if byte_truncated:
                notes.append(f"one or more files exceeded the {_SEARCH_MAX_FILE_BYTES}-byte read cap")
            out = "\n".join(hits) if hits else "No matches"
            if notes:
                out += "\n... [Ironclad: search truncated — " + "; ".join(notes) + "] ..."
            return out

        elif name == "create_directory":
            _resolve_exec_path(args["path"]).mkdir(parents=True, exist_ok=True)
            return f"OK: Created {args['path']}"

        elif name == "advance_pipeline":
            # S6b: route through the curated facade (the engine driver, registered at import, delegates to
            # _advance_pipeline). Fall back to the impl only when the facade/driver is absent.
            tid, ag, nxt = args.get("task_id", ""), args.get("agent", ""), args.get("next_task_id")
            if _devapi is not None and _devapi.get_driver() is not None:
                return _devapi.advance(tid, ag, next_task_id=nxt)
            return _advance_pipeline(tid, ag, nxt)

        elif name == "stage_handover":
            # #1287: route the MODEL's agent pick to the cheapest CAPABLE coder for the task's cost tier (this is
            # the orchestrator's TOOL call, so a direct operator/internal/test _stage_handover keeps its explicit
            # agent). Fix 3 (dev-loop stab): when routing wins, STAMP the routed agent as the SINGLE canonical
            # identity — task_json.assigned_to AND the handover body `to:` — so filename == assigned_to == body
            # (else the coder mirrors a stale agent into feedback `from:` and readers / the reconciler first-guess
            # misattribute). Fail-soft: keep the model's pick when nothing routes.
            _agent = args.get("agent", "")
            _tj_out = args.get("task_json")
            _ho_out = args.get("handover_md", "")
            try:
                _fields = _tj_out if isinstance(_tj_out, dict) else (json.loads(_tj_out) if isinstance(_tj_out, str) and _tj_out.strip() else None)
                # #1296 parity: the re-hand path (task_id, no task_json — the continuation's
                # [NEXT-UNIT] staging) routes off the STORED task, so a lazily staged unit gets the
                # same deterministic cost routing as a created one.
                if not isinstance(_fields, dict) and args.get("task_id"):
                    _stored = _store().get(str(args.get("task_id")))
                    if isinstance(_stored, dict):
                        _fields = _stored
                _routed = _route_code_agent(_fields) if isinstance(_fields, dict) else None
                if _routed and _routed != _agent:
                    _agent = _routed
                    if isinstance(_fields, dict) and _tj_out is not None:
                        _fields["assigned_to"] = _routed
                        _tj_out = _fields if isinstance(args.get("task_json"), dict) else json.dumps(_fields)
                    _ho_out = re.sub(r"(?im)^(\s*to:\s*).*$", lambda m: m.group(1) + _routed, _ho_out, count=1)
            except Exception:  # noqa: BLE001 — routing must never break the tool call
                pass
            # S6b: route through the curated facade (the engine driver delegates to _stage_handover).
            if _devapi is not None and _devapi.get_driver() is not None:
                return _devapi.stage_handover(
                    _agent,
                    _ho_out,
                    task_id=args.get("task_id"),
                    task_json=_tj_out,
                    set_active=args.get("set_active", True),
                    force=args.get("force", False),
                )
            return _stage_handover(
                args.get("task_id"),
                _agent,
                _ho_out,
                _tj_out,
                args.get("set_active", True),
                args.get("force", False),
            )

        elif name == "plan_units":
            return _plan_units(
                args.get("epic_json"),
                args.get("units_json"),
                epic_id=args.get("epic_id") or None,
                force=args.get("force", False),
            )

        elif name == "check_task_exists":
            title = args.get("title", "")
            if not title.strip():
                return "ERROR: title required"
            existing = _store().find_duplicate(title, args.get("description", ""))
            return f"EXISTS: {existing}" if existing else "NONE"

        elif name == "launch_coder":
            return _trigger_coder(args.get("task_id") or None)

        elif name == "record_design":
            try:
                rel = record_design(
                    args.get("title", ""),
                    args.get("body", ""),
                    language=args.get("language", "") or "",
                    network=args.get("network", "") or "",
                )
            except (GateRefusal, ValueError) as e:
                return f"ERROR: {e}"
            return (f"OK: design proposal recorded at {rel} (type: proposal, stage: design, approved: false). "
                    f"STOP — get it approved "
                    f"(/approve) before an implementation handover; the engine refuses one until then.")

        elif name == "record_constraints":
            if not FRAMING_NOTES_ENABLED:
                return "ERROR: framing notes disabled"
            try:
                rel = record_constraints(
                    args.get("title", ""),
                    args.get("body", ""),
                    language=args.get("language", "") or "",
                    network=args.get("network", "") or "",
                    source=args.get("source", "") or "",
                )
            except (GateRefusal, ValueError) as e:
                return f"ERROR: {e}"
            status, _body = _constraint_status(active_slug())
            return f"OK: framing notes recorded at {rel} ({status})."

        elif name == "query_memory":
            if _MEMORY is None or not _MEMORY.is_available():
                return "[Memory] unavailable — is the memory stack running? `docker compose --profile memory up -d` (in core/)"
            return _MEMORY.query(
                args.get("query", ""),
                int(args.get("limit", 8)),
            )

        elif name == "deep_query_memory":
            # §3-Mechanismus 5: relational/multi-hop (graph=true), off the hot path. Re-gated.
            if _MEMORY is None or not _MEMORY.is_available():
                return "[Memory] unavailable — is the memory stack running? `docker compose --profile memory up -d` (in core/)"
            return _MEMORY.deep_query(
                args.get("query", ""),
                int(args.get("limit", 5)),
            )

        elif name == "remember":
            # #1076: deliberate durable memory write. Gated on a configured store; scope-aware + fail-soft
            # (add_bulk is fire-and-forget on a daemon thread, like the eviction archive) — best-effort.
            if _MEMORY is None or not _MEMORY.is_available():
                return "[Memory] unavailable — remember needs the memory stack (docker compose --profile memory up -d in core/)."
            text = str(args.get("text", "")).strip()
            if not text:
                return "ERROR: remember needs a non-empty `text` (the fact/decision to persist)."
            _MEMORY.add_bulk(text, {"source": "model_remember"})
            return f"OK: remembered ({len(text)} chars persisted to project memory; retrievable via query_memory / RAG)."

        elif name == "web_search":
            # #459 / epic #505: run the search through the standalone adapter seam (cli-delegate runs
            # SERVER-side via the captured CLI runner → immune to the console-write scaling break;
            # brave/mock run directly). Re-gated: the tool is only offered when available, but a
            # runtime config change could remove it.
            # S7 exec re-gate (mandatory): the offer-gate is bypassed by a manual `/tool web_search`
            # call and by hallucinated/continued-context calls, so the sealed trust block MUST also
            # sit here — a deterministic refusal, never a silent egress.
            if not _web_search_trust_ok():
                return ("[web_search] blocked under the sealed (sovereign) trust profile — outbound "
                        "web search is off; set security.web_in_sealed=true to allow it.")
            if not _web_search_available():
                return ("[web_search] unavailable — no usable search adapter is configured "
                        "(set search.adapter and, for brave, GX10_SEARCH_API_KEY).")
            # Validate + normalize the input at the tool boundary (Validate->Reask): a violation
            # returns a model-readable error so the call is re-emitted rather than swallowed.
            from websearch import validate_web_search_input
            req, verr = validate_web_search_input(args)
            if verr is not None:
                return verr
            out = _WEBSEARCH.run(req.query, req.allow_domains, req.block_domains)
            # S9: emit a `[search]` control frame (the [perf]/[agent] pattern) → the client footer
            # ("web N · Xms"), stripped from the chat by every client. n = result batches, ms = the
            # measured duration. Fail-soft: a frame error must never break the turn.
            try:
                _q = (req.query or "").replace('"', "'").replace("\n", " ")[:60]
                _ui_print(col(f'  [search] q="{_q}" n={out.batch_count()} ms={out.duration_ms}', C.GRAY))
            except Exception:
                pass
            # S5: deterministic `Sources:` block + the max-output cap; the structured SearchOutput
            # stays internal to the renderer/sentinel path. The model receives clean text + sources,
            # never JSON (D5). max_output_chars comes from config (S8); default otherwise.
            from websearch_adapters import format_for_model
            _scfg = (_EFFECTIVE_CFG or {}).get("search") or {}
            return format_for_model(out, max_output_chars=_scfg.get("max_output_chars"))

        elif name == "parallel_reason":
            if _WORKERS is None:
                return ("[parallel_reason] unavailable — this tool runs only under the "
                        "server (the governed fan-out workers live there).")
            items = args.get("items")
            if (not isinstance(items, list) or not items
                    or not all(isinstance(x, str) for x in items)):
                return "ERROR: 'items' must be a non-empty list of strings"
            mt = args.get("max_tokens")
            instruction = args.get("instruction") or None
            # §3c MAP: each worker becomes a memory READ-citizen — shared rolling-summary floor
            # (fold into the system instruction) + per-item retrieved foreground. All flag-gated,
            # fail-soft; with the flag off floor="" and contexts=None ⇒ today's stateless fan-out.
            floor = _worker_shared_floor()
            system = ((instruction + "\n\n" + floor) if instruction else floor) if floor else instruction
            contexts = _worker_contexts(items)
            # P0: when the provider router is active (enabled + a pool), route each item to its
            # provider (Spark fanout / external CLI) under per-substrate governors; otherwise the
            # exact, byte-identical fanout path below. system/contexts/mt/instruction are REUSED
            # (so §3c-MAP still applies). Lazy imports → no top-level import churn when off.
            if _DISPATCHER is not None and _DISPATCHER.active():
                from router import Budget, LoadSignal, RouteRequest
                from dispatch import DispatchPolicy
                probe = getattr(_DISPATCHER, "chat_busy_probe", None)
                load = LoadSignal(
                    spark_chat_busy=bool(probe()) if callable(probe) else False,
                    spark_batch_width=_WORKERS.max_concurrency,
                )
                eff = args.get("effort", "medium")
                reqs = [
                    RouteRequest(index=i, effort=eff,
                                 est_input_chars=len(it) + len((contexts[i] if contexts else "") or ""))
                    for i, it in enumerate(items)
                ]
                _pcfg = (_EFFECTIVE_CFG or {}).get("providers") or {}
                budget = Budget(usd_cap=(_pcfg.get("budget") or {}).get("usd_cap"))
                results = _DISPATCHER.dispatch(
                    items, contexts=contexts,
                    policy=DispatchPolicy(reqs, system=system, load=load, budget=budget),
                    max_tokens=int(mt) if mt else None, think=True,
                )
                _emit_agent_frames(results)              # #453: surface which coder(s) were routed
            else:
                results = _WORKERS.fanout(
                    items,
                    system=system,
                    contexts=contexts,
                    max_tokens=int(mt) if mt else None,
                    think=True,
                )
            # §3c REDUCE: single-writer consolidation of the OK outputs into ONE cold write
            # (flag-gated, fail-soft, fire-and-forget) — no parallel-write races. Off ⇒ no-op.
            _reduce_worker_results(results, topic=(instruction or ""))
            return _format_parallel(results)

        elif name == "use_skill":
            # Playbook skill kind (ADR-0001): progressive-disclosure access to SKILL.md skills.
            return _use_skill(str(args.get("capability", "") or ""),
                              str(args.get("reference", "") or ""))

        elif name == "use_prompt":
            # Prompt skill kind (ADR-0003): list → guided elicitation → multilingual assemble.
            return _use_prompt(str(args.get("capability", "") or ""),
                               str(args.get("values", "") or ""),
                               str(args.get("lang", "") or ""))

        elif name in _PLUGIN_TOOLS:
            # Open extension surface: dispatch to a discovered plugin skill's run().
            handler = _PLUGIN_TOOLS[name]["handler"]
            result = _run_plugin_handler_bounded(name, handler, args)
            if isinstance(result, str) and result.startswith("ERROR: plugin tool '"):
                return result
            if inspect.iscoroutine(result):
                result.close()
                return (f"ERROR: plugin '{name}' is async; the engine tool path needs a "
                        f"synchronous run() (see docs/plugin-api.md).")
            return str(result)

        else:
            return f"ERROR: Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return f"ERROR: Timeout after {args.get('timeout', _EXEC_COMMAND_TIMEOUT_S)}s"
    except _CommandCancelled:
        return "ERROR: cancelled"
    except Exception as e:
        return f"ERROR: {e}"

# ─── Orchestrator ─────────────────────────────────────────────
class GX10:
    def __init__(self, base_url: str, api_key: str, model: str, prompt_path: str,
                 stream: bool = True, max_tokens: int = MAX_TOKENS,
                 thinking_mode: str = "auto", platform: Optional[str] = None,
                 onboarding: Optional[bool] = None):
        self.client        = OpenAI(base_url=base_url, api_key=api_key,
                                    timeout=_client_timeout(), max_retries=LLM_MAX_RETRIES)   # #1131: fail-soft bound
        self.model         = model
        self.stream        = stream
        self.max_tokens    = max_tokens
        self.thinking_mode = thinking_mode   # "auto" | "first" | "off" | "all"
        self.platform      = platform or PLATFORM   # "windows" | "linux"
        self.onboarding    = ONBOARDING_MODE if onboarding is None else bool(onboarding)
        self.messages: List[Dict] = []
        self.last_response = ""
        # #1049 (L3): the current user turn, captured at run() entry. Read by _summarize to BIAS the
        # rolling summary toward task-relevant state on eviction (bias, not filter). "" ⇒ the generic
        # instruction (byte-identical to today) — so a summarize outside a run() never changes behaviour.
        self._current_user_turn = ""
        # #1050 (L3): set when a generation attempt THIS user turn errored → the emergency-rung summarize is
        # skipped (don't hit an already-sick endpoint on the recovery path). Reset at run() entry.
        self._turn_gen_errored = False
        self._first_token_seen = False
        self._finalized_this_turn = False
        # #1051 (L3): per-turn summarize counters — the shared rate-limit + telemetry across the steady-state
        # roll, the emergency rung, and the proactive accountant. Reset at run() entry.
        self._summaries_this_turn = 0
        self._summary_tokens_this_turn = 0
        self._turn_think = True   # auto decision per turn (safe default)
        # OPT-3: cumulative performance counters over the session
        self._perf = {"gens": 0, "prompt": 0, "completion": 0, "wall": 0.0, "last": ""}
        self._load_prompt(prompt_path)
        self._inject_platform_guidance()
        if self.onboarding:
            self._append_guidance(_onboarding_guidance())
        self._ensure_dirs()
        # Initialize the memory layer (fail-soft, once per process)
        global _MEMORY, _WARM, _TOKENS
        # Activation keys on a CONFIGURED endpoint, not on the config dict being non-empty: the typed
        # schema (F6a) always seeds a full `memory`/`warm` default tree, so the dict is now truthy even
        # when unconfigured. Without a base_url/url the layer stays off (hooks inert), as documented.
        if (_MemoryManager is not None and _MEMORY is None
                and str((_MEMORY_CONFIG or {}).get("base_url") or "").strip()):
            _MEMORY = _MemoryManager(_MEMORY_CONFIG)
        # Initialize the warm tier (B0) — optional; without a url the tier stays a no-op (fail-soft).
        if (_WarmTier is not None and _WARM is None
                and str((_WARM_CONFIG or {}).get("url") or "").strip()):
            _WARM = _WarmTier(_WARM_CONFIG)
        # Epic #366 — the per-engine token counter (vLLM /tokenize + calibrated char fallback).
        # GX10_TOKENIZE: unset/auto ⇒ probe only a real remote/LAN host; 1/on ⇒ force the probe
        # (a server-mode loopback deployment opts in here); 0/off ⇒ pure calibrated char fallback.
        _tok_env = os.environ.get("GX10_TOKENIZE", "").strip().lower()
        if _TOKENS is None and _tok_env not in ("0", "false", "off", "no"):
            _TOKENS = _TokenCounter(base_url, self.model,
                                    force_probe=_tok_env in ("1", "true", "on", "yes"))
        # #377: adopt the LIVE model window from GET /v1/models at boot (fail-soft) — prevents budget
        # drift if the Spark relaunched with a different --max-model-len. Only a real remote/LAN host
        # (keeps the offline suite hermetic); disable with GX10_DISCOVER_WINDOW=0.
        global MAX_MODEL_LEN, MAX_CTX_CHARS, TRIM_TARGET_CHARS
        _disc_env = os.environ.get("GX10_DISCOVER_WINDOW", "").strip().lower()
        if _disc_env not in ("0", "false", "off", "no") and _host_is_probeable(base_url):
            _live_win = _discover_max_model_len(base_url, self.model)
            if _live_win and _live_win != MAX_MODEL_LEN:
                _ui_print(col(f"[INFO] adopting live model window: max_model_len={_live_win} "
                              f"(configured {MAX_MODEL_LEN})", C.GRAY))
                MAX_MODEL_LEN = _live_win
                # re-derive the char-fallback watermarks for the new window — but BUDGET-3 (#503): keep an
                # operator-supplied GX10_MAX_CTX_CHARS/GX10_TRIM_TARGET_CHARS (don't clobber it here either).
                if TOKEN_BUDGET and not (os.environ.get("GX10_MAX_CTX_CHARS") or os.environ.get("GX10_TRIM_TARGET_CHARS")):
                    MAX_CTX_CHARS, TRIM_TARGET_CHARS = _derive_ctx_budget(
                        MAX_MODEL_LEN, MAX_TOKENS, RAG_MAX_TOKENS, SUMMARY_MAX_TOKENS, CHARS_PER_TOKEN)

    def _append_guidance(self, note: str):
        """Appends a runtime note to the system prompt (or creates a
        minimal system message if --no-prompt). Happens BEFORE
        load_session, so the note is preserved on session resume."""
        sys_msg = next((m for m in self.messages if m.get("role") == "system"), None)
        if sys_msg:
            sys_msg["content"] = sys_msg["content"].rstrip() + "\n\n" + note
        else:
            self.messages.insert(0, {"role": "system", "content": note})

    def _inject_platform_guidance(self):
        self._append_guidance(_platform_guidance(self.platform))
        self._append_guidance(_language_guidance(LANGUAGE))
        # #967: inject the spec-derived command surface (canonical verbs + deprecated + danger tiers) so the
        # model names commands correctly and never recommends a deprecated one (the operator hit exactly this
        # — the model pushed /initiative and denied /project). Fail-soft + additive: the prompt FILE is
        # untouched, and a missing/empty spec injects nothing.
        try:
            import command_spec as _command_spec
            _cmd_surface = _command_spec.context_summary()
        except Exception:
            _cmd_surface = ""
        if _cmd_surface:
            self._append_guidance(_cmd_surface)

    # OPT-4: one completion call with 1× retry on a transient API error
    def _preflight_context(self, think: bool) -> int:
        """Epic #366 (#372/#379): decide the output-token budget for the imminent vLLM call so the full
        prompt + the reserves it must leave free — output + the tools schema vLLM serializes into the
        prompt + the CONDITIONAL thinking budget (only when ``think``) — fit the model window. Returns the
        EFFECTIVE ``max_tokens`` for this request (``<= self.max_tokens``).

        The output reserve is a CEILING, not a fixed floor. When the full reserve would push the prompt
        over the window, we reserve LESS output — down to ``MIN_OUTPUT_TOKENS`` — so the turn proceeds
        LOSSLESSLY (all context kept, just a shorter answer) instead of failing. Only when even a minimal
        answer will not fit do we emergency-trim the oldest WHOLE rounds (then, as a last resort, truncate
        an irreducible oversized turn); only when THAT still cannot free room do we raise a clear
        ``ContextOverflowError`` instead of letting vLLM return a raw HTTP 400.

        Fail-fast (no retry against vLLM) and fail-soft: when token budgeting is off OR no EXACT tokenizer
        is reachable, return the full reserve unchanged — the calibrated estimate over-counts, so trusting
        it here could shrink/raise on input that would actually fit; #371's calibrated ``_trim_context``
        has already budgeted conservatively in that mode."""
        ceiling = int(self.max_tokens)
        if not TOKEN_BUDGET or _live_token_counter() is None:
            return ceiling
        tools_tok = _tools_schema_tokens()
        think_tok = int(THINKING_RESERVE if think else 0)
        # non-output reserves — these cannot be shrunk. OVERFLOW_SAFETY_TOKENS is headroom kept BELOW the
        # wall so the adaptive clamp never targets it to the token: `est` undercounts vLLM's exact rendered
        # prompt (chat-template framing + tools/tool-call serialization), so a zero-margin send still 400s.
        fixed = tools_tok + think_tok + int(OVERFLOW_SAFETY_TOKENS)
        est = _count_prompt_tokens(self.messages)
        if _live_token_counter() is None:
            return ceiling                              # tokenizer DIED mid-count ⇒ `est` is contaminated by
                                                        # the over-counting char fallback ⇒ trust the trim
        avail = MAX_MODEL_LEN - fixed - est             # room left for generation at the full prompt
        if avail >= ceiling:
            return ceiling                              # the full output reserve fits — nothing to do
        if avail >= MIN_OUTPUT_TOKENS:
            return int(avail)                           # reserve LESS output, keep ALL context (lossless)
        # even a minimal answer will not fit → free room by dropping the oldest whole rounds (then, as a
        # last resort inside _emergency_trim, truncating an irreducible oversized turn).
        est = self._emergency_trim(MAX_MODEL_LEN - fixed - MIN_OUTPUT_TOKENS)
        if _live_token_counter() is None:
            return ceiling                              # died mid-trim ⇒ don't raise on a contaminated count
        avail = MAX_MODEL_LEN - fixed - est
        if avail >= MIN_OUTPUT_TOKENS:
            return min(ceiling, int(avail))
        raise ContextOverflowError(
            f"context overflow: prompt ~{est} tok + reserve {fixed + MIN_OUTPUT_TOKENS} "
            f"(min output {MIN_OUTPUT_TOKENS}"
            + (f" + thinking {THINKING_RESERVE}" if think else "")
            + f" + tools {tools_tok}) exceeds the model window {MAX_MODEL_LEN}. "
            f"Shorten this turn (smaller paste / file excerpt / tool output) or raise the model "
            f"window (GX10_MAX_MODEL_LEN / a larger --max-model-len).")

    def _emergency_trim(self, budget: int) -> int:
        """Drop the oldest WHOLE rounds (the round's user turn + its assistant.tool_calls + their
        tool responses, atomically — dropping a partial round triggers a different 400,
        ``tool_calls must be followed by tool messages``) until the prompt fits ``budget`` tokens or
        only the system partition + the last user turn remain (an irreducible single oversized turn
        is then the caller's ``ContextOverflowError``). Best-effort lossless cold archive of the
        evicted text. Returns the final estimated prompt tokens."""
        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]
        evicted: List[Dict] = []
        while len(others) > 1 and _count_prompt_tokens(system + others) > budget:
            cut = 1
            while cut < len(others) and others[cut].get("role") != "user":
                cut += 1
            if cut >= len(others):
                break                                # only the last user turn remains → irreducible
            evicted.extend(others[:cut])
            del others[:cut]
        if evicted and _MEMORY is not None:          # lossless archive, fire-and-forget (no model call)
            try:
                if _MEMORY.is_available():
                    _MEMORY.add_bulk(self._render_rounds(evicted), {"source": "emergency_trim"})
            except Exception:  # noqa: BLE001 — the archive is best effort
                pass
        self.messages = system + others
        est = _count_prompt_tokens(self.messages)
        if est > budget:                                 # #994-S16 / #366: whole-round eviction couldn't fit
            est = self._trim_oversized_messages(budget)   # → truncate the biggest turns' content, don't raise
        return est

    _TRUNCATE_FLOOR_CHARS = 256   # smallest a message's content is shrunk to (keep a head+tail excerpt)

    def _trim_oversized_messages(self, budget: int) -> int:
        """#994-S16 / #366: last-resort recovery — when whole-round eviction can't fit the transcript,
        ITERATIVELY truncate the largest non-system string-content messages (head+tail + a marker) until it
        fits ``budget`` or nothing reducible remains. Iterative (not one message): an agentic loop is ONE
        user turn with MANY accumulated tool reads and no user boundary to evict — a single truncation left
        the turn over the wall (the operator's #1035 follow-up: ~28–34k after one cut). Only content strings
        shrink, so the assistant.tool_calls ↔ tool-response pairing is untouched. Returns the final estimate
        (still > budget only when the system partition + framing alone overflow — then the caller raises)."""
        floor = self._TRUNCATE_FLOOR_CHARS
        summarized = False   # #1050: at most ONE recovery-path summarize per invocation (bounds the cost)
        for _ in range(len(self.messages) + 8):          # bounded: at most one pass per message (+ slack)
            est = _count_prompt_tokens(self.messages)
            over = est - budget
            if over <= 0:
                return est
            cands = [(i, len(m["content"])) for i, m in enumerate(self.messages)
                     if m.get("role") != "system" and isinstance(m.get("content"), str)
                     and len(m["content"]) > floor]
            if not cands:
                return est                               # everything reducible is at the floor → caller raises
            i, clen = max(cands, key=lambda t: t[1])
            content = self.messages[i]["content"]
            cut_chars = min(clen - floor, int((over + 128) * float(CHARS_PER_TOKEN)))   # +128 tok margin
            if cut_chars <= 0:
                return est
            keep = clen - cut_chars
            head_n = keep * 2 // 3
            tail_n = keep - head_n
            discarded = content[head_n:clen - tail_n] if tail_n > 0 else content[head_n:]
            # #1050: ALWAYS cold-archive the discarded slice so rung-2 recovery stops losing data silently
            # (B2 RAG re-injects it query-aware next turn) — fail-soft, fire-and-forget, no model call.
            self._archive_trimmed_slice(discarded)
            # #1050: optional summarize-not-truncate (DEFAULT OFF) — replace the raw drop with a bounded
            # summary. At most ONE model call per invocation, under a hard timeout, skipped when a generation
            # this turn already errored, and ALWAYS falling through to the raw marker on timeout/exception.
            middle = f"\n\n... [Ironclad: {clen - keep} chars truncated to fit the context window] ...\n\n"
            if (EMERGENCY_SUMMARIZE and not summarized and not self._turn_gen_errored
                    and self._summary_budget_ok() and discarded.strip()):   # #1051: shared per-turn cap
                summarized = True   # mark the one attempt USED before the call → no retry against a sick endpoint
                summ = self._summarize_slice_timed(discarded)
                if summ:
                    self._note_summary(summ)   # #1051: count toward the shared per-turn cap
                    middle = f"\n\n... [Ironclad: {clen - keep} chars summarized to fit] {summ} ...\n\n"
            self.messages[i]["content"] = content[:head_n] + middle + (content[-tail_n:] if tail_n > 0 else "")
        return _count_prompt_tokens(self.messages)

    def _archive_trimmed_slice(self, text: str) -> None:
        """#1050: lossless cold-archive of a slice discarded by fragment truncation — so the rung-2
        recovery stops silently dropping data (B2 RAG re-injects it query-aware next turn). Fail-soft,
        fire-and-forget, NO model call; mirrors _emergency_trim's whole-round archive."""
        if not text or _MEMORY is None:
            return
        try:
            if _MEMORY.is_available():
                _MEMORY.add_bulk(text, {"source": "fragment_trim"})
        except Exception:  # noqa: BLE001 — the archive is best effort
            pass

    def _summarize_slice_timed(self, text: str) -> Optional[str]:
        """#1050: run _summarize on a to-be-truncated slice under a HARD wall-clock timeout, on a DAEMON
        thread (signal.alarm is unusable on win32 and off the main thread; a daemon thread never blocks
        process exit). Returns the summary, or None on timeout/exception → the caller falls through to raw
        head+tail truncation. Never raises; never blocks longer than EMERGENCY_SUMMARIZE_TIMEOUT_S."""
        box: Dict[str, Any] = {}

        def _work() -> None:
            try:
                box["v"] = self._summarize("", text)
            except Exception:  # noqa: BLE001 — a summarizer error → the caller falls through to raw truncation
                box["v"] = None

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout=EMERGENCY_SUMMARIZE_TIMEOUT_S)
        if t.is_alive():
            return None    # timed out — abandon the daemon thread, fall through to raw truncation
        val = box.get("v")
        return (val or "").strip() or None

    def _live_read_budget(self) -> int:
        """#994-S16: the safe char cap for a tool result THIS turn — the model window minus the reserves it
        must leave free (output + tools schema + thinking) minus what the transcript already uses, in chars,
        with a 0.8 safety margin and a small floor. So a single read/tool result can't by itself overflow the
        window, on any model. Fails soft to the fixed ``MAX_FILE_CHARS`` when budgeting is off / no exact
        tokenizer (the calibrated estimate over-counts → don't starve a read that would actually fit)."""
        if not TOKEN_BUDGET or _live_token_counter() is None:
            return MAX_FILE_CHARS
        reserve = int(self.max_tokens) + _tools_schema_tokens() + int(THINKING_RESERVE)
        free_tok = MAX_MODEL_LEN - reserve - _count_prompt_tokens(self.messages)
        free_chars = int(max(0, free_tok) * float(CHARS_PER_TOKEN) * 0.8)
        return max(_READ_FLOOR_CHARS, min(MAX_FILE_CHARS, free_chars))

    def _sanitize_tool_call_history(self) -> None:
        """#1039 defense-in-depth: EVERY assistant tool_call's arguments in the history must be valid JSON
        before the request goes out — vLLM json.loads() them when it renders the prompt. #1039 sanitises at
        the APPEND site, but a tool_call can also enter the history from a LOADED session (session.json
        persists the raw arguments verbatim) or a resume/handover — a persisted MALFORMED call would 400 the
        FIRST request after a restart (operator hit exactly this: a truncated `write_file` call reloaded from
        session.json → `Expecting ',' delimiter`, 0-gen 400). One cheap, idempotent pass over the history at
        the single send choke-point covers every entry path."""
        for m in self.messages:
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn["arguments"] = _valid_tool_args_json(fn.get("arguments"))

    def _make_completion(self, think: bool, stream: bool):
        self._sanitize_tool_call_history()   # #1039: no malformed tool-call args (incl. loaded session) reach vLLM
        # #372/#379: guard + adaptive output reserve + emergency trim (raises only on irreducible overflow).
        eff_max_tokens = self._preflight_context(think)   # <= self.max_tokens; shrinks to fit rather than fail
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=self.messages,
            tools=_effective_tools(),
            tool_choice="auto",
            temperature=TEMPERATURE,
            max_tokens=eff_max_tokens,
            extra_body={"chat_template_kwargs": {"enable_thinking": think}},
        )
        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}   # OPT-3: usage in the stream
        last_err = None
        for attempt in range(2):
            if _CANCEL_EVENT.is_set():
                raise RuntimeError("cancelled")
            try:
                _cli = self.client.with_options(max_retries=0) if _decoupled() else self.client
                return _cli.chat.completions.create(**kwargs)
            except Exception as e:
                last_err = e
                if attempt == 0 and not _CANCEL_EVENT.is_set() and not (_decoupled() and _is_timeout_error(e)):
                    time.sleep(RETRY_BACKOFF)
                    continue
                self._turn_gen_errored = True   # #1050: a generation this turn errored → skip the recovery-path summarize
                raise last_err

    @staticmethod
    def _fmt_perf(m: Dict[str, Any]) -> str:
        parts = []
        if m.get("ttft") is not None:
            parts.append(f"TTFT {m['ttft']:.1f}s")
        ct, gt = m.get("completion_tokens"), m.get("gen")
        if ct and gt:
            parts.append(f"{ct} tok/{gt:.1f}s = {ct / gt:.0f} tok/s")
        elif m.get("total") is not None:
            parts.append(f"{m['total']:.1f}s")
        if m.get("prompt_tokens"):
            parts.append(f"prompt {m['prompt_tokens']}")
        return "[perf] " + " · ".join(parts) if parts else "[perf] —"

    def _load_prompt(self, path_str: str):
        if not path_str:
            _ui_print(col("[INFO] started without a system prompt.", C.GRAY))
            return
        p = Path(path_str)
        if p.exists():
            content = p.read_text(encoding="utf-8")
            self.messages.append({"role": "system", "content": content})
            _ui_print(col(f"[OK] Prompt: {p} ({len(content)} chars)", C.GREEN))
        else:
            _ui_print(col(f"[WARN] not found: {p}", C.YELLOW))

    def save_session(self, *, strict: bool = False):
        # Silent by design: called after every turn (see _dispatch) — a per-turn "[OK] session saved"
        # would stream into the client as noise. Only a real failure is surfaced. ``strict`` re-raises
        # instead of swallowing — a /switch saves the LEAVING conversation with strict=True so a failed
        # save aborts the switch (ctx still on the leaving project) rather than silently losing it.
        try:
            p = session_path()
            p.parent.mkdir(parents=True, exist_ok=True)   # state_root existiert i.d.R. (ensure_dirs); idempotent
            data = json.dumps({"messages": self.messages}, ensure_ascii=False, indent=2)
            fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            if strict:
                raise
            _ui_print(col(f"[WARN] session not saved: {e}", C.YELLOW))

    @staticmethod
    def _sanitize_messages(msgs: List[Dict]) -> List[Dict]:
        """Repairs a message list so the API invariant holds:
        - orphaned tool responses (without a matching tool_call) are discarded
        - assistant.tool_calls without a (complete) tool response are removed."""
        out: List[Dict] = []
        open_ids: set = set()

        def close_open():
            if not open_ids:
                return
            for i in range(len(out) - 1, -1, -1):
                a = out[i]
                if a.get("role") == "assistant" and a.get("tool_calls"):
                    kept = [tc for tc in a["tool_calls"] if tc.get("id") not in open_ids]
                    a = dict(a)
                    if kept:
                        a["tool_calls"] = kept
                        out[i] = a
                    else:
                        a.pop("tool_calls", None)
                        if a.get("content"):
                            out[i] = a
                        else:
                            out.pop(i)
                    return

        for m in msgs:
            role = m.get("role")
            if role == "tool":
                tcid = m.get("tool_call_id")
                if tcid in open_ids:
                    out.append(m)
                    open_ids.discard(tcid)
                continue
            close_open()
            open_ids = set()
            out.append(m)
            if role == "assistant" and m.get("tool_calls"):
                open_ids = {tc.get("id") for tc in m["tool_calls"]}
        close_open()
        return out

    def load_session(self) -> int:
        p = session_path()
        if not p.exists():
            return 0
        try:
            data   = json.loads(p.read_text(encoding="utf-8"))
            raw    = data.get("messages", [])
            # #1547: retain the generated rolling-summary system message (it captures evicted history) —
            # only the BASE prompt system message is replaced by the current one. Distinguish by the marker.
            summary = next(
                (m for m in raw
                 if m.get("role") == "system" and str(m.get("content", "")).startswith(_SUMMARY_MARKER)),
                None,
            )
            loaded = self._sanitize_messages([m for m in raw if m.get("role") != "system"])
            system = next((m for m in self.messages if m.get("role") == "system"), None)
            self.messages = ([system] if system else []) + ([summary] if summary else []) + loaded
            return len(loaded)
        except Exception as e:
            _ui_print(col(f"[WARN] session not loadable: {e}", C.YELLOW))
            return 0

    def _trim_context(self):
        # Epic #366: trim against REAL token counts when the live tokenizer is reachable — the
        # gating path no longer guesses chars/token, so a dense (code/JSON/CJK) window can't
        # silently exceed the model wall. Fail-soft: no live tokenizer (or it dies mid-trim) ⇒
        # today's calibrated char hysteresis below (_trim_context_chars).
        if TOKEN_BUDGET:
            tok = _live_token_counter()
            if tok is not None and self._trim_context_tokens(tok):
                return
        self._trim_context_chars()

    def _trim_context_tokens(self, tok) -> bool:
        """Token-accurate trim: evict whole oldest rounds until the FULL prompt the server will see —
        the system partition (prompt + rolling summary) + the non-system window + the tools schema +
        the output reserve — fits the model wall. Returns True ONLY when the result actually fits;
        False (⇒ the caller falls back to the char path, and ultimately the #372 pre-flight guard)
        when the tokenizer dies mid-count OR the system partition + tools + output alone can't fit
        (an irreducible over-wall window is the guard's job, not the trimmer's). Preserves the
        prefix-cache hysteresis (no edit below the high-water) and the whole-round trim invariant."""
        # Reserve the value the REQUEST actually uses (self.max_tokens) — the module global desyncs
        # on a runtime `config set generation.max_tokens` (#371 review S3) — plus the tools schema
        # vLLM serializes into the prompt on every call (#371 review S2).
        out_reserve = int(self.max_tokens)
        tools_tok = _tools_schema_tokens()
        high, low = _derive_token_budget(MAX_MODEL_LEN, out_reserve, RAG_MAX_TOKENS, SUMMARY_MAX_TOKENS)
        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]
        sys_tok = tok.count_prompt(system)
        if sys_tok is None:
            return False
        # the hard ceiling for non-system content: what's left under the wall after system + tools +
        # output. The hysteresis watermarks are CLAMPED to it, so a floor can never certify a non-fit
        # (#371 review S2 — the old max(512,…) floor lifted the ceiling above the wall).
        hard_room = MAX_MODEL_LEN - out_reserve - tools_tok - sys_tok
        if hard_room <= 0:
            return False                        # system + tools + output alone overflow ⇒ defer to #372
        hi_others = max(0, min(high - sys_tok - tools_tok, hard_room))
        lo_others = max(0, min(low - sys_tok - tools_tok, int(hard_room * 0.9)))
        others_tok = tok.count_prompt(others)
        if others_tok is None:
            return False
        if others_tok > hi_others:              # above the high-water ⇒ trim down to the low-water
            track = SUMMARIZE_EVICTED
            evicted: List[Dict] = []
            while len(others) > 1:
                cur = tok.count_prompt(others)
                if cur is None:
                    return False                # endpoint died mid-trim ⇒ let the char path retry
                if cur <= lo_others:
                    break
                cut = 1
                while cut < len(others) and others[cut].get("role") != "user":
                    cut += 1
                # Safety: never delete the only user turn (→ API 400 "No user query found"). A single
                # irreducible oversized turn is the pre-flight guard's job (#372), not the trimmer's.
                if cut >= len(others):
                    break
                if track:
                    evicted.extend(others[:cut])
                del others[:cut]
            if track and evicted:
                self._roll_summary(system, evicted)   # mutates `system` (adds/updates the summary block)
            self.messages = system + others
        # Honest fit check on the FINAL prompt (system may have grown a summary block above) — only
        # claim success if it really fits, else fall back (so a genuinely-too-big window is not
        # silently certified and sent to vLLM).
        final = tok.count_prompt(self.messages)
        if final is None:
            return False
        return (final + tools_tok + out_reserve) <= MAX_MODEL_LEN

    def _trim_context_chars(self):
        def total_len(msgs):
            # BUDGET-2 (#503): count tool_call name+arguments too — assistant tool-call messages carry
            # empty content but often-large arguments, so a content-only sum under-measured the window.
            return sum(len(_message_text(m)) for m in msgs)

        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]

        # BUDGET-1 (#503): the char-fallback watermark must reserve the partitions that go into EVERY
        # prompt but are NOT in `others` — the system partition, the tools schema vLLM serializes, and the
        # thinking-output reserve — mirroring the token path's `hard_room = window - out - tools - sys`.
        # MAX_CTX_CHARS already reserves output+RAG+summary; subtract sys+tools+thinking here (floored so a
        # large system prompt can't drive the budget <= 0), then trim to 60 % of THAT.
        cpt = max(1.0, float(CHARS_PER_TOKEN))
        # Reserve the thinking-output headroom only when this turn may actually think (mirrors the token
        # path's `THINKING_RESERVE if think else 0`); a turn with thinking off needs no output reserve.
        think_on = bool(getattr(self, "_turn_think", True)) and getattr(self, "thinking_mode", "auto") != "off"
        reserve_chars = (total_len(system) + int(_tools_schema_tokens() * cpt)
                         + int((THINKING_RESERVE if think_on else 0) * cpt))
        # Floor at 25 % of the configured budget (not an absolute) so a pathological reserve can't drive the
        # working set toward 0, while a small configured budget isn't inflated by a fixed floor.
        high = max(max(512, int(MAX_CTX_CHARS * 0.25)), MAX_CTX_CHARS - reserve_chars)
        low = min(TRIM_TARGET_CHARS, int(high * 0.6))

        # PERF-06: hysteresis trimming for the vLLM prefix cache. Below the high-water mark the message
        # list stays UNCHANGED → the prefix after the system prompt is stable and the server's KV/prefix
        # cache holds across many rounds.
        if total_len(others) <= high:
            return
        # Trimming happens only when exceeded — then in one go down to the low-water mark (rare cache
        # invalidation instead of every iteration).

        # B1: only when the switch is on, record the removed rounds
        # to summarize + archive them. OFF = empty path, no overhead.
        track = SUMMARIZE_EVICTED
        evicted: List[Dict] = []

        # Trim in whole "rounds", so assistant.tool_calls and the
        # associated tool responses stay together (API invariant).
        while total_len(others) > low and len(others) > 1:
            cut = 1
            while cut < len(others) and others[cut].get("role") != "user":
                cut += 1
            # Safety: no second user message → stop trimming.
            # Otherwise the only user message would be deleted → API 400
            # "No user query found in messages" (happens e.g. with autoplan
            # when only one user turn is in the context but many tool results).
            if cut >= len(others):
                break
            if track:
                evicted.extend(others[:cut])
            del others[:cut]

        # B1: roll the evicted rounds into the summary block + archive to cold.
        # _roll_summary mutates `system` in place (summary message directly below the
        # prompt) — OFF or without eviction `system` stays untouched → the reassign
        # below is then byte-identical to today's trim. Fail-soft: any error
        # degrades to a plain drop (the rounds are already removed above).
        if track and evicted:
            self._roll_summary(system, evicted)

        self.messages = system + others

    # ── B1: rolling summarization on eviction (flag-gated, fail-soft) ──
    @staticmethod
    def _find_summary(system: List[Dict]) -> Optional[Dict]:
        """The existing summary message in the system partition (or None)."""
        for m in system:
            if str(m.get("content") or "").startswith(_SUMMARY_MARKER):
                return m
        return None

    @staticmethod
    def _render_rounds(msgs: List[Dict]) -> str:
        """Render evicted messages compactly as a transcript (raw text for the summary +
        the lossless cold archive). Tool calls without content are named."""
        parts: List[str] = []
        for m in msgs:
            role = m.get("role", "?")
            content = str(m.get("content") or "")
            if not content and m.get("tool_calls"):
                names = ", ".join(
                    str((c.get("function") or {}).get("name", "?"))
                    for c in (m.get("tool_calls") or [])
                )
                content = f"(tool calls: {names})"
            parts.append(f"[{role}] {content}")
        return "\n\n".join(parts)

    def _summarize(self, prev_summary: str, raw: str) -> str:
        """A capped, non-thinking completion call that merges the previous summary with the
        new transcript into ONE consolidated summary (hierarchical — the new one
        subsumes the old, the block stays bounded). Raises on error → the caller falls
        back to the plain drop. No tools, off the normal generation path."""
        instr = (
            "You maintain a running summary of an ongoing agent session so older turns can be "
            "dropped from the context window without losing essential state. Merge the PREVIOUS "
            "SUMMARY with the NEW TRANSCRIPT into a single concise summary. Preserve: decisions "
            "made, facts established, file paths, task ids, open threads — anything needed to "
            "continue the work. Be terse and factual. Output only the summary text."
        )
        # #1049 (L3): query-aware fidelity — bias, NOT filter. The generic instruction above already
        # preserves all established state; when a user turn is in scope we additionally ask the summarizer
        # to PREFER task-relevant facts/paths/ids/threads when the summary must be tight. This biases the
        # rolling summary only; it does not make eviction relevance-ranked (recency eviction is unchanged;
        # relevance recall stays with B2 RAG). Empty turn ⇒ the instruction is byte-identical to today.
        # The focus is bounded so a large paste can't bloat the summarizer's own prompt.
        _turn = (getattr(self, "_current_user_turn", "") or "").strip()
        if _turn:
            _focus = _turn if len(_turn) <= 400 else _turn[:400] + "…"
            instr += (
                " The user's CURRENT task is: \"" + _focus + "\". When the summary must be tight, PREFER "
                "keeping facts, paths, ids and open threads relevant to that task over unrelated ones — "
                "this biases retention; it does not license dropping state you could otherwise keep."
            )
        prev_body = ""
        if prev_summary.strip():
            # remove the marker line before re-feeding (only the content matters)
            prev_body = prev_summary.split("\n", 1)[1].strip() if "\n" in prev_summary else ""
        # #373: bound the summarizer INPUT (tail-first) so a large evicted transcript can't itself
        # overflow the model window and get silently truncated by vLLM (a lossy rolling summary). The
        # FULL raw was already archived losslessly to cold by _roll_summary; here we summarize the
        # most recent (tail) within budget. input_budget = min(4096, max_model_len // 4) (decision #3).
        input_budget = min(4096, max(256, int(MAX_MODEL_LEN) // 4))
        raw_budget = max(256, input_budget - _count_tokens(prev_body))
        raw, truncated = _bound_text_tail(raw, raw_budget)
        if truncated:
            _ui_print(col("[WARN] summarizer input bounded to the most recent rounds "
                          f"(~{raw_budget} tok); older rounds were dropped from the summary "
                          "(kept in cold if memory is configured).", C.YELLOW))
        user = ""
        if prev_body:
            user += "PREVIOUS SUMMARY:\n" + prev_body + "\n\n"
        user += "NEW TRANSCRIPT:\n" + raw
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": instr},
                      {"role": "user", "content": user}],
            max_tokens=SUMMARY_MAX_TOKENS,
            temperature=0.2,
            stream=False,
        )
        return (resp.choices[0].message.content or "").strip()

    def _summary_budget_ok(self) -> bool:
        """#1051: the shared per-turn summarize rate-limit consulted by ALL three summarize triggers (the
        steady-state roll, the emergency rung, the proactive accountant) so they can't compound into
        multiple full model round-trips in one turn. MAX_SUMMARIES_PER_TURN <= 0 ⇒ unlimited (byte-identical
        to today — the gate always passes)."""
        return MAX_SUMMARIES_PER_TURN <= 0 or self._summaries_this_turn < MAX_SUMMARIES_PER_TURN

    def _note_summary(self, summary_text: str) -> None:
        """#1051: telemetry for the shared rate-limit — count one summarize + a cheap CHAR-based estimate of
        the tokens it produced (no tokenizer round-trip, so the default path adds no network call)."""
        self._summaries_this_turn += 1
        self._summary_tokens_this_turn += int(len(summary_text or "") / max(1.0, float(CHARS_PER_TOKEN)))

    def _proactive_roll_if_needed(self) -> None:
        """#1051 (L3): the proactive cumulative-ingestion accountant. DEFAULT OFF (byte-identical — a single
        flag check, no token count, no eviction). ON → once the transcript crosses INGEST_SOFT_FRAC of the
        model window, proactively shed the oldest WHOLE tool rounds via a query-aware roll-summary (high
        floor) instead of waiting for the reactive low-floor truncation. Bounded by the shared per-turn
        summarize cap (past it _roll_summary degrades to a plain archived drop) and self-debouncing (it sheds
        back under the soft mark). Fail-soft; an irreducible single turn is left to the reactive ladder."""
        if not PROACTIVE_ROLL:
            return
        soft = int(MAX_MODEL_LEN * INGEST_SOFT_FRAC)
        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]
        if _count_prompt_tokens(system + others) < soft:
            return
        evicted: List[Dict] = []
        while len(others) > 1 and _count_prompt_tokens(system + others) >= soft:
            cut = 1
            while cut < len(others) and others[cut].get("role") != "user":
                cut += 1
            if cut >= len(others):
                break                       # only the last user turn remains → leave it to the reactive ladder
            evicted.extend(others[:cut])
            del others[:cut]
        if not evicted:
            return
        self._roll_summary(system, evicted)   # query-aware summary (budget-gated) + lossless cold archive
        self.messages = system + others       # system may have grown a summary block in place

    def _roll_summary(self, system: List[Dict], evicted: List[Dict]) -> None:
        """Roll the evicted rounds into the summary block (directly below the system prompt) and
        archive the raw text losslessly to cold. Fail-soft: any error leaves `system`
        unchanged → it stays with today's plain drop (the rounds are already removed)."""
        raw = self._render_rounds(evicted)
        # 1) Lossless cold archive (fire-and-forget, vector-only) — nothing is lost.
        if _MEMORY is not None:
            try:
                if _MEMORY.is_available():
                    _MEMORY.add_bulk(raw, {"source": "context_eviction"})
            except Exception:  # noqa: BLE001 — the archive is best effort
                pass
        # 2) In-window rolling summary (synchronous, capped model call, fail-soft).
        prev = self._find_summary(system)
        if not self._summary_budget_ok():   # #1051: shared per-turn cap hit → plain archived drop (no summary)
            return
        try:
            new_summary = self._summarize(prev["content"] if prev else "", raw)
        except Exception as e:  # noqa: BLE001 — a failed summary must not tip the turn
            _ui_print(col(f"[WARN] context summary skipped: {e}", C.YELLOW))
            return
        if not new_summary.strip():
            return
        self._note_summary(new_summary)     # #1051: count this summarize toward the shared per-turn cap
        content = _SUMMARY_MARKER + "\n" + new_summary.strip()
        if prev is not None:
            prev["content"] = content          # find-and-update (no duplicate)
        else:
            system.append({"role": "system", "content": content})   # directly below the prompt
        # §3c: also mirror the rolling summary into the warm tier — survives an orchestrator restart
        # and is readable by the fan-out workers as a shared floor. Fail-soft; no-op without a warm tier.
        if _WARM is not None:
            try:
                _WARM.set_session(_active_warm_session(), "summary", new_summary.strip())
            except Exception:  # noqa: BLE001 — the warm mirror is best effort
                pass

    # ── B2: per-turn retrieval assembly (flag-gated, fail-soft, cache-aside) ──
    def _retrieve_context(self, query: str) -> str:
        """A vector-only retrieval on *query*, deduplicated against the live window and
        token-budgeted, as a ``## Relevant context (retrieved)`` block (or "" on miss /
        unavailable / flag OFF). Cache-aside via the optional warm tier; fail-soft
        end-to-end — any error → "". FLAG OFF returns "" immediately (no memory touch,
        no network) → the user message is appended verbatim = byte-identical to today."""
        if not RAG_ENABLED or not (query or "").strip() or _MEMORY is None:
            return ""
        try:
            if not _MEMORY.is_available():
                return ""
        except Exception:  # noqa: BLE001
            return ""
        hits = _retrieve_hits(query, RAG_TOP_K)
        if not hits:
            return ""
        # dedup against what the model already sees anyway (don't inject twice)
        in_window = "\n".join(str(m.get("content") or "") for m in self.messages)
        return _rag_block(hits, RAG_MAX_TOKENS, in_window)

    def _ensure_dirs(self):
        # Engine machinery (hidden): state_root + local warm cache.
        state_root().mkdir(parents=True, exist_ok=True)
        (state_root() / "memory").mkdir(parents=True, exist_ok=True)
        for d in WORKSPACE_DIRS:
            Path(d).mkdir(parents=True, exist_ok=True)

    def _think_for(self, iteration: int) -> bool:
        if self.thinking_mode == "off":
            return False
        if self.thinking_mode == "all":
            return True
        if self.thinking_mode == "auto":
            # Thinking is front-loaded → only iteration 0, and only when the
            # turn classification deems it necessary (otherwise execute directly).
            return iteration == 0 and self._turn_think
        return iteration == 0   # "first": only the planning round ever thinks

    @staticmethod
    def _classify_thinking(text: str) -> bool:
        """auto mode: True = iteration 0 WITH thinking.
        Safe failure mode: when in doubt, True (think). Only for clear routine
        (status/lookup/`done`) WITHOUT a planning verb → False."""
        t = (text or "").lower().strip()
        if not t:
            return False
        if any(k in t for k in _PLANNING_KW):
            return True                      # planning detected → think
        if t == "done" or any(k in t for k in _ROUTINE_KW):
            return False                     # clear routine → no thinking
        if len(t.split()) <= 8 and any(k in t for k in _SMALLTALK_KW):
            return False                     # short smalltalk/identity → no thinking
        return True                          # doubt → think

    # ── Generation: streaming (PERF-01) ──────────────────────
    def _generate(self, think: bool) -> Tuple[str, List[Dict], bool, Optional[Exception], Dict[str, Any]]:
        """Returns (content, tool_calls, cancelled, err, metrics).
        The streaming path shows content live (thinking filtered out)."""
        self._first_token_seen = False
        if not self.stream:
            return self._generate_plain(think)

        chunk_q: _q.Queue = _q.Queue()
        err     = [None]
        usage   = [None]          # OPT-3: usage from the last chunk
        finish  = [None]          # #1048: last finish_reason (=="length" ⇒ generation cut off by the token cap)
        stream_ref = [None]
        done    = threading.Event()

        def _worker():
            try:
                s = self._make_completion(think, stream=True)
                stream_ref[0] = s
                for chunk in s:
                    if _CANCEL_EVENT.is_set():
                        try:
                            s.close()
                        except Exception:
                            pass
                        break
                    u = getattr(chunk, "usage", None)
                    if u:
                        usage[0] = u
                    if not chunk.choices:
                        continue
                    if getattr(chunk.choices[0], "finish_reason", None):
                        finish[0] = chunk.choices[0].finish_reason      # #1048: capture the terminal reason
                    chunk_q.put(chunk.choices[0].delta)
            except Exception as e:
                err[0] = e
            finally:
                done.set()

        t0 = time.time()
        t0_mono = time.monotonic()                       # #1544: monotonic base for the whole-request cap
        total_deadline = _total_request_deadline_s()     # #1544
        total_timed_out = False
        t_first = [None]          # OPT-3: time of the first token
        th = threading.Thread(                                  # bind the active ProjectContext into the worker (S3b):
            target=(_pc.bound_target(_worker) if _pc is not None else _worker), daemon=True)
        th.start()

        tf        = _ThinkFilter()
        tf_tool   = _ThinkFilter("<tool_call>", "</tool_call>")   # #1266: hide raw text tool-call markup from the render
        parts: List[str] = []
        tool_acc: Dict[int, Dict[str, str]] = {}
        prefix    = [False]
        tool_note = [False]   # B: one-time live hint during tool generation

        def _emit_line(line: str):
            if not prefix[0]:
                _ui_print(col("\n[GX10]", C.CYAN))
                prefix[0] = True
            _ui_print(line)
        renderer = _TableLineRenderer(_emit_line)

        cancelled = False
        while not (done.is_set() and chunk_q.empty()):
            if _CANCEL_EVENT.is_set():
                cancelled = True
                break
            if total_deadline > 0 and time.monotonic() - t0_mono > total_deadline:
                # #1544: hard whole-request wall-clock cap. A slow-drip stream (a chunk every < idle_limit AND
                # < the httpx read timeout) evades the idle watchdog + read timeout and would otherwise hold the
                # turn + agent lock forever. Close the upstream stream to unblock the (possibly httpx-blocked)
                # worker, then bail with a NAMED cause (never a silent indefinite hold).
                total_timed_out = True
                _s = stream_ref[0]
                if _s is not None:
                    try:
                        _s.close()
                    except Exception:
                        pass
                break
            try:
                delta = chunk_q.get(timeout=0.1)
            except _q.Empty:
                continue
            self._last_progress = time.time()   # S2 (#1132): a streamed chunk is progress — reset the idle watchdog

            if t_first[0] is None:
                t_first[0] = time.time()
                self._first_token_seen = True

            if getattr(delta, "content", None):
                parts.append(delta.content)                       # RAW content — feeds the post-turn tool-call recovery
                renderer.feed(tf_tool.feed(tf.feed(delta.content)))   # #1266: strip <think> AND <tool_call> from the render
                if tf_tool.entered and not tool_note[0]:          # #1266: one-time hint for the TEXT tool-call path
                    tool_note[0] = True
                    _ui_print(col("  ⋯ tool call …", C.GRAY))

            if getattr(delta, "tool_calls", None):
                # B: fill dead time — as soon as tool tokens arrive (and no
                # visible text yet), show once that work is happening.
                if not prefix[0] and not tool_note[0]:
                    tool_note[0] = True
                    _ui_print(col("  ⋯ Qwen erzeugt Tool-Aufruf …", C.GRAY))
                for tcd in delta.tool_calls:
                    idx  = tcd.index if tcd.index is not None else 0
                    slot = tool_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tcd.id:
                        slot["id"] = tcd.id
                    fn = getattr(tcd, "function", None)
                    if fn:
                        if fn.name:
                            slot["name"] += fn.name
                        if fn.arguments:
                            slot["arguments"] += fn.arguments

        # The consumer releases the turn on cancel; the daemon worker may linger
        # until httpx read timeout. close() is only a best-effort wake-up.
        if _decoupled() and cancelled and stream_ref[0] is not None:
            try:
                stream_ref[0].close()
            except Exception:
                pass

        reasoning_open = tf.in_think
        renderer.feed(tf_tool.feed(tf.flush()))   # #1266: drain the think filter THROUGH the tool-call filter
        renderer.feed(tf_tool.flush())
        renderer.flush()
        if prefix[0]:
            _ui_print("")   # closing newline after streamed content

        t_end   = time.time()
        ttft    = (t_first[0] - t0) if t_first[0] else None
        metrics = {
            "ttft":  ttft,
            "gen":   (t_end - t_first[0]) if t_first[0] else None,
            "total": t_end - t0,
            "prompt_tokens":     getattr(usage[0], "prompt_tokens", None) if usage[0] else None,
            "completion_tokens": getattr(usage[0], "completion_tokens", None) if usage[0] else None,
            "finish_reason":     finish[0],                             # #1048: "length" ⇒ output truncated
            "in_think":          reasoning_open,
        }
        if total_timed_out:
            # #1544: the worker unblocks on the stream close and writes its own (stream-closed) err[0]; join it
            # first (bounded — it unblocks fast) so this NAMED timeout deterministically wins the assignment.
            th.join(timeout=2.0)
            err[0] = TimeoutError(
                f"request exceeded its total wall-clock budget of {total_deadline:.0f}s "
                f"(connection.request_timeout_s{' + first_token_timeout_s' if _decoupled() else ''}) — "
                f"the stream never terminated"
            )
        tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
        return "".join(parts), tool_calls, cancelled, err[0], metrics

    # ── Generation: without streaming (comparison/fallback) ──────
    def _generate_plain(self, think: bool) -> Tuple[str, List[Dict], bool, Optional[Exception], Dict[str, Any]]:
        err  = [None]
        res  = [None]
        done = threading.Event()

        def _worker():
            try:
                res[0] = self._make_completion(think, stream=False)
            except Exception as e:
                err[0] = e
            finally:
                done.set()

        t0 = time.time()
        th = threading.Thread(                                  # bind the active ProjectContext into the worker (S3b):
            target=(_pc.bound_target(_worker) if _pc is not None else _worker), daemon=True)
        th.start()
        while not done.is_set():
            if _CANCEL_EVENT.is_set():
                return "", [], True, None, {}
            done.wait(0.1)

        if err[0]:
            return "", [], False, err[0], {}

        t_end   = time.time()
        msg     = res[0].choices[0].message
        content = msg.content or ""
        tool_calls: List[Dict] = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id":        tc.id,
                    "name":      tc.function.name,
                    "arguments": tc.function.arguments,
                })
        usage   = getattr(res[0], "usage", None)
        metrics = {
            "ttft":  None,
            "gen":   t_end - t0,
            "total": t_end - t0,
            "prompt_tokens":     getattr(usage, "prompt_tokens", None) if usage else None,
            "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "finish_reason":     getattr(res[0].choices[0], "finish_reason", None),   # #1048
            "in_think":          False,
        }
        disp = clean(content)
        if disp:
            _ui_print(col("\n[GX10]", C.CYAN))
            r = _TableLineRenderer(lambda ln: _ui_print(ln))
            r.feed(disp)
            r.flush()
        return content, tool_calls, False, None, metrics

    def _print_turn_end(self, turn: Dict[str, Any], outcome: Dict[str, Any]):
        """#3: ONE deterministic completion marker at the turn end — on EVERY
        exit path (success, abort, error, max-iter, internal crash).
        Guaranteed via try/finally in run(), so "ready for input" never
        fails to appear. IMPORTANT: no \\n INSIDE col() — otherwise the
        color code is cut off on the line split; hence the blank line separately."""
        dt   = time.time() - turn["t0"]
        kind = outcome.get("kind", "done")
        marks = {
            "done":  ("✓ DONE",          C.GREEN),
            "abort": ("⚠ CANCELLED",     C.YELLOW),
            "error": ("✗ ERROR",         C.RED),
            "max":   (f"⏱ MAX-ITER ({_loop_profile().max_iterations})", C.YELLOW),   # #602 SUB-8a: the profile bound (== MAX_ITERATIONS by default)
            "crash": ("✗ ERROR (internal)", C.RED),
            "stalled": ("⏱ TURN ABORTED (model stalled — no progress)", C.YELLOW),   # S2 (#1132): never a silent hang
        }
        label, color = marks.get(kind, marks["done"])
        detail = outcome.get("detail") or ""
        if detail:
            detail = " · " + detail.replace("\n", " ")[:80]
        _ui_print("")   # spacing as its own line
        _ui_print(col(
            f"  ======== {label} · ready · "
            f"{turn['gens']} gen · {dt:.0f}s · {turn['completion']} tok{detail} ========",
            color))

    # ── Agent loop ────────────────────────────────────────────
    def run(self, user_input: str):
        _CANCEL_EVENT.clear()
        # #1049 (L3): remember this turn so a mid-turn eviction can bias the rolling summary toward the
        # current task (read in _summarize via the instance; fail-soft to the generic instruction when
        # empty). Set at entry, before any work that could trigger a summarize.
        self._current_user_turn = user_input or ""
        self._turn_gen_errored = False   # #1050 (L3): reset the per-turn generation-error flag (guards the emergency summarize)
        self._finalized_this_turn = False
        self._summaries_this_turn = 0            # #1051 (L3): reset the shared per-turn summarize counters
        self._summary_tokens_this_turn = 0
        # B2: per-turn retrieval BEFORE the append (query = user message, dedup against the existing
        # window). FLAG OFF → "" → the user message is appended verbatim (byte-identical).
        rag = self._retrieve_context(user_input)
        # #459: PROACTIVELY steer a current-info request to web_search (only when one is available), so the
        # model doesn't improvise a shell web fetch. A per-turn hint folded into the message (like rag) —
        # transient, non-accumulating; the model still decides.
        steer = _websearch_steer(user_input)
        # #602 S602-6: an opt-in pre-turn hint of known working approaches (process-lessons); "" by default
        # → the prefix is byte-identical (transient + non-accumulating, like rag/steer).
        prefix = "\n\n".join(p for p in (rag, steer, _process_hint()) if p)
        # #1225 (S3): the AUTHORITATIVE steering-state block is kept as EXACTLY ONE current message — drop any
        # stale copy from earlier turns, then append the fresh one right before this user turn. So a project/
        # unit switch can never leave an obsolete "authoritative" block in history. "" when nothing is bound →
        # no message added → byte-identical.
        self.messages = [m for m in self.messages
                         if not (isinstance(m.get("content"), str)
                                 and m["content"].startswith(_STEERING_MARKER))]
        steer_state = _steering_state_block()
        if steer_state:
            self.messages.append({"role": "user", "content": steer_state})
        self.messages.append({"role": "user",
                              "content": (prefix + "\n\n" + user_input) if prefix else user_input})
        # #602 2.0/#690: publish the turn-start boundary (observer-only; byte-identical with no subscriber).
        _emit_hook("pre_turn", {"user_input": user_input, "agent": self})

        # auto mode: decide once per turn whether iteration 0 thinks
        self._turn_think = self._classify_thinking(user_input)

        turn = {"t0": time.time(), "gens": 0, "prompt": 0, "completion": 0}
        # Turn outcome — ALWAYS printed as a status line in finally.
        outcome: Dict[str, Any] = {"kind": "max"}

        # S2 (#1132): per-turn IDLE watchdog. Reset on every progress signal (a generation chunk, a completed
        # generation, a tool result); if nothing progresses for TURN_IDLE_TIMEOUT_S the turn is aborted and
        # surfaced as 'stalled' — never a silent indefinite hold of the agent lock. Off (<=0) ⇒ byte-identical.
        self._last_progress = time.time()
        self._first_token_seen = False
        self._watchdog_tripped = False
        _wd_stop = threading.Event()
        _wd_thread: Optional[threading.Thread] = None
        if TURN_IDLE_TIMEOUT_S and TURN_IDLE_TIMEOUT_S > 0:
            def _watchdog():
                while not _wd_stop.wait(min(5.0, TURN_IDLE_TIMEOUT_S / 4.0)):
                    limit = _idle_limit(self._first_token_seen)
                    if time.time() - self._last_progress > limit:
                        self._watchdog_tripped = True
                        _CANCEL_EVENT.set()   # break every poll site → the turn ends (abort → relabelled 'stalled')
                        break
            _wd_thread = threading.Thread(target=_watchdog, daemon=True)
            _wd_thread.start()

        try:
          # #602 SUB-8a: the chat loop's iteration bound comes from the default loop profile — which is the
          # global MAX_ITERATIONS unless an operator configured loop_profiles (byte-identical by default).
          loop_max = _loop_profile().max_iterations
          for iteration in range(loop_max):
            if _CANCEL_EVENT.is_set():
                outcome = {"kind": "abort"}
                return

            self._trim_context()

            think = self._think_for(iteration)
            label = "Qwen (planning)" if think else "Qwen (running)"
            _ui_print(col(f"  [{label}]", C.GRAY))

            spinner = Spinner(label)
            spinner.start()
            content, tool_calls, cancelled, err, metrics = self._generate(think)
            spinner.stop()
            self._last_progress = time.time()   # S2 (#1132): a completed generation is progress (covers non-stream)

            if not cancelled:   # #1060: telemetry — record every real generation (success + error), skip aborts
                try:
                    import telemetry as _tel
                    _m = metrics or {}
                    _tel.record_turn(latency_s=_m.get("total") or 0.0,
                                     prompt_tokens=_m.get("prompt_tokens") or 0,
                                     completion_tokens=_m.get("completion_tokens") or 0,
                                     ok=not err, ts=time.time())
                except Exception:   # noqa: BLE001 — telemetry must never break a turn
                    pass

            if cancelled:
                if _should_persist_partial(
                    _decoupled(),
                    self._watchdog_tripped,
                    self._first_token_seen,
                    (metrics or {}).get("in_think"),
                ):
                    partial = _partial_assistant_message(content)
                    if partial:
                        self.last_response = partial["content"]
                        self.messages.append(partial)
                outcome = {"kind": "abort"}
                return
            if err:
                outcome = _generation_error_outcome(self.stream, err, self._first_token_seen)
                return
            if _should_finalize_truncation(
                FINALIZE_ON_TRUNCATION,
                self._finalized_this_turn,
                tool_calls,
                (metrics or {}).get("finish_reason"),
                content,
            ):
                self._finalized_this_turn = True
                _ui_print(col("  [reasoning bound reached -- forcing a final answer]", C.GRAY))
                gen1_metrics = metrics
                _accumulate_generation_metrics(self._perf, turn, gen1_metrics)
                nudge = {"role": "user", "content": _REASONING_FINALIZE_NUDGE}
                self.messages.append(nudge)
                finalize_label = "Qwen (finalizing)"
                finalize_spinner = Spinner(finalize_label)
                finalize_spinner.start()
                try:
                    content, tool_calls, cancelled, err, metrics = self._generate(think=False)
                finally:
                    finalize_spinner.stop()
                    try:
                        for _i in range(len(self.messages) - 1, -1, -1):
                            if self.messages[_i] is nudge:
                                del self.messages[_i]
                                break
                    except Exception:  # noqa: BLE001 — nudge cleanup must never break a turn
                        pass
                self._last_progress = time.time()   # the finalize generation is progress
                if cancelled:
                    outcome = {"kind": "abort"}
                    return
                if err:
                    outcome = _generation_error_outcome(self.stream, err, self._first_token_seen)
                    return
                try:
                    import telemetry as _tel
                    _m = metrics or {}
                    _tel.record_turn(latency_s=_m.get("total") or 0.0,
                                     prompt_tokens=_m.get("prompt_tokens") or 0,
                                     completion_tokens=_m.get("completion_tokens") or 0,
                                     ok=True, ts=time.time())
                except Exception:   # noqa: BLE001 — telemetry must never break a turn
                    pass
            self._first_token_seen = True        # post-generation work uses the tight idle limit in stream and non-stream

            # Model-agnostic fallback: if the endpoint returned no native tool_calls,
            # try to recover any the model emitted as text (no server-side tool parser).
            recovered_from_text = False
            if not tool_calls and content:
                recovered = _extract_tool_calls_from_text(
                    content, {t["function"]["name"] for t in _effective_tools()})
                if recovered:
                    tool_calls = recovered
                    recovered_from_text = True

            # OPT-3: perf line + accumulation (session + this turn)
            if _accumulate_generation_metrics(self._perf, turn, metrics):
                self._perf["last"]        = self._fmt_perf(metrics)
                _ui_print(col("  " + self._perf["last"], C.GRAY))

            # PERF-02: persist ONLY the cleaned content (no <think>). When the
            # tool call was recovered from the text, `content` is the call marker
            # itself — don't duplicate it as assistant.content (confuses some templates).
            cleaned = "" if recovered_from_text else clean(content)
            if cleaned:
                self.last_response = cleaned   # #1048: track the latest model text so write_last_reply can persist it escape-free
            msg_dict: Dict[str, Any] = {"role": "assistant", "content": cleaned or None}
            if tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id":       t["id"] or f"call_{iteration}_{i}",
                        "type":     "function",
                        "function": {
                            "name":      t["name"] or "",
                            # the arguments stored in history MUST be valid JSON — vLLM json.loads() them on
                            # the next request; a malformed string would hard-400 the reask (defeats reask).
                            "arguments": _valid_tool_args_json(t["arguments"]),
                        },
                    }
                    for i, t in enumerate(tool_calls)
                ]
            self.messages.append(msg_dict)
            # #602 2.0/#690: publish the generation boundary — the Verifier (2.1) feed site.
            _emit_hook("post_generate", {"content": cleaned, "tool_calls": tool_calls,
                                         "iteration": iteration, "metrics": metrics, "agent": self})

            if not tool_calls:
                self.last_response = cleaned
                outcome = {"kind": "done"}
                return

            _ui_print()
            for i, t in enumerate(tool_calls):
                # Guard 1 (#1131): honour cancellation BETWEEN tool calls — but never leave the assistant
                # tool_calls message with missing tool rows (an orphan → a hard vLLM 400 on the next send). So
                # on cancel, emit a 'cancelled' result for THIS + every remaining call, then end the turn cleanly.
                if _CANCEL_EVENT.is_set():
                    for j in range(i, len(tool_calls)):
                        self.messages.append({"role": "tool",
                                              "tool_call_id": (tool_calls[j]["id"] or f"call_{iteration}_{j}"),
                                              "content": "ERROR: cancelled"})
                    outcome = {"kind": "abort"}
                    return
                name = t["name"] or ""
                tcid = t["id"] or f"call_{iteration}_{i}"
                # Validate→Reask at the tool boundary: malformed JSON or a schema
                # violation is fed back as the tool result so the model re-emits,
                # instead of silently degrading to empty args.
                args, arg_err = _parse_tool_args(name, t["arguments"])
                if arg_err:
                    _ui_print(col(f"  ● {name}", C.GRAY))
                    _ui_print(col(f"  ⎿ ✗ {arg_err}", C.RED))
                    self.messages.append({
                        "role": "tool", "tool_call_id": tcid,
                        "content": f"ERROR: {arg_err}",
                    })
                    continue

                _ui_print(col(f"  ● {_tool_display(name, args)}", C.GRAY))

                cap = self._live_read_budget()                        # #994-S16/#1046: LIVE per-turn budget
                _rb = _READ_BUDGET_CV.set(cap)                        # read_file reads it for its own cap
                try:
                    raw_result = None   # #1196: pre-strip raw result for the display (set for ingestion tools)
                    result_failed = False
                    if name == "write_last_reply":
                        # #1048 (L1-write): escape-free authoring — persist the model's PREVIOUS reply text
                        # (already produced as ordinary output) instead of a huge JSON-escaped write_file
                        # 'content'. Delegates to write_file so path resolution / the local bridge / append
                        # all apply. Returns a short status (not ingested content) → no _cap.
                        body = self.last_response or ""
                        if not body:
                            result_t = ("ERROR: no previous reply to write — produce the file body as your "
                                        "message text FIRST, then call write_last_reply(path).")
                        else:
                            result_t = run_tool("write_file", {"path": args.get("path", ""),
                                                               "content": body,
                                                               "mode": args.get("mode", "write")})
                        result_failed = result_t.startswith("ERROR")
                    else:
                        # #1202: run_tool itself renders a listing's AnswerData into the localized Answer
                        # (command-gated, all topologies) — so the machine line is already resolved here,
                        # BEFORE the cap/fence below, for every caller.
                        result_t = run_tool(name, args)
                        result_failed = result_t.startswith("ERROR")
                        # #1196: keep the RAW (possibly ANSI-coloured, e.g. `ls --color`) result for the
                        # DISPLAY, and STRIP escapes for the model context + cap/fence below — the model reads
                        # clean text (escape bytes are noise and skew the char count), the user sees colour.
                        # SCOPED to execute_command (where our coloured listing default comes from): a
                        # read_file/search_files/fetch_url result that legitimately CONTAINS escape bytes
                        # (a terminal capture, ANSI-art, a colour-coded log) reaches the model verbatim, as
                        # before — we never silently alter ingested file content.
                        raw_result = result_t
                        if name == "execute_command":
                            result_t = _strip_ansi(result_t)
                        # #1046 (L1-choke): cap EVERY character-capped ingestion result at this choke point —
                        # just read_file (which caps itself) but search_files/list_directory/execute_command
                        # AND the local-bridge return (which bypasses read_file's cap). Idempotent + scoped;
                        # structured/provider/plugin/memory results deliberately bypass this destructive cap.
                        result_t = _cap_ingested_result(name, result_t, cap)
                        # #1464 F3b: ONE mandatory post-serialization fence for every untrusted source,
                        # including web/provider/plugin/MPR/memory results. A wrapper failure returns only
                        # a safe error; raw content is never appended to the model context.
                        result_t = _fence_untrusted_result(name, result_t)
                        result_failed = result_failed or result_t.startswith("ERROR")
                    # #1048: warn-only integrity guard — if the generation that EMITTED this write was cut off
                    # by the token limit (finish_reason=length), the body may be silently truncated (a
                    # char-count can't detect it). Warn + steer to append; never block.
                    if (name in ("write_file", "write_last_reply")
                            and (metrics or {}).get("finish_reason") == "length"
                            and not result_t.startswith("ERROR")):
                        result_t += ("\n\n[Ironclad: WARNING — the generation that produced this write was cut "
                                     "off by the token limit (finish_reason=length); the file may be truncated. "
                                     "Continue it with mode='append'.]")
                finally:
                    _READ_BUDGET_CV.reset(_rb)
                _ok = not result_failed
                # #1196: DISPLAY the raw (coloured) result for ingestion tools (e.g. `ls --color`), else the
                # final result_t (carries write-warnings etc.). A line that carries its OWN colour streams
                # as-is (native ls colours — the prefix stays plain so the client still parses the block); a
                # plain line gets the default GRAY/RED tool-result styling. The MODEL sees result_t (stripped).
                _disp = raw_result if (raw_result is not None and name == "execute_command") else result_t
                for _ln in _tool_result_lines(_disp):
                    if _ok and _has_ansi(_ln):
                        _ui_print(_ln)
                    else:
                        _ui_print(col(_ln, C.GRAY if _ok else C.RED))

                self.messages.append({
                    "role":         "tool",
                    "tool_call_id": tcid,
                    "content":      result_t
                })
                self._last_progress = time.time()   # S2 (#1132): a tool result is progress — reset the idle watchdog
                # #1051 (L3): proactive cumulative-ingestion accountant — proactively shed the oldest tool
                # rounds via a query-aware summary once ingestion crosses the soft mark (default OFF ⇒ no-op).
                self._proactive_roll_if_needed()
                # #602 2.0/#690: publish the tool-result boundary (ctx carries the tool name).
                _emit_hook("post_toolresult", {"tool": name, "args": args, "result": result_t,
                                               "agent": self})
          # Loop ran through normally → max iterations (outcome stays "max")
        except Exception as e:
            # Catches unexpected errors so the agent thread does NOT die
            # and the turn still gets a completion marker.
            outcome = {"kind": "crash", "detail": repr(e)}
        finally:
            _wd_stop.set()                          # S2 (#1132): stop the idle watchdog
            if _wd_thread is not None:
                _wd_thread.join(timeout=1.0)
            outcome = _finalize_outcome(outcome, self._watchdog_tripped, self._first_token_seen)
            _status["thinking"] = False   # toolbar back to idle (even on crash)
            self._print_turn_end(turn, outcome)

    # ── Manual commands ──────────────────────────────────────
    def manual_read(self, path: str) -> str:
        result = run_tool("read_file", {"path": path})
        if result.startswith("ERROR"):
            return col(result, C.RED)
        self.messages.append({
            "role":    "user",
            "content": f"DATEIINHALT {path}:\n```\n{result}\n```"
        })
        return col(f"[OK] {path} loaded into the context", C.GREEN)

    def manual_write(self, path: str) -> str:
        if not self.last_response:
            return col("[ERROR] no previous response!", C.RED)
        r = run_tool("write_file", {"path": path, "content": self.last_response})
        return col(r, C.GREEN if r.startswith("OK") else C.RED)

    def manual_cat(self, path: str) -> str:
        r = run_tool("read_file", {"path": path})
        if r.startswith("ERROR"):
            return col(r, C.RED)
        # MEM-20: fence the content (with the language from the extension) so markdown clients show it
        # as preserved, highlighted code instead of reflowing it as prose.
        return f"```{_lang_for_path(path)}\n{r}\n```"

    def manual_ls(self, path: str = ".") -> str:
        return run_tool("list_directory", {"path": path})

    def clear_context(self) -> str:
        system = next((m for m in self.messages if m["role"] == "system"), None)
        self.messages      = [system] if system else []   # drops the B1 summary block (a 2nd system msg)
        self.last_response = ""
        # MEM-12: also drop the warm rolling summary so /clear and /reset truly start clean
        # (otherwise a stale/contradictory summary resurrects). Fail-soft; Cold/Mem0 untouched.
        if _WARM is not None:
            try:
                _WARM.del_session(_active_warm_session(), "summary")
            except Exception:  # noqa: BLE001
                pass
        return col("[OK] context reset (the system prompt stays).", C.YELLOW)

    def context_report(self) -> str:
        """MEM-13: read-only diagnosis of what context is currently injected — the rolling summary
        block (B1) and the last per-turn retrieved block (B2) — so you can tell whether a wrong
        answer comes from a stale summary vs bad retrieval. Plus the recovery hints."""
        lines = [col("  Injected context (diagnosis):", C.GRAY)]
        summ = next((m for m in self.messages if m.get("role") == "system"
                     and str(m.get("content") or "").startswith(_SUMMARY_MARKER)), None)
        if summ:
            body = str(summ["content"]).split("\n", 1)[1] if "\n" in str(summ["content"]) else ""
            lines.append(col(f"  - rolling summary ({len(body)} chars): {body[:400]}", C.GRAY))
        else:
            lines.append(col("  - rolling summary: (none)", C.GRAY))
        last_user = next((m for m in reversed(self.messages) if m.get("role") == "user"), None)
        rag = ""
        cu = str(last_user.get("content") or "") if last_user else ""
        if _RAG_MARKER in cu:
            rag = cu[cu.index(_RAG_MARKER):].split("\n\n", 1)[0]
        lines.append(col(f"  - last retrieved block: {rag[:400] if rag else '(none)'}", C.GRAY))
        lines.append(col("  Tip: 'clear'/'reset' clears window+summary (keeps long-term memory) · "
                         "'rag off' disables retrieval · purge a wrong fact point-level only (never blind).", C.GRAY))
        return "\n".join(lines)

    def status(self) -> str:
        chars     = sum(len(str(m.get("content") or "")) for m in self.messages)
        tool_msgs = sum(1 for m in self.messages if m.get("role") == "tool")
        p         = self._perf
        avg_tps   = (p["completion"] / p["wall"]) if p["wall"] > 0 else 0.0
        return "\n".join([
            col(f"  Model        : {self.model}",                C.GRAY),
            col(f"  Streaming    : {'on' if self.stream else 'off'}", C.GRAY),
            col(f"  Platform     : {self.platform}",              C.GRAY),
            col(f"  Onboarding   : {'on' if self.onboarding else 'off'}", C.GRAY),
            col(f"  Autopilot    : {('on (max=' + str(AUTOPILOT_MAX_CONCURRENT) + (', stream' if AUTOPILOT_STREAM else '') + (', replan' if AUTOPILOT_AUTOPLAN else '') + ')') if AUTOPILOT_ENABLED else 'off'}", C.GRAY),
            col(f"  Thinking     : {self.thinking_mode}",         C.GRAY),
            col(f"  max_tokens   : {self.max_tokens}",            C.GRAY),
            col(f"  Messages     : {len(self.messages)}",         C.GRAY),
            col(f"  Chars        : {chars}",                      C.GRAY),
            col(f"  Tool Results : {tool_msgs}",                  C.GRAY),
            col(f"  Tools active : {len(_effective_tools())}",    C.GRAY),
            col(f"  Perf         : {p['gens']} Gens · prompt {p['prompt']} · "
                f"completion {p['completion']} tok · ⌀ {avg_tps:.0f} tok/s", C.GRAY),
            col(f"  Last gen     : {p['last'] or '—'}",            C.GRAY),
            col(f"  Parser       : qwen3_coder (native)",            C.GREEN),
        ])

# ─── Hilfe ────────────────────────────────────────────────────
HELP = """
  Manual commands:
    read <file>      load a file into the context
    write <path>     save the last reply
    cat <path>       show a file
    ls [dir]         list a directory
    clear            clear the context (the prompt stays)
    status           context info (incl. streaming/thinking/max_tokens)
    prompts          list the loaded prompt-library items (name, languages, description)
    <prompt-name>    run a prompt item directly: /<name> [var=value …] [--lang xx] (see /prompts)
    skills           list the loaded skills (playbooks + typed tools, incl. MPR)
    config           the effectively-loaded CLI config + source
    config get <key>          read a dotted config key (e.g. mpr.enabled)
    config set <key> <value>  override a dotted config key at runtime
                              (on|off|true|false|num|str; e.g. mpr.enabled on)
    quality reset    clear a latched output-quality staging hold
    tool <name> <args|text>   run a tool DIRECTLY/deterministically (no model election, no RAG);
                              text → first required arg, or {json}. e.g. tool mpr_research <frage>
    rag on|off       toggle per-turn retrieval (RAG) for this session
    context          show the context-budget report
    fork [unit]      show the MPR architecture-decision proposal at a fork (recommendation only — you decide)
    ace warmup --ledger <path>   offline warm-start the active playbook from a dev-loop ledger's history
    ace eval --ledger <path>     efficiency diagnostic: ACE vs full-rewrite/evolutionary (J-001/J-002)
    generate <args>  scaffold a paved-road capability into the active project library
    project new <name> [--path <dir>]   create + activate a project (the guided setup command)
    project list | use <slug> | active | track new|use|list | delete <id> [--purge] | archive|unarchive <id>
                  manage registered, isolated projects (artefact home under vault/<slug>/)
    initiative …  deprecated alias for /project (kept one release)
    switch <project_id>   rebind this engine to a project (own paths + memory partition)
    watcher on|off        deprecated alias for /auto on|off
    autopilot on|off      toggle autopilot (auto-launch of Claude)
    autoplan on [N]       autonomous planning (optional: max N tasks, then stop)
    autoplan off          stop autoplan + reset the counter
    log-terminal on|off   open a live-log window on every autopilot start
    help / exit

  Everything else → agent loop
"""

# ─── `config` command: authoritative, effectively loaded configuration ──
def _render_config() -> str:
    """Shows the EFFECTIVELY loaded config (real values, not docs/prompt) +
    source. Deterministic, no LLM. Secrets are not printed."""
    c = _EFFECTIVE_CFG or _code_defaults()
    conn = c["connection"]; gen = c["generation"]; ctx = c["context"]
    pl = c["platform"]; pa = c["paths"]; ta = c["thinking_auto"]
    ws = c["workspace"]; wa = c["watcher"]; tk = c["tasks"]
    ob = c["onboarding"]; ap = c["autopilot"]; ui = c["ui"]
    key_env = conn.get("api_key_env", "GX10_API_KEY")
    key_state = "set" if os.environ.get(key_env) else "not set"
    return "\n".join([
        col(f"  source        : {_CFG_SOURCE if _CFG_SOURCE else '— (code defaults)'}", C.GREEN),
        col(f"  connection    : {conn['base_url']} · {conn['model']}", C.GRAY),
        col(f"  api-key       : from env {key_env} ({key_state})", C.GRAY),
        col(f"  platform      : {PLATFORM} (mode={pl['mode']})", C.GRAY),
        col(f"  paths         : prompt={pa['system_prompt']} · workdir={pa['workdir']} · state={pa.get('state_root','.ironclad')} · vault={pa.get('vault_root','vault')} · session={pa['session_file']}", C.GRAY),
        col(f"  generation    : temp={gen['temperature']} · max_tokens={gen['max_tokens']} · thinking={gen['thinking_mode']} · stream={gen['stream']} · retry={gen['retry_backoff']}", C.GRAY),
        col(f"  context       : iter={ctx['max_iterations']} · ctx={ctx['max_ctx_chars']} · trim={ctx['trim_target_chars']} · file_cap={ctx['max_file_chars']} · list_cap={ctx['list_dir_hard_cap']}", C.GRAY),
        col(f"  tasks         : dedup_threshold={tk['dedup_threshold']}", C.GRAY),
        col(f"  onboarding    : {bool(ob['enabled'])}", C.GRAY),
        col(f"  autopilot     : enabled={bool(ap['enabled'])} · claude={ap['claude_bin']} · max_concurrent={ap['max_concurrent']} · effort={ap['default_effort']} · stream={bool(ap.get('stream',False))} · terminate={bool(ap.get('terminate_on_advance',False))} · autoplan={bool(ap.get('autoplan',False))} · log_terminal={bool(ap.get('log_terminal',False))}", C.GRAY),
        col(f"  watcher       : enabled={bool(_WATCHER_ENABLED)} · interval={wa['interval']}s · dir={wa['feedback_dir']}", C.GRAY),
        col(f"  thinking_auto : {len(ta['planning_keywords'])} planning / {len(ta['routine_keywords'])} routine keywords", C.GRAY),
        col(f"  workspace     : {len(ws['dirs'])} dirs", C.GRAY),
        col(f"  ui            : max_lines={ui['max_lines']} · refresh={ui['refresh_interval']}s", C.GRAY),
        col(f"  Precedence    : code-defaults < file/conf < env", C.GRAY),
    ])


# ─── Runtime config control (/config get|set) ─────────────────
# Generic, plugin-agnostic runtime override of the live config tree. `/config set <dotted.key> <value>`
# clones the merged config, validates and derives it completely, then commits globals + integrations before
# publishing the candidate. Plugin sections (e.g. MPR) take effect on their next call after the same commit.
# Secret-free + no plugin-specific knowledge here — see docs/config-runtime.md.
#
# Frozen keys are BOOT-ONLY: they wire something at startup (e.g. the offload runner for `setup.type`),
# so a runtime mutation would be incoherent. `/config set` refuses them; `/config get` still reads them.
# Boot-only keys: read once at startup to wire the runner/topology (setup.type) or the trust policy
# + the effective bind host (security.profile, e.g. sealed→loopback). A runtime change would
# NOT re-wire the already-built dispatcher/policy/socket → `/config set` refuses it
# with the boot-only message. Set it in the deploy config + restart. See config-runtime.md.
_FROZEN_CONFIG_KEYS = config_schema.BOOT_ONLY_KEYS

_CONFIG_TOMBSTONES = {
    key: spec.reason for key, spec in config_schema.TOMBSTONES.items() if not spec.alias
}

_CONFIG_ALIASES = {
    key: spec.replacement for key, spec in config_schema.TOMBSTONES.items() if spec.alias
}

_ENV_TOMBSTONES = {
    "GX10_TOOLING_ENVELOPE_ENABLED": "tooling authorization is always on",
    "GX10_AUDIT_ENABLED": "mutating-action audit is always on",
    "GX10_INJECTION_DEFENSE": "injection fencing is always on",
    "GX10_EGRESS_ANALYSIS_ENABLED": "egress enforcement is always on",
    "GX10_AMBIGUITY_DETECT": "the no-guessing ambiguity gate is always on",
    "GX10_PROVIDERS": "setup.type is the single provider-topology authority",
}

_SANDBOX_POLICIES = frozenset({"auto", "bwrap", "firejail"})
_RETIRED_SANDBOX_POLICIES = frozenset({"off", "none"})


def _validated_sandbox_policy(value: Any) -> str:
    """Return a live sandbox policy or raise. Retired off/none are handled at their input boundaries."""
    if not isinstance(value, str):
        raise ValueError("security.sandbox must be one of: auto, bwrap, firejail")
    policy = value.strip().lower()
    if policy not in _SANDBOX_POLICIES:
        raise ValueError("security.sandbox must be one of: auto, bwrap, firejail")
    return policy


def _coerce_cfg_value(raw: str):
    """CLI string → bool/int/float/str. on|true|yes → True, off|false|no → False; else numeric, else str."""
    low = raw.strip().lower()
    if low in ("on", "true", "yes"):
        return True
    if low in ("off", "false", "no"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _cfg_set(cfg: Dict[str, Any], dotted: str, value) -> None:
    """Write a dotted key path into *cfg*, creating intermediate dict sections as needed."""
    keys = dotted.split(".")
    node = cfg
    for k in keys[:-1]:
        nxt = node.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            node[k] = nxt
        node = nxt
    node[keys[-1]] = value


def _cfg_get(cfg: Dict[str, Any], dotted: str):
    """Read a dotted key path from *cfg*; None if any segment is missing."""
    node: Any = cfg
    for k in dotted.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _consume_config_aliases(cfg: Dict[str, Any]) -> None:
    """Map one-release legacy leaves to their canonical keys, warn, and remove the aliases."""
    for legacy, canonical in _CONFIG_ALIASES.items():
        keys = legacy.split(".")
        node: Any = cfg
        parents: "List[tuple[Dict[str, Any], str]]" = []
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                break
            parents.append((node, key))
            node = node[key]
        else:
            if not isinstance(node, dict) or keys[-1] not in node:
                continue
            value = node[keys[-1]]
            _cfg_set(cfg, canonical, value)
            node.pop(keys[-1])
            for parent, key in reversed(parents):
                child = parent.get(key)
                if isinstance(child, dict) and not child:
                    parent.pop(key)
                else:
                    break
            print(col(f"  [DEPRECATED] config key '{legacy}' is an alias for '{canonical}'; "
                      "the alias will be removed after one release.", C.YELLOW))


def _config_tombstone_reason(dotted: str) -> Optional[str]:
    """Return the retirement reason for an exact tombstone or a leaf below a retired subtree."""
    if dotted in _CONFIG_TOMBSTONES:
        return _CONFIG_TOMBSTONES[dotted]
    return next((reason for retired, reason in _CONFIG_TOMBSTONES.items()
                 if dotted.startswith(retired + ".")), None)


def _consume_config_tombstones(cfg: Dict[str, Any]) -> None:
    """Warn once per loaded tree for retired leaves, remove them, and never apply their values."""
    for dotted, replacement in _CONFIG_TOMBSTONES.items():
        keys = dotted.split(".")
        node: Any = cfg
        parents: "List[tuple[Dict[str, Any], str]]" = []
        for key in keys[:-1]:
            if not isinstance(node, dict) or key not in node:
                break
            parents.append((node, key))
            node = node[key]
        else:
            if isinstance(node, dict) and keys[-1] in node:
                node.pop(keys[-1])
                print(col(f"  [DEPRECATED] config key '{dotted}' is retired and ignored; {replacement}.",
                          C.YELLOW))
                for parent, key in reversed(parents):
                    child = parent.get(key)
                    if isinstance(child, dict) and not child:
                        parent.pop(key)
                    else:
                        break
    for key, default in config_schema.CONTAINER_DEFAULTS.items():
        cfg.setdefault(key, copy.deepcopy(default))


def _cfg_flatten_keys(cfg: Dict[str, Any], prefix: str = "") -> "List[str]":
    """All dotted leaf keys of a config tree (e.g. ``context.rag_enabled``). Backs ``/config keys``
    discovery + the config-set unknown-root guard (#932)."""
    out: "List[str]" = []
    for k, v in cfg.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out.extend(_cfg_flatten_keys(v, dotted + "."))
        else:
            out.append(dotted)
    return out


# ─── Setup type (server | local) → offload runner wiring ──────
# Boot-only derivation: the setup.type (docs/setup-types.md) selects WHERE the orchestrator + agents run,
# and thus how the provider dispatcher's runner is wired. Orchestrator + agents are ALWAYS co-located
# (one machine) — no cross-machine offload. Generic + secret-free (role names only). Fail-CLOSED on a
# misconfigured topology (the server aborts boot rather than silently degrading).
#   server (default): everything on the model host → in-engine only (external agents deferred); byte-identical.
#   local:            engine + agents native on the desktop → offload = local subprocess (default_cli_runner);
#                     the model + memory live remotely (over the network), so base_url must be REMOTE.
#   auto:             INSTALL-1 (#503) — derive from base_url at boot: a loopback model is fully in-box
#                     (→ server/in-engine), a remote model is the LAN-offload desktop (→ local). Lets the
#                     desktop launcher ship a self-consistent default that boots without baking a host in.
_VALID_SETUP_TYPES = ("server", "local", "auto")


def _is_local_url(url: str) -> bool:
    """True if *url*'s host is loopback (localhost/127.0.0.1/::1) — used to reject a `local` setup that
    points at an in-box model instead of the remote model host."""
    import urllib.parse
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "")


def resolve_offload_topology(cfg: Dict[str, Any], *, cli_available: bool = True) -> Dict[str, Any]:
    """Derive the runner wiring from the boot-fixed ``setup.type`` (docs/setup-types.md).

    Returns ``{"setup_type", "providers_enabled", "runner_mode", "note"?}`` with
    ``runner_mode`` ∈ {``"none"``, ``"local"``}. Pure + testable (``cli_available`` injected). Raises
    ``ValueError`` (fail-closed) on an unknown or misconfigured topology. ``security.profile=sealed``
    forces ``server`` (no egress), overriding any setup.type.
    """
    setup_type = ((cfg.get("setup") or {}).get("type") or "server").strip().lower()
    if setup_type not in _VALID_SETUP_TYPES:
        raise ValueError(f"unknown setup.type={setup_type!r} (expected one of {', '.join(_VALID_SETUP_TYPES)}).")
    profile = (cfg.get("security") or {}).get("profile", "open")
    # sealed = no egress → no external agents, regardless of setup.type → force server/in-engine.
    if profile == "sealed" and setup_type != "server":
        return {"setup_type": "server", "providers_enabled": False, "runner_mode": "none",
                "note": f"sealed profile forces server (no egress); setup.type={setup_type} ignored"}
    base_url = (cfg.get("connection") or {}).get("base_url", "")
    auto_note = None
    if setup_type == "auto":
        # INSTALL-1 (#503): a fresh desktop default ships a loopback base_url; derive the topology so it
        # BOOTS out of the box — a loopback model is fully in-box (server/in-engine), a remote model is the
        # LAN-offload desktop (local). No host is baked into the repo; the user just points GX10_BASE_URL.
        setup_type = "server" if _is_local_url(base_url) else "local"
        auto_note = (f"setup.type=auto → {setup_type} "
                     f"({'loopback' if setup_type == 'server' else 'remote'} base_url)")
    if setup_type == "server":
        out = {"setup_type": "server", "providers_enabled": False, "runner_mode": "none"}
        if auto_note:
            out["note"] = auto_note
        return out
    # local
    if _is_local_url(base_url):
        raise ValueError("setup.type=local requires a REMOTE base_url (the model runs on the GPU host; "
                         "the engine co-locates with the code CLIs). Got a loopback endpoint — set "
                         "GX10_BASE_URL to the remote model.")
    if not cli_available:
        raise ValueError("setup.type=local requires a reachable agent CLI on this host (none found via "
                         "PATH). Install it or set GX10_CLAUDE_BIN/GX10_AGENT_CMD.")
    out = {"setup_type": "local", "providers_enabled": True, "runner_mode": "local"}
    if auto_note:
        out["note"] = auto_note
    return out


# ─── Dispatcher ───────────────────────────────────────────────
def _initiative_command(arg_str: str) -> str:
    """`/initiative new|list|use|active|reconcile` — deterministic CLI control of the
    initiative-centric vault. Pure bookkeeping (no model call). Errors (unknown type,
    no active/known initiative) are returned as a clear, fail-closed message."""
    parts = arg_str.split()
    sub = parts[0].lower() if parts else "list"
    rest = arg_str[len(parts[0]):].strip() if parts else ""
    try:
        if sub == "new":
            # #984: MPR is an embedded dev-process function, not a project type — there is one type
            # (software). `--type` is dropped; a stray `--type <val>` is tolerated + ignored (back-compat).
            rest = re.sub(r"--type[=\s]+\S+", "", rest).strip()
            if not rest:
                return "usage: /initiative new <name>"
            v = initiative_new(rest)
            # Name the artefacts ACTUALLY seeded (derived from the skeleton, so the message can never drift).
            visible = ", ".join(sorted(
                {d.split("/")[0] for d in _INITIATIVE_SKELETON[v.type] if not d.startswith(WORKFLOW_DIR)}
            ))
            return _msg("init.cmd_created", slug=v.slug, type=v.type, path=v.path.as_posix(), visible=visible)
        if sub == "use":
            if not rest:
                return "usage: /initiative use <slug>"
            v = initiative_use(rest)
            return _msg("init.cmd_active", slug=v.slug, type=v.type, path=v.path.as_posix())
        if sub == "list":
            vs = initiative_list()
            if not vs:
                return _msg("init.cmd_none")
            cur = active_slug()
            # #1238: the row marker must survive the client's markdown renderer — a leading "* " became a
            # generic "- " bullet, dropping the active marker. Use the existing "[…]" tag convention instead.
            lines = ["[initiative]  ([active] = current)"]
            for v in vs:
                tag = " [active]" if v.slug == cur else ""
                lines.append(f"- {v.slug}{tag}  ·  type {v.type} · status {v.status} · {v.created}")
            return "\n".join(lines)
        if sub == "active":
            v = initiative_active()
            return (_msg("init.cmd_active", slug=v.slug, type=v.type, path=v.path.as_posix())
                    if v else _msg("init.cmd_none_active"))
        if sub == "reconcile":
            fn = globals().get("reconcile_vault")          # Unit C provides the function
            if fn is None:
                return "[initiative] reconcile unavailable"
            if rest.strip() in ("all", "--all", "--all-tracks"):   # S12e cross-track sweep
                res = reconcile_all_tracks(links=True)
                lines = [f"[initiative] reconcile all ({len(res)} track(s)):"]
                for track in res:
                    lines.append(f"  [{track}] {len(res[track])} initiative(s)")
                    lines += [f"    - {r}" for r in res[track]]
                return "\n".join(lines)
            slug = rest.strip() or active_slug()
            if not slug:
                return _msg("init.cmd_reconcile_needs_slug")
            return f"[initiative] reconcile {slug}: {fn(slug)}"
        return ("usage: /initiative new <name> | list | use <slug> | "
                "active | reconcile [slug|all]")
    except (ValueError, RuntimeError) as e:
        return f"[initiative] {e}"


# ─── /lifecycle — the engine DELIVER-leg lifecycle-completeness gate (S13b / AD-7) ────────────────
# Wires the S13a primitives (project_evidence + lifecycle_completeness) + the pure transition projector
# (lifecycle_projector) into a FUNCTIONING, invokable gate: it reads the dev-process transition ledger
# as plain JSONL DATA (stdlib json — NEVER importing the private scripts/devprocess|devloop modules),
# projects each stage-bearing transition into tree_sha-bound vault evidence, then runs the completeness
# gate. Fail-closed: a missing/bad tree_sha, no resolvable slug, or an unreadable/tampered ledger →
# a clear BLOCKED / usage message, never a silent pass.
# Fork C: the DEFAULT required stages. The per-unit driver GATE/REVIEW transitions ARE now appended to the
# ledger (#830 wired the driver `log` seam to `ledger.append` in run.py), so `tests` (a green composed GATE)
# and `reviews` (an ENFORCED, non-inert review-evidence leg) are real, enforceable evidence. The default
# stays the conservative `delivery` — a delivery-completeness check shouldn't presume the full driver loop
# ran for that unit — while `tests`/`reviews` are enforced via `--stages tests,reviews,delivery`. The
# projector maps all three (lifecycle_projector); a dry-run/inert review is excluded from `reviews` (#830).
_LIFECYCLE_DEFAULT_STAGES = ("delivery",)
_LEDGER_GENESIS = "GENESIS"


def _ledger_canonical(obj: Any) -> str:
    """Canonical JSON of a ledger record / payload — mirrors ``scripts/devloop/ledger._canonical`` so the
    hash chain recomputes identically engine-side (the ledger is read as DATA, the private bare-runner
    module is never imported — core/ boundary)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ledger_hash(seq: Any, prev_hash: str, payload: Any) -> str:
    """``sha256(seq | prev_hash | canonical(payload))`` — mirrors ``scripts/devloop/ledger._hash``."""
    import hashlib
    return hashlib.sha256(f"{seq}|{prev_hash}|{_ledger_canonical(payload)}".encode("utf-8")).hexdigest()


def _read_ledger_payloads(path: Path) -> "tuple[List[Dict[str, Any]], List[str]]":
    """Read the hash-chain transition ledger at *path* as plain JSONL (stdlib only; one ``json.loads``
    per non-empty line) and return ``(payloads, chain_errors)``. ``payloads`` = each record's ``payload``
    (the driver/deliver dict, in file order). ``chain_errors`` = integrity violations recomputed by
    re-reading (seq gap, broken ``prev_hash`` link, tampered payload, or a non-JSON line) — non-empty ⇒
    the caller fails closed. A missing file is an empty ledger (no error)."""
    if not path.is_file():
        return [], []
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError as e:
        return [], [f"ledger unreadable: {e!r}"]
    payloads: List[Dict[str, Any]] = []
    errors: List[str] = []
    prev = _LEDGER_GENESIS
    for i, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except (ValueError, TypeError) as e:
            errors.append(f"record {i}: invalid JSON ({e})")
            continue
        if not isinstance(rec, dict):
            errors.append(f"record {i}: not a JSON object")
            continue
        if rec.get("seq") != i:
            errors.append(f"record {i}: seq {rec.get('seq')!r} (expected {i})")
        if rec.get("prev_hash") != prev:
            errors.append(f"record {i}: prev_hash break (chain reordered/truncated)")
        if _ledger_hash(rec.get("seq"), rec.get("prev_hash"), rec.get("payload")) != rec.get("hash"):
            errors.append(f"record {i}: hash mismatch (payload tampered)")
        prev = rec.get("hash")
        payload = rec.get("payload")
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads, errors


def _git_head_tree(root: "Optional[Path]" = None) -> str:
    """#933: resolve the git HEAD *tree* sha (the COMMITTED tree) as the default delivery tree for
    ``/lifecycle gate`` when ``--tree`` is omitted. A DELIBERATE, scoped exception to the 'no git/SHA in
    core' convention (see ``_orchestrator_version``): a single read-only ``git rev-parse HEAD^{tree}``,
    **fail-soft to ""** so the existing ``BLOCKED: no delivery tree_sha`` path still fires — it never binds a
    bogus tree. An explicit ``--tree`` (e.g. the operator's DELIVER-GO tree) always overrides this default,
    so the automated GO path is unaffected; this only gives the INTERACTIVE gate a sensible default."""
    try:
        cwd = str(root or _project_root() or Path.cwd())
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD^{tree}"],
                             capture_output=True, text=True, timeout=5)
        sha = (out.stdout or "").strip()
        return sha if out.returncode == 0 and re.fullmatch(r"[0-9a-f]{7,64}", sha) else ""
    except Exception:  # noqa: BLE001 — no git / no repo / timeout → fail-soft to "" (BLOCKED stands)
        return ""


def _dev_target_descriptor(root: "Optional[Path]" = None) -> "Optional[Dict[str, Any]]":
    """#974/#977: the active project's INJECTION descriptor — marks the project as running the INTERNAL
    (extension-driven) dev process rather than the normal (public DEV-1) one. Read as PLAIN DATA from
    ``<project_root>/.devloop/dev-target.json`` (mirroring the ledger read) — the engine NEVER imports the
    private ``scripts/devloop`` machinery (the tool<->process edge stays data-only). ``None`` when absent /
    unreadable / not an object. The fail-closed mutual-exclusion gate (#978) consumes this."""
    p = (root or _project_root() or Path.cwd()) / ".devloop" / "dev-target.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _dev_target_drift() -> "List[str]":
    """#977: fail-closed reconcile at the ``/lifecycle`` gate — if the active project carries an injection
    descriptor, its ``project_id`` MUST name a registered project. Returns [] when consistent, when there is
    no descriptor, or when the registry is unavailable (never block the gate on registry-read uncertainty —
    the #978 gate is the real enforcement). Inline (no private import)."""
    d = _dev_target_descriptor()
    if not d:
        return []
    pid = d.get("project_id")
    if not pid:
        return ["injection descriptor has no project_id"]
    try:
        ids = {p.id for p in _REGISTRY.list()} if _REGISTRY is not None else None
    except Exception:  # noqa: BLE001
        ids = None
    if ids is None:
        return []
    return [] if pid in ids else [f"injection descriptor names project {pid!r} which is not registered"]


def _active_is_internal_target() -> bool:
    """#979: True iff the active project is bound as an INTERNAL dev-process target (it carries an injection
    descriptor). Data-only (the plain-JSON marker); the engine never imports the private devloop machinery."""
    return _dev_target_descriptor() is not None


def _dev_target_status_line() -> str:
    """#982: a one-line operator view (for ``/status``) of the active project's dev-process mode — whether it
    is bound as an INTERNAL dev-process target (with its exec_mode / tier / plugin) or runs the NORMAL
    (public DEV-1) process. Data-only (reads the plain-JSON marker; the live plugin health is a run-time
    concern the internal driver checks, #978)."""
    d = _dev_target_descriptor()
    if not d:
        return "  dev-process : normal (public DEV-1) — no internal target bound"
    return (f"  dev-process : INTERNAL target — exec_mode={d.get('exec_mode')} tier={d.get('tier')} "
            f"plugin={d.get('plugin_id') or '-'}"
            f"{' (required)' if d.get('plugin_required') else ''}  · the normal in-engine pipeline is OFF")


def _internal_target_blocks_normal() -> "Optional[str]":
    """#979 mutual exclusion — the NORMAL (in-engine DEV-1) task pipeline (stage_handover / advance /
    TaskStore) must NOT run on a project bound as an INTERNAL dev-process target: the internal
    (extension-driven) process drives it instead. Returns a clear refusal iff the active project has an
    injection descriptor; otherwise None. The symmetric counterpart to the internal driver's #978 gate."""
    d = _dev_target_descriptor()
    if d:
        return (f"the normal dev-process pipeline is disabled on project {d.get('project_id')!r} — it is an "
                f"INTERNAL dev-process target (exec_mode {d.get('exec_mode')!r}); drive it with the internal "
                f"dev-loop, not the in-engine pipeline.")
    return None


def _lifecycle_command(arg_str: str) -> str:
    """`/lifecycle gate --slug <slug> --tree <sha> [--ledger <path>] [--stages tests,reviews,delivery]`
    (S13b / AD-7) — the functioning engine DELIVER-leg lifecycle-completeness gate (Fork A1, engine-side).
    Reads + chain-verifies the transition ledger as plain data, projects each stage-bearing transition
    into tree_sha-bound vault evidence via ``lifecycle_projector.project_transitions`` (wiring the REAL
    ``project_evidence`` + ``lifecycle_completeness``), and prints the projected evidence paths +
    ``READY`` / ``BLOCKED: <reasons>``. Defaults: ledger = ``<repo>/.devloop/ledger.jsonl`` (the active
    project root, else cwd); slug = ``active_slug()`` (Fork D); stages = ``delivery`` (Fork C — the
    conservative default; ``tests``/``reviews`` are now logged by the driver (#830) and enforced via
    ``--stages tests,reviews,delivery``). FAIL-CLOSED — a missing slug/tree_sha, an unreadable/tampered ledger, or a projection error
    yields a clear BLOCKED/usage message, never a silent pass."""
    parts = arg_str.split()
    sub = parts[0].lower() if parts else ""
    if sub != "gate":
        # #936: spec-derived guidance (single source — cannot drift from the command-spec)
        import command_spec as _command_spec
        return _command_spec.guided_usage("lifecycle") or (
            "usage: /lifecycle gate --slug <slug> --tree <sha> [--ledger <path>] [--stages tests,reviews,delivery]")
    rest = arg_str[len(parts[0]):].strip()

    def _flag(name: str) -> "Optional[str]":
        m = re.search(rf"--{name}[=\s]+(\S+)", rest)   # --x VALUE or --x=VALUE (position-independent)
        return m.group(1) if m else None

    if _lifecycle_projector is None:
        return "[lifecycle] BLOCKED: lifecycle projector unavailable (engine module not importable)"
    slug = (_flag("slug") or active_slug() or "").strip()
    # #933: --tree wins (e.g. the operator's DELIVER-GO tree); else default to the committed HEAD tree
    # (fail-soft to "" → the BLOCKED-no-tree path below still fires; never binds a diverging/bogus tree).
    tree_sha = (_flag("tree") or _git_head_tree()).strip()
    stages_arg = _flag("stages")
    required_stages = ([s.strip() for s in stages_arg.split(",") if s.strip()]
                       if stages_arg else list(_LIFECYCLE_DEFAULT_STAGES))
    if not slug:
        return "[lifecycle] BLOCKED: no slug — pass --slug <slug> or set an active initiative"
    if not tree_sha:
        return "[lifecycle] BLOCKED: no delivery tree_sha — pass --tree <sha>"
    if not required_stages:
        return "[lifecycle] BLOCKED: no required stages — pass --stages a,b,c"
    _drift = _dev_target_drift()   # #977: fail-closed if the active project's injection descriptor is stale
    if _drift:
        return "[lifecycle] BLOCKED: dev-target drift — " + "; ".join(_drift)
    ledger_arg = _flag("ledger")
    ledger_path = (Path(ledger_arg) if ledger_arg
                   else (_project_root() or Path.cwd()) / ".devloop" / "ledger.jsonl")
    payloads, chain_errors = _read_ledger_payloads(ledger_path)
    if chain_errors:
        return (f"[lifecycle] BLOCKED: ledger integrity failure ({ledger_path.as_posix()}): "
                + "; ".join(chain_errors[:5]))
    # M4-2 (#879): ACE dev-process self-learning — reuse the (valid) ledger we just read to submit each
    # newly-terminal unit's Trajectory off the hot path (advisory, fail-soft, exactly-once; does not affect
    # the gate result). The dev-loop's DELIVER-leg runs this gate, so it is the natural ledger touchpoint.
    _ace_scan_dev_ledger(payloads, chain_errors)
    # M5-2 (#883): ACE MPR-at-fork — dispatch each newly-declared ForkSignal in the same (valid) ledger to the
    # gated MPR architecture-decision panel off the hot path (gate OFF ⇒ no-op; exactly-once; fail-soft).
    _ace_scan_fork_signals(payloads, chain_errors)
    # M5-4 (#885): ACE fork-learn — turn each newly-RESOLVED fork into a fork-decision Trajectory (→ bullet via
    # the reflection worker) so the next comparable fork is pre-informed (gate OFF ⇒ no-op; exactly-once).
    _ace_scan_fork_resolutions(payloads, chain_errors)
    try:
        result = _lifecycle_projector.project_transitions(
            payloads, slug=slug, tree_sha=tree_sha, required_stages=required_stages,
            project_evidence=project_evidence, lifecycle_completeness=lifecycle_completeness)
    except (ValueError, RuntimeError, OSError) as e:   # fail-closed: never a silent pass on a projection error
        return f"[lifecycle] BLOCKED: {e}"
    lines = [f"[lifecycle] gate {slug} @ {tree_sha[:12]} "
             f"(ledger {ledger_path.as_posix()}, {len(payloads)} record(s); "
             f"stages: {', '.join(required_stages)})"]
    lines.append("  projected evidence:" if result["projected"] else "  projected evidence: (none)")
    lines += [f"    - {p}" for p in result["projected"]]
    if result["ready"]:
        lines.append("  READY — every required stage has evidence bound to the delivery tree_sha")
    else:
        lines.append("  BLOCKED: " + ("; ".join(result["reasons"]) or "(no reason given)"))
    return "\n".join(lines)


# ─── Project switch (ADR-0011 AD-1 / S5b-2) ───────────────────
class _SwitchAgent:
    """Adapts the live ``GX10`` agent to ``project_switch``'s contract. The BASE system prompt is
    project-INDEPENDENT (the engine's operating instructions) so it is preserved; everything else — the
    conversation AND the rolling-summary system block, which is memory-partition-specific — is dropped and
    rebuilt from the target. Mirrors ``clear_context``'s base-only reset, so nothing bleeds across a switch
    even if the target session is missing or corrupt."""

    def __init__(self, gx: "GX10") -> None:
        self._gx = gx

    def _reset_to_base_system(self) -> None:
        # Keep ONLY the first (base) system message — drops the leaving conversation AND the leaving
        # project's rolling-summary block (a 2nd system message); clears the stale last_response so a
        # post-switch /write can't emit the leaving project's last answer.
        base = next((m for m in self._gx.messages if m.get("role") == "system"), None)
        self._gx.messages = [base] if base else []
        self._gx.last_response = ""

    def save_session(self) -> None:
        self._gx.save_session(strict=True)   # a failed save of the LEAVING conversation aborts the switch

    def load_session(self) -> bool:
        # Drop the leaving conversation + summary FIRST, so a missing/corrupt target session (GX10's loader
        # leaves messages unchanged on a parse error) can never leave the leaving conversation live.
        self._reset_to_base_system()
        existed = session_path().exists()    # resolves under the TARGET ctx — the switch bound it first
        self._gx.load_session()               # append the target's non-system window onto the base system
        return existed

    def start_fresh(self, _prompt_path: str) -> None:
        # No target session → just the project-independent base system message (the prompt path is unused).
        self._reset_to_base_system()


class _CacheActiveRegistry:
    """A registry view whose ``active()`` returns THIS process's cached active project
    (``_ACTIVE_PROJECT``) — never the persisted continuity pointer, which may have been moved by another
    process. ``get()``/``set_active()`` delegate to the real installation-global registry (``set_active``
    persists the continuity pointer for the next boot)."""

    def __init__(self, reg) -> None:
        self._reg = reg

    def get(self, pid: str):
        return self._reg.get(pid)

    def active(self):
        return _ACTIVE_PROJECT

    def set_active(self, pid: str) -> None:
        self._reg.set_active(pid)


def _switch_apply_config(merged: Dict[str, Any]) -> None:
    """``apply_config`` seam for a ``/switch``: re-derive the engine globals from *merged* AND publish it as
    ``_EFFECTIVE_CFG`` so ``/config get`` and the rest of the engine see the active project's config. A failed
    re-derive restores the complete pre-switch runtime snapshot before the project switch rolls its context back."""
    global _EFFECTIVE_CFG
    with _CONFIG_LOCK:
        original = _EFFECTIVE_CFG
        snapshot = _snapshot_config_runtime()
        try:
            _apply_config(merged)
        except Exception:
            _restore_config_runtime(snapshot)
            _EFFECTIVE_CFG = original
            raise
        _EFFECTIVE_CFG = merged


def _project_overlay_for(project) -> Dict[str, Any]:
    """A project's config overlay. The per-project overlay source is a later spine step (S6+); today a
    switch changes only the per-project paths + memory partition (via the bound ctx), not the base config
    surface, so this returns ``{}`` — but ``apply_project_overlay``'s locked-key discipline is already on
    the path the moment an overlay exists."""
    return {}


def _project_in_flight(pid: str) -> bool:
    """Best-effort: is a dev unit currently holding *pid*'s project lock (a run in progress, possibly in
    another process)? Probes the OS lock non-blocking — held ⇒ in-flight (the switch is refused). Never
    raises (an unsafe id / missing registry ⇒ not in-flight)."""
    if _REGISTRY is None:
        return False
    try:
        lk = _REGISTRY.project_lock(pid, timeout_s=0.0)
    except Exception:   # noqa: BLE001 — unsafe id etc. → treat as not-in-flight (the switch will KeyError on get)
        return False
    try:
        lk.acquire()
        lk.release()
        return False
    except Exception:   # noqa: BLE001 — could not acquire ⇒ a holder is running ⇒ in-flight
        return True


def _switch_serialize():
    """#979: a repo-global lock context so concurrent ``/switch`` operations serialize — at most one active
    mode/project transition at a time (a second session waits, never races the quiesce/rebind). Fail-soft:
    if locking is unavailable, a no-op context (a missing lock must never block a switch). The boundary
    itself (normal-off on an internal target) is enforced by ``_internal_target_blocks_normal`` on the next
    pipeline op AFTER the switch commits, so this only serializes the transition."""
    import contextlib
    try:
        from project_registry import FileLock, ironclad_home
        return FileLock(ironclad_home() / "locks" / "switch.lock")
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext()


def _switch_command(agent: "GX10", arg_str: str) -> str:
    """`/switch <project_id>` — quiesce this engine and rebind it to a registered project (per-process
    active; the continuity pointer is persisted). Saves the leaving conversation under its own root, binds
    the target, rebuilds the project config, and swaps the conversation; refuses when a dev unit is
    in-flight for either side. No argument → report the active project."""
    if _REGISTRY is None or _ps is None or _pc is None:
        return "[switch] project isolation unavailable (registry/context seam absent)"
    pid = arg_str.strip()
    if not pid:
        cur = _ACTIVE_PROJECT
        return (f"[switch] active: {cur.id} → {_engine_ctx_for(cur).root}\n"
                "  usage: /switch <project_id>  ·  /project list"
                if cur is not None else "[switch] no active project — /project list")
    shim = _CacheActiveRegistry(_REGISTRY)
    _tgt = shim.get(pid)
    if _tgt is None:                          # pre-check so a real error inside the switch is never
        return f"[switch] unknown project {pid!r} — /project list"   # mis-reported as "unknown project"
    if getattr(_tgt, "archived", False):      # an archived project is not a valid switch target (S16)
        return f"[switch] {pid!r} is archived — /project unarchive {pid} first"
    if _BASE_CFG is None:
        return "[switch] no live base config (the switch rebuilds the project config from it)"
    with _switch_serialize():                 # #979: serialize concurrent switches (at most one active mode)
        try:
            target, dropped = _ps.switch_project(
                pid,
                registry=shim,
                agent=_SwitchAgent(agent),
                base_cfg=_BASE_CFG,
                apply_config=_switch_apply_config,
                overlay_for=_project_overlay_for,
                in_flight=_project_in_flight,
                ctx_for=_engine_ctx_for,
            )
        except _ps.SwitchRefused as e:
            return f"[switch] refused — {e}"
        except Exception as e:   # noqa: BLE001 — switch_project rolled the ctx back to the leaving project
            return f"[switch] failed — {e!r}"
        _set_active_project(target)           # publish to this process's other threads (only AFTER commit)
    # S11a-2 (#630): reload skills so the NEW project's library is discovered (and the previous project's
    # library items dropped) — build-then-swap, so a reload hiccup never empties the live registries and an
    # already-committed switch is never failed by it. plugins_dir is a locked key (stable across projects).
    try:
        _load_skills(((_EFFECTIVE_CFG or {}).get("paths") or {}).get("plugins_dir"))
    except Exception as e:  # noqa: BLE001 — the switch is committed; a reload warning must not fail it
        _ui_print(col(f"[switch] skills reload warning: {e!r}", C.YELLOW))
    msg = f"[switch] now on {target.id} → {_engine_ctx_for(target).root}"
    if dropped:
        msg += f"\n  (dropped locked config overrides: {', '.join(dropped)})"
    return msg


def _project_track_command(args: "List[str]") -> str:
    """`/project track new|use|list` — manage the parallel TRACKS of the ACTIVE project (ADR-0011 AD-2' /
    S16). Each non-``main`` track has its own vault subtree (``.tracks/<track>/``) and memory sub-scope
    (``<mem_ns>::track::<tid>``), so parallel lines of work stay isolated. ``new`` creates AND switches to a
    track (create-and-switch, like ``git checkout -b``); ``use`` switches to an existing one; ``list`` shows
    them. Switching rebinds the engine context (vault + memory follow) and reloads the per-track library."""
    if _REGISTRY is None:
        return "[project] project registry unavailable"
    cur = _ACTIVE_PROJECT
    if cur is None:
        return "[project] no active project — `/switch <id>` first"
    sub = args[0].lower() if args else "list"
    if sub == "list":
        tracks = list(getattr(cur, "tracks", ["main"]) or ["main"])
        act = getattr(cur, "active_track", "main") or "main"
        lines = ["[project] tracks  ([active] = current):"]     # #1238: markdown-safe marker ([…] tag, not "* ")
        lines += [f"- {t}{' [active]' if t == act else ''}" for t in tracks]
        return "\n".join(lines)
    if sub in ("new", "use"):
        if len(args) < 2:
            return f"usage: /project track {sub} <track>"
        track = args[1]
        try:
            if sub == "new":
                _REGISTRY.add_track(cur.id, track)          # idempotent register
            _REGISTRY.set_active_track(cur.id, track)        # new = create-and-switch; use = switch
        except (ValueError, KeyError) as e:
            return f"[project] {e}"
        _set_active_project(_REGISTRY.get(cur.id))           # refresh the cached project (new active_track)
        try:
            bind_active()                                    # rebind ctx: vault + memory now scope to the track
            _load_skills(((_EFFECTIVE_CFG or {}).get("paths") or {}).get("plugins_dir"))
        except Exception as e:   # noqa: BLE001 — the track switch is committed; a reload hiccup must not fail it
            _ui_print(col(f"[project] track reload warning: {e!r}", C.YELLOW))
        verb = "created + active" if sub == "new" else "active"
        return f"[project] track '{track}' {verb} — vault + memory now scoped to it"
    return "usage: /project track new <track> | use <track> | list"


_PROJECT_NEW_USAGE = "usage: /project new <name> [--path <dir>]"


def _parse_project_new(arg_str: str) -> "Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]":
    """Parse `new <name> [--path P]` (P may be quoted) → (name, "software", path|None, error|None).
    The name is every remaining positional token (a multi-word name is slugified). #984: MPR is an
    embedded dev-process function, not a project type — there is one type (``software``). ``--type`` is
    dropped; any ``--type[ =]<val>`` (or a bare ``--type``) is tolerated + IGNORED (back-compat), never
    validated. The mint always seeds a software unit."""
    s = re.sub(r"--type(?:[=\s]+\S*)?", "", arg_str)     # tolerate + ignore a legacy --type (incl. bare/empty)
    path: Optional[str] = None
    mp = re.search(r"--path(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)", s)
    if mp:
        raw = mp.group(1)
        path = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" else raw
        s = s[:mp.start()] + s[mp.end():]
    name = " ".join(s.split())
    # A bare / empty-valued --path does not match the value-bearing regex above, so it would otherwise
    # survive in the name and mint a bogus project — fail-closed instead.
    if re.search(r"--path\b", name):
        return (None, "software", None, "a --path flag needs a value")
    if not name:
        return (None, "software", None, _PROJECT_NEW_USAGE)
    return (name, "software", path, None)


def _project_new_mint(agent: "GX10", arg_str: str) -> str:
    """`/project new <name> [--path <dir>]` — the guided-setup mint (ADR-0011 / S16):
    register a fresh isolated PROJECT (root = ``--path`` or ``<cwd>/<slug>``; a minted ``mem_ns``), then
    **activate it through the full quiesced switch** (so the leaving conversation is saved, a fresh one is
    started, the rolling summary / last-response are cleared, and in-flight work is refused — exactly like
    ``/switch``, so a mid-session ``new`` never bleeds the old conversation into the new project), and finally
    seed its first work unit (a ``software`` vault initiative) under the new project (#984: one type only —
    MPR is an embedded dev-process function, not a project type).
    **Atomic**: a bad name / duplicate root / a refused-or-failed switch leaves nothing registered; the unit
    seed is fail-soft (a project without a seeded unit is still valid — add one later)."""
    if _REGISTRY is None:
        return "[project] project registry unavailable"
    if agent is None:                                    # activation goes through the switch → needs a session
        return "[project] /project new requires an interactive session"
    name, typ, path, err = _parse_project_new(arg_str)
    if err:
        return err if err.startswith("usage") else f"[project] {err}"
    if not re.search(r"[A-Za-z0-9]", name):              # _slugify would otherwise fall back to "initiative"
        return f"[project] invalid name {name!r} (no usable characters for a slug)"
    slug = _slugify(name)
    base = Path(_BOOT_WORKDIR) if _BOOT_WORKDIR is not None else Path.cwd()
    root = Path(path) if path else (base / slug)
    # Register inactive FIRST (fail-closed on a duplicate id/root) so a duplicate never creates an orphan dir.
    try:
        proj = _REGISTRY.register(slug, str(root), make_active=False)
    except (ValueError, KeyError) as e:
        return f"[project] {e}"
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        try:
            _REGISTRY.remove(proj.id)                    # atomic: no orphan registry entry on a root-create failure
        except Exception:  # noqa: BLE001
            pass
        return f"[project] cannot create root {root}: {e}"
    # Activate through the REAL quiesced switch — it saves the leaving conversation, swaps to a fresh one,
    # clears rolling-summary/last-response, refuses in-flight work, rebinds paths+memory, and reloads the
    # library. On a refusal/failure it has already rolled the context back to the leaving project.
    sw = _switch_command(agent, proj.id)
    if not sw.startswith("[switch] now on"):
        try:
            _REGISTRY.remove(proj.id)                    # atomic: roll the registration back if activation didn't commit
        except Exception:  # noqa: BLE001
            pass
        return f"[project] mint of {slug!r} rolled back — {sw}"
    seeded = ""
    if typ:
        try:
            # #1276 (facet 2): seed the unit under a CANONICAL `main` slug, NOT the project name — so the vault
            # doc path is `<project>/vault/main/…`, not the redundant `<project>/vault/<project>/…` double name.
            v = initiative_new("main", typ)              # seed the first work unit under the now-active project
            seeded = f" · seeded {typ} unit '{v.slug}'"
        except Exception as e:  # noqa: BLE001 — seed is best-effort (incl. FS errors); the project is still valid
            seeded = f" · (unit seed skipped: {e})"
    return f"[project] created {proj.id} → {proj.root}  (mem_ns {proj.mem_ns[:8]}, active){seeded}\n  {sw}"


def _project_scopes(proj: "Any") -> "List[str]":
    """Every memory partition a project owns: its base ``mem_ns`` (the ``main`` track) plus one composite
    ``<mem_ns>::track::<tid>`` per non-``main`` track — the scopes a delete must forget."""
    ns = getattr(proj, "mem_ns", "") or ""
    if not ns:
        return []
    scopes = [ns]
    for t in (getattr(proj, "tracks", None) or []):
        if t and t != "main":
            scopes.append(f"{ns}::track::{t}")
    return scopes


def _safe_to_purge(root_str: str) -> "Tuple[bool, str]":
    """Guard for ``/project delete --purge`` (destructive ``rmtree``). Refuse anything that is not a safe,
    self-contained project directory: a non-directory, a filesystem root, or any of the protected bases — the
    user **home**, the engine **boot workdir**, the live **cwd** — OR an ANCESTOR of any of them (which would
    delete the home / working tree out from under us)."""
    try:
        root = Path(root_str).resolve()
    except Exception:  # noqa: BLE001
        return (False, "unresolvable path")
    if not root.is_dir():
        return (False, "not a directory")
    if root == root.parent:
        return (False, "refusing to purge a filesystem root")
    bases = []
    for getter in (Path.home, Path.cwd):
        try:
            bases.append(getter().resolve())
        except Exception:  # noqa: BLE001
            pass
    if _BOOT_WORKDIR is not None:
        try:
            bases.append(Path(_BOOT_WORKDIR).resolve())
        except Exception:  # noqa: BLE001
            pass
    for base in bases:
        if root == base:
            return (False, "refusing to purge a protected directory (home/cwd/boot)")
        try:
            base.relative_to(root)                          # root is an ANCESTOR of home/cwd/boot
            return (False, "refusing to purge an ancestor of home/cwd/boot")
        except ValueError:
            pass
    return (True, "")


def _project_delete(agent: "Optional[GX10]", args: "List[str]") -> str:
    """`/project delete <id> [--purge]` (ADR-0011 / S16) — registry-mediated removal. Forgets ALL of the
    project's memory scopes (cold + warm + lessons, every track) BEFORE removing the registry entry; the
    on-disk directories are LEFT untouched unless ``--purge`` is given (and even then only for a safe,
    self-contained dir). Fail-closed: the ``default`` project is never deletable. Deleting the ACTIVE project
    first switches to ``default`` (a clean unbind + conversation save), so the engine is never left bound to
    a deleted project."""
    if _REGISTRY is None:
        return "[project] project registry unavailable"
    purge = "--purge" in args
    ids = [a for a in args if not a.startswith("--")]
    if not ids:
        return "usage: /project delete <id> [--purge]"
    pid = ids[0]
    default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default") if _pr is not None else "default"
    if pid == default_id:
        return "[project] the default project cannot be deleted"
    proj = _REGISTRY.get(pid)
    if proj is None:
        return f"[project] unknown project {pid!r} — /project list"
    # If deleting the active project, switch away first (clean unbind + leaving-conversation save).
    if _ACTIVE_PROJECT is not None and _ACTIVE_PROJECT.id == pid:
        if agent is None:
            return "[project] delete of the ACTIVE project requires an interactive session"
        sw = _switch_command(agent, default_id)
        if not sw.startswith("[switch] now on"):
            return f"[project] cannot delete the active project — switch to {default_id} failed: {sw}"
    root = proj.root
    # #1263: forget the project's memory scopes in the BACKGROUND. A synchronous remote /delete_all (up to the
    # memory client's add_timeout, over LAN) on the single request thread FROZE the whole engine + the client
    # (which has no own timeout) — the delete must return promptly. The registry removal below is authoritative
    # and fast; any residual partition is swept by the S15 orphan-GC. Best-effort + fail-soft per scope.
    scopes = list(_project_scopes(proj))
    forgotten = len(scopes)
    if scopes:
        def _bg_forget(_scopes=scopes):
            for sc in _scopes:
                try:
                    _forget_scope(sc)
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=_bg_forget, daemon=True, name="project-forget").start()

    if not purge:
        # Atomic against root reuse: remove only if pid still owns this exact root.
        try:
            removed = _REGISTRY.remove(pid, expected_root=root)
        except Exception as e:  # noqa: BLE001
            return f"[project] delete failed: {e!r}"
        if removed is None:
            return f"[project] {pid} was already removed or changed underneath — nothing deleted"
        return f"[project] deleted {pid} (forgetting {forgotten} memory scope(s) in the background)"

    # --purge: the directory delete must be claimed while STILL serialized against re-registration.
    ok, why = _safe_to_purge(root)
    if not ok:
        # unsafe to purge → fall back to a registry-only removal (never touch the filesystem)
        try:
            removed = _REGISTRY.remove(pid, expected_root=root)
        except Exception as e:  # noqa: BLE001
            return f"[project] delete failed: {e!r}"
        if removed is None:
            return f"[project] {pid} was already removed or changed underneath — nothing deleted"
        return (f"[project] deleted {pid} (forgetting {forgotten} memory scope(s) in the background) · "
                f"purge refused ({why}) — dir left at {root}")
    # Under the registry lock: verify ownership, rename the root to a fresh unique tombstone (claims it), and
    # drop the entry — atomically. Then rmtree the tombstone outside the lock.
    try:
        res = _REGISTRY.remove_purge(pid, expected_root=root)
    except OSError as e:
        return f"[project] purge failed (directory not claimed): {e} — nothing deleted"
    except Exception as e:  # noqa: BLE001
        return f"[project] delete failed: {e!r}"
    if res is None:
        return f"[project] {pid} was already removed or changed underneath — nothing deleted"
    _removed, tomb = res
    try:
        shutil.rmtree(tomb)
        return f"[project] deleted {pid} (forgetting {forgotten} memory scope(s) in the background) · purged {root}"
    except OSError as e:
        return (f"[project] deleted {pid} (forgetting {forgotten} memory scope(s) in the background) · "
                f"removed but the tombstone could not be deleted: {e} (at {tomb})")


def _project_archive(args: "List[str]", *, archive: bool) -> str:
    """`/project archive <id>` / `/project unarchive <id>` (ADR-0011 / S16) — toggle the reversible archived
    flag. Archived projects are hidden from the default ``list`` and refused as a switch target; data +
    memory are untouched. Fail-closed: the ``default`` and the ACTIVE project cannot be archived (switch
    away first)."""
    if _REGISTRY is None:
        return "[project] project registry unavailable"
    if not args:
        return f"usage: /project {'archive' if archive else 'unarchive'} <id>"
    pid = args[0]
    default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default") if _pr is not None else "default"
    if archive and pid == default_id:
        return "[project] the default project cannot be archived"
    if archive and _ACTIVE_PROJECT is not None and _ACTIVE_PROJECT.id == pid:
        return "[project] cannot archive the ACTIVE project — switch away first"
    try:
        _REGISTRY.set_archived(pid, archive)
    except KeyError:
        return f"[project] unknown project {pid!r} — /project list"
    except Exception as e:  # noqa: BLE001
        return f"[project] {e!r}"
    return f"[project] {pid} {'archived' if archive else 'un-archived'}"


def _project_command(arg_str: str, agent: "Optional[GX10]" = None) -> str:
    """`/project list | new <name> [--type] [--path] | active | track …` — the guided project-setup command
    (ADR-0011 / S16): manage registered, isolated projects (the SSOT the `/switch` verb selects from) and
    their parallel tracks. ``new`` activates through the quiesced switch and so needs the session *agent*;
    the other verbs are pure bookkeeping. (`/initiative` is a deprecated alias.)"""
    if _REGISTRY is None:
        return "[project] project registry unavailable"
    default_id = getattr(_pr, "DEFAULT_PROJECT_ID", "default") if _pr is not None else "default"
    parts = arg_str.split()
    sub = parts[0].lower() if parts else "list"
    try:
        if sub == "list":
            show_all = "--all" in parts[1:]                # archived hidden unless --all
            projs = sorted(_REGISTRY.list(), key=lambda p: p.id)
            shown = [p for p in projs if show_all or not getattr(p, "archived", False)]
            if not shown:
                return "[project] none registered — /project new <name>"
            cur = _ACTIVE_PROJECT.id if _ACTIVE_PROJECT is not None else None
            n_arch = sum(1 for p in projs if getattr(p, "archived", False))
            # #1238: markdown-safe marker — a leading "* " row became a generic "- " bullet in the client's
            # markdown renderer, dropping the active marker. Use the existing "[…]" tag convention instead.
            head = "[project]  ([active] = current" + (", [archived] hidden — /project list --all" if (n_arch and not show_all) else "") + ")"
            lines = [head]
            for p in shown:
                active_tag = " [active]" if p.id == cur else ""
                root = str(_BOOT_WORKDIR) if (p.id == default_id and _BOOT_WORKDIR is not None) else p.root
                arch_tag = " [archived]" if getattr(p, "archived", False) else ""
                lines.append(f"- {p.id}{active_tag}{arch_tag}  ·  {root}  ·  mem_ns {(p.mem_ns or '-')[:8]}")
            return "\n".join(lines)
        if sub in ("new", "add"):
            rest = arg_str.split(None, 1)[1] if len(parts) > 1 else ""
            return _project_new_mint(agent, rest)
        if sub == "active":
            cur = _ACTIVE_PROJECT
            return (f"[project] active: {cur.id} → {_engine_ctx_for(cur).root}"
                    if cur is not None else "[project] none active")
        if sub == "track":
            return _project_track_command(parts[1:])
        if sub in ("delete", "rm", "remove"):
            return _project_delete(agent, parts[1:])
        if sub == "archive":
            return _project_archive(parts[1:], archive=True)
        if sub == "unarchive":
            return _project_archive(parts[1:], archive=False)
        import command_spec as _command_spec   # #953: spec-derived usage (single source)
        return _command_spec.guided_usage("project")
    except Exception as e:   # noqa: BLE001 — registry I/O / lock / validation → a clean message, never a crash
        return f"[project] {e}"


def _catalogue_snapshot() -> Dict[str, List[Dict[str, Any]]]:
    """A read-only snapshot of the **one loaded registry** — the single source for both the
    ``/prompts``/``/skills`` discovery commands and the ``/catalogue`` endpoint. No re-scan: it
    reads the live ``_PROMPTS`` / ``_PLAYBOOKS`` / ``_PLUGIN_TOOLS`` dicts that ``_load_skills``
    populated at startup (so discovery never drifts from what is actually loaded).

    Returns ``{"prompts": [{name, description, languages}], "skills": [{name, kind, description}]}``
    — ``skills`` covers both discovered skill kinds: ``SKILL.md`` playbooks (``_PLAYBOOKS``) and
    typed ``CASE``+``run`` tools (``_PLUGIN_TOOLS``, incl. the MPR built-in)."""
    prompts: List[Dict[str, Any]] = []
    for cap in sorted(_PROMPTS):
        p = _PROMPTS[cap]
        prompts.append({"name": cap, "description": p.description, "languages": list(p.languages)})
    skills: List[Dict[str, Any]] = []
    for cap in sorted(_PLAYBOOKS):
        pb = _PLAYBOOKS[cap]
        skills.append({"name": cap, "kind": "playbook", "description": pb.description})
    for name in sorted(_PLUGIN_TOOLS):
        fn = (_PLUGIN_TOOLS[name].get("schema") or {}).get("function") or {}
        pa = fn.get("parameters") or {}
        params = pa.get("required") or list((pa.get("properties") or {}).keys())
        skills.append({"name": name, "kind": "tool", "description": str(fn.get("description", "") or ""),
                       "params": [str(p) for p in params]})   # #932: surface tool params for /skills + /tool
    skills.sort(key=lambda s: s["name"])
    # #931: also serve the command-spec (server verbs + danger-tier) so the client generates its
    # server-command completions FROM this one source (its static list is the cold-start fallback).
    # Additive + fail-soft: a spec import hiccup must never break discovery of prompts/skills.
    commands: List[Dict[str, Any]] = []
    try:
        import command_spec as _command_spec
        commands = _command_spec.catalogue_entries()
    except Exception:  # noqa: BLE001 — discovery degrades to prompts/skills, never crashes
        commands = []
    return {"prompts": prompts, "skills": skills, "commands": commands}


def _render_prompts() -> str:
    """Text view of the loaded ``kind: prompt`` items for the ``/prompts`` command."""
    items = _catalogue_snapshot()["prompts"]
    if not items:
        return col("  No prompt items are loaded.", C.YELLOW)
    lines = [col(f"  Loaded prompts ({len(items)}) — [languages] description:", C.CYAN)]
    for it in items:
        langs = ",".join(it["languages"]) or "en"
        lines.append(f"    {it['name']:<18} [{langs}]  {it['description']}")
    lines.append(col("  → invoke one directly: /<name> [var=value …] [--lang xx]  "
                     "(or the use_prompt tool for a model-guided flow).", C.GRAY))
    return "\n".join(lines)


def _render_skills() -> str:
    """Text view of the loaded skills (playbooks + typed tools) for the ``/skills`` command."""
    items = _catalogue_snapshot()["skills"]
    if not items:
        return col("  No skills are loaded.", C.YELLOW)
    lines = [col(f"  Loaded skills ({len(items)}) — kind  name  description:", C.CYAN)]
    for it in items:
        lines.append(f"    {it['kind']:<9} {it['name']:<22} {it['description']}")
        if it.get("params"):   # #932: show a tool's parameters so /tool <name> is callable without reading the schema
            lines.append(col(f"    {'':<9} {'':<22} {_msg('skills.params')} {', '.join(it['params'])}", C.GRAY))
    lines.append(col("  → playbooks load via the use_skill tool; typed tools are model-elected (or /tool <name>).", C.GRAY))
    return "\n".join(lines)


def _render_command_tiers() -> str:
    """#932: the server commands grouped by danger-tier (from the command-spec) so ``/help`` conveys which
    commands change/cost/destroy vs merely read. Fail-soft — empty string on any spec import hiccup."""
    try:
        import command_spec as _cs
    except Exception:  # noqa: BLE001 — /help must never break on a spec issue
        return ""
    order = [(_cs.READ_ONLY, _msg("tiers.read_only")), (_cs.MUTATING, _msg("tiers.mutating")),
             (_cs.COSTLY, _msg("tiers.costly")), (_cs.DESTRUCTIVE, _msg("tiers.destructive"))]
    out = [col("  " + _msg("tiers.header"), C.CYAN)]
    for tier, label in order:
        verbs = sorted(c.verb for c in _cs.COMMAND_SPECS if c.tier == tier)
        if verbs:
            out.append(f"    {label}: {', '.join(verbs)}")
    return "\n".join(out)


_LANG_TAIL_RE = re.compile(r"\s+--lang(?:=|\s+)(\S+)\s*$")


def _parse_prompt_args(prompt: Any, rest: str) -> Tuple[Dict[str, str], Optional[str], Optional[str]]:
    """Parse the argument string of a `/<prompt-name> …` invocation into (values, lang, error).

    Two ergonomic forms, both deterministic. A trailing ``--lang xx`` / ``--lang=xx`` is peeled
    first (only at the very end — a ``--lang`` *inside* a value is left intact). Then:
      * **single positional** — if there is exactly one required variable AND the remaining text is
        not an explicit ``<declared-var>=…`` assignment, the whole text is that variable's value.
        This is the headline path for code/diffs (``/explain-code def f(x=1): ...``) — a value
        containing ``=`` is preserved verbatim, never tokenised.
      * **key=value** — ``var=value`` tokens (``shlex``-quoted for spaces), used when the text
        begins with a declared variable assignment or there is not exactly one required variable.
        Unrecognised barewords are ignored (not an error)."""
    import shlex
    rest = rest.strip()
    if not rest:
        return {}, None, None
    lang: Optional[str] = None
    m = _LANG_TAIL_RE.search(rest)               # peel a TRAILING --lang only (mid-value --lang stays)
    if m:
        lang = m.group(1)
        rest = rest[: m.start()].rstrip()
    if not rest:
        return {}, lang, None
    declared = {v.name for v in prompt.variables}
    required = [v.name for v in prompt.variables if v.required]
    # Does the text start with an explicit `<declared-var>=` assignment? Only then is it key=value.
    head = rest.split("=", 1)[0].strip() if "=" in rest else ""
    looks_kv = head in declared and head != ""
    if not looks_kv and len(required) == 1:
        return {required[0]: rest}, lang, None    # positional: whole value, '=' and all
    try:
        tokens = shlex.split(rest)
    except ValueError as e:
        return {}, lang, f"could not parse arguments: {e}"
    values: Dict[str, str] = {}
    for t in tokens:
        if "=" in t:
            k, v = t.split("=", 1)
            if k.strip():
                values[k.strip()] = v
    return values, lang, None


def _resolve_prompt_name(text: str) -> Optional[str]:
    """The first whitespace token of *text* if it names a loaded prompt (case-insensitive), else
    None. Safe on empty/whitespace input (returns None — never an IndexError)."""
    toks = text.split()
    if not toks:
        return None
    first = toks[0]
    if first in _PROMPTS:
        return first
    low = first.lower()
    return next((cap for cap in _PROMPTS if cap.lower() == low), None)


def _invoke_prompt(user_input: str) -> str:
    """Resolve + run one prompt-library item by name. Returns the finished prompt when all required
    variables are present, else the guiding questions for what is still missing (#148)."""
    from ack.promptgen import run_prompt
    name = _resolve_prompt_name(user_input)
    if name is None:                                     # defensive — the dispatch guard checked this
        return col("  no such prompt — try /prompts", C.YELLOW)
    prompt = _PROMPTS[name]
    parts = user_input.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    values, lang, err = _parse_prompt_args(prompt, rest)
    if err:
        return col(f"  /{name}: {err}", C.YELLOW)
    try:
        step = run_prompt(prompt, values, lang=(lang or None))
    except Exception as e:  # noqa: BLE001 — surfaced as a message, never raises into dispatch
        return col(f"  /{name}: could not assemble — {e!r}", C.RED)
    if step["status"] == "done":
        return (col(f"  /{name} → assembled prompt ({step['lang']}):", C.GREEN)
                + "\n\n" + step["prompt"])
    # status == "ask": guide the user to provide the missing required input(s)
    missing = set(step.get("missing") or [])
    first = step.get("variable") or (sorted(missing)[0] if missing else "var")
    langs = ",".join(prompt.languages) or "en"
    lines = [col(f"  /{name}: {prompt.description}  [{langs}]", C.CYAN),
             col(f"  Provide the required input(s) — e.g.  /{name} {first}=\"…\"  [--lang de]", C.GRAY),
             col("  Required:", C.GRAY)]
    for v in prompt.variables:
        if v.name in missing:
            q = v.question or v.description or f"value for {v.name}"
            lines.append(f"    {v.name} — {q}")
    optional = [v for v in prompt.variables if not v.required]
    if optional:
        lines.append(col("  Optional:", C.GRAY))
        for v in optional:
            q = v.question or v.description or ""
            lines.append(f"    {v.name}{(' — ' + q) if q else ''}")
    lines.append(col("  (or use the use_prompt tool for a model-guided flow)", C.GRAY))
    return "\n".join(lines)


# ─── Per-project paved-road generator (ADR-0011 S10 / #629) ───────────────────
# The generator (`ack.generator`) renders the paved-road template tree from the ACTIVE ProjectContext into
# the PER-PROJECT library — a `library/` subtree under the ctx-resolved vault_root — so a generated dev-
# project artifact lands under the active project and NEVER in core/skills. The core built-in capabilities
# are injected as the generator's collision guard (S10a), so a generated item can never shadow a built-in.
def _project_library_root() -> Path:
    """The active project's per-project capability library: a ``library/`` subtree under the ctx-resolved
    ``vault_root()``. Generated artifacts land here (the loader discovers it in S11). Under the implicit
    ``default`` project this is the boot workdir's ``vault/library`` — the same place a single-project
    install would write, so there is no surprise relocation."""
    return vault_root() / "library"


def _builtin_capabilities(*, include_prompts: bool = False) -> "set[str]":
    """The CORE built-in capabilities, injected as the generator's collision guard so a generated
    per-project item can never shadow a built-in (S10a). The base set is ``core/skills`` tools +
    playbooks (the catalogue's two kinds). When *include_prompts* is set (the ``--kind prompt`` path),
    the built-in ``kind: prompt`` capabilities are unioned in too, so a generated prompt cannot collide
    with a seed prompt. Fail-soft: a discovery hiccup yields an empty/partial set (no guard) rather than
    blocking generation."""
    caps: "set[str]" = set()
    try:
        from ack.catalogue import build_catalogue   # lazy: never import ack at gx10 top-level (S6b lesson)
        caps |= set(build_catalogue([(str(_BUILTIN_DIR), "built-in")]).entries)
    except Exception:  # noqa: BLE001
        pass
    if include_prompts:
        try:
            from ack.prompt import discover_prompts   # lazy (S6b lesson)
            caps |= {p.capability for p in discover_prompts(_BUILTIN_DIR)}
        except Exception:  # noqa: BLE001
            pass
    return caps


def _generate_command(arg_str: str) -> str:
    """`/generate --domain <d> --case <c> --description <text> [--prefix x] [--dry-run] …` — render the
    paved-road template tree into the ACTIVE project's per-project library (ctx-resolved), guarded against
    shadowing a core built-in. The output_root + reserved capabilities are engine-enforced (a ``--output-root``
    the user passes is ignored): generation always targets the active project's library, never core/skills."""
    import shlex
    from ack import generator as _gen        # lazy import (S6b lesson)
    if not arg_str.strip():
        import command_spec as _command_spec   # #953: spec-derived usage (single source)
        return (_command_spec.guided_usage("generate") + "\n"
                "  renders the paved-road template (case=tool [default], prompt=prompt item) into the "
                "active project's library (guarded vs built-ins)")
    try:
        args = _gen.build_parser().parse_args(shlex.split(arg_str))
    except SystemExit:
        return "generate: invalid args — required: --domain, --case, --description (see `ack.generator -h`)"
    except ValueError as e:                     # shlex.split on an unbalanced quote, etc.
        return f"generate: could not parse args ({e})"
    ctx = _gen.build_context(args)
    out_root = _project_library_root()
    # case path keeps the exact pre-S10c call (byte-identical); only --kind prompt widens the guard.
    reserved = (_builtin_capabilities(include_prompts=True) if args.kind == "prompt"
                else _builtin_capabilities())
    try:
        res = _gen.generate(ctx, template_root=_gen.template_root_for(args), output_root=out_root,
                            force=args.force, dry_run=args.dry_run,
                            reserved_capabilities=reserved)
    except FileNotFoundError as e:              # e.g. a missing --template tree
        return f"generate: {e}"
    except Exception as e:  # noqa: BLE001 — a slash command must never crash the dispatch/turn
        return f"generate: failed — {e!r}"
    if res.refused:
        return f"[REFUSED] {res.refused}"
    tag = " (dry-run)" if args.dry_run else ""
    lines = [f"generated{tag} '{ctx['capability_key']}' into the project library: {out_root.as_posix()}"]
    lines += [f"  [{f.action}] {f.rel}" for f in res.files]
    if res.conflicts:
        lines.append(f"  [CONFLICT] {res.conflicts} file(s) need manual resolution (diff3 markers written)")
    return "\n".join(lines)


def _dispatch(agent: GX10, user_input: str):
    cmd = user_input.lower()
    if cmd == "help":
        _ui_print(col(HELP, C.YELLOW))
        _tiers = _render_command_tiers()   # #932: append the danger-tier grouping from the command-spec
        if _tiers:
            _ui_print(_tiers)
    elif cmd == "clear":
        _ui_print(agent.clear_context())
    elif cmd == "status":
        _ui_print(agent.status())
        _ui_print(_dev_target_status_line())     # #982: surface the injection (internal-target) mode
    elif cmd == "prompts":
        _ui_print(_render_prompts())
    elif cmd == "skills":
        _ui_print(_render_skills())
    elif cmd == "config":
        _ui_print(_render_config())
    elif cmd == "config keys":
        # #932: discovery — the dotted keys `/config get|set` accept (boot-only keys flagged). Closes the
        # "opaque dotted keys with zero discovery" gap the C0 review named.
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        keys = sorted(_cfg_flatten_keys(cfg))
        out = [col("  " + _msg("keys.header", n=len(keys)), C.CYAN)]
        for k in keys:
            v = _cfg_get(cfg, k)   # #956: show the current value + inferred type (AC-2 "+values/types")
            flag = col("  " + _msg("keys.boot_only"), C.YELLOW) if k in _FROZEN_CONFIG_KEYS else ""
            out.append(f"    {k} = {v!r}  ({type(v).__name__}){flag}")
        _ui_print("\n".join(out))
    elif cmd == "config get" or cmd.startswith("config get "):
        parts = user_input.split(None, 2)            # config get <dotted.key>
        if len(parts) < 3 or not parts[2].strip():   # bare `config get` (clients trim) or a trailing space (raw HTTP)
            _ui_print(col("  usage: /config get <dotted.key>", C.YELLOW))
        else:
            key = parts[2].strip()
            val = _cfg_get(_EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults(), key)
            _ui_print(col(f"  {key} = {val!r}" if val is not None else f"  {key} = (not set)", C.CYAN))
    elif cmd.startswith("config set "):
        parts = user_input.split(None, 3)            # config set <dotted.key> <value...>  (original case)
        if len(parts) < 4:
            import command_spec as _command_spec   # #953: spec-derived usage (single source)
            _ui_print(col("  " + _command_spec.guided_usage("config set"), C.YELLOW))
        elif _EFFECTIVE_CFG is None:
            _ui_print(col("  [config] refused: no live config to set (start the server first)", C.RED))
        elif parts[2].strip() in _CONFIG_ALIASES:
            legacy = parts[2].strip()
            key, val = _CONFIG_ALIASES[legacy], _coerce_cfg_value(parts[3])
            refusal = _config_set_atomic(key, val)
            if refusal is not None:
                _ui_print(col(f"  [config] refused: {refusal}", C.RED))
            else:
                _ui_print(col(f"  [config] '{legacy}' is deprecated; set {key} = {val!r} "
                              "(alias kept for one release)", C.YELLOW))
        elif _config_tombstone_reason(parts[2].strip()) is not None:
            _ui_print(col(f"  [config] refused: '{parts[2].strip()}' is retired and cannot be set; "
                          f"{_config_tombstone_reason(parts[2].strip())}.", C.RED))
        elif (parts[2].strip() == "security.sandbox"
              and parts[3].strip().lower() in _RETIRED_SANDBOX_POLICIES):
            _ui_print(col("  [config] refused: security.sandbox off/none is retired and ignored; model command "
                          "isolation remains mandatory.", C.RED))
        elif (parts[2].strip() == "security.sandbox"
              and parts[3].strip().lower() not in _SANDBOX_POLICIES):
            _ui_print(col("  [config] refused: security.sandbox must be one of: auto, bwrap, firejail.", C.RED))
        elif parts[2].strip() in _FROZEN_CONFIG_KEYS:
            _ui_print(col(f"  [config] refused: '{parts[2].strip()}' is boot-only — set it in the deploy "
                          f"(env/config-file), not at runtime.", C.RED))
        elif parts[2].strip().split(".")[0] not in _EFFECTIVE_CFG:
            # #932 gap-2: an unknown ROOT section is a typo, not a real key — REFUSE (no silent write, no
            # false-GREEN). Known core sections + existing plugin namespaces (e.g. mpr.*) have a live root,
            # so they still set; only a mistyped/unknown root is rejected. (A wrong leaf under a known root
            # cannot be caught without a schema — accepted oracle limit.)
            _ui_print(col("  " + _msg("config.unknown_key", name=parts[2].strip()), C.RED))
        else:
            key, val = parts[2].strip(), _coerce_cfg_value(parts[3])
            if key in config_schema.LEAVES:
                try:
                    config_schema.validate_leaf(key, val)
                except config_schema.ConfigError as e:
                    _ui_print(col(f"  [config] refused: {e}", C.RED))
                    return
            elif key.split(".")[0] in {leaf.split(".")[0] for leaf in config_schema.LEAVES}:
                _ui_print(col("  " + _msg("config.unknown_key", name=key), C.RED))
                return
            refusal = _config_set_atomic(key, val)
            if refusal is not None:
                _ui_print(col(f"  [config] refused: {refusal}", C.RED))
            else:
                _ui_print(col(f"  [config] set {key} = {val!r}", C.GREEN))
    elif cmd == "quality reset":
        global _QUALITY_TRIPPED
        with _QUALITY_LOCK:
            breaker = _quality_breaker()
            if breaker is not None:
                breaker.reset()
            _QUALITY_TRIPPED = None
        _ui_print(col("  [quality] reset — staging hold cleared.", C.GREEN))
    elif cmd.startswith("read "):
        _ui_print(agent.manual_read(user_input[5:].strip()))
    elif cmd.startswith("write "):
        _ui_print(agent.manual_write(user_input[6:].strip()))
    elif cmd.startswith("cat "):
        _ui_print(agent.manual_cat(user_input[4:].strip()))
    elif cmd == "ls" or cmd.startswith("ls "):
        _ui_print(agent.manual_ls(user_input[2:].strip() or "."))
    elif cmd == "auto" or cmd.startswith("auto "):
        # #1296: the consolidated automation meta-switch. `auto on [N]` = FULL automation — watcher
        # (feedback→advance) + autopilot (launch) + continuation (post-advance next-unit/backlog
        # planning) coherently on, optional N = the max-tasks cap. `auto off` = GUIDED mode — nothing
        # fires by itself; the engine still selects the next unit deterministically and RECOMMENDS it
        # (steering state / this status), the operator drives each step. The watcher is a facet of this
        # meta-switch; /watcher is kept only as a compatibility alias for the same on/off path.
        global _WATCHER_ENABLED, AUTOPILOT_ENABLED, AUTOPILOT_AUTOPLAN, AUTOPILOT_MAX_TASKS, _AUTOPLAN_DONE
        parts = cmd.split()
        arg   = parts[1] if len(parts) > 1 else ""
        n_arg = parts[2] if len(parts) > 2 else None
        if arg == "on":
            try:
                task_cap = AUTOPILOT_MAX_TASKS if n_arg is None else int(n_arg)
                config_schema.validate_leaf("autopilot.autoplan_max_tasks", task_cap)
            except (ValueError, config_schema.ConfigError) as exc:
                _ui_print(col(f"[AUTO] invalid task cap {n_arg!r}: {exc}", C.RED))
                return  # type: ignore
            for key, value in (("autopilot.autoplan_max_tasks", task_cap),
                               ("autopilot.enabled", True),
                               ("autopilot.autoplan", True)):
                refusal = _config_set_atomic(key, value)
                if refusal is not None:
                    _ui_print(col(f"[AUTO] refused — {refusal}", C.RED))
                    return  # type: ignore
            _WATCHER_ENABLED   = True
            _AUTOPLAN_DONE     = 0
            cap = (f"max {AUTOPILOT_MAX_TASKS} tasks, stops automatically" if AUTOPILOT_MAX_TASKS > 0
                   else "UNBOUNDED — every unit is a paid coder run; cap it with `auto on N`")
            _ui_print(col(f"[AUTO] FULL automation ON — watcher + autopilot "
                          f"(max_concurrent={AUTOPILOT_MAX_CONCURRENT}) + continuation ({cap}).", C.GREEN))
            _ui_print(col("  The loop now advances finished tasks, stages the next open unit and "
                          "launches its coder until the epic is drained. `auto off` returns to guided mode.",
                          C.GRAY))
            # #1296 bootstrap: the first unit of a planned epic has no predecessor advance — arming
            # the loop kicks its [NEXT-UNIT] authoring turn itself (idle + selectable unit only).
            if not _continuation_kick():
                _pipeline_hint = _empty_pipeline_hint()   # #1268: no silent no-op on an empty pipeline
                if _pipeline_hint:
                    _ui_print(col(_pipeline_hint, C.GRAY))
        elif arg == "off":
            for key in ("autopilot.enabled", "autopilot.autoplan"):
                refusal = _config_set_atomic(key, False)
                if refusal is not None:
                    _ui_print(col(f"[AUTO] refused — {refusal}", C.RED))
                    return  # type: ignore
            _WATCHER_ENABLED   = False
            _AUTOPLAN_DONE     = 0
            _ui_print(col("[AUTO] GUIDED mode — nothing fires by itself (advance/launch/planning are "
                          "yours); the engine keeps recommending the next step.", C.YELLOW))
            _pipeline_hint = _empty_pipeline_hint()
            if _pipeline_hint:
                _ui_print(col(_pipeline_hint, C.GRAY))
        else:
            _full = _WATCHER_ENABLED and AUTOPILOT_ENABLED and AUTOPILOT_AUTOPLAN
            _none = not (_WATCHER_ENABLED or AUTOPILOT_ENABLED or AUTOPILOT_AUTOPLAN)
            mode = (col("FULL automation", C.GREEN) if _full
                    else col("GUIDED", C.YELLOW) if _none
                    else col("MIXED (granular toggles)", C.CYAN))
            limit_str = f"max={AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else "max=unbounded"
            _ui_print(f"  auto: {mode}  |  watcher {'on' if _WATCHER_ENABLED else 'off'} · "
                      f"autopilot {'on' if AUTOPILOT_ENABLED else 'off'} · "
                      f"continuation {'on' if AUTOPILOT_AUTOPLAN else 'off'} ({limit_str}, "
                      f"done={_AUTOPLAN_DONE})  |  auto on [N] / auto off")
            _pipeline_hint = _empty_pipeline_hint()
            if _pipeline_hint:
                _ui_print(col(_pipeline_hint, C.GRAY))
    elif cmd.startswith("watcher"):
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            _ui_print(col("[WATCHER] /watcher is deprecated; using /auto on instead.", C.YELLOW))
            _dispatch(agent, "auto on")
        elif arg == "off":
            _ui_print(col("[WATCHER] /watcher is deprecated; using /auto off instead.", C.YELLOW))
            _dispatch(agent, "auto off")
        else:
            state = col("ON", C.GREEN) if _WATCHER_ENABLED else col("OFF", C.YELLOW)
            _ui_print(f"  auto-driven watcher: {state}  |  use auto on [N] / auto off")
    elif cmd.startswith("autopilot"):
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            refusal = _config_set_atomic("autopilot.enabled", True)
            if refusal is not None:
                _ui_print(col(f"[AUTOPILOT] refused — {refusal}", C.RED))
                return  # type: ignore
            msg = (f"[AUTOPILOT] ON (max_concurrent={AUTOPILOT_MAX_CONCURRENT}); "
                   f"takes effect on the next tick (~{RECONCILER_INTERVAL:.0f}s).")
            if not _WATCHER_ENABLED and not AUTOMATION_DECOUPLED:
                # S7 (#1229): only true in coupled mode — decoupled autopilot is self-sufficient.
                msg += "  ⚠ automation is guided — use '/auto on' to arm feedback advance and continuation."
            _ui_print(col(msg, C.GREEN))
            _pipeline_hint = _empty_pipeline_hint()   # #1268: no silent no-op on an empty pipeline
            if _pipeline_hint:
                _ui_print(col(_pipeline_hint, C.GRAY))
        elif arg == "off":
            refusal = _config_set_atomic("autopilot.enabled", False)
            if refusal is not None:
                _ui_print(col(f"[AUTOPILOT] refused — {refusal}", C.RED))
                return  # type: ignore
            _ui_print(col("[AUTOPILOT] OFF — no new auto-starts (running sessions remain)", C.YELLOW))
        else:
            state = col("ON", C.GREEN) if AUTOPILOT_ENABLED else col("OFF", C.YELLOW)
            _ui_print(f"  autopilot: {state}  |  autopilot on / autopilot off")
    elif cmd.startswith("autoplan"):
        parts = cmd.split()
        arg   = parts[1] if len(parts) > 1 else ""
        n_arg = parts[2] if len(parts) > 2 else None
        if arg == "on":
            # Optional count: "autoplan on 5"
            try:
                task_cap = AUTOPILOT_MAX_TASKS if n_arg is None else int(n_arg)
                config_schema.validate_leaf("autopilot.autoplan_max_tasks", task_cap)
            except (ValueError, config_schema.ConfigError) as exc:
                _ui_print(col(f"[AUTOPLAN] invalid task cap {n_arg!r}: {exc}", C.RED))
                return  # type: ignore
            for key, value in (("autopilot.autoplan_max_tasks", task_cap),
                               ("autopilot.autoplan", True)):
                refusal = _config_set_atomic(key, value)
                if refusal is not None:
                    _ui_print(col(f"[AUTOPLAN] refused — {refusal}", C.RED))
                    return  # type: ignore
            _AUTOPLAN_DONE     = 0   # always reset the counter on activation
            limit_info = f", max {AUTOPILOT_MAX_TASKS} tasks — stops automatically"
            _ui_print(col(
                f"[AUTOPLAN] continuation ON{limit_info}",
                C.GREEN))
            _ui_print(col(
                "  ⚠ COST: every continued task launches a PAID coder run (claude/codex/…) — the "
                "planner turn is the cheap part.\n"
                "    Use a local vLLM for planning and set a task cap unless you mean it.",
                C.RED))
            # #1296 bootstrap: arming the continuation kicks the first open unit's authoring turn.
            if not _continuation_kick():
                _pipeline_hint = _empty_pipeline_hint()   # #1268: no silent no-op on an empty pipeline
                if _pipeline_hint:
                    _ui_print(col(_pipeline_hint, C.GRAY))
        elif arg == "off":
            refusal = _config_set_atomic("autopilot.autoplan", False)
            if refusal is not None:
                _ui_print(col(f"[AUTOPLAN] refused — {refusal}", C.RED))
                return  # type: ignore
            _AUTOPLAN_DONE     = 0
            _ui_print(col("[AUTOPLAN] OFF — pipeline stops when the queue is empty. Counter reset.", C.YELLOW))
        else:
            state     = col("ON", C.GREEN) if AUTOPILOT_AUTOPLAN else col("OFF", C.YELLOW)
            limit_str = f"  max={AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else "  unlimited"
            _ui_print(f"  Autoplan: {state}{limit_str}  |  done={_AUTOPLAN_DONE}  "
                      f"|  autoplan on [N] / autoplan off")
    elif cmd.startswith("log-terminal"):
        global AUTOPILOT_LOG_TERMINAL
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            refusal = _config_set_atomic("autopilot.log_terminal", True)
            if refusal is not None:
                _ui_print(col(f"[LOG-TERMINAL] refused — {refusal}", C.RED))
                return  # type: ignore
            _ui_print(col("[LOG-TERMINAL] ON — the next autopilot start opens a live window (wt / PowerShell)", C.GREEN))
        elif arg == "off":
            refusal = _config_set_atomic("autopilot.log_terminal", False)
            if refusal is not None:
                _ui_print(col(f"[LOG-TERMINAL] refused — {refusal}", C.RED))
                return  # type: ignore
            _ui_print(col("[LOG-TERMINAL] OFF", C.YELLOW))
        else:
            state = col("ON", C.GREEN) if AUTOPILOT_LOG_TERMINAL else col("OFF", C.YELLOW)
            _ui_print(f"  Log-Terminal: {state}  |  log-terminal on / log-terminal off")
    elif cmd == "rag" or cmd.startswith("rag "):
        # MEM-13: session toggle for per-turn retrieval — turn it OFF when the retrieval itself
        # looks like the source of inconsistent answers (answers then use only the live window).
        global RAG_ENABLED
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            refusal = _config_set_atomic("context.rag_enabled", True)
            if refusal is not None:
                _ui_print(col(f"[RAG] refused — {refusal}", C.RED))
                return  # type: ignore
            _ui_print(col("[RAG] per-turn retrieval ON", C.GREEN))
        elif arg == "off":
            refusal = _config_set_atomic("context.rag_enabled", False)
            if refusal is not None:
                _ui_print(col(f"[RAG] refused — {refusal}", C.RED))
                return  # type: ignore
            _ui_print(col("[RAG] per-turn retrieval OFF — answers use only the live window", C.YELLOW))
        else:
            state = col("ON", C.GREEN) if RAG_ENABLED else col("OFF", C.YELLOW)
            _ui_print(f"  per-turn retrieval (RAG): {state}  |  rag on / rag off")
    elif cmd == "context":
        _ui_print(agent.context_report())
    elif cmd == "initiative" or cmd.startswith("initiative "):
        # `/initiative` is a DEPRECATED alias for `/project` (ADR-0011 / S16) — kept functional for one
        # release for the nested vault-unit verbs. New work flows through `/project`.
        _ui_print(col("[deprecated] /initiative is now an alias — use /project (kept one release)", C.YELLOW))
        _ui_print(col(_initiative_command(user_input[len("initiative"):].strip()), C.CYAN))
    elif cmd == "switch" or cmd.startswith("switch "):
        _ui_print(col(_switch_command(agent, user_input[len("switch"):].strip()), C.CYAN))
    elif cmd == "design" or cmd.startswith("design "):
        _ui_print(col(_design_command(agent, user_input[len("design"):].strip()), C.CYAN))
    elif cmd == "approve" or cmd.startswith("approve "):
        # S5 (#1227/#1336): design approval (bare /approve or /approve design [slug|proposal-id]).
        _ui_print(col(_approve_command(user_input[len("approve"):].strip() or None), C.CYAN))
    elif cmd == "board" or cmd.startswith("board "):
        # S6 (#1228 / R5): render the task board (all units pending/in_progress/done) to BOARD.md + show it.
        _ui_print(col(_board_command(user_input[len("board"):].strip() or None), C.CYAN))
    elif cmd == "lifecycle" or cmd.startswith("lifecycle "):
        # S13b / AD-7: the engine DELIVER-leg lifecycle-completeness gate (reads the transition ledger as
        # data → projects stage-tagged evidence → verifies completeness). Deterministic, model-free.
        _ui_print(col(_lifecycle_command(user_input[len("lifecycle"):].strip()), C.CYAN))
    elif cmd == "fork" or cmd.startswith("fork "):
        # #903 (M5-3): list M5 architecture-fork unit proposals (read-only).
        _ui_print(col(_fork_command(user_input[len("fork"):].strip()), C.CYAN))
    elif cmd == "ace" or cmd.startswith("ace "):
        # #915: ACE ops — `/ace warmup --ledger <path>` offline warm-starts the active playbook from a
        # dev-loop ledger's historical trajectories (opt-in, off-hot-path, fail-soft).
        _ui_print(col(_ace_command(user_input[len("ace"):].strip()), C.CYAN))
    elif cmd == "project" or cmd.startswith("project "):
        _ui_print(col(_project_command(user_input[len("project"):].strip(), agent), C.CYAN))
    elif cmd == "generate" or cmd.startswith("generate "):
        _ui_print(col(_generate_command(user_input[len("generate"):].strip()), C.CYAN))
    elif cmd.startswith("tool "):
        # Deterministic, model-free tool call: `/tool <name> <json|text>`. Runs run_tool() DIRECTLY, so a
        # tool (e.g. the mpr_research panel) fires WITHOUT depending on the model electing it AND without the
        # per-turn RAG context (there is no model turn) — fixes the run_mpr trigger/RAG-recycle fork. Plain
        # text maps to the tool's first required parameter; `{...}` is parsed as explicit JSON args.
        parts = user_input.split(None, 2)
        name = parts[1] if len(parts) > 1 else ""
        rest = parts[2].strip() if len(parts) > 2 else ""
        args: Optional[Dict[str, Any]] = {}
        if not name:
            _ui_print(col("  usage: /tool <name> <json-args | text-for-first-arg>", C.YELLOW))
            args = None
        elif rest.startswith("{"):
            try:
                args = json.loads(rest)
            except Exception as e:  # noqa: BLE001
                _ui_print(col(f"  [tool] invalid JSON args: {e}", C.RED)); args = None
        elif rest:
            prim = None
            for t in _effective_tools():
                fn = t.get("function", {})
                if fn.get("name") == name:
                    pa = fn.get("parameters", {}) or {}
                    reqs = pa.get("required") or list((pa.get("properties") or {}).keys())
                    prim = reqs[0] if reqs else None
                    break
            args = {prim: rest} if prim else {}
        if args is not None:
            _ui_print(run_tool(name, args))
    elif _PROMPTS and _resolve_prompt_name(user_input) is not None:
        # Per-item prompt invocation: `/<prompt-name> [var=value ...] [--lang xx]`. Checked AFTER
        # every built-in command above, so a real command always wins (no shadowing). Deterministic,
        # model-free — reuses the `ack.promptgen` elicitation state machine. The model-elected
        # `use_prompt` tool stays available; this is the additive direct surface (#148; design: ADR-0003 D5).
        _ui_print(_invoke_prompt(user_input))
    else:
        agent.run(user_input)
        # LOK-1: persist the LLM context after each real turn so it survives an orchestrator restart.
        # In local mode the orchestrator is ephemeral (started per local-mode launch, stopped on client
        # exit); load_session() runs on boot (server.py) but nothing wrote the file. Silent save.
        agent.save_session()

# ─── Autopilot: handover → start Claude (API-free) ─────────
_HO_AGENT_RE = re.compile(r"_([A-Za-z]+)\.md$")

def _autopilot_active() -> int:
    with _AUTOPILOT_LOCK:
        return _AUTOPILOT_ACTIVE

def _autopilot_reserve():
    global _AUTOPILOT_ACTIVE
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_ACTIVE += 1

def _autopilot_release():
    global _AUTOPILOT_ACTIVE
    with _AUTOPILOT_LOCK:
        _AUTOPILOT_ACTIVE = max(0, _AUTOPILOT_ACTIVE - 1)

def _terminate_autopilot(task_id: str):
    """Terminates the claude session started for task_id (incl. child processes),
    if still active. FAIL-SAFE: any error is swallowed — the advance must
    NEVER fail because of this. The monitor thread frees the slot + registry."""
    with _AUTOPILOT_LOCK:
        proc = _AUTOPILOT_PROCS.get(task_id)
    if proc is None or proc.poll() is not None:
        return
    try:
        if PLATFORM == "windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        _ui_print(col(f"  [AUTO] session for {task_id} terminated (PID {proc.pid}) — task is done", C.GRAY))
    except Exception as e:
        _ui_print(col(f"  [AUTO] could not terminate the session for {task_id}: {e!r}", C.YELLOW))

def _find_handover(task_id: str) -> Optional[Path]:
    d = handovers_dir(soft=True)          # B3: <initiative>/.work/handovers (soft → daemon-safe)
    if d is None or not d.exists():
        return None
    hits = sorted(d.glob(f"{task_id}_*.md"))
    return hits[0] if hits else None

def _agent_from_handover(name: str) -> str:
    # #449: the filename token IS the agent id (letters only, _HO_AGENT_RE). No KIMI→SONNET norm —
    # an unconfigured token fails closed at the membership guard downstream.
    m = _HO_AGENT_RE.search(name)
    return m.group(1).upper() if m else ""

def _task_agent(task: Dict[str, Any]) -> str:
    """The agent ASSIGNED to the task — from assigned_to (matched against the configured agent
    names, #449), otherwise from the existing handover. Prevents a foreign agent's feedback from
    completing another agent's task. #449 (review B-3): match assigned_to on WHITESPACE-split tokens,
    not a loose substring — so a model string like "claude-opus-4-8" does NOT mis-resolve to OPUS
    (which would run a CODEX handover as the wrong agent); such a value falls through to the filename."""
    a = (task.get("assigned_to") or "").lower()
    toks = set(a.split())
    for name in _agent_names():               # config-driven: OPUS/SONNET[/…], declaration order
        if name.lower() in toks or name.lower() == a.strip():
            return name
    ho = _find_handover(task.get("id", ""))
    return _agent_from_handover(ho.name) if ho else ""

def _parse_handover_meta(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Reads (model, effort) from the handover frontmatter (`to:` / `effort:`)."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None, None
    m = re.search(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    block = m.group(1) if m else text[:600]
    model = effort = None
    for line in block.splitlines():
        mm = re.match(r"\s*to:\s*(\S+)", line)
        if mm:
            model = mm.group(1).strip()
        me = re.match(r"\s*effort:\s*(\S+)", line)
        if me:
            effort = me.group(1).strip()
    return model, effort


def _surface_coder_result(task_id: str, agent: str, rc, logfile) -> None:
    """Surface a completed local-lane coder run when it produced no usable feedback. Never raises."""
    try:
        ok = (rc == 0)
        _ui_print(col(f"  {'✓' if ok else '⚠'} [AUTO] claude finished: {task_id} "
                      f"(exit {rc})", C.GREEN if ok else C.YELLOW))
        fb_dir = feedback_dir(soft=True)
        found = list(fb_dir.glob(f"{task_id}_*-feedback.md")) if (fb_dir and fb_dir.exists()) else []
        if ok:
            for _fb in found:
                try:
                    _txt = _fb.read_text(encoding="utf-8")
                except Exception:   # noqa: BLE001
                    continue
                _stamped = _stamp_done_if_clean(_txt, rc)
                if _stamped != _txt:
                    _fb.write_text(_stamped, encoding="utf-8", newline="\n")
                    _ui_print(col(f"  [AUTO] {task_id}: stamped `status: done` on {_fb.name} "
                                  f"(exit 0, capture had no status token)", C.CYAN))
        has_usable = False
        for _fb in found:
            try:
                if _fb.read_text(encoding="utf-8").strip():
                    has_usable = True
                    break
            except Exception:  # noqa: BLE001
                continue
        try:
            t = _store().get(task_id)
        except Exception:  # noqa: BLE001
            t = None
        if t is not None and t.get("status") == "done":
            return
        if has_usable:
            return
        try:
            tail = Path(logfile).read_text(encoding="utf-8", errors="replace")[-2000:]
        except Exception:  # noqa: BLE001
            tail = ""
        snippet = tail.strip()[:400]
        reason = f"coder exit {rc}, no feedback" + (f" — {snippet}" if snippet else "")
        kind = "errored"
        _store().mark_blocked(task_id, reason=reason, kind=kind)
        _ui_print(col(f"  ⚠ [AUTO] {task_id}: {reason}", C.RED))
    except Exception:  # noqa: BLE001
        pass


def _do_launch(task_id: str, agent: str):
    """Starts `claude --print` for a handover and moves the task to
    in_progress. The subprocess runs detached; a monitor thread frees the
    concurrency slot on exit. On error the slot is freed
    immediately. (The reconciler has already reserved the slot.)"""
    if _task_is_escalated(_store().get(task_id)):
        _autopilot_release()
        _ui_print(col(f"  [AUTO] {task_id} is terminally escalated — launch discarded", C.YELLOW))
        return
    ho = _find_handover(task_id)
    if not ho:
        _autopilot_release()
        _ui_print(col(f"  [AUTO] handover for {task_id} vanished — launch discarded", C.YELLOW))
        return
    # #449: resolve the agent through the config-driven registry (no hardcoded OPUS/SONNET model,
    # no KIMI-norm). Unknown agent → fail-closed (release the slot + discard the launch).
    agent = (agent or "").upper()
    spec = _code_agent_registry().resolve(agent)
    if spec is None:
        _autopilot_release()
        _ui_print(col(f"  [AUTO] unknown agent {agent!r} for {task_id} — launch discarded "
                      f"(configured: {', '.join(_agent_names()) or 'none'})", C.YELLOW))
        return
    # #454 (review B): honour the handover's `to:`/`effort:` only when no pin overrode the staged agent;
    # a pinned (different) agent runs with ITS OWN model/effort, not the staged agent's.
    if agent == _agent_from_handover(ho.name):
        model, effort = _parse_handover_meta(ho)
        # #1236: the handover's `to:` is the RECIPIENT AGENT (e.g. "to: CODEX"), which the orchestrator writes
        # there — it is NOT a model override. An agent-name in `to:` must never become `--model CODEX` (a
        # non-Claude CLI rejects it: "the 'CODEX' model is not supported"). Drop it so spec.model wins; only a
        # genuine model string in `to:` still overrides.
        if model and _code_agent_registry().has(model.strip().upper()):
            model = None
    else:
        model, effort = None, None
    model  = model or spec.model                          # registry model (a genuine `to:` model still overrides)
    # #500: when the handover carries no explicit `effort:`, auto-tier it by the task's class (security/
    # architecture → xhigh, routine → high) instead of the flat default; an explicit `effort:` still wins,
    # and a task that can't be loaded / an unmapped class falls through unchanged (fail-open).
    try:
        _rec = _store().get(task_id)
    except Exception:  # noqa: BLE001 — effort tiering must never block a launch
        _rec = None
    effort = _resolve_handover_effort(effort, _task_class(_rec) if _rec else None, spec.effort)
    mm = _cached_model_mismatch(agent)
    if mm is not None and str(model) == str(mm.configured):
        available = ", ".join(mm.available) or mm.available_raw.strip()
        err = f"agent {agent}: model {model!r} not offered by {spec.bin!r} — available: {available}"
        try:
            _store().transition(task_id, "in_progress")
        except Exception:  # noqa: BLE001
            pass
        try:
            _store().mark_blocked(task_id, reason=err, kind="errored")
        except Exception:  # noqa: BLE001
            pass
        _autopilot_release()
        _ui_print(col(f"  ✗ [AUTO] {err}", C.RED))
        return
    # #1288: the ENGINE — not the handover body — owns the feedback filename+location for EVERY coder. The
    # CODEX branch gets it via `-o {feedback}`; the Claude `--print` shape has no such flag, so state the exact
    # path in the prompt (overriding any divergent name the orchestrator wrote into the handover body), else a
    # completed Claude run drops its feedback where the reconciler never looks → the task stays in_progress and
    # the pipeline stalls.
    _fbd = feedback_dir(soft=True)
    _fb_name = f"{task_id}_{agent}-feedback.md"
    _fb_path = (_fbd / _fb_name) if _fbd is not None else None
    _fb_disp = _fb_path.as_posix() if _fb_path is not None else _fb_name
    # Fix 5 (dev-loop stab): state the engine's ground-truth PROJECT NAME + CODE ROOT so the coder builds HERE,
    # aligned to the project — not a design-derived, double-nested project inside the code root (#1291).
    _proot = _project_root()
    _proj = (_proot.name if _proot else "") or "this project"
    prompt = (f"Autonomously read and work the handover {ho.as_posix()}. "
              f"Follow the instructions in .claude/CLAUDE.md. "
              f"Build ALL code directly under the current working directory (the code root of project "
              f"'{_proj}'); do NOT create a top-level wrapper directory named after the design — that "
              f"double-nests the tree. "
              f"When the work is finished you MUST write your handover feedback to EXACTLY this file — this "
              f"exact path and filename, ignoring any other feedback filename the handover body may name: "
              f"{_fb_disp}. The FIRST line of that file must be `status: done` when complete (the pipeline "
              f"advances ONLY on `status: done`), otherwise `status: blocked` or `status: clarification_needed`.")
    _bin = spec.bin or AUTOPILOT_CLAUDE_BIN
    _tmpl = spec.cmd_template or ""
    # #449 (review B-1): the Claude `--print` autopilot shape keeps its stream plumbing. The permission
    # mode comes from the agent spec; bypass is emitted only for an explicit per-agent capability opt-in.
    _is_claude_print = _bin in (AUTOPILOT_CLAUDE_BIN, "claude") and "--print" in _tmpl
    if _is_claude_print or not _tmpl:
        argv = [_bin, "--model", str(model), "--effort", str(effort)]
        extra = list(AUTOPILOT_EXTRA_ARGS)
        bypass_allowed = bool(getattr(spec.capabilities, "permission_bypass", False))
        bypass_requested = spec.permission_mode == "bypassPermissions"
        dangerous_flag = "--dangerously-skip-permissions"
        if dangerous_flag in extra and not bypass_allowed:
            _autopilot_release()
            _ui_print(col(
                f"  ✗ [AUTO] agent {agent} requires capabilities.permission_bypass=true before "
                f"{dangerous_flag} may be used — launch {task_id} discarded",
                C.RED,
            ))
            return
        if bypass_requested:
            if not bypass_allowed:
                _autopilot_release()
                _ui_print(col(
                    f"  ✗ [AUTO] agent {agent} requests bypassPermissions without "
                    f"capabilities.permission_bypass=true — launch {task_id} discarded",
                    C.RED,
                ))
                return
            if dangerous_flag not in extra:
                extra.append(dangerous_flag)
        elif dangerous_flag not in extra and "--permission-mode" not in extra:
            extra.extend(["--permission-mode", spec.permission_mode or "default"])
        if AUTOPILOT_STREAM:
            # Live streaming: stream-json NEEDS --verbose (otherwise claude aborts).
            # Stdout is piped to the line-oriented log drainer below so tailers see live output without
            # handing the child a block-buffered file handle.
            if "--verbose" not in extra:
                extra.append("--verbose")
            if "--output-format" not in extra:
                extra += ["--output-format", "stream-json"]
        argv += extra + ["--print", prompt]
    else:
        # Config-driven non-Claude agent (e.g. CODEX): render via the shared builder; the template owns
        # its flags. #449 (review B-2): pass a {feedback} capture path — a template with `-o {feedback}`
        # would otherwise render an EMPTY path. Point it at the feedback file the reconciler advances on,
        # so the agent's final message lands where the autopilot already looks.
        from commands import build_agent_argv
        cap = str(_fb_path) if _fb_path is not None else _fb_name   # #1288: same feedback path stated in the prompt
        argv = build_agent_argv(_tmpl, bin=_bin, model=str(model), effort=str(effort),
                                permission=spec.permission_mode or "", prompt=prompt, feedback=cap)
    try:
        from tooling_envelope_runtime import _envelope_authorize
        refused = _envelope_authorize(_bin, argv if (_is_claude_print or not _tmpl) else _tmpl)
    except Exception:
        refused = "tooling envelope refused malformed coder command"
    if refused:
        _autopilot_release()
        _ui_print(col(f"  ✗ [AUTO] {refused} — launch {task_id} discarded", C.RED))
        return
    # Autopilot logs are engine machinery (subprocess stdout), not an initiative artefact → under
    # state_root() (.ironclad/logs) instead of scattered in the WORKDIR root. An absolute override stays.
    _ld = Path(AUTOPILOT_LOGS_DIR)
    logdir = _ld if _ld.is_absolute() else state_root() / _ld
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{task_id}_{agent}.log"
    lf = None
    try:
        lf = open(logfile, "w", encoding="utf-8")
        # PYTHONIOENCODING=utf-8: prevents a cp1252 crash on non-ASCII characters
        # (e.g. → in handover texts) on Windows. Kimi and Claude both inherit it.
        _launch_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # S9c: the code-agent runs in the active project's root (so its file edits land in that tree); the
        # default project resolves to None → the process workdir, byte-identical to the pre-isolation launch.
        proc = subprocess.Popen(argv, cwd=(_exec_cwd() or "."), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                env=_launch_env,
                                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                                start_new_session=(os.name != "nt"))
    except Exception as e:
        try:
            lf.close()
        except Exception:
            pass
        _autopilot_release()
        _ui_print(col(f"  ✗ [AUTO] launch {task_id} failed: {e!r}", C.RED))
        return

    def _drain_stdout():
        written = 0
        capped = False
        try:
            stream = getattr(proc, "stdout", None)
            if stream is not None:
                for line in stream:
                    if not capped and written < _LOG_CAP_BYTES:
                        encoded = line.encode("utf-8", "replace")
                        remaining = _LOG_CAP_BYTES - written
                        if len(encoded) <= remaining:
                            lf.write(line)
                            written += len(encoded)
                        else:
                            chunk = encoded[:remaining].decode("utf-8", "ignore")
                            lf.write(chunk)
                            written += len(chunk.encode("utf-8", "replace"))
                        if written >= _LOG_CAP_BYTES or len(encoded) > remaining:
                            capped = True
                            lf.write("\n… [log truncated at 8 MiB — coder still running/producing output] …\n")
                        lf.flush()
                    # Keep consuming the stream after the cap so the child's stdout pipe never blocks.
        finally:
            try:
                stream = getattr(proc, "stdout", None)
                if stream is not None:
                    stream.close()
            except Exception:
                pass
            try:
                lf.close()
            except Exception:
                pass

    threading.Thread(target=_drain_stdout, daemon=True, name=f"coder-log-{task_id}-{agent}").start()

    with _AUTOPILOT_LOCK:
        _AUTOPILOT_PROCS[task_id] = proc
    try:
        _store().transition(task_id, "in_progress")
    except KeyError:
        pass
    _ui_print(col(f"  → [AUTO] claude launched: "
                  f"{task_id} ({agent}, {model}, effort={effort}) · PID {proc.pid} · log {logfile}",
                  C.MAGENTA))

    # Log terminal: open a new console window with Get-Content -Wait (Windows only).
    # Tries Windows Terminal (wt) first, falls back to a standalone PowerShell.
    if AUTOPILOT_LOG_TERMINAL and PLATFORM == "windows":
        _cmd = (f"$host.UI.RawUI.WindowTitle='{task_id} {agent} live'; "
                f"Write-Host '=== {task_id} {agent} live log ===' -ForegroundColor Cyan; "
                f"Get-Content -Wait '{logfile}'")
        _opened = False
        try:
            subprocess.Popen(
                ["wt", "new-tab", "--title", f"{task_id} {agent}",
                 "powershell", "-NoProfile", "-NoExit", "-Command", _cmd],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _opened = True
        except Exception:
            pass
        if not _opened:
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-NoExit", "-Command", _cmd],
                    stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
                _opened = True
            except Exception as _te:
                _ui_print(col(f"  [AUTO] log terminal not opened: {_te!r}", C.YELLOW))
        if _opened:
            _ui_print(col(f"  [AUTO] log terminal opened for {task_id}", C.CYAN))

    def _wait():
        timeout_s = _code_agent_timeout_s()
        try:
            try:
                rc = proc.wait(timeout=timeout_s if timeout_s and timeout_s > 0 else None)
            except subprocess.TimeoutExpired:
                _kill_command_process_tree(proc)
                try:
                    rc = proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    rc = None
                _ui_print(col(f"  ⏱ [AUTO] coder {task_id} exceeded {timeout_s:.0f}s wall-clock — killed",
                              C.YELLOW))
        finally:
            with _AUTOPILOT_LOCK:
                _AUTOPILOT_PROCS.pop(task_id, None)
            _autopilot_release()
        _surface_coder_result(task_id, agent, rc, logfile)
    threading.Thread(target=_wait, daemon=True).start()


def _auto_owns_launching() -> bool:
    """#1309: True when the automation loop is ACTUALLY driving coder launches — the launcher (autopilot)
    is on AND the reconciler loop runs (watcher on, or decoupled autopilot). In the mixed states
    `autopilot on` alone (watcher off + automation.decoupled False) or `autoplan on` alone (continuation
    without the launcher), the loop never launches, so launching stays a guided/manual action
    (launch_coder). Both `launch_coder` (defer) and the `plan_units` armed prompt gate on this."""
    return AUTOPILOT_ENABLED and (_WATCHER_ENABLED or AUTOMATION_DECOUPLED)


def _trigger_coder(task_id: "Optional[str]" = None) -> str:
    """#1226 (S4): the model-invokable trigger verb. The orchestrator — the SINGLE steering author — launches
    the coding agent for a staged handover ON DEMAND, bypassing the autopilot daemon (which stays off by
    default; ADR-0002 D7 / #312 S4: no second steering authority). Resolves the current staged unit, launches
    it via the SAME machinery the reconciler uses (`_do_launch`, NOT gated on AUTOPILOT_ENABLED), and flips it
    to in_progress. Fail-closed with a clear message; never double-launches; respects the concurrency cap."""
    store = _store()
    # 1. resolve the target task (explicit id, else the newest pending task that has a staged handover)
    if task_id:
        target = store.get(task_id)
        if target is None:
            return f"ERROR: no such task {task_id!r}."
    else:
        pend = sorted((t for t in store.list("pending") if not _task_is_escalated(t)),
                      key=lambda t: (t.get("created_at", ""), t.get("id", "")))
        target = next((t for t in reversed(pend) if _find_handover(t.get("id", ""))), None)
        if target is None:
            return ("No staged handover to launch — nothing pending has a handover. Stage one via "
                    "stage_handover first.")
    tid = target.get("id", "")
    # 2. double-launch guard — in_progress (running) OR done (a stale handover that advance failed to unlink)
    if target.get("status") in ("in_progress", "done"):
        return f"{tid} is already {target.get('status')} — not relaunched."
    if _task_is_escalated(target):
        return f"{tid} is terminally escalated — not relaunched."
    ho = _find_handover(tid)
    if not ho:
        return f"ERROR: {tid} has no staged handover file — stage_handover first."
    # 3. resolve the effective agent (pin/failover), fail-closed. An empty registry = the server topology
    #    (providers disabled): the coder runs on the CLIENT there, not on this box.
    agent = _effective_code_agent(_agent_from_handover(ho.name), task_class=_task_class(target))
    if not _code_agent_registry().has(agent):
        names = _agent_names()
        if not names:
            return (f"Nothing to launch here — no coding agent runs on this box (server topology: the coder "
                    f"runs on the client, which polls pending handovers). {tid} stays staged.")
        return f"ERROR: unknown/unconfigured agent for {tid} (configured: {', '.join(names)})."
    # #1309: the agent is now VALIDATED (an unknown/disabled staged agent surfaced above — the defer must
    # NOT mask a fail-closed config error, since the reconciler skips unconfigured agents too and the task
    # would strand). When /auto OWNS the drive the loop launches staged handovers itself (the client polls
    # `/pending`), so a manual launch_coder here would be a SECOND launcher racing the loop for the single
    # coder slot (the "BUSY" collision + the contradictory double message). Defer with a clear no-op — but
    # ONLY when the loop actually drives launches (see _auto_owns_launching); in the autopilot-only /
    # autoplan-only mixed states nothing else launches, so this verb must still launch it (guided fallback).
    if _auto_owns_launching():
        return (f"OK: /auto owns launching — the loop starts {tid} automatically once its handover is "
                f"staged. No manual launch_coder is needed (a second launch would only collide on the "
                f"single coder slot).")
    # 4. concurrency cap — the same bound the reconciler honours. The check-then-reserve is not locked as one
    #    atom, but it does not race: tool dispatch runs under _AGENT_LOCK (one turn at a time) and the
    #    orchestrator is the SINGLE steering author, so two launch_coder calls never overlap; the autopilot
    #    daemon (the only other launcher) stays off by default.
    if _autopilot_active() >= AUTOPILOT_MAX_CONCURRENT:
        return (f"BUSY: {_autopilot_active()} coder(s) already running (max_concurrent="
                f"{AUTOPILOT_MAX_CONCURRENT}) — {tid} not launched, retry after one finishes.")
    # 5. reserve a slot + launch via the SAME machinery (NOT AUTOPILOT_ENABLED-gated). `_do_launch` flips the
    #    task to in_progress + spawns detached, and frees the slot itself on any error (its documented contract).
    _autopilot_reserve()
    try:
        _do_launch(tid, agent)
    except Exception as e:  # noqa: BLE001 — _do_launch frees the slot on its OWN handled errors, but an
        _autopilot_release()  # unguarded raise (a bad logdir, or a non-KeyError transition error) would leak it.
        return f"ERROR: launch of {tid} failed to start ({e.__class__.__name__}) — slot released."
    after = store.get(tid)
    if after and after.get("blocked") and after.get("blocked_kind") in ("errored", "unavailable"):
        return f"ERROR: {after.get('blocked_reason') or f'launch of {tid} was blocked'}"
    if after and after.get("status") == "in_progress":
        return (f"OK: launched {agent} for {tid} — the coding session is running; its feedback will "
                f"auto-advance the pipeline.")
    return f"ERROR: launch of {tid} did not start (see the log)."


# ─── Feedback reconciler (polling instead of event triggers) ───────
# Reads the TRUE state every tick: for each in_progress task a
# complete feedback file is sought and the completion is triggered DETERMINISTICALLY (without
# an LLM). Misses/duplicates no FS events, is idempotent.
# Autopilot side (optional): pending task with handover → start claude.
# #449 (review A): letters-only, SYMMETRIC with _HO_AGENT_RE. Previously `\w+` — so a multi-segment
# filename parsed DIFFERENTLY on the two sides (`_CLAUDE_OPUS.md` → "OPUS" via _HO_AGENT_RE, but
# `_CLAUDE_OPUS-feedback.md` → "CLAUDE_OPUS" via `\w+`). Letters-only makes both sides yield the same
# trailing token, so an agent_id round-trips identically through BOTH regexes (charter §C0R-1).
_FB_RE = re.compile(r"_([A-Za-z]+)-feedback\.md$")

def _task_progress_mtime(store: "TaskStore", tid: str) -> "Optional[float]":
    """S7 (#1229): the newest 'work is happening' signal for an in_progress task — the max mtime of the coder
    log and any feedback file. Deliberately EXCLUDES the task-json mtime: mark_blocked/clear_blocked rewrite it,
    so counting it would make marking a task look like progress and flap the stall on and off. None when neither
    a log nor a feedback exists (a manual task with no observable signal is never false-flagged as stalled)."""
    mtimes: List[float] = []
    d = feedback_dir(soft=True)                     # any (partial) feedback of this task
    if d and d.exists():
        for f in d.glob(f"{tid}_*-feedback.md"):
            try:
                mtimes.append(f.stat().st_mtime)
            except OSError:
                pass
    try:                                            # a launched coder's live log (autopilot / launch_coder)
        logs = state_root() / AUTOPILOT_LOGS_DIR
        if logs.exists():
            for lg in logs.glob(f"{tid}_*.log"):
                try:
                    mtimes.append(lg.stat().st_mtime)
                except OSError:
                    pass
    except Exception:   # noqa: BLE001 — a log-dir hiccup must not break a reconcile tick
        pass
    return max(mtimes) if mtimes else None


def _reconcile_once(store: "TaskStore", enqueue, seen_mtime: Dict[str, float],
                    enqueued: set, launch_enqueue=None, launched: Optional[set] = None):
    """One reconciler tick. seen_mtime/enqueued/launched are persistent
    across ticks (completeness or dedup gate)."""
    # ── Launch side (autopilot): pending + handover → start claude ──
    if AUTOPILOT_ENABLED and launch_enqueue is not None and launched is not None:
        for task in sorted(store.list("pending"),
                           key=lambda t: (t.get("created_at", ""), t.get("id", ""))):
            if _task_is_escalated(task):
                continue
            tid = task.get("id") or ""
            ho = _find_handover(tid)
            if not ho:
                continue                      # no handover → not yet launchable
            # Launch dedup by (tid, handover mtime) instead of just tid: a
            # withdrawn + re-staged task under the SAME ID has a new
            # handover (new mtime) → is relaunched. Otherwise a once-
            # launched (possibly crashed) task would NEVER start again (bug: KGC-387
            # after an OPUS rate-limit crash + re-stage as KIMI).
            try:
                ho_key = (tid, ho.stat().st_mtime)
            except OSError:
                ho_key = (tid, 0.0)
            if ho_key in launched:
                continue
            if _autopilot_active() >= AUTOPILOT_MAX_CONCURRENT:
                break                         # no free slot → retry later
            # #454: operator pin override. #456: a budget failover stays within the task-class-capable
            # agents on the autopilot launch path too (NOT just the server /pending path) — else a tripped
            # Opus on a security task would silently fail over to a cheaper, non-security-capable agent here.
            agent = _effective_code_agent(_agent_from_handover(ho.name), task_class=_task_class(task))
            if not _code_agent_registry().has(agent):   # #449: config-driven membership (fail-closed)
                continue
            launched.add(ho_key)
            _autopilot_reserve()              # reserve a slot (worker starts, monitor frees)
            launch_enqueue(tid, agent)

    # Client-run claims are leases renewed through idempotent POST /claim calls. A hard-dead client stops
    # renewing, so reclaim its task for a later /pending poll. Server-launched/autopilot tasks have no
    # claimed_at field and are deliberately excluded. Re-read under the store lock before transitioning so
    # a renewal racing this tick cannot be overwritten from the stale list snapshot.
    if CLAIM_LEASE_TTL_S > 0:
        now = time.time()
        for listed in store.list("in_progress"):
            tid = listed.get("id") or ""
            lease_key = f"__lease_{tid}"
            if not tid:
                continue
            reclaimed_age: Optional[float] = None
            with store._lock:
                task = store.get(tid)
                claimed_at = task.get("claimed_at") if task else None
                blocked = bool(task and task.get("blocked"))
                try:
                    claim_stamp = float(claimed_at)
                    claim_age = now - claim_stamp if math.isfinite(claim_stamp) else -1.0
                except (TypeError, ValueError):
                    claim_age = -1.0
                if (not task or task.get("status") != "in_progress" or blocked
                        or claimed_at is None or claim_age <= CLAIM_LEASE_TTL_S):
                    enqueued.discard(lease_key)
                    continue
                try:
                    store.transition(tid, "pending")
                except Exception:   # noqa: BLE001 — a reclaim hiccup must not break the tick
                    continue
                reclaimed_age = claim_age
            if reclaimed_age is not None and lease_key not in enqueued:
                enqueued.add(lease_key)
                secs = int(reclaimed_age)
                _ui_print(col(
                    f"  ⚠ [WATCHER] task {tid} claim lease expired ({secs}s) — reclaimed to pending",
                    C.YELLOW,
                ))

    # S7 (#1229): when decoupled and only autopilot is on, this is a launch-only tick — the feedback-advance
    # side belongs to the watcher concern. Byte-identical when coupled (the loop only runs with watcher on).
    # ── S7 (#1229) heartbeat side: flag an in_progress task that had a progress signal (coder log /
    #    feedback mtime) and then went silent for HEARTBEAT_STALL_S seconds. Runs whenever the loop ticks —
    #    INDEPENDENT of the watcher/feedback concern (a wedged autopilot coder must be caught in decoupled,
    #    watcher-off mode too), so it sits BEFORE the decoupled feedback-side skip. Dedup + un-stall via the
    #    persistent `enqueued` set (a `__stall_<tid>` key), mirroring the orphan-warning dedup.
    if HEARTBEAT_STALL_S > 0:
        now = time.time()
        for task in store.list("in_progress"):
            tid = task.get("id") or ""
            if not tid:
                continue
            newest = _task_progress_mtime(store, tid)
            stall_key = f"__stall_{tid}"
            # never clobber a DIFFERENT block reason (e.g. an advance-gate refusal) with 'stalled'
            blocked_other = bool(task.get("blocked")) and task.get("blocked_kind") != "stalled"
            if newest is not None and (now - newest) > HEARTBEAT_STALL_S:
                if stall_key not in enqueued and not blocked_other:
                    enqueued.add(stall_key)
                    secs = int(now - newest)
                    _ui_print(col(f"  ⚠ [WATCHER] task {tid} stalled — no progress for {secs}s", C.YELLOW))
                    try:
                        store.mark_blocked(tid, reason=f"no progress for {secs}s", kind="stalled")
                    except Exception:   # noqa: BLE001 — a marking hiccup must not break the tick
                        pass
            elif stall_key in enqueued:
                enqueued.discard(stall_key)           # progress resumed → un-stall
                try:
                    if task.get("blocked_kind") == "stalled":
                        store.clear_blocked(tid)
                except Exception:   # noqa: BLE001
                    pass
    # S7 (#1229): when decoupled and only autopilot is on, this is a launch-only tick — the feedback-advance
    # side belongs to the watcher concern. Byte-identical when coupled (the loop only runs with watcher on).
    if AUTOMATION_DECOUPLED and not _WATCHER_ENABLED:
        return
    # ── Feedback side: pending OR in_progress + feedback OF THE ASSIGNED
    #    agent → advance. IMPORTANT: also scan `pending` — a task processed
    #    manually (outside autopilot) stays in `pending`
    #    (no pending→in_progress launch). If only `in_progress` were scanned,
    #    such a task with finished feedback would sit there forever (bug: KGC-387).
    d = feedback_dir(soft=True)          # B3: <initiative>/.work/feedback (soft → no active initiative = nothing to do)
    if d is None or not d.exists():
        return
    # Warning for files that don't match the pattern {task_id}_{agent}-feedback.md
    # (e.g. analysis documents that Qwen mistakenly writes into the feedback inbox)
    for orphan in d.iterdir():
        if orphan.is_file() and not _FB_RE.search(orphan.name):
            warn_key = f"__orphan_{orphan.name}"
            if warn_key not in enqueued:
                enqueued.add(warn_key)
                _ui_print(col(
                    f"  ⚠ [WATCHER] foreign file in the feedback inbox: {orphan.name} "
                    f"— cannot advance (not in task_id_agent format). "
                    f"Analysis documents do not belong in the .work/feedback inbox",
                    C.YELLOW))
    for task in (store.list("pending") + store.list("in_progress")):
        if _task_is_escalated(task):
            continue
        tid = task.get("id") or ""
        staged = _task_agent(task)           # expected agent (not from an arbitrary filename!)
        # #454 (review B): the handover may have run under a pin that has since changed/cleared — so the
        # feedback file is named for whatever agent ACTUALLY ran, which we can no longer recompute from
        # the current pin. Discover it: prefer the current effective agent, then the staged agent, then
        # ANY configured agent's {tid}_*-feedback.md (a pin added OR cleared mid-handover). Only a
        # CONFIGURED agent is accepted (fail-closed); a foreign/unconfigured token is ignored.
        order: List[str] = []
        # #456: derive the first-guess candidate with the same task_class scoping the launch path uses, so
        # the most-likely feedback filename matches what would actually have run; staged + every configured
        # agent still follow as fallbacks, so discovery never misses a file regardless of class.
        for cand in (_effective_code_agent(staged, task_class=_task_class(task)), staged, *_agent_names()):
            if cand and _code_agent_registry().has(cand) and cand not in order:
                order.append(cand)
        agent = fb = None
        for cand in order:
            p = d / f"{tid}_{cand}-feedback.md"
            if p.exists():
                agent, fb = cand, p
                break
        if fb is None:
            continue
        try:
            mt = fb.stat().st_mtime
        except OSError:
            continue
        # Fix 2 (dev-loop stab): key the enqueue-dedup on the feedback MTIME too. The old (tid,agent) latch was
        # added BEFORE the async advance ran, so an advance that REFUSED (gate: malformed status) latched the
        # task forever — a corrected/re-written feedback never re-fired and the stall was permanent until a
        # process restart. Including mtime makes a changed feedback a NEW key ⇒ it re-fires and recovers.
        key = (tid, agent, mt)
        if key in enqueued:
            continue
        # Completeness gate: mtime stable across a tick → fully written
        if seen_mtime.get(str(fb)) != mt:
            seen_mtime[str(fb)] = mt
            continue
        enqueued.add(key)
        enqueue(tid, agent, fb.name)


def _reconciler_loop(stop_event: threading.Event, interval: float):
    seen_mtime: Dict[str, float] = {}
    enqueued: set = set()
    launched: set = set()

    def enqueue(tid, agent, fname):
        _ui_print(col(f"\n[AUTO] feedback detected: {fname} → advance {tid} ({agent})", C.GREEN))
        _INPUT_QUEUE.put(f"{_ADVANCE_CMD}{tid}\x00{agent}")

    def launch_enqueue(tid, agent):
        _ui_print(col(f"\n[AUTO] handover {tid} ({agent}) → launching Claude", C.GREEN))
        _INPUT_QUEUE.put(f"{_LAUNCH_CMD}{tid}\x00{agent}")

    while not stop_event.wait(interval):
        # S7 (#1229): OFF → coupled (the loop runs iff watcher on). ON → autopilot is self-sufficient (the loop
        # runs if EITHER concern is on; the feedback side stays _WATCHER-gated inside _reconcile_once).
        if not (_WATCHER_ENABLED or (AUTOMATION_DECOUPLED and AUTOPILOT_ENABLED)):
            continue
        bind_active()           # S5b: this daemon thread → the active project (re-read each tick; follows a switch)
        try:
            _reconcile_once(_store(), enqueue, seen_mtime, enqueued,
                            launch_enqueue, launched)
        except Exception as e:
            _ui_print(col(f"[WARN] reconciler tick failed: {e}", C.YELLOW))


# ─── Application UI ───────────────────────────────────────────
def _empty_pipeline_hint() -> "Optional[str]":
    """#1268/#1296: when autonomous mode is switched on but nothing is actively running, SAY what the loop
    will do next — instead of a silent no-op. Three states: work in flight → None (the loop acts); open
    handover-less units → name the selected next unit (or the dependency deadlock); nothing at all → the
    seed hint (plan_units from the approved design), plus whether the capability-backlog leg could continue
    afterwards. Never raises (returns None when the store is unavailable — never break the toggle)."""
    try:
        s = _store()
        if _work_in_flight(s):
            return None
        unit, _elig, n_open = _select_next_unit(s)
    except Exception:  # noqa: BLE001 — no store / no project → skip the hint, never break the toggle
        return None
    if unit is not None:
        return (f"  ⓘ {n_open} open unit(s) — next: {unit['id']} ({unit.get('title')!r}). With automation on "
                f"the engine stages + runs it after each advance; in guided mode, stage its handover via "
                f"stage_handover (task_id='{unit['id']}', no task_json).")
    if n_open > 0:
        return (f"  ⚠ {n_open} open unit(s) but NONE is selectable — blocked or with unsatisfied "
                f"dependencies. Inspect /board and unblock (clear the block / finish or fix the dependency).")
    has_backlog = bool((_EFFECTIVE_CFG or {}).get("paths", {}).get("active_capability_backlog"))
    tail = "" if has_backlog else " (no capability backlog is configured either, so nothing continues after that)"
    return ("  ⓘ pipeline empty — nothing to run yet. Seed it: ask the model to break the approved "
            "design into units via plan_units (one epic + ALL implementation units)" + tail + ".")


def _autoplan_prompt(tid: str) -> Optional[str]:
    """Build the 'plan the next task' prompt from the configured capability backlog
    (``paths.active_capability_backlog``) — the SECOND continuation leg (#1296): it runs only
    when no open unit is left to drain. Returns None when no backlog is configured — this leg
    then has no source and the tick goes idle WITHOUT disarming (generic-safe: no vessel-specific
    default is assumed)."""
    backlog = (_EFFECTIVE_CFG or {}).get("paths", {}).get("active_capability_backlog")
    if not backlog:
        return None
    return (
        f"[AUTOPLAN] Task {tid} is complete, the pipeline is empty. "
        f"Plan the next step: read the active capability backlog {backlog} and take the "
        f"TOP entry (rank #1 — whether 🟡 partial or 🔴 not-started; partial means OPEN, "
        f"do not skip). Adopt the handover seed (type, effort, assignee, scope, anchors) "
        f"and create it via stage_handover — with capability:'<key>' in the task_json "
        f"(required — drift-free status join). Codebase paths ONLY from the anchors field "
        f"or verified via search_files — do NOT guess. No duplicate — the store checks "
        f"automatically. PLAN-CHANGE DUTY: first read the completed task's feedback. Does it "
        f"have items under ## Issues with plan relevance (effort change, new dependency, path "
        f"correction, architecture insight)? → Then FIRST adjust the gap-tracking/mapping, "
        f"THEN stage_handover. AUTONOMY DUTY: call stage_handover NOW, directly — NO "
        f"questions. If the backlog is empty (no open gaps) or only DEFER/out-of-scope: "
        f"report it, create NO task, and stop (pipeline goal reached)."
    )


def _next_unit_prompt(done_tid: "Optional[str]", unit: "Dict[str, Any]") -> str:
    """#1296: the [NEXT-UNIT] staging turn — the engine has already SELECTED the unit
    (deterministic policy); the model's only job is to AUTHOR its handover. The unit exists in
    the store, so the call is stage_handover with task_id and WITHOUT task_json. ``done_tid`` is
    the just-advanced predecessor (its feedback carries the plan-change duty) — None on the
    BOOTSTRAP kick (arming the loop on a freshly planned epic: no predecessor yet)."""
    parent = str(unit.get("parent") or "").strip()
    progress = ""
    if parent:
        try:
            sibs = [t for t in _store().list() if str(t.get("parent") or "") == parent]
            n_done = sum(1 for t in sibs if t.get("status") == "done")
            progress = f" — epic {parent}: {n_done}/{len(sibs)} units done"
        except Exception:  # noqa: BLE001 — progress is advisory
            progress = f" — epic {parent}"
    if done_tid:
        fb_dir = archive_feedback_dir()
        head = (f"[NEXT-UNIT] Task {done_tid} is complete. The engine selected the next open unit"
                f"{progress}: ")
        duty = (f"PLAN-CHANGE DUTY: first read the completed task's feedback "
                f"({(fb_dir / (done_tid + '_<agent>-feedback.md')).as_posix()}). Plan-relevant items under "
                f"## Issues (effort change, new dependency, path correction, architecture insight)? → Then "
                f"FIRST adjust the plan (add units via plan_units with epic_id, or report), THEN continue. ")
    else:
        head = f"[NEXT-UNIT] Automation armed. The engine selected the FIRST open unit{progress}: "
        duty = ""
    return (
        head
        + f"{unit['id']} [{unit.get('type')}/{unit.get('priority')}] "
          f"{unit.get('title')!r} — {unit.get('description', '')} "
        + duty
        + f"AUTHOR THE HANDOVER NOW: ONE stage_handover call with task_id='{unit['id']}' and NO "
          f"task_json (the unit already exists — a task_json would create a duplicate). Codebase "
          f"paths ONLY verified via search_files / a shell listing — do NOT guess; the code from "
          f"completed units EXISTS, extend it. AUTONOMY DUTY: no questions; if this unit is obsolete "
          f"or impossible, say why and stage nothing."
    )


def _continuation_kick() -> bool:
    """#1296 bootstrap: the continuation is edge-triggered on an ADVANCE — but the FIRST unit of a
    freshly planned epic has no predecessor advance, so arming the loop (`/auto on`, `/autoplan on`)
    must kick it once itself. When the continuation is armed, nothing is in flight and a unit is
    selectable, enqueue its [NEXT-UNIT] authoring turn on the input queue (consumed exactly like a
    post-advance continuation turn). Returns True when a turn was enqueued. Fail-soft: never raises
    (arming a toggle must not break on a store hiccup)."""
    try:
        if not AUTOPILOT_AUTOPLAN:
            return False
        s = _store()
        if _work_in_flight(s):
            return False
        unit, _elig, _n_open = _select_next_unit(s)
        if unit is None:
            return False
        _INPUT_QUEUE.put(_next_unit_prompt(None, unit))
        _ui_print(col(f"  → [CONTINUATION] bootstrapping: authoring the handover for "
                      f"{unit['id']} ({str(unit.get('title') or '')!r})", C.CYAN))
        return True
    except Exception:  # noqa: BLE001
        return False


def _continuation_tick(tid: str, enqueue) -> None:
    """#1296: after a successful advance — count it, enforce the max-tasks limit, and when nothing
    is actively running continue the loop in leg order: (1) next OPEN UNIT of the decomposition →
    enqueue its [NEXT-UNIT] handover-authoring turn; (2) no units left → the capability-backlog
    autoplan leg (``_autoplan_prompt``); (3) no source at all → idle, ARMED (a missing source is
    an informational line per advance, never a silent self-disable — only the max-tasks limit
    stops the loop). ``enqueue(prompt:str)`` puts the turn on the input queue. Gated on
    ``AUTOPILOT_AUTOPLAN`` only — independent of autopilot's *launch* side, so it works in the
    server/client split (server plans, client executes, server advances, server plans again)."""
    global _AUTOPLAN_DONE, AUTOPILOT_AUTOPLAN
    if not AUTOPILOT_AUTOPLAN:
        return
    _AUTOPLAN_DONE += 1
    _ui_print(col(f"  [CONTINUATION] {_AUTOPLAN_DONE}"
                  + (f"/{AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else "")
                  + " tasks completed", C.CYAN))
    if AUTOPILOT_MAX_TASKS > 0 and _AUTOPLAN_DONE >= AUTOPILOT_MAX_TASKS:
        AUTOPILOT_AUTOPLAN = False
        if _EFFECTIVE_CFG:
            _EFFECTIVE_CFG["autopilot"]["autoplan"] = False
        _ui_print(col(f"\n  ✓ [CONTINUATION] limit reached ({_AUTOPLAN_DONE}/"
                      f"{AUTOPILOT_MAX_TASKS}) — continuation stopped.", C.GREEN))
        return
    s = _store()
    if _work_in_flight(s):
        return                       # something runs / is staged → the launcher's turn, not ours
    unit, _elig, n_open = _select_next_unit(s)
    if unit is not None:
        enqueue(_next_unit_prompt(tid, unit))
        _ui_print(col(f"\n  → [CONTINUATION] next open unit after {tid}: {unit['id']} "
                      f"({unit.get('title')!r}) — authoring its handover", C.CYAN))
        return
    if n_open > 0:
        _ui_print(col(f"  ⚠ [CONTINUATION] {n_open} open unit(s) but NONE selectable — blocked or "
                      f"unsatisfied dependencies. Inspect /board; the loop stays armed.", C.YELLOW))
        return
    prompt = _autoplan_prompt(tid)
    if prompt is None:
        _ui_print(col("  [CONTINUATION] pipeline drained — no open units, no capability backlog "
                      "(paths.active_capability_backlog). Idle, armed.", C.CYAN))
        return
    enqueue(prompt)
    _ui_print(col(f"\n  → [AUTOPLAN] queue empty after {tid} — planning the next task "
                  f"from the backlog", C.CYAN))


def _code_defaults() -> Dict[str, Any]:
    """Return a fresh lowest-precedence tree derived from the typed schema."""
    return config_schema.defaults_tree()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive merge: override wins; nested dicts are merged
    field by field instead of replaced. Returns a fresh structure."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_config_source(cli_config: Optional[str]) -> Optional[Path]:
    """Location precedence: --config (file OR directory) > env GX10_CONFIG
    > ./conf/ > ./gx10.config.json > <SCRIPT_DIR>/conf/ > <SCRIPT_DIR>/gx10.config.json.
    A directory is loaded as a normalized domain config (with includes)."""
    for c in (cli_config, os.environ.get("GX10_CONFIG")):
        if c:
            p = Path(c).expanduser()
            if p.exists():
                return p
            print(col(f"  [WARN] config not found: {p}", C.YELLOW))
            return None
    for p in (Path.cwd() / "conf", Path.cwd() / "gx10.config.json",
              SCRIPT_DIR / "conf", SCRIPT_DIR / "gx10.config.json"):
        if p.exists():
            return p
    return None


def _read_json_dict(p: Path) -> Dict[str, Any]:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise config_schema.ConfigError(f"config not loadable ({p}): {e}") from e
    if not isinstance(data, dict):
        raise config_schema.ConfigError(
            f"config root in {p}: expected object, got {type(data).__name__}"
        )
    return data


def _load_config_tree(source: Optional[Path], _seen: Optional[set] = None) -> Dict[str, Any]:
    """Loads a config from a file OR directory and merges includes
    recursively. Rules:
      • Directory with `gx10.config.json` → load that index file.
      • Directory without an index → deep-merge all `*.json` (sorted), THEN descend
        into subdirectories (each again as a tree; subdirs override
        the top-level files). This loads e.g. `conf/connection/connection.json`.
      • File with `include: [...]` → merge the entries (relative to the file) first,
        then the file's own inline blocks (inline wins).
    Returns the same flat cfg tree as a single file."""
    if not source:
        return {}
    _seen = _seen if _seen is not None else set()
    p = Path(source)
    rp = str(p.resolve())
    if rp in _seen:                      # cycle protection
        return {}
    _seen.add(rp)

    if p.is_dir():
        idx = p / "gx10.config.json"
        if idx.is_file():
            return _load_config_tree(idx, _seen)
        merged: Dict[str, Any] = {}
        for f in sorted(p.glob("*.json")):
            merged = _deep_merge(merged, _load_config_tree(f, _seen))
        # Descend into subdirectories — the config is a TREE (e.g. conf/connection/).
        # Skip hidden/dotted dirs (.git, .vscode, …) so they are never slurped in.
        for d in sorted(x for x in p.iterdir()
                        if x.is_dir() and not x.name.startswith(".")):
            merged = _deep_merge(merged, _load_config_tree(d, _seen))
        return merged

    if p.is_file():
        data = _read_json_dict(p)
        includes = data.pop("include", [])
        # never carry comment/meta keys (_ prefix) into cfg
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        # Normalize one-release aliases before defaults/includes are merged so a legacy value has the same
        # precedence as its canonical spelling instead of being masked by the canonical code default.
        _consume_config_aliases(data)
        merged = {}
        if isinstance(includes, list):
            for inc in includes:
                merged = _deep_merge(merged, _load_config_tree(p.parent / inc, _seen))
        # the file's own inline blocks override the includes
        return _deep_merge(merged, data)

    print(col(f"  [WARN] config source is neither a file nor a directory: {p}", C.YELLOW))
    return {}


def _apply_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Env override (level 3). Only explicitly set GX10_* variables.
    The API key itself does NOT come from here, but only in main() from
    the variable named via api_key_env."""
    env = os.environ
    for name, replacement in _ENV_TOMBSTONES.items():
        if name in env:
            print(col(f"  [DEPRECATED] env '{name}' is retired and ignored; {replacement}.", C.YELLOW))
    for name, spec in config_schema.ENV_BINDINGS.items():
        raw = env.get(name)
        if raw in (None, ""):        # an unset OR empty env var means "not provided" for every leaf
            continue
        if name == "GX10_SANDBOX" and raw.strip().lower() in _RETIRED_SANDBOX_POLICIES:
            print(col("  [DEPRECATED] env GX10_SANDBOX=off/none is retired and ignored; model command "
                      "isolation remains mandatory.", C.YELLOW))
            continue
        try:
            value = spec.env_parser(raw)
            config_schema.validate_leaf(spec.key, value)
            _cfg_set(cfg, spec.key, value)
        except config_schema.ConfigError as e:
            print(col(f"  [WARN] env {name}={raw!r} ignored ({e})", C.YELLOW))
    return cfg


def _normalize_config_for_apply(cfg: Dict[str, Any]) -> None:
    """Consume supported legacy boundaries on an unpublished config candidate."""
    _consume_config_aliases(cfg)
    _consume_config_tombstones(cfg)
    _sandbox_boundary = cfg.get("security", {}).get("sandbox") if isinstance(cfg.get("security"), dict) else None
    if isinstance(_sandbox_boundary, str) and _sandbox_boundary.strip().lower() in _RETIRED_SANDBOX_POLICIES:
        print(col("  [DEPRECATED] security.sandbox off/none is retired and ignored; model command "
                  "isolation remains mandatory (using auto).", C.YELLOW))
        cfg["security"]["sandbox"] = "auto"


def _config_plugin_roots(cfg: Dict[str, Any]) -> Tuple[str, ...]:
    """Return the loaded, non-core namespaces that the core schema must preserve."""
    _core_roots = set(config_schema.defaults_tree())
    return tuple(k for k in cfg
                 if isinstance(k, str) and not k.startswith("_") and k not in _core_roots)


def _validate_config_projection(cfg: Dict[str, Any]) -> None:
    """Validate the complete merged core projection while retaining loaded plugin roots."""
    config_schema.validate(cfg, plugin_roots=_config_plugin_roots(cfg))


@dataclass(frozen=True)
class _DerivedConfigState:
    """An unpublished, immutable container for one complete runtime-state derivation."""

    values: "Mapping[str, Any]"
    warnings: Tuple[str, ...] = ()


_CONFIG_DERIVED_GLOBALS = (
    "DEFAULT_BASE_URL", "DEFAULT_MODEL", "API_KEY_ENV", "LLM_REQUEST_TIMEOUT_S",
    "LLM_CONNECT_TIMEOUT_S", "LLM_FIRST_TOKEN_TIMEOUT_S", "LLM_MAX_RETRIES", "STATE_ROOT",
    "VAULT_ROOT", "CODE_SUBDIR", "SESSION_FILE", "CODE_ROOT", "PLATFORM_MODE", "PLATFORM",
    "TASKS_DEDUP_THRESHOLD", "TASK_PREFIX", "_TASK_ID_RE", "FORGE_ENABLED", "FORGE_REPO",
    "FORGE_ADAPTER", "FORGE_TOKEN_ENV", "REVIEW_AGENT", "REVIEW_TIMEOUT_S", "NOTIFY_WEBHOOK",
    "AUDIT_SCOPE", "SANDBOX", "MULTI_TENANT", "TOOLING_ENVELOPE_POLICY", "ALERT_ENABLED",
    "LODESTAR_ENABLED", "ONBOARDING_MODE", "AUTOPILOT_ENABLED", "AUTOPILOT_CLAUDE_BIN",
    "AUTOPILOT_EXTRA_ARGS", "AUTOPILOT_DEFAULT_EFFORT", "AUTOPILOT_LOGS_DIR",
    "AUTOPILOT_MAX_CONCURRENT", "AUTOPILOT_STREAM", "AUTOPILOT_TERMINATE_ON_ADVANCE",
    "AUTOPILOT_AUTOPLAN", "AUTOPILOT_MAX_TASKS", "AUTOPILOT_LOG_TERMINAL", "TEMPERATURE",
    "MAX_TOKENS", "FINALIZE_ON_TRUNCATION", "RETRY_BACKOFF", "LANGUAGE", "MAX_ITERATIONS",
    "MAX_CTX_CHARS", "TRIM_TARGET_CHARS", "MAX_FILE_CHARS", "LIST_DIR_HARD_CAP",
    "SUMMARIZE_EVICTED", "SUMMARY_MAX_TOKENS", "EMERGENCY_SUMMARIZE", "PROACTIVE_ROLL",
    "INGEST_SOFT_FRAC", "MAX_SUMMARIES_PER_TURN", "RAG_ENABLED", "RAG_TOP_K",
    "RAG_MAX_TOKENS", "MAX_MODEL_LEN", "TOKEN_BUDGET", "CHARS_PER_TOKEN", "THINKING_RESERVE",
    "MIN_OUTPUT_TOKENS", "OVERFLOW_SAFETY_TOKENS", "TURN_IDLE_TIMEOUT_S",
    "MEMORY_BRIEF_TOKENS", "WORKER_MEMORY", "WORKER_WRITE", "WORKER_WRITE_MODE",
    "WARM_SESSION_ID", "_PLANNING_KW", "_ROUTINE_KW", "WORKSPACE_DIRS", "_IDLE_ACTIVE",
    "_MEMORY_CONFIG", "_WARM_CONFIG", "WATCHER_FEEDBACK_DIR", "RECONCILER_INTERVAL",
    "SPINNER_FRAMES", "UI_REFRESH_INTERVAL", "_UI_MAX_LINES", "_UI_LINES",
    "FRAMING_NOTES_ENABLED", "AUTOMATION_DECOUPLED", "HEARTBEAT_STALL_S", "CLAIM_LEASE_TTL_S",
    "_VERIFY_GROUNDING_THRESHOLD", "_STRATEGY_BUDGET", "_QUALITY_BREAKER", "_QUALITY_TRIPPED",
)


def _derive_config_state(cfg: Dict[str, Any]) -> _DerivedConfigState:
    """Derive every config-owned module global without publishing runtime state or side effects."""
    current = globals()
    (API_KEY_ENV, LLM_REQUEST_TIMEOUT_S, LLM_MAX_RETRIES, STATE_ROOT, VAULT_ROOT, CODE_ROOT,
     TASK_PREFIX, FORGE_ENABLED, FORGE_REPO, FORGE_ADAPTER, FORGE_TOKEN_ENV, REVIEW_AGENT,
     REVIEW_TIMEOUT_S, NOTIFY_WEBHOOK, AUDIT_SCOPE, MULTI_TENANT, ALERT_ENABLED, LODESTAR_ENABLED,
     FINALIZE_ON_TRUNCATION, SUMMARY_MAX_TOKENS, INGEST_SOFT_FRAC, MAX_SUMMARIES_PER_TURN,
     RAG_TOP_K, RAG_MAX_TOKENS, MAX_MODEL_LEN, CHARS_PER_TOKEN, THINKING_RESERVE,
     MIN_OUTPUT_TOKENS, OVERFLOW_SAFETY_TOKENS, TURN_IDLE_TIMEOUT_S, MEMORY_BRIEF_TOKENS,
     RECONCILER_INTERVAL, _UI_MAX_LINES, _UI_LINES) = (
        current[name] for name in (
            "API_KEY_ENV", "LLM_REQUEST_TIMEOUT_S", "LLM_MAX_RETRIES", "STATE_ROOT", "VAULT_ROOT",
            "CODE_ROOT", "TASK_PREFIX", "FORGE_ENABLED", "FORGE_REPO", "FORGE_ADAPTER",
            "FORGE_TOKEN_ENV", "REVIEW_AGENT", "REVIEW_TIMEOUT_S", "NOTIFY_WEBHOOK", "AUDIT_SCOPE",
            "MULTI_TENANT", "ALERT_ENABLED", "LODESTAR_ENABLED", "FINALIZE_ON_TRUNCATION",
            "SUMMARY_MAX_TOKENS", "INGEST_SOFT_FRAC", "MAX_SUMMARIES_PER_TURN", "RAG_TOP_K",
            "RAG_MAX_TOKENS", "MAX_MODEL_LEN", "CHARS_PER_TOKEN", "THINKING_RESERVE",
            "MIN_OUTPUT_TOKENS", "OVERFLOW_SAFETY_TOKENS", "TURN_IDLE_TIMEOUT_S",
            "MEMORY_BRIEF_TOKENS", "RECONCILER_INTERVAL", "_UI_MAX_LINES", "_UI_LINES",
        )
    )

    conn, paths, gen = cfg["connection"], cfg["paths"], cfg["generation"]
    ctx, ta, ws       = cfg["context"], cfg["thinking_auto"], cfg["workspace"]
    wa, ui            = cfg["watcher"], cfg["ui"]

    DEFAULT_BASE_URL = conn["base_url"]
    DEFAULT_MODEL    = conn["model"]
    API_KEY_ENV      = conn.get("api_key_env", API_KEY_ENV)
    LLM_REQUEST_TIMEOUT_S = float(conn.get("request_timeout_s", LLM_REQUEST_TIMEOUT_S))   # #1131: per-request LLM bound
    LLM_CONNECT_TIMEOUT_S = _opt_float(conn.get("connect_timeout_s"))
    LLM_FIRST_TOKEN_TIMEOUT_S = _opt_float(conn.get("first_token_timeout_s"))
    LLM_MAX_RETRIES       = int(conn.get("max_retries", LLM_MAX_RETRIES))                 # #1131
    STATE_ROOT       = paths.get("state_root", STATE_ROOT)
    VAULT_ROOT       = paths.get("vault_root", VAULT_ROOT)
    # S? (#1237): isolate the software tree (absent → off). CONTAINMENT — a code_subdir must be a RELATIVE path
    # inside the project; reject an absolute path, a drive (``D:``) or any ``..`` traversal, else it would
    # redirect every model/coder file op outside the tree (fall back to off = the project root).
    _cs = (paths.get("code_subdir", "") or "").strip().replace("\\", "/").strip("/")
    if _cs and (":" in _cs or ".." in _cs.split("/")):
        _cs = ""
    CODE_SUBDIR      = _cs
    SESSION_FILE     = paths["session_file"]
    CODE_ROOT        = paths.get("code_root", CODE_ROOT)

    PLATFORM_MODE = cfg["platform"]["mode"]
    PLATFORM      = _resolve_platform(PLATFORM_MODE)   # one-time resolution of 'auto'

    TASKS_DEDUP_THRESHOLD = float(cfg["tasks"]["dedup_threshold"])
    TASK_PREFIX           = str(cfg["tasks"].get("id_prefix", TASK_PREFIX))
    _TASK_ID_RE           = re.compile(rf"^{re.escape(TASK_PREFIX)}-[A-Za-z0-9_]+$")
    FORGE_ENABLED         = cfg.get("forge", {}).get("enabled", FORGE_ENABLED)   # #1073 default OFF
    FORGE_REPO            = str(cfg.get("forge", {}).get("repo", FORGE_REPO) or "")
    FORGE_ADAPTER         = str(cfg.get("forge", {}).get("adapter", FORGE_ADAPTER) or "cli").strip().lower()  # #1213
    FORGE_TOKEN_ENV       = str(cfg.get("forge", {}).get("token_env", FORGE_TOKEN_ENV) or "GX10_FORGE_TOKEN")  # #1213
    REVIEW_AGENT          = str(cfg.get("review", {}).get("agent", REVIEW_AGENT) or "").strip().upper()  # #1221
    try:
        REVIEW_TIMEOUT_S  = float(cfg.get("review", {}).get("timeout_s", REVIEW_TIMEOUT_S) or 180.0)  # #1221
    except (TypeError, ValueError):
        REVIEW_TIMEOUT_S  = 180.0
    NOTIFY_WEBHOOK        = str(cfg.get("notify", {}).get("webhook", NOTIFY_WEBHOOK) or "")   # #1083
    AUDIT_SCOPE           = str(cfg.get("audit", {}).get("scope", AUDIT_SCOPE) or "mutating").lower()   # #1067
    if AUDIT_SCOPE not in {"mutating", "all"}:
        raise ValueError("audit.scope must be 'mutating' or 'all'")
    _sandbox_raw = cfg.get("security", {}).get("sandbox", "auto")
    if isinstance(_sandbox_raw, str) and _sandbox_raw.strip().lower() in _RETIRED_SANDBOX_POLICIES:
        print(col("  [DEPRECATED] security.sandbox off/none is retired and ignored; model command "
                  "isolation remains mandatory (using auto).", C.YELLOW))
        cfg.setdefault("security", {})["sandbox"] = "auto"
        _sandbox_raw = "auto"
    SANDBOX               = _validated_sandbox_policy(_sandbox_raw)
    MULTI_TENANT          = cfg.get("security", {}).get("multi_tenant", MULTI_TENANT)   # #1071 default OFF
    from ack.tooling_envelope import load_tooling_envelope_policy
    TOOLING_ENVELOPE_POLICY = load_tooling_envelope_policy(cfg)   # ADR-0007: always-on launch enforcement
    ALERT_ENABLED         = cfg.get("alert", {}).get("enabled", ALERT_ENABLED)   # #1061 default OFF
    LODESTAR_ENABLED      = cfg.get("lodestar", {}).get("enabled", LODESTAR_ENABLED)
    ONBOARDING_MODE       = cfg["onboarding"]["enabled"]

    ap = cfg["autopilot"]
    AUTOPILOT_ENABLED        = ap["enabled"]
    AUTOPILOT_CLAUDE_BIN     = ap["claude_bin"]
    AUTOPILOT_EXTRA_ARGS     = list(ap["extra_args"])
    AUTOPILOT_DEFAULT_EFFORT = ap["default_effort"]
    AUTOPILOT_LOGS_DIR       = ap["logs_dir"]
    AUTOPILOT_MAX_CONCURRENT = int(ap["max_concurrent"])
    AUTOPILOT_STREAM         = ap.get("stream", False)
    AUTOPILOT_TERMINATE_ON_ADVANCE = ap.get("terminate_on_advance", False)
    AUTOPILOT_AUTOPLAN    = ap.get("autoplan", False)
    AUTOPILOT_MAX_TASKS   = int(ap["autoplan_max_tasks"])
    AUTOPILOT_LOG_TERMINAL = ap.get("log_terminal", False)

    TEMPERATURE   = float(gen["temperature"])
    MAX_TOKENS    = int(gen["max_tokens"])
    FINALIZE_ON_TRUNCATION = gen.get("finalize_on_truncation", FINALIZE_ON_TRUNCATION)
    RETRY_BACKOFF = float(gen["retry_backoff"])
    LANGUAGE      = (str(gen.get("language", "en")).strip() or "en")

    MAX_ITERATIONS    = int(ctx["max_iterations"])
    MAX_CTX_CHARS     = int(ctx["max_ctx_chars"])
    TRIM_TARGET_CHARS = int(ctx["trim_target_chars"])
    MAX_FILE_CHARS    = int(ctx["max_file_chars"])
    LIST_DIR_HARD_CAP = int(ctx["list_dir_hard_cap"])
    SUMMARIZE_EVICTED  = ctx.get("summarize_evicted", True)    # B1: default ON (06-18)
    SUMMARY_MAX_TOKENS = int(ctx.get("summary_max_tokens", SUMMARY_MAX_TOKENS))
    EMERGENCY_SUMMARIZE = ctx.get("emergency_summarize", False)   # #1050 L3: default OFF
    PROACTIVE_ROLL         = ctx.get("proactive_roll", False)           # #1051 L3: default OFF
    INGEST_SOFT_FRAC       = float(ctx.get("ingest_soft_frac", INGEST_SOFT_FRAC))
    MAX_SUMMARIES_PER_TURN = int(ctx.get("max_summaries_per_turn", MAX_SUMMARIES_PER_TURN))
    RAG_ENABLED        = ctx.get("rag_enabled", True)          # B2: default ON (06-18)
    RAG_TOP_K          = int(ctx.get("rag_top_k", RAG_TOP_K))
    RAG_MAX_TOKENS     = int(ctx.get("rag_max_tokens", RAG_MAX_TOKENS))
    # MEM-9: couple the trim working set to the model window (after output/RAG/summary reserve). ON →
    # derive MAX_CTX_CHARS/TRIM_TARGET_CHARS from MAX_MODEL_LEN (overrides the char defaults);
    # OFF → the char thresholds above stay (today's behaviour, GX10_MAX_CTX_CHARS applies).
    MAX_MODEL_LEN      = int(ctx.get("max_model_len", MAX_MODEL_LEN))
    TOKEN_BUDGET       = ctx.get("token_budget", True)
    CHARS_PER_TOKEN    = float(ctx.get("chars_per_token", CHARS_PER_TOKEN))   # #366 calibrated fallback
    THINKING_RESERVE   = int(ctx.get("thinking_reserve", THINKING_RESERVE))   # #366 D5
    MIN_OUTPUT_TOKENS  = max(1, int(ctx.get("min_output_tokens", MIN_OUTPUT_TOKENS)))   # #366 adaptive-reserve floor
    OVERFLOW_SAFETY_TOKENS = max(0, int(ctx.get("overflow_safety_tokens", OVERFLOW_SAFETY_TOKENS)))   # #366 estimate-slop headroom
    TURN_IDLE_TIMEOUT_S = float(ctx.get("turn_idle_timeout_s", TURN_IDLE_TIMEOUT_S))   # #1132: idle-watchdog bound
    MEMORY_BRIEF_TOKENS = int(ctx.get("memory_brief_tokens", MEMORY_BRIEF_TOKENS))   # #458 D1 handover brief budget
    if TOKEN_BUDGET and not (os.environ.get("GX10_MAX_CTX_CHARS") or os.environ.get("GX10_TRIM_TARGET_CHARS")):
        # MEM-9: derive the char watermark from the window (output/RAG/summary reserve). BUDGET-3 (#503):
        # SKIP the derive when the operator explicitly set GX10_MAX_CTX_CHARS / GX10_TRIM_TARGET_CHARS, so
        # those env vars are honored instead of being silently overwritten (the token budget stays primary).
        MAX_CTX_CHARS, TRIM_TARGET_CHARS = _derive_ctx_budget(
            MAX_MODEL_LEN, MAX_TOKENS, RAG_MAX_TOKENS, SUMMARY_MAX_TOKENS, CHARS_PER_TOKEN)
    _wcfg = cfg.get("workers", {})
    WORKER_MEMORY      = _wcfg.get("memory_read", True)    # §3c MAP: default ON (06-18)
    WORKER_WRITE       = _wcfg.get("memory_write", True)   # §3c REDUCE: default ON (06-18)
    WORKER_WRITE_MODE  = _wcfg.get("write_mode", "reducer")
    WARM_SESSION_ID    = (os.environ.get("GX10_SESSION_ID", "").strip() or "main")   # pure-from-base, no self-ref accumulation (S3b)

    _PLANNING_KW = tuple(ta["planning_keywords"])
    _ROUTINE_KW  = tuple(ta["routine_keywords"])

    WORKSPACE_DIRS = list(ws["dirs"])
    _IDLE_ACTIVE   = ws["idle_marker"]

    # Memory config: file (conf/memory/memory.json) OR env (GX10_MEMORY_URL).
    # Optional — without base_url _MEMORY_CONFIG stays empty → memory off (hooks inert).
    _MEMORY_CONFIG = copy.deepcopy(cfg.get("memory") or {})
    _mem_cfg_path = _BOOT_CWD / "conf" / "memory" / "memory.json"
    if _mem_cfg_path.exists():
        # An external component seam: MemoryManager owns its own field handling + fallbacks (incl. the
        # legacy `timeout` key), so a memory.json may legitimately carry extra/legacy keys. Merge it
        # tolerantly — the env memory config is already typed via _apply_env + the boot validate — rather
        # than hard-refusing an out-of-schema file key (which main accepted and the component still reads).
        _MEMORY_CONFIG = _deep_merge(_MEMORY_CONFIG, _read_json_dict(_mem_cfg_path))
    _mem_url = os.environ.get("GX10_MEMORY_URL")
    if _mem_url:
        _MEMORY_CONFIG = {**(_MEMORY_CONFIG or {}), "base_url": _mem_url}
        _MEMORY_CONFIG.setdefault("enabled", True)
        _MEMORY_CONFIG.setdefault("agent_id", os.environ.get("GX10_MEMORY_AGENT", "ironclad"))
    # Warm tier config (B0): file (conf/warm/warm.json) OR env (GX10_WARM_URL).
    # Optional — without a url _WARM_CONFIG stays empty → warm tier off (no-op, fail-soft).
    _WARM_CONFIG = copy.deepcopy(cfg.get("warm") or {})
    _warm_cfg_path = _BOOT_CWD / "conf" / "warm" / "warm.json"
    if _warm_cfg_path.exists():
        # External seam (WarmTier owns its field handling) — merge the file tolerantly, same rationale as
        # the memory seam above; the env warm config is already typed via _apply_env + the boot validate.
        _WARM_CONFIG = _deep_merge(_WARM_CONFIG, _read_json_dict(_warm_cfg_path))
    _warm_url = os.environ.get("GX10_WARM_URL")
    if _warm_url:
        _WARM_CONFIG = {**(_WARM_CONFIG or {}), "url": _warm_url}
        _WARM_CONFIG.setdefault("enabled", True)

    WATCHER_FEEDBACK_DIR = wa["feedback_dir"]
    RECONCILER_INTERVAL  = float(wa.get("interval", RECONCILER_INTERVAL))

    SPINNER_FRAMES      = ui["spinner_frames"]
    UI_REFRESH_INTERVAL = float(ui["refresh_interval"])
    new_max = int(ui["max_lines"])
    if new_max != _UI_MAX_LINES:
        _UI_MAX_LINES = new_max
        _UI_LINES = deque(_UI_LINES, maxlen=new_max)

    # The typed schema enforces the one unambiguous timeout invariant (connect <= request); request,
    # idle-watchdog, and first-token bound DIFFERENT things and are independently tuned per deployment
    # (#1131/#1397), so no cross-ordering between them is asserted or warned here.
    warnings: List[str] = []

    FRAMING_NOTES_ENABLED = cfg["framing_notes"]["enabled"]
    AUTOMATION_DECOUPLED = cfg["automation"]["decoupled"]
    HEARTBEAT_STALL_S = float(cfg["heartbeat"]["stall_seconds"])
    CLAIM_LEASE_TTL_S = float(cfg["heartbeat"]["claim_lease_seconds"])
    _VERIFY_GROUNDING_THRESHOLD = float(cfg["verify"]["grounding_threshold"])
    from ack.validated_emit import MAX_RETRY_BUDGET
    _STRATEGY_BUDGET = min(int(cfg["strategy"]["budget"]), MAX_RETRY_BUDGET)
    _QUALITY_BREAKER, _QUALITY_TRIPPED = _derive_quality_breaker_state(cfg)

    derived_locals = locals()
    values = {name: derived_locals[name] for name in _CONFIG_DERIVED_GLOBALS}
    return _DerivedConfigState(MappingProxyType(values), tuple(warnings))


_CONFIG_RECONFIG_GLOBALS = (
    "_NOTIFY_CONSUMER", "_ACE_STORE", "_ACE_WORKER", "_ACE_MIGRATED", "_ACE_FORK_MPR",
    "_ACE_FORK_WORKER",
)


@dataclass(frozen=True)
class _ConfigRuntimeSnapshot:
    """Exact pre-transaction runtime and reversible integration state."""

    globals: "Mapping[str, Any]"
    hooks: "Optional[Dict[str, tuple]]"
    lesson_provider: Any
    ace_store: Any
    ace_store_attrs: "Optional[Dict[str, Any]]"
    ace_fork_inflight: frozenset


def _snapshot_config_runtime() -> _ConfigRuntimeSnapshot:
    names = _CONFIG_DERIVED_GLOBALS + _CONFIG_RECONFIG_GLOBALS
    runtime_globals = MappingProxyType({name: globals().get(name) for name in names})
    hooks_snapshot = None
    lesson_provider = None
    try:
        from ack import hooks as _hooks
        with _hooks._LOCK:
            hooks_snapshot = dict(_hooks._HOOKS)
    except Exception:  # noqa: BLE001 -- an absent optional bus has no state to snapshot
        pass
    try:
        from ack import lessons as _lessons
        lesson_provider = _lessons.get_provider()
    except Exception:  # noqa: BLE001 -- an absent optional provider has no state to snapshot
        pass
    store = globals().get("_ACE_STORE")
    store_attrs = None
    if store is not None and hasattr(store, "__dict__"):
        # Shallow-copy the attr dict, then deep-copy ONLY the one nested-mutable attr the ACE
        # reconfiguration touches in place: PlaybookStore.configure/set_transports mutate scalars
        # (_max/_top_k/_chat/_embed/_budget/_eval_fn) plus `self._config` (AdaptConfig.max_bullets).
        # Scalars restore by rebind; `_config` needs the deep copy. A blanket deepcopy is NOT usable —
        # `self._lock` is a threading.Lock (not deep-copyable). If a future reconfig method mutates any
        # OTHER nested-mutable attr in place, add it to this deep-copy set so rollback stays exact.
        store_attrs = dict(store.__dict__)
        if "_config" in store_attrs:
            store_attrs["_config"] = copy.deepcopy(store_attrs["_config"])
    return _ConfigRuntimeSnapshot(
        runtime_globals,
        hooks_snapshot,
        lesson_provider,
        store,
        store_attrs,
        frozenset(globals().get("_ACE_FORK_INFLIGHT", ())),
    )


def _restore_config_runtime(snapshot: _ConfigRuntimeSnapshot) -> None:
    """Restore globals and integration registries after a failed runtime commit."""
    for name in ("_ACE_WORKER", "_ACE_FORK_WORKER"):
        current = globals().get(name)
        previous = snapshot.globals.get(name)
        if current is not None and current is not previous:
            try:
                current.stop()
            except Exception:  # noqa: BLE001 -- restoration continues through every remaining seam
                pass
    globals().update(snapshot.globals)
    if snapshot.ace_store is not None and snapshot.ace_store_attrs is not None:
        snapshot.ace_store.__dict__.clear()
        snapshot.ace_store.__dict__.update(snapshot.ace_store_attrs)
    inflight = globals().get("_ACE_FORK_INFLIGHT")
    if isinstance(inflight, set):
        inflight.clear()
        inflight.update(snapshot.ace_fork_inflight)
    try:
        from ack import lessons as _lessons
        _lessons.set_provider(snapshot.lesson_provider)
    except Exception:  # noqa: BLE001 -- restoration continues through every remaining seam
        pass
    if snapshot.hooks is not None:
        try:
            from ack import hooks as _hooks
            with _hooks._LOCK:
                _hooks._HOOKS.clear()
                _hooks._HOOKS.update(snapshot.hooks)
        except Exception:  # noqa: BLE001 -- globals are still restored if an optional bus disappeared
            pass


def _commit_config_state(derived: _DerivedConfigState) -> None:
    """Publish one completely derived module-global state. Caller holds ``_CONFIG_LOCK``."""
    globals().update(derived.values)


def _apply_config_reconfiguration(cfg: Dict[str, Any], *, strict: bool) -> None:
    """Apply reversible hooks/stores/threads after the derived globals are committed."""
    _apply_notify(cfg, strict=strict)
    _apply_quality_consumer(cfg, strict=strict)
    # ACE is last: it is the only reconfiguration that may start/stop workers. A later core step can
    # therefore never fail after an existing worker has been stopped.
    _apply_ace(cfg, strict=strict)


def _apply_config(cfg: Dict[str, Any]):
    """Fast startup apply: normalize, validate, derive, commit, then wire integrations."""
    with _CONFIG_LOCK:
        _normalize_config_for_apply(cfg)
        _validate_config_projection(cfg)
        derived = _derive_config_state(cfg)
        _commit_config_state(derived)
        _apply_config_reconfiguration(cfg, strict=False)
        for warning in derived.warnings:
            print(col(warning, C.YELLOW))


def _config_set_atomic(key: str, value: Any) -> Optional[str]:
    """Clone-validate-derive-commit one runtime leaf, or return a refusal with no live mutation."""
    global _EFFECTIVE_CFG
    with _CONFIG_LOCK:
        if _EFFECTIVE_CFG is None:
            return "no live config to set (start the server first)"
        if (key in {"context.max_ctx_chars", "context.trim_target_chars"}
                and (_EFFECTIVE_CFG.get("context") or {}).get("token_budget")):
            return (f"{key} is derived from the model token budget while context.token_budget is on — "
                    "set context.token_budget off first (or GX10_TOKEN_BUDGET=0) to set char sizes directly")
        if key in _FROZEN_CONFIG_KEYS:
            return f"'{key}' is boot-only -- set it in config and restart"
        original = _EFFECTIVE_CFG
        root = key.split(".", 1)[0]
        if root not in original:
            return f"unknown configuration key '{key}'"
        core_roots = set(config_schema.defaults_tree())
        if root in core_roots and key not in config_schema.LEAVES:
            return f"{key}: unknown configuration leaf"
        try:
            candidate = copy.deepcopy(original)
            _cfg_set(candidate, key, value)
            _normalize_config_for_apply(candidate)
            _validate_config_projection(candidate)
            derived = _derive_config_state(candidate)
        except Exception as exc:  # noqa: BLE001 -- every candidate/derivation failure is one refusal
            return str(exc) or type(exc).__name__

        snapshot = _snapshot_config_runtime()
        try:
            _commit_config_state(derived)
            _apply_config_reconfiguration(candidate, strict=True)
        except Exception as exc:  # noqa: BLE001 -- restore every seam before reporting the refusal
            _restore_config_runtime(snapshot)
            _EFFECTIVE_CFG = original
            return f"runtime apply failed: {exc}"

        _EFFECTIVE_CFG = candidate
        for warning in derived.warnings:
            print(col(warning, C.YELLOW))
        return None


def _as_bool(v: object) -> bool:
    """Return an actual config boolean; never apply Python truthiness."""
    if type(v) is not bool:
        raise config_schema.ConfigError(f"expected bool, got {type(v).__name__}")
    return v


def _apply_framing_notes(cfg: Dict[str, Any]) -> None:
    """Capture ``framing_notes.enabled`` for optional framing-note capture and tool exposure.

    S1 retired the product presence gate; absent/invalid remains False for byte-identical defaults.
    """
    global FRAMING_NOTES_ENABLED
    try:
        FRAMING_NOTES_ENABLED = _as_bool(_cfg_get(cfg, "framing_notes.enabled"))
    except Exception:  # noqa: BLE001 -- advisory wiring: default to off
        FRAMING_NOTES_ENABLED = False


def _apply_automation(cfg: Dict[str, Any]) -> None:
    """S7 (#1229): capture ``automation.decoupled`` (watcher/autopilot disentangle). Default OFF → byte-
    identical coupled loop; DEV-1 turns it on. Fail-soft."""
    global AUTOMATION_DECOUPLED
    try:
        AUTOMATION_DECOUPLED = _as_bool(_cfg_get(cfg, "automation.decoupled"))
    except Exception:   # noqa: BLE001 — advisory wiring: default to off
        AUTOMATION_DECOUPLED = False


def _apply_heartbeat(cfg: Dict[str, Any]) -> None:
    """Capture positive finite ``heartbeat.stall_seconds`` tuning; invalid values restore 900 seconds."""
    global HEARTBEAT_STALL_S
    try:
        raw = _cfg_get(cfg, "heartbeat.stall_seconds")
        if isinstance(raw, bool):
            raise ValueError("heartbeat.stall_seconds must be a positive finite number")
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("heartbeat.stall_seconds must be a positive finite number")
        HEARTBEAT_STALL_S = value
    except Exception:   # noqa: BLE001 — invalid tuning cannot disable the protection
        HEARTBEAT_STALL_S = 900.0


def _apply_lessons_provider(cfg: Dict[str, Any]) -> None:
    """Deprecated compatibility seam; ACE owns provider registration and legacy-lesson migration."""
    return


_NOTIFY_CONSUMER = None   # #1083: the currently-registered escalation → webhook consumer (None = none)


def _apply_notify(cfg: Dict[str, Any], *, strict: bool = False) -> None:
    """#1083: (un)register the `escalation` → webhook notifier per `notify.webhook` (a deploy secret via
    GX10_NOTIFY_WEBHOOK — never a URL literal in core). DEFAULT-OFF: an empty URL removes the consumer, so a
    dispatch is an O(1) no-op → byte-identical. Idempotent — tracks the registered consumer so a URL change
    swaps cleanly. Lazy-imports ``ack`` (S6b). Fail-soft: notification wiring never breaks config apply."""
    global _NOTIFY_CONSUMER
    try:
        url = str(((cfg or {}).get("notify") or {}).get("webhook", "") or "").strip()
        from ack import hooks as _hooks       # lazy: never import ack at gx10 top-level (S6b lesson)
        if _NOTIFY_CONSUMER is not None:
            _hooks.unregister_hook("escalation", _NOTIFY_CONSUMER)
            _NOTIFY_CONSUMER = None
        if url:
            import notify as _notify
            _NOTIFY_CONSUMER = _notify.make_escalation_consumer(url)
            _hooks.register_hook("escalation", _NOTIFY_CONSUMER)
    except Exception:   # noqa: BLE001 — boot stays fail-soft; runtime transactions must roll back
        _NOTIFY_CONSUMER = None
        if strict:
            raise


# ═══ ACE (epic #855 ACE-WIRE / #863): the always-on Agentic Context Engineering loop-intelligence core ═══
# ACE SUPERSEDES the #602 string lesson + Process-SC consumers (operator decision 2026-06-30): it is the
# engine's loop-intelligence mechanic, always on (NO enable flag). `_apply_ace` registers a PlaybookStore as
# the ack.lessons provider and a `post_feedback` consumer that SUBMITS a Trajectory to a background
# ReflectionWorker — the reflect→curate→refine runs OFF the hot path (NEVER inline on the turn, the C0
# correctness requirement). The orchestrator-model chat, the memory-service /embed adapter and the token
# budget are injected into the store. Everything is fail-soft: with no model/endpoint reachable ACE no-ops.
_ACE_STORE = None              # the registered PlaybookStore (None until the first _apply_ace)
_ACE_WORKER = None             # the background ReflectionWorker draining adaptation tasks off the hot path
_ACE_MIGRATED = False          # one-time #602 EngineLessonStore → playbook migration guard (per process)
#: M5-2 (#883): the gated MPR-at-fork OPTION. `_ACE_FORK_MPR` mirrors `cfg['ace']['fork_mpr']['enabled']`
#: (default OFF — gate OFF ⇒ byte-identical to today's STOP-and-ask). When ON, a recognized `ForkSignal`
#: fires MPR's `architecture-decision` panel OFF the hot path on `_ACE_FORK_WORKER` (a ReflectionWorker
#: reused as a generic queue worker) so the dev-loop / turn path is never blocked. NOT a ProjectContext-scoped
#: global (not linted by check_no_raw_globals).
_ACE_FORK_MPR = False
_ACE_FORK_WORKER = None
#: M5-2/#904: forks dispatched to the fork worker but not yet completed (process-local) — the scan skips these
#: so a fork is never dispatched twice concurrently, while the DURABLE exactly-once key is committed only AFTER
#: the MPR run completes (so a queue-drop / worker crash leaves the fork un-committed and it is retried).
_ACE_FORK_INFLIGHT: "set" = set()
#: M4-0 (#877): which playbook bullet ids were injected into a task's handover (keyed by task_id), so the
#: post_feedback consumer can populate Trajectory.used_bullet_ids and the Reflector can rate them
#: helpful/harmful (E-004/H-002). Bounded in-memory map (popped on consume); NOT a ProjectContext-scoped
#: global (not linted by check_no_raw_globals). M4-3 (#880) persists the per-unit variant for the dev-loop.
_ACE_INJECTED: "Dict[str, List[str]]" = {}
_ACE_INJECTED_CAP = 512        # safety bound: a task whose handover is staged but never advanced can't leak unboundedly

_ACE_BULLET_ID_RE = re.compile(r"^\s*-\s*\[([^\]]+)\]")   # the per-line `- [id] …` token _render_bullets emits


def _ace_bullet_ids(rendered: str) -> "List[str]":
    """Parse the injected bullet ids from a rendered playbook context (the leading ``- [id]`` token on each
    line — the format ``PlaybookStore._render_bullets`` emits). Never raises."""
    try:
        out: "List[str]" = []
        for line in (rendered or "").splitlines():
            m = _ACE_BULLET_ID_RE.match(line)
            if m:
                out.append(m.group(1))
        return out
    except Exception:   # noqa: BLE001 — advisory: an id parse must never break the handover
        return []


def _ace_record_injected(task_id: str, bullet_ids: "List[str]") -> None:
    """Record the bullet ids injected into *task_id*'s handover (M4-0). Bounded + fail-soft; a no-op on an
    empty task_id / empty id list."""
    try:
        if not (task_id and bullet_ids):
            return
        if len(_ACE_INJECTED) >= _ACE_INJECTED_CAP:    # drop the oldest to stay bounded (insertion-ordered dict)
            for stale in list(_ACE_INJECTED)[: max(1, len(_ACE_INJECTED) - _ACE_INJECTED_CAP + 1)]:
                _ACE_INJECTED.pop(stale, None)
        _ACE_INJECTED[task_id] = list(bullet_ids)
    except Exception:   # noqa: BLE001 — advisory
        return


def _ace_take_injected(task_id: str) -> "List[str]":
    """Pop + return the bullet ids injected into *task_id*'s handover (or ``[]``). Never raises."""
    try:
        return _ACE_INJECTED.pop(task_id, []) if task_id else []
    except Exception:   # noqa: BLE001 — advisory
        return []


#: M4-3 (#880): issue#s a handover references — the standard `Closes/Fixes/Resolves #N` linkage + a `(#N)` /
#: `#N` token in the task title. Used to key the durable injected-bullet map by the dev-loop UNIT (issue#),
#: so the per-unit ledger scan (M4-2) can correlate which bullets the unit's handover carried.
_ACE_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", re.IGNORECASE)
_ACE_HASHNUM_RE = re.compile(r"#(\d+)\b")


def _ace_unit_keys(task_id: str, fields: "Any", ho_md: str) -> "List[str]":
    """The keys to durably record a handover's injected bullets under: the engine task id PLUS the issue# the
    handover is FOR — the FIRST `#N` in the task title (the primary unit; C2 #906 — only the first, so a title
    that also mentions another issue does not cross-attribute) plus any `Closes/Fixes/Resolves #N` link in the
    body (deliberate linkage). The per-UNIT ledger trajectory (keyed by the ledger `unit` = issue#) reads back
    by issue#. Never raises."""
    keys: "List[str]" = []
    try:
        if task_id:
            keys.append(str(task_id))
        title = str((fields or {}).get("title") or "") if isinstance(fields, dict) else ""
        title_hits = _ACE_HASHNUM_RE.findall(title)       # the PRIMARY unit is the first `#N` (e.g. "fix(#503): …")
        if title_hits:
            keys.append(str(title_hits[0]))
        for n in _ACE_CLOSES_RE.findall(ho_md or ""):     # a `Closes #N` linkage in the handover body (deliberate)
            keys.append(str(n))
        # de-dup, order-preserving
        seen: set = set()
        return [k for k in keys if not (k in seen or seen.add(k))]
    except Exception:   # noqa: BLE001 — advisory: a key-derivation hiccup must never break the handover
        return [str(task_id)] if task_id else []


def _ace_persist_injected(keys: "List[str]", bullet_ids: "List[str]") -> None:
    """Durably record *bullet_ids* under each of *keys* (task id + issue#s) in the shared per-unit map, so the
    cross-process M4-2 ledger scan can populate ``used_bullet_ids``. Advisory + fail-soft."""
    try:
        if not (keys and bullet_ids):
            return
        from playbook_store import record_unit_bullets   # bare engine-sibling import
        from project_registry import ironclad_home
        home = ironclad_home()
        for k in keys:
            record_unit_bullets(home, k, bullet_ids)
    except Exception:   # noqa: BLE001 — advisory: a correlation write must never break the handover
        return


def _ace_chat_adapter():
    """A single-shot orchestrator-model completion ``chat(prompt)->str`` for the Reflector, built from the
    effective connection config. Runs OFF the hot path in the ReflectionWorker. Any transport error
    propagates to ``ack.ace.reflect``, which treats it as an empty reflection (no-op) — so a missing/
    unreachable model just means ACE doesn't learn this round.

    #922: the Reflector emits STRUCTURED JSON (insights/ratings), NOT free reasoning — so qwen3 thinking is
    disabled (``enable_thinking: False``, mirroring MPR's classify/``complete_json`` + ``workers._one``).
    With thinking ON a reasoning model (qwen3.6-35b) burns the whole token cap on a ``<think>`` block
    (``finish_reason=length``) and returns EMPTY ``content`` → 0 insights → the always-on loop never learns.
    This is a live-only gap the stub-transport unit tests miss; a busy reasoning model made ACE inert."""
    def chat(prompt: str) -> str:
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        conn = (cfg.get("connection") or {})
        client = OpenAI(base_url=(conn.get("base_url") or DEFAULT_BASE_URL),
                        api_key=(os.environ.get(conn.get("api_key_env", "GX10_API_KEY")) or "not-needed"),
                        timeout=LLM_REQUEST_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)   # #1131: fail-soft bound
        resp = client.chat.completions.create(
            model=(conn.get("model") or DEFAULT_MODEL),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048, temperature=0.2, stream=False,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},   # #922: structured emission, not reasoning
        )
        return (resp.choices[0].message.content or "")
    return chat


def _ace_embed_adapter():
    """A batched embedder ``embed(texts)->List[List[float]]`` over the memory-service ``/embed`` endpoint
    (BGE-M3) for ACE's semantic dedup + relevant-bullet retrieval. URL = ``ace.embed_url`` else derived from
    ``GX10_MEMORY_URL``; with neither set returns ``None`` so ``ack.ace`` falls back to its dependency-free
    lexical similarity. The callable may raise (network) — ``ack.ace.grow``/``generator`` wrap it fail-soft."""
    cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
    url = str((cfg.get("ace") or {}).get("embed_url") or "").strip()
    if not url:
        mem = os.environ.get("GX10_MEMORY_URL", "").strip().rstrip("/")
        url = (mem + "/embed") if mem else ""
    if not url:
        return None
    def embed(texts):
        import urllib.request
        body = json.dumps({"texts": list(texts)}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:   # noqa: S310 — operator-configured memory URL
            data = json.loads(r.read().decode("utf-8"))
        return data.get("vectors") or []
    return embed


def _ace_run_task(task) -> None:
    """The ReflectionWorker's per-item processor: run one online adaptation for the submitted scope's
    playbook. Fail-soft — the worker already guards, this just routes by scope."""
    store = _ACE_STORE
    if store is None or not isinstance(task, dict):
        return
    store.adapt(task.get("trajectory"), scope=str(task.get("scope") or ""))


def _ace_consumer_hook(ctx) -> None:
    """`post_feedback` consumer (ACE-WIRE + M4-0 #877): on a fresh COMPLETION **or** a genuine FAILURE, build a
    Trajectory from the task record + the bullets that were injected into this task's handover + the archived
    feedback, and SUBMIT it to the background ReflectionWorker — an O(1), non-blocking enqueue that NEVER runs
    the model inline (the C0 hot-path requirement). The LABEL-FREE outcome is derived from the advance result:
    a fresh ``OK: pipeline advanced`` ⇒ ``success``; an ``ERROR: pipeline step failed`` ⇒ ``failed`` (so the
    Reflector learns from failures too — E-001/O-002 — and rates the used bullets harmful, E-004/H-002). It
    SKIPS an already-done re-advance and trivial precondition errors (bad task_id / unknown agent / missing
    feedback / no active initiative) — those are not a real attempt. Gated on a bound scope + a wired worker.
    **Fail-soft** (never raises)."""
    try:
        ctx = ctx or {}
        result = str(ctx.get("result") or "")
        fresh_success = result.startswith("OK: pipeline advanced")
        genuine_failure = result.startswith("ERROR: pipeline step failed")
        if not (fresh_success or genuine_failure):
            return                                   # already-done re-advance / trivial precondition error → no learning
        worker = _ACE_WORKER
        if worker is None or _ACE_STORE is None:
            return
        scope = _active_mem_ns()
        if not scope:                                # no project bound (base partition) → nothing to learn
            return
        task_id = str(ctx.get("task_id") or ""); agent = str(ctx.get("agent") or "")
        existing = _store().get(task_id) if task_id else None
        title = str((existing or {}).get("title") or "") if isinstance(existing, dict) else ""
        ttype = str((existing or {}).get("type") or "") if isinstance(existing, dict) else ""
        vfb = archive_feedback_dir() / f"{task_id}_{agent}-feedback.md"   # archived by step 2 of the advance
        fb = vfb.read_text(encoding="utf-8").strip() if vfb.exists() else ""
        used = _ace_take_injected(task_id)           # M4-0: the bullets this task's handover actually carried
        outcome = "success" if fresh_success else "failed"
        steps = [fb] if fb else ([f"pipeline step failed: {result.split(':',2)[-1].strip()[:200]}"] if genuine_failure else [])
        from ack.ace import Trajectory      # lazy: never import ack at gx10 top-level (S6b lesson)
        traj = Trajectory(query=(title or ttype or task_id or "task"),
                          steps=steps, outcome=outcome, used_bullet_ids=used)
        worker.submit({"scope": scope, "trajectory": traj})   # non-blocking; the worker reflects off the hot path
    except Exception:   # noqa: BLE001 — advisory: a reflection submit must never break a turn
        return


# ── M4-2 (#879): the dev-process learn-trigger — scan the dev-loop ledger off the hot path, submit each
# newly-TERMINAL unit's Trajectory to the ReflectionWorker exactly-once. Variant A (ledger-derived): the
# ledger is read as PLAIN DATA via `_read_ledger_payloads` (no import of the private scripts/devloop); a
# chain-tampered/unreadable ledger is skipped fail-closed (never a learning source). Distinct from the
# in-process post_feedback hook (#863/M4-0, per-handover) — this is the per-UNIT merge/abort arc, and the
# dev-loop runner is a SEPARATE process whose ledger writes never fire the in-process hook (no double-learning).
_ACE_TERMINAL_OUTCOMES = frozenset({"reached-human-merge-gate", "aborted"})


def _ace_devscan_path() -> "Optional[Path]":
    """The persisted exactly-once record (the set of dev-loop units already submitted), scoped PER-PROJECT
    (#979) under the install home — so one project's submitted-units set never masks another's. ``None`` if
    the home can't resolve."""
    try:
        from project_registry import ironclad_home   # bare engine-sibling import (like project_registry)
        return ironclad_home() / "ace_devscan" / f"{_active_mem_ns() or 'base'}.json"
    except Exception:   # noqa: BLE001
        return None


def _ace_load_submitted() -> set:
    try:
        p = _ace_devscan_path()
        data = json.loads(p.read_text(encoding="utf-8")) if p and p.is_file() else {}
        return set(data.get("submitted") or []) if isinstance(data, dict) else set()
    except Exception:   # noqa: BLE001 — a missing/corrupt record reads as empty (re-learn is bounded by the set we then save)
        return set()


def _ace_save_submitted(submitted: set) -> None:
    tmp = None
    try:
        p = _ace_devscan_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"submitted": sorted(submitted)}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:   # noqa: BLE001 — advisory persistence; never break the caller
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _ace_scan_dev_ledger(payloads: "Any" = None, chain_errors: "Any" = None, *,
                         ledger_path: "Optional[Path]" = None) -> int:
    """OFF the hot path (DP-4): project the dev-loop ledger into per-unit Trajectories (via
    `ack.ace.devtraj`) and submit each NEWLY-TERMINAL unit (reached-human-merge-gate / aborted) to the
    background ReflectionWorker **exactly once** (a persisted submitted-units set survives restarts). The
    ledger is read as plain data; a chain-tampered/unreadable ledger (``chain_errors``) is skipped
    fail-closed — never learned from. ``payloads``/``chain_errors`` may be passed in (the `/lifecycle gate`
    site reuses what it already read) or read here. ``scope`` = the active project mem-ns (project-private,
    like #863). Returns the number submitted. **Fail-soft** — never raises, never blocks a turn or the dev-loop."""
    try:
        worker = _ACE_WORKER
        if worker is None or _ACE_STORE is None:
            return 0
        scope = _active_mem_ns()
        if not scope:                                # no bound project → nothing to attribute learning to
            return 0
        if payloads is None:
            path = ledger_path or ((_project_root() or Path.cwd()) / ".devloop" / "ledger.jsonl")
            payloads, chain_errors = _read_ledger_payloads(path)
        if chain_errors:                             # tamper / unreadable → fail-closed, never learn from a corrupt ledger
            return 0
        from ack.ace import ledger_to_trajectories   # lazy: never import ack at gx10 top-level (S6b lesson)
        from playbook_store import read_unit_bullets   # M4-3: the per-unit injected-bullet correlation
        from project_registry import ironclad_home
        home = ironclad_home()
        submitted = _ace_load_submitted()
        n = 0
        for traj in ledger_to_trajectories(payloads):
            if traj.outcome in _ACE_TERMINAL_OUTCOMES and traj.query not in submitted:
                # M4-3 (#880): populate used_bullet_ids from the durable map keyed by the unit (issue#) — the
                # bullets injected into this unit's handover(s) — so the Reflector rates them (E-004). [] if none.
                try:
                    traj.used_bullet_ids = read_unit_bullets(home, str(traj.query))
                except Exception:   # noqa: BLE001 — advisory: correlation is best-effort, [] is "weaker not wrong"
                    pass
                # #905: persist the exactly-once key BEFORE submit (per-item) — a crash between submit and a
                # batched save would otherwise re-learn the unit. At-most-once (a rare crash loses one unit)
                # is safer than double-counting for a learning signal.
                submitted.add(traj.query)
                _ace_save_submitted(submitted)
                worker.submit({"scope": scope, "trajectory": traj})   # O(1) non-blocking; reflection runs off the turn path
                n += 1
        return n
    except Exception:   # noqa: BLE001 — advisory: a ledger scan must never break a turn or the dev-loop
        return 0


# ─── M5-2 (#883): the gated MPR-at-fork panel (MPR-A-2 gated invocation + MPR-A-5 pre-informed query) ──
def _ace_forkscan_path() -> "Optional[Path]":
    """The persisted exactly-once record (fork keys already dispatched to MPR), scoped PER-PROJECT (#979)
    under the install home — a project's fork proposals never bleed into another's (the C0-review finding).
    Separate file from the dev-scan units set so neither save clobbers the other. ``None`` if unresolved."""
    try:
        from project_registry import ironclad_home   # bare engine-sibling import
        return ironclad_home() / "ace_forkscan" / f"{_active_mem_ns() or 'base'}.json"
    except Exception:   # noqa: BLE001
        return None


def _ace_load_fork_submitted() -> set:
    try:
        p = _ace_forkscan_path()
        data = json.loads(p.read_text(encoding="utf-8")) if p and p.is_file() else {}
        return set(data.get("forks") or []) if isinstance(data, dict) else set()
    except Exception:   # noqa: BLE001 — a missing/corrupt record reads as empty
        return set()


def _ace_save_fork_submitted(submitted: set) -> None:
    tmp = None
    try:
        p = _ace_forkscan_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"forks": sorted(submitted)[-4096:]}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:   # noqa: BLE001 — advisory: an exactly-once write must never break the dev-loop
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _ace_fork_key(sig: "Any") -> str:
    """A stable exactly-once key for a fork (unit + question) — a re-scan never re-dispatches the same fork."""
    try:
        return f"{getattr(sig, 'unit', '')}|{(getattr(sig, 'question', '') or '')[:120]}"
    except Exception:   # noqa: BLE001
        return ""


def _ace_fork_query(sig: "Any", scope: str) -> str:
    """The MPR query for a fork — the question + candidate options + touched paths, PRE-INFORMED (MPR-A-5) by
    ACE's prior relevant fork-decision bullets via the registered PlaybookStore's query-aware ``context_for``.
    Fail-soft: the pre-informing read is advisory (a fork query never depends on it)."""
    parts: "List[str]" = []
    q = (getattr(sig, "question", "") or "").strip()
    if q:
        parts.append(f"Architecture decision: {q}")
    opts = [o for o in (getattr(sig, "options", None) or []) if o]
    if opts:
        parts.append("Candidate options:\n" + "\n".join(f"- {o}" for o in opts))
    paths = [p for p in (getattr(sig, "touched_paths", None) or []) if p]
    if paths:
        parts.append("Touched paths: " + ", ".join(paths))
    try:
        store = _ACE_STORE
        if store is not None and scope and hasattr(store, "context_for"):
            prior = store.context_for([scope], query=q)
            if prior:
                parts.append("Prior comparable decisions (from the playbook):\n" + prior)
                # M5-4 (#885): remember which prior fork bullets seeded this query (keyed `fork:<unit>`), so
                # the fork-decision Trajectory can set used_bullet_ids → the Reflector rates which prior
                # decisions actually helped (O-001). Reuses the M4-3 durable map; advisory + fail-soft.
                _ace_persist_injected([f"fork:{getattr(sig, 'unit', '')}"], _ace_bullet_ids(prior))
    except Exception:   # noqa: BLE001 — advisory pre-informing read
        pass
    return "\n\n".join(parts)


def _ace_fork_mpr_run(sig: "Any", scope: str) -> str:
    """Run MPR's ``architecture-decision`` panel for a declared fork (OFF the hot path, on the fork worker).
    Fires MPR model-free via ``run_tool('mpr_research', …)`` with ``domain_hint='architecture-decision'`` +
    ``mode_hint='decision'`` so the router selects the existing decision-matrix panel. **Fail-soft** — every
    failure mode (MPR disabled / no orchestrator model / no active initiative per MPR's B3 / RunBudget
    exhausted / MPR raises) degrades to a no-op string; the operator ask still surfaces (M5-3 attaches the
    matrix, M5-4 records the outcome). Producing the matrix is all this leg does."""
    try:
        query = _ace_fork_query(sig, scope)
        if not query.strip():
            return ""
        matrix = run_tool("mpr_research", {"query": query, "domain_hint": "architecture-decision",
                                           "mode_hint": "decision"}) or ""
        # M5-3 (#884): record the produced decision-matrix as a fork proposal bound to the unit, so the
        # dev-process ask surface can render it as a RECOMMENDATION (recommendation-only; the operator decides).
        _ace_record_fork_proposal(getattr(sig, "unit", ""), matrix)
        return matrix
    except Exception:   # noqa: BLE001 — a fork MPR run must never break; the ask still surfaces
        return ""


# ─── M5-3 (#884): the propose surface — attach the MPR matrix to the operator ask as a RECOMMENDATION ──
def _ace_record_fork_proposal(unit: "Any", matrix: str) -> None:
    """Persist the MPR decision-matrix produced for a fork *unit* as a proposal pointer (advisory, fail-soft).
    A no-op MPR result (disabled / declined / ERROR / BLOCKED) is NOT a proposal — the ask surfaces
    unchanged, never an empty artifact (MPR-A-3 fail-soft)."""
    try:
        m = (matrix or "").strip()
        u = str(unit or "").strip()
        if not (u and m) or m.startswith(("ERROR", "MPR declined", "MPR is disabled", "BLOCKED")):
            return
        from playbook_store import record_fork_proposal   # bare engine-sibling import
        from project_registry import ironclad_home
        record_fork_proposal(ironclad_home(), u, m)
    except Exception:   # noqa: BLE001 — advisory: a proposal write must never break the fork worker
        return


_ACE_REC_RE = re.compile(r"^\s*[#*>\-•\s]*(recommendation|recommended|verdict|top[- ]ranked|chosen"
                         r"|winner)\**\s*[:\-–—]\s*(.+)$", re.IGNORECASE)


def _ace_extract_recommendation(matrix: str) -> str:
    """Best-effort top-ranked option from the MPR decision-matrix synthesis (a `Recommendation:`/`Verdict:` …
    line). ``""`` if none — the matrix itself carries the ranking. Never raises."""
    try:
        for line in (matrix or "").splitlines():
            m = _ACE_REC_RE.match(line)
            if m:
                rec = m.group(2).strip().strip("*").strip()   # drop a trailing `**` bold close etc.
                if rec:
                    return rec[:300]
        return ""
    except Exception:   # noqa: BLE001
        return ""


def _ace_fork_proposal_for(unit: "Any") -> str:
    """The RECOMMENDATION-only rendering of the MPR proposal for a fork *unit* (or ``""`` if none) — the
    GENERIC, boundary-clean seam BOTH dev-processes attach to their operator ask (the dev-loop's GitHub
    comment / the raised question). Explicitly framed as a recommendation: the operator decides; ACE learns
    from the choice (M5-4). Fail-soft: no matrix ⇒ ``""`` ⇒ the ask is unchanged."""
    try:
        from playbook_store import read_fork_proposal   # bare engine-sibling import
        from project_registry import ironclad_home
        matrix = read_fork_proposal(ironclad_home(), str(unit or "").strip())
    except Exception:   # noqa: BLE001
        return ""
    if not matrix:
        return ""
    parts = ["### MPR architecture proposal — recommendation only (you decide)"]
    rec = _ace_extract_recommendation(matrix)
    if rec:
        parts.append(f"**MPR's top-ranked option:** {rec}")
    parts.append(matrix)
    parts.append("_This is an MPR-generated multi-perspective decision-matrix — a well-founded recommendation, "
                 "NOT a decision. Review the ranked options + dissent above and choose what fits; ACE learns "
                 "from the choice._")
    return "\n\n".join(parts)


def _fork_command(arg: str) -> str:
    """Read-only M5 architecture-fork proposal surface."""
    parts = (arg or "").split()
    try:
        from playbook_store import list_fork_proposals
        from project_registry import ironclad_home
        home = ironclad_home()
        units = list_fork_proposals(home)
    except Exception as ex:  # noqa: BLE001
        return f"ERROR: could not read fork proposals ({ex!r})."
    if not parts or parts[0].lower() == "list":
        if not units:
            return "No pending MPR fork proposals recorded."
        if len(units) == 1:
            return _ace_fork_proposal_for(units[0]) or f"No MPR fork proposal recorded for #{units[0]}."
        return ("\n".join([f"{len(units)} pending MPR fork proposal(s) — `/fork <unit>` for the full matrix:"]
                          + [f"  - #{u}" for u in units]))
    token = parts[0].lstrip("#").strip()
    return _ace_fork_proposal_for(token) or f"No MPR fork proposal recorded for #{token}."


def _ace_command(arg: str) -> str:
    """ACE ops over a dev-loop ledger (read as plain data — boundary-clean, no private import; off the hot
    path, opt-in, fail-soft). `/ace warmup --ledger <path>` (#915) offline warm-STARTs the active scope's
    playbook from the ledger's historical trajectories. `/ace eval --ledger <path>` (#918) runs the efficiency
    DIAGNOSTIC — compares ACE vs full-rewrite/evolutionary over those trajectories and reports the paper's
    J-001/J-002 verdict (measurement only; no playbook is mutated)."""
    parts = (arg or "").split()
    sub = parts[0] if parts else ""
    if sub in ("snapshot", "versions", "rollback", "unlearn"):   # #1082: playbook safety verbs (active scope)
        return _ace_playbook_command(sub, parts[1:])
    if sub not in ("warmup", "eval"):
        import command_spec as _command_spec   # #953: spec-derived usage (single source)
        return _command_spec.guided_usage("ace")
    ledger = ""
    if "--ledger" in parts:
        i = parts.index("--ledger")
        ledger = parts[i + 1] if i + 1 < len(parts) else ""
    if not ledger:   # #936: default the ledger like /lifecycle (no more required flag to type)
        ledger = str((_project_root() or Path.cwd()) / ".devloop" / "ledger.jsonl")
    store = _ACE_STORE
    if store is None or not hasattr(store, sub if sub == "warmup" else "benchmark"):
        return f"ace {sub}: no ACE playbook store is registered"
    try:
        from ack.ace import ledger_to_trajectories   # lazy: never import ack at gx10 top-level (S6b lesson)
        payloads, chain_errors = _read_ledger_payloads(Path(ledger))
        if chain_errors:
            return f"ace {sub}: BLOCKED — ledger integrity failure ({'; '.join(chain_errors[:3])})"
        trajectories = ledger_to_trajectories(payloads)
        if not trajectories:
            return f"ace {sub}: no trajectories in {ledger} (nothing to do)"
        if sub == "warmup":
            scope = _active_mem_ns()
            if not scope:
                return "ace warmup: no active project scope (open a project first)"
            report = store.warmup(scope, trajectories)
            if report.get("skipped"):
                return "ace warmup: skipped — no orchestrator model reachable (nothing seeded)"
            return _msg("ace.warmup_done", samples=report.get("samples_seen", 0),
                        added=report.get("added", 0), pruned=report.get("pruned", 0))
        # sub == "eval" — the efficiency diagnostic (J-001/J-002); no playbook mutated
        rep = store.benchmark(trajectories)
        if rep.get("skipped"):
            return "ace eval: skipped — no orchestrator model reachable"
        ace, fr, evo = rep["ace"], rep["full_rewrite"], rep["evolutionary"]
        j1_ok = rep.get("no_full_rewrite")
        j2_ok = rep.get("rollout_target_met")
        red = rep.get("rollout_reduction_vs_evolutionary", 0.0)
        # #936: plain-language first; #938: localized via _msg (the paper's J-001/J-002 kept as a parenthetical)
        return _msg("ace.eval_verdict", n=len(trajectories), calls=ace.rollouts,
                    j1clause=_msg("ace.eval_j1_pass" if j1_ok else "ace.eval_j1_fail"),
                    j1="PASS" if j1_ok else "FAIL", reduction=f"{red:.0%}",
                    j2clause=_msg("ace.eval_j2_over" if j2_ok else "ace.eval_j2_under"),
                    j2="PASS" if j2_ok else "FAIL", ace=ace.rollouts, fr=fr.rollouts, evo=evo.rollouts)
    except Exception as ex:   # noqa: BLE001 — the command must never crash the REPL
        return f"ace {sub}: failed ({ex!r})"


def _ace_playbook_command(sub: str, args: "List[str]") -> str:
    """#1082: operator-facing ACE playbook SAFETY verbs over the ACTIVE scope's learned playbook —
    `/ace snapshot` (record a rollback point), `/ace versions`, `/ace rollback [<version>]` (restore the
    previous or a named snapshot), `/ace unlearn <id…>` (selectively forget bullets). Wired to the
    PlaybookStore (M-002 versioning + Q-001 unlearn); each mutating verb snapshots first, so it is itself
    reversible. Fail-soft; never crashes the REPL."""
    store = _ACE_STORE
    if store is None or not hasattr(store, sub):
        return f"ace {sub}: no ACE playbook store is registered"
    scope = _active_mem_ns()
    if not scope:
        return f"ace {sub}: no active project scope (open a project first)"
    try:
        if sub == "snapshot":
            r = store.snapshot(scope)
            return (f"ace snapshot: failed — {r['error']}" if r.get("error")
                    else f"ace snapshot: recorded version {r['version']} ({r['versions']} kept)")
        if sub == "versions":
            vs = store.versions(scope)
            return "ace versions: " + (", ".join(vs) if vs else "(none — run /ace snapshot first)")
        if sub == "rollback":
            r = store.rollback(scope, args[0] if args else None)
            return (f"ace rollback: failed — {r['error']}" if r.get("error")
                    else f"ace rollback: restored to {r['rolled_back_to']} ({r['size']} bullet(s))")
        # sub == "unlearn"
        if not args:
            return "ace unlearn: give one or more bullet ids to forget (e.g. /ace unlearn b12 b7)"
        r = store.unlearn(scope, args)
        return (f"ace unlearn: failed — {r['error']}" if r.get("error")
                else f"ace unlearn: removed {r['removed']}, {len(r.get('missing', []))} not found")
    except Exception as ex:   # noqa: BLE001 — the command must never crash the REPL
        return f"ace {sub}: failed ({ex!r})"


def _ace_mark_fork_done(key: str) -> None:
    """Durably commit a fork's exactly-once key (#904) — called only AFTER its MPR run completes, so a
    dropped/crashed run is retried on the next scan. Advisory + fail-soft."""
    try:
        if not key:
            return
        done = _ace_load_fork_submitted()
        done.add(key)
        _ace_save_fork_submitted(done)
    except Exception:   # noqa: BLE001
        pass


def _ace_fork_run_task(item: "Any") -> None:
    """The `_ACE_FORK_WORKER` task fn: run the MPR panel for one submitted fork, then durably commit its
    exactly-once key (#904 — commit-on-completion, not at dispatch). A worker-level crash leaves the key
    UN-committed (retried next scan). The in-flight guard is always cleared. Never raises.

    """
    key = item.get("key") if isinstance(item, dict) else None
    try:
        if isinstance(item, dict) and item.get("signal") is not None:
            _ace_fork_mpr_run(item.get("signal"), item.get("scope", ""))
            _ace_mark_fork_done(key)          # ran to completion ⇒ commit exactly-once (retry only on crash/drop)
    except Exception:   # noqa: BLE001 — a crash leaves the key un-committed so the next scan retries
        pass
    finally:
        try:
            if key is not None:
                _ACE_FORK_INFLIGHT.discard(key)
        except Exception:   # noqa: BLE001
            pass


def _ace_scan_fork_signals(payloads: "Any" = None, chain_errors: "Any" = None) -> int:
    """Off the hot path (at the dev-loop ledger touchpoint), dispatch each NEWLY-declared ``ForkSignal`` to the
    gated MPR panel on `_ACE_FORK_WORKER`, **exactly-once** (a persisted fork-key set survives restarts). GATE
    OFF (`_ACE_FORK_MPR`) ⇒ 0 (byte-identical to today's STOP-and-ask); a chain-tampered ledger ⇒ skipped
    fail-closed. Advisory; never raises. Returns the number of forks dispatched."""
    try:
        if not _ACE_FORK_MPR or _ACE_FORK_WORKER is None:
            return 0                                  # gate off / not wired ⇒ untouched STOP-and-ask
        if chain_errors:
            return 0                                  # never dispatch from a corrupt ledger
        from ack.ace import fork_signals_from         # lazy: never import ack at gx10 top-level (S6b lesson)
        scope = _active_mem_ns()
        done = _ace_load_fork_submitted()             # durably-committed forks (persisted only after MPR runs)
        n = 0
        for sig in fork_signals_from(payloads):
            key = _ace_fork_key(sig)
            if not key or key in done or key in _ACE_FORK_INFLIGHT:
                continue                              # already committed, or in flight this process
            if _ACE_FORK_WORKER.submit({"scope": scope, "signal": sig, "key": key}):   # O(1); MPR runs off-path
                _ACE_FORK_INFLIGHT.add(key)           # #904: committed to the durable set only AFTER it runs
                n += 1
            # submit() False (queue full) ⇒ not marked ⇒ retried next scan (no lost proposal)
        return n
    except Exception:   # noqa: BLE001 — advisory: a fork scan must never break the dev-loop / a turn
        return 0


# ─── M5-4 (#885): learn the fork decision + outcome so the NEXT comparable fork is pre-informed (MPR-A-4) ──
def _ace_forklearn_path() -> "Optional[Path]":
    """The persisted exactly-once record (fork-decision keys already learned) — a SEPARATE file from the
    MPR-dispatch set so neither save clobbers the other. ``None`` if the home can't resolve."""
    try:
        from project_registry import ironclad_home   # bare engine-sibling import
        return ironclad_home() / "ace_forklearn.json"
    except Exception:   # noqa: BLE001
        return None


def _ace_load_fork_learned() -> set:
    try:
        p = _ace_forklearn_path()
        data = json.loads(p.read_text(encoding="utf-8")) if p and p.is_file() else {}
        return set(data.get("learned") or []) if isinstance(data, dict) else set()
    except Exception:   # noqa: BLE001
        return set()


def _ace_save_fork_learned(learned: set) -> None:
    tmp = None
    try:
        p = _ace_forklearn_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.parent / f"{p.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"learned": sorted(learned)[-4096:]}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:   # noqa: BLE001
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _ace_fork_res_key(res: "Any") -> str:
    """A stable exactly-once key for a learned fork DECISION (unit + chosen option) — distinct from M4-2's
    unit-arc trajectory + #863's per-handover completion, so a fork is never double-counted."""
    try:
        return f"{getattr(res, 'unit', '')}|{(getattr(res, 'chosen_option', '') or '')[:120]}"
    except Exception:   # noqa: BLE001
        return ""


def _ace_fork_trajectory(res: "Any", question: str, used_ids: "List[str]") -> "Any":
    """Build the fork-decision ``ack.ace.Trajectory``: query = the fork's question/area (the stable
    comparability key M5-2's ``context_for`` retrieves by); steps = the chosen option (+ the panel verdict if
    known); outcome = chose <option> [→ <outcome>]; used_bullet_ids = the prior fork bullets that seeded the
    MPR query (so the Reflector rates which prior decisions helped). ``None`` on an empty resolution."""
    try:
        from ack.ace import Trajectory   # lazy: never import ack at gx10 top-level (S6b lesson)
        chosen = (getattr(res, "chosen_option", "") or "").strip()
        q = (question or getattr(res, "area", "") or "").strip()
        if not (chosen and q):
            return None
        outcome_note = (getattr(res, "outcome", "") or "").strip()
        outcome = f"chose '{chosen}'" + (f" -> {outcome_note}" if outcome_note else "")
        steps = [f"decision: {chosen}"]
        area = (getattr(res, "area", "") or "").strip()
        if area:
            steps.append(f"area: {area}")
        return Trajectory(query=f"architecture fork: {q}", steps=steps, outcome=outcome,
                          used_bullet_ids=list(used_ids or []))
    except Exception:   # noqa: BLE001 — a malformed resolution never breaks the scan
        return None


def _ace_scan_fork_resolutions(payloads: "Any" = None, chain_errors: "Any" = None) -> int:
    """Off the hot path, turn each NEWLY-resolved fork (a ``ForkResolution`` on the ledger) into a
    fork-decision Trajectory submitted to the EXISTING `_ACE_WORKER` (reflect→curate→refine writes a bullet in
    ``strategies_and_hard_rules``), so M5-2's ``context_for`` retrieves it at the next comparable fork —
    closing the propose→decide→record→pre-inform loop. GATE OFF (`_ACE_FORK_MPR`) ⇒ 0 (byte-identical); a
    chain-tampered ledger ⇒ skipped fail-closed; **exactly-once** (a persisted decision-key set). Never raises."""
    try:
        if not _ACE_FORK_MPR or _ACE_WORKER is None:
            return 0                                  # gate off / ACE not wired ⇒ no fork learning
        if chain_errors:
            return 0
        from ack.ace import fork_resolutions_from, fork_signals_from   # lazy (S6b lesson)
        from playbook_store import read_unit_bullets                    # the seeding bullets (M5-4 capture)
        from project_registry import ironclad_home
        home = ironclad_home()
        scope = _active_mem_ns()
        q_by_unit = {s.unit: s.question for s in fork_signals_from(payloads) if getattr(s, "unit", "")}
        submitted = _ace_load_fork_learned()
        n = 0
        for res in fork_resolutions_from(payloads):
            key = _ace_fork_res_key(res)
            if not key or key in submitted:
                continue
            used = read_unit_bullets(home, f"fork:{getattr(res, 'unit', '')}")   # prior bullets that seeded the query
            traj = _ace_fork_trajectory(res, q_by_unit.get(getattr(res, "unit", ""), ""), used)
            if traj is not None:
                _ACE_WORKER.submit({"scope": scope, "trajectory": traj})   # O(1); reflect runs off the turn path
                submitted.add(key)
                n += 1
        if n:
            _ace_save_fork_learned(submitted)
        return n
    except Exception:   # noqa: BLE001 — advisory: fork learning must never break the dev-loop / a turn
        return 0


def _apply_ace(cfg: Dict[str, Any], *, strict: bool = False) -> None:
    """Register the always-on ACE PlaybookStore provider + the `post_feedback` ACE consumer + the background
    ReflectionWorker, SUPERSEDING the #602 string-lesson + Process-SC consumers. Runs on every config
    application (boot + ``/config set`` + ``/switch``). NO enable flag — ACE is the core mechanic. Keeps the
    extension-friendly *richer-wins* rule: a FOREIGN provider (a plugin's own backend) is never clobbered, and
    while one is active ACE steps back (the extension owns the loop). One-time best-effort migration of the
    legacy #602 lesson tree on first registration. Lazy-imports ``ack``/the engine siblings (S6b). Fail-soft —
    a wiring hiccup never breaks config application."""
    global _ACE_STORE, _ACE_WORKER, _ACE_MIGRATED, _ACE_FORK_MPR, _ACE_FORK_WORKER
    try:
        from ack import lessons as _lessons          # lazy: never import ack at gx10 top-level (S6b lesson)
        from ack import hooks as _hooks
        from ack.ace import AdaptConfig, ReflectionWorker
        from playbook_store import PlaybookStore, migrate_lessons   # bare engine-sibling import
        from lesson_store import EngineLessonStore
        from project_registry import ironclad_home
    except Exception:   # noqa: BLE001 — boot is fail-soft; a runtime transaction must roll back
        if strict:
            raise
        return
    try:
        ace = (cfg.get("ace") or {})
        def _int(key, default):
            try:
                return max(1, int(ace.get(key, default)))
            except Exception:   # noqa: BLE001 — a malformed tuning value falls back to the default
                return default
        max_bullets, rounds, cost = _int("max_bullets", 200), _int("rounds", 1), _int("cost", 1)
        top_k = _int("top_k", 8)                      # #905: the context_for injection cap — now actually threaded
        # M5-2 (#883): the gated MPR-at-fork OPTION — `ace.fork_mpr.enabled` (default OFF). Gate OFF ⇒ the
        # fork scan is a no-op ⇒ byte-identical to today's STOP-and-ask.
        _ACE_FORK_MPR = (ace.get("fork_mpr") or {}).get("enabled", False)
        current = _lessons.get_provider()
        # Supersede the built-in (None / the #602 EngineLessonStore / OUR OWN store on re-apply). #905: never
        # clobber a FOREIGN PlaybookStore — the faithful check is identity (`current is _ACE_STORE`), not type.
        if current is None or isinstance(current, EngineLessonStore) or current is _ACE_STORE:
            store = _ACE_STORE if isinstance(_ACE_STORE, PlaybookStore) else PlaybookStore(
                ironclad_home() / "ace_playbooks", max_bullets=max_bullets,
                config=AdaptConfig(rounds=rounds, max_bullets=max_bullets, cost=cost))
            store.set_transports(chat=_ace_chat_adapter(), embed=_ace_embed_adapter())
            store.configure(max_bullets=max_bullets, top_k=top_k)
            if current is not store:
                _lessons.set_provider(store)
            _ACE_STORE = store
            if not _ACE_MIGRATED:                    # one-time: replay the legacy #602 lesson tree into the playbook
                _ACE_MIGRATED = True
                base = ironclad_home() / "ace_playbooks"
                marker = base / ".migrated"
                try:
                    if not marker.exists() and migrate_lessons(ironclad_home() / "lessons", store) > 0:
                        base.mkdir(parents=True, exist_ok=True)   # only persist the guard once something migrated
                        marker.write_text("1", encoding="utf-8")
                except Exception:   # noqa: BLE001 — migration is best-effort; a hiccup must not block wiring
                    pass
        # Wire the consumer iff OUR store owns the provider.
        if _lessons.get_provider() is _ACE_STORE and _ACE_STORE is not None:
            _hooks.register_hook("post_feedback", _ace_consumer_hook)
            if _ACE_WORKER is None:
                _ACE_WORKER = ReflectionWorker(_ace_run_task)
                _ACE_WORKER.start()
            # M5-2: the fork MPR worker exists ONLY while the gate is ON (byte-identical when OFF). Reuses
            # ReflectionWorker as a generic off-hot-path queue worker so a (multi-LLM-call) MPR panel never
            # blocks the dev-loop / turn path. #904: tear it down on a gate ON->OFF flip (no stranded daemon).
            if _ACE_FORK_MPR and _ACE_FORK_WORKER is None:
                _ACE_FORK_WORKER = ReflectionWorker(_ace_fork_run_task)
                _ACE_FORK_WORKER.start()
            elif not _ACE_FORK_MPR and _ACE_FORK_WORKER is not None:
                try:
                    _ACE_FORK_WORKER.stop()
                except Exception:   # noqa: BLE001
                    pass
                _ACE_FORK_WORKER = None
                _ACE_FORK_INFLIGHT.clear()
        else:                                        # a foreign provider won ⇒ ACE steps back
            _hooks.unregister_hook("post_feedback", _ace_consumer_hook)
    except Exception:   # noqa: BLE001 — boot is fail-soft; a runtime transaction must roll back
        if strict:
            raise
        return


def _loop_profile(task_type=None):
    """Resolve the loop budget for *task_type* (#602 SUB-8a) — the ``loop_profiles`` config deep-merged over
    the engine globals (``MAX_ITERATIONS`` + the re-ask budget). With nothing configured (the default) the
    profile is BYTE-IDENTICAL to today's limits; ``task_type=None`` (the chat loop) selects the default
    profile. Fail-soft: any resolver/import hiccup falls back to the globals. Never raises."""
    try:
        from ack.loop_profile import resolve_loop_profile      # lazy: never import ack at gx10 top-level
        from ack.validated_emit import DEFAULT_RETRY_BUDGET, MAX_RETRY_BUDGET
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        return resolve_loop_profile(
            (cfg or {}).get("loop_profiles"), task_type,
            default_max_iterations=MAX_ITERATIONS,
            default_retry_budget=DEFAULT_RETRY_BUDGET,
            max_retry_budget=MAX_RETRY_BUDGET,
        )
    except Exception:   # noqa: BLE001 — never let profile resolution break the loop; fall back to the globals
        from types import SimpleNamespace   # stdlib (no ack import in the fallback) → duck-typed .max_iterations
        return SimpleNamespace(max_iterations=MAX_ITERATIONS, retry_budget=3, effort="medium", eval_verifiers=())


def _derive_quality_breaker_state(cfg: Dict[str, Any]):
    """Build an unpublished quality-breaker replacement while retaining compatible live history."""
    from ack.quality import QualityBreaker   # lazy: never import ack at gx10 top-level (S6b lesson)
    q = (_cfg_get(cfg, "quality") or {}) if isinstance(_cfg_get(cfg, "quality"), dict) else {}
    candidate = QualityBreaker(
        threshold=q.get("threshold", 0.5),
        min_consecutive=q.get("min_consecutive", 3),
        window=q.get("window", 20),
    )
    with _QUALITY_LOCK:
        current = _QUALITY_BREAKER
        if current is not None:
            old = current.snapshot()
            new = candidate.snapshot()
            old_window = getattr(getattr(current, "_scores", None), "maxlen", None)
            new_window = getattr(getattr(candidate, "_scores", None), "maxlen", None)
            if (old.threshold, old.min_consecutive, old_window) == (
                    new.threshold, new.min_consecutive, new_window):
                return current, _QUALITY_TRIPPED
            rebuilt = current.reconfigure(
                threshold=q.get("threshold", 0.5),
                min_consecutive=q.get("min_consecutive", 3),
                window=q.get("window", 20),
            )
        else:
            rebuilt = candidate
        snapshot = rebuilt.snapshot()
        return rebuilt, snapshot if snapshot.tripped else None


def _apply_quality_breaker(cfg: Dict[str, Any]) -> None:
    """Atomically apply live quality tuning while retaining history and the breaker's live latch state."""
    global _QUALITY_BREAKER, _QUALITY_TRIPPED
    _QUALITY_BREAKER, _QUALITY_TRIPPED = _derive_quality_breaker_state(cfg)


def _quality_breaker():
    """The always-on process-global output-quality breaker (#602 SUB-9)."""
    return _QUALITY_BREAKER


# ─── epic #602 SUB-4 / 2.1: mark-only Verifier on the dev-task pipeline (pre_handover) ─────────────
_LAST_VERDICT = None
_VERIFY_GROUNDING_THRESHOLD = 0.5   # captured at config-apply time (#809); the verifier reads this, not _EFFECTIVE_CFG


def _set_last_verdict(v) -> None:
    global _LAST_VERDICT
    _LAST_VERDICT = v


def _last_verdict():
    """The most recent handover :class:`~ack.verify.VerdictResult`, consumed once by Quality."""
    return _LAST_VERDICT


def _apply_verifier(cfg: Dict[str, Any]) -> None:
    """Capture bounded advisory-grounding tuning for the always-on synchronous verifier."""
    global _VERIFY_GROUNDING_THRESHOLD
    try:
        th = _cfg_get(cfg, "verify.grounding_threshold")
        _VERIFY_GROUNDING_THRESHOLD = float(th) if isinstance(th, (int, float)) and not isinstance(th, bool) else 0.5
    except Exception:
        _VERIFY_GROUNDING_THRESHOLD = 0.5


# ─── epic #602 SUB-9 / 2.7: Quality breaker CONSUMER — feed Verifier scores, surface a trip ────────
_QUALITY_TRIPPED = None


def _quality_tripped():
    """The latest latched tripped :class:`~ack.quality.QualitySnapshot`, or ``None`` when untripped."""
    return _QUALITY_TRIPPED


def _quality_consumer_hook(ctx) -> None:
    """Feed the latest Verifier score into Quality, surface a trip, and reset a latch on a passing score.

    No-op when no verdict is present; **fail-soft** (never raises).
    """
    global _QUALITY_TRIPPED
    try:
        with _QUALITY_LOCK:
            qb = _quality_breaker()
            v = _last_verdict()
            if qb is None or v is None:
                return
            was_tripped = _QUALITY_TRIPPED is not None
            tripped = qb.record(v.score)
            _set_last_verdict(None)   # feed-once: consume the verdict so a later handover can't re-feed it stale
            score = float(v.score)
            recovered = math.isfinite(score) and score >= qb.snapshot().threshold
            if recovered:
                qb.reset()
                _QUALITY_TRIPPED = None
            elif tripped:
                _QUALITY_TRIPPED = qb.snapshot()
                if not was_tripped:   # surface only on the not-tripped → tripped transition (no re-print while latched)
                    _ui_print(col(f"  [quality] output-quality breaker tripped — {_QUALITY_TRIPPED.reason}", C.YELLOW))
            else:
                _QUALITY_TRIPPED = None
    except Exception:   # noqa: BLE001 — mark-only + fail-soft: a quality hiccup never breaks a handover
        return


def _apply_quality_consumer(cfg: Dict[str, Any], *, strict: bool = False) -> None:
    """Register the always-on ``post_handover`` quality consumer (#602 2.7)."""
    try:
        from ack import hooks as _hooks   # lazy: never import ack at gx10 top-level (S6b lesson)
    except Exception:   # noqa: BLE001 — boot is fail-soft; a runtime transaction must roll back
        if strict:
            raise
        return
    try:
        _hooks.register_hook("post_handover", _quality_consumer_hook)
    except Exception:   # noqa: BLE001 — boot is fail-soft; a runtime transaction must roll back
        if strict:
            raise
        return


# ─── epic #602 SUB-3 / 2.4: FailureClass at the code-agent failover (the Strategy consumer's input) ───
_LAST_FAILURE_CLASS = None
_STRATEGY_BUDGET = 3
_FAILURE_ATTEMPTS: "Dict[str, int]" = {}   # per-task code-agent failure counter (#602 2.5); reset on success
_LAST_STRATEGY = None


def _apply_strategy(cfg: Dict[str, Any]) -> None:
    """Capture bounded ``strategy.budget`` tuning for the always-on finite failure strategy."""
    global _STRATEGY_BUDGET
    try:
        b = _cfg_get(cfg, "strategy.budget")
        if (isinstance(b, bool) or not isinstance(b, (int, float))
                or not math.isfinite(float(b)) or float(b) <= 0 or not float(b).is_integer()):
            raise ValueError("strategy.budget must be a positive finite integer")
        from ack.validated_emit import MAX_RETRY_BUDGET
        _STRATEGY_BUDGET = min(int(b), MAX_RETRY_BUDGET)
    except Exception:   # noqa: BLE001 — invalid tuning cannot disable or unbound the strategy
        _STRATEGY_BUDGET = 3


def _last_strategy():
    """The most recent :class:`~ack.strategy.Strategy` from the failover consumer (#602 2.5), or ``None``.
    Observability only; the feedback handler gates directly on the fresh action returned by
    :func:`_revise_on_failure`."""
    return _LAST_STRATEGY


def _strategy_escalated(action: Any) -> bool:
    """Return whether a strategy action is the terminal retry-budget signal."""
    return action == "human_escalation"


def _mark_strategy_escalated(task_id: str, result_cls: Any) -> str:
    """Persist and return the canonical terminal retry-budget reason for *task_id*."""
    attempts = _FAILURE_ATTEMPTS.get(task_id, 0)
    reason = f"retry budget spent after {attempts} attempts ({result_cls})"
    _store().mark_blocked(task_id, kind="escalated", reason=reason)
    return reason


def _failover_budget(task_id) -> int:
    """The **per-TaskType** code-agent failover attempt budget (#602 2.6 / #807): the active task's
    `loop_profiles.by_type[<type>].retry_budget` layered over `strategy.budget` (the default) and clamped to
    the hard re-ask ceiling (`MAX_RETRY_BUDGET`) — so a per-type override can only LOWER the budget (e.g. a
    `chat`/`bug` type escalates sooner). The task's type is read from the store; any hiccup / unknown type /
    empty `by_type` → the default `_STRATEGY_BUDGET` (byte-identical to the flat #806 budget). Reads
    `loop_profiles` from `_EFFECTIVE_CFG` (the `_loop_profile` accessor pattern). Never raises."""
    try:
        task_type = (_store().get(task_id) or {}).get("type")
    except Exception:   # noqa: BLE001 — store hiccup → no type → the default budget
        task_type = None
    try:
        from ack.loop_profile import resolve_loop_profile        # lazy: never import ack at gx10 top-level
        from ack.validated_emit import MAX_RETRY_BUDGET
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        prof = resolve_loop_profile(
            (cfg or {}).get("loop_profiles"), task_type,
            default_max_iterations=MAX_ITERATIONS,
            default_retry_budget=_STRATEGY_BUDGET,
            max_retry_budget=MAX_RETRY_BUDGET,
        )
        return int(prof.retry_budget)
    except Exception:   # noqa: BLE001 — resolver hiccup → the flat default
        return _STRATEGY_BUDGET


def _revise_on_failure(task_id, result_cls):
    """Apply the always-on finite strategy and durably block a task when its retry budget is spent.

    Uses the fresh ``result_cls`` (never a stale ``_last_failure_class``); success resets the per-task
    counter. The returned action is the feedback handler's terminal signal. Strategy calculation remains
    fail-soft, while a spent-budget persistence failure still returns the escalation signal so automatic
    failover stops at the protected boundary.
    """
    global _LAST_STRATEGY
    try:
        if not task_id:
            return None
        from providers import code_agent_strategy, RESULT_OK   # bare engine-sibling import
        if result_cls == RESULT_OK:
            _FAILURE_ATTEMPTS.pop(task_id, None)               # success → reset (also avoids a stale class)
            return None
        if len(_FAILURE_ATTEMPTS) > 4096:                      # runaway guard for the per-task counter map
            _FAILURE_ATTEMPTS.clear()
        n = _FAILURE_ATTEMPTS.get(task_id, 0) + 1
        _FAILURE_ATTEMPTS[task_id] = n
        strat = code_agent_strategy(result_cls, attempt=n, budget=_failover_budget(task_id))
        if strat is None:
            return None
        _LAST_STRATEGY = strat
    except Exception:   # noqa: BLE001 — strategy calculation is fail-soft
        return None
    action = strat.action.value
    if getattr(strat, "escalate", False):
        try:
            _mark_strategy_escalated(task_id, result_cls)
        except Exception:   # noqa: BLE001 — caller retries persistence and still stops automatic failover
            pass
        try:
            _ui_print(col(f"  [strategy] human escalation after {n} attempt(s) on {task_id} "
                          f"({result_cls}) → {action}", C.YELLOW))
        except Exception:   # noqa: BLE001 — observability must not mask the terminal signal
            pass
        try:
            _emit_hook("escalation", {"task_id": task_id, "attempts": n,  # #1083: off-duty human
                                      "result_cls": result_cls, "action": action})
        except Exception:   # noqa: BLE001 — notifier failures do not reopen automatic retries
            pass
    return action


def _last_failure_class():
    """The shared :class:`~ack.failure_class.FailureClass` of the most recent code-agent run failure (#602
    2.4), or ``None``. Read by the Strategy Revisor consumer (#602 2.5 / #806) — advisory only."""
    return _LAST_FAILURE_CLASS


def _record_failure_class(result_cls):
    """On a code-agent run result, record + return the shared FailureClass (#602 2.4 / #805) so the Strategy
    consumer (2.5) can act on WHY a run failed. Classification is always on. Returns the FailureClass string
    value, or ``None`` for ``RESULT_OK`` / an unknown result. **Fail-soft** — classifying a failure must never
    break the feedback path."""
    global _LAST_FAILURE_CLASS
    try:
        from providers import result_failure_class   # bare engine-sibling import (like project_registry)
        fc = result_failure_class(result_cls)
        if fc is None:
            return None
        _LAST_FAILURE_CLASS = fc
        return fc.value
    except Exception:   # noqa: BLE001 — advisory: a classification hiccup must never break the feedback path
        return None


# ─── epic #602 SUB-6: optional ACE-backed pre-turn process hints ───────────────────────────────────
def _concrete_lesson_provider():
    """The registered lesson provider IFF it exposes the typed ``by_category`` surface; ``None`` otherwise.

    Process hints read typed entries through this surface; the string-only ``ack.lessons`` seam cannot provide
    them. DUCK-TYPED (#863): since ACE supersedes the #602
    ``EngineLessonStore`` with the :class:`~engine.playbook_store.PlaybookStore` (which implements the SAME
    read surface over the bullet playbook), the check is a capability probe, not an ``isinstance``. Never raises."""
    try:
        from ack import lessons as _lessons     # lazy: never import ack at gx10 top-level (S6b lesson)
        p = _lessons.get_provider()
        return p if callable(getattr(p, "by_category", None)) else None
    except Exception:   # noqa: BLE001 — advisory: a lookup hiccup → no concrete provider
        return None


def _process_hint() -> str:
    """An ACE-backed pre-turn hint of known working approaches for the active scope, or ``""`` when disabled,
    no typed provider is registered, or none are recorded. Fail-soft."""
    try:
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        if not _as_bool(_cfg_get(cfg, "process.hints_enabled")):
            return ""
        provider = _concrete_lesson_provider()
        if provider is None:
            return ""
        scope = _active_mem_ns()
        if not scope:    # no project bound (base partition) → no hint, byte-identical
            return ""
        from ack.process import format_process_hint
        from lesson_store import LessonCategory
        try:
            limit = int(_cfg_get(cfg, "process.max_hints"))
        except Exception:   # noqa: BLE001 — incl. OverflowError(int(inf)) → fall back to the default, not ""
            limit = 3
        texts = provider.by_category(scope, LessonCategory.BEST_KNOWN_PATH, limit=limit)
        return format_process_hint(texts, limit=limit)
    except Exception:   # noqa: BLE001 — advisory hint: never break a turn
        return ""


# ─── Main ─────────────────────────────────────────────────────
# Engine library — the standalone monolith CLI was removed (one way: server + client).
_REMOVED_MSG = (
    "The monolithic gx10 CLI has been removed - gx10 is now the engine library. "
    "Run the server (engine/server.py) and connect with the client (engine/client.py "
    "or engine/tui.py). See SETUP.md."
)


def main() -> None:
    """Removed: gx10 is the engine library; use the server + client instead."""
    print(_REMOVED_MSG, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
