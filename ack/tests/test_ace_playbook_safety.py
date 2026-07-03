"""#1082 (epic #1043 quick-win): operator-facing ACE playbook safety.

The learned playbook adapts silently; the M-002 versioning (snapshot/rollback) and Q-001 unlearn primitives
existed in ack.ace.robust but were unreachable. This wires them into the PlaybookStore (persisted per-scope
history) and the `/ace snapshot|versions|rollback|unlearn` verbs.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("openai", types.SimpleNamespace(OpenAI=object))

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from playbook_store import PlaybookStore  # noqa: E402

SCOPE = "proj::main"


def _store(tmp_path):
    return PlaybookStore(tmp_path / "ace")


def test_snapshot_then_rollback_restores_prior_playbook(tmp_path):
    st = _store(tmp_path)
    st.report_lesson(SCOPE, "first lesson")
    v1 = st.snapshot(SCOPE)["version"]
    assert v1 and st.versions(SCOPE) == [v1]
    st.report_lesson(SCOPE, "second lesson")                 # mutate after the snapshot
    r = st.rollback(SCOPE, v1)
    assert "rolled_back_to" in r
    lessons = " ".join(st.get_lessons(SCOPE))
    assert "first" in lessons and "second" not in lessons    # the second lesson is gone


def test_unlearn_removes_a_bullet_by_id(tmp_path):
    st = _store(tmp_path)
    bid = st._add(SCOPE, "forget me", section="strategies_and_hard_rules")
    assert bid
    r = st.unlearn(SCOPE, [bid])
    assert r["removed"] == 1 and r["missing"] == []
    assert "forget me" not in " ".join(st.get_lessons(SCOPE))


def test_unlearn_reports_missing_ids(tmp_path):
    st = _store(tmp_path)
    st.report_lesson(SCOPE, "keep me")
    r = st.unlearn(SCOPE, ["nope"])
    assert r["removed"] == 0 and r["missing"] == ["nope"]
    assert "keep me" in " ".join(st.get_lessons(SCOPE))       # unchanged


def test_guards_empty_scope_and_no_history(tmp_path):
    st = _store(tmp_path)
    assert st.snapshot("").get("error")
    assert st.rollback("").get("error")
    assert st.versions("") == []
    st.report_lesson(SCOPE, "only")
    assert st.rollback(SCOPE).get("error")                    # one snapshot → nothing earlier to restore


def test_history_persists_across_store_instances(tmp_path):
    st = _store(tmp_path)
    st.report_lesson(SCOPE, "durable")
    v = st.snapshot(SCOPE)["version"]
    assert PlaybookStore(tmp_path / "ace").versions(SCOPE) == [v]   # a fresh handle sees the persisted history


def test_ace_command_verbs_are_wired(monkeypatch, tmp_path):
    import gx10
    st = PlaybookStore(tmp_path / "ace2")
    monkeypatch.setattr(gx10, "_ACE_STORE", st)
    monkeypatch.setattr(gx10, "_active_mem_ns", lambda: SCOPE)
    st.report_lesson(SCOPE, "cmd lesson")
    assert "recorded version" in gx10._ace_command("snapshot")
    assert "ace versions:" in gx10._ace_command("versions")
    assert "give one or more bullet ids" in gx10._ace_command("unlearn")   # no ids → guidance, not a crash
