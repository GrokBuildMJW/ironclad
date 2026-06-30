from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=lambda **kw: object()))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

import project_context as pc
from project_context import ProjectContext
import project_registry as pr
import gx10
import memory


def _mm(agent_id="ironclad", user_id=""):
    m = memory.MemoryManager.__new__(memory.MemoryManager)
    m.agent_id = agent_id
    m.user_id = user_id
    return m


def test_single_handle_follows_ctx_switch():
    mm = _mm()
    with pc.use(ProjectContext("a", "/a", "nsA")):
        assert mm._ids()["agent_id"] == "nsA"
    with pc.use(ProjectContext("b", "/b", "nsB")):
        assert mm._ids()["agent_id"] == "nsB"
    assert pc.current() is None
    assert mm._ids()["agent_id"] == "ironclad"


def test_single_handle_switch_back_restores():
    mm = _mm()
    with pc.use(ProjectContext("a", "/a", "nsA")):
        first = mm._ids()["agent_id"]
    with pc.use(ProjectContext("b", "/b", "nsB")):
        pass
    with pc.use(ProjectContext("a", "/a", "nsA")):
        again = mm._ids()["agent_id"]
    assert first == again == "nsA"


def test_single_handle_track_dimension_follows():
    mm = _mm()
    with pc.use(ProjectContext("a", "/a", "nsA", track="feature")):
        assert mm._ids()["agent_id"] == "nsA::track::feature"
    with pc.use(ProjectContext("a", "/a", "nsA")):
        assert mm._ids()["agent_id"] == "nsA"


def test_registered_project_never_base():
    mm = _mm(agent_id="ironclad")
    with pc.use(ProjectContext("a", "/a", "nsA")):
        assert mm._ids()["agent_id"] != "ironclad"


def test_engine_ctx_for_refuses_nondefault_invalid_mem_ns():
    # S14-2 fail-closed: a non-default project with an empty/invalid mem_ns must NOT bind (it would leak
    # to the base partition). A corrupt persisted registry entry is the real-world trigger.
    bad = pr.Project(id="proj", slug="proj", root="/r", mem_ns="")
    with pytest.raises(ValueError):
        gx10._engine_ctx_for(bad)
    with pytest.raises(ValueError):
        gx10._engine_ctx_for(pr.Project(id="proj", slug="proj", root="/r", mem_ns="x"))  # low-entropy


def test_engine_ctx_for_default_empty_mem_ns_is_allowed():
    # the implicit `default` project legitimately uses the base partition (empty mem_ns) — never refused.
    dflt = pr.Project(id=pr.DEFAULT_PROJECT_ID, slug=pr.DEFAULT_PROJECT_ID, root="/r", mem_ns="")
    ctx = gx10._engine_ctx_for(dflt)
    assert ctx.mem_ns == ""


def test_engine_ctx_for_nondefault_valid_mem_ns_binds():
    good = pr.Project(id="proj", slug="proj", root="/r", mem_ns=pr.mint_mem_ns())
    ctx = gx10._engine_ctx_for(good)
    assert pr.valid_mem_ns(ctx.mem_ns)


def test_active_accessors_follow_switch():
    with pc.use(ProjectContext("a", "/a", "nsA")):
        assert gx10._active_mem_ns() == "nsA" and gx10._active_warm_session() == "nsA"
    with pc.use(ProjectContext("b", "/b", "nsB", track="t")):
        assert gx10._active_mem_ns() == "nsB::track::t" and gx10._active_warm_session() == "nsB::track::t"
    assert pc.current() is None
    assert gx10._active_mem_ns() == "" and gx10._active_warm_session() == gx10.WARM_SESSION_ID


def test_two_projects_isolated_partitions():
    mm = _mm()
    with pc.use(ProjectContext("a", "/a", "nsA")):
        a = mm._ids()["agent_id"]
    with pc.use(ProjectContext("b", "/b", "nsB")):
        b = mm._ids()["agent_id"]
    assert a != b
