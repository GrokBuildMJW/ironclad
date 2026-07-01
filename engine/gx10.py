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
import re
import sys
import json
import inspect
import time
import shutil
import subprocess
import threading
import queue as _q
from contextlib import contextmanager
import argparse
import math
import copy
import urllib.request
import urllib.error
import urllib.parse
from collections import deque
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable

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
# _register_devprocess_driver() below, AFTER core/ is placed on sys.path — so the real launch (only
# core/engine on the path at import time) still resolves it. Until set, the tools call the impls directly.
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

# core/ on sys.path so the ACK package (core/ack) is importable when the
# engine runs as a script — SCRIPT_DIR is core/engine, its parent is core/.
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
    stamp) → "unknown". Pure read — NO git/SHA logic in core (the deploy stamps it); generic + secret-free."""
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
MAX_FILE_CHARS   = 24_000        # PERF-05: read_file cap (head+tail)
LIST_DIR_HARD_CAP = 200          # HV-B: hard cap in list_directory
TEMPERATURE      = 0.3
RETRY_BACKOFF    = 1.5           # OPT-4: wait time (s) before 1× retry on an API error
# Engine machinery lives hidden under STATE_ROOT (initiative-independent): session.json, the
# local warm cache (memory/), config.json/active (ITYPE). Relative to WORKDIR (after chdir = CWD),
# overridable via cfg["paths"]["state_root"] (default ".ironclad") — absolute too. Boundary
# clean (no private literal). Helpers: state_root() / session_path().
STATE_ROOT       = ".ironclad"
SESSION_FILE     = "session.json"   # basename, resolved under STATE_ROOT (was ".gx10_session.json" at the root)
# Visible knowledge root (Obsidian-navigable): vault/<slug>/ per initiative. Engine machinery
# is STATE_ROOT, KNOWLEDGE is VAULT_ROOT — strictly separated. Overridable via cfg["paths"]["vault_root"].
VAULT_ROOT       = "vault"
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
# ACK contract (ack.case_spec). Soft path: on a violation the exact error is
# returned → the agent loop hands it back to the model as a tool result (reask),
# nothing is created. LODESTAR_ENABLED → CapabilityTaskSpec (capability mandatory for
# buildable types). Both config-driven (ack.enabled / lodestar.enabled).
ACK_ENABLED      = True
LODESTAR_ENABLED = False

# Onboarding mode: proactive duplicate pre-check BEFORE the (expensive) handover.
# Default off (store dedup guarantees correctness anyway). Helpful when
# migrating from another CLI / with many legacy tasks. When active, the
# `check_task_exists` tool is offered and the prompt instructs to pre-check.
ONBOARDING_MODE = False

# Autopilot (Path B): for pending tasks with a handover the reconciler
# automatically starts `claude --print` (API-free execution) and moves pending →
# in_progress. Default OFF (starts Claude autonomously with skip-permissions).
AUTOPILOT_ENABLED        = False
AUTOPILOT_CLAUDE_BIN     = "claude"
AUTOPILOT_EXTRA_ARGS     = ["--dangerously-skip-permissions"]
AUTOPILOT_DEFAULT_EFFORT = "medium"
AUTOPILOT_LOGS_DIR       = "logs"     # resolved under state_root() (.ironclad/logs); absolute path verbatim
AUTOPILOT_MAX_CONCURRENT = 1            # 1 = sequential; >1 parallel; 0 = unlimited
AUTOPILOT_STREAM         = False        # live log streaming (claude --verbose --output-format stream-json); default OFF
AUTOPILOT_TERMINATE_ON_ADVANCE = False  # terminate the associated claude session on advance; default OFF
AUTOPILOT_AUTOPLAN       = False   # after an empty queue, have GX10 automatically plan the next task; default OFF
AUTOPILOT_MAX_TASKS      = 0       # max tasks autoplan plans (0 = unlimited — use LOCAL vLLM ONLY!)
_AUTOPLAN_DONE           = 0       # session counter (touched only in the agent_thread → no lock needed)
# ROUTE-2 (#503): removed the dead `_TURN_DID_ADVANCE` guard — it was only reset, never set or read, so it
# never blocked anything (its comment claimed otherwise). The actual auto-plan control is AUTOPILOT_AUTOPLAN.
AUTOPILOT_LOG_TERMINAL   = False        # on every autopilot start open a new terminal with Get-Content -Wait; default OFF
# Kimi was replaced by Sonnet on 2026-06-15. "KIMI" remains only as a
# legacy alias and is transparently normalized to SONNET everywhere
# (Claude Code CLI + claude-sonnet-4-6). No Kimi CLI plumbing anymore.
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
INITIATIVE_TYPES = ("mpr", "software")

# Hidden machine plumbing per initiative (hybrid layout, 06-20): active.md + handover/
# feedback inbox + history live under <initiative>/.work/ (out of sight); the visible
# artefacts (decisions/ proposals/ reviews/ runs/ tasks/) stay navigable on top.
WORKFLOW_DIR = ".work"

# Skeleton directories per type (relative to vault/<slug>/). software = task pipeline +
# file-communication plumbing; mpr = reasoning runs + decision reports.
_INITIATIVE_SKELETON: Dict[str, List[str]] = {
    "software": ["tasks/pending", "tasks/in_progress", "tasks/done",
                 "decisions", "proposals", "reviews",
                 f"{WORKFLOW_DIR}/handovers", f"{WORKFLOW_DIR}/feedback",
                 f"{WORKFLOW_DIR}/archive/handovers", f"{WORKFLOW_DIR}/archive/feedback"],
    "mpr":      ["runs", "decisions"],
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
        return cls(
            slug=slug,
            type=fm.get("type", ""),
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


def initiative_new(name: str, type: str) -> Initiative:
    """Creates a new initiative (meta.md + type skeleton), sets it active. Colliding slugs
    get a -N suffix. Unknown type / empty name → ValueError."""
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


def _mpr_blocks_tasks() -> Optional[str]:
    """Issue #15 — the task pipeline (tasks/handovers/feedback) is software-only. If the ACTIVE
    initiative is type mpr (reasoning-only), return a clear refusal message; otherwise None. The type
    becomes a real contract instead of only choosing the seed skeleton."""
    v = initiative_active()
    if v is not None and v.type == "mpr":
        return _msg("mpr.blocks_tasks", slug=v.slug)
    return None


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
_INDEX_AUTO_START = "<!-- ironclad:index:auto START — generiert von reconcile_vault, nicht von Hand ändern -->"
_INDEX_AUTO_END   = "<!-- ironclad:index:auto END -->"
_LINKS_AUTO_START = "<!-- ironclad:related:auto START -->"
_LINKS_AUTO_END   = "<!-- ironclad:related:auto END -->"
# S12c: a machine-readable typed-edge graph (GRAPH.json) + a human LIFECYCLE.md view, both generated
# next to INDEX.md. The HTML markers above stay FROZEN; LIFECYCLE.md adds its own managed-block markers.
_LIFECYCLE_AUTO_START = "<!-- ironclad:lifecycle:auto START -->"
_LIFECYCLE_AUTO_END   = "<!-- ironclad:lifecycle:auto END -->"
GRAPH_FILENAME     = "GRAPH.json"
LIFECYCLE_FILENAME = "LIFECYCLE.md"
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
        if p.name in ("INDEX.md", LIFECYCLE_FILENAME) or (rel.parts and rel.parts[0] == WORKFLOW_DIR):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
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


def _exec_cwd() -> "Optional[str]":
    """The filesystem working directory for MODEL-DRIVEN execution — the code-tools (read/write/list/…),
    ``execute_command``, and the launched code-agent — under the active ProjectContext (ADR-0011 AD-1 / S9c).
    Returns the active project's ``root`` ONLY when a genuinely non-default project is bound; otherwise
    ``None`` so the caller keeps the process workdir (``_BOOT_WORKDIR``, set by the boot ``os.chdir``) —
    BYTE-IDENTICAL to the pre-isolation engine. A ``/switch`` does NOT chdir the process (a global chdir under
    the daemons/fan-out threads is unsafe), so this is the seam that points a switched project's file ops at
    its own tree.

    NB: when a local-tool bridge is active the code-tools run on the CLIENT's tree (``run_tool`` returns
    early), so this governs only SERVER-side execution — exactly where the project root must be honoured."""
    pc = _pc.current() if _pc is not None else None
    if pc is None or not pc.root:
        return None
    if _BOOT_WORKDIR is not None and Path(pc.root) == _BOOT_WORKDIR:
        return None                              # the default project == the boot workdir → today's behaviour
    return pc.root


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

def clean(text: str) -> str:
    return THINK_RE.sub("", text).strip() if text else ""

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

    def __init__(self):
        self.in_think = False
        self.buf      = ""

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
    """Takes text line by line. Markdown/pipe tables are buffered
    and emitted with exactly aligned columns; everything else passes
    through unchanged (line by line). The `|---|` separator row and `**`/`` ` ``
    are removed. Costs NO extra tokens — the alignment
    happens locally during rendering."""

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

    @staticmethod
    def _cells(line: str):
        s = line.strip().strip("|")
        return [c.strip().replace("**", "").replace("`", "") for c in s.split("|")]

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
        self.emit_line(line.replace("**", ""))   # remove literal markdown bold markers

    def _flush_table(self):
        if not self.table:
            return
        rows = [self._cells(r) for r in self.table]
        ncol = max(len(r) for r in rows)
        widths = [0] * ncol
        for r in rows:
            for i, c in enumerate(r):
                if len(c) > widths[i]:
                    widths[i] = len(c)
        for r in rows:
            cells = [(r[i] if i < len(r) else "").ljust(widths[i]) for i in range(ncol)]
            self.emit_line("  " + "  ".join(cells).rstrip())
        self.table = []

    def flush(self):
        if self.buf:
            self._line(self.buf)
            self.buf = ""
        self._flush_table()


# ─── Global UI state ────────────────────────────────────────
_UI_MAX_LINES                 = 5000
_UI_LINES:   "deque[str]"     = deque(maxlen=_UI_MAX_LINES)
_UI_PARTIAL: str              = ""
_UI_LOCK                      = threading.Lock()
_UI_APP: Optional[Application] = None

# Headless capture hook: set by the server mode (engine/server.py)
# when NO prompt_toolkit UI is running (_UI_APP is None). A callable(text:str)->None
# that taps the output (e.g. into a thread-local request buffer) instead of printing
# to stdout. Stays None in normal CLI/REPL operation → behaviour unchanged.
_UI_SINK: Optional[Callable[[str], None]] = None

_INPUT_QUEUE: _q.Queue        = _q.Queue()
_CANCEL_EVENT                 = threading.Event()
_RELOAD_FLAG                  = False
_WATCHER_ENABLED              = True    # auto-advance via reconciler (stable now → default on)
RECONCILER_INTERVAL           = 3.0     # polling interval (s)
_ADVANCE_CMD                  = "\x00advance\x00"   # internal structured reconciler command
_LAUNCH_CMD                   = "\x00launch\x00"    # internal autopilot launch command

# Autopilot: counter of reserved/running claude processes (concurrency gate)
_AUTOPILOT_ACTIVE             = 0
_AUTOPILOT_LOCK               = threading.Lock()
_AUTOPILOT_PROCS: Dict[str, Any] = {}   # task_id -> Popen (for targeted termination on advance)

_status = {"thinking": False, "label": "ready"}

