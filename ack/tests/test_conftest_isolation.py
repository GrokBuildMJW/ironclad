"""Rot guards for the ACK gx10 isolation fixture."""

import ast
import sys
import types
from types import SimpleNamespace
from pathlib import Path

from conftest import _GX10_STATE_ATTRS, _ace_isolation

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
    """Globals explicitly rebound by gx10._apply_config must be fixture-snapshotted."""
    gx10_path = Path(__file__).resolve().parents[2] / "engine" / "gx10.py"
    tree = ast.parse(gx10_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_config":
            return {
                name
                for stmt in ast.walk(node)
                if isinstance(stmt, ast.Global)
                for name in stmt.names
            }
    raise AssertionError("gx10._apply_config not found")


def test_gx10_state_attrs_cover_apply_config_rebindings():
    globals_ = _apply_config_globals()
    assert globals_
    # ACE globals are lifecycle-managed: teardown stops live workers and hard-clears the globals instead of
    # snapshot-restoring stale refs that can orphan daemon threads.
    assert set(_GX10_STATE_ATTRS) >= (globals_ - _LIFECYCLE_MANAGED_ACE_GLOBALS)


def test_ace_isolation_stops_live_worker_started_by_apply_config(tmp_path, monkeypatch):
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

    try:
        next(fixture)
    except StopIteration:
        pass

    assert gx10._ACE_WORKER is None
    assert worker._thread is None
