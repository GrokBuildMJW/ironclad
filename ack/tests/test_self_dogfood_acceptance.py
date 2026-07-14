"""Canonical self-dogfood ISOLATION acceptance (epic #601 S17 / AD-8) — offline + deterministic.

Drives the real engine surface (`/project new` → `/switch` → stage a unit of work → switch back) for TWO
projects through the actual quiesced-switch machinery, with no live infra, no model/agent, and no gh/PyPI
deliver. It asserts the whole-epic invariant: each project's vault, state machinery, and memory partition are
fully isolated under its own root; switching does not bleed conversation; and the implicit base/`default`
project is never touched by per-project work.

The live self-dogfood run (a real separate checkout, a real run→deliver) is the operator-gated deploy step;
this is its deterministic offline counterpart.
"""
from __future__ import annotations

from design_test_support import approve_active_design

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


def _is_under(child: Path, parent: Path) -> bool:
    """Path containment that is robust to Windows 8.3 short names (e.g. LONGNA~1 vs LongName) — resolve both."""
    c, p = child.resolve(), parent.resolve()
    return c == p or c.is_relative_to(p)


def _files_containing(base: Path, needle: str) -> "list[Path]":
    """Every file under *base* whose text contains *needle* — for content-level cross-bleed assertions."""
    out: list[Path] = []
    if not base.exists():
        return out
    for p in base.rglob("*"):
        if not p.is_file():
            continue
        try:
            if needle in p.read_text(encoding="utf-8"):
                out.append(p)
        except (OSError, UnicodeDecodeError):
            continue
    return out


class FakeGx:
    def __init__(self):
        self.messages = [{"role": "system", "content": "SYS"}]
        self.last_response = ""
        self.fail_save = False

    def save_session(self, *, strict=False):
        if self.fail_save:
            if strict:
                raise OSError("disk full")
            return
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
    gx10.init_registry(wd)
    gx10._BASE_CFG = gx10._code_defaults()
    monkeypatch.setattr(gx10, "STORE", None)
    monkeypatch.setattr(gx10, "_load_skills", lambda *a, **k: None)
    yield wd
    gx10._REGISTRY = None
    gx10._ACTIVE_PROJECT = None
    gx10._BOOT_WORKDIR = None
    gx10._BASE_CFG, gx10._EFFECTIVE_CFG = _orig_base, _orig_eff
    pc.set_current(None)


def _stage_unit(agent, title):
    """A deterministic 'unit of work' under the active project: stage a task+handover (no model/agent)."""
    approve_active_design(gx10)
    return gx10._stage_handover(
        None, "OPUS", f"## Handover\n{title}",
        task_json=json.dumps({"type": "feature", "priority": "high",
                              "title": f"Complete validated unit {title}",
                              "description": "Complete the validated unit with implementation and regression coverage."}),
        force=True,
    )


def _snapshot(agent, wd, title):
    """Mint a fresh project (cwd/<slug>) WITH a seeded unit, record its scope/paths, and stage work in it."""
    out = gx10._project_command(f"new {title} --type software", agent)
    assert out.startswith("[project] created"), out
    proj = gx10._ACTIVE_PROJECT
    info = {
        "id": proj.id,
        "mem_ns": proj.mem_ns,
        "active_mem_ns": gx10._active_mem_ns(),
        "vault": gx10.vault_root(),
        "state": gx10.state_root(),
        "root": Path(proj.root),
    }
    assert _stage_unit(agent, f"{title}-task").startswith("OK")
    return info


def test_two_projects_isolated_partitions(engine):
    wd = engine
    ag = FakeGx()
    a = _snapshot(ag, wd, "alpha")
    b = _snapshot(ag, wd, "beta")

    # distinct, non-empty (non-base) memory partitions
    assert a["mem_ns"] and b["mem_ns"] and a["mem_ns"] != b["mem_ns"]
    assert a["active_mem_ns"] == a["mem_ns"] and b["active_mem_ns"] == b["mem_ns"]

    # vault + state machinery resolve under each project's OWN root, and not under the other's
    assert _is_under(a["vault"], a["root"]) and _is_under(a["state"], a["root"])
    assert _is_under(b["vault"], b["root"]) and _is_under(b["state"], b["root"])
    assert not _is_under(a["vault"], b["root"]) and not _is_under(b["vault"], a["root"])


