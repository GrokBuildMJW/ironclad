"""Rot guards for the ACK gx10 isolation fixture."""

import ast
import importlib.util as _ilu
import sys
import threading
import types
from types import SimpleNamespace
from pathlib import Path

import pytest

_ackconf_spec = _ilu.spec_from_file_location(
    "ack_tests_conftest", Path(__file__).with_name("conftest.py")
)
_ackconf = _ilu.module_from_spec(_ackconf_spec)
_ackconf_spec.loader.exec_module(_ackconf)
_GX10_STATE_ATTRS = _ackconf._GX10_STATE_ATTRS
_ace_isolation = _ackconf._ace_isolation
_join_ace_worker_threads = _ackconf._join_ace_worker_threads

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))


_LIFECYCLE_MANAGED_ACE_GLOBALS = {
    "_ACE_STORE",
    "_ACE_WORKER",
    "_ACE_MIGRATED",
    "_ACE_FORK_MPR",
    "_ACE_FORK_WORKER",
    "_ACE_INJECTED",
}


def _apply_config_globals() -> set[str]:
    """Globals published by config commit/reconfiguration must be fixture-snapshotted."""
    gx10_path = Path(__file__).resolve().parents[2] / "engine" / "gx10.py"
    tree = ast.parse(gx10_path.read_text(encoding="utf-8"))
    found = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if names & {"_CONFIG_DERIVED_GLOBALS", "_CONFIG_RECONFIG_GLOBALS"}:
            found.update(ast.literal_eval(node.value))
    return found


def test_gx10_state_attrs_cover_apply_config_rebindings():
    globals_ = _apply_config_globals()
    assert globals_
    # ACE globals are lifecycle-managed: teardown stops live workers and hard-clears the globals instead of
    # snapshot-restoring stale refs that can orphan daemon threads.
    assert set(_GX10_STATE_ATTRS) >= (globals_ - _LIFECYCLE_MANAGED_ACE_GLOBALS)


def _iter_target_names(target):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _iter_target_names(elt)


def _derive_state_assigned_module_globals() -> set[str]:
    """Module globals assigned inside `_derive_config_state`.

    These are exactly the config-owned globals the function must hand to `_commit_config_state` via
    `_CONFIG_DERIVED_GLOBALS`. Guards the code→list direction: a new derived global omitted from the list
    would be silently neither committed, snapshotted for rollback, nor fixture-isolated (the runtime
    ``values = {n: locals[n] for n in _CONFIG_DERIVED_GLOBALS}`` only guards list→code)."""
    gx10_path = Path(__file__).resolve().parents[2] / "engine" / "gx10.py"
    tree = ast.parse(gx10_path.read_text(encoding="utf-8"))
    module_globals: set[str] = set()
    for node in tree.body:                                    # module-level bindings
        if isinstance(node, ast.Assign):
            for t in node.targets:
                module_globals.update(_iter_target_names(t))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            module_globals.update(_iter_target_names(node.target))
    for node in ast.walk(tree):                               # + names rebound via `global`
        if isinstance(node, ast.Global):
            module_globals.update(node.names)
    derive = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_derive_config_state"), None)
    assert derive is not None, "_derive_config_state not found"
    assigned: set[str] = set()
    for node in ast.walk(derive):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                assigned.update(_iter_target_names(t))
        elif isinstance(node, ast.AnnAssign):
            assigned.update(_iter_target_names(node.target))
    return assigned & module_globals


def test_derive_config_state_globals_are_all_declared():
    import gx10
    assigned = _derive_state_assigned_module_globals()
    assert assigned                                          # non-vacuous: the scan found real globals
    declared = set(gx10._CONFIG_DERIVED_GLOBALS)
    missing = assigned - declared
    assert not missing, (
        f"_derive_config_state assigns module global(s) {sorted(missing)} absent from "
        "_CONFIG_DERIVED_GLOBALS — they would be neither committed by _commit_config_state, nor "
        "snapshotted for rollback, nor fixture-isolated. Add them to the tuple."
    )


def test_ace_isolation_joins_retained_worker_and_fails_if_stuck(tmp_path, monkeypatch):
    import gx10

    fixture = _ace_isolation.__wrapped__(
        SimpleNamespace(module=SimpleNamespace(__name__="test_conftest_isolation")),
        tmp_path,
        monkeypatch,
    )
    next(fixture)
    gx10._apply_config(gx10._code_defaults())
    worker = gx10._ACE_WORKER
    assert worker is not None
    assert worker._thread is not None and worker._thread.is_alive()

    entered = threading.Event()
    release = threading.Event()

    def block_in_flight(_item):
        entered.set()
        release.wait(timeout=5.0)

    worker._process = block_in_flight
    assert worker.submit(object()) is True
    assert entered.wait(timeout=1.0)
    thread = worker._thread
    monkeypatch.setattr(worker, "stop", lambda: worker.__class__.stop(worker, timeout=0.0))

    delayed_release = threading.Timer(0.2, release.set)
    delayed_release.start()
    try:
        try:
            next(fixture)
        except StopIteration:
            pass
        leaked = thread.is_alive()
    finally:
        release.set()
        delayed_release.join(timeout=1.0)
        thread.join(timeout=1.0)

    assert gx10._ACE_WORKER is None
    assert worker._thread is None
    assert leaked is False

    stuck_release = threading.Event()
    stuck = threading.Thread(target=stuck_release.wait, name="ace-reflection-worker", daemon=True)
    stuck.start()
    try:
        with pytest.raises(RuntimeError, match="survived test teardown after 0.0s"):
            _join_ace_worker_threads(timeout=0.01)
    finally:
        stuck_release.set()
        stuck.join(timeout=1.0)
