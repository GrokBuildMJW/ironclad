"""ACE-WIRE (#855 / #863): the engine-side :class:`~engine.playbook_store.PlaybookStore` — the always-on
ACE backend that supersedes the #602 EngineLessonStore. Pins the dual contract (the string LessonProvider
surface AND the typed record/by_category Process-SC surface), the ACE-native query-aware ``context_for`` +
``adapt``, the 32k-window cap, the one-time legacy migration, and fail-soft throughout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2] / "engine"
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))

from playbook_store import (PlaybookStore, migrate_lessons, _render_bullets, record_unit_bullets,
                            read_unit_bullets, record_fork_proposal, read_fork_proposal, list_fork_proposals)
from ack.ace import Trajectory, HELPFUL

_SCOPE = "proj::track::main"


def _store(tmp_path, **kw):
    return PlaybookStore(tmp_path / "ace_playbooks", **kw)


# ─── the string LessonProvider surface (engine read/write + facade) ──────────────────────────────────
def test_report_lesson_then_get_and_brief(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "always write the test first", {"category": "best_known_path"})
    assert s.get_lessons(_SCOPE) == ["always write the test first"]
    brief = s.brief([_SCOPE])
    assert "always write the test first" in brief and "[strategies_and_hard_rules]" in brief


def test_get_lessons_is_query_ranked(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "validate the parser output")
    s.report_lesson(_SCOPE, "cache the database handle")
    top = s.get_lessons(_SCOPE, query="parser", limit=1)
    assert top == ["validate the parser output"]            # query relevance beats recency


def test_report_lesson_dedupes_exact_content(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "do not mutate global state")
    s.report_lesson(_SCOPE, "do not mutate global state")   # exact duplicate → bumped, not re-added
    assert len(s.get_lessons(_SCOPE)) == 1


def test_empty_scope_is_noop(tmp_path):
    s = _store(tmp_path)
    s.report_lesson("", "ignored")
    s.report_lesson("   ", "ignored")
    assert s.get_lessons("") == [] and s.brief([""]) == ""


# ─── the typed surface the in-process #602 Process-SC consumers couple to ────────────────────────────
def test_record_and_by_category_roundtrip(tmp_path):
    s = _store(tmp_path)
    s.record(_SCOPE, "the X approach works", "best_known_path")
    s.record(_SCOPE, "never call Y twice", "known_bad_strategy")
    assert s.by_category(_SCOPE, "best_known_path") == ["the X approach works"]
    assert s.by_category(_SCOPE, "known_bad_strategy") == ["never call Y twice"]


def test_forget_purges_the_scope(tmp_path):
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "a lesson")
    assert s.forget(_SCOPE) is True
    assert s.get_lessons(_SCOPE) == [] and s.forget(_SCOPE) is False   # idempotent


def test_forget_also_purges_history_and_quarantine(tmp_path):
    # #1552: forget must remove the version-history and quarantine side files too, so a forgotten lesson
    # cannot be recovered via versions()/rollback().
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "secret lesson")
    v = s.snapshot(_SCOPE)["version"]                     # creates <hash>.history.json
    assert s.get_lessons(_SCOPE) == ["secret lesson"]
    assert s.versions(_SCOPE)                             # history present before forget
    assert s.forget(_SCOPE) is True
    assert s.get_lessons(_SCOPE) == []
    assert s.versions(_SCOPE) == []                       # history file gone → nothing to roll back to
    base = tmp_path / "ace_playbooks"
    assert not list(base.glob("*.history.json")) and not list(base.glob("*.quarantine.json"))  # side files purged
    s.rollback(_SCOPE, v)                                 # rolling back to the forgotten version restores nothing
    assert s.get_lessons(_SCOPE) == []


def test_rollback_surfaces_a_persistence_failure(tmp_path, monkeypatch):
    # #1551: if persisting the restored playbook fails (read-only ACE dir / full disk), rollback must return an
    # error — NOT a false success naming the target version — so the operator never believes an unsafe playbook
    # was removed while the harmful active JSON is in fact unchanged on disk.
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "v1 safe lesson")
    v1 = s.snapshot(_SCOPE)["version"]
    s.report_lesson(_SCOPE, "v2 harmful lesson")   # the current (harmful) active state
    monkeypatch.setattr(s, "_save", lambda scope, pb: False)   # the restored playbook never reaches disk
    res = s.rollback(_SCOPE, v1)
    assert "error" in res and "rolled_back_to" not in res      # honest failure, not a false success
    assert "could not persist" in res["error"]
    assert "v2 harmful lesson" in _store(tmp_path).get_lessons(_SCOPE)   # active playbook UNCHANGED (no rollback)


def test_rollback_surfaces_a_history_persistence_failure(tmp_path, monkeypatch):
    # #1551: the playbook saved but its version history did not — still an error, never a clean success.
    s = _store(tmp_path)
    s.report_lesson(_SCOPE, "v1")
    v1 = s.snapshot(_SCOPE)["version"]
    s.report_lesson(_SCOPE, "v2")
    monkeypatch.setattr(s, "_save_history", lambda scope, hist: False)
    res = s.rollback(_SCOPE, v1)
    assert "error" in res and "rolled_back_to" not in res


def test_persistence_across_instances(tmp_path):
    _store(tmp_path).report_lesson(_SCOPE, "persisted lesson")
    assert _store(tmp_path).get_lessons(_SCOPE) == ["persisted lesson"]   # one JSON file per scope


# ─── the 32k-window cap (the #366 guard) ─────────────────────────────────────────────────────────────
def test_max_bullets_caps_the_playbook(tmp_path):
    s = _store(tmp_path, max_bullets=3)
    for i in range(10):
        s.report_lesson(_SCOPE, f"lesson number {i}")
    assert len(s.get_lessons(_SCOPE, limit=100)) <= 3       # bounded → the playbook can't overflow the window


# ─── the ACE-native query-aware Generator read (the 32k-safe relevant subset) ────────────────────────
def test_context_for_is_query_aware_and_bounded(tmp_path):
    s = _store(tmp_path)
    for i in range(20):
        s.report_lesson(_SCOPE, f"strategy about topic {i}")
    s.report_lesson(_SCOPE, "the parser must be validated")
    ctx = s.context_for([_SCOPE], query="parser validation", limit=3)
    assert "the parser must be validated" in ctx
    assert ctx.count("\n- ") <= 3                            # only the relevant subset is injected (not all 21)


# ─── the online adaptation step (fail-soft without a model) ──────────────────────────────────────────
def test_adapt_is_noop_without_chat(tmp_path):
    s = _store(tmp_path)                                      # no chat injected
    out = s.adapt(Trajectory(query="q", outcome="success"), scope=_SCOPE)
    assert out["skipped"] is True and s.get_lessons(_SCOPE) == []


def test_adapt_learns_with_an_injected_chat(tmp_path):
    s = _store(tmp_path)
    payload = json.dumps({"insights": [{"content": "write tests first",
                                        "section": "strategies_and_hard_rules"}], "ratings": []})
    s.set_transports(chat=lambda prompt: payload)
    out = s.adapt(Trajectory(query="add(a,b)", outcome="success"), scope=_SCOPE)
    assert out.get("added") == 1
    assert "write tests first" in s.get_lessons(_SCOPE)       # the reflected bullet is persisted (cumulative)


def test_adapt_fail_soft_on_raising_chat(tmp_path):
    s = _store(tmp_path)
    def boom(prompt):
        raise RuntimeError("model down")
    s.set_transports(chat=boom)
    out = s.adapt(Trajectory(query="q", outcome="success"), scope=_SCOPE)   # never raises
    assert s.get_lessons(_SCOPE) == []                       # empty reflection → no-op


# ─── one-time migration of the legacy #602 EngineLessonStore tree ────────────────────────────────────
def test_migrate_lessons_replays_legacy_tree(tmp_path):
    legacy = tmp_path / "lessons"
    legacy.mkdir(parents=True)
    (legacy / "abc.json").write_text(json.dumps({
        "scope": _SCOPE, "next_seq": 3,
        "lessons": [{"seq": 1, "text": "legacy lesson A", "category": "best_known_path", "meta": {}},
                    {"seq": 2, "text": "legacy lesson B", "category": "known_bad_strategy", "meta": {}}],
    }), encoding="utf-8")
    s = _store(tmp_path)
    n = migrate_lessons(legacy, s)
    assert n == 2
    assert set(s.get_lessons(_SCOPE)) == {"legacy lesson A", "legacy lesson B"}
    assert s.by_category(_SCOPE, "known_bad_strategy") == ["legacy lesson B"]


def test_migrate_lessons_fail_soft_on_missing_or_corrupt(tmp_path):
    s = _store(tmp_path)
    assert migrate_lessons(tmp_path / "does-not-exist", s) == 0      # missing tree → 0, never raises
    bad = tmp_path / "lessons"; bad.mkdir()
    (bad / "corrupt.json").write_text("{not json", encoding="utf-8")
    assert migrate_lessons(bad, s) == 0                              # corrupt file skipped


def test_render_bullets_groups_by_section_with_ids(tmp_path):
    s = _store(tmp_path)
    s.record(_SCOPE, "a rule", "known_bad_strategy")
    rendered = _render_bullets(s._load(_SCOPE).bullets())
    assert rendered.startswith("[strategies_and_hard_rules]")
    assert "- [b-0] a rule" in rendered and "#known_bad_strategy" in rendered


# ─── M4-3 (#880): the durable unit→injected-bullet-ids correlation map ───────────────────────────────
def test_unit_bullets_record_read_and_union(tmp_path):
    record_unit_bullets(tmp_path, "880", ["b-0", "b-1"])
    record_unit_bullets(tmp_path, "880", ["b-1", "b-2"])      # a unit's 2nd handover → UNION, not overwrite
    assert read_unit_bullets(tmp_path, "880") == ["b-0", "b-1", "b-2"]
    assert read_unit_bullets(tmp_path, "999") == []           # unknown unit → []


def test_unit_bullets_fail_soft_and_noops(tmp_path):
    record_unit_bullets(tmp_path, "", ["b-0"]); record_unit_bullets(tmp_path, "u", [])   # empty key / ids → no-op
    assert read_unit_bullets(tmp_path, "u") == [] and read_unit_bullets(tmp_path, "") == []
    # a corrupt map file reads as empty, never raises
    (tmp_path / "ace_devbullets.json").write_text("{not json", encoding="utf-8")
    assert read_unit_bullets(tmp_path, "880") == []
    record_unit_bullets(tmp_path, "880", ["b-9"])             # rewrites cleanly over the corrupt file
    assert read_unit_bullets(tmp_path, "880") == ["b-9"]


# ─── #905: ace.top_k is live-configurable and caps context_for ──────────────────────────────────────
def test_configure_top_k_caps_context_for(tmp_path):
    s = _store(tmp_path)
    for i in range(6):
        s.record(_SCOPE, f"testing lesson number {i}", "apis_to_use")
    s.configure(top_k=2)
    assert s.context_for([_SCOPE], query="testing").count("- [") == 2   # capped to top_k
    s.configure(top_k=5)
    assert s.context_for([_SCOPE], query="testing").count("- [") == 5
    s.configure(top_k="bad")                                            # malformed ⇒ kept (no silent 0)
    assert s.context_for([_SCOPE], query="testing").count("- [") == 5


# ─── M5-3 (#884): the fork→proposal pointer ──────────────────────────────────────────────────────────
def test_fork_proposal_record_read_and_latest_wins(tmp_path):
    record_fork_proposal(tmp_path, "880", "## matrix v1")
    record_fork_proposal(tmp_path, "880", "## matrix v2")     # latest matrix per fork wins (overwrite)
    assert read_fork_proposal(tmp_path, "880") == "## matrix v2"
    assert read_fork_proposal(tmp_path, "999") == ""          # unknown fork → ""


def test_fork_proposal_fail_soft_and_bounded(tmp_path):
    record_fork_proposal(tmp_path, "", "x"); record_fork_proposal(tmp_path, "u", "")   # empty key/text → no-op
    assert read_fork_proposal(tmp_path, "u") == ""
    record_fork_proposal(tmp_path, "u", "y" * 50000)          # oversized text is capped, never a blob
    assert 0 < len(read_fork_proposal(tmp_path, "u")) <= 20000


def test_list_fork_proposals(tmp_path):
    assert list_fork_proposals(tmp_path) == []                # none yet
    record_fork_proposal(tmp_path, "702", "## m2")
    record_fork_proposal(tmp_path, "701", "## m1")
    assert list_fork_proposals(tmp_path) == ["701", "702"]     # sorted units with a recorded proposal
