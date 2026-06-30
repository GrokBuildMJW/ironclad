from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10                       # noqa: E402
import project_context as pc       # noqa: E402
import project_registry as pr      # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # isolated installation home so the real registry is never touched
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    yield
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)


def test_init_registry_binds_default_behaviour_preserving(tmp_path):
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)
    cur = pc.current()
    assert cur is not None and cur.project_id == "default"
    assert Path(cur.root) == wd.resolve()
    assert cur.mem_ns == ""                                  # default binds the base (empty) partition
    assert gx10.state_root() == wd.resolve() / ".ironclad"
    assert gx10.vault_root() == wd.resolve() / "vault"
    assert gx10.session_path() == wd.resolve() / ".ironclad" / "session.json"
    assert gx10._active_mem_ns() == ""                        # memory falls back to legacy
    assert gx10._active_warm_session() == "main"


def test_non_default_project_isolates(tmp_path):
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)
    proj = gx10._REGISTRY.register("acme", wd / "acme", make_active=True)
    assert proj.mem_ns and len(proj.mem_ns) >= 16
    gx10._set_active_project(proj); gx10.bind_active()
    cur = pc.current()
    assert cur.project_id == "acme" and cur.mem_ns == proj.mem_ns
    assert gx10.state_root() == (wd / "acme").resolve() / ".ironclad"
    assert gx10._active_mem_ns() == proj.mem_ns
    assert gx10._active_warm_session() == proj.mem_ns


def test_switch_back_to_default_restores_base_partition(tmp_path):
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)
    acme = gx10._REGISTRY.register("acme", wd / "acme", make_active=True)
    gx10._set_active_project(acme); gx10.bind_active()
    gx10._set_active_project(gx10._REGISTRY.get("default")); gx10.bind_active()
    assert pc.current().mem_ns == "" and gx10._active_mem_ns() == ""
    assert Path(pc.current().root) == wd.resolve()


def test_engine_ctx_for_policy(tmp_path):
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)
    proj = gx10._REGISTRY.register("acme", wd / "acme")
    d = gx10._REGISTRY.get("default")
    assert gx10._engine_ctx_for(proj).mem_ns == proj.mem_ns
    assert gx10._engine_ctx_for(d).mem_ns == ""


def test_bind_active_uses_cache_not_registry(tmp_path):
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)                              # cache = default
    gx10._REGISTRY.register("acme", wd / "acme", make_active=True)   # registry.active = acme, cache unchanged
    gx10.bind_active()
    assert pc.current().project_id == "default"        # bind uses the per-process cache, NOT registry.active


def test_init_binds_boot_workdir_even_if_stored_default_root_differs(tmp_path):
    import project_registry as pr
    a = tmp_path / "wdA"; a.mkdir(); b = tmp_path / "wdB"; b.mkdir()
    pr.Registry().ensure_default(a)                     # a prior boot from wdA created default.root = wdA
    gx10.init_registry(b)                               # THIS process boots from wdB
    cur = pc.current()
    assert cur.project_id == "default"
    assert Path(cur.root) == b.resolve()               # binds the boot workdir, not the stored wdA
    assert gx10._REGISTRY.get("default").root == str(a.resolve())   # stored root NOT re-pointed (no cross-process write)


def test_bind_active_without_registry_is_noop():
    gx10._REGISTRY = None
    pc.set_current(None)
    gx10.bind_active()           # must not raise
    assert pc.current() is None


def test_init_registry_failure_clears_ctx_and_runs_unisolated(tmp_path, monkeypatch):
    pc.set_current(pc.ProjectContext("stale", "/x", "deadbeefdeadbeef", "main"))
    monkeypatch.setattr(pr.Registry, "ensure_default", lambda self, root: (_ for _ in ()).throw(RuntimeError("boom")))
    wd = tmp_path / "wd"; wd.mkdir()
    gx10.init_registry(wd)
    assert gx10._REGISTRY is None
    assert gx10._ACTIVE_PROJECT is None
    assert pc.current() is None                  # failure must clear the stale ctx
