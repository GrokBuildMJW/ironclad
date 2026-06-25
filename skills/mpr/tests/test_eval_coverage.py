"""§2 collection gate + the §2.4 'reiten statt duplizieren' boundary guard (Spec 08 §2/§2.4/§7).

This is the Ev-2 Sammel-Gate. The §2.1–§2.9 spec test-names are covered by the per-unit Phase-1 suites
under different names (full reconcile map in TASKS.md, Ev-1+Ev-2 receipt); this file enforces the two
structural invariants that the per-unit suites don't:

  * ``test_mpr_brings_no_own_dispatcher`` — AST proof that the plugin builds NO own fan-out /
    dispatcher / governor / agent-CLI adapter / subprocess-pool; it RIDES ironclad's primitives
    (Spec 08 §2.4, the boundary check listed in the Merge-Gate §7). AST-node based, never a substring
    grep — comments/docstrings can never trip it; the deny-rule + allowlist are grounded against the
    real MVP shape so it has ZERO false positives yet catches a genuine future rebuild.
  * ``test_component_test_files_present`` — a §2-component inventory so a test file can't vanish silently.

A skip stub tracks the §2.4 P0 dispatch-seam (deferred until ``run_mpr`` is rewired to call P0 — the
in-engine MVP makes that seam-test vacuous today; see TASKS.md 'P0-Wiring').
"""
from __future__ import annotations

import ast
from pathlib import Path

_MPR_ROOT = Path(__file__).resolve().parents[1]               # skills/mpr/

# ── §2.4 deny-rule (grounded against the real code — AST nodes only) ─────────────────────────────────
# Allowlist is deliberately tiny; test_allowlisted_symbols_still_exist pins it so a rename forces review.
_ALLOWLIST_ADAPTER = {"_ClassifierAdapter"}          # LLM-port adapter, NOT an agent-CLI AgentAdapter
_ALLOWLIST_DISPATCHER = {"ProviderDispatcher"}       # the P0 type MPR may reference once dispatch is wired
_FORBIDDEN_IMPORT_MODULES = {"subprocess", "multiprocessing", "concurrent.futures", "asyncio"}
_FORBIDDEN_IMPORT_HEADS = {"subprocess", "multiprocessing", "concurrent"}   # first dotted segment
_FORBIDDEN_IMPORT_NAMES = {"ThreadPoolExecutor", "ProcessPoolExecutor", "Popen"}
_FORBIDDEN_DEF_NAMES = {"_plan_concurrency", "_build_agent_argv", "build_agent_argv"}
# `import threading` is NOT banned — loader.py legitimately uses threading.Lock() for the registry
# singleton (mirrors ack.Registry). A thread-POOL rebuild is caught at the call site instead
# (threading.Thread(...)), so the Lock passes while a hand-rolled fan-out does not.
_FORBIDDEN_CALL_ATTRS = {("subprocess", "Popen"), ("subprocess", "run"), ("subprocess", "call"),
                         ("subprocess", "check_output"), ("os", "fork"), ("os", "system"),
                         ("os", "popen"), ("threading", "Thread")}
_FORBIDDEN_CALL_TAILS = {"ThreadPoolExecutor", "ProcessPoolExecutor"}
# Out of scope BY DESIGN (this is a code-hygiene boundary guard, not a sandbox against an adversarial
# author): dynamic import (__import__/importlib), exec/eval-laundered imports, os.posix_spawn/execv, and
# an arbitrarily-named fan-out function (e.g. `def _scatter`) — an AST name/import guard cannot catch the
# last without shape analysis. The realistic accidental-rebuild vectors (subprocess/concurrent.futures/
# multiprocessing imports — alias-proof; *Dispatcher/*Pool/*Adapter classes incl. underscore-private;
# threading.Thread pools) ARE caught.


def _scan_source(src: str) -> list:
    """Return the list of 'own-dispatcher' violations in one Python source string (AST-node based)."""
    bad: list = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        # (a) imports — forbid the concurrency/subprocess stacks; bare `async def` is fine (no import)
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name in _FORBIDDEN_IMPORT_MODULES or a.name.split(".")[0] in _FORBIDDEN_IMPORT_HEADS:
                    bad.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base in _FORBIDDEN_IMPORT_MODULES or base.split(".")[0] in _FORBIDDEN_IMPORT_HEADS:
                bad.append(f"from {base} import ...")
            for a in node.names:
                if a.name in _FORBIDDEN_IMPORT_NAMES:
                    bad.append(f"from {base} import {a.name}")
        # (b) def/class NAMES only (fields/params/attrs named fanout/pool/dispatch are not these nodes)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            n = node.name
            # leading underscores are this codebase's private-naming convention → strip them before the
            # case/suffix test so `_MprDispatcher`/`_WorkerPool`/`_AgentAdapter` cannot slip the rules
            # (allowlist is still checked on the ORIGINAL name, which carries its underscores).
            core = n.lstrip("_")
            if n in _FORBIDDEN_DEF_NAMES or core.lower().replace("_", "") == "fanout":
                bad.append(f"def/class {n}")
            elif core[:1].isupper() and core.endswith("Dispatcher") and n not in _ALLOWLIST_DISPATCHER:
                bad.append(f"class {n}")
            elif core[:1].isupper() and core.endswith("Pool"):
                bad.append(f"class {n}")
            elif core[:1].isupper() and core.endswith("Adapter") and n not in _ALLOWLIST_ADAPTER:
                bad.append(f"class {n}")
        # (c) calls — forbid spawning a process / building an executor pool
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in _FORBIDDEN_CALL_TAILS:
                bad.append(f"call ...{attr}()")
            elif isinstance(node.func.value, ast.Name) and (node.func.value.id, attr) in _FORBIDDEN_CALL_ATTRS:
                bad.append(f"call {node.func.value.id}.{attr}()")
    return bad