# Effectively loaded config + source (set in main()) — for the `config` command.
_EFFECTIVE_CFG: Optional[Dict[str, Any]] = None
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
            "description": "Read the full content of a file. Use for handovers, task JSONs, feedback, CLAUDE.md.",
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
            "name": "write_file",
            "description": (
                "Write content to a file. Creates missing parent directories. "
                "Handover naming: KGC-XXX_OPUS.md, KGC-XXX_SONNET.md. "
                "Feedback: KGC-XXX_OPUS-feedback.md, KGC-XXX_SONNET-feedback.md. "
                "IMPORTANT: If a conflicting ID exists, use move_file to rename first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": (
                "List files and subdirectories. For large folders like "
                "tasks/done ALWAYS pass sort='time' and a small limit to "
                "get only the newest entries — never dump the whole folder."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":  {"type": "string", "default": "."},
                    "sort":  {"type": "string", "enum": ["name", "time"], "default": "name"},
                    "limit": {"type": "integer", "description": "max number of entries (newest first when sort='time')"}
                },
                "required": []
            }
        }
    },
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
    }
]

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
    GATED on the sealed profile + a configured memory service + the agent's mcp_template. ("", {}) when not
    gated (the agent launches byte-identically). Fail-soft."""
    try:
        import memory_mcp
        cfg = _MEMORY_CONFIG or {}
        return memory_mcp.render_mcp_launch(
            getattr(spec, "mcp_template", None),
            sealed=_is_sealed_profile(),
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
# distinct from the per-peer availability breaker above (folding quality in would corrupt failover). Built /
# cleared by ``_apply_quality_breaker`` from the ``quality`` config block; OPT-IN, default off → None → no-op
# byte-identical. A trip is ADVISORY (escalate/surface, never a hard-abort).
_QUALITY_BREAKER = None

# #456 (FORK-D): task_class is derived DETERMINISTICALLY from task_json.type — never from model output.
# Security/architecture get their own class (the OPUS matrix); verification = analysis; everything else
# is coding. The class only SCOPES failover/peer selection to task-appropriate agents (it does NOT
# override the orchestrator's staged pick — operator-confirmed 2026-06-25).
_TASK_CLASS_BY_TYPE = {
    "security": "security", "security-audit": "security",
    "architecture": "architecture",
    "verification": "analysis",
}

def _task_class(task: Dict[str, Any]) -> str:
    t = str((task or {}).get("type") or "").strip().lower()
    return _TASK_CLASS_BY_TYPE.get(t, "coding")

# #500 (FORK-D follow-up, token-balancing): auto-tier the handover reasoning effort by the derived
# task_class — security/architecture get xhigh (the OPUS matrix), routine work (coding/analysis) gets high.
# An UNMAPPED class returns None ⇒ fail-open: the effort chain is left unchanged (a future class cannot
# silently force an effort until it is mapped here).
_EFFORT_BY_CLASS = {"security": "xhigh", "architecture": "xhigh", "coding": "high", "analysis": "high"}

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
    for sn in ("MEMORY_TOOL", "DEEP_MEMORY_TOOL", "PARALLEL_TOOL", "WEBSEARCH_TOOL",
               "USE_SKILL_TOOL", "USE_PROMPT_TOOL"):
        t = g.get(sn)
        n = ((t or {}).get("function") or {}).get("name") if isinstance(t, dict) else None
        if n:
            names.add(n)
    if include_plugins:   # ROUTE-4 (#503): include_plugins=False → the BUILT-IN tool names only (collision check)
        names.update((g.get("_PLUGIN_TOOLS") or {}).keys())
    return frozenset(names)


def _effective_tools() -> List[Dict[str, Any]]:
    """Tool list depending on the mode — onboarding tools only when active."""
    # Offer the tool only when memory is CONFIGURED (not just the module present) —
    # otherwise the tool would be offered even though every call would return "unavailable".
    mem = [MEMORY_TOOL, DEEP_MEMORY_TOOL] if _MEMORY is not None else []
    par = [PARALLEL_TOOL] if _WORKERS is not None else []
    # #459 / epic #505: offer web_search only when a usable search adapter is configured (else every
    # call would return "unavailable"). Adapter-aware (cli / brave / mock) — not dispatcher-only.
    web = [WEBSEARCH_TOOL] if _web_search_available() else []
    plug = [t["schema"] for t in _PLUGIN_TOOLS.values()]
    skl = [USE_SKILL_TOOL] if _PLAYBOOKS else []
    prm = [USE_PROMPT_TOOL] if _PROMPTS else []
    return (_tools_with_agent_enum(TOOLS) + mem + par + web + plug + skl + prm
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
    _mpr = _mpr_blocks_tasks()
    if _mpr:
        return f"ERROR: {_mpr}"               # #15: mpr initiative is reasoning-only

    store = _store()
    log: List[str] = []

    # Idempotency gate: task already done → no re-advance needed
    existing = store.get(task_id)
    if existing and existing.get("status") == "done":
        return (f"OK: task {task_id} is already done — no re-advance needed. "
                f"feedback is in {(archive_feedback_dir() / f'{task_id}_{agent}-feedback.md').as_posix()}")

    # 0. Fail-closed gate: feedback MUST exist
    # Primary: <initiative>/.work/feedback/ (reconciler inbox)
    # Fallback: <initiative>/.work/archive/feedback/ (already archived by the reconciler)
    fb = feedback_dir() / f"{task_id}_{agent}-feedback.md"
    if not fb.exists():
        fb_arch = archive_feedback_dir() / f"{task_id}_{agent}-feedback.md"
        if fb_arch.exists():
            fb = fb_arch
            log.append(f"feedback read from archive: {fb_arch}")
        else:
            return (f"ERROR: feedback missing: {fb.as_posix()} "
                    f"and {fb_arch.as_posix()} "
                    f"— the task is NOT considered complete. Pipeline not advanced.")
    log.append(f"feedback found: {fb}")

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

        # 3a. Memory: store the task completion as an episode (fail-soft)
        if _MEMORY is not None and _MEMORY.is_available():
            try:
                fb_text = vfb.read_text(encoding="utf-8") if vfb.exists() else ""
                _MEMORY.store_task_completion(task_id, existing or {}, fb_text)
            except Exception:
                pass

        # 3b. LessonStore (ADR-0011 AD-10 / S14-4) is RE-HOMED onto the Hook-Bus (#804): the completion
        # feedback is reported as a scoped loop-lesson by the `post_feedback` consumer (`_lessons_consumer_hook`)
        # emitted by the wrapper below — gated on a registered provider (byte-identical no-op when none is
        # wired), outside the vault lock. (No inline call here.)

        # 3c. Process-SC (#602 S602-6) is RE-HOMED onto the Hook-Bus (#803): the typed process-lesson is
        # distilled + stored by the `post_feedback` consumer (`_process_consumer_hook`) emitted by the
        # wrapper below — one consistent reflection path, outside the vault lock. (No inline call here.)

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
    return f"OK: pipeline advanced for {task_id} ({agent})\n" + "\n".join(f"  - {l}" for l in log)


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
def _ack_validate(fields: Dict[str, Any]) -> Optional[str]:
    """ACK soft-path gate: validates a model-emitted task_json against the
    ACK contract. Returns an EXACT error string on a violation, otherwise None
    (valid / gate off / ACK package unavailable → degrades softly, the engine
    keeps running). With Lodestar enabled, the capability-bearing spec is
    used (capability mandatory for buildable types)."""
    if not ACK_ENABLED:
        return None
    try:
        from ack.case_spec import TaskSpec
        spec_cls = TaskSpec
        if LODESTAR_ENABLED:
            from ack.lodestar.spec import CapabilityTaskSpec
            spec_cls = CapabilityTaskSpec
        from pydantic import ValidationError
    except Exception:
        return None  # ACK not importable → degrade softly
    try:
        spec_cls.model_validate(fields)
        return None
    except ValidationError as e:
        return str(e)


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
    _mpr = _mpr_blocks_tasks()
    if _mpr:
        return f"ERROR: {_mpr}"               # #15: mpr initiative is reasoning-only

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
            # ACK soft-path gate: validate the task_json against the contract BEFORE
            # the store mutates anything. On a violation, fail-closed with the exact error
            # → the agent loop hands it back to the model (reask).
            ack_err = _ack_validate(fields)
            if ack_err:
                return ("ERROR: task_json violates the ACK contract (nothing created):\n"
                        + ack_err + "\n→ fix the fields and call stage_handover again.")
            # Store: dedup + ID + created_at + schema, writes the pending JSON
            try:
                task = store.create(fields, force=bool(force))
            except DuplicateTaskError as e:
                return (f"ERROR: duplicate — a task on the same topic already exists as "
                        f"{e.existing_id}. No new task created. Use the existing task "
                        f"or (only when instructed) set force=true.")
            except ValueError as e:
                return f"ERROR: {e} — no task created."
            tid = task["id"]
            log.append(f"task created: {tid} (pending, created_at={task['created_at']})")
            ho_md = _normalize_handover_id(handover_md, tid)
            # append the richer token-budgeted Memory brief from past patterns (#458 D1, fail-soft):
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
                        title=fields.get("title", task.get("title", "")),
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
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, ho_md)
            log.append(f"handover written: {ho} ({len(ho_md)} chars)")
        else:
            # Pure handover without task JSON — requires a valid task_id.
            if not task_id or not _TASK_ID_RE.match(task_id):
                return f"ERROR: without task_json a valid task_id is required (was: {task_id!r})"
            tid = task_id
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, handover_md)
            log.append(f"handover written: {ho} ({len(handover_md)} chars)")

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


# ─── TaskStore: deterministic task truth (model 3) ─────
# Single truth: tasks/<status>/KGC-NNN.json. The DIRECTORY is the
# status authority; the status field is updated by the store; active.md
# is a projection of the in_progress handover. All mutations go
# through this API, serialized (single-writer). NO AI involvement:
# ID assignment, created_at, schema, double-ID and topic dedup are code.

class DuplicateTaskError(Exception):
    """Raised when a task on the same topic already exists."""
    def __init__(self, existing_id: str):
        super().__init__(f"duplicate of {existing_id}")
        self.existing_id = existing_id


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
        if self.root is None:                  # #15: only gate when routed to the ACTIVE initiative
            _mpr = _mpr_blocks_tasks()
            if _mpr:
                raise RuntimeError(_mpr)
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
        validates, rejects a topic duplicate (unless force). Model-supplied
        id/created_at/status are IGNORED/overwritten."""
        with self._lock:
            self._validate(fields)
            self._require_base()   # B3: fail-closed — no writing to the root without an active initiative
            if not force:
                dup = self.find_duplicate(fields["title"], fields.get("description", ""))
                if dup:
                    raise DuplicateTaskError(dup)
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
            self._dir(to_status).mkdir(parents=True, exist_ok=True)
            _atomic_write(self._path(task_id, to_status),
                          json.dumps(data, ensure_ascii=False, indent=2))
            if s != to_status:
                p.unlink()
            self.project_active()
            return data

    def project_active(self):
        """active.md = handover of the newest NON-done task (in_progress before
        pending at the same timestamp), otherwise idle. Pure projection — never
        to be maintained by hand."""
        with self._lock:
            b = self._base()
            if b is None:
                return                       # no active initiative → no projection (soft)
            active = b / WORKFLOW_DIR / "active.md"
            # in_progress ranks before pending; within, by created_at/id.
            cands = [(0, t) for t in self.list("pending")] + \
                    [(1, t) for t in self.list("in_progress")]
            if cands:
                cands.sort(key=lambda it: (it[0], it[1].get("created_at", ""), it[1].get("id", "")))
                ho = self._handover_path(cands[-1][1].get("id", ""))
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
            "Operating system: **Windows**. For `execute_command` use PowerShell syntax "
            "(e.g. `Get-Date`, `Get-ChildItem`, `Get-Content`, `Select-String`) — NO Unix "
            "commands like `date`, `ls`, `cat`, `grep`."
        )
    return (
        "## Runtime environment\n"
        "Operating system: **Linux**. For `execute_command` use POSIX/bash syntax "
        "(e.g. `date`, `ls`, `cat`, `grep`)."
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


def run_tool(name: str, args: Dict[str, Any]) -> str:
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
            # errors="replace": a non-UTF-8 file is read lossily, never crashes the read.
            text = p.read_text(encoding="utf-8", errors="replace")
            # PERF-05: don't load very large files uncapped into the context
            if len(text) > MAX_FILE_CHARS:
                head_n = MAX_FILE_CHARS * 2 // 3
                tail_n = MAX_FILE_CHARS - head_n
                omitted = len(text) - head_n - tail_n
                return (
                    text[:head_n]
                    + f"\n\n... [Ironclad: {omitted} chars omitted — file {len(text)} "
                      f"chars, capped at {MAX_FILE_CHARS}. For targeted excerpts use "
                      f"execute_command, e.g. findstr/Select-String.] ...\n\n"
                    + text[-tail_n:]
                )
            return text

        elif name == "write_file":
            p   = _resolve_exec_path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(args["content"], encoding="utf-8")
            tmp.replace(p)
            return f"OK: Written {len(args['content'])} chars to {args['path']}"

        elif name == "list_directory":
            p = _resolve_exec_path(args.get("path", "."))
            if not p.exists():
                return f"ERROR: Not found: {args.get('path', '.')}"
            items = list(p.iterdir())
            total = len(items)
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
            out = "\n".join(lines) if lines else "(empty)"
            shown = len(lines)
            if shown < total:
                out += (f"\n... [GX10v3: showing {shown} of {total} entries"
                        + (f" (hard cap {LIST_DIR_HARD_CAP} — use sort='time'+limit)" if capped else f" (limit={limit})")
                        + "]")
            return out

        elif name == "execute_command":
            timeout = int(args.get("timeout", 30))
            command = args["command"]
            # #459: the fail-closed shell guardrail already ran at the top of run_tool (server-side, before
            # any bridge), so a blocked command never reaches here.
            # Platform mode determines the interpreter — consistent with the
            # syntax guidance injected into the model.
            # stdin=DEVNULL: interactive commands (e.g. cmd `date` without an arg)
            # get EOF immediately instead of blocking for the full timeout.
            # encoding/errors explicit: decode command output as UTF-8 lossily, so a
            # non-locale byte (cp1252 on Windows) never raises decoding the result.
            if PLATFORM == "windows":
                # #459: harden the PowerShell invocation — silence WriteProgress so a progress bar can
                # never draw into the renderer-owned conhost (a 2nd layer behind the deny-list above).
                hardened = "$ProgressPreference='SilentlyContinue'; " + command
                argv = ["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", hardened]
                r = subprocess.run(
                    argv, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, cwd=_exec_cwd()       # S9c: run in the active project's root (None → process workdir)
                )
            else:
                r = subprocess.run(
                    command, shell=True, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, cwd=_exec_cwd()       # S9c: run in the active project's root (None → process workdir)
                )
            out = (r.stdout + r.stderr).strip()
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
            for fp in _resolve_exec_path(directory).rglob(file_pattern):
                if fp.is_file():
                    try:
                        for i, line in enumerate(
                            fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                        ):
                            if _hit(line):
                                hits.append(f"{fp}:{i}: {line.strip()}")
                    except Exception:
                        pass
            return "\n".join(hits[:50]) if hits else "No matches"

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
            # S6b: route through the curated facade (the engine driver delegates to _stage_handover).
            if _devapi is not None and _devapi.get_driver() is not None:
                return _devapi.stage_handover(
                    args.get("agent", ""),
                    args.get("handover_md", ""),
                    task_id=args.get("task_id"),
                    task_json=args.get("task_json"),
                    set_active=args.get("set_active", True),
                    force=args.get("force", False),
                )
            return _stage_handover(
                args.get("task_id"),
                args.get("agent", ""),
                args.get("handover_md", ""),
                args.get("task_json"),
                args.get("set_active", True),
                args.get("force", False),
            )

        elif name == "check_task_exists":
            title = args.get("title", "")
            if not title.strip():
                return "ERROR: title required"
            existing = _store().find_duplicate(title, args.get("description", ""))
            return f"EXISTS: {existing}" if existing else "NONE"

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
            result = handler(**args)
            if inspect.iscoroutine(result):
                result.close()
                return (f"ERROR: plugin '{name}' is async; the engine tool path needs a "
                        f"synchronous run() (see docs/plugin-api.md).")
            return str(result)

        else:
            return f"ERROR: Unknown tool: {name}"

    except subprocess.TimeoutExpired:
        return f"ERROR: Timeout after {args.get('timeout', 30)}s"
    except Exception as e:
        return f"ERROR: {e}"

# ─── Orchestrator ─────────────────────────────────────────────
class GX10:
    def __init__(self, base_url: str, api_key: str, model: str, prompt_path: str,
                 stream: bool = True, max_tokens: int = MAX_TOKENS,
                 thinking_mode: str = "auto", platform: Optional[str] = None,
                 onboarding: Optional[bool] = None):
        self.client        = OpenAI(base_url=base_url, api_key=api_key)
        self.model         = model
        self.stream        = stream
        self.max_tokens    = max_tokens
        self.thinking_mode = thinking_mode   # "auto" | "first" | "off" | "all"
        self.platform      = platform or PLATFORM   # "windows" | "linux"
        self.onboarding    = ONBOARDING_MODE if onboarding is None else bool(onboarding)
        self.messages: List[Dict] = []
        self.last_response = ""
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
        if _MemoryManager is not None and _MEMORY_CONFIG and _MEMORY is None:
            _MEMORY = _MemoryManager(_MEMORY_CONFIG)
        # Initialize the warm tier (B0) — optional; without a url the tier stays a no-op (fail-soft).
        if _WarmTier is not None and _WARM_CONFIG and _WARM is None:
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

    # OPT-4: one completion call with 1× retry on a transient API error
    def _preflight_context(self, think: bool) -> None:
        """Epic #366 (#372): before the vLLM call, make sure the full prompt + the reserves it must
        leave free — output (``self.max_tokens``) + the tools schema vLLM serializes into the prompt
        + the CONDITIONAL thinking budget (only when ``think``) — fit the model window. If not, do
        ONE emergency trim of the oldest WHOLE rounds; if it still can't fit, raise a clear
        ``ContextOverflowError`` instead of letting vLLM return a raw HTTP 400. Fail-fast (no retry
        against vLLM) and fail-soft: skipped when token budgeting is off OR no EXACT tokenizer is
        reachable — the calibrated estimate over-counts, so trusting it here could raise a FALSE
        ContextOverflowError on input that would actually fit; #371's calibrated ``_trim_context`` has
        already budgeted conservatively in that mode."""
        if not TOKEN_BUDGET or _live_token_counter() is None:
            return
        tools_tok = _tools_schema_tokens()
        think_tok = THINKING_RESERVE if think else 0
        reserve = int(self.max_tokens) + tools_tok + int(think_tok)
        budget = MAX_MODEL_LEN - reserve
        est = _count_prompt_tokens(self.messages)
        if _live_token_counter() is None:
            return                                  # tokenizer DIED mid-count ⇒ `est` is contaminated by
                                                    # the over-counting char fallback ⇒ abort (no false raise)
        if est <= budget:
            return
        est = self._emergency_trim(budget)          # ONE pass: drop oldest whole rounds
        if _live_token_counter() is None:
            return                                  # died mid-trim ⇒ don't raise on a contaminated count
        if est > budget:
            raise ContextOverflowError(
                f"context overflow: prompt ~{est} tok + reserve {reserve} "
                f"(output {self.max_tokens}"
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
        return _count_prompt_tokens(self.messages)

    def _make_completion(self, think: bool, stream: bool):
        self._preflight_context(think)              # #372: guard + emergency trim (raises on irreducible overflow)
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=self.messages,
            tools=_effective_tools(),
            tool_choice="auto",
            temperature=TEMPERATURE,
            max_tokens=self.max_tokens,
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
                return self.client.chat.completions.create(**kwargs)
            except Exception as e:
                last_err = e
                if attempt == 0 and not _CANCEL_EVENT.is_set():
                    time.sleep(RETRY_BACKOFF)
                    continue
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
            p.write_text(
                json.dumps({"messages": self.messages}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
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
            loaded = [m for m in data.get("messages", []) if m.get("role") != "system"]
            loaded = self._sanitize_messages(loaded)
            system = next((m for m in self.messages if m.get("role") == "system"), None)
            self.messages = ([system] if system else []) + loaded
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
        try:
            new_summary = self._summarize(prev["content"] if prev else "", raw)
        except Exception as e:  # noqa: BLE001 — a failed summary must not tip the turn
            _ui_print(col(f"[WARN] context summary skipped: {e}", C.YELLOW))
            return
        if not new_summary.strip():
            return
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
        if not self.stream:
            return self._generate_plain(think)

        chunk_q: _q.Queue = _q.Queue()
        err     = [None]
        usage   = [None]          # OPT-3: usage from the last chunk
        done    = threading.Event()

        def _worker():
            try:
                s = self._make_completion(think, stream=True)
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
                    chunk_q.put(chunk.choices[0].delta)
            except Exception as e:
                err[0] = e
            finally:
                done.set()

        t0 = time.time()
        t_first = [None]          # OPT-3: time of the first token
        th = threading.Thread(                                  # bind the active ProjectContext into the worker (S3b):
            target=(_pc.bound_target(_worker) if _pc is not None else _worker), daemon=True)
        th.start()

        tf        = _ThinkFilter()
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
            try:
                delta = chunk_q.get(timeout=0.1)
            except _q.Empty:
                continue

            if t_first[0] is None:
                t_first[0] = time.time()

            if getattr(delta, "content", None):
                parts.append(delta.content)
                renderer.feed(tf.feed(delta.content))

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

        renderer.feed(tf.flush())
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
        }
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
        self.messages.append({"role": "user",
                              "content": (prefix + "\n\n" + user_input) if prefix else user_input})
        # #602 2.0/#690: publish the turn-start boundary (observer-only; byte-identical with no subscriber).
        _emit_hook("pre_turn", {"user_input": user_input, "agent": self})

        # auto mode: decide once per turn whether iteration 0 thinks
        self._turn_think = self._classify_thinking(user_input)

        turn = {"t0": time.time(), "gens": 0, "prompt": 0, "completion": 0}
        # Turn outcome — ALWAYS printed as a status line in finally.
        outcome: Dict[str, Any] = {"kind": "max"}

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

            if cancelled:
                outcome = {"kind": "abort"}
                return
            if err:
                outcome = {"kind": "error", "detail": f"API: {err}"}
                return

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
            if metrics:
                self._perf["gens"]       += 1
                self._perf["prompt"]     += metrics.get("prompt_tokens") or 0
                self._perf["completion"] += metrics.get("completion_tokens") or 0
                self._perf["wall"]       += metrics.get("total") or 0.0
                self._perf["last"]        = self._fmt_perf(metrics)
                turn["gens"]       += 1
                turn["prompt"]     += metrics.get("prompt_tokens") or 0
                turn["completion"] += metrics.get("completion_tokens") or 0
                _ui_print(col("  " + self._perf["last"], C.GRAY))

            # PERF-02: persist ONLY the cleaned content (no <think>). When the
            # tool call was recovered from the text, `content` is the call marker
            # itself — don't duplicate it as assistant.content (confuses some templates).
            cleaned = "" if recovered_from_text else clean(content)
            msg_dict: Dict[str, Any] = {"role": "assistant", "content": cleaned or None}
            if tool_calls:
                msg_dict["tool_calls"] = [
                    {
                        "id":       t["id"] or f"call_{iteration}_{i}",
                        "type":     "function",
                        "function": {
                            "name":      t["name"] or "",
                            "arguments": t["arguments"] or "{}",
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
                name = t["name"] or ""
                tcid = t["id"] or f"call_{iteration}_{i}"
                # Validate→Reask at the tool boundary: malformed JSON or a schema
                # violation is fed back as the tool result so the model re-emits,
                # instead of silently degrading to empty args.
                args, arg_err = _parse_tool_args(name, t["arguments"])
                if arg_err:
                    _ui_print(col(f"  → {name}", C.MAGENTA) +
                              col(f"  ✗ {arg_err[:80]}", C.RED))
                    self.messages.append({
                        "role": "tool", "tool_call_id": tcid,
                        "content": f"ERROR: {arg_err}",
                    })
                    continue

                args_disp = ", ".join(
                    f"{k}={repr(str(v))[:50]}" for k, v in args.items()
                )
                _ui_print(
                    col(f"  → {name}", C.MAGENTA) +
                    col(f"({args_disp})", C.GRAY),
                    end="  "
                )

                result_t = run_tool(name, args)
                preview  = result_t.replace("\n", " ")[:70]
                _ui_print(col(
                    f"✓ {preview}",
                    C.GREEN if not result_t.startswith("ERROR") else C.RED
                ))

                self.messages.append({
                    "role":         "tool",
                    "tool_call_id": tcid,
                    "content":      result_t
                })
                # #602 2.0/#690: publish the tool-result boundary (ctx carries the tool name).
                _emit_hook("post_toolresult", {"tool": name, "args": args, "result": result_t,
                                               "agent": self})
          # Loop ran through normally → max iterations (outcome stays "max")
        except Exception as e:
            # Catches unexpected errors so the agent thread does NOT die
            # and the turn still gets a completion marker.
            outcome = {"kind": "crash", "detail": repr(e)}
        finally:
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
    tool <name> <args|text>   run a tool DIRECTLY/deterministically (no model election, no RAG);
                              text → first required arg, or {json}. e.g. tool mpr_research <frage>
    rag on|off       toggle per-turn retrieval (RAG) for this session
    context          show the context-budget report
    fork [unit]      show the MPR architecture-decision proposal at a fork (recommendation only — you decide)
    ace warmup --ledger <path>   offline warm-start the active playbook from a dev-loop ledger's history
    ace eval --ledger <path>     efficiency diagnostic: ACE vs full-rewrite/evolutionary (J-001/J-002)
    generate <args>  scaffold a paved-road capability into the active project library
    initiative new <name> --type mpr|software   create + activate a initiative (artefact home)
    initiative list | use <slug> | active | reconcile [slug]
    project list | new <name> [--type mpr|software] [--path <dir>] | active | track new|use|list
                  manage registered, isolated projects (the guided setup command; /initiative is a deprecated alias)
    switch <project_id>   rebind this engine to a project (own paths + memory partition)
    watcher on|off        enable / disable the feedback watcher
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
        col(f"  watcher       : enabled={bool(wa['enabled'])} · interval={wa['interval']}s · dir={wa['feedback_dir']}", C.GRAY),
        col(f"  thinking_auto : {len(ta['planning_keywords'])} planning / {len(ta['routine_keywords'])} routine keywords", C.GRAY),
        col(f"  workspace     : {len(ws['dirs'])} dirs", C.GRAY),
        col(f"  ui            : max_lines={ui['max_lines']} · refresh={ui['refresh_interval']}s", C.GRAY),
        col(f"  Precedence    : code-defaults < file/conf < env", C.GRAY),
    ])


# ─── Runtime config control (/config get|set) ─────────────────
# Generic, plugin-agnostic runtime override of the live config tree. `/config set <dotted.key> <value>`
# writes the merged in-memory config (_EFFECTIVE_CFG) and re-derives the engine globals via _apply_config;
# plugin sections (e.g. an `mpr` block read by the MPR plugin per request) take effect on their next call.
# Secret-free + no plugin-specific knowledge here — see docs/config-runtime.md.
#
# Frozen keys are BOOT-ONLY: they wire something at startup (e.g. the offload runner for `setup.type`),
# so a runtime mutation would be incoherent. `/config set` refuses them; `/config get` still reads them.
# Boot-only keys: read once at startup to wire the runner/topology (setup.type) or the trust policy
# + the effective bind host (security.profile, e.g. sealed→loopback). A runtime change would
# NOT re-wire the already-built dispatcher/policy/socket → `/config set` refuses it
# with the boot-only message. Set it in the deploy config + restart. See config-runtime.md.
_FROZEN_CONFIG_KEYS = frozenset({
    "setup.type", "security.profile",
    # epic #505: boot-only so a runtime `/config set` cannot lift the seal or re-point the search
    # adapter/key without a restart (else it defeats the boot-time fail-closed guarantees).
    "security.web_in_sealed", "search.enabled", "search.adapter", "search.api_key_env",
})


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
            m = re.search(r"--type[=\s]+(\S+)", rest)   # --type X or --type=X (position-independent)
            if not m:
                return "usage: /initiative new <name> --type mpr|software"
            name = (rest[:m.start()] + rest[m.end():]).strip()
            v = initiative_new(name, m.group(1))
            # Name the artefacts ACTUALLY seeded for this type (derived from the skeleton, so the
            # message can never drift from reality) — mpr has no tasks/handovers.
            visible = ", ".join(sorted(
                {d.split("/")[0] for d in _INITIATIVE_SKELETON[v.type] if not d.startswith(WORKFLOW_DIR)}
            ))
            out = _msg("init.cmd_created", slug=v.slug, type=v.type, path=v.path.as_posix(), visible=visible)
            _cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
            # #503 MPR-ENV-2: mpr.enabled defaults to ON (MprConfig.enabled=True). The engine config tree
            # carries no mpr default, so `_cfg_get` returns None when the key is unset — only warn "MPR not
            # active" when it is EXPLICITLY disabled (a falsy value that is actually present), never on unset.
            _mpr_enabled = _cfg_get(_cfg, "mpr.enabled")
            if v.type == "mpr" and _mpr_enabled is not None and not _mpr_enabled:
                out += _msg("init.cmd_mpr_hint")
            return out
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
            lines = ["[initiative]  (* = active)"]
            for v in vs:
                mark = "*" if v.slug == cur else " "
                lines.append(f"  {mark} {v.slug}  ·  type {v.type} · status {v.status} · {v.created}")
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
        return ("usage: /initiative new <name> --type mpr|software | list | use <slug> | "
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
        return ("usage: /lifecycle gate --slug <slug> --tree <sha> [--ledger <path>] "
                "[--stages tests,reviews,delivery]")
    rest = arg_str[len(parts[0]):].strip()

    def _flag(name: str) -> "Optional[str]":
        m = re.search(rf"--{name}[=\s]+(\S+)", rest)   # --x VALUE or --x=VALUE (position-independent)
        return m.group(1) if m else None

    if _lifecycle_projector is None:
        return "[lifecycle] BLOCKED: lifecycle projector unavailable (engine module not importable)"
    slug = (_flag("slug") or active_slug() or "").strip()
    tree_sha = (_flag("tree") or "").strip()
    stages_arg = _flag("stages")
    required_stages = ([s.strip() for s in stages_arg.split(",") if s.strip()]
                       if stages_arg else list(_LIFECYCLE_DEFAULT_STAGES))
    if not slug:
        return "[lifecycle] BLOCKED: no slug — pass --slug <slug> or set an active initiative"
    if not tree_sha:
        return "[lifecycle] BLOCKED: no delivery tree_sha — pass --tree <sha>"
    if not required_stages:
        return "[lifecycle] BLOCKED: no required stages — pass --stages a,b,c"
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
    ``_EFFECTIVE_CFG`` so ``/config get`` and the rest of the engine see the active project's config."""
    global _EFFECTIVE_CFG
    _apply_config(merged)
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
    _set_active_project(target)               # publish to this process's other threads (only AFTER commit)
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
        lines = ["[project] tracks  (* = active):"]
        lines += [f"  {'*' if t == act else ' '} {t}" for t in tracks]
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


_PROJECT_NEW_USAGE = "usage: /project new <name> [--type mpr|software] [--path <dir>]"


def _parse_project_new(arg_str: str) -> "Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]":
    """Parse `new <name> [--type T] [--path P]` (P may be quoted) → (name, type|None, path|None, error|None).
    The name is every remaining positional token (a multi-word name is slugified). ``--type`` is validated
    against the initiative types; an unknown type is an error (fail-closed, nothing is created)."""
    s = arg_str
    typ: Optional[str] = None
    path: Optional[str] = None
    mt = re.search(r"--type(?:=|\s+)(\S+)", s)
    if mt:
        typ = mt.group(1)
        s = s[:mt.start()] + s[mt.end():]
    mp = re.search(r"--path(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)", s)
    if mp:
        raw = mp.group(1)
        path = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'" else raw
        s = s[:mp.start()] + s[mp.end():]
    name = " ".join(s.split())
    # A bare / empty-valued flag (e.g. `--type`, `--type=`, `--path`) does not match the value-bearing regex
    # above, so it would otherwise survive in the name and mint a bogus project — fail-closed instead.
    if re.search(r"--(?:type|path)\b", name):
        return (None, None, None, "a --type/--path flag needs a value")
    if not name:
        return (None, None, None, _PROJECT_NEW_USAGE)
    if typ is not None and typ.strip().lower() not in INITIATIVE_TYPES:
        return (None, None, None, f"unknown --type {typ!r} (allowed: {', '.join(INITIATIVE_TYPES)})")
    return (name, (typ.strip().lower() if typ else None), path, None)


def _project_new_mint(agent: "GX10", arg_str: str) -> str:
    """`/project new <name> [--type mpr|software] [--path <dir>]` — the guided-setup mint (ADR-0011 / S16):
    register a fresh isolated PROJECT (root = ``--path`` or ``<cwd>/<slug>``; a minted ``mem_ns``), then
    **activate it through the full quiesced switch** (so the leaving conversation is saved, a fresh one is
    started, the rolling summary / last-response are cleared, and in-flight work is refused — exactly like
    ``/switch``, so a mid-session ``new`` never bleeds the old conversation into the new project), and finally
    — when a ``--type`` is given — seed its first work unit (a typed vault initiative) under the new project.
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
            v = initiative_new(name, typ)                # seed the first work unit under the now-active project
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
    # Forget every memory scope the project owns BEFORE dropping the registry entry (so an interruption can
    # never leave orphan memory behind a still-removed entry). Best-effort; a forget hiccup never blocks the
    # delete, and re-running delete (or the S15 orphan-GC) cleans any residue.
    forgotten = 0
    for sc in _project_scopes(proj):
        try:
            _forget_scope(sc)
            forgotten += 1
        except Exception:  # noqa: BLE001
            pass

    if not purge:
        # Atomic against root reuse: remove only if pid still owns this exact root.
        try:
            removed = _REGISTRY.remove(pid, expected_root=root)
        except Exception as e:  # noqa: BLE001
            return f"[project] delete failed: {e!r}"
        if removed is None:
            return f"[project] {pid} was already removed or changed underneath — nothing deleted"
        return f"[project] deleted {pid} (forgot {forgotten} memory scope(s))"

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
        return (f"[project] deleted {pid} (forgot {forgotten} memory scope(s)) · "
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
        return f"[project] deleted {pid} (forgot {forgotten} memory scope(s)) · purged {root}"
    except OSError as e:
        return (f"[project] deleted {pid} (forgot {forgotten} memory scope(s)) · "
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
                return "[project] none registered — /project new <name> [--type mpr|software]"
            cur = _ACTIVE_PROJECT.id if _ACTIVE_PROJECT is not None else None
            n_arch = sum(1 for p in projs if getattr(p, "archived", False))
            head = "[project]  (* = active" + (", [archived] hidden — /project list --all" if (n_arch and not show_all) else "") + ")"
            lines = [head]
            for p in shown:
                mark = "*" if p.id == cur else " "
                root = str(_BOOT_WORKDIR) if (p.id == default_id and _BOOT_WORKDIR is not None) else p.root
                tag = " [archived]" if getattr(p, "archived", False) else ""
                lines.append(f"  {mark} {p.id}{tag}  ·  {root}  ·  mem_ns {(p.mem_ns or '-')[:8]}")
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
        return ("usage: /project list [--all] | new <name> [--type mpr|software] [--path <dir>] | active | "
                "track new|use|list | delete <id> [--purge] | archive <id> | unarchive <id>")
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
        skills.append({"name": name, "kind": "tool", "description": str(fn.get("description", "") or "")})
    skills.sort(key=lambda s: s["name"])
    return {"prompts": prompts, "skills": skills}


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
    lines.append(col("  → playbooks load via the use_skill tool; typed tools are model-elected (or /tool <name>).", C.GRAY))
    return "\n".join(lines)


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
        return ("usage: /generate [--kind case|prompt] --domain <d> --case <c> --description <text> "
                "[--prefix x] [--dry-run]\n"
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
    elif cmd == "clear":
        _ui_print(agent.clear_context())
    elif cmd == "status":
        _ui_print(agent.status())
    elif cmd == "prompts":
        _ui_print(_render_prompts())
    elif cmd == "skills":
        _ui_print(_render_skills())
    elif cmd == "config":
        _ui_print(_render_config())
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
            _ui_print(col("  usage: /config set <dotted.key> <value>", C.YELLOW))
        elif _EFFECTIVE_CFG is None:
            _ui_print(col("  [config] no live config to set (start the server first)", C.YELLOW))
        elif parts[2].strip() in _FROZEN_CONFIG_KEYS:
            _ui_print(col(f"  [config] '{parts[2].strip()}' is boot-only — set it in the deploy "
                          f"(env/config-file), not at runtime.", C.YELLOW))
        else:
            key, val = parts[2].strip(), _coerce_cfg_value(parts[3])
            _cfg_set(_EFFECTIVE_CFG, key, val)
            try:
                _apply_config(_EFFECTIVE_CFG)        # re-derive core globals; plugin sections re-read per call
            except Exception as e:  # noqa: BLE001 — non-core key (e.g. a plugin section) → dict write stands
                _ui_print(col(f"  [config] stored (not a core global: {e!r})", C.GRAY))
            _ui_print(col(f"  [config] set {key} = {val!r}", C.GREEN))
    elif cmd.startswith("read "):
        _ui_print(agent.manual_read(user_input[5:].strip()))
    elif cmd.startswith("write "):
        _ui_print(agent.manual_write(user_input[6:].strip()))
    elif cmd.startswith("cat "):
        _ui_print(agent.manual_cat(user_input[4:].strip()))
    elif cmd == "ls" or cmd.startswith("ls "):
        _ui_print(agent.manual_ls(user_input[2:].strip() or "."))
    elif cmd.startswith("watcher"):
        global _WATCHER_ENABLED
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            _WATCHER_ENABLED = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["watcher"]["enabled"] = True
            _ui_print(col("[RECONCILER] auto-advance ON — feedback is completed automatically", C.GREEN))
        elif arg == "off":
            _WATCHER_ENABLED = False
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["watcher"]["enabled"] = False
            _ui_print(col("[RECONCILER] auto-advance OFF — complete manually", C.YELLOW))
        else:
            state = col("ON", C.GREEN) if _WATCHER_ENABLED else col("OFF", C.YELLOW)
            _ui_print(f"  auto-advance (reconciler): {state}  |  watcher on / watcher off")
    elif cmd.startswith("autopilot"):
        global AUTOPILOT_ENABLED
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            AUTOPILOT_ENABLED = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["enabled"] = True
            msg = (f"[AUTOPILOT] ON (max_concurrent={AUTOPILOT_MAX_CONCURRENT}); "
                   f"takes effect on the next tick (~{RECONCILER_INTERVAL:.0f}s).")
            if not _WATCHER_ENABLED:
                msg += "  ⚠ reconciler is OFF — 'watcher on' is required, else nothing happens."
            _ui_print(col(msg, C.GREEN))
        elif arg == "off":
            AUTOPILOT_ENABLED = False
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["enabled"] = False
            _ui_print(col("[AUTOPILOT] OFF — no new auto-starts (running sessions remain)", C.YELLOW))
        else:
            state = col("ON", C.GREEN) if AUTOPILOT_ENABLED else col("OFF", C.YELLOW)
            _ui_print(f"  autopilot: {state}  |  autopilot on / autopilot off")
    elif cmd.startswith("autoplan"):
        global AUTOPILOT_AUTOPLAN, AUTOPILOT_MAX_TASKS, _AUTOPLAN_DONE
        parts = cmd.split()
        arg   = parts[1] if len(parts) > 1 else ""
        n_arg = parts[2] if len(parts) > 2 else None
        if arg == "on":
            # Optional count: "autoplan on 5"
            if n_arg is not None:
                try:
                    AUTOPILOT_MAX_TASKS = int(n_arg)
                    if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["autoplan_max_tasks"] = AUTOPILOT_MAX_TASKS
                except ValueError:
                    _ui_print(col(f"[AUTOPLAN] invalid number: {n_arg!r}", C.RED))
                    return  # type: ignore
            _AUTOPLAN_DONE     = 0   # always reset the counter on activation
            AUTOPILOT_AUTOPLAN = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["autoplan"] = True
            if AUTOPILOT_MAX_TASKS > 0:
                limit_info = f", max {AUTOPILOT_MAX_TASKS} tasks — stops automatically"
                _ui_print(col(
                    f"[AUTOPLAN] ON{limit_info}",
                    C.GREEN))
            else:
                _ui_print(col(
                    "[AUTOPLAN] ON — max_tasks=0 (INFINITE LOOP, no automatic stop!)\n"
                    "  → recommendation: set a limit with  autoplan off  then  autoplan on N",
                    C.YELLOW))
            _ui_print(col(
                "  ⚠ WARNING: NEVER use autoplan with a paid API subscription!\n"
                "    Every planning step = one model turn = cost. Local vLLM instances only!",
                C.RED))
        elif arg == "off":
            AUTOPILOT_AUTOPLAN = False
            _AUTOPLAN_DONE     = 0
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["autoplan"] = False
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
            AUTOPILOT_LOG_TERMINAL = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["log_terminal"] = True
            _ui_print(col("[LOG-TERMINAL] ON — the next autopilot start opens a live window (wt / PowerShell)", C.GREEN))
        elif arg == "off":
            AUTOPILOT_LOG_TERMINAL = False
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["log_terminal"] = False
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
            RAG_ENABLED = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["context"]["rag_enabled"] = True
            _ui_print(col("[RAG] per-turn retrieval ON", C.GREEN))
        elif arg == "off":
            RAG_ENABLED = False
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["context"]["rag_enabled"] = False
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
    elif cmd == "lifecycle" or cmd.startswith("lifecycle "):
        # S13b / AD-7: the engine DELIVER-leg lifecycle-completeness gate (reads the transition ledger as
        # data → projects stage-tagged evidence → verifies completeness). Deterministic, model-free.
        _ui_print(col(_lifecycle_command(user_input[len("lifecycle"):].strip()), C.CYAN))
    elif cmd == "fork" or cmd.startswith("fork "):
        # #903 (M5-3 output leg): surface the MPR architecture-decision proposal(s) at a fork as a
        # recommendation — the operator sees it here and decides (ACE learns the choice, M5-4). Read-only.
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

def _do_launch(task_id: str, agent: str):
    """Starts `claude --print` for a handover and moves the task to
    in_progress. The subprocess runs detached; a monitor thread frees the
    concurrency slot on exit. On error the slot is freed
    immediately. (The reconciler has already reserved the slot.)"""
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
    else:
        model, effort = None, None
    model  = model or spec.model                          # registry model (frontmatter `to:` overrides)
    # #500: when the handover carries no explicit `effort:`, auto-tier it by the task's class (security/
    # architecture → xhigh, routine → high) instead of the flat default; an explicit `effort:` still wins,
    # and a task that can't be loaded / an unmapped class falls through unchanged (fail-open).
    try:
        _rec = _store().get(task_id)
    except Exception:  # noqa: BLE001 — effort tiering must never block a launch
        _rec = None
    effort = _resolve_handover_effort(effort, _task_class(_rec) if _rec else None, spec.effort)
    prompt = (f"Autonomously read and work the handover {ho.as_posix()}. "
              f"Follow the instructions in .claude/CLAUDE.md.")
    _bin = spec.bin or AUTOPILOT_CLAUDE_BIN
    _tmpl = spec.cmd_template or ""
    # #449 (review B-1): the Claude `--print` autopilot shape KEEPS its stream plumbing (--verbose +
    # output-format stream-json + AUTOPILOT_EXTRA_ARGS) so the default OPUS/SONNET launch stays
    # byte-identical — the defaults carry a cmd_template now, so branch on the SHAPE, not its presence.
    _is_claude_print = _bin in (AUTOPILOT_CLAUDE_BIN, "claude") and "--print" in _tmpl
    if _is_claude_print or not _tmpl:
        argv = [_bin, "--model", str(model), "--effort", str(effort)]
        extra = list(AUTOPILOT_EXTRA_ARGS)
        if AUTOPILOT_STREAM:
            # Live streaming: stream-json NEEDS --verbose (otherwise claude aborts).
            # Output still goes to the log FILE (no pipe read) → no deadlock.
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
        _fbd = feedback_dir(soft=True)
        cap = str((_fbd / f"{task_id}_{agent}-feedback.md")) if _fbd else f"{task_id}_{agent}-feedback.md"
        argv = build_agent_argv(_tmpl, bin=_bin, model=str(model), effort=str(effort),
                                permission=spec.permission_mode or "", prompt=prompt, feedback=cap)
    # Autopilot logs are engine machinery (subprocess stdout), not an initiative artefact → under
    # state_root() (.ironclad/logs) instead of scattered in the WORKDIR root. An absolute override stays.
    _ld = Path(AUTOPILOT_LOGS_DIR)
    logdir = _ld if _ld.is_absolute() else state_root() / _ld
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{task_id}_{agent}.log"
    try:
        lf = open(logfile, "w", encoding="utf-8")
        # PYTHONIOENCODING=utf-8: prevents a cp1252 crash on non-ASCII characters
        # (e.g. → in handover texts) on Windows. Kimi and Claude both inherit it.
        _launch_env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # S9c: the code-agent runs in the active project's root (so its file edits land in that tree); the
        # default project resolves to None → the process workdir, byte-identical to the pre-isolation launch.
        proc = subprocess.Popen(argv, cwd=(_exec_cwd() or "."), stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, env=_launch_env)
    except Exception as e:
        _autopilot_release()
        _ui_print(col(f"  ✗ [AUTO] launch {task_id} failed: {e!r}", C.RED))
        return
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
        try:
            rc = proc.wait()
        finally:
            try:
                lf.close()
            except Exception:
                pass
            with _AUTOPILOT_LOCK:
                _AUTOPILOT_PROCS.pop(task_id, None)
            _autopilot_release()
        ok = (rc == 0)
        _ui_print(col(f"  {'✓' if ok else '⚠'} [AUTO] claude finished: {task_id} "
                      f"(exit {rc})", C.GREEN if ok else C.YELLOW))
        # Feedback check: warn if Claude finished without writing feedback.
        # No alert if the task is already done (advance ran before _wait → feedback already deleted).
        try:
            fb_dir = feedback_dir(soft=True)     # B3: <initiative>/.work/feedback (soft)
            found = list(fb_dir.glob(f"{task_id}_*-feedback.md")) if (fb_dir and fb_dir.exists()) else []
            if not found:
                # Task already completed? → no alert (advance deleted the feedback correctly)
                t = _store().get(task_id)
                already_done = t is not None and t.get("status") == "done"
                if not already_done:
                    _ui_print(col(
                        f"  ⚠ [AUTO] {task_id}: claude finished (exit {rc}) "
                        f"but NO feedback in .work/feedback/ — the task stays in_progress!",
                        C.RED))
        except Exception:
            pass
    threading.Thread(target=_wait, daemon=True).start()


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

def _reconcile_once(store: "TaskStore", enqueue, seen_mtime: Dict[str, float],
                    enqueued: set, launch_enqueue=None, launched: Optional[set] = None):
    """One reconciler tick. seen_mtime/enqueued/launched are persistent
    across ticks (completeness or dedup gate)."""
    # ── Launch side (autopilot): pending + handover → start claude ──
    if AUTOPILOT_ENABLED and launch_enqueue is not None and launched is not None:
        for task in sorted(store.list("pending"),
                           key=lambda t: (t.get("created_at", ""), t.get("id", ""))):
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
            if AUTOPILOT_MAX_CONCURRENT and _autopilot_active() >= AUTOPILOT_MAX_CONCURRENT:
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
        key = (tid, agent)
        if key in enqueued:
            continue
        try:
            mt = fb.stat().st_mtime
        except OSError:
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
        if not _WATCHER_ENABLED:
            continue
        bind_active()           # S5b: this daemon thread → the active project (re-read each tick; follows a switch)
        try:
            _reconcile_once(_store(), enqueue, seen_mtime, enqueued,
                            launch_enqueue, launched)
        except Exception as e:
            _ui_print(col(f"[WARN] reconciler tick failed: {e}", C.YELLOW))


# ─── Application UI ───────────────────────────────────────────
def _autoplan_prompt(tid: str) -> Optional[str]:
    """Build the 'plan the next task' prompt for autoplan from the configured
    capability backlog (``paths.active_capability_backlog``). Returns None when no
    backlog is configured — autoplan then has no source to plan from and stays idle
    (generic-safe: no vessel-specific default is assumed)."""
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


def _autoplan_tick(tid: str, enqueue) -> None:
    """After a successful advance: count it, enforce the max-tasks limit, and when the
    pipeline is empty enqueue the next planning turn. ``enqueue(prompt:str)`` puts the
    turn on the input queue. Gated on ``AUTOPILOT_AUTOPLAN`` only — independent of
    autopilot's *launch* side, so it works in the server/client split (server plans,
    client executes, server advances, server plans again)."""
    global _AUTOPLAN_DONE, AUTOPILOT_AUTOPLAN
    if not AUTOPILOT_AUTOPLAN:
        return
    _AUTOPLAN_DONE += 1
    _ui_print(col(f"  [AUTOPLAN] {_AUTOPLAN_DONE}"
                  + (f"/{AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else "")
                  + " tasks completed", C.CYAN))
    if AUTOPILOT_MAX_TASKS > 0 and _AUTOPLAN_DONE >= AUTOPILOT_MAX_TASKS:
        AUTOPILOT_AUTOPLAN = False
        if _EFFECTIVE_CFG:
            _EFFECTIVE_CFG["autopilot"]["autoplan"] = False
        _ui_print(col(f"\n  ✓ [AUTOPLAN] limit reached ({_AUTOPLAN_DONE}/"
                      f"{AUTOPILOT_MAX_TASKS}) — autoplan stopped.", C.GREEN))
        return
    s = _store()
    if s.list("pending") or s.list("in_progress"):
        return                       # pipeline not empty → nothing to plan
    prompt = _autoplan_prompt(tid)
    if prompt is None:
        AUTOPILOT_AUTOPLAN = False   # no backlog → autoplan has no source
        if _EFFECTIVE_CFG:
            _EFFECTIVE_CFG["autopilot"]["autoplan"] = False
        _ui_print(col("  [AUTOPLAN] no backlog configured "
                      "(paths.active_capability_backlog) — autoplan off.", C.YELLOW))
        return
    enqueue(prompt)
    _ui_print(col(f"\n  → [AUTOPLAN] queue empty after {tid} — planning the next task", C.CYAN))


def _code_defaults() -> Dict[str, Any]:
    """Snapshot of the module constants as the lowest precedence level."""
    return {
        "connection": {
            "base_url":    DEFAULT_BASE_URL,
            "model":       DEFAULT_MODEL,
            "api_key_env": API_KEY_ENV,
        },
        # epic #505 S3: minimal web-search seam selector. The FULL surface — env vars, the
        # frozen/runtime split, max_searches / max_output_chars / default_permission / domains and
        # boot key resolution — lands in S8 (#513). adapter: "cli" | "brave" | "mock".
        "search": {
            "enabled":          True,
            "adapter":          "cli",                  # backward-compatible default = today's CLI-delegate
            "api_key_env":      "GX10_SEARCH_API_KEY",  # NAME only; the secret VALUE is read from env at boot
            "count":            10,                     # results per native (http) search request
            "max_output_chars": 100_000,                # cap on the model-facing tool result (S5)
        },
        "platform": {
            "mode": PLATFORM_MODE,   # "auto" | "windows" | "linux"
        },
        "tasks": {
            "dedup_threshold": TASKS_DEDUP_THRESHOLD,
            "id_prefix":       TASK_PREFIX,
        },
        "ack": {
            "enabled": ACK_ENABLED,
        },
        "lodestar": {
            "enabled": LODESTAR_ENABLED,
        },
        "loop_profiles": {
            # epic #602 SUB-8a: per-TaskType loop budgets (max_iterations / retry_budget / effort). EMPTY by
            # default → resolve_loop_profile falls back to the engine globals (MAX_ITERATIONS + the re-ask
            # budget) → BYTE-IDENTICAL to today + single-sourced. An operator (or the private monorepo's
            # conf/loops override layer — NOT core/conf; the boundary forbids it) may add a `default` override
            # or per-type entries under `by_type` (e.g. {"by_type": {"research": {"max_iterations": 40}}}).
            "default": {},
            "by_type": {},
        },
        "lessons": {
            # epic #602 SUB-5: the project-private lesson distiller (a LessonProvider registered via
            # ack.lessons.set_provider). OPT-IN / default OFF → byte-identical: no provider is wired, so
            # the #601 lesson seam (handover brief read + completion write + scope-aware forget) stays a
            # no-op. When ON, EngineLessonStore persists scope-keyed lessons under ironclad_home()/lessons;
            # the typed distiller schema is provider-internal. C1 = project-private only (global
            # user_preferences tier deferred — needs the curated-global store + a promote() redactor).
            "enabled":       False,
            "max_per_scope": 200,    # compaction cap per scope (oldest dropped first)
        },
        "quality": {
            # epic #602 SUB-9: a SEPARATE per-task output-quality circuit breaker (distinct from the
            # availability breaker _CODE_AGENT_BREAKER). OPT-IN / default OFF → no breaker is built → no-op
            # byte-identical. When ON, a QualityBreaker trips on `min_consecutive` mark-only verifier scores
            # (ack.verify) below `threshold`; a trip is ADVISORY (escalate/surface, NEVER a hard-abort).
            "enabled":         False,
            "threshold":       0.5,
            "min_consecutive": 3,
            "window":          20,
        },
        "process": {
            # epic #602 SUB-6: Process-Level Self-Correction. OPT-IN / default OFF → no process-lesson is
            # recorded at completion and no hint is injected pre-turn (byte-identical). It records TYPED
            # process-lessons via the concrete EngineLessonStore (so it also needs lessons.enabled), NOT the
            # string-only ack.lessons seam.
            "enabled":   False,
            "max_hints": 3,
        },
        "ace": {
            # epic #855 ACE-WIRE (#863): the always-on Agentic Context Engineering loop-intelligence core.
            # ACE SUPERSEDES the #602 string lesson + Process-SC consumers (operator decision 2026-06-30) —
            # it is the engine's loop-intelligence mechanic, ALWAYS ON, there is NO enable flag. A
            # PlaybookStore is registered as the ack.lessons provider; a post_feedback consumer submits a
            # Trajectory to a background ReflectionWorker (reflect→curate→refine runs OFF the hot path, never
            # inline). The keys below are TUNING only; with no orchestrator model reachable, ACE simply
            # no-ops (fail-soft) and the playbook stays empty. C1 = project-private scope, like #602.
            "max_bullets": 200,    # per-scope playbook cap — the 32k-window guard (#366); 0/None = uncapped
            "rounds":      1,      # reflection rounds per online adaptation (L-001)
            "top_k":       8,      # bullets injected into the Generator handover context (H-001)
            "cost":        1,      # budget units charged per online adaptation (when a budget is wired)
            "embed_url":   "",     # the memory-service /embed endpoint (semantic dedup/retrieval); "" ⇒ derive
                                   # from GX10_MEMORY_URL, else lexical fallback (the dependency-free default)
        },
        "verify": {
            # epic #602 SUB-4 / 2.1: the MARK-ONLY Verifier on the dev-task pipeline. OPT-IN / default OFF →
            # no hook is registered, so the `pre_handover` Hook-Bus dispatch is an O(1) no-op (byte-identical).
            # When ON, a runner evaluates each staged task: deterministic BEHAVIORAL rules over task_json +
            # (when a memory tier is up) GROUNDING of the handover's claims against the cold store. It produces
            # a VerdictResult the Quality breaker (#602 SUB-9) reads — it NEVER gates a handover. The LLM-judge
            # is a separate explicit opt-in (it charges the budget ledger) and is not run by this hook.
            "enabled":             False,
            "grounding_threshold": 0.5,
        },
        "strategy": {
            # epic #602 SUB-3/SUB-7 (2.4-2.5): the failure→action policy on the code-agent failover. OPT-IN /
            # default OFF → no FailureClass is recorded on a run failure and no strategy is applied
            # (byte-identical). When ON, a code-agent run failure is classified into the shared FailureClass
            # (#805) and the Strategy Revisor maps it to a targeted action on the failover/retry path (#806):
            # a per-task attempt counter vs `budget` escalates to HUMAN_ESCALATION when spent (no endless
            # silent failover). MARK-ONLY/advisory — it surfaces, never hard-aborts.
            "enabled": False,
            "budget":  3,
        },
        "security": {
            # Phase-d trust profile (single-tenant): open | token | sealed.
            # The server (engine/security.py) reads this block; the token VALUE comes
            # from the env named here, never from config. See docs/roadmap.md.
            "profile":             "open",
            "token_env":           "GX10_SERVER_TOKEN",
            "session_heartbeat_s": 30,
            "code_locality":       "mount",   # sealed forces "local"
            "web_in_sealed":       False,     # epic #505 S7: opt-in to allow outbound web_search under sealed
        },
        "setup": {
            # Boot-fixed deployment topology (docs/setup-types.md). NOT runtime-switchable — a frozen
            # key (`/config set setup.type` is refused). Orchestrator + agents are ALWAYS co-located; the
            # setup.type just says on WHICH machine. Generic, secret-free:
            #   server (default): everything on the model host → in-engine only (external agents deferred);
            #                     byte-identical to a no-provider deployment.
            #   local:            engine + agents native on the desktop → offload = local subprocess; the
            #                     model + memory live remotely (base_url/memory_url point over the network).
            "type": "server",
        },
        "workers": {
            # Phase-e reasoning fan-out governor (engine/workers.py). CONSERVATIVE,
            # model-agnostic defaults — safe for an unknown endpoint, NOT tuned. The
            # private deploy pins the model-matched values in conf/ (our reference model
            # qwen3.6-35b: concurrency 8 = max_num_seqs). Envelope: concurrency × max_tokens
            # ≤ max_batch_tokens, so a large max_tokens lowers parallelism (never crashes).
            "concurrency":      4,
            "max_tokens":       1024,
            "max_batch_tokens": 8192,
            "memory_read":      WORKER_MEMORY,      # §3c MAP: per-item RAG + shared floor (default ON, 06-18)
            "memory_write":     WORKER_WRITE,       # §3c REDUCE: single-writer cold consolidation (default ON, 06-18)
            "write_mode":       WORKER_WRITE_MODE,  # "reducer" (default) | "direct" (autonomous agents)
        },
        "providers": {
            # P0 provider router (engine/providers.py + router.py + dispatch.py). Default EMPTY/OFF →
            # parallel_reason stays on _WORKERS.fanout, byte-identical. The private deploy supplies the
            # real pool (models, $/token, endpoints) in conf/ — NO provider literal in core/.
            "enabled":     False,            # global on/off; off ⇒ dispatcher delegates to _WORKERS
            "default_id":  None,
            "max_agents":  3,                # server CLI-pool cap (own default; NOT == client --max-agents)
            "cli_timeout_s": None,           # timeout for default_cli_runner (None ⇒ no timeout)
            "effort_max_tokens": {"low": 512, "medium": 1024, "high": 2048, "xhigh": 4096},
            "scoring":     {"w_cost": 1.0, "w_sensitivity": 0.5, "cost_norm_usd": 0.10,
                            "input_chars_per_token": 4},
            "budget":      {"usd_cap": None},
            "pool":        [],               # no default providers hard-coded (boundary); conf/ fills it
        },
        "code_agents": {
            # #449 (C0R-9): the handover code-AGENT registry — a SEPARATE, ALWAYS-ON surface, independent
            # of providers.enabled (which is True in local-mode). Each entry is a providers.ProviderSpec
            # carrying an agent_id (ASCII-letters-only filename token, C0R-1). Ironclad ships OPUS/SONNET
            # as OVERRIDABLE defaults (public Claude model ids — already used by the handover lane); conf/
            # re-lists the pool to add its own agents (lists replace on merge). Unknown agent → fail-closed.
            # The default agent CLI is Claude Code (the documented default backend — same shape as
            # client.DEFAULT_AGENT_CMD). Fully-specified so the server ships a complete spec; conf/ may
            # override the bin/template/model freely (or drop these for a different default agent).
            "pool": [
                {"provider_id": "claude-opus",   "kind": "cli", "agent_id": "OPUS",
                 "display": "Claude Opus 4.8",   "model": "claude-opus-4-8", "bin": "claude",
                 "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
                 "effort": "xhigh", "permission_mode": "acceptEdits"},
                {"provider_id": "claude-sonnet", "kind": "cli", "agent_id": "SONNET",
                 "display": "Claude Sonnet 4.6", "model": "claude-sonnet-4-6", "bin": "claude",
                 "cmd_template": "{bin} --model {model} --effort {effort} --permission-mode {permission} --print {prompt}",
                 "effort": "high", "permission_mode": "acceptEdits"},
            ],
            # #454: runtime operator OVERRIDE — `/coders use <id>` pins one agent so ALL handovers run
            # on it (the runtime switch); None ⇒ use the orchestrator's task-chosen (staged) agent. An
            # unknown/disabled pin fails closed (kept None / ignored). task-aware auto-routing is Phase 5.
            "pinned": None,
            # #455 (FORK-C=C): signals that mean an agent is OUT OF BUDGET/QUOTA → classify the run as
            # `agent-unavailable` (trip the circuit-breaker + fail over to the cheapest capable peer),
            # NOT a normal task failure. Layered JSON→stderr→exit; conservative. GENERIC, public-safe
            # defaults here; a deployment refines per-agent in conf/ — an agent's EXACT exhausted signal
            # is calibrated from ONE real run with operator consent (e.g. Kimi at #460).
            "exhausted": {
                "stderr_patterns": [
                    r"(?i)\b(quota|usage limit|rate limit|insufficient (credit|balance|quota))\b",
                    r"(?i)\b(out of|exceeded)\b.{0,24}\b(quota|credit|budget|tokens?)\b",
                    r"(?i)\b429\b.{0,20}too many requests",
                ],
                "exit_codes": [],
                "json_event_types": [],
            },
            # #456: task_class → the agents CAPABLE of that class (the operator matrix: OPUS for
            # security/architecture, all-rounders for coding, the cheaper/broad set for analysis). This
            # SCOPES failover (#455) + distinct-reviewer (#457) to task-appropriate agents — it does NOT
            # override the orchestrator's staged pick. Public default lists only OPUS/SONNET; conf/ adds
            # CODEX/KIMI. An unknown/missing class ⇒ no restriction (fail-open, byte-identical to #455).
            "classes": {
                "security":     ["OPUS"],
                "architecture": ["OPUS"],
                "coding":       ["OPUS", "SONNET"],
                "analysis":     ["SONNET"],
            },
        },
        "onboarding": {
            "enabled": ONBOARDING_MODE,
        },
        "autopilot": {
            "enabled":        AUTOPILOT_ENABLED,
            "claude_bin":     AUTOPILOT_CLAUDE_BIN,
            "extra_args":     list(AUTOPILOT_EXTRA_ARGS),
            "default_effort": AUTOPILOT_DEFAULT_EFFORT,
            "logs_dir":       AUTOPILOT_LOGS_DIR,
            "max_concurrent": AUTOPILOT_MAX_CONCURRENT,
            "stream":         AUTOPILOT_STREAM,
            "terminate_on_advance": AUTOPILOT_TERMINATE_ON_ADVANCE,
            "autoplan":           AUTOPILOT_AUTOPLAN,
            "autoplan_max_tasks": AUTOPILOT_MAX_TASKS,
            "log_terminal":       AUTOPILOT_LOG_TERMINAL,
        },
        "paths": {
            "system_prompt": DEFAULT_PROMPT,
            "workdir":       DEFAULT_WORKDIR,
            "state_root":    STATE_ROOT,    # hidden engine machinery (session.json, memory/, …)
            "vault_root":    VAULT_ROOT,    # visible knowledge root (vault/<slug>/ per initiative)
            "session_file":  SESSION_FILE,  # basename, resolved under state_root
            "code_root":     CODE_ROOT,
            # Open plugin surface: a dir scanned for `skills/*.py` plugins at startup
            # (GX10_PLUGINS_DIR). Empty = no plugins. See docs/plugin-api.md.
            "plugins_dir":   "",
            # ROUTE-1 (#503): optional list of scripts run (fail-soft) after a pipeline advance to
            # regenerate deployment-specific vault projections. Empty (default) ⇒ no subprocess. A
            # deploy sets e.g. ["scripts/update_capability_tracking.py", …]; absent scripts are skipped.
            "post_advance_hooks": [],
        },
        "generation": {
            "temperature":   TEMPERATURE,
            "max_tokens":    MAX_TOKENS,
            "thinking_mode": "auto",
            "stream":        True,
            "retry_backoff": RETRY_BACKOFF,
            "language":      LANGUAGE,
        },
        "context": {
            "max_iterations":     MAX_ITERATIONS,
            "max_ctx_chars":      MAX_CTX_CHARS,
            "trim_target_chars":  TRIM_TARGET_CHARS,
            "max_model_len":      MAX_MODEL_LEN,    # MEM-9: hard token window (budget source)
            "token_budget":       TOKEN_BUDGET,     # MEM-9: couple the trim to the window (default ON)
            "chars_per_token":    CHARS_PER_TOKEN,  # #366: calibrated chars/token FALLBACK (live tokenizer is primary)
            "thinking_reserve":   THINKING_RESERVE, # #366 D5: output headroom reserved when think=True
            "memory_brief_tokens": MEMORY_BRIEF_TOKENS,  # #458 D1: token budget of the handover memory brief
            "max_file_chars":     MAX_FILE_CHARS,
            "list_dir_hard_cap":  LIST_DIR_HARD_CAP,
            "summarize_evicted":  SUMMARIZE_EVICTED,   # B1: default ON (06-18); off → byte-identical trim
            "summary_max_tokens": SUMMARY_MAX_TOKENS,
            "rag_enabled":        RAG_ENABLED,         # B2: default ON (06-18); off → user message verbatim
            "rag_top_k":          RAG_TOP_K,
            "rag_max_tokens":     RAG_MAX_TOKENS,
        },
        "thinking_auto": {
            "planning_keywords": list(_PLANNING_KW),
            "routine_keywords":  list(_ROUTINE_KW),
        },
        "workspace": {
            "dirs":        list(WORKSPACE_DIRS),
            "idle_marker": _IDLE_ACTIVE,
        },
        "watcher": {
            "feedback_dir": WATCHER_FEEDBACK_DIR,
            "enabled":      _WATCHER_ENABLED,
            "interval":     RECONCILER_INTERVAL,
        },
        "ui": {
            "max_lines":        _UI_MAX_LINES,
            "refresh_interval": UI_REFRESH_INTERVAL,
            "spinner_frames":   SPINNER_FRAMES,
        },
    }


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
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(col(f"  [WARN] config not loadable ({p}): {e} — skipped.", C.YELLOW))
        return {}


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
    def setif(name, section, key, transform=lambda x: x):
        v = env.get(name)
        if v not in (None, ""):
            try:
                cfg[section][key] = transform(v)
            except Exception:
                print(col(f"  [WARN] env {name}={v!r} ignored (invalid)", C.YELLOW))
    setif("GX10_BASE_URL",   "connection", "base_url")
    setif("GX10_MODEL",      "connection", "model")
    setif("GX10_WORKDIR",    "paths",      "workdir")
    setif("GX10_PROMPT",     "paths",      "system_prompt")
    setif("GX10_MAX_TOKENS", "generation", "max_tokens", int)
    setif("GX10_THINKING",   "generation", "thinking_mode")
    setif("GX10_LANGUAGE",   "generation", "language")
    setif("GX10_PLATFORM",   "platform",   "mode")
    setif("GX10_PROFILE",          "security", "profile")
    setif("GX10_SESSION_HEARTBEAT","security", "session_heartbeat_s", int)
    setif("GX10_CODE_LOCALITY",    "security", "code_locality")
    setif("GX10_SETUP_TYPE",       "setup",    "type")    # boot-fixed offload topology (docs/setup-types.md)
    setif("GX10_PLUGINS_DIR",            "paths",    "plugins_dir")
    setif("GX10_FANOUT_CONCURRENCY",     "workers", "concurrency",      int)
    setif("GX10_WORKERS_MAX_TOKENS",     "workers", "max_tokens",       int)
    setif("GX10_WORKERS_MAX_BATCH_TOKENS","workers", "max_batch_tokens", int)
    _truthy = lambda v: v.strip().lower() in ("1", "true", "yes", "on")
    setif("GX10_ONBOARDING", "onboarding", "enabled", _truthy)
    # epic #505 S8: web-search knobs (the secret VALUE GX10_SEARCH_API_KEY is read from env at boot,
    # NOT here — non-secret only). search.web_in_sealed lives under security (S7).
    setif("GX10_SEARCH_ENABLED",          "search", "enabled", _truthy)
    setif("GX10_SEARCH_ADAPTER",          "search", "adapter")
    setif("GX10_SEARCH_COUNT",            "search", "count", int)
    setif("GX10_SEARCH_MAX_OUTPUT_CHARS", "search", "max_output_chars", int)
    setif("GX10_PROVIDERS",              "providers", "enabled",   _truthy)   # P0 router on/off (A/B switch)
    setif("GX10_PROVIDERS_DEFAULT",      "providers", "default_id")           # default provider id
    setif("GX10_PROVIDERS_BUDGET_USD",   "providers", "budget", lambda v: {"usd_cap": float(v)})  # run budget
    setif("GX10_PROVIDERS_MAX_AGENTS",   "providers", "max_agents", int)      # server CLI-pool cap
    setif("GX10_PROVIDERS_CLI_TIMEOUT_S","providers", "cli_timeout_s", int)   # CLI spawn timeout
    setif("GX10_CONTEXT_SUMMARY",    "context", "summarize_evicted",  _truthy)   # B1 switch
    setif("GX10_SUMMARY_MAX_TOKENS", "context", "summary_max_tokens", int)
    setif("GX10_CONTEXT_RAG",        "context", "rag_enabled",        _truthy)   # B2 switch
    setif("GX10_RAG_TOP_K",          "context", "rag_top_k",          int)
    setif("GX10_RAG_MAX_TOKENS",     "context", "rag_max_tokens",     int)
    setif("GX10_WORKER_MEMORY",      "workers", "memory_read",        _truthy)   # §3c MAP switch
    setif("GX10_WORKER_WRITE",       "workers", "memory_write",       _truthy)   # §3c REDUCE switch
    setif("GX10_WORKER_WRITE_MODE",  "workers", "write_mode")                    # reducer | direct
    # B4: trim working set re-tunable via env (proportional to a raised model window
    # IRONCLAD_MAX_MODEL_LEN), without a config-file edit on the Spark. Unset → today's values.
    setif("GX10_MAX_CTX_CHARS",      "context", "max_ctx_chars",      int)
    setif("GX10_TRIM_TARGET_CHARS",  "context", "trim_target_chars",  int)
    # MEM-9: model window as the budget source. IRONCLAD_MAX_MODEL_LEN (deploy/vLLM var) first,
    # GX10_MAX_MODEL_LEN overrides (more specific). GX10_TOKEN_BUDGET=0 → fixed char thresholds.
    setif("IRONCLAD_MAX_MODEL_LEN",  "context", "max_model_len",      int)
    setif("GX10_MAX_MODEL_LEN",      "context", "max_model_len",      int)
    setif("GX10_TOKEN_BUDGET",       "context", "token_budget",       _truthy)
    setif("GX10_CHARS_PER_TOKEN",    "context", "chars_per_token",    float)   # #366: calibrated fallback ratio
    setif("GX10_MEMORY_BRIEF_TOKENS", "context", "memory_brief_tokens", int)   # #458 D1: handover brief budget
    setif("GX10_THINKING_RESERVE",   "context", "thinking_reserve",   int)     # #366 D5: thinking output reserve
    setif("GX10_AUTOPILOT",  "autopilot",  "enabled", _truthy)
    setif("GX10_AUTOPILOT_STREAM",    "autopilot", "stream",          _truthy)
    setif("GX10_AUTOPILOT_TERMINATE", "autopilot", "terminate_on_advance", _truthy)
    setif("GX10_AUTOPILOT_AUTOPLAN",       "autopilot", "autoplan",           _truthy)
    setif("GX10_AUTOPILOT_MAX_TASKS",      "autopilot", "autoplan_max_tasks", int)
    setif("GX10_AUTOPILOT_LOG_TERMINAL", "autopilot", "log_terminal",  _truthy)
    return cfg


def _apply_config(cfg: Dict[str, Any]):
    """Writes the merged config back into the module globals, so the
    existing references (run_tool, macros, _trim_context, _classify_thinking,
    watcher, UI …) keep running unchanged."""
    global DEFAULT_BASE_URL, DEFAULT_MODEL, API_KEY_ENV, STATE_ROOT, VAULT_ROOT, SESSION_FILE, CODE_ROOT
    global PLATFORM_MODE, PLATFORM, TASKS_DEDUP_THRESHOLD, ONBOARDING_MODE, TASK_PREFIX, _TASK_ID_RE, ACK_ENABLED, LODESTAR_ENABLED
    global AUTOPILOT_ENABLED, AUTOPILOT_CLAUDE_BIN, AUTOPILOT_EXTRA_ARGS
    global AUTOPILOT_DEFAULT_EFFORT, AUTOPILOT_LOGS_DIR, AUTOPILOT_MAX_CONCURRENT, AUTOPILOT_STREAM, AUTOPILOT_TERMINATE_ON_ADVANCE, AUTOPILOT_AUTOPLAN, AUTOPILOT_MAX_TASKS, AUTOPILOT_LOG_TERMINAL
    global TEMPERATURE, MAX_TOKENS, RETRY_BACKOFF, LANGUAGE
    global MAX_ITERATIONS, MAX_CTX_CHARS, TRIM_TARGET_CHARS, MAX_FILE_CHARS, LIST_DIR_HARD_CAP
    global SUMMARIZE_EVICTED, SUMMARY_MAX_TOKENS, RAG_ENABLED, RAG_TOP_K, RAG_MAX_TOKENS
    global MAX_MODEL_LEN, TOKEN_BUDGET, CHARS_PER_TOKEN, THINKING_RESERVE, MEMORY_BRIEF_TOKENS
    global WORKER_MEMORY, WORKER_WRITE, WORKER_WRITE_MODE, WARM_SESSION_ID
    global _PLANNING_KW, _ROUTINE_KW, WORKSPACE_DIRS, _IDLE_ACTIVE
    global WATCHER_FEEDBACK_DIR, _WATCHER_ENABLED, RECONCILER_INTERVAL
    global SPINNER_FRAMES, UI_REFRESH_INTERVAL, _UI_MAX_LINES, _UI_LINES
    global _MEMORY_CONFIG, _WARM_CONFIG

    conn, paths, gen = cfg["connection"], cfg["paths"], cfg["generation"]
    ctx, ta, ws       = cfg["context"], cfg["thinking_auto"], cfg["workspace"]
    wa, ui            = cfg["watcher"], cfg["ui"]

    DEFAULT_BASE_URL = conn["base_url"]
    DEFAULT_MODEL    = conn["model"]
    API_KEY_ENV      = conn.get("api_key_env", API_KEY_ENV)
    STATE_ROOT       = paths.get("state_root", STATE_ROOT)
    VAULT_ROOT       = paths.get("vault_root", VAULT_ROOT)
    SESSION_FILE     = paths["session_file"]
    CODE_ROOT        = paths.get("code_root", CODE_ROOT)

    PLATFORM_MODE = cfg["platform"]["mode"]
    PLATFORM      = _resolve_platform(PLATFORM_MODE)   # one-time resolution of 'auto'

    TASKS_DEDUP_THRESHOLD = float(cfg["tasks"]["dedup_threshold"])
    TASK_PREFIX           = str(cfg["tasks"].get("id_prefix", TASK_PREFIX))
    _TASK_ID_RE           = re.compile(rf"^{re.escape(TASK_PREFIX)}-[A-Za-z0-9_]+$")
    ACK_ENABLED           = bool(cfg.get("ack", {}).get("enabled", ACK_ENABLED))
    LODESTAR_ENABLED      = bool(cfg.get("lodestar", {}).get("enabled", LODESTAR_ENABLED))
    ONBOARDING_MODE       = bool(cfg["onboarding"]["enabled"])

    ap = cfg["autopilot"]
    AUTOPILOT_ENABLED        = bool(ap["enabled"])
    AUTOPILOT_CLAUDE_BIN     = ap["claude_bin"]
    AUTOPILOT_EXTRA_ARGS     = list(ap["extra_args"])
    AUTOPILOT_DEFAULT_EFFORT = ap["default_effort"]
    AUTOPILOT_LOGS_DIR       = ap["logs_dir"]
    AUTOPILOT_MAX_CONCURRENT = int(ap["max_concurrent"])
    AUTOPILOT_STREAM         = bool(ap.get("stream", False))
    AUTOPILOT_TERMINATE_ON_ADVANCE = bool(ap.get("terminate_on_advance", False))
    AUTOPILOT_AUTOPLAN    = bool(ap.get("autoplan", False))
    AUTOPILOT_MAX_TASKS   = int(ap.get("autoplan_max_tasks", 0))
    AUTOPILOT_LOG_TERMINAL = bool(ap.get("log_terminal", False))

    TEMPERATURE   = float(gen["temperature"])
    MAX_TOKENS    = int(gen["max_tokens"])
    RETRY_BACKOFF = float(gen["retry_backoff"])
    LANGUAGE      = (str(gen.get("language", "en")).strip() or "en")

    MAX_ITERATIONS    = int(ctx["max_iterations"])
    MAX_CTX_CHARS     = int(ctx["max_ctx_chars"])
    TRIM_TARGET_CHARS = int(ctx["trim_target_chars"])
    MAX_FILE_CHARS    = int(ctx["max_file_chars"])
    LIST_DIR_HARD_CAP = int(ctx["list_dir_hard_cap"])
    SUMMARIZE_EVICTED  = bool(ctx.get("summarize_evicted", True))    # B1: default ON (06-18)
    SUMMARY_MAX_TOKENS = int(ctx.get("summary_max_tokens", SUMMARY_MAX_TOKENS))
    RAG_ENABLED        = bool(ctx.get("rag_enabled", True))          # B2: default ON (06-18)
    RAG_TOP_K          = int(ctx.get("rag_top_k", RAG_TOP_K))
    RAG_MAX_TOKENS     = int(ctx.get("rag_max_tokens", RAG_MAX_TOKENS))
    # MEM-9: couple the trim working set to the model window (after output/RAG/summary reserve). ON →
    # derive MAX_CTX_CHARS/TRIM_TARGET_CHARS from MAX_MODEL_LEN (overrides the char defaults);
    # OFF → the char thresholds above stay (today's behaviour, GX10_MAX_CTX_CHARS applies).
    MAX_MODEL_LEN      = int(ctx.get("max_model_len", MAX_MODEL_LEN))
    TOKEN_BUDGET       = bool(ctx.get("token_budget", True))
    CHARS_PER_TOKEN    = float(ctx.get("chars_per_token", CHARS_PER_TOKEN))   # #366 calibrated fallback
    THINKING_RESERVE   = int(ctx.get("thinking_reserve", THINKING_RESERVE))   # #366 D5
    MEMORY_BRIEF_TOKENS = int(ctx.get("memory_brief_tokens", MEMORY_BRIEF_TOKENS))   # #458 D1 handover brief budget
    if TOKEN_BUDGET and not (os.environ.get("GX10_MAX_CTX_CHARS") or os.environ.get("GX10_TRIM_TARGET_CHARS")):
        # MEM-9: derive the char watermark from the window (output/RAG/summary reserve). BUDGET-3 (#503):
        # SKIP the derive when the operator explicitly set GX10_MAX_CTX_CHARS / GX10_TRIM_TARGET_CHARS, so
        # those env vars are honored instead of being silently overwritten (the token budget stays primary).
        MAX_CTX_CHARS, TRIM_TARGET_CHARS = _derive_ctx_budget(
            MAX_MODEL_LEN, MAX_TOKENS, RAG_MAX_TOKENS, SUMMARY_MAX_TOKENS, CHARS_PER_TOKEN)
    _wcfg = cfg.get("workers", {})
    WORKER_MEMORY      = bool(_wcfg.get("memory_read", True))    # §3c MAP: default ON (06-18)
    WORKER_WRITE       = bool(_wcfg.get("memory_write", True))   # §3c REDUCE: default ON (06-18)
    WORKER_WRITE_MODE  = (str(_wcfg.get("write_mode", "reducer")).strip().lower() or "reducer")
    WARM_SESSION_ID    = (os.environ.get("GX10_SESSION_ID", "").strip() or "main")   # pure-from-base, no self-ref accumulation (S3b)

    _PLANNING_KW = tuple(ta["planning_keywords"])
    _ROUTINE_KW  = tuple(ta["routine_keywords"])

    WORKSPACE_DIRS = list(ws["dirs"])
    _IDLE_ACTIVE   = ws["idle_marker"]

    # Memory config: file (conf/memory/memory.json) OR env (GX10_MEMORY_URL).
    # Optional — without base_url _MEMORY_CONFIG stays empty → memory off (hooks inert).
    _MEMORY_CONFIG = {}                       # pure-from-base: re-derive fresh from file + env each reload (no stale keys, S3b)
    _mem_cfg_path = Path("conf/memory/memory.json")
    if _mem_cfg_path.exists():
        try:
            _MEMORY_CONFIG = json.loads(_mem_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    _mem_url = os.environ.get("GX10_MEMORY_URL")
    if _mem_url:
        _MEMORY_CONFIG = {**(_MEMORY_CONFIG or {}), "base_url": _mem_url}
        _MEMORY_CONFIG.setdefault("enabled", True)
        _MEMORY_CONFIG.setdefault("agent_id", os.environ.get("GX10_MEMORY_AGENT", "ironclad"))
    # B3 switches (optional; only apply with configured memory): chunk long feedback losslessly
    # instead of truncating + recency tiebreak in retrieval. Default OFF → today's behaviour.
    _mem_bool = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on")
    _mem_chunk = os.environ.get("GX10_MEMORY_CHUNKING")
    if _mem_chunk not in (None, ""):
        _MEMORY_CONFIG = {**(_MEMORY_CONFIG or {}), "chunk_long_artifacts": _mem_bool(_mem_chunk)}
    _mem_rec = os.environ.get("GX10_MEMORY_RECENCY")
    if _mem_rec not in (None, ""):
        _MEMORY_CONFIG = {**(_MEMORY_CONFIG or {}), "recency_tiebreak": _mem_bool(_mem_rec)}

    # Warm tier config (B0): file (conf/warm/warm.json) OR env (GX10_WARM_URL).
    # Optional — without a url _WARM_CONFIG stays empty → warm tier off (no-op, fail-soft).
    _WARM_CONFIG = {}                         # pure-from-base: re-derive fresh from file + env each reload (S3b)
    _warm_cfg_path = Path("conf/warm/warm.json")
    if _warm_cfg_path.exists():
        try:
            _WARM_CONFIG = json.loads(_warm_cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    _warm_url = os.environ.get("GX10_WARM_URL")
    if _warm_url:
        _WARM_CONFIG = {**(_WARM_CONFIG or {}), "url": _warm_url}
        _WARM_CONFIG.setdefault("enabled", True)

    WATCHER_FEEDBACK_DIR = wa["feedback_dir"]
    _WATCHER_ENABLED     = bool(wa["enabled"])
    RECONCILER_INTERVAL  = float(wa.get("interval", RECONCILER_INTERVAL))

    SPINNER_FRAMES      = ui["spinner_frames"]
    UI_REFRESH_INTERVAL = float(ui["refresh_interval"])
    new_max = int(ui["max_lines"])
    if new_max != _UI_MAX_LINES:
        _UI_MAX_LINES = new_max
        _UI_LINES = deque(_UI_LINES, maxlen=new_max)

    _apply_ace(cfg)                # epic #855 ACE-WIRE (#863): the ALWAYS-ON ACE loop-intelligence core —
                                   # registers the PlaybookStore provider + the post_feedback ACE consumer +
                                   # the background ReflectionWorker, and SUPERSEDES the #602 string lessons
                                   # (_apply_lessons_provider/_apply_lessons_consumer) + Process-SC consumer
                                   # (_apply_process_consumer): those #602 reflection seams are no longer wired.
    _apply_quality_breaker(cfg)    # epic #602 SUB-9: build/clear the opt-in quality breaker
    _apply_verifier(cfg)           # epic #602 SUB-4/2.1: register/clear the opt-in pre_handover Verifier
    _apply_quality_consumer(cfg)   # epic #602 SUB-9/2.7: register/clear the post_handover quality consumer
    _apply_strategy(cfg)           # epic #602 SUB-3/2.4: capture strategy.enabled for the failure recorder


def _apply_lessons_provider(cfg: Dict[str, Any]) -> None:
    """Register (or clear) the project-private lesson distiller per ``lessons.enabled`` (epic #602 SUB-5).

    Runs on every config application (boot + ``/config set`` + ``/switch``). OPT-IN: when on and no provider
    is wired, registers an :class:`~engine.lesson_store.EngineLessonStore` (persisting under
    ``ironclad_home()/lessons``); when off, clears it — but ONLY our own store, so a richer extension that
    registered its own provider is never clobbered (mirrors the dev-process driver's let-the-richer-one-win
    rule). Lazy-imports ``ack`` (never at the gx10 top level) + the store. Fail-soft — a registration hiccup
    must never fail config application; default-off keeps the seam a byte-identical no-op."""
    try:
        from ack import lessons as _lessons              # lazy: never import ack at gx10 top-level (S6b lesson)
        from lesson_store import EngineLessonStore        # bare engine-sibling import (like project_registry)
    except Exception:   # noqa: BLE001 — seam/store unavailable ⇒ leave the no-op default
        return
    try:
        enabled = bool(_cfg_get(cfg, "lessons.enabled"))
        current = _lessons.get_provider()
        if enabled:
            # Pass the RAW cap straight through — the store's _safe_cap coerces it (bad/overflow/None ⇒
            # default), so there is no int() here that could raise and derail registration. The DISABLE
            # branch reads no cap at all, so a malformed cap can never leave a store wired while off.
            raw_cap = _cfg_get(cfg, "lessons.max_per_scope")
            if current is None:                          # don't clobber a foreign provider (richer wins)
                from project_registry import ironclad_home
                _lessons.set_provider(EngineLessonStore(ironclad_home() / "lessons", max_per_scope=raw_cap))
            elif isinstance(current, EngineLessonStore):  # already ours ⇒ apply a live cap change
                current.configure(max_per_scope=raw_cap)
        elif isinstance(current, EngineLessonStore):     # off ⇒ clear only OUR store (raise-free path)
            _lessons.set_provider(None)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


def _lessons_consumer_hook(ctx) -> None:
    """`post_feedback` consumer (#602 2.3 / #804): on a FRESH task completion, report the completion feedback
    as a scoped, actionable loop-lesson via the registered `ack.lessons` provider — the #601 S14-4 write
    re-homed from the inline advance site onto the Hook-Bus (one consistent reflection path, outside the vault
    lock). Gates on the completion result so it never fires on an already-done re-advance or an error, and on a
    registered provider + a non-empty archived feedback file (BYTE-IDENTICAL no-op when none is wired — no file
    read). **Fail-soft** (never raises — a lesson write must never break a turn)."""
    try:
        ctx = ctx or {}
        if not str(ctx.get("result") or "").startswith("OK: pipeline advanced"):
            return                                   # only a fresh completed advance (not already-done / error)
        from ack import lessons as _lessons          # lazy: never import ack at gx10 top-level (S6b lesson)
        if _lessons.get_provider() is None:          # no backend wired ⇒ zero extra work (byte-identical)
            return
        task_id = str(ctx.get("task_id") or ""); agent = str(ctx.get("agent") or "")
        vfb = archive_feedback_dir() / f"{task_id}_{agent}-feedback.md"   # feedback is archived by step 2 of the advance
        _fb = vfb.read_text(encoding="utf-8").strip() if vfb.exists() else ""
        if _fb:
            _lessons.report_lesson(_active_mem_ns(), _fb, {"task_id": task_id, "source": "task_completion"})
    except Exception:   # noqa: BLE001 — advisory: a lesson write must never break a turn
        return


def _apply_lessons_consumer(cfg: Dict[str, Any]) -> None:
    """Register (or unregister) the `post_feedback` Lessons consumer per **provider presence** (#602 2.3 /
    #804) — NOT a config flag: this mirrors the inline write it replaces, which fired whenever a provider was
    registered (a foreign provider stays honoured even with `lessons.enabled` off). Runs AFTER
    `_apply_lessons_provider` in `_apply_config`, so when `lessons.enabled` flips the provider is wired/cleared
    first and the consumer follows. OPT-IN: no provider ⇒ the hook is removed (dispatch O(1) no-op →
    byte-identical default). Idempotent (dedup by identity). Lazy-imports ``ack`` (S6b). Fail-soft."""
    try:
        from ack import hooks as _hooks       # lazy: never import ack at gx10 top-level (S6b lesson)
        from ack import lessons as _lessons
    except Exception:   # noqa: BLE001 — bus/seam unavailable ⇒ leave the no-op default
        return
    try:
        if _lessons.get_provider() is not None:
            _hooks.register_hook("post_feedback", _lessons_consumer_hook)
        else:
            _hooks.unregister_hook("post_feedback", _lessons_consumer_hook)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


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
                        api_key=(os.environ.get(conn.get("api_key_env", "GX10_API_KEY")) or "not-needed"))
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
    """The persisted exactly-once record (the set of dev-loop units already submitted), under the install
    home. ``None`` if the home can't resolve."""
    try:
        from project_registry import ironclad_home   # bare engine-sibling import (like project_registry)
        return ironclad_home() / "ace_devscan.json"
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
    """The persisted exactly-once record (fork keys already dispatched to MPR), under the install home.
    Separate file from the dev-scan units set so neither save clobbers the other. ``None`` if unresolved."""
    try:
        from project_registry import ironclad_home   # bare engine-sibling import
        return ironclad_home() / "ace_forkscan.json"
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
    """`/fork [unit]` — the operator-facing surface for the M5 MPR-at-fork proposals (#903). With no arg it
    lists the units that currently have a recorded architecture-decision matrix (or renders the single pending
    one); `/fork <unit>` renders that unit's full recommendation-only matrix. This is the production caller of
    the M5-3 propose OUTPUT leg: at an architecture fork, the operator sees the MPR proposal here and decides
    (ACE then learns the choice, M5-4). Read-only; fail-soft."""
    arg = (arg or "").strip().lstrip("#").strip()
    try:
        from playbook_store import list_fork_proposals   # bare engine-sibling import
        from project_registry import ironclad_home
        home = ironclad_home()
    except Exception:   # noqa: BLE001
        return "fork: proposal store unavailable"
    if arg:
        return _ace_fork_proposal_for(arg) or f"No MPR fork proposal recorded for #{arg}."
    units = list_fork_proposals(home)
    if not units:
        return ("No pending MPR fork proposals. When an architecture fork is declared and the gate "
                "`ace.fork_mpr.enabled` is on, its decision-matrix appears here as a recommendation.")
    if len(units) == 1:
        return _ace_fork_proposal_for(units[0]) or f"No MPR fork proposal recorded for #{units[0]}."
    return ("\n".join([f"{len(units)} pending MPR fork proposal(s) — `/fork <unit>` for the full matrix:"]
                      + [f"  - #{u}" for u in units]))


_ACE_USAGE = "usage: /ace warmup --ledger <path>  |  /ace eval --ledger <path>"


def _ace_command(arg: str) -> str:
    """ACE ops over a dev-loop ledger (read as plain data — boundary-clean, no private import; off the hot
    path, opt-in, fail-soft). `/ace warmup --ledger <path>` (#915) offline warm-STARTs the active scope's
    playbook from the ledger's historical trajectories. `/ace eval --ledger <path>` (#918) runs the efficiency
    DIAGNOSTIC — compares ACE vs full-rewrite/evolutionary over those trajectories and reports the paper's
    J-001/J-002 verdict (measurement only; no playbook is mutated)."""
    parts = (arg or "").split()
    sub = parts[0] if parts else ""
    if sub not in ("warmup", "eval"):
        return _ACE_USAGE
    ledger = ""
    if "--ledger" in parts:
        i = parts.index("--ledger")
        ledger = parts[i + 1] if i + 1 < len(parts) else ""
    if not ledger:
        return f"usage: /ace {sub} --ledger <path>"
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
            return (f"ace warmup: replayed {report.get('samples_seen', 0)} trajectory record(s) into the playbook "
                    f"— +{report.get('added', 0)} bullet(s), {report.get('pruned', 0)} pruned")
        # sub == "eval" — the efficiency diagnostic (J-001/J-002); no playbook mutated
        rep = store.benchmark(trajectories)
        if rep.get("skipped"):
            return "ace eval: skipped — no orchestrator model reachable"
        ace, fr, evo = rep["ace"], rep["full_rewrite"], rep["evolutionary"]
        j1 = "PASS" if rep.get("no_full_rewrite") else "FAIL"
        j2 = "PASS" if rep.get("rollout_target_met") else "FAIL"
        red = rep.get("rollout_reduction_vs_evolutionary", 0.0)
        return (f"ace eval (over {len(trajectories)} trajectories): "
                f"ACE={ace.rollouts} rollouts / {ace.full_rewrites} full-rewrites / {ace.llm_merges} LLM-merges; "
                f"full-rewrite={fr.rollouts}; evolutionary={evo.rollouts}. "
                f"rollout-reduction vs evolutionary={red:.0%} (J-002 >50%: {j2}); no-full-rewrite (J-001): {j1}")
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
    UN-committed (retried next scan). The in-flight guard is always cleared. Never raises."""
    key = item.get("key") if isinstance(item, dict) else None
    try:
        if isinstance(item, dict) and item.get("signal") is not None:
            _ace_fork_mpr_run(item.get("signal"), item.get("scope", ""))
            _ace_mark_fork_done(key)          # ran to completion ⇒ commit exactly-once (retry only on crash/drop)
    except Exception:   # noqa: BLE001 — a crash leaves the key un-committed so the next scan retries
        pass
    finally:
        try:
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


def _apply_ace(cfg: Dict[str, Any]) -> None:
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
    except Exception:   # noqa: BLE001 — seam/store unavailable ⇒ leave the (no-ACE) state untouched
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
        _ACE_FORK_MPR = bool((ace.get("fork_mpr") or {}).get("enabled", False))
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
        # Wire the consumer iff OUR store owns the provider; retire the #602 lesson/process consumers either way.
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
        _hooks.unregister_hook("post_feedback", _lessons_consumer_hook)    # superseded (#804)
        _hooks.unregister_hook("post_feedback", _process_consumer_hook)    # superseded (#803)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
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


def _apply_quality_breaker(cfg: Dict[str, Any]) -> None:
    """Build (or clear) the SEPARATE quality breaker per ``quality.enabled`` (epic #602 SUB-9). Runs on every
    config application (boot + ``/config set`` + ``/switch``). OPT-IN: when on and none exists, build a
    ``QualityBreaker`` from the config (keeping an already-built one so its accumulated streak survives a
    re-apply); when off, clear it. Lazy-imports ``ack`` (never at the gx10 top level). **Fail-soft** — a
    hiccup never breaks config application; default-off keeps it a byte-identical no-op."""
    global _QUALITY_BREAKER
    try:
        enabled = bool(_cfg_get(cfg, "quality.enabled"))
        if not enabled:
            _QUALITY_BREAKER = None
            return
        if _QUALITY_BREAKER is None:
            from ack.quality import QualityBreaker   # lazy: never import ack at gx10 top-level (S6b lesson)
            q = (_cfg_get(cfg, "quality") or {}) if isinstance(_cfg_get(cfg, "quality"), dict) else {}
            # `.get(key, default)` (not _cfg_get) so a PARTIAL quality block falls back to the code defaults
            # rather than passing None (which would coerce threshold to a never-trip 0.0).
            _QUALITY_BREAKER = QualityBreaker(
                threshold=q.get("threshold", 0.5),
                min_consecutive=q.get("min_consecutive", 3),
                window=q.get("window", 20),
            )
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


def _quality_breaker():
    """The process-global quality breaker (#602 SUB-9), or ``None`` when ``quality.enabled`` is off (the
    default → no-op byte-identical). Consumers feed it verifier scores via ``.record(score)`` and surface
    ``.tripped`` / ``.snapshot()`` — advisory only."""
    return _QUALITY_BREAKER


# ─── epic #602 SUB-4 / 2.1: mark-only Verifier on the dev-task pipeline (pre_handover) ─────────────
_LAST_VERDICT = None
_VERIFY_GROUNDING_THRESHOLD = 0.5   # captured at config-apply time (#809); the verifier reads this, not _EFFECTIVE_CFG


def _set_last_verdict(v) -> None:
    global _LAST_VERDICT
    _LAST_VERDICT = v


def _last_verdict():
    """The most recent mark-only Verifier :class:`~ack.verify.VerdictResult` (#602 2.1), or ``None`` when the
    Verifier is off / has not run yet. Consumed by the Quality breaker (#602 SUB-9 / 2.7) — advisory only."""
    return _LAST_VERDICT


def _verifier_hook(ctx) -> None:
    """Mark-only Verifier for a staged handover (#602 2.1) — a ``pre_handover`` Hook-Bus subscriber. Evaluates
    the task with deterministic BEHAVIORAL rules over ``task_json`` and (when a memory tier is up) GROUNDING of
    the handover's claims against the cold store, then stores a combined :class:`~ack.verify.VerdictResult` for
    the Quality breaker. **MARK-ONLY** (never gates a handover) and **fail-soft** (never raises; the bus also
    swallows). The opt-in LLM-judge is a SEPARATE explicit activation (it charges the budget ledger) and is not
    run here. Registered only while ``verify.enabled`` (see :func:`_apply_verifier`)."""
    try:
        from ack.verify import verify_rules, verify_grounding, VerdictResult   # lazy: never import ack at top
        td = ctx if isinstance(ctx, dict) else {}
        raw = td.get("task_json")
        if isinstance(raw, dict):
            fields = raw
        elif isinstance(raw, str) and raw.strip():
            try:
                fields = json.loads(raw)
            except Exception:   # noqa: BLE001 — a non-JSON task_json → no field rules; grounding still runs
                fields = {}
        else:
            fields = {}
        if not isinstance(fields, dict):
            fields = {}
        handover_md = str(td.get("handover_md") or "")

        # 8b — which mark-only verifiers run for THIS task's type (`LoopProfile.eval_verifiers`); empty → the
        # DEFAULT rules + grounding set (the operator decision). The async LLM-judge is a SEPARATE opt-in
        # consumer (validated_emit's `strategist` / a future async eval-gate), not run by this sync hook.
        try:
            evs = tuple(_loop_profile(fields.get("type")).eval_verifiers or ())
        except Exception:   # noqa: BLE001 — profile hiccup → the default set
            evs = ()
        run_rules = ("rules" in evs) if evs else True
        run_grounding = ("grounding" in evs) if evs else True

        verdicts = []
        if run_rules:
            # BEHAVIORAL (beyond-schema) quality rules — advisory; the ACK gate already enforces schema validity.
            rules = [
                ("description_substantive", lambda f: len(str((f or {}).get("description", "")).strip()) >= 40),
                ("title_specific",          lambda f: len(str((f or {}).get("title", "")).split()) >= 3),
            ]
            verdicts.append(verify_rules(fields, rules))

        # GROUNDING — only when selected AND a memory tier is up; its OWN try so a memory hiccup drops ONLY
        # grounding and never discards the rules verdict. Capped (12 claims) to bound the sync cold-store lookups.
        if run_grounding:
            try:
                if _MEMORY is not None and _MEMORY.is_available():
                    claims = [ln.strip() for ln in handover_md.splitlines()
                              if len(ln.strip()) >= 30 and not ln.lstrip().startswith("#")][:12]
                    if claims:
                        verdicts.append(verify_grounding(
                            claims,
                            lambda c: bool(_MEMORY.search(c, limit=3)),
                            threshold=_VERIFY_GROUNDING_THRESHOLD,
                        ))
            except Exception:   # noqa: BLE001 — a memory hiccup drops only grounding; the rules verdict survives
                pass

        if verdicts:
            score = sum(v.score for v in verdicts) / len(verdicts)
            passed = all(v.passed for v in verdicts)
            reason = "; ".join(f"{v.verifier} {v.score:.2f}" for v in verdicts)
            _set_last_verdict(VerdictResult(passed, score, reason, "handover"))
    except Exception:   # noqa: BLE001 — mark-only + fail-soft: a Verifier hiccup never breaks a handover
        pass


def _apply_verifier(cfg: Dict[str, Any]) -> None:
    """Register (or unregister) the mark-only Verifier on the ``pre_handover`` Hook-Bus event per
    ``verify.enabled`` (#602 2.1). Runs on every config application (boot + ``/config set`` + ``/switch``).
    OPT-IN: when off the hook is removed (dispatch O(1) no-op → byte-identical); when on it is registered
    (additive + idempotent — dedup by identity, so a re-apply never double-registers, and it never clobbers a
    sibling hook). Lazy-imports ``ack.hooks`` (never at the gx10 top level). Fail-soft — a hiccup never breaks
    config application."""
    try:
        from ack import hooks as _hooks   # lazy: never import ack at gx10 top-level (S6b lesson)
    except Exception:   # noqa: BLE001 — bus unavailable ⇒ leave the no-op default
        return
    global _VERIFY_GROUNDING_THRESHOLD
    try:   # capture the grounding threshold at apply-time (the verifier reads the flag, not _EFFECTIVE_CFG)
        th = _cfg_get(cfg, "verify.grounding_threshold")
        _VERIFY_GROUNDING_THRESHOLD = float(th) if isinstance(th, (int, float)) and not isinstance(th, bool) else 0.5
    except Exception:   # noqa: BLE001 — bad value → the safe default
        _VERIFY_GROUNDING_THRESHOLD = 0.5
    try:
        if bool(_cfg_get(cfg, "verify.enabled")):
            _hooks.register_hook("pre_handover", _verifier_hook)
        else:
            _hooks.unregister_hook("pre_handover", _verifier_hook)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


# ─── epic #602 SUB-9 / 2.7: Quality breaker CONSUMER — feed Verifier scores, surface a trip ────────
_QUALITY_TRIPPED = None


def _quality_tripped():
    """The latest :class:`~ack.quality.QualitySnapshot` from a tripped quality breaker (#602 2.7), or ``None``
    when untripped / off. Advisory + observability only — a trip NEVER hard-aborts a turn."""
    return _QUALITY_TRIPPED


def _quality_consumer_hook(ctx) -> None:
    """`post_handover` consumer (#602 2.7): feed the latest mark-only Verifier score (`_last_verdict()`) into
    the quality breaker and SURFACE a sustained-degradation trip — advisory (escalate/surface), NEVER a gate.
    No-op when the breaker is off (`quality.enabled`) or no verdict is present; **fail-soft** (never raises)."""
    global _QUALITY_TRIPPED
    try:
        qb = _quality_breaker()
        v = _last_verdict()
        if qb is None or v is None:
            return
        was_tripped = _QUALITY_TRIPPED is not None
        tripped = qb.record(v.score)
        _set_last_verdict(None)   # feed-once: consume the verdict so a later handover can't re-feed it stale
        if tripped:
            _QUALITY_TRIPPED = qb.snapshot()
            if not was_tripped:   # surface only on the not-tripped → tripped transition (no re-print while latched)
                _ui_print(col(f"  [quality] output-quality breaker tripped — {_QUALITY_TRIPPED.reason}", C.YELLOW))
        else:
            _QUALITY_TRIPPED = None
    except Exception:   # noqa: BLE001 — mark-only + fail-soft: a quality hiccup never breaks a handover
        return


def _apply_quality_consumer(cfg: Dict[str, Any]) -> None:
    """Register (or unregister) the `post_handover` quality consumer per ``quality.enabled`` (#602 2.7). When
    on, the breaker is fed the Verifier scores and a trip is surfaced; when off the hook is removed (dispatch
    O(1) no-op → byte-identical). Idempotent (dedup by identity). Lazy-imports ``ack.hooks`` (S6b). Fail-soft."""
    try:
        from ack import hooks as _hooks   # lazy: never import ack at gx10 top-level (S6b lesson)
    except Exception:   # noqa: BLE001 — bus unavailable ⇒ leave the no-op default
        return
    try:
        if bool(_cfg_get(cfg, "quality.enabled")):
            _hooks.register_hook("post_handover", _quality_consumer_hook)
        else:
            _hooks.unregister_hook("post_handover", _quality_consumer_hook)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


# ─── epic #602 SUB-3 / 2.4: FailureClass at the code-agent failover (the Strategy consumer's input) ───
_LAST_FAILURE_CLASS = None
_STRATEGY_ENABLED = False
_STRATEGY_BUDGET = 3
_FAILURE_ATTEMPTS: "Dict[str, int]" = {}   # per-task code-agent failure counter (#602 2.5); reset on success
_LAST_STRATEGY = None


def _apply_strategy(cfg: Dict[str, Any]) -> None:
    """Capture ``strategy.enabled`` + ``strategy.budget`` at config-application time (#602 2.4/2.5), mirroring
    the other `_apply_*` seams. Runtime (the server feedback path) reads the `_STRATEGY_ENABLED` /
    `_STRATEGY_BUDGET` flags — NOT `_EFFECTIVE_CFG`, which only the config-tree loader sets, not
    `_apply_config`. Default OFF → byte-identical. Fail-soft — never breaks config application."""
    global _STRATEGY_ENABLED, _STRATEGY_BUDGET
    try:
        _STRATEGY_ENABLED = bool(_cfg_get(cfg, "strategy.enabled"))
        b = _cfg_get(cfg, "strategy.budget")
        _STRATEGY_BUDGET = b if isinstance(b, int) and not isinstance(b, bool) and b >= 1 else 3
    except Exception:   # noqa: BLE001 — advisory wiring: default to off
        _STRATEGY_ENABLED = False
        _STRATEGY_BUDGET = 3


def _last_strategy():
    """The most recent :class:`~ack.strategy.Strategy` from the failover consumer (#602 2.5), or ``None``.
    Advisory / observability — never gates."""
    return _LAST_STRATEGY


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
    """#602 2.5 / #806: consult the Strategy Revisor on a code-agent run result and **surface** a
    ``HUMAN_ESCALATION`` when the per-task attempt budget is spent — instead of an endless silent failover.
    Uses the FRESH ``result_cls`` (never a stale ``_last_failure_class``); a successful run RESETS the task's
    attempt counter. **OPT-IN** per ``strategy.enabled`` (default OFF → ``None``, byte-identical). Returns the
    chosen ``StrategyAction`` value (or ``None``). **Fail-soft** — never breaks the feedback path."""
    global _LAST_STRATEGY
    try:
        if not _STRATEGY_ENABLED or not task_id:
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
        if getattr(strat, "escalate", False):
            _ui_print(col(f"  [strategy] human escalation after {n} attempt(s) on {task_id} "
                          f"({result_cls}) → {strat.action.value}", C.YELLOW))
        return strat.action.value
    except Exception:   # noqa: BLE001 — advisory: a strategy hiccup must never break the feedback path
        return None


def _last_failure_class():
    """The shared :class:`~ack.failure_class.FailureClass` of the most recent code-agent run failure (#602
    2.4), or ``None``. Read by the Strategy Revisor consumer (#602 2.5 / #806) — advisory only."""
    return _LAST_FAILURE_CLASS


def _record_failure_class(result_cls):
    """On a code-agent run result, record + return the shared FailureClass (#602 2.4 / #805) so the Strategy
    consumer (2.5) can act on WHY a run failed. **OPT-IN per ``strategy.enabled``** (default OFF → ``None``,
    byte-identical: nothing recorded, no response field). Returns the FailureClass string value, or ``None``
    for ``RESULT_OK`` / an unknown result / when off. **Fail-soft** — classifying a failure must never break
    the feedback path."""
    global _LAST_FAILURE_CLASS
    try:
        if not _STRATEGY_ENABLED:
            return None
        from providers import result_failure_class   # bare engine-sibling import (like project_registry)
        fc = result_failure_class(result_cls)
        if fc is None:
            return None
        _LAST_FAILURE_CLASS = fc
        return fc.value
    except Exception:   # noqa: BLE001 — advisory: a classification hiccup must never break the feedback path
        return None


# ─── epic #602 SUB-6: Process-Level Self-Correction (post_feedback → pre_turn) ─────────────────────
def _concrete_lesson_provider():
    """The registered lesson provider IFF it exposes the TYPED ``record``/``by_category`` surface; ``None``
    otherwise. Process-SC reads/writes TYPED process-lessons through this surface — the string-only
    ``ack.lessons`` seam can't round-trip them. DUCK-TYPED (#863): since ACE supersedes the #602
    ``EngineLessonStore`` with the :class:`~engine.playbook_store.PlaybookStore` (which implements the SAME
    typed surface over the bullet playbook), the check is a capability probe, not an ``isinstance`` — so
    Process-SC keeps working against whichever concrete backend is wired and NEVER silently breaks. Never raises."""
    try:
        from ack import lessons as _lessons     # lazy: never import ack at gx10 top-level (S6b lesson)
        p = _lessons.get_provider()
        return p if (callable(getattr(p, "record", None))
                     and callable(getattr(p, "by_category", None))) else None
    except Exception:   # noqa: BLE001 — advisory: a lookup hiccup → no concrete provider
        return None


def _record_process_lesson(existing, agent: str = "") -> None:
    """At task completion, distill a TYPED process-lesson from the workflow signal and store it via the
    concrete provider (#602 SUB-6). OPT-IN: a no-op when ``process.enabled`` is off OR no concrete
    EngineLessonStore is registered (byte-identical). Fail-soft — never raises, never breaks a turn."""
    try:
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        if not bool(_cfg_get(cfg, "process.enabled")):
            return
        provider = _concrete_lesson_provider()
        if provider is None:
            return
        scope = _active_mem_ns()
        if not scope:    # no project bound (base partition) → no-op, byte-identical
            return
        from ack.process import ProcessSignal, ProcessLessonKind, distill_process_lesson
        from lesson_store import LessonCategory
        task_type = str((existing or {}).get("type") or "") if isinstance(existing, dict) else ""
        lesson = distill_process_lesson(ProcessSignal(task_type=task_type, succeeded=True, agent=str(agent or "")))
        if lesson is None:
            return
        cat = (LessonCategory.BEST_KNOWN_PATH if lesson.kind == ProcessLessonKind.WORKING_PATH
               else LessonCategory.LAST_FAILURE_REASON)
        provider.record(scope, lesson.text, cat, {"source": "process_sc", "kind": lesson.kind.value})
    except Exception:   # noqa: BLE001 — advisory: process-SC must never break a turn
        return


def _process_consumer_hook(ctx) -> None:
    """`post_feedback` consumer (#602 2.2 / #803): on a FRESH task completion, distill + store a TYPED
    process-lesson via the concrete provider — the Process-SC write re-homed from the inline advance site onto
    the Hook-Bus (one consistent reflection path, outside the vault lock). Gates on the completion result so it
    never fires on an already-done re-advance or an error; `_record_process_lesson` keeps its own
    `process.enabled` + concrete-provider + bound-scope gates (byte-identical no-op by default). **Fail-soft**
    (never raises — a process-SC hiccup must not break a turn)."""
    try:
        ctx = ctx or {}
        if not str(ctx.get("result") or "").startswith("OK: pipeline advanced"):
            return                                   # only a fresh completed advance (not already-done / error)
        task_id = str(ctx.get("task_id") or "")
        existing = _store().get(task_id) if task_id else None
        _record_process_lesson(existing, str(ctx.get("agent") or ""))
    except Exception:   # noqa: BLE001 — advisory: process-SC must never break a turn
        return


def _apply_process_consumer(cfg: Dict[str, Any]) -> None:
    """Register (or unregister) the `post_feedback` Process-SC consumer per ``process.enabled`` (#602 2.2 /
    #803), mirroring the other `_apply_*` consumer seams. OPT-IN: when off the hook is removed (dispatch O(1)
    no-op → byte-identical); when on it is registered (additive + idempotent — dedup by identity, never
    clobbers a sibling). Lazy-imports ``ack.hooks`` (never at the gx10 top level, S6b). Fail-soft — a wiring
    hiccup never breaks config application."""
    try:
        from ack import hooks as _hooks   # lazy: never import ack at gx10 top-level (S6b lesson)
    except Exception:   # noqa: BLE001 — bus unavailable ⇒ leave the no-op default
        return
    try:
        if bool(_cfg_get(cfg, "process.enabled")):
            _hooks.register_hook("post_feedback", _process_consumer_hook)
        else:
            _hooks.unregister_hook("post_feedback", _process_consumer_hook)
    except Exception:   # noqa: BLE001 — advisory wiring: never break config application
        return


def _process_hint() -> str:
    """A pre-turn hint of known working approaches (process-lessons) for the active scope (#602 SUB-6), or
    ``""`` (byte-identical) when ``process.enabled`` is off / no concrete provider / none recorded. Fail-soft."""
    try:
        cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
        if not bool(_cfg_get(cfg, "process.enabled")):
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
