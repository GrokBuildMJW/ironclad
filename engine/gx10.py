#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ironclad orchestration engine — agent loop, deterministic TaskStore, fail-closed
macros (advance_pipeline / stage_handover), config-tree loader, and the interactive
CLI. Runs against any OpenAI-compatible endpoint; every model-emitted task_json is
validated against the ACK contract at the stage_handover boundary.

Key design points:
  - Macro tools collapse multi-step workflows into a single deterministic call
    (advance_pipeline for completion, stage_handover for creation) — far fewer
    LLM round-trips than step-by-step file ops.
  - Streaming + incremental output; hysteresis context-trimming (prefix-cache
    friendly); per-generation perf instrumentation (TTFT, tokens/s).
  - Thinking is decided per turn (planning thinks; routine lookups don't); the ACK
    emitter turns it off per-request for reliable structured output.

Default system prompt: prompts/GX10_Orchestrator_SystemPrompt.md.

Examples:
    python gx10.py
    python gx10.py --thinking off   # fastest
    python gx10.py --prompt prompts/GX10_Orchestrator_SystemPrompt.md
"""

import os
import re
import sys
import json
import time
import shutil
import subprocess
import threading
import queue as _q
import argparse
from collections import deque
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, Callable

# Hinweis: Der frühere watchdog-basierte Feedback-Watcher wurde durch einen
# Polling-Reconciler ersetzt (zuverlässiger, keine Abhängigkeit nötig).

try:
    from openai import OpenAI
except ImportError:
    # Soft: das Modul bleibt importierbar OHNE openai (z. B. der Thin-Client lädt nur
    # die UI-Primitive). Erst die GX10-Konstruktion (die einen Client braucht) failt
    # dann mit klarer Meldung — siehe GX10.__init__.
    OpenAI = None  # type: ignore[assignment,misc]

try:
    from memory import MemoryManager as _MemoryManager
except ImportError:
    _MemoryManager = None

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
    # prompt_toolkit fehlt (z. B. headless Server-Modus): `Application` trotzdem als
    # Name bereitstellen, sonst crasht die Modul-Annotation `Optional[Application]`
    # beim Import. Any ist hier korrekt — die echte App wird nur unter HAS_PT gebaut.
    Application = Any  # type: ignore[assignment,misc]

# ─── Installationsort (Code, read-only) ─────────────────────
# SCRIPT_DIR = wo gx10_v3.py + prompts/ liegen. Davon getrennt: WORKDIR
# (wo der Orchestrator arbeitet) — siehe Config-Loader / main().
SCRIPT_DIR = Path(__file__).resolve().parent

# core/ auf sys.path, damit das ACK-Paket (core/ack) importierbar ist, wenn die
# Engine als Script läuft — SCRIPT_DIR ist core/engine, der Parent ist core/.
_CORE_DIR = SCRIPT_DIR.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

# ─── Konfiguration (Code-Defaults) ──────────────────────────
# Diese Modul-Konstanten sind die schwächste Stufe der Wert-Precedence
# (Code-Defaults < Config-Datei < Env < CLI). Beim Start überschreibt
# `_apply_config()` sie aus der geladenen Config — so bleiben alle
# bestehenden Referenzen (run_tool, Makros, _trim_context …) unverändert.
DEFAULT_BASE_URL = "http://localhost:8000/v1"   # generischer Default; echter Endpoint via Config (connection.base_url)
DEFAULT_API_KEY  = "not-needed"
DEFAULT_MODEL    = "qwen3.6-35b"   # aktuelles Orchestrator-Modell; echter Endpoint via conf/connection
DEFAULT_PROMPT   = "prompts/GX10_Orchestrator_SystemPrompt.md"
DEFAULT_WORKDIR  = "."           # WORKDIR: Arbeitsort (CWD-Verhalten wie bisher)
CODE_ROOT        = ""            # optionaler Code-Root für den Handover-Pfad-Guard
                                 # (vessel-spezifisch, z. B. ein Service-Unterordner
                                 # im Repo); leer = nur Repo-Root prüfen. Via paths.code_root.
MAX_ITERATIONS   = 20
MAX_CTX_CHARS    = 80_000        # High-Water: erst hier wird getrimmt
TRIM_TARGET_CHARS = 48_000       # PERF-06: Low-Water nach dem Trim (60 %)
MAX_TOKENS       = 8192          # PERF-10: vorher 4096 → Handover-Truncation
LANGUAGE         = "en"          # Antwortsprache des Orchestrators (OSS-Default en; per GX10_LANGUAGE/Config)
MAX_FILE_CHARS   = 24_000        # PERF-05: read_file-Cap (Head+Tail)
LIST_DIR_HARD_CAP = 200          # HV-B: harter Cap in list_directory
TEMPERATURE      = 0.3
RETRY_BACKOFF    = 1.5           # OPT-4: Wartezeit (s) vor 1× Retry bei API-Fehler
SESSION_FILE     = ".gx10_session.json"
SPINNER_FRAMES   = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
UI_REFRESH_INTERVAL = 0.1        # prompt_toolkit Application-Refresh

# Plattform-Modus: bestimmt Shell + Befehls-Syntax in execute_command.
# PLATFORM_MODE ist der Config-Wert ("auto" wird beim Start aufgelöst);
# PLATFORM ist der EFFEKTIVE Modus ("windows" | "linux"), nie "auto".
PLATFORM_MODE = "auto"           # "auto" | "windows" | "linux"
PLATFORM      = "windows" if os.name == "nt" else "linux"

# Task-Management (TaskStore): Schwelle für deterministische Themen-Dedup.
TASKS_DEDUP_THRESHOLD = 0.8      # Jaccard über Titel+Beschreibung

# Task-ID-Prefix (vessel-konfigurierbar via tasks.id_prefix). IDs sind
# {prefix}-N (monoton). Default "KGC" erhält bestehendes Verhalten; die
# Beispiel-IDs in den Tool-Beschreibungen nennen weiterhin den Default-Prefix.
TASK_PREFIX = "KGC"

# ─── ACK (Agent-Contract-Kernel) Integration ──────────────────
# Validiert jedes modell-emittierte task_json an der stage_handover-Grenze gegen den
# ACK-Vertrag (ack.case_spec). Soft-Pfad: bei Verletzung wird der exakte Fehler
# zurückgegeben → der Agent-Loop reicht ihn dem Modell als Tool-Result zurück (Reask),
# nichts wird angelegt. LODESTAR_ENABLED → CapabilityTaskSpec (capability pflicht für
# buildable types). Beide config-getrieben (ack.enabled / lodestar.enabled).
ACK_ENABLED      = True
LODESTAR_ENABLED = False

# Onboarding-Modus: proaktive Duplikat-Vorprüfung VOR dem (teuren) Handover.
# Default aus (Store-Dedup garantiert Korrektheit ohnehin). Hilfreich bei
# Migration von einem anderen CLI / vielen Alt-Tasks. Bei aktivem Modus wird
# das Tool `check_task_exists` angeboten und der Prompt weist zur Vorprüfung an.
ONBOARDING_MODE = False

# Autopilot (Path B): Der Reconciler startet für pending-Tasks mit Handover
# automatisch `claude --print` (API-freie Ausführung) und schaltet pending →
# in_progress. Default AUS (startet Claude autonom mit skip-permissions).
AUTOPILOT_ENABLED        = False
AUTOPILOT_CLAUDE_BIN     = "claude"
AUTOPILOT_EXTRA_ARGS     = ["--dangerously-skip-permissions"]
AUTOPILOT_DEFAULT_EFFORT = "medium"
AUTOPILOT_LOGS_DIR       = "logs"
AUTOPILOT_MAX_CONCURRENT = 1            # 1 = sequentiell; >1 parallel; 0 = unbegrenzt
AUTOPILOT_STREAM         = False        # Live-Log-Streaming (claude --verbose --output-format stream-json); Default AUS
AUTOPILOT_TERMINATE_ON_ADVANCE = False  # beim advance die zugehörige claude-Session beenden; Default AUS
AUTOPILOT_AUTOPLAN       = False   # Nach leerem Queue GX10 automatisch den nächsten Task planen; Default AUS
AUTOPILOT_MAX_TASKS      = 0       # Max. Tasks die autoplan plant (0 = unbegrenzt — NUR lokale vLLM verwenden!)
_AUTOPLAN_DONE           = 0       # Session-Zähler (nur im agent_thread angefasst → kein Lock nötig)
_TURN_DID_ADVANCE        = False   # Guard: True nach advance_pipeline im laufenden Turn. Verhindert,
                                   # dass das Modell im SELBEN Turn (ohne Operator-Eingabe) direkt
                                   # stage_handover nachschiebt ("Auto-Plan"), solange AUTOPILOT_AUTOPLAN
                                   # aus ist. Reset bei jedem neuen Operator-Turn (run()).
AUTOPILOT_LOG_TERMINAL   = False        # Bei jedem Autopilot-Start neues Terminal mit Get-Content -Wait öffnen; Default AUS
# Kimi wurde am 2026-06-15 durch Sonnet ersetzt. "KIMI" bleibt nur als
# Legacy-Alias und wird überall transparent auf SONNET normalisiert
# (Claude Code CLI + claude-sonnet-4-6). Keine Kimi-CLI-Plumbing mehr.
WATCHER_FEEDBACK_DIR = "summaries/feedback"   # Watch-Pfad (relativ zum WORKDIR)
API_KEY_ENV      = "GX10_API_KEY"             # Secrets nur aus Env, nie aus Datei

# Workspace-Struktur (von _ensure_dirs angelegt) — generischer Default,
# pro Deployment/Vessel via Config (workspace.dirs) überschreibbar. Die
# funktionalen Verzeichnisse (tasks/, summaries/handovers, summaries/feedback)
# werden von den Makros und dem Reconciler vorausgesetzt.
WORKSPACE_DIRS = [
    "tasks/pending", "tasks/in_progress", "tasks/done",
    "summaries/handovers", "summaries/feedback",
    "summaries/proposals", "summaries/decisions",
    "reviews",
    "vault",
    "memory",
]

# Memory-Layer — module-level Singleton, initialisiert in GX10.__init__()
_MEMORY_CONFIG: Dict[str, Any] = {}
_MEMORY: Optional[Any] = None

# ─── Farben ──────────────────────────────────────────────────
class C:
    GREEN   = "\033[92m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    GRAY    = "\033[90m"
    MAGENTA = "\033[95m"
    BOLD    = "\033[1m"
    RESET   = "\033[0m"

def col(text: str, c: str) -> str:
    return f"{c}{text}{C.RESET}"

THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

def clean(text: str) -> str:
    return THINK_RE.sub("", text).strip() if text else ""

# ─── Thinking-Auto-Klassifikation ────────────────────────────
# Sicherer Fehlermodus: im Zweifel DENKEN. Thinking wird nur bei klarer
# Routine (Status/Lookup/done) OHNE Planungs-Verb abgeschaltet.
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
    # Routine-Status-Abfragen (eng gehalten — kein breites "gibt es" / "liegt an",
    # damit diagnostisches "woran liegt das?" weiterhin denkt):
    "etwas zu tun", "zu tun", "steht an", "todo", "to-do", "idle",
    "anything to do", "was liegt an", "liegt was an",
)


# ─── Streaming Think-Filter (PERF-01 + PERF-02 Anzeige) ──────
class _ThinkFilter:
    """Inkrementeller Filter: unterdrückt alles zwischen <think> und
    </think> über Chunk-Grenzen hinweg. Hält am Puffer-Ende einen
    möglichen Teil-Tag zurück, damit kein Tag zerschnitten wird."""
    OPEN  = "<think>"
    CLOSE = "</think>"

    def __init__(self):
        self.in_think = False
        self.buf      = ""

    @staticmethod
    def _safe_cut(s: str, tag: str) -> int:
        """Index bis zu dem s gefahrlos emittiert/verworfen werden darf;
        hält ein Suffix zurück, das Präfix von tag sein könnte."""
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
                    self.buf = self.buf[cut:]   # verwerfen, Teil-Tag behalten
                    break
                self.buf = self.buf[j + len(self.CLOSE):]
                self.in_think = False
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.in_think else self.buf
        self.buf = ""
        return rest


# ─── Tabellen-bewusste Zeilenausgabe (Code-gerenderte Tabellen) ──
class _TableLineRenderer:
    """Nimmt Text zeilenweise an. Markdown-/Pipe-Tabellen werden gepuffert
    und mit exakt ausgerichteten Spalten ausgegeben; alles andere geht
    unverändert (zeilenweise) durch. Die `|---|`-Trennzeile und `**`/`` ` ``
    werden entfernt. Kostet KEINE zusätzlichen Tokens — die Ausrichtung
    passiert lokal beim Rendern."""

    def __init__(self, emit_line):
        self.emit_line = emit_line     # callable(str): gibt EINE fertige Zeile aus
        self.buf   = ""
        self.table = []                # gesammelte Roh-Tabellenzeilen (ohne Trenner)

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
        self.emit_line(line.replace("**", ""))   # literale Markdown-Bold-Marker entfernen

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


# ─── Globaler UI-State ────────────────────────────────────────
_UI_MAX_LINES                 = 5000
_UI_LINES:   "deque[str]"     = deque(maxlen=_UI_MAX_LINES)
_UI_PARTIAL: str              = ""
_UI_LOCK                      = threading.Lock()
_UI_APP: Optional[Application] = None

# Headless-Capture-Hook: wird vom Server-Modus (core/engine/server.py) gesetzt,
# wenn KEINE prompt_toolkit-UI läuft (_UI_APP is None). Ein Callable(text:str)->None,
# das die Ausgabe abgreift (z. B. in einen Thread-lokalen Request-Buffer) statt auf
# stdout zu drucken. Bleibt None im normalen CLI-/REPL-Betrieb → Verhalten unverändert.
_UI_SINK: Optional[Callable[[str], None]] = None

_INPUT_QUEUE: _q.Queue        = _q.Queue()
_CANCEL_EVENT                 = threading.Event()
_RELOAD_FLAG                  = False
_WATCHER_ENABLED              = True    # Auto-Advance via Reconciler (jetzt stabil → Default an)
RECONCILER_INTERVAL           = 3.0     # Polling-Intervall (s)
_ADVANCE_CMD                  = "\x00advance\x00"   # interner strukturierter Reconciler-Befehl
_LAUNCH_CMD                   = "\x00launch\x00"    # interner Autopilot-Launch-Befehl

# Autopilot: Zähler reservierter/laufender claude-Prozesse (Nebenläufigkeits-Gate)
_AUTOPILOT_ACTIVE             = 0
_AUTOPILOT_LOCK               = threading.Lock()
_AUTOPILOT_PROCS: Dict[str, Any] = {}   # task_id -> Popen (für gezieltes Beenden beim advance)

_status = {"thinking": False, "label": "bereit"}

# Effektiv geladene Config + Quelle (in main() gesetzt) — für den `config`-Befehl.
_EFFECTIVE_CFG: Optional[Dict[str, Any]] = None
_CFG_SOURCE: Optional[Path] = None

def _ui_print(*args, sep: str = " ", end: str = "\n", flush: bool = False):
    """Universelle Ausgabe: Application-Fenster oder Fallback-stdout."""
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
        # Headless-Server-Modus: Ausgabe an den Capture-Hook geben statt stdout.
        _UI_SINK(text + end)
    else:
        print(*args, sep=sep, end=end, flush=flush)

_ANSI_LEN_RE = re.compile(r"\x1b\[[0-9;]*m")

def _visual_rows(line: str, width: int) -> int:
    """Wie viele Bildschirmzeilen eine (ggf. umbrechende) Zeile belegt —
    ANSI-Farbcodes zählen nicht zur Breite."""
    n = len(_ANSI_LEN_RE.sub("", line))
    return max(1, -(-n // width))   # ceil(n/width)

def _get_output():
    # WICHTIG: Größe aus prompt_toolkits EIGENER Quelle nehmen, wenn die App
    # läuft — sonst kann sie von shutil abweichen (bis zum ersten Resize), und
    # das Tail-Budget passt nicht zur tatsächlichen Fensterhöhe → unterste
    # Zeilen (perf, ✓ FERTIG) werden geclippt, bis man das Terminal bewegt.
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
    # Untere, fixe UI: Trennlinie(1) + Eingabe(1) + Trennlinie(1) + Toolbar(3) = 6.
    rows  = max(1, term_rows - 6)
    width = max(1, term_cols)
    with _UI_LOCK:
        lines = list(_UI_LINES)
        if _UI_PARTIAL:
            lines.append(_UI_PARTIAL)
    # Vom ENDE her sammeln, bis das Fenster (in SICHTBAREN, umgebrochenen Zeilen)
    # voll ist — so bleibt die neueste Zeile (✓ FERTIG) garantiert unten sichtbar.
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

    # Status-Indikatoren — immer sichtbar, auch während Thinking
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

# ─── Tool Definitionen ────────────────────────────────────────
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
                "Task transitions: pending→in_progress→done. "
                "Vault archiving: vault/_Workflow/active.md → vault/_Workflow/handovers/KGC-XXX_OPUS.md. "
                "ID conflict resolution: rename existing file before writing new one."
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
                "Copy a file without removing the original. "
                "Primary use: copy KGC-XXX_OPUS.md to vault/_Workflow/active.md. "
                "Also: copy feedback to vault/_Workflow/feedback/."
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
                "deterministic step: archive vault/_Workflow/active.md → handovers/, "
                "copy the feedback into the vault, set the task JSON to status=done and "
                "move it to tasks/done/, delete the handover in summaries/handovers/, and "
                "optionally activate the next task. "
                "On 'done' ALWAYS use this tool instead of individual "
                "move_file/copy_file/delete_file calls. Fail-closed: aborts if the "
                "feedback file is missing. Never touches code/ or the audit chain."
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

# Nur im Onboarding-Modus angebotenes Tool: billige Duplikat-Vorprüfung,
# die DIESELBE deterministische Store-Dedup nutzt wie das stage_handover-Gate.
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
    """Tool-Liste je nach Modus — Onboarding-Tools nur, wenn aktiv."""
    # Tool nur anbieten, wenn Memory KONFIGURIERT ist (nicht bloß das Modul da ist) —
    # sonst böte sich der Tool an, obwohl jeder Aufruf "nicht verfügbar" zurückgäbe.
    mem = [MEMORY_TOOL] if _MEMORY is not None else []
    return TOOLS + mem + (ONBOARDING_TOOLS if ONBOARDING_MODE else [])

# ─── Makro-Tool: deterministische Pipeline (HV-A) ─────────────
_TASK_ID_RE = re.compile(rf"^{re.escape(TASK_PREFIX)}-[A-Za-z0-9_]+$")
_IDLE_ACTIVE = "# Workflow — idle\n\nKein aktiver Handover.\n"

def _atomic_write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    # Windows: os.replace scheitert mit [WinError 5], wenn das Ziel von einem
    # anderen Prozess offen gehalten wird (z. B. Obsidian auf active.md). Der
    # Lock ist i. d. R. transient → kurz zurückversuchen; hält er an, direkt
    # (nicht-atomar) überschreiben, statt die ganze Pipeline scheitern zu lassen.
    for attempt in range(8):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if attempt < 7:
                time.sleep(0.25)
                continue
            p.write_text(content, encoding="utf-8")   # Fallback: direkt schreiben
            try:
                tmp.unlink()
            except OSError:
                pass
            return

def _normalize_handover_id(md: str, tid: str) -> str:
    """Setzt ALLE `task_id:`-Zeilen im Handover (Frontmatter + Feedback-Template)
    auf die vom Store vergebene ID. count=0 = alle Vorkommen ersetzen, damit
    das Feedback-Template im Body nicht KGC-XXX behält (Reconciler-Fallback)."""
    return re.sub(r"(?m)^(task_id:\s*).*$", rf"\g<1>{tid}", md, count=0)


def _advance_pipeline(task_id: str, agent: str, next_task_id: Optional[str] = None) -> str:
    """Schaltet die 'done'-Pipeline für EINEN Task deterministisch weiter.
    Status-Übergänge laufen über den TaskStore (Verzeichnis = Wahrheit),
    active.md wird projiziert. Fail-closed: ohne Feedback-Datei kein
    Abschluss. Rührt weder code/ noch die Audit-Chain an."""
    if not task_id or not _TASK_ID_RE.match(task_id):
        return f"ERROR: invalid task_id: {task_id!r} (expected e.g. KGC-315)"
    agent = (agent or "").upper()
    if agent == "KIMI":
        agent = "SONNET"                      # Kimi → Sonnet (Legacy-Alias, 2026-06-15)
    if agent not in ("OPUS", "SONNET"):
        return f"ERROR: agent must be OPUS or SONNET (was: {agent!r})"
    if next_task_id and not _TASK_ID_RE.match(next_task_id):
        return f"ERROR: invalid next_task_id: {next_task_id!r}"

    store = _store()
    log: List[str] = []

    # Idempotenz-Gate: Task bereits done → kein erneutes Advance nötig
    existing = store.get(task_id)
    if existing and existing.get("status") == "done":
        return (f"OK: task {task_id} is already done — no re-advance needed. "
                f"Feedback liegt in vault/_Workflow/feedback/{task_id}_{agent}-feedback.md")

    # 0. Fail-closed-Gate: Feedback MUSS existieren
    # Primär: summaries/feedback/ (Reconciler-Inbox)
    # Fallback: vault/_Workflow/feedback/ (bereits vom Reconciler archiviert)
    fb = Path(f"summaries/feedback/{task_id}_{agent}-feedback.md")
    if not fb.exists():
        fb_vault = Path(f"vault/_Workflow/feedback/{task_id}_{agent}-feedback.md")
        if fb_vault.exists():
            fb = fb_vault
            log.append(f"feedback aus Vault-Archiv gelesen: {fb_vault}")
        else:
            return (f"ERROR: Feedback fehlt: summaries/feedback/{task_id}_{agent}-feedback.md "
                    f"und vault/_Workflow/feedback/{task_id}_{agent}-feedback.md "
                    f"— Task gilt als NICHT abgeschlossen. Pipeline nicht weitergeschaltet.")
    log.append(f"feedback found: {fb}")

    try:
        # 1. aktuellen active.md-Handover archivieren (vor dem Umschalten)
        active  = Path("vault/_Workflow/active.md")
        archive = Path(f"vault/_Workflow/handovers/{task_id}_{agent}.md")
        if active.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(active), str(archive))
            log.append(f"active.md archiviert → {archive}")
        else:
            log.append("active.md not present (skip archive)")

        # 2. Feedback ins Vault archivieren UND Original entfernen — sonst
        #    sammeln sich Alt-Feedbacks in summaries/feedback/ und matchen bei
        #    ID-Wiederverwendung erneut (Stale-Trigger).
        #    Wenn fb bereits aus vault/ kommt (Fallback), kein Copy+Delete nötig.
        vfb = Path("vault/_Workflow/feedback") / fb.name
        vfb.parent.mkdir(parents=True, exist_ok=True)
        if fb.resolve() != vfb.resolve():
            shutil.copy2(str(fb), str(vfb))
            try:
                fb.unlink()
                log.append(f"feedback archiviert → {vfb} (Original entfernt)")
            except OSError:
                log.append(f"feedback → {vfb}")
        else:
            log.append(f"feedback already in vault: {vfb} (no copy needed)")

        # 3. Status-Übergang → done (über den Store)
        try:
            store.transition(task_id, "done")
            log.append(f"task {task_id} → tasks/done (status=done)")
        except KeyError:
            log.append("task-json not found (skip)")

        # 3a. Memory: Task-Abschluss als Episode speichern (fail-soft)
        if _MEMORY is not None and _MEMORY.is_available():
            try:
                fb_text = vfb.read_text(encoding="utf-8") if vfb.exists() else ""
                _MEMORY.store_task_completion(task_id, existing or {}, fb_text)
            except Exception:
                pass

        # 4. Handover in summaries/handovers löschen
        deleted = False
        for cand in (Path(f"summaries/handovers/{task_id}_{agent}.md"),
                     Path(f"summaries/handovers/{task_id}_{agent.capitalize()}.md")):
            if cand.exists():
                cand.unlink()
                log.append(f"handover deleted: {cand}")
                deleted = True
                break
        if not deleted:
            log.append("no handover in summaries/handovers (skip)")

        # 5. nächsten Task aktivieren (Store) — active.md folgt aus Projektion
        if next_task_id:
            try:
                store.transition(next_task_id, "in_progress")
                log.append(f"next task {next_task_id} → in_progress")
            except KeyError:
                log.append(f"WARN: next task {next_task_id} not found")

        # 6. active.md projizieren (neuester nicht-done Handover bzw. idle)
        store.project_active()
        log.append("active.md projiziert")

        # 7. Optional: zugehörige Autopilot-Session beenden (Task ist done)
        if AUTOPILOT_TERMINATE_ON_ADVANCE:
            _terminate_autopilot(task_id)
            log.append("autopilot-session beendet (falls aktiv)")

        # 8. Vault-Projektionen DETERMINISTISCH regenerieren — mechanisch, NICHT
        #    von GX10s Schritt-6-Disziplin abhängig (verhindert Stale-Backlog →
        #    Autoplan plant sonst aus veralteten Daten → Dublette). Idempotent +
        #    fail-soft: ein Skript-Fehler bricht den bereits erfolgten Advance NICHT.
        #    UTF-8-Env, damit Emoji-Ausgaben nicht an cp1252-stdout crashen.
        _env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # update_capability_tracking.py regeneriert ALLE Capability-Domains
        # (n8n-parity, frontend-ux-parity, …) generisch aus ihren *-gap-tracking.md.
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

    return f"OK: pipeline advanced for {task_id} ({agent})\n" + "\n".join(f"  - {l}" for l in log)


# ─── Pfad-Guard: erfundene Codebase-Pfade im Handover erkennen ───
# Der Orchestrator rät mitunter nicht-existente "Aktueller Codebase-Zustand"-
# Pfade, was den Code-Agenten zum Neubau statt Erweitern verleitet
# (Dublettenrisiko). Dieser Check meldet code-artige Pfade, die weder relativ
# zum Repo-Root noch unter dem optionalen, vessel-spezifischen CODE_ROOT
# (paths.code_root; leer = aus) existieren.
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


# ─── Makro-Tool: Handover veröffentlichen (OPT-2, store-gestützt) ──
def _ack_validate(fields: Dict[str, Any]) -> Optional[str]:
    """ACK-Soft-Pfad-Gate: validiert ein modell-emittiertes task_json gegen den
    ACK-Vertrag. Liefert einen EXAKTEN Fehlerstring bei Verletzung, sonst None
    (gültig / Gate aus / ACK-Paket nicht verfügbar → degradiert weich, die Engine
    läuft weiter). Bei aktiviertem Lodestar wird die capability-tragende Spec
    genutzt (capability pflicht für buildable types)."""
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
        return None  # ACK nicht importierbar → weich degradieren
    try:
        spec_cls.model_validate(fields)
        return None
    except ValidationError as e:
        return str(e)


def _stage_handover(task_id: Optional[str], agent: str, handover_md: str,
                    task_json: Optional[str] = None, set_active: bool = True,
                    force: bool = False) -> str:
    """Veröffentlicht einen NEUEN Task+Handover in EINEM Schritt über den
    TaskStore: ID-Vergabe, created_at-Stempel, Schema- und Themen-Dedup sind
    deterministisch (kein KI-Anteil). Bei Themen-Duplikat fail-closed —
    nichts wird angelegt, der bestehende Task wird genannt."""
    agent = (agent or "").upper()
    if agent == "KIMI":
        agent = "SONNET"                      # Kimi → Sonnet (Legacy-Alias, 2026-06-15)
    if agent not in ("OPUS", "SONNET"):
        return f"ERROR: agent must be OPUS or SONNET (was: {agent!r})"
    if not handover_md or not handover_md.strip():
        return "ERROR: handover_md is empty — the full handover text is required."

    store = _store()
    log: List[str] = []
    task_type = ""
    try:
        if task_json:
            # Task-Felder parsen
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
            # ACK-Soft-Pfad-Gate: validiere das task_json gegen den Vertrag, BEVOR
            # der Store etwas mutiert. Bei Verletzung fail-closed mit exaktem Fehler
            # → der Agent-Loop reicht ihn dem Modell zurück (Reask).
            ack_err = _ack_validate(fields)
            if ack_err:
                return ("ERROR: task_json verletzt den ACK-Vertrag (nichts angelegt):\n"
                        + ack_err + "\n→ Felder korrigieren und stage_handover erneut aufrufen.")
            # Store: Dedup + ID + created_at + Schema, schreibt pending-JSON
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
            # Memory-Kontext aus Vergangenheits-Patterns anhängen (fail-soft)
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
            ho = Path(f"summaries/handovers/{tid}_{agent}.md")
            _atomic_write(ho, ho_md)
            log.append(f"handover geschrieben: {ho} ({len(ho_md)} Zeichen)")
        else:
            # Reiner Handover ohne Task-JSON — verlangt eine gültige task_id.
            if not task_id or not _TASK_ID_RE.match(task_id):
                return f"ERROR: without task_json a valid task_id is required (was: {task_id!r})"
            tid = task_id
            ho = Path(f"summaries/handovers/{tid}_{agent}.md")
            _atomic_write(ho, handover_md)
            log.append(f"handover geschrieben: {ho} ({len(handover_md)} Zeichen)")

        if set_active:
            store.project_active()
            log.append("active.md projected (= newest non-done handover)")

    except Exception as e:
        return f"ERROR: stage_handover fehlgeschlagen: {e}\nBisher:\n" + "\n".join(f"  - {l}" for l in log)

    result = f"OK: Handover {tid} ({agent}) bereitgestellt\n" + "\n".join(f"  - {l}" for l in log)
    # Pfad-Guard nur für Code-Tasks: bei type=documentation (Memory-Seed, Doku)
    # baut der Agent keinen Code → kein Dubletten-Risiko, der Check wäre nur Rauschen.
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
    return result


# ─── TaskStore: deterministische Task-Wahrheit (Modell 3) ─────
# Einzige Wahrheit: tasks/<status>/KGC-NNN.json. Das VERZEICHNIS ist die
# Status-Autorität; das status-Feld wird vom Store nachgezogen; active.md
# ist eine Projektion des in_progress-Handovers. Alle Mutationen laufen
# durch diese API, serialisiert (Single-Writer). KEIN KI-Anteil:
# ID-Vergabe, created_at, Schema, Doppel-ID- und Themen-Dedup sind Code.

class DuplicateTaskError(Exception):
    """Wird ausgelöst, wenn ein themengleicher Task bereits existiert."""
    def __init__(self, existing_id: str):
        super().__init__(f"Duplikat zu {existing_id}")
        self.existing_id = existing_id


class TaskStore:
    STATUSES = ("pending", "in_progress", "done")
    REQUIRED = ("type", "priority", "title", "description")
    # Generische Funktionswörter (de/en) — Domänenbegriffe bleiben erhalten.
    _STOP = {
        "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen",
        "einer", "eines", "und", "oder", "fur", "für", "zu", "mit", "von",
        "im", "in", "an", "auf", "aus", "bei", "nach", "uber", "über", "vor",
        "ist", "sind", "als", "wie", "the", "a", "an", "and", "or", "for",
        "to", "of", "with", "on", "at",
    }

    def __init__(self, root: str = ".", dedup_threshold: Optional[float] = None):
        # Default zur INSTANZIIERUNGSZEIT aus dem (ggf. per Config gesetzten)
        # Global lesen — nicht als Param-Default binden (würde den Import-Wert
        # einfrieren und spätere Config-Änderungen ignorieren).
        self.root            = Path(root)
        self.dedup_threshold = float(dedup_threshold if dedup_threshold is not None
                                     else TASKS_DEDUP_THRESHOLD)
        self._lock           = threading.RLock()

    # ── Pfade ────────────────────────────────────────────────
    def _dir(self, status: str) -> Path:
        return self.root / "tasks" / status

    def _path(self, task_id: str, status: str) -> Path:
        return self._dir(status) / f"{task_id}.json"

    def _find(self, task_id: str) -> Tuple[Optional[Path], Optional[str]]:
        for s in self.STATUSES:
            p = self._path(task_id, s)
            if p.exists():
                return p, s
        return None, None

    def _handover_path(self, task_id: str) -> Optional[Path]:
        d = self.root / "summaries" / "handovers"
        if not d.exists():
            return None
        hits = sorted(d.glob(f"{task_id}_*.md"))
        return hits[0] if hits else None

    # ── Identität ────────────────────────────────────────────
    def next_id(self) -> str:
        """Nächste freie ID über ALLE Status (monoton, nie wiederverwendet).
        Prefix aus dem (ggf. per Config gesetzten) TASK_PREFIX-Global zur
        Laufzeit lesen — nicht einfrieren."""
        pref = TASK_PREFIX
        id_re = re.compile(rf"^{re.escape(pref)}-(\d+)$")
        with self._lock:
            mx = 0
            for s in self.STATUSES:
                d = self._dir(s)
                if not d.exists():
                    continue
                for f in d.glob(f"{pref}-*.json"):
                    m = id_re.match(f.stem)
                    if m:
                        mx = max(mx, int(m.group(1)))
            return f"{pref}-{mx + 1}"

    # ── Dedup (rein deterministisch) ─────────────────────────
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

    # ── Lesen ────────────────────────────────────────────────
    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            p, s = self._find(task_id)
            if not p:
                return None
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["status"] = s          # Verzeichnis ist Autorität
                data.setdefault("id", task_id)
            return data

    def list(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            out: List[Dict[str, Any]] = []
            for s in ((status,) if status else self.STATUSES):
                d = self._dir(s)
                if not d.exists():
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

    # ── Mutationen ───────────────────────────────────────────
    @staticmethod
    def _now_iso() -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def create(self, fields: Dict[str, Any], *, force: bool = False,
               now_iso: Optional[str] = None) -> Dict[str, Any]:
        """Legt einen pending-Task an. Vergibt ID, stempelt created_at,
        validiert, lehnt Themen-Duplikat ab (außer force). Modell-gelieferte
        id/created_at/status werden IGNORIERT/überschrieben."""
        with self._lock:
            self._validate(fields)
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
        """Verschiebt das Task-JSON zwischen Status-Ordnern (atomar), zieht
        das status-Feld nach und projiziert active.md neu."""
        if to_status not in self.STATUSES:
            raise ValueError(f"invalid status: {to_status!r}")
        with self._lock:
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
        """active.md = Handover des neuesten NICHT-done Tasks (in_progress vor
        pending bei gleichem Zeitstempel), sonst idle. Reine Projektion — nie
        von Hand zu pflegen."""
        with self._lock:
            active = self.root / "vault" / "_Workflow" / "active.md"
            # in_progress rangiert vor pending; innerhalb nach created_at/id.
            cands = [(0, t) for t in self.list("pending")] + \
                    [(1, t) for t in self.list("in_progress")]
            if cands:
                cands.sort(key=lambda it: (it[0], it[1].get("created_at", ""), it[1].get("id", "")))
                ho = self._handover_path(cands[-1][1].get("id", ""))
                if ho and ho.exists():
                    _atomic_write(active, ho.read_text(encoding="utf-8"))
                    return
            _atomic_write(active, _IDLE_ACTIVE)


# Einziger, geteilter Store (ein Lock → serialisierte Mutationen über Makros
# UND Reconciler). root="." ist spät aufgelöst → greift im WORKDIR (nach chdir).
STORE: Optional["TaskStore"] = None

def _store() -> "TaskStore":
    global STORE
    if STORE is None:
        STORE = TaskStore()
    return STORE


# ─── Plattform-Modus (Shell + Syntax-Guidance aus EINER Quelle) ──
def _resolve_platform(mode: Optional[str]) -> str:
    """Löst 'auto' beim Start zu einem konkreten Modus auf. Ungültige Werte
    fallen sicher auf die OS-Detektion zurück."""
    m = (mode or "auto").strip().lower()
    if m in ("windows", "win", "nt"):
        return "windows"
    if m in ("linux", "posix", "unix", "mac", "darwin"):
        return "linux"
    # "auto" oder unbekannt → automatisch erkennen
    return "windows" if os.name == "nt" else "linux"


_LANG_NAMES = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish",
}


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
    """Dynamisch injizierte Laufzeit-Notiz — hält den Prompt-File neutral."""
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


# ─── Tool Ausführung ──────────────────────────────────────────
def run_tool(name: str, args: Dict[str, Any]) -> str:
    try:
        if name == "read_file":
            p = Path(args["path"])
            if not p.exists():
                return f"ERROR: Not found: {args['path']}"
            text = p.read_text(encoding="utf-8")
            # PERF-05: sehr große Dateien nicht ungekappt in den Kontext laden
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

            # HV-B: optionales Limit + harter Cap gegen Kontext-Bomben
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
            # Plattform-Modus bestimmt den Interpreter — konsistent mit der
            # Syntax-Guidance, die dem Modell injiziert wird.
            # stdin=DEVNULL: interaktive Befehle (z. B. cmd-`date` ohne Arg)
            # bekommen sofort EOF statt die volle Timeout-Zeit zu blockieren.
            if PLATFORM == "windows":
                argv = ["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", command]
                r = subprocess.run(
                    argv, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=timeout
                )
            else:
                r = subprocess.run(
                    command, shell=True, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, timeout=timeout
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
            # Echtes Regex (case-insensitive); bei ungültigem Muster sicherer
            # Fallback auf literalen Substring-Match.
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
                            fp.read_text(encoding="utf-8").splitlines(), 1
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
                return "[Memory] unavailable — is the memory stack running? `docker compose -f memory-stack/docker-compose.yml up -d`"
            return _MEMORY.query(
                args.get("query", ""),
                int(args.get("limit", 8)),
            )

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
        self._turn_think = True   # auto-Entscheidung je Turn (sicherer Default)
        # OPT-3: kumulierte Performance-Zähler über die Session
        self._perf = {"gens": 0, "prompt": 0, "completion": 0, "wall": 0.0, "last": ""}
        self._load_prompt(prompt_path)
        self._inject_platform_guidance()
        if self.onboarding:
            self._append_guidance(_onboarding_guidance())
        self._ensure_dirs()
        # Memory-Layer initialisieren (fail-soft, einmalig pro Prozess)
        global _MEMORY
        if _MemoryManager is not None and _MEMORY_CONFIG and _MEMORY is None:
            _MEMORY = _MemoryManager(_MEMORY_CONFIG)

    def _append_guidance(self, note: str):
        """Hängt eine Laufzeit-Notiz an den System-Prompt (oder legt eine
        minimale System-Nachricht an, falls --no-prompt). Geschieht VOR
        load_session, damit die Notiz beim Session-Resume erhalten bleibt."""
        sys_msg = next((m for m in self.messages if m.get("role") == "system"), None)
        if sys_msg:
            sys_msg["content"] = sys_msg["content"].rstrip() + "\n\n" + note
        else:
            self.messages.insert(0, {"role": "system", "content": note})

    def _inject_platform_guidance(self):
        self._append_guidance(_platform_guidance(self.platform))
        self._append_guidance(_language_guidance(LANGUAGE))

    # OPT-4: ein Completion-Call mit 1× Retry bei transientem API-Fehler
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
            kwargs["stream_options"] = {"include_usage": True}   # OPT-3: Usage im Stream
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
        try:
            Path(SESSION_FILE).write_text(
                json.dumps({"messages": self.messages}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            _ui_print(col(f"[OK] session saved: {SESSION_FILE}", C.GRAY))
        except Exception as e:
            _ui_print(col(f"[WARN] session not saved: {e}", C.YELLOW))

    @staticmethod
    def _sanitize_messages(msgs: List[Dict]) -> List[Dict]:
        """Repariert eine Nachrichtenliste so, dass die API-Invariante hält:
        - verwaiste tool-Antworten (ohne passenden tool_call) werden verworfen
        - assistant.tool_calls ohne (vollständige) tool-Antwort werden entfernt."""
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
        p = Path(SESSION_FILE)
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

        # PERF-06: Hysterese-Trimming für den vLLM Prefix-Cache.
        # Solange unter dem High-Water bleibt die Nachrichtenliste
        # UNVERÄNDERT → der Prefix nach dem System-Prompt ist stabil und
        # der KV-/Prefix-Cache des Servers greift über viele Runden.
        others_len = total_len([m for m in self.messages if m.get("role") != "system"])
        if others_len <= MAX_CTX_CHARS:
            return

        # Erst beim Überschreiten wird gekürzt — dann aber in einem Rutsch
        # bis auf das Low-Water, statt jede Runde ein bisschen. So entsteht
        # nur SELTEN eine Cache-Invalidierung statt bei jeder Iteration.
        system = [m for m in self.messages if m.get("role") == "system"]
        others = [m for m in self.messages if m.get("role") != "system"]

        # In ganzen "Runden" kürzen, damit assistant.tool_calls und die
        # zugehörigen tool-Antworten zusammenbleiben (API-Invariante).
        while total_len(others) > TRIM_TARGET_CHARS and len(others) > 1:
            cut = 1
            while cut < len(others) and others[cut].get("role") != "user":
                cut += 1
            # Safety: keine zweite User-Message → nicht weiter kürzen.
            # Sonst würde die einzige User-Message gelöscht → API 400
            # "No user query found in messages" (passiert z.B. bei Autoplan
            # wenn nur ein User-Turn im Kontext liegt aber viele Tool-Results).
            if cut >= len(others):
                break
            del others[:cut]

        self.messages = system + others

    def _ensure_dirs(self):
        for d in WORKSPACE_DIRS:
            Path(d).mkdir(parents=True, exist_ok=True)

    def _think_for(self, iteration: int) -> bool:
        if self.thinking_mode == "off":
            return False
        if self.thinking_mode == "all":
            return True
        if self.thinking_mode == "auto":
            # Thinking ist front-loaded → nur Iteration 0, und nur wenn die
            # Turn-Klassifikation es für nötig hält (sonst direkt ausführen).
            return iteration == 0 and self._turn_think
        return iteration == 0   # "first": immer nur die Planungs-Runde denkt

    @staticmethod
    def _classify_thinking(text: str) -> bool:
        """auto-Modus: True = Iteration 0 MIT Thinking.
        Sicherer Fehlermodus: im Zweifel True (denken). Nur bei klarer Routine
        (Status/Lookup/`done`) OHNE Planungs-Verb → False."""
        t = (text or "").lower().strip()
        if not t:
            return False
        if any(k in t for k in _PLANNING_KW):
            return True                      # Planung erkannt → denken
        if t == "done" or any(k in t for k in _ROUTINE_KW):
            return False                     # klare Routine → kein Thinking
        return True                          # Zweifel → denken

    # ── Generierung: Streaming (PERF-01) ──────────────────────
    def _generate(self, think: bool) -> Tuple[str, List[Dict], bool, Optional[Exception], Dict[str, Any]]:
        """Liefert (content, tool_calls, cancelled, err, metrics).
        Streaming-Pfad zeigt Inhalt live (Thinking herausgefiltert)."""
        if not self.stream:
            return self._generate_plain(think)

        chunk_q: _q.Queue = _q.Queue()
        err     = [None]
        usage   = [None]          # OPT-3: Usage aus dem letzten Chunk
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
        t_first = [None]          # OPT-3: Zeitpunkt erstes Token
        th = threading.Thread(target=_worker, daemon=True)
        th.start()

        tf        = _ThinkFilter()
        parts: List[str] = []
        tool_acc: Dict[int, Dict[str, str]] = {}
        prefix    = [False]
        tool_note = [False]   # B: einmaliger Live-Hinweis bei Tool-Generierung

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
                # B: Tote-Zeit füllen — sobald Tool-Tokens kommen (und noch kein
                # sichtbarer Text), einmal anzeigen, dass gearbeitet wird.
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
            _ui_print("")   # Abschluss-Zeilenumbruch nach gestreamtem Inhalt

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

    # ── Generierung: ohne Streaming (Vergleich/Fallback) ──────
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
        """#3: EIN deterministischer Abschluss-Marker am Turn-Ende — auf JEDEM
        Ausstiegspfad (Erfolg, Abbruch, Fehler, Max-Iter, interner Crash).
        Garantiert per try/finally in run(), damit „bereit für Eingabe" nie
        ausbleibt. WICHTIG: kein \\n INNERHALB von col() — sonst wird der
        Farbcode beim Zeilen-Split abgetrennt; daher Leerzeile separat."""
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

    # ── Agent Loop ────────────────────────────────────────────
    def run(self, user_input: str):
        global _TURN_DID_ADVANCE
        _CANCEL_EVENT.clear()
        self.messages.append({"role": "user", "content": user_input})
        # Neuer Operator-Turn → Auto-Plan-Guard zurücksetzen. Ein advance_pipeline
        # in DIESEM Turn setzt ihn wieder; ein darauffolgendes stage_handover im
        # selben Turn wird dann blockiert (solange autoplan aus).
        _TURN_DID_ADVANCE = False

        # auto-Modus: einmal pro Turn entscheiden, ob Iteration 0 denkt
        self._turn_think = self._classify_thinking(user_input)

        turn = {"t0": time.time(), "gens": 0, "prompt": 0, "completion": 0}
        # Turn-Ausgang — wird in finally IMMER als Statuszeile gedruckt.
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

            # OPT-3: Perf-Zeile + Kumulierung (Session + dieser Turn)
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

            # PERF-02: NUR den bereinigten Inhalt persistieren (kein <think>)
            cleaned = clean(content)
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
                try:
                    args = json.loads(t["arguments"]) if t["arguments"] else {}
                except json.JSONDecodeError:
                    args = {}

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
          # Schleife regulär durchlaufen → Max-Iterationen (outcome bleibt "max")
        except Exception as e:
            # Fängt unerwartete Fehler ab, damit der Agent-Thread NICHT stirbt
            # und der Turn trotzdem einen Abschluss-Marker bekommt.
            outcome = {"kind": "crash", "detail": repr(e)}
        finally:
            _status["thinking"] = False   # Toolbar zurück auf idle (auch bei Crash)
            self._print_turn_end(turn, outcome)

    # ── Manuelle Befehle ──────────────────────────────────────
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
        return r if not r.startswith("ERROR") else col(r, C.RED)

    def manual_ls(self, path: str = ".") -> str:
        return run_tool("list_directory", {"path": path})

    def clear_context(self) -> str:
        system = next((m for m in self.messages if m["role"] == "system"), None)
        self.messages      = [system] if system else []
        self.last_response = ""
        return col("[OK] context reset (the system prompt stays).", C.YELLOW)

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
    reload           reload gx10.py (the session stays)
    watcher on|off        enable / disable the feedback watcher
    autopilot on|off      toggle autopilot (auto-launch of Claude)
    autoplan on [N]       autonomous planning (optional: max N tasks, then stop)
    autoplan off          stop autoplan + reset the counter
    log-terminal on|off   open a live-log window on every autopilot start
    help / exit

  Everything else → agent loop
"""

# ─── `config`-Befehl: autoritative, effektiv geladene Konfiguration ──
def _render_config() -> str:
    """Zeigt die EFFEKTIV geladene Config (echte Werte, nicht Doku/Prompt) +
    Quelle. Deterministisch, ohne LLM. Secrets werden nicht ausgegeben."""
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
        col(f"  paths         : prompt={pa['system_prompt']} · workdir={pa['workdir']} · session={pa['session_file']}", C.GRAY),
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


# ─── Dispatcher ───────────────────────────────────────────────
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
            # Optionale Anzahl: "autoplan on 5"
            if n_arg is not None:
                try:
                    AUTOPILOT_MAX_TASKS = int(n_arg)
                    if _EFFECTIVE_CFG: _EFFECTIVE_CFG["autopilot"]["autoplan_max_tasks"] = AUTOPILOT_MAX_TASKS
                except ValueError:
                    _ui_print(col(f"[AUTOPLAN] invalid number: {n_arg!r}", C.RED))
                    return  # type: ignore
            _AUTOPLAN_DONE     = 0   # Zähler immer auf null beim Aktivieren
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
    else:
        agent.run(user_input)

# ─── Autopilot: Handover → Claude starten (API-frei) ─────────
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
    """Beendet die für task_id gestartete claude-Session (inkl. Kindprozesse),
    falls noch aktiv. FAIL-SAFE: jeder Fehler wird geschluckt — der advance darf
    NIE daran scheitern. Der Monitor-Thread gibt Slot + Registry frei."""
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
    d = Path("summaries/handovers")
    if not d.exists():
        return None
    hits = sorted(d.glob(f"{task_id}_*.md"))
    return hits[0] if hits else None

def _agent_from_handover(name: str) -> str:
    m = _HO_AGENT_RE.search(name)
    if not m:
        return ""
    agent = m.group(1).upper()
    return "SONNET" if agent == "KIMI" else agent   # Legacy _KIMI.md → Sonnet

def _task_agent(task: Dict[str, Any]) -> str:
    """Der dem Task ZUGEWIESENE Agent (OPUS/SONNET) — aus assigned_to, sonst
    aus dem vorhandenen Handover. Verhindert, dass ein fremdes Agenten-Feedback
    einen OPUS-Task abschließt. "kimi" (Legacy) wird auf SONNET normalisiert."""
    a = (task.get("assigned_to") or "").lower()
    if "opus" in a:
        return "OPUS"
    if "sonnet" in a or "kimi" in a:          # Kimi → Sonnet (Legacy-Alias, 2026-06-15)
        return "SONNET"
    ho = _find_handover(task.get("id", ""))
    return _agent_from_handover(ho.name) if ho else ""

def _parse_handover_meta(path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Liest (model, effort) aus dem Handover-Frontmatter (`to:` / `effort:`)."""
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
    """Startet `claude --print` für einen Handover und schaltet den Task auf
    in_progress. Subprozess läuft detached; ein Monitor-Thread gibt den
    Nebenläufigkeits-Slot beim Exit frei. Bei Fehler wird der Slot sofort
    freigegeben. (Der Reconciler hat den Slot bereits reserviert.)"""
    ho = _find_handover(task_id)
    if not ho:
        _autopilot_release()
        _ui_print(col(f"  [AUTO] handover for {task_id} vanished — launch discarded", C.YELLOW))
        return
    model, effort = _parse_handover_meta(ho)
    effort = effort or AUTOPILOT_DEFAULT_EFFORT
    # Kimi wurde durch Sonnet ersetzt (2026-06-15): jede Rest-Referenz läuft als Sonnet.
    if agent == "KIMI":
        agent = "SONNET"
    prompt = (f"Lies und bearbeite autonom den Handover {ho.name} in "
              f"summaries/handovers/. Folge den Anweisungen in .claude/CLAUDE.md.")
    if model and str(model).startswith("kimi"):
        model = None                          # Legacy-Kimi-Modell → Default (Sonnet/Opus)
    model  = model or ("claude-opus-4-8" if agent == "OPUS" else "claude-sonnet-4-6")
    argv = [AUTOPILOT_CLAUDE_BIN, "--model", model, "--effort", effort]
    extra = list(AUTOPILOT_EXTRA_ARGS)
    if AUTOPILOT_STREAM:
        # Live-Streaming: stream-json BRAUCHT --verbose (sonst bricht claude ab).
        # Ausgabe geht weiter in die Log-DATEI (kein Pipe-Read) → kein Deadlock.
        if "--verbose" not in extra:
            extra.append("--verbose")
        if "--output-format" not in extra:
            extra += ["--output-format", "stream-json"]
    argv += extra + ["--print", prompt]
    logdir = Path(AUTOPILOT_LOGS_DIR)
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{task_id}_{agent}.log"
    try:
        lf = open(logfile, "w", encoding="utf-8")
        # PYTHONIOENCODING=utf-8: verhindert cp1252-Crash bei Non-ASCII-Zeichen
        # (z. B. → in Handover-Texten) auf Windows. Kimi und Claude erben beide.
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

    # Log-Terminal: neues Konsolenfenster mit Get-Content -Wait öffnen (nur Windows).
    # Versucht zuerst Windows Terminal (wt), fällt auf eigenständiges PowerShell zurück.
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
        # Feedback-Check: Warnung wenn Claude beendet hat ohne Feedback zu schreiben.
        # Kein Alert wenn Task bereits done (Advance lief vor _wait → Feedback schon gelöscht).
        try:
            fb_dir = Path("summaries/feedback")
            found = list(fb_dir.glob(f"{task_id}_*-feedback.md")) if fb_dir.exists() else []
            if not found:
                # Task bereits abgeschlossen? → kein Alert (Advance hat Feedback korrekt gelöscht)
                t = _store().get(task_id)
                already_done = t is not None and t.get("status") == "done"
                if not already_done:
                    _ui_print(col(
                        f"  ⚠ [AUTO] {task_id}: claude beendet (exit {rc}) "
                        f"aber KEIN Feedback in summaries/feedback/ — Task bleibt in_progress!",
                        C.RED))
        except Exception:
            pass
    threading.Thread(target=_wait, daemon=True).start()


# ─── Feedback-Reconciler (Polling statt Event-Trigger) ───────
# Liest jeden Tick den WAHREN Zustand: für jeden in_progress-Task wird eine
# vollständige Feedback-Datei gesucht und der Abschluss DETERMINISTISCH (ohne
# LLM) ausgelöst. Verpasst/dupliziert keine FS-Events, ist idempotent.
# Autopilot-Seite (optional): pending-Task mit Handover → claude starten.
_FB_RE = re.compile(r"_(\w+)-feedback\.md$")

def _reconcile_once(store: "TaskStore", enqueue, seen_mtime: Dict[str, float],
                    enqueued: set, launch_enqueue=None, launched: Optional[set] = None):
    """Ein Reconciler-Tick. seen_mtime/enqueued/launched sind über Ticks
    persistent (Vollständigkeits- bzw. Dedup-Gate)."""
    # ── Launch-Seite (Autopilot): pending + Handover → claude starten ──
    if AUTOPILOT_ENABLED and launch_enqueue is not None and launched is not None:
        for task in sorted(store.list("pending"),
                           key=lambda t: (t.get("created_at", ""), t.get("id", ""))):
            tid = task.get("id") or ""
            ho = _find_handover(tid)
            if not ho:
                continue                      # kein Handover → noch nicht startbar
            # Launch-Dedup per (tid, Handover-mtime) statt nur tid: ein zurück-
            # gezogener + unter GLEICHER ID re-stageter Task hat einen neuen
            # Handover (neue mtime) → wird neu gelauncht. Sonst würde ein einmal
            # gelaunchter (evtl. gecrashter) Task NIE wieder starten (Bug: KGC-387
            # nach OPUS-Rate-Limit-Crash + Re-Stage als KIMI).
            try:
                ho_key = (tid, ho.stat().st_mtime)
            except OSError:
                ho_key = (tid, 0.0)
            if ho_key in launched:
                continue
            if AUTOPILOT_MAX_CONCURRENT and _autopilot_active() >= AUTOPILOT_MAX_CONCURRENT:
                break                         # kein Slot frei → später erneut
            agent = _agent_from_handover(ho.name)
            if agent not in ("OPUS", "SONNET"):
                continue
            launched.add(ho_key)
            _autopilot_reserve()              # Slot reservieren (Worker startet, Monitor gibt frei)
            launch_enqueue(tid, agent)

    # ── Feedback-Seite: pending ODER in_progress + Feedback DES ZUGEWIESENEN
    #    Agenten → advance. WICHTIG: auch `pending` scannen — ein Task, der
    #    manuell (außerhalb autopilot) abgearbeitet wurde, bleibt in `pending`
    #    (kein pending→in_progress-Launch). Würde nur `in_progress` gescannt,
    #    bliebe so ein Task mit fertigem Feedback ewig liegen (Bug: KGC-387).
    d = Path(WATCHER_FEEDBACK_DIR)
    if not d.exists():
        return
    # Warnung für Dateien die nicht dem Muster {task_id}_{agent}-feedback.md entsprechen
    # (z.B. Analyse-Dokumente die Qwen fälschlicherweise in den Feedback-Inbox schreibt)
    for orphan in d.iterdir():
        if orphan.is_file() and not _FB_RE.search(orphan.name):
            warn_key = f"__orphan_{orphan.name}"
            if warn_key not in enqueued:
                enqueued.add(warn_key)
                _ui_print(col(
                    f"  ⚠ [WATCHER] Fremde Datei in feedback-Inbox: {orphan.name} "
                    f"— kein Advance möglich (kein task_id_agent-Format). "
                    f"Analyse-Dokumente gehören nach vault/_Workflow/analysis/",
                    C.YELLOW))
    for task in (store.list("pending") + store.list("in_progress")):
        tid = task.get("id") or ""
        agent = _task_agent(task)            # erwarteter Agent (nicht aus beliebigem Dateinamen!)
        if agent not in ("OPUS", "SONNET"):
            continue
        fb = d / f"{tid}_{agent}-feedback.md"  # NUR das Feedback des zugewiesenen Agenten
        if not fb.exists():
            continue
        key = (tid, agent)
        if key in enqueued:
            continue
        try:
            mt = fb.stat().st_mtime
        except OSError:
            continue
        # Vollständigkeits-Gate: mtime über einen Tick stabil → fertig geschrieben
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
def _build_app() -> Application:
    input_buf = Buffer(name="input_buf", multiline=False)
    kb        = KeyBindings()

    @kb.add("enter")
    def _enter(event):
        text = input_buf.text.strip()
        input_buf.reset()
        if text:
            _ui_print(col(f"\n[You] > {text}", C.BOLD))
        _INPUT_QUEUE.put(text)

    @kb.add("c-c")
    def _ctrl_c(event):
        if _status["thinking"]:
            _CANCEL_EVENT.set()
        else:
            _INPUT_QUEUE.put("\x03")

    @kb.add("c-d")
    def _ctrl_d(event):
        _INPUT_QUEUE.put("\x04")

    layout = Layout(
        HSplit([
            Window(
                content=FormattedTextControl(_get_output, focusable=False),
                wrap_lines=True,
            ),
            Window(height=1, char="─"),
            Window(
                content=BufferControl(buffer=input_buf, focusable=True),
                height=1,
                get_line_prefix=lambda i, wrap_count: "│ [You] > ",
            ),
            Window(height=1, char="─"),
            Window(
                content=FormattedTextControl(_toolbar, focusable=False),
                height=3,
            ),
        ])
    )

    return Application(
        layout=layout,
        key_bindings=kb,
        full_screen=True,
        refresh_interval=UI_REFRESH_INTERVAL,
        mouse_support=False,
    )


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


def _agent_thread(agent: GX10, app: Application):
    """Agent-Loop läuft im Hintergrund-Thread."""
    global _AUTOPLAN_DONE, AUTOPILOT_AUTOPLAN, _RELOAD_FLAG
    while True:
        user_input = _INPUT_QUEUE.get().strip()

        if user_input == "\x04":            # Ctrl+D
            agent.save_session()
            app.exit()
            return

        if user_input == "\x03":            # Ctrl+C ohne aktiven Call
            _ui_print(col("  (tippe 'exit' zum Beenden)", C.GRAY))
            continue

        # Strukturierter Reconciler-Befehl: deterministischer Abschluss, KEIN LLM.
        if user_input.startswith(_ADVANCE_CMD):
            parts = user_input.split("\x00")   # ['', 'advance', tid, agent]
            if len(parts) >= 4:
                tid, agent_adv = parts[2], parts[3]
                _ui_print(col(f"  → advance_pipeline({tid}, {agent_adv}) [auto]", C.MAGENTA))
                try:
                    res = _advance_pipeline(tid, agent_adv)
                except Exception as e:
                    res = f"ERROR: {e!r}"
                ok = res.startswith("OK")
                _ui_print(col(f"  {'✓' if ok else '✗'} {res.splitlines()[0]}",
                              C.GREEN if ok else C.RED))
                # Autoplan: nach erfolgreichem Advance bei leerer Queue den nächsten
                # Task planen lassen. Monolith-Politik: nur wenn Autopilot AN (Launch
                # lokal). Logik geteilt mit dem Server über _autoplan_tick.
                if ok and AUTOPILOT_ENABLED:
                    _autoplan_tick(tid, lambda p: _INPUT_QUEUE.put(p))
            continue

        # Autopilot-Launch: startet claude für einen Handover (detached).
        if user_input.startswith(_LAUNCH_CMD):
            parts = user_input.split("\x00")   # ['', 'launch', tid, agent]
            if len(parts) >= 4:
                try:
                    _do_launch(parts[2], parts[3])
                except Exception as e:
                    _autopilot_release()
                    _ui_print(col(f"  ✗ [AUTO] Launch-Fehler: {e!r}", C.RED))
            continue

        if not user_input:
            continue

        if user_input.lower() == "reload":
            global _RELOAD_FLAG
            _RELOAD_FLAG = True
            agent.save_session()
            _ui_print(col("[OK] restart — session saved.", C.GREEN))
            app.exit()
            return

        if user_input.lower() == "exit":
            agent.save_session()
            app.exit()
            return

        # Absicherung: eine unerwartete Exception in der Verarbeitung darf den
        # Worker-Thread NICHT stillschweigend killen (sonst wirkt die CLI
        # „bereit", verarbeitet aber keine Eingaben mehr).
        try:
            _dispatch(agent, user_input)
        except Exception as e:
            _status["thinking"] = False
            _ui_print(col(f"\n  ✗ FEHLER (Verarbeitung): {e!r}", C.RED))

# ─── Konfiguration: Laden, Mergen, Precedence ────────────────
# Wert-Precedence (schwach → stark): Code-Defaults < Config-Datei < Env < CLI.

def _code_defaults() -> Dict[str, Any]:
    """Schnappschuss der Modul-Konstanten als unterste Precedence-Stufe."""
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
            "session_file":  SESSION_FILE,
            "code_root":     CODE_ROOT,
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
            "max_iterations":    MAX_ITERATIONS,
            "max_ctx_chars":     MAX_CTX_CHARS,
            "trim_target_chars": TRIM_TARGET_CHARS,
            "max_file_chars":    MAX_FILE_CHARS,
            "list_dir_hard_cap": LIST_DIR_HARD_CAP,
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
    """Rekursives Merge: override gewinnt; verschachtelte Dicts werden
    feldweise zusammengeführt statt ersetzt. Liefert eine frische Struktur."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_config_source(cli_config: Optional[str]) -> Optional[Path]:
    """Fundort-Precedence: --config (Datei ODER Verzeichnis) > Env GX10_CONFIG
    > ./conf/ > ./gx10.config.json > <SCRIPT_DIR>/conf/ > <SCRIPT_DIR>/gx10.config.json.
    Ein Verzeichnis wird als entzerrte Domain-Config geladen (mit Includes)."""
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
    """Lädt eine Config aus Datei ODER Verzeichnis und merged Includes
    rekursiv. Regeln:
      • Verzeichnis mit `gx10.config.json` → diese Index-Datei laden.
      • Verzeichnis ohne Index → alle `*.json` (sortiert) deep-mergen.
      • Datei mit `include: [...]` → Einträge (relativ zur Datei) zuerst
        mergen, danach die eigenen Inline-Blöcke (Inline gewinnt).
    Liefert denselben flachen cfg-Baum wie eine Einzeldatei."""
    if not source:
        return {}
    _seen = _seen if _seen is not None else set()
    p = Path(source)
    rp = str(p.resolve())
    if rp in _seen:                      # Zyklusschutz
        return {}
    _seen.add(rp)

    if p.is_dir():
        idx = p / "gx10.config.json"
        if idx.is_file():
            return _load_config_tree(idx, _seen)
        merged: Dict[str, Any] = {}
        for f in sorted(p.glob("*.json")):
            merged = _deep_merge(merged, _load_config_tree(f, _seen))
        return merged

    if p.is_file():
        data = _read_json_dict(p)
        includes = data.pop("include", [])
        # Kommentar-/Meta-Keys (_-Präfix) nie in cfg übernehmen
        data = {k: v for k, v in data.items() if not k.startswith("_")}
        merged = {}
        if isinstance(includes, list):
            for inc in includes:
                merged = _deep_merge(merged, _load_config_tree(p.parent / inc, _seen))
        # eigene Inline-Blöcke übersteuern die Includes
        return _deep_merge(merged, data)

    print(col(f"  [WARN] config source is neither a file nor a directory: {p}", C.YELLOW))
    return {}


def _apply_env(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Env-Override (Stufe 3). Nur explizit gesetzte GX10_*-Variablen.
    Der API-Key selbst kommt NICHT hierher, sondern erst in main() aus
    der via api_key_env benannten Variable."""
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
    _truthy = lambda v: v.strip().lower() in ("1", "true", "yes", "on", "an")
    setif("GX10_ONBOARDING", "onboarding", "enabled", _truthy)
    setif("GX10_AUTOPILOT",  "autopilot",  "enabled", _truthy)
    setif("GX10_AUTOPILOT_STREAM",    "autopilot", "stream",          _truthy)
    setif("GX10_AUTOPILOT_TERMINATE", "autopilot", "terminate_on_advance", _truthy)
    setif("GX10_AUTOPILOT_AUTOPLAN",       "autopilot", "autoplan",           _truthy)
    setif("GX10_AUTOPILOT_MAX_TASKS",      "autopilot", "autoplan_max_tasks", int)
    setif("GX10_AUTOPILOT_LOG_TERMINAL", "autopilot", "log_terminal",  _truthy)
    return cfg


def _apply_cli(cfg: Dict[str, Any], args) -> Dict[str, Any]:
    """CLI-Override (Stufe 4, stärkste). Nur tatsächlich gesetzte Flags."""
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
    """Schreibt die gemergte Config in die Modul-Globals zurück, sodass die
    bestehenden Referenzen (run_tool, Makros, _trim_context, _classify_thinking,
    Watcher, UI …) unverändert weiterlaufen."""
    global DEFAULT_BASE_URL, DEFAULT_MODEL, API_KEY_ENV, SESSION_FILE, CODE_ROOT
    global PLATFORM_MODE, PLATFORM, TASKS_DEDUP_THRESHOLD, ONBOARDING_MODE, TASK_PREFIX, _TASK_ID_RE, ACK_ENABLED, LODESTAR_ENABLED
    global AUTOPILOT_ENABLED, AUTOPILOT_CLAUDE_BIN, AUTOPILOT_EXTRA_ARGS
    global AUTOPILOT_DEFAULT_EFFORT, AUTOPILOT_LOGS_DIR, AUTOPILOT_MAX_CONCURRENT, AUTOPILOT_STREAM, AUTOPILOT_TERMINATE_ON_ADVANCE, AUTOPILOT_AUTOPLAN, AUTOPILOT_MAX_TASKS, AUTOPILOT_LOG_TERMINAL
    global TEMPERATURE, MAX_TOKENS, RETRY_BACKOFF, LANGUAGE
    global MAX_ITERATIONS, MAX_CTX_CHARS, TRIM_TARGET_CHARS, MAX_FILE_CHARS, LIST_DIR_HARD_CAP
    global _PLANNING_KW, _ROUTINE_KW, WORKSPACE_DIRS, _IDLE_ACTIVE
    global WATCHER_FEEDBACK_DIR, _WATCHER_ENABLED, RECONCILER_INTERVAL
    global SPINNER_FRAMES, UI_REFRESH_INTERVAL, _UI_MAX_LINES, _UI_LINES
    global _MEMORY_CONFIG

    conn, paths, gen = cfg["connection"], cfg["paths"], cfg["generation"]
    ctx, ta, ws       = cfg["context"], cfg["thinking_auto"], cfg["workspace"]
    wa, ui            = cfg["watcher"], cfg["ui"]

    DEFAULT_BASE_URL = conn["base_url"]
    DEFAULT_MODEL    = conn["model"]
    API_KEY_ENV      = conn.get("api_key_env", API_KEY_ENV)
    SESSION_FILE     = paths["session_file"]
    CODE_ROOT        = paths.get("code_root", CODE_ROOT)

    PLATFORM_MODE = cfg["platform"]["mode"]
    PLATFORM      = _resolve_platform(PLATFORM_MODE)   # einmalige Auflösung von 'auto'

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

    _PLANNING_KW = tuple(ta["planning_keywords"])
    _ROUTINE_KW  = tuple(ta["routine_keywords"])

    WORKSPACE_DIRS = list(ws["dirs"])
    _IDLE_ACTIVE   = ws["idle_marker"]

    # Memory-Config: Datei (conf/memory/memory.json) ODER Env (GX10_MEMORY_URL).
    # Optional — ohne base_url bleibt _MEMORY_CONFIG leer → Memory aus (Hooks inert).
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
def main():
    parser = argparse.ArgumentParser(
        description="GX10 Orchestrator v3 (Performance-Fixes, konfigurierbar)")
    parser.add_argument("--config",     default=None,
                        help="JSON config path (else env GX10_CONFIG / "
                             "./gx10.config.json / <SCRIPT_DIR>/gx10.config.json)")
    parser.add_argument("--workdir",    default=None,
                        help="working directory (tasks/, summaries/, vault/, session); default '.'")
    parser.add_argument("--base-url",   default=None)
    parser.add_argument("--api-key",    default=None,
                        help="override the API key ad-hoc (else from env GX10_API_KEY)")
    parser.add_argument("--model",      default=None)
    parser.add_argument("--prompt",     default=None)
    parser.add_argument("--no-prompt",  action="store_true")
    parser.add_argument("--fresh",      action="store_true",
                        help="ignore the saved session")
    parser.add_argument("--no-stream",  action="store_true",
                        help="disable streaming (compare against v1 behaviour)")
    parser.add_argument("--max-tokens", type=int, default=None,
                        help=f"output token limit (default {MAX_TOKENS})")
    parser.add_argument("--thinking",   choices=["auto", "first", "off", "all"], default=None,
                        help="thinking mode: auto=decide per turn (default, thinks only "
                             "when planning, yes if in doubt), first=always a planning round, "
                             "off=never, all=always")
    parser.add_argument("--platform",   choices=["auto", "windows", "linux"], default=None,
                        help="shell/syntax mode for execute_command "
                             "(default auto = detect at startup)")
    parser.add_argument("--onboarding",    dest="onboarding", action="store_const", const=True,
                        default=None, help="onboarding mode ON (duplicate pre-check before handover)")
    parser.add_argument("--no-onboarding", dest="onboarding", action="store_const", const=False,
                        help="onboarding mode OFF")
    parser.add_argument("--autopilot",     dest="autopilot", action="store_const", const=True,
                        default=None, help="autopilot ON (launches Claude for handovers autonomously)")
    parser.add_argument("--no-autopilot",  dest="autopilot", action="store_const", const=False,
                        help="autopilot OFF")
    args = parser.parse_args()

    if os.name == "nt":
        os.system("")

    # ── Config laden & Precedence anwenden (Code < Datei/conf < Env < CLI) ──
    cfg      = _code_defaults()
    cfg_path = _resolve_config_source(args.config)
    cfg      = _deep_merge(cfg, _load_config_tree(cfg_path))
    cfg      = _apply_env(cfg)
    cfg      = _apply_cli(cfg, args)
    _apply_config(cfg)
    global _EFFECTIVE_CFG, _CFG_SOURCE
    _EFFECTIVE_CFG, _CFG_SOURCE = cfg, cfg_path   # für den `config`-Befehl

    # ── Prompt VOR dem chdir absolut auflösen (relativ → SCRIPT_DIR) ──
    prompt_cfg = "" if args.no_prompt else cfg["paths"]["system_prompt"]
    prompt_abs = ""
    if prompt_cfg:
        pp = Path(prompt_cfg).expanduser()
        prompt_abs = str(pp if pp.is_absolute() else (SCRIPT_DIR / pp))

    # ── WORKDIR bestimmen und hineinwechseln (relative tasks/… bleiben gültig) ──
    workdir = Path(cfg["paths"]["workdir"]).expanduser().resolve()
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        os.chdir(workdir)
    except Exception as e:
        print(col(f"  [FEHLER] WORKDIR {workdir}: {e}", C.RED))
        sys.exit(1)

    # ── API-Key: nur aus Env (api_key_env) bzw. --api-key (ad-hoc) ──
    api_key       = args.api_key or os.environ.get(cfg["connection"]["api_key_env"]) or DEFAULT_API_KEY
    base_url      = cfg["connection"]["base_url"]
    model         = cfg["connection"]["model"]
    stream        = bool(cfg["generation"]["stream"])
    max_tokens    = int(cfg["generation"]["max_tokens"])
    thinking_mode = cfg["generation"]["thinking_mode"]

    Cy = "\033[96m"; Gy = "\033[90m"; Bo = "\033[1m"; R = "\033[0m"
    print(f"{Cy}{Bo}  Ironclad — Orchestrator CLI{R}")
    print(f"{Gy}  Model  : {model}  |  qwen3_coder{R}")
    print(f"{Gy}  URL    : {base_url}{R}")
    print(f"{Gy}  Stream : {'on' if stream else 'off'}  |  "
          f"thinking={thinking_mode}  |  max_tokens={max_tokens}{R}")
    print(f"{Gy}  Platf.: {PLATFORM}"
          + (f" (aus '{PLATFORM_MODE}' erkannt)" if PLATFORM_MODE == 'auto' else "")
          + (f"  |  Onboarding: AN" if ONBOARDING_MODE else "")
          + f"{R}")
    if AUTOPILOT_ENABLED:
        print(f"\033[93m  autopilot: ON — launches Claude autonomously "
              f"(max_concurrent={AUTOPILOT_MAX_CONCURRENT}, {' '.join(AUTOPILOT_EXTRA_ARGS)}){R}")
    if AUTOPILOT_ENABLED and AUTOPILOT_AUTOPLAN:
        limit_str = f", max_tasks={AUTOPILOT_MAX_TASKS}" if AUTOPILOT_MAX_TASKS > 0 else ", unlimited"
        print(f"\033[93m  autoplan: ON{limit_str}{R}")
        print(f"\033[91m  ⚠ WARNING: never use autoplan with a paid API plan! "
              f"Local vLLM instances only.{R}")
    print(f"{Gy}  Prompt : {prompt_abs or '— (none)'}{R}")
    print(f"{Gy}  WORKDIR: {workdir}{R}")
    print(f"{Gy}  Config : {cfg_path or '— (code defaults)'}{R}")
    print()
    if not HAS_PT:
        print("\033[93m  [WARN] pip install prompt_toolkit\033[0m")
        print()

    agent = GX10(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt_path=prompt_abs,
        stream=stream,
        max_tokens=max_tokens,
        thinking_mode=thinking_mode,
        platform=PLATFORM,
        onboarding=ONBOARDING_MODE,
    )

    if not args.fresh and Path(SESSION_FILE).exists():
        n = agent.load_session()
        if n > 0:
            print(f"[OK] session resumed — {n} messages loaded")
            print("     (--fresh for a new session)")

    if HAS_PT:
        global _UI_APP
        app     = _build_app()
        _UI_APP = app

        t = threading.Thread(target=_agent_thread, args=(agent, app), daemon=True)
        t.start()

        Path(WATCHER_FEEDBACK_DIR).mkdir(parents=True, exist_ok=True)
        recon_stop = threading.Event()
        rt = threading.Thread(target=_reconciler_loop,
                              args=(recon_stop, RECONCILER_INTERVAL), daemon=True)
        rt.start()
        state = "aktiv" if _WATCHER_ENABLED else "deaktiviert"
        col_  = C.GREEN if _WATCHER_ENABLED else C.YELLOW
        _ui_print(col(f"[OK] feedback reconciler ready ({state}, polling every "
                      f"{RECONCILER_INTERVAL:.0f}s — 'watcher on/off')", col_))

        app.run()
        recon_stop.set()

        if _RELOAD_FLAG:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    else:
        print(col(HELP, C.YELLOW))
        while True:
            try:
                user_input = input("\n[You] > ").strip()
            except KeyboardInterrupt:
                print("  (Ctrl+C — type 'exit' to quit)")
                continue
            except EOFError:
                agent.save_session()
                break
            if not user_input:
                continue
            if user_input.lower() == "exit":
                agent.save_session()
                break
            _dispatch(agent, user_input)


if __name__ == "__main__":
    main()