def _mpr_source_files():
    """Every non-test plugin source file (recurse), excluding tests/, the bootstrap entry, caches."""
    for p in sorted(_MPR_ROOT.rglob("*.py")):
        rel = p.relative_to(_MPR_ROOT).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        if rel == "skills/mpr_research.py":          # the standalone loader bootstrap (Spec 08 §2.4 excludes it)
            continue
        yield p


def test_mpr_brings_no_own_dispatcher():
    """The plugin must RIDE ironclad's fan-out/dispatcher/governor — never rebuild one (Spec 08 §2.4)."""
    offenders = {}
    files = list(_mpr_source_files())
    assert files, "no MPR source files discovered — path anchor wrong"
    for p in files:
        v = _scan_source(p.read_text(encoding="utf-8"))
        if v:
            offenders[p.relative_to(_MPR_ROOT).as_posix()] = v
    assert not offenders, f"MPR rebuilt an ironclad primitive (reiten-statt-duplizieren broken): {offenders}"


def test_no_own_dispatcher_guard_catches_violations():
    """The guard is not vacuous: a genuine rebuild in any of the three node classes IS flagged."""
    assert _scan_source("import subprocess\n")
    assert _scan_source("from concurrent.futures import ThreadPoolExecutor\n")
    assert _scan_source("import asyncio\n")
    assert _scan_source("def fanout(x):\n    return x\n")
    assert _scan_source("def _plan_concurrency(n, m):\n    return 1\n")
    assert _scan_source("class MprDispatcher:\n    pass\n")
    assert _scan_source("class WorkerPool:\n    pass\n")
    assert _scan_source("class AgentAdapter:\n    pass\n")
    assert _scan_source("import subprocess\nsubprocess.Popen(['x'])\n")
    assert _scan_source("import threading\nthreading.Thread(target=f)\n")        # F3: thread-pool fan-out
    # F2: leading-underscore privates (this codebase's convention) must NOT slip the suffix rules:
    assert _scan_source("class _MprDispatcher:\n    pass\n")
    assert _scan_source("class _WorkerPool:\n    pass\n")
    assert _scan_source("class _AgentAdapter:\n    pass\n")
    assert _scan_source("def _fanout(x):\n    return x\n")
    # …and the legitimate MVP shapes are NOT flagged (the grounded naive hits + the real Lock):
    assert _scan_source("class _ClassifierAdapter:\n    pass\n") == []          # allowlisted LLM-port adapter
    assert _scan_source("def plan_perspective_dispatch():\n    pass\n") == []    # planner, not *Dispatcher
    assert _scan_source("async def generate_adhoc_panel():\n    pass\n") == []   # async def ≠ import asyncio
    assert _scan_source("fanout = None\npool = {}\n") == []                      # fields, not def/class
    assert _scan_source("import threading\nthreading.Lock()\n") == []           # registry Lock is legit
    assert _scan_source("import gx10\nfrom ack.validated_emit import emit_validated\n") == []  # engine/core import


def test_allowlisted_symbols_still_exist():
    """If an allowlisted name is renamed, the allowlist must be revisited — pin the ones present today."""
    entry_src = (_MPR_ROOT / "entry.py").read_text(encoding="utf-8")
    assert "class _ClassifierAdapter" in entry_src, "_ClassifierAdapter renamed → revisit the AST allowlist"
    # ProviderDispatcher is a forward-looking allowlist entry (P0 type, referenced once dispatch is wired);
    # it does not yet appear in MPR code, so it is intentionally not asserted here.


_EXPECTED_TEST_FILES = {
    "test_router_schema.py", "test_router_snapshots.py", "test_router_decline.py", "test_router_guards.py",
    "test_router_adhoc.py", "test_router_replay.py", "test_router_provenance.py", "test_router_config.py",
    "test_registry_schema.py", "test_registry_resolve.py", "test_registry_synthesis.py",
    "test_registry_loader.py", "test_registry_guards.py", "test_registry_adaptive.py",
    "test_registry_config.py", "test_registry_versioning.py", "test_registry_gate.py",
    "test_start_panels.py", "test_conflicts.py", "test_templates.py", "test_synthesis.py",
    "test_audit.py", "test_sovereignty.py", "test_mpr_config.py", "test_mpr_packaging.py",
}


def test_component_test_files_present():
    """Structural §2 inventory: a component's deterministic test file cannot disappear unnoticed."""
    present = {p.name for p in (_MPR_ROOT / "tests").glob("test_*.py")}
    missing = _EXPECTED_TEST_FILES - present
    assert not missing, f"§2 component test files missing: {sorted(missing)}"


# §2.4 P0 dispatch-seam: NO LONGER deferred — run_mpr now routes via ProviderDispatcher.dispatch
# (PW-1). The RouteRequest[]/DispatchPolicy contract + DispatchResult provenance are exercised live in
# test_p0_dispatch.py (test_local_only_role_never_offloaded / _offloadable_role_allows_spill /
# _policy_passed_through_unaltered / _effort_forwarded_to_dispatch / _manifest_provenance_from_*).