def test_work_artifacts_live_only_under_active_project(engine):
    wd = engine
    ag = FakeGx()
    _snapshot(ag, wd, "alpha")
    _snapshot(ag, wd, "beta")
    # CONTENT-level cross-bleed: scan the WHOLE working dir — every file carrying a project's unit-of-work
    # title must live ONLY under that project's own root (catches leakage anywhere, incl. .ironclad / base).
    assert _files_containing(wd / "alpha", "alpha-task"), "alpha's unit-of-work was not written under alpha"
    assert _files_containing(wd / "beta", "beta-task"), "beta's unit-of-work was not written under beta"
    for hit in _files_containing(wd, "alpha-task"):
        assert _is_under(hit, wd / "alpha"), f"alpha-task leaked outside wd/alpha: {hit}"
    for hit in _files_containing(wd, "beta-task"):
        assert _is_under(hit, wd / "beta"), f"beta-task leaked outside wd/beta: {hit}"


def test_base_default_project_untouched_by_project_work(engine):
    wd = engine
    ag = FakeGx()
    ag.messages.append({"role": "user", "content": "base-turn"})   # a turn on the base/default project
    _snapshot(ag, wd, "alpha")
    ag.messages.append({"role": "user", "content": "alpha-turn"})
    _snapshot(ag, wd, "beta")
    ag.messages.append({"role": "user", "content": "beta-turn"})    # a turn on beta
    # Switch all the way back to the base/default project — this forces beta's conversation to SAVE (to beta's
    # own session) and reloads the default conversation, so the beta-turn negative check below is non-vacuous.
    assert gx10._switch_command(ag, "default").startswith("[switch] now on default")

    # the base/default project never gained the new projects' roots or their work, by content (non-vacuous)
    assert not (wd / "vault" / "alpha").exists() and not (wd / "vault" / "beta").exists()
    assert not _files_containing(wd / "vault", "alpha-task") and not _files_containing(wd / "vault", "beta-task")
    # the reloaded default conversation carries ONLY its own base turn — no per-project bleed
    contents = [m.get("content") for m in ag.messages]
    assert "base-turn" in contents and "alpha-turn" not in contents and "beta-turn" not in contents
    # and the same on disk: the base/default session keeps only the base turn (beta's turn saved to beta)
    base_sess = wd / ".ironclad" / "session.json"
    assert base_sess.exists(), "the base/default session was not saved on switch-away"
    txt = base_sess.read_text(encoding="utf-8")
    assert "base-turn" in txt and "alpha-turn" not in txt and "beta-turn" not in txt
    # beta's own session DID capture beta-turn (proving the save happened — so the negative base check is real)
    assert _files_containing(wd / "beta", "beta-turn"), "beta's conversation was not saved under beta"


def test_switch_back_restores_conversation_without_bleed(engine):
    wd = engine
    ag = FakeGx()
    _snapshot(ag, wd, "alpha")
    ag.messages.append({"role": "user", "content": "alpha-turn"})
    _snapshot(ag, wd, "beta")
    ag.messages.append({"role": "user", "content": "beta-turn"})

    assert gx10._switch_command(ag, "alpha").startswith("[switch] now on alpha")
    assert gx10._ACTIVE_PROJECT.id == "alpha"
    contents = [m.get("content") for m in ag.messages]
    assert "alpha-turn" in contents and "beta-turn" not in contents   # no cross-project conversation bleed


def test_full_cycle_returns_to_default_clean(engine):
    wd = engine
    ag = FakeGx()
    _snapshot(ag, wd, "alpha")
    _snapshot(ag, wd, "beta")
    # switch all the way back to the base/default project
    assert gx10._switch_command(ag, "default").startswith("[switch] now on default")
    assert gx10._ACTIVE_PROJECT.id == "default"
    # base memory partition is the empty/legacy base again (byte-identical to a single-project install)
    assert gx10._active_mem_ns() == ""
    # both projects remain registered + isolated on disk
    ids = {p.id for p in gx10._REGISTRY.list()}
    assert {"alpha", "beta", "default"} <= ids
    assert (wd / "alpha").is_dir() and (wd / "beta").is_dir()
