"""#1237 — isolate the developed software under a code subdir so it doesn't mix with the control-plane.

Opt-in `paths.code_subdir` (default "" → byte-identical): when set, model-driven execution (code-tools,
execute_command, the launched coder — all via _exec_cwd) runs under `<root>/<code_subdir>`, while the
control-plane (vault/, .ironclad/) keeps resolving to the project root.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))
_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import gx10  # noqa: E402


def _default_project(monkeypatch, tmp_path, subdir):
    monkeypatch.setattr(gx10, "CODE_SUBDIR", subdir)
    monkeypatch.setattr(gx10, "_pc", None)               # no bound ProjectContext → the default project
    monkeypatch.setattr(gx10, "_BOOT_WORKDIR", tmp_path)


def test_exec_cwd_byte_identical_when_empty(monkeypatch, tmp_path):
    _default_project(monkeypatch, tmp_path, "")
    assert gx10._exec_cwd() is None                       # default project + no subdir → pre-isolation behaviour


def test_exec_cwd_isolates_when_set(monkeypatch, tmp_path):
    _default_project(monkeypatch, tmp_path, "src")
    out = gx10._exec_cwd()
    assert out == str(tmp_path / "src") and (tmp_path / "src").is_dir()   # created + isolated


def test_resolve_exec_path_under_subdir(monkeypatch, tmp_path):
    _default_project(monkeypatch, tmp_path, "src")
    assert gx10._resolve_exec_path("app/main.py") == tmp_path / "src" / "app" / "main.py"


def test_resolve_exec_path_absolute_unchanged(monkeypatch, tmp_path):
    _default_project(monkeypatch, tmp_path, "src")
    abs_p = tmp_path / "elsewhere" / "x.py"
    assert gx10._resolve_exec_path(str(abs_p)) == abs_p   # an absolute path is taken verbatim


def test_control_plane_not_under_code_subdir(monkeypatch, tmp_path):
    _default_project(monkeypatch, tmp_path, "src")
    monkeypatch.chdir(tmp_path)
    assert "src" in gx10._exec_cwd()                      # code execution is under src …
    assert "src" not in str(gx10.vault_root())            # … but the control-plane is not
    assert "src" not in str(gx10.state_root())


def test_apply_config_reads_and_normalizes_code_subdir(monkeypatch):
    monkeypatch.setattr(gx10, "CODE_SUBDIR", "")
    cfg = gx10._code_defaults()
    cfg["paths"]["code_subdir"] = "/src/"                 # leading/trailing slashes stripped
    gx10._apply_config(cfg)
    assert gx10.CODE_SUBDIR == "src"


def test_apply_config_default_is_empty(monkeypatch):
    monkeypatch.setattr(gx10, "CODE_SUBDIR", "leftover")
    gx10._apply_config(gx10._code_defaults())             # defaults carry no code_subdir → off
    assert gx10.CODE_SUBDIR == ""


def test_code_subdir_rejects_traversal_and_absolute(monkeypatch):
    # Sonnet defect 1: an escaping code_subdir is rejected (containment) → off, never redirecting ops outside.
    for bad in ("../../etc", "..", "a/../../b", "D:/evil", "C:\\Windows"):
        monkeypatch.setattr(gx10, "CODE_SUBDIR", "sentinel")
        cfg = gx10._code_defaults(); cfg["paths"]["code_subdir"] = bad
        gx10._apply_config(cfg)
        assert gx10.CODE_SUBDIR == "", f"{bad!r} should be rejected"


def test_code_subdir_accepts_nested(monkeypatch):
    monkeypatch.setattr(gx10, "CODE_SUBDIR", "")
    cfg = gx10._code_defaults(); cfg["paths"]["code_subdir"] = "packages/app"
    gx10._apply_config(cfg)
    assert gx10.CODE_SUBDIR == "packages/app"             # a contained nested subdir is fine


def test_exec_cwd_falls_back_when_subdir_blocked(monkeypatch, tmp_path):
    # Sonnet defect 2: if a plain FILE occupies <root>/src, mkdir fails → fall back to root, not a broken cwd.
    _default_project(monkeypatch, tmp_path, "src")
    (tmp_path / "src").write_text("i am a file, not a dir", encoding="utf-8")
    assert gx10._exec_cwd() is None                       # default project → fall back to None (process workdir)
