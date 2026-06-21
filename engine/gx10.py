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
import argparse
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
# (code defaults < config file < env < CLI). At startup
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
DEFAULT_WORKDIR  = "."           # WORKDIR: work location (CWD behaviour as before)
CODE_ROOT        = ""            # optional code root for the handover path guard
                                 # (vessel-specific, e.g. a service subfolder
                                 # in the repo); empty = check repo root only. Via paths.code_root.
MAX_ITERATIONS   = 20
MAX_CTX_CHARS    = 80_000        # high-water: trimming starts only here (char-based; derived from MAX_MODEL_LEN under TOKEN_BUDGET)
TRIM_TARGET_CHARS = 48_000       # PERF-06: low-water after the trim (60 %)
MAX_TOKENS       = 8192          # PERF-10: was 4096 → handover truncation
# MEM-9 / §3-mechanism 3 — token-accurate budgeting: couple the trim working set to the MODEL WINDOW
# instead of fixed chars. When TOKEN_BUDGET=True, _apply_config derives MAX_CTX_CHARS/
# TRIM_TARGET_CHARS from MAX_MODEL_LEN (minus reserve for output+RAG+summary, chars/4 estimate,
# 10 % headroom) → the working set scales with the window but never overflows it. OFF = fixed
# char thresholds as today (then context.max_ctx_chars / GX10_MAX_CTX_CHARS apply).
MAX_MODEL_LEN    = 32768         # hard per-request token window (vLLM --max-model-len); GX10_MAX_MODEL_LEN/IRONCLAD_MAX_MODEL_LEN
TOKEN_BUDGET     = True          # default ON (06-18); off via context.token_budget=false / GX10_TOKEN_BUDGET=0
CHARS_PER_TOKEN  = 4             # rough token estimate (chars/token)
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
RAG_MAX_TOKENS  = 1024            # token budget of the injected block (chars/4 estimate)
_RAG_MARKER     = "## Relevant context (retrieved)"
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
_TURN_DID_ADVANCE        = False   # guard: True after advance_pipeline in the running turn. Prevents
                                   # the model from immediately pushing a stage_handover in the SAME
                                   # turn (without operator input) ("auto-plan"), as long as AUTOPILOT_AUTOPLAN
                                   # is off. Reset on every new operator turn (run()).
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


def state_root() -> Path:
    """Root of the hidden engine machinery (initiative-independent): session.json, the local
    warm cache (memory/), config.json/active. Relative to WORKDIR (after chdir), overridable via
    cfg["paths"]["state_root"] (default ``.ironclad``); absolute overrides are taken
    unchanged. Boundary clean — no private literal."""
    return Path(STATE_ROOT)


def session_path() -> Path:
    """Path of the session file: ``state_root()/SESSION_FILE``. An absolutely configured
    SESSION_FILE is used unchanged (backward compatibility)."""
    p = Path(SESSION_FILE)
    return p if p.is_absolute() else state_root() / p


def vault_root() -> Path:
    """Visible knowledge root (initiative-centric): ``vault/<slug>/`` per initiative. Relative to
    WORKDIR (after chdir), overridable via cfg["paths"]["vault_root"] (default ``vault``)."""
    return Path(VAULT_ROOT)


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

_NO_ACTIVE_MSG = ("kein aktives Initiative — `/initiative new <name> --type mpr|software` "
                  "(oder `/initiative use <slug>`) zuerst")


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
            f"_Initiative (type: {self.type}). Artefakte unter `{vault_root().as_posix()}/{self.slug}/`. "
            "INDEX.md wird automatisch gepflegt (reconcile)._\n"
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
        raise ValueError(f"unbekannter Initiative-Typ {type!r} — erlaubt: {', '.join(INITIATIVE_TYPES)}")
    title = (name or "").strip()
    if not title:
        raise ValueError("Initiative braucht einen Namen")
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
    _reconcile_active_soft()   # C2: seed INDEX.md immediately → navigable from the start
    return v


def initiative_use(slug: str) -> Initiative:
    """Sets an existing initiative active. Unknown slug → ValueError."""
    v = initiative_get((slug or "").strip())
    if v is None:
        raise ValueError(f"kein Initiative {slug!r} unter {vault_root().as_posix()}/ — "
                         "`/initiative new <name> --type mpr|software` zuerst")
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
        raise RuntimeError(_NO_ACTIVE_MSG)
    return v.path


def _mpr_blocks_tasks() -> Optional[str]:
    """Issue #15 — the task pipeline (tasks/handovers/feedback) is software-only. If the ACTIVE
    initiative is type mpr (reasoning-only), return a clear refusal message; otherwise None. The type
    becomes a real contract instead of only choosing the seed skeleton."""
    v = initiative_active()
    if v is not None and v.type == "mpr":
        return (f"Task-Pipeline (tasks/handovers/feedback) nur in einem `--type software`-Initiative — "
                f"aktives Initiative '{v.slug}' ist type mpr (reasoning-only). "
                f"`/initiative new <name> --type software` oder `/initiative use <slug>`.")
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
# Doc categories (first path segment) that get a "Verwandt" (related) block — curated knowledge,
# NOT the auto-generated MPR runs/ and not the meta.md.
_LINK_CATEGORIES  = {"decisions", "proposals", "reviews", "(root)"}


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


def _vault_docs(vdir: Path) -> List[Dict[str, Any]]:
    """Indexable docs under the initiative (excluding INDEX.md and the hidden .work/), with metadata."""
    out: List[Dict[str, Any]] = []
    for p in sorted(vdir.rglob("*.md")):
        rel = p.relative_to(vdir)
        if p.name == "INDEX.md" or (rel.parts and rel.parts[0] == WORKFLOW_DIR):
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
            "text": text,
        })
    return out


