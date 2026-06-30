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
import gx10
import memory


def _mm(agent_id="ironclad", user_id=""):
    m = memory.MemoryManager.__new__(memory.MemoryManager)
    m.agent_id = agent_id
    m.user_id = user_id
    return m


def test_mem_scope_main_is_bare():
    assert ProjectContext("p", "/r", "ns123").mem_scope() == "ns123"


def test_mem_scope_non_main_composes():
    assert ProjectContext("p", "/r", "ns123", track="feature").mem_scope() == "ns123::track::feature"


def test_mem_scope_empty_ns_unchanged():
    assert ProjectContext("p", "/r", "", track="feature").mem_scope() == ""


def test_mem_scope_unsafe_track_falls_back_to_main():
    for bad in ("../x", ".", "..", "a/b", "a b", ""):
        assert ProjectContext("p", "/r", "ns123", track=bad).mem_scope() == "ns123"


def test_track_safety_predicates_agree_and_are_crash_safe():
    # the vault (gx10._is_safe_track) and the memory sub-scope (project_context._safe_track) must agree
    # for EVERY input, including non-str (no raise) — else vault and memory could pick different tracks.
    for val in ("main", "feature", "v1.2", "..", ".", "a/b", "", None, 123, [], object()):
        assert gx10._is_safe_track(val) == pc._safe_track(val)


def test_mem_scope_ns_with_double_colon_still_composes():
    # a mem_ns that itself contains '::' still composes deterministically (no special-casing/collision)
    assert ProjectContext("p", "/r", "a::b", track="feature").mem_scope() == "a::b::track::feature"


def test_active_mem_ns_no_ctx_is_default():
    assert pc.current() is None
    assert gx10._active_mem_ns() == ""
    assert gx10._active_mem_ns("fallback") == "fallback"


def test_active_mem_ns_main_is_bare():
    with pc.use(ProjectContext("p", "/r", "ns123")):
        assert gx10._active_mem_ns() == "ns123"


def test_active_mem_ns_track_composes():
    with pc.use(ProjectContext("p", "/r", "ns123", track="feature")):
        assert gx10._active_mem_ns() == "ns123::track::feature"


def test_active_warm_session_no_ctx_is_global():
    assert pc.current() is None
    assert gx10._active_warm_session() == gx10.WARM_SESSION_ID


def test_active_warm_session_track_composes():
    with pc.use(ProjectContext("p", "/r", "ns123", track="feature")):
        assert gx10._active_warm_session() == "ns123::track::feature"


def test_cold_ids_track_scoped():
    with pc.use(ProjectContext("p", "/r", "ns123", track="feature")):
        assert _mm()._ids()["agent_id"] == "ns123::track::feature"


def test_cold_ids_main_is_bare():
    with pc.use(ProjectContext("p", "/r", "ns123")):
        assert _mm()._ids()["agent_id"] == "ns123"


def test_cold_ids_no_ctx_is_instance_default():
    assert pc.current() is None
    assert _mm(agent_id="ironclad")._ids()["agent_id"] == "ironclad"


def test_two_tracks_isolated_partitions():
    a = ProjectContext("p", "/r", "ns123", track="a").mem_scope()
    b = ProjectContext("p", "/r", "ns123", track="b").mem_scope()
    assert a != b and a == "ns123::track::a" and b == "ns123::track::b"
