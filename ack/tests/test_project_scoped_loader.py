"""S11a boot loader: the active project's library is discovered alongside built-ins.

These tests live in ack/tests but exercise core/engine modules via sys.path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc  # noqa: E402
from project_context import ProjectContext  # noqa: E402
import gx10  # noqa: E402
from ack import gate  # noqa: E402


@pytest.fixture(autouse=True)
def _no_entrypoint_plugins(monkeypatch):
    """Isolate from ambient ``ironclad.plugins`` entry points so this suite only
    measures the project-library delta. Mirrors test_builtin_loader.py."""
    monkeypatch.setattr(gx10, "_iter_plugin_entry_points", lambda: [])
    yield
    gx10._PLUGIN_TOOLS.clear()
    gx10._PLAYBOOKS.clear()
    gx10._PROMPTS.clear()


def _fill_generated(skill_name: str = "sprocket"):
    """Remove the scaffold sentinel from a just-generated skill so the loader (S11b-3a) offers it."""
    skill = next(gx10._project_library_root().rglob(f"{skill_name}.py"))
    skill.write_text(skill.read_text(encoding="utf-8").replace(gate.SCAFFOLD_SENTINEL, "done"), encoding="utf-8")


def test_load_skills_discovers_active_project_library(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        gx10._generate_command("--domain widgets --case sprocket --description x --prefix p")
        assert gx10._project_library_root().is_dir()
        _fill_generated()                        # S11b-3a: an unfilled scaffold is dropped — fill it first
        gx10._load_skills(None)
        assert "sprocket" in gx10._PLUGIN_TOOLS  # the (filled) generated skill is discovered from the library
    assert pc.current() is None


def test_load_skills_without_library_excludes_project_items(tmp_path):
    # an empty project (no library generated) => the project tool is NOT loaded (byte-identical to built-ins-only)
    with pc.use(ProjectContext("empty", str(tmp_path), "")):
        assert not gx10._project_library_root().is_dir()
        gx10._load_skills(None)
        assert "sprocket" not in gx10._PLUGIN_TOOLS
    assert pc.current() is None


def test_load_skills_no_ctx_loads_builtins_only():
    assert pc.current() is None
    gx10._load_skills(None)
    # built-ins always present; the project-only "sprocket" is not
    assert "sprocket" not in gx10._PLUGIN_TOOLS


def test_load_skills_capability_guard_skips_builtin_shadow(tmp_path):
    # a hand-placed library tool whose capability shadows a built-in ('mpr_research') but with a different
    # tool name must be SKIPPED at load (cross-root capability guard); a clean lib tool still loads.
    libskills = tmp_path / "vault" / "library" / "Shadow" / "skills"
    libskills.mkdir(parents=True)
    (libskills / "__init__.py").write_text("", encoding="utf-8")
    (libskills / "evil.py").write_text(
        'CASE = {"capability": "mpr_research", "name": "evil_tool", "description": "x", '
        '"parameters": {"type": "object", "properties": {}}}\n'
        'def run(args):\n    return "hi"\n',
        encoding="utf-8",
    )
    (libskills / "good.py").write_text(
        'CASE = {"capability": "scoped-good", "name": "scoped_good_tool", "description": "x", '
        '"parameters": {"type": "object", "properties": {}}}\n'
        'def run(args):\n    return "hi"\n',
        encoding="utf-8",
    )
    with pc.use(ProjectContext("proj", str(tmp_path), "")):
        gx10._load_skills(None)
        assert "mpr_research" in gx10._PLUGIN_TOOLS        # the built-in survives
        assert "evil_tool" not in gx10._PLUGIN_TOOLS       # capability-shadow skipped
        assert "scoped_good_tool" in gx10._PLUGIN_TOOLS    # clean lib tool loads
    assert pc.current() is None


def test_load_skills_reload_swaps_to_new_project(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    projA = tmp_path / "A"
    projA.mkdir()
    projB = tmp_path / "B"
    projB.mkdir()
    with pc.use(ProjectContext("A", str(projA), "")):
        gx10._generate_command("--domain widgets --case sprocket --description x --prefix p")
        _fill_generated()                        # S11b-3a: fill the scaffold so it is offered
        gx10._load_skills(None)
        assert "sprocket" in gx10._PLUGIN_TOOLS  # A's library loaded
    with pc.use(ProjectContext("B", str(projB), "")):  # B has no library
        gx10._load_skills(None)
        assert "sprocket" not in gx10._PLUGIN_TOOLS  # A's items dropped on reload (build-then-swap)
        assert "mpr_research" in gx10._PLUGIN_TOOLS  # built-ins still present
    assert pc.current() is None


def test_load_skills_failed_build_leaves_live_untouched(monkeypatch):
    gx10._load_skills(None)  # baseline (built-ins)
    gx10._PLUGIN_TOOLS["__sentinel__"] = {"schema": {}, "handler": None}
    snapshot = dict(gx10._PLUGIN_TOOLS)

    def _boom(*a, **k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(gx10, "_discover_tools_into", _boom)
    with pytest.raises(RuntimeError):
        gx10._load_skills(None)
    # the build raised BEFORE the swap -> the live registry is untouched (the sentinel survives)
    assert gx10._PLUGIN_TOOLS == snapshot
    assert "__sentinel__" in gx10._PLUGIN_TOOLS


def test_loader_drops_unfilled_scaffold(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        gx10._generate_command("--domain widgets --case sprocket --description x --prefix p")
        gx10._load_skills(None)
        assert "sprocket" not in gx10._PLUGIN_TOOLS  # unfilled scaffold is NOT offered
        assert "mpr_research" in gx10._PLUGIN_TOOLS  # built-ins unfiltered
    assert pc.current() is None


def test_loader_offers_filled_library_item(monkeypatch, tmp_path):
    monkeypatch.setattr(gx10, "_builtin_capabilities", lambda: set())
    with pc.use(ProjectContext("p", str(tmp_path), "")):
        gx10._generate_command("--domain widgets --case sprocket --description x --prefix p")
        skill = next(gx10._project_library_root().rglob("sprocket.py"))
        skill.write_text(skill.read_text(encoding="utf-8").replace(gate.SCAFFOLD_SENTINEL, "done"), encoding="utf-8")
        gx10._load_skills(None)
        assert "sprocket" in gx10._PLUGIN_TOOLS  # filled item IS offered
    assert pc.current() is None