def _index_block(slug: str, docs: List[Dict[str, Any]]) -> str:
    lines = [_INDEX_AUTO_START,
             f"_Automatisch gepflegt (reconcile_vault, LLM-frei) — {len(docs)} Dokument(e)._", ""]
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


def reconcile_vault(slug: str, *, links: bool = True) -> str:
    """Maintains INDEX.md (always) + optionally the "Verwandt (auto)" (related) blocks (``links=True``) of an initiative.
    Deterministic, idempotent, LLM-free. ``links=False`` (auto-trigger) only keeps the index fresh and
    does not touch doc bodies (no conflict with an open editor)."""
    vdir = vault_root() / slug
    if not (vdir / "meta.md").is_file():
        return f"kein Initiative {slug!r} unter {vault_root().as_posix()}/"
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

    linked = 0
    if links:
        for d in docs:
            if d["category"] not in _LINK_CATEGORIES or d["rel"].name == "meta.md":
                continue
            related = _related_docs(d, docs)
            if related:
                items = sorted(related, key=lambda x: x["title"].lower())
                body = "\n".join([_LINKS_AUTO_START, "", "## Verwandt (auto)",
                                  *[f"- [[{o['stem']}|{o['title']}]]" for o in items],
                                  "", _LINKS_AUTO_END])
                new = _set_managed_block(d["text"], _LINKS_AUTO_START, _LINKS_AUTO_END, body)
            else:   # no related docs → remove any existing block (tidy)
                new = _set_managed_block(d["text"], _LINKS_AUTO_START, _LINKS_AUTO_END, None)
            if new != d["text"]:
                d["path"].write_text(new, encoding="utf-8", newline="\n")
                linked += 1
    suffix = f", {linked} Related-Block/Blöcke aktualisiert" if links else " (nur Index)"
    return f"{slug}: {len(docs)} Dokument(e) indiziert{suffix}"


def _reconcile_active_soft(*, links: bool = False) -> None:
    """Auto-reconcile of the active initiative after a write (fail-soft, never raises).
    Default ``links=False`` → only keep INDEX.md fresh, doc bodies untouched."""
    try:
        s = active_slug()
        if s:
            reconcile_vault(s, links=links)
    except Exception:   # noqa: BLE001 — reconcile must never make a write fail
        pass


# Memory layer — module-level singleton, initialized in GX10.__init__()
_MEMORY_CONFIG: Dict[str, Any] = {}
_MEMORY: Optional[Any] = None
# Warm tier (Valkey, B0) — optional cache-aside layer in front of the cold vector store (B2 retrieval)
# + session state. Singleton, initialized in GX10.__init__(); without a url it stays None (no-op).
_WARM_CONFIG: Dict[str, Any] = {}
_WARM: Optional[Any] = None
#: Phase-e reasoning fan-out (engine/workers.py). Set by the server; stays None in the
#: monolithic CLI, so the parallel tool is offered only where the governed workers exist.
_WORKERS: Optional[Any] = None
#: P0 provider router — set at server boot beside _WORKERS (server.py). None or inactive ⇒
#: parallel_reason uses today's _WORKERS.fanout path, byte-identically.
_DISPATCHER: Optional[Any] = None
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

_status = {"thinking": False, "label": "bereit"}

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
            ("",                 f"  {frame}  {_status['label']}...   Strg+C = abbrechen   "),
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
            },
            "required": ["items"],
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

def _effective_tools() -> List[Dict[str, Any]]:
    """Tool list depending on the mode — onboarding tools only when active."""
    # Offer the tool only when memory is CONFIGURED (not just the module present) —
    # otherwise the tool would be offered even though every call would return "unavailable".
    mem = [MEMORY_TOOL, DEEP_MEMORY_TOOL] if _MEMORY is not None else []
    par = [PARALLEL_TOOL] if _WORKERS is not None else []
    plug = [t["schema"] for t in _PLUGIN_TOOLS.values()]
    skl = [USE_SKILL_TOOL] if _PLAYBOOKS else []
    prm = [USE_PROMPT_TOOL] if _PROMPTS else []
    return TOOLS + mem + par + plug + skl + prm + (ONBOARDING_TOOLS if ONBOARDING_MODE else [])

# ─── Macro tool: deterministic pipeline (HV-A) ─────────────
_TASK_ID_RE = re.compile(rf"^{re.escape(TASK_PREFIX)}-[A-Za-z0-9_]+$")
_IDLE_ACTIVE = "# Workflow — idle\n\nKein aktiver Handover.\n"

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


