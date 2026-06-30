from __future__ import annotations
import sys
import threading
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402


def _ctx(tmp_path: Path, name: str) -> ProjectContext:
    return ProjectContext(
        project_id=name,
        root=str((tmp_path / name).resolve()),
        mem_ns="abcdef1234567890",
    )


def test_current_defaults_to_none() -> None:
    assert pc.current() is None


def test_use_sets_and_restores(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "p1")
    assert pc.current() is None
    with pc.use(ctx):
        assert pc.current() is ctx
    assert pc.current() is None


def test_use_restores_on_exception(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "p1")
    assert pc.current() is None
    with pytest.raises(RuntimeError):
        with pc.use(ctx):
            assert pc.current() is ctx
            raise RuntimeError("boom")
    assert pc.current() is None


def test_use_nests_and_restores(tmp_path: Path) -> None:
    a = _ctx(tmp_path, "a")
    b = _ctx(tmp_path, "b")
    assert pc.current() is None
    with pc.use(a):
        assert pc.current() is a
        with pc.use(b):
            assert pc.current() is b
        assert pc.current() is a
    assert pc.current() is None


def test_set_current_and_reset(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "p1")
    assert pc.current() is None
    token = pc.set_current(ctx)
    assert pc.current() is ctx
    pc.reset(token)
    assert pc.current() is None


def test_bound_target_carries_ctx_into_thread(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "p1")
    result: dict[str, object] = {}
    lock = threading.Lock()

    def record() -> None:
        current = pc.current()
        with lock:
            result["root"] = None if current is None else current.root

    with pc.use(ctx):
        t = threading.Thread(target=pc.bound_target(record))
        t.start()
        t.join()

    assert result["root"] == ctx.root


def test_naive_thread_does_not_see_ctx(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "p1")
    result: dict[str, object] = {}
    lock = threading.Lock()

    def record() -> None:
        with lock:
            result["current"] = pc.current()

    with pc.use(ctx):
        t = threading.Thread(target=record)
        t.start()
        t.join()

    assert result["current"] is None


def test_state_and_vault_root_unchanged_without_ctx() -> None:
    assert pc.current() is None
    assert gx10.state_root() == Path(gx10.STATE_ROOT)
    assert gx10.vault_root() == Path(gx10.VAULT_ROOT)


def test_roots_resolve_under_active_project(tmp_path: Path) -> None:
    ctx = ProjectContext(
        project_id="proj",
        root=str((tmp_path / "proj").resolve()),
        mem_ns="abcdef1234567890",
    )
    with pc.use(ctx):
        assert gx10.state_root() == Path(ctx.root) / Path(gx10.STATE_ROOT)
        assert gx10.vault_root() == Path(ctx.root) / Path(gx10.VAULT_ROOT)
        assert gx10.session_path() == Path(ctx.root) / Path(gx10.STATE_ROOT) / gx10.SESSION_FILE


def test_absolute_state_root_override_ignores_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = ProjectContext(
        project_id="proj",
        root=str((tmp_path / "other").resolve()),
        mem_ns="abcdef1234567890",
    )
    abs_state = str((tmp_path / "abs_state").resolve())
    monkeypatch.setattr(gx10, "STATE_ROOT", abs_state)
    with pc.use(ctx):
        assert gx10.state_root() == Path(abs_state)



def test_absolute_vault_root_override_ignores_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gx10, "VAULT_ROOT", str((tmp_path / "abs_vault").resolve()))
    ctx = ProjectContext(project_id="p", root=str((tmp_path / "proj").resolve()), mem_ns="abcdef1234567890")
    with pc.use(ctx):
        assert gx10.vault_root() == Path(gx10.VAULT_ROOT)   # absolute override NOT prefixed by the project root


def test_absolute_session_file_override_ignores_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gx10, "SESSION_FILE", str((tmp_path / "abs_session.json").resolve()))
    ctx = ProjectContext(project_id="p", root=str((tmp_path / "proj").resolve()), mem_ns="abcdef1234567890")
    with pc.use(ctx):
        assert gx10.session_path() == Path(gx10.SESSION_FILE)  # absolute SESSION_FILE used unchanged even under a ctx


def test_accessors_fall_back_when_pc_module_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # if the project_context import was unavailable, gx10._pc is None and the accessors use the legacy globals
    monkeypatch.setattr(gx10, "_pc", None)
    ctx = ProjectContext(project_id="p", root=str((tmp_path / "proj").resolve()), mem_ns="abcdef1234567890")
    with pc.use(ctx):                                      # ctx is set, but gx10._pc is None -> ignored
        assert gx10.state_root() == Path(gx10.STATE_ROOT)
        assert gx10.vault_root() == Path(gx10.VAULT_ROOT)
