"""#974/#977: the engine-side plain read of the per-project INJECTION descriptor + the /lifecycle
fail-closed drift check. The engine reads the descriptor as PLAIN DATA (no import of the private
scripts/devloop machinery — the tool<->process edge stays data-only)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _write(root: Path, desc: dict) -> None:
    (root / ".devloop").mkdir(parents=True, exist_ok=True)
    (root / ".devloop" / "dev-target.json").write_text(json.dumps(desc), encoding="utf-8")


def _reg(*ids: str):
    return types.SimpleNamespace(list=lambda: [types.SimpleNamespace(id=i) for i in ids])


def test_descriptor_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    assert gx10._dev_target_descriptor() is None


def test_descriptor_reads_plain(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    _write(tmp_path, {"project_id": "default", "exec_mode": "github", "tier": 2, "plugin_required": False})
    d = gx10._dev_target_descriptor()
    assert d and d["exec_mode"] == "github" and d["project_id"] == "default"


def test_descriptor_corrupt_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    (tmp_path / ".devloop").mkdir(parents=True)
    (tmp_path / ".devloop" / "dev-target.json").write_text("{not json", encoding="utf-8")
    assert gx10._dev_target_descriptor() is None


def test_drift_flags_unregistered_project(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(gx10, "_REGISTRY", _reg("default"), raising=False)
    _write(tmp_path, {"project_id": "ghost", "exec_mode": "github", "tier": 2, "plugin_required": False})
    errs = gx10._dev_target_drift()
    assert errs and "ghost" in errs[0]


def test_drift_ok_when_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(gx10, "_REGISTRY", _reg("proj", "default"), raising=False)
    _write(tmp_path, {"project_id": "proj", "exec_mode": "local", "tier": 2, "plugin_required": False})
    assert gx10._dev_target_drift() == []


def test_drift_empty_without_descriptor(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    monkeypatch.setattr(gx10, "_REGISTRY", _reg("default"), raising=False)
    assert gx10._dev_target_drift() == []


# ── #979: mutual-exclusion helpers + per-project forkscan/devscan scoping + switch serialization ──
def test_internal_target_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    assert gx10._active_is_internal_target() is False
    assert gx10._internal_target_blocks_normal() is None
    _write(tmp_path, {"project_id": "default", "exec_mode": "github", "tier": 2, "plugin_required": False})
    assert gx10._active_is_internal_target() is True
    msg = gx10._internal_target_blocks_normal()
    assert msg and "INTERNAL" in msg and "default" in msg


def test_forkscan_devscan_paths_are_per_mem_ns(monkeypatch):
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "ns123", raising=False)
    assert gx10._ace_forkscan_path().as_posix().endswith("ace_forkscan/ns123.json")
    assert gx10._ace_devscan_path().as_posix().endswith("ace_devscan/ns123.json")
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda default="": "", raising=False)
    assert gx10._ace_forkscan_path().as_posix().endswith("ace_forkscan/base.json")   # base project fallback


def test_switch_serialize_is_a_context():
    with gx10._switch_serialize():
        pass    # a repo-global lock context (or a fail-soft no-op) — must not raise


# ── #982: the /status operator view of the injection (internal-target) mode ──
def test_dev_target_status_line(tmp_path, monkeypatch):
    monkeypatch.setattr(gx10, "_project_root", lambda: tmp_path, raising=False)
    assert "normal" in gx10._dev_target_status_line()                 # no descriptor → normal process
    _write(tmp_path, {"project_id": "default", "exec_mode": "github", "tier": 3,
                      "plugin_required": True, "plugin_id": "example-plugin"})
    line = gx10._dev_target_status_line()
    assert "INTERNAL" in line and "github" in line and "example-plugin" in line and "tier=3" in line