def _advance_pipeline(task_id: str, agent: str, next_task_id: Optional[str] = None) -> str:
    """Advances the 'done' pipeline for ONE task deterministically.
    Status transitions go through the TaskStore (directory = truth),
    active.md is projected. Fail-closed: no completion without a feedback
    file. Touches neither code/ nor the audit chain."""
    if not task_id or not _TASK_ID_RE.match(task_id):
        return f"ERROR: invalid task_id: {task_id!r} (expected e.g. KGC-315)"
    agent = (agent or "").upper()
    if agent == "KIMI":
        agent = "SONNET"                      # Kimi → Sonnet (legacy alias, 2026-06-15)
    if agent not in ("OPUS", "SONNET"):
        return f"ERROR: agent must be OPUS or SONNET (was: {agent!r})"
    if next_task_id and not _TASK_ID_RE.match(next_task_id):
        return f"ERROR: invalid next_task_id: {next_task_id!r}"

    if artifact_root_soft() is None:
        return f"ERROR: {_NO_ACTIVE_MSG}"     # B3: fail-closed — artefacts route to the active initiative
    _mpr = _mpr_blocks_tasks()
    if _mpr:
        return f"ERROR: {_mpr}"               # #15: mpr initiative is reasoning-only

    store = _store()
    log: List[str] = []

    # Idempotency gate: task already done → no re-advance needed
    existing = store.get(task_id)
    if existing and existing.get("status") == "done":
        return (f"OK: task {task_id} is already done — no re-advance needed. "
                f"Feedback liegt in {(archive_feedback_dir() / f'{task_id}_{agent}-feedback.md').as_posix()}")

    # 0. Fail-closed gate: feedback MUST exist
    # Primary: <initiative>/.work/feedback/ (reconciler inbox)
    # Fallback: <initiative>/.work/archive/feedback/ (already archived by the reconciler)
    fb = feedback_dir() / f"{task_id}_{agent}-feedback.md"
    if not fb.exists():
        fb_arch = archive_feedback_dir() / f"{task_id}_{agent}-feedback.md"
        if fb_arch.exists():
            fb = fb_arch
            log.append(f"feedback aus Archiv gelesen: {fb_arch}")
        else:
            return (f"ERROR: Feedback fehlt: {fb.as_posix()} "
                    f"und {fb_arch.as_posix()} "
                    f"— Task gilt als NICHT abgeschlossen. Pipeline nicht weitergeschaltet.")
    log.append(f"feedback found: {fb}")

    try:
        # 1. archive the current active.md handover (before the switch)
        active  = active_md_path()
        archive = archive_handovers_dir() / f"{task_id}_{agent}.md"
        if active.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(active), str(archive))
            log.append(f"active.md archiviert → {archive}")
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
                log.append(f"feedback archiviert → {vfb} (Original entfernt)")
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
        log.append("active.md projiziert")

        # 7. Optional: terminate the associated autopilot session (task is done)
        if AUTOPILOT_TERMINATE_ON_ADVANCE:
            _terminate_autopilot(task_id)
            log.append("autopilot-session beendet (falls active)")

        # 8. regenerate the vault projections DETERMINISTICALLY — mechanically, NOT
        #    dependent on GX10's step-6 discipline (prevents a stale backlog →
        #    otherwise autoplan plans from outdated data → duplicate). Idempotent +
        #    fail-soft: a script error does NOT abort the already-completed advance.
        #    UTF-8 env so emoji output doesn't crash on cp1252 stdout.
        _env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # update_capability_tracking.py regenerates ALL capability domains
        # (n8n-parity, frontend-ux-parity, …) generically from their *-gap-tracking.md.
        for _script in ("update_capability_tracking.py", "update_workflow_mocs.py",
                        "update_masterplan_status.py"):
            try:
                _r = subprocess.run([sys.executable, f"scripts/{_script}"],
                                    cwd=".", capture_output=True, text=True,
                                    timeout=60, env=_env)
                log.append(f"regen {_script}: {'ok' if _r.returncode == 0 else 'WARN rc=' + str(_r.returncode)}")
            except Exception as _e:
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
    """Publishes a NEW task+handover in ONE step via the
    TaskStore: ID assignment, created_at stamp, schema and topic dedup are
    deterministic (no AI involvement). On a topic duplicate, fail-closed —
    nothing is created, the existing task is named."""
    agent = (agent or "").upper()
    if agent == "KIMI":
        agent = "SONNET"                      # Kimi → Sonnet (legacy alias, 2026-06-15)
    if agent not in ("OPUS", "SONNET"):
        return f"ERROR: agent must be OPUS or SONNET (was: {agent!r})"
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
                return ("ERROR: task_json verletzt den ACK-Vertrag (nichts angelegt):\n"
                        + ack_err + "\n→ Felder korrigieren und stage_handover erneut aufrufen.")
            # Store: dedup + ID + created_at + schema, writes the pending JSON
            try:
                task = store.create(fields, force=bool(force))
            except DuplicateTaskError as e:
                return (f"ERROR: duplicate — a task on the same topic already exists as "
                        f"{e.existing_id}. KEIN neuer Task angelegt. Bestehenden Task "
                        f"nutzen oder (nur auf Anweisung) force=true setzen.")
            except ValueError as e:
                return f"ERROR: {e} — no task created."
            tid = task["id"]
            log.append(f"task created: {tid} (pending, created_at={task['created_at']})")
            ho_md = _normalize_handover_id(handover_md, tid)
            # append memory context from past patterns (fail-soft)
            if _MEMORY is not None and _MEMORY.is_available():
                try:
                    mem_ctx = _MEMORY.get_context(
                        fields.get("type", ""),
                        fields.get("title", task.get("title", "")),
                    )
                    if mem_ctx:
                        ho_md = ho_md.rstrip() + "\n\n---\n\n" + mem_ctx
                        log.append("Memory-Kontext injiziert")
                except Exception:
                    pass
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, ho_md)
            log.append(f"handover geschrieben: {ho} ({len(ho_md)} Zeichen)")
        else:
            # Pure handover without task JSON — requires a valid task_id.
            if not task_id or not _TASK_ID_RE.match(task_id):
                return f"ERROR: without task_json a valid task_id is required (was: {task_id!r})"
            tid = task_id
            ho = handovers_dir() / f"{tid}_{agent}.md"
            _atomic_write(ho, handover_md)
            log.append(f"handover geschrieben: {ho} ({len(handover_md)} Zeichen)")

        if set_active:
            store.project_active()
            log.append("active.md projected (= newest non-done handover)")

    except Exception as e:
        return f"ERROR: stage_handover fehlgeschlagen: {e}\nBisher:\n" + "\n".join(f"  - {l}" for l in log)

    result = f"OK: Handover {tid} ({agent}) bereitgestellt\n" + "\n".join(f"  - {l}" for l in log)
    # Path guard only for code tasks: with type=documentation (memory seed, docs)
    # the agent builds no code → no duplication risk, the check would only be noise.
    bad = [] if task_type == "documentation" else _handover_path_warnings(handover_md)
    if bad:
        result += (
            "\n\n⚠ PFAD-CHECK — diese code-Pfade im Handover existieren NICHT "
            "(weder relativ zum Repo-Root noch unter CODE_ROOT):\n"
            + "\n".join(f"    - {p}" for p in bad[:10])
            + "\n  → Referenzieren sie BESTEHENDEN Code, sind sie FALSCH — "
              "korrigiere sie, sonst baut der Agent neu statt zu erweitern (Dublette). "
              "Neu anzulegende Dateien sind ok."
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
        super().__init__(f"Duplikat zu {existing_id}")
        self.existing_id = existing_id


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
            raise RuntimeError(_NO_ACTIVE_MSG)
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


def _discover_tools_into(root: str) -> int:
    """Discover typed ``CASE``+``run`` skills under *root* and ADD them to _PLUGIN_TOOLS
    (no clear — additive). Returns how many this root contributed. Fail-soft."""
    try:
        from ack.registry import Registry, derive_tool_schema
    except Exception as e:  # noqa: BLE001 — no registry → no tools, never fatal
        _ui_print(col(f"  [skills] registry unavailable: {e!r}", C.YELLOW))
        return 0
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
        if name in _PLUGIN_TOOLS:        # tool names must be unique — otherwise silent shadowing
            _ui_print(col(f"  [skills] duplicate tool name {name!r} — first kept, rest skipped", C.YELLOW))
            continue
        _PLUGIN_TOOLS[name] = {
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


def _discover_playbooks_into(root: str) -> int:
    """Discover ``SKILL.md`` playbooks under *root* and ADD them to _PLAYBOOKS (no clear —
    additive; first capability wins). Returns how many this root contributed. Fail-soft."""
    try:
        from ack.registry import Registry
        found = Registry.discover_playbooks(root)
    except Exception as e:  # noqa: BLE001 — no registry/discovery → no playbooks, never fatal
        _ui_print(col(f"  [skills] playbook discovery failed in {root!r}: {e!r}", C.YELLOW))
        return 0
    n = 0
    for pb in found:
        if pb.capability in _PLAYBOOKS:
            continue
        _PLAYBOOKS[pb.capability] = pb
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


def _discover_prompts_into(root: str) -> int:
    """Discover ``kind: prompt`` items under *root* and ADD them to _PROMPTS (no clear —
    additive; first capability wins). Returns how many this root contributed. Fail-soft."""
    try:
        from ack.prompt import discover_prompts
        found = discover_prompts(root)
    except Exception as e:  # noqa: BLE001 — no discovery → no prompts, never fatal
        _ui_print(col(f"  [skills] prompt discovery failed in {root!r}: {e!r}", C.YELLOW))
        return 0
    n = 0
    for p in found:
        if p.capability in _PROMPTS:
            continue
        _PROMPTS[p.capability] = p
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
    """Startup loader (ADR-0002 #114): **always** load core built-ins from ``_BUILTIN_DIR``,
    then **additively** load 3rd-party/internal skills — from *plugins_dir* (a dir, dev) **and**
    from packaged plugins advertised via the ``ironclad.plugins`` entry-point group (ADR-0004
    #136). Clears once. Returns (n_tools, n_playbooks, n_prompts)."""
    _PLUGIN_TOOLS.clear()
    _PLAYBOOKS.clear()
    _PROMPTS.clear()
    ep_roots = _entrypoint_plugin_roots()
    roots = [str(_BUILTIN_DIR)] + ([plugins_dir] if plugins_dir else []) + ep_roots
    for root in roots:
        _discover_tools_into(root)
        _discover_playbooks_into(root)
        _discover_prompts_into(root)
    if _PLUGIN_TOOLS or _PLAYBOOKS or _PROMPTS:
        srcs = "built-ins" + (f" + {plugins_dir}" if plugins_dir else "") + (f" + {len(ep_roots)} entry-point(s)" if ep_roots else "")
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
    if warm is not None:
        try:
            hits = warm.cache_get(query)
        except Exception:  # noqa: BLE001
            hits = None
    if hits is None:
        try:
            hits = _MEMORY.search(query, top_k)
        except Exception:  # noqa: BLE001
            return []
        if warm is not None and hits:
            try:
                warm.cache_set(query, hits)
            except Exception:  # noqa: BLE001
                pass
    return list(hits or [])


def _rag_block(hits: List[str], budget_tokens: int, in_window: str = "") -> str:
    """Format hits into a token-budgeted, deduped ``## Relevant context (retrieved)`` block (or
    ""). Dedups within the block and against *in_window* (already-visible context; "" ⇒ skip)."""
    budget_chars = max(0, budget_tokens) * 4   # rough chars/4 token estimate
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
        if used + len(line) + 1 > budget_chars:
            break
        lines.append(line)
        seen.add(key)
        used += len(line) + 1
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
        s = _WARM.get_session(WARM_SESSION_ID, "summary")
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
                       summary_tokens: int, chars_per_token: int = CHARS_PER_TOKEN) -> tuple:
    """MEM-9 / §3-Mechanismus 3: trim watermark (in CHARS, since _trim_context measures chars)
    derived from the model window minus the reserves it must leave free — output (``max_tokens``)
    + the RAG block + the summary block — via the chars/token estimate with 10% headroom. Returns
    ``(high_chars, low_chars)`` with low = 60% of high (mirrors the legacy 80k→48k hysteresis).
    Scales the working set with ``max_model_len`` while never overflowing it; floored so a tiny
    window can't yield ≤0."""
    reserve = max(0, int(max_tokens) + int(rag_tokens) + int(summary_tokens))
    budget_tok = max(2048, int((int(max_model_len) - reserve) * 0.9))
    high = budget_tok * max(1, int(chars_per_token))
    return high, int(high * 0.6)


def run_tool(name: str, args: Dict[str, Any]) -> str:
    try:
        # Pass code-tools THROUGH to the driving client (runs them on the local fs) when a
        # bridge is active; otherwise they fall through and run server-side as before.
        if _LOCAL_TOOL_BRIDGE is not None and name in LOCAL_TOOL_NAMES:
            return _LOCAL_TOOL_BRIDGE(name, args)
        if name == "read_file":
            p = Path(args["path"])
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
            p   = Path(args["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(args["content"], encoding="utf-8")
            tmp.replace(p)
            return f"OK: Written {len(args['content'])} chars to {args['path']}"

        elif name == "list_directory":
            p = Path(args.get("path", "."))
            if not p.exists():
                return f"ERROR: Not found: {p}"
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
                out += (f"\n... [GX10v3: {shown} von {total} Einträgen gezeigt"
                        + (f" (Hard-Cap {LIST_DIR_HARD_CAP} — nutze sort='time'+limit)" if capped else f" (limit={limit})")
                        + "]")
            return out

        elif name == "execute_command":
            timeout = int(args.get("timeout", 30))
            command = args["command"]
            # Platform mode determines the interpreter — consistent with the
            # syntax guidance injected into the model.
            # stdin=DEVNULL: interactive commands (e.g. cmd `date` without an arg)
            # get EOF immediately instead of blocking for the full timeout.
            # encoding/errors explicit: decode command output as UTF-8 lossily, so a
            # non-locale byte (cp1252 on Windows) never raises decoding the result.
            if PLATFORM == "windows":
                argv = ["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", command]
                r = subprocess.run(
                    argv, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout
                )
            else:
                r = subprocess.run(
                    command, shell=True, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout
                )
            out = (r.stdout + r.stderr).strip()
            return out or f"(exit {r.returncode}, no output)"

        elif name == "move_file":
            dst = Path(args["destination"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(args["source"]), str(dst))
            return f"OK: Moved {args['source']} → {args['destination']}"

        elif name == "delete_file":
            Path(args["path"]).unlink()
            return f"OK: Deleted {args['path']}"

        elif name == "copy_file":
            src = Path(args["source"])
            dst = Path(args["destination"])
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
            for fp in Path(directory).rglob(file_pattern):
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
            Path(args["path"]).mkdir(parents=True, exist_ok=True)
            return f"OK: Created {args['path']}"

        elif name == "advance_pipeline":
            return _advance_pipeline(
                args.get("task_id", ""),
                args.get("agent", ""),
                args.get("next_task_id"),
            )

        elif name == "stage_handover":
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
        global _MEMORY, _WARM
        if _MemoryManager is not None and _MEMORY_CONFIG and _MEMORY is None:
            _MEMORY = _MemoryManager(_MEMORY_CONFIG)
        # Initialize the warm tier (B0) — optional; without a url the tier stays a no-op (fail-soft).
        if _WarmTier is not None and _WARM_CONFIG and _WARM is None:
            _WARM = _WarmTier(_WARM_CONFIG)

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
    def _make_completion(self, think: bool, stream: bool):
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
            _ui_print(col(f"[OK] Prompt: {p} ({len(content)} Zeichen)", C.GREEN))
        else:
            _ui_print(col(f"[WARN] not found: {p}", C.YELLOW))

    def save_session(self):
        # Silent by design: called after every turn (see _dispatch) — a per-turn "[OK] session saved"
        # would stream into the client as noise. Only a real failure is surfaced.
        try:
            p = session_path()
            p.parent.mkdir(parents=True, exist_ok=True)   # state_root existiert i.d.R. (ensure_dirs); idempotent
            p.write_text(
                json.dumps({"messages": self.messages}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
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
        def total_len(msgs):
            return sum(len(str(m.get("content") or "")) for m in msgs)

        # PERF-06: hysteresis trimming for the vLLM prefix cache.
        # As long as it stays below the high-water mark, the message list
        # stays UNCHANGED → the prefix after the system prompt is stable and
        # the server's KV/prefix cache holds across many rounds.
        others_len = total_len([m for m in self.messages if m.get("role") != "system"])
        if others_len <= MAX_CTX_CHARS:
            return

        # Trimming happens only when exceeded — but then in one go
        # down to the low-water mark, instead of a little each round. This causes
        # cache invalidation only RARELY instead of on every iteration.
        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]

        # B1: only when the switch is on, record the removed rounds
        # to summarize + archive them. OFF = empty path, no overhead.
        track = SUMMARIZE_EVICTED
        evicted: List[Dict] = []

        # Trim in whole "rounds", so assistant.tool_calls and the
        # associated tool responses stay together (API invariant).
        while total_len(others) > TRIM_TARGET_CHARS and len(others) > 1:
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
        user = ""
        prev_body = ""
        if prev_summary.strip():
            # remove the marker line before re-feeding (only the content matters)
            prev_body = prev_summary.split("\n", 1)[1].strip() if "\n" in prev_summary else ""
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
                _WARM.set_session(WARM_SESSION_ID, "summary", new_summary.strip())
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
        th = threading.Thread(target=_worker, daemon=True)
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
        th = threading.Thread(target=_worker, daemon=True)
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
            "max":   (f"⏱ MAX-ITER ({MAX_ITERATIONS})", C.YELLOW),
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
        global _TURN_DID_ADVANCE
        _CANCEL_EVENT.clear()
        # B2: per-turn retrieval BEFORE the append (query = user message, dedup against the existing
        # window). FLAG OFF → "" → the user message is appended verbatim (byte-identical).
        rag = self._retrieve_context(user_input)
        self.messages.append({"role": "user",
                              "content": (rag + "\n\n" + user_input) if rag else user_input})
        # New operator turn → reset the auto-plan guard. An advance_pipeline
        # in THIS turn sets it again; a following stage_handover in the
        # same turn is then blocked (as long as autoplan is off).
        _TURN_DID_ADVANCE = False

        # auto mode: decide once per turn whether iteration 0 thinks
        self._turn_think = self._classify_thinking(user_input)

        turn = {"t0": time.time(), "gens": 0, "prompt": 0, "completion": 0}
        # Turn outcome — ALWAYS printed as a status line in finally.
        outcome: Dict[str, Any] = {"kind": "max"}

        try:
          for iteration in range(MAX_ITERATIONS):
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
            return col("[FEHLER] Keine letzte Antwort!", C.RED)
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
                _WARM.del_session(WARM_SESSION_ID, "summary")
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
            col(f"  Streaming    : {'an' if self.stream else 'aus'}", C.GRAY),
            col(f"  Platform     : {self.platform}",              C.GRAY),
            col(f"  Onboarding   : {'on' if self.onboarding else 'off'}", C.GRAY),
            col(f"  Autopilot    : {('on (max=' + str(AUTOPILOT_MAX_CONCURRENT) + (', stream' if AUTOPILOT_STREAM else '') + (', replan' if AUTOPILOT_AUTOPLAN else '') + ')') if AUTOPILOT_ENABLED else 'off'}", C.GRAY),
            col(f"  Thinking     : {self.thinking_mode}",         C.GRAY),
            col(f"  max_tokens   : {self.max_tokens}",            C.GRAY),
            col(f"  Nachrichten  : {len(self.messages)}",         C.GRAY),
            col(f"  Zeichen      : {chars}",                      C.GRAY),
            col(f"  Tool Results : {tool_msgs}",                  C.GRAY),
            col(f"  Tools active : {len(_effective_tools())}",    C.GRAY),
            col(f"  Perf         : {p['gens']} Gens · prompt {p['prompt']} · "
                f"completion {p['completion']} tok · ⌀ {avg_tps:.0f} tok/s", C.GRAY),
            col(f"  Letzte Gen   : {p['last'] or '—'}",            C.GRAY),
            col(f"  Parser       : qwen3_coder (nativ)",            C.GREEN),
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
    config           the effectively-loaded CLI config + source
    config get <key>          read a dotted config key (e.g. mpr.enabled)
    config set <key> <value>  override a dotted config key at runtime
                              (on|off|true|false|num|str; e.g. mpr.enabled on)
    tool <name> <args|text>   run a tool DIRECTLY/deterministically (no model election, no RAG);
                              text → first required arg, or {json}. e.g. tool mpr_research <frage>
    initiative new <name> --type mpr|software   create + activate a initiative (artefact home)
    initiative list | use <slug> | active | reconcile [slug]
    reload           reload gx10.py (the session stays)
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
    key_state = "gesetzt" if os.environ.get(key_env) else "nicht gesetzt"
    return "\n".join([
        col(f"  Quelle        : {_CFG_SOURCE if _CFG_SOURCE else '— (code defaults)'}", C.GREEN),
        col(f"  connection    : {conn['base_url']} · {conn['model']}", C.GRAY),
        col(f"  api-key       : aus Env {key_env} ({key_state})", C.GRAY),
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
        col(f"  Precedence    : code-defaults < file/conf < env < CLI", C.GRAY),
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
_FROZEN_CONFIG_KEYS = frozenset({"setup.type", "security.profile"})


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
_VALID_SETUP_TYPES = ("server", "local")


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
    if setup_type == "server":
        return {"setup_type": "server", "providers_enabled": False, "runner_mode": "none"}
    # local
    base_url = (cfg.get("connection") or {}).get("base_url", "")
    if _is_local_url(base_url):
        raise ValueError("setup.type=local requires a REMOTE base_url (the model runs on the GPU host; "
                         "the engine co-locates with the code CLIs). Got a loopback endpoint — set "
                         "GX10_BASE_URL to the remote model.")
    if not cli_available:
        raise ValueError("setup.type=local requires a reachable agent CLI on this host (none found via "
                         "PATH). Install it or set GX10_CLAUDE_BIN/GX10_AGENT_CMD.")
    return {"setup_type": "local", "providers_enabled": True, "runner_mode": "local"}


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
            out = (f"[initiative] angelegt + active: {v.slug} (type {v.type}) → {v.path.as_posix()}/\n"
                   f"  Artefakte ({visible}) landen jetzt hier; INDEX.md wird automatisch gepflegt.")
            _cfg = _EFFECTIVE_CFG if _EFFECTIVE_CFG is not None else _code_defaults()
            if v.type == "mpr" and not _cfg_get(_cfg, "mpr.enabled"):
                out += "\n  Hinweis: MPR ist noch nicht active — `/config set mpr.enabled on`."
            return out
        if sub == "use":
            if not rest:
                return "usage: /initiative use <slug>"
            v = initiative_use(rest)
            return f"[initiative] active: {v.slug} (type {v.type}) → {v.path.as_posix()}/"
        if sub == "list":
            vs = initiative_list()
            if not vs:
                return "[initiative] keine — `/initiative new <name> --type mpr|software`"
            cur = active_slug()
            lines = ["[initiative]  (* = active)"]
            for v in vs:
                mark = "*" if v.slug == cur else " "
                lines.append(f"  {mark} {v.slug}  ·  type {v.type} · status {v.status} · {v.created}")
            return "\n".join(lines)
        if sub == "active":
            v = initiative_active()
            return (f"[initiative] active: {v.slug} (type {v.type}) → {v.path.as_posix()}/"
                    if v else "[initiative] keins active — `/initiative new …` oder `/initiative use <slug>`")
        if sub == "reconcile":
            fn = globals().get("reconcile_vault")          # Unit C provides the function
            if fn is None:
                return "[initiative] reconcile kommt mit Unit C (INDEX.md + [[links]])"
            slug = rest.strip() or active_slug()
            if not slug:
                return "[initiative] reconcile: kein Initiative angegeben/active"
            return f"[initiative] reconcile {slug}: {fn(slug)}"
        return ("usage: /initiative new <name> --type mpr|software | list | use <slug> | "
                "active | reconcile [slug]")
    except (ValueError, RuntimeError) as e:
        return f"[initiative] {e}"


def _dispatch(agent: GX10, user_input: str):
    cmd = user_input.lower()
    if cmd == "help":
        _ui_print(col(HELP, C.YELLOW))
    elif cmd == "clear":
        _ui_print(agent.clear_context())
    elif cmd == "status":
        _ui_print(agent.status())
    elif cmd == "config":
        _ui_print(_render_config())
    elif cmd.startswith("config get "):
        key = user_input.split(None, 2)[2].strip()
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
            state = col("AN", C.GREEN) if _WATCHER_ENABLED else col("AUS", C.YELLOW)
            _ui_print(f"  auto-advance (reconciler): {state}  |  watcher on / watcher off")
    elif cmd.startswith("autopilot"):
        global AUTOPILOT_ENABLED
        arg = cmd.split()[-1] if len(cmd.split()) > 1 else ""
        if arg == "on":
            AUTOPILOT_ENABLED = True
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["enabled"] = True
            msg = (f"[AUTOPILOT] AN (max_concurrent={AUTOPILOT_MAX_CONCURRENT}); "
                   f"greift beim nächsten Tick (~{RECONCILER_INTERVAL:.0f}s).")
            if not _WATCHER_ENABLED:
                msg += "  ⚠ Reconciler ist AUS — 'watcher on' nötig, sonst passiert nichts."
            _ui_print(col(msg, C.GREEN))
        elif arg == "off":
            AUTOPILOT_ENABLED = False
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["enabled"] = False
            _ui_print(col("[AUTOPILOT] OFF — no new auto-starts (running sessions remain)", C.YELLOW))
        else:
            state = col("AN", C.GREEN) if AUTOPILOT_ENABLED else col("AUS", C.YELLOW)
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
                limit_info = f", max {AUTOPILOT_MAX_TASKS} Tasks — stoppt automatisch"
                _ui_print(col(
                    f"[AUTOPLAN] AN{limit_info}",
                    C.GREEN))
            else:
                _ui_print(col(
                    "[AUTOPLAN] AN — max_tasks=0 (DAUERSCHLEIFE, kein automatischer Stopp!)\n"
                    "  → Empfehlung: Limit setzen mit  autoplan off  dann  autoplan on N",
                    C.YELLOW))
            _ui_print(col(
                "  ⚠ WARNUNG: Autoplan NIEMALS mit einem bezahlten API-Abo verwenden!\n"
                "    Jede Planung = ein Qwen-Turn = Kosten. Nur für lokale vLLM-Instanzen!",
                C.RED))
        elif arg == "off":
            AUTOPILOT_AUTOPLAN = False
            _AUTOPLAN_DONE     = 0
            if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["autoplan"] = False
            _ui_print(col("[AUTOPLAN] OFF — pipeline stops when the queue is empty. Counter reset.", C.YELLOW))
        else:
            state     = col("AN", C.GREEN) if AUTOPILOT_AUTOPLAN else col("AUS", C.YELLOW)
            limit_str = f"  max={AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else "  unbegrenzt"
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
            _ui_print(col("[LOG-TERMINAL] AUS", C.YELLOW))
        else:
            state = col("AN", C.GREEN) if AUTOPILOT_LOG_TERMINAL else col("AUS", C.YELLOW)
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
            state = col("AN", C.GREEN) if RAG_ENABLED else col("AUS", C.YELLOW)
            _ui_print(f"  per-turn retrieval (RAG): {state}  |  rag on / rag off")
    elif cmd == "context":
        _ui_print(agent.context_report())
    elif cmd == "initiative" or cmd.startswith("initiative "):
        _ui_print(col(_initiative_command(user_input[len("initiative"):].strip()), C.CYAN))
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
        _ui_print(col(f"  [AUTO] Session zu {task_id} beendet (PID {proc.pid}) — Task ist done", C.GRAY))
    except Exception as e:
        _ui_print(col(f"  [AUTO] could not terminate the session for {task_id}: {e!r}", C.YELLOW))

def _find_handover(task_id: str) -> Optional[Path]:
    d = handovers_dir(soft=True)          # B3: <initiative>/.work/handovers (soft → daemon-safe)
    if d is None or not d.exists():
        return None
    hits = sorted(d.glob(f"{task_id}_*.md"))
    return hits[0] if hits else None

def _agent_from_handover(name: str) -> str:
    m = _HO_AGENT_RE.search(name)
    if not m:
        return ""
    agent = m.group(1).upper()
    return "SONNET" if agent == "KIMI" else agent   # legacy _KIMI.md → Sonnet

def _task_agent(task: Dict[str, Any]) -> str:
    """The agent ASSIGNED to the task (OPUS/SONNET) — from assigned_to, otherwise
    from the existing handover. Prevents a foreign agent's feedback from
    completing an OPUS task. "kimi" (legacy) is normalized to SONNET."""
    a = (task.get("assigned_to") or "").lower()
    if "opus" in a:
        return "OPUS"
    if "sonnet" in a or "kimi" in a:          # Kimi → Sonnet (legacy alias, 2026-06-15)
        return "SONNET"
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
    model, effort = _parse_handover_meta(ho)
    effort = effort or AUTOPILOT_DEFAULT_EFFORT
    # Kimi was replaced by Sonnet (2026-06-15): any remaining reference runs as Sonnet.
    if agent == "KIMI":
        agent = "SONNET"
    prompt = (f"Lies und bearbeite autonom den Handover {ho.as_posix()}. "
              f"Folge den Anweisungen in .claude/CLAUDE.md.")
    if model and str(model).startswith("kimi"):
        model = None                          # legacy Kimi model → default (Sonnet/Opus)
    model  = model or ("claude-opus-4-8" if agent == "OPUS" else "claude-sonnet-4-6")
    argv = [AUTOPILOT_CLAUDE_BIN, "--model", model, "--effort", effort]
    extra = list(AUTOPILOT_EXTRA_ARGS)
    if AUTOPILOT_STREAM:
        # Live streaming: stream-json NEEDS --verbose (otherwise claude aborts).
        # Output still goes to the log FILE (no pipe read) → no deadlock.
        if "--verbose" not in extra:
            extra.append("--verbose")
        if "--output-format" not in extra:
            extra += ["--output-format", "stream-json"]
    argv += extra + ["--print", prompt]
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
        proc = subprocess.Popen(argv, cwd=".", stdout=lf, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, env=_launch_env)
    except Exception as e:
        _autopilot_release()
        _ui_print(col(f"  ✗ [AUTO] Launch {task_id} fehlgeschlagen: {e!r}", C.RED))
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
        _ui_print(col(f"  {'✓' if ok else '⚠'} [AUTO] claude beendet: {task_id} "
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
                        f"  ⚠ [AUTO] {task_id}: claude beendet (exit {rc}) "
                        f"aber KEIN Feedback in .work/feedback/ — Task bleibt in_progress!",
                        C.RED))
        except Exception:
            pass
    threading.Thread(target=_wait, daemon=True).start()


# ─── Feedback reconciler (polling instead of event triggers) ───────
# Reads the TRUE state every tick: for each in_progress task a
# complete feedback file is sought and the completion is triggered DETERMINISTICALLY (without
# an LLM). Misses/duplicates no FS events, is idempotent.
# Autopilot side (optional): pending task with handover → start claude.
_FB_RE = re.compile(r"_(\w+)-feedback\.md$")

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
            agent = _agent_from_handover(ho.name)
            if agent not in ("OPUS", "SONNET"):
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
                    f"  ⚠ [WATCHER] Fremde Datei in feedback-Inbox: {orphan.name} "
                    f"— kein Advance möglich (kein task_id_agent-Format). "
                    f"Analyse-Dokumente gehören nicht in die .work/feedback-Inbox",
                    C.YELLOW))
    for task in (store.list("pending") + store.list("in_progress")):
        tid = task.get("id") or ""
        agent = _task_agent(task)            # expected agent (not from an arbitrary filename!)
        if agent not in ("OPUS", "SONNET"):
            continue
        fb = d / f"{tid}_{agent}-feedback.md"  # ONLY the assigned agent's feedback
        if not fb.exists():
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
                  + " Tasks abgeschlossen", C.CYAN))
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
        "security": {
            # Phase-d trust profile (single-tenant): open | token | sealed.
            # The server (engine/security.py) reads this block; the token VALUE comes
            # from the env named here, never from config. See docs/roadmap.md.
            "profile":             "open",
            "token_env":           "GX10_SERVER_TOKEN",
            "session_heartbeat_s": 30,
            "code_locality":       "mount",   # sealed forces "local"
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
    setif("GX10_AUTOPILOT",  "autopilot",  "enabled", _truthy)
    setif("GX10_AUTOPILOT_STREAM",    "autopilot", "stream",          _truthy)
    setif("GX10_AUTOPILOT_TERMINATE", "autopilot", "terminate_on_advance", _truthy)
    setif("GX10_AUTOPILOT_AUTOPLAN",       "autopilot", "autoplan",           _truthy)
    setif("GX10_AUTOPILOT_MAX_TASKS",      "autopilot", "autoplan_max_tasks", int)
    setif("GX10_AUTOPILOT_LOG_TERMINAL", "autopilot", "log_terminal",  _truthy)
    return cfg


def _apply_cli(cfg: Dict[str, Any], args) -> Dict[str, Any]:
    """CLI override (level 4, strongest). Only flags actually set."""
    if args.base_url   is not None: cfg["connection"]["base_url"]    = args.base_url
    if args.model      is not None: cfg["connection"]["model"]       = args.model
    if args.prompt     is not None: cfg["paths"]["system_prompt"]    = args.prompt
    if args.workdir    is not None: cfg["paths"]["workdir"]          = args.workdir
    if args.max_tokens is not None: cfg["generation"]["max_tokens"]  = args.max_tokens
    if args.thinking   is not None: cfg["generation"]["thinking_mode"] = args.thinking
    if args.platform   is not None: cfg["platform"]["mode"]          = args.platform
    if args.onboarding is not None: cfg["onboarding"]["enabled"]     = args.onboarding
    if args.autopilot  is not None: cfg["autopilot"]["enabled"]      = args.autopilot
    if args.no_stream:              cfg["generation"]["stream"]      = False
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
    global MAX_MODEL_LEN, TOKEN_BUDGET
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
    if TOKEN_BUDGET:
        MAX_CTX_CHARS, TRIM_TARGET_CHARS = _derive_ctx_budget(
            MAX_MODEL_LEN, MAX_TOKENS, RAG_MAX_TOKENS, SUMMARY_MAX_TOKENS, CHARS_PER_TOKEN)
    _wcfg = cfg.get("workers", {})
    WORKER_MEMORY      = bool(_wcfg.get("memory_read", True))    # §3c MAP: default ON (06-18)
    WORKER_WRITE       = bool(_wcfg.get("memory_write", True))   # §3c REDUCE: default ON (06-18)
    WORKER_WRITE_MODE  = (str(_wcfg.get("write_mode", "reducer")).strip().lower() or "reducer")
    WARM_SESSION_ID    = (os.environ.get("GX10_SESSION_ID", "").strip() or WARM_SESSION_ID)

    _PLANNING_KW = tuple(ta["planning_keywords"])
    _ROUTINE_KW  = tuple(ta["routine_keywords"])

    WORKSPACE_DIRS = list(ws["dirs"])
    _IDLE_ACTIVE   = ws["idle_marker"]

    # Memory config: file (conf/memory/memory.json) OR env (GX10_MEMORY_URL).
    # Optional — without base_url _MEMORY_CONFIG stays empty → memory off (hooks inert).
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
