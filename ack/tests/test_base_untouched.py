"""Base-untouched reconciler check (epic #601 S17 / AD-8) — offline + deterministic.

The "delivered state" — the engine's own source surface (`core/skills` + `engine/prompts`) — must be
**byte-unchanged** by per-project work: minting, switching, staging units, and deleting projects all happen
under a project ROOT, never inside the installed engine. This test snapshots that surface, runs a full
project lifecycle in a throwaway working dir, and asserts the surface is byte-identical afterwards (catching a
regression where a path resolves into the engine's own tree instead of the project root).

The LIVE counterpart — the installed engine + the private `conf/` asserted byte-unchanged after a REAL dev
cycle — is the operator-gated deploy check.
"""
from __future__ import annotations

from design_test_support import approve_active_design

import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_registry as pr   # noqa: E402
import project_context as pc    # noqa: E402
import gx10                     # noqa: E402

# The engine's delivered source surface (relative to the private monorepo's core/).
_SKILLS = _ENGINE.parent / "skills"          # core/skills
_ENGINE_PROMPTS = _ENGINE / "prompts"        # engine/prompts


def _surface_hash(dirs) -> "dict[str, str]":
    """A {relpath: sha256} map of every real file under *dirs* — excluding Python bytecode caches so the
    snapshot is deterministic across runs."""
    out: dict[str, str] = {}
    for base in dirs:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            out[str(p.relative_to(base.parent))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class FakeGx:
    def __init__(self):
        self.messages = [{"role": "system", "content": "SYS"}]
        self.last_response = ""
        self.fail_save = False

    def save_session(self, *, strict=False):
        if self.fail_save and strict:
            raise OSError("disk full")
        p = gx10.session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"messages": self.messages}), encoding="utf-8")

    def load_session(self):
        p = gx10.session_path()
        if not p.exists():
            return 0
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return 0
        loaded = [m for m in data.get("messages", []) if m.get("role") != "system"]
        system = next((m for m in self.messages if m.get("role") == "system"), None)
        self.messages = ([system] if system else []) + loaded
        return len(loaded)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("GX10_HOME", str(tmp_path / "home"))
    wd = tmp_path / "wd"
    wd.mkdir()
    monkeypatch.chdir(wd)
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    pc.set_current(None)
    _orig_base, _orig_eff = gx10._BASE_CFG, gx10._EFFECTIVE_CFG
    # Snapshot the live skill registries: _load_skills is left REAL here (it is the switch-time path that
    # READS the built-in skills, so running it for real makes this base-untouched check cover the reload too),
    # and it clear()+update()s these in place — restore them on teardown so the per-project discovery can't
    # leak into a later test.
    _orig_prompts = dict(gx10._PROMPTS)
    _orig_playbooks = dict(gx10._PLAYBOOKS)
    _orig_tools = dict(gx10._PLUGIN_TOOLS)
    gx10.init_registry(wd)
    gx10._BASE_CFG = gx10._code_defaults()
    monkeypatch.setattr(gx10, "STORE", None)
    yield wd
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    gx10._BASE_CFG, gx10._EFFECTIVE_CFG = _orig_base, _orig_eff
    for _live, _orig in ((gx10._PROMPTS, _orig_prompts),
                         (gx10._PLAYBOOKS, _orig_playbooks),
                         (gx10._PLUGIN_TOOLS, _orig_tools)):
        _live.clear()
        _live.update(_orig)
    pc.set_current(None)


def _stage_unit(agent, title):
    approve_active_design(gx10)
    return gx10._stage_handover(
        None, "OPUS", f"## Handover\n{title}",
        task_json=json.dumps({"type": "feature", "priority": "high",
                              "title": f"Complete validated unit {title}",
                              "description": "Complete the validated unit with implementation and regression coverage."}),
        force=True,
    )


def test_engine_source_surface_untouched_by_project_lifecycle(engine):
    wd = engine
    before = _surface_hash([_SKILLS, _ENGINE_PROMPTS])
    assert before, "no engine source surface found to check (core/skills + engine/prompts)"

    # A full project lifecycle entirely under the throwaway wd: mint+switch+seed two projects, stage a unit,
    # switch, and delete one.
    ag = FakeGx()
    assert gx10._project_command("new alpha --type software", ag).startswith("[project] created")
    assert _stage_unit(ag, "alpha-task").startswith("OK")
    assert gx10._project_command("new beta --type software", ag).startswith("[project] created")
    assert gx10._switch_command(ag, "alpha").startswith("[switch] now on alpha")
    assert "deleted beta" in gx10._project_command("delete beta", ag)

    # The lifecycle did real work under wd (non-vacuous) ...
    assert (wd / "alpha").is_dir() and list((wd / "alpha").rglob("*_OPUS.md"))
    assert not any(p.id == "beta" for p in gx10._REGISTRY.list())

    # ... yet the engine's own delivered source surface is byte-identical (base/delivered state untouched).
    after = _surface_hash([_SKILLS, _ENGINE_PROMPTS])
    assert after == before, "project work mutated the engine's own source surface (core/skills or prompts)"


def test_surface_hash_detects_a_change(tmp_path):
    # the snapshot must actually distinguish content (so the equality assertion above is meaningful)
    d = tmp_path / "skills"
    d.mkdir()
    (d / "a.md").write_text("one", encoding="utf-8")
    h1 = _surface_hash([d])
    assert h1
    (d / "a.md").write_text("two", encoding="utf-8")
    assert _surface_hash([d]) != h1
    # and bytecode caches are ignored (no spurious diffs)
    (d / "__pycache__").mkdir()
    (d / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
    assert _surface_hash([d]) == _surface_hash([d])
