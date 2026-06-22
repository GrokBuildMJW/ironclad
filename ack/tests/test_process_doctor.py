"""process-doctor invariant checks (#190, ADR-0007), offline.

The check LOGIC is pure (data -> violations) so it is unit-tested without GitHub. Lives in
`scripts/ci/` (private) -> skips in an installed/clean-room tree. The live pass runs in
reconcile.yml; here we pin the seed invariant (closed issue => no status/* label) + its heal +
the fail-closed/registry wiring.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PD = _REPO / "scripts" / "ci" / "process_doctor.py"

pytestmark = pytest.mark.skipif(
    not _PD.is_file(),
    reason="private CI process-doctor (scripts/ci/process_doctor.py) absent — installed/clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_procdoc", _PD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # @dataclass resolves annotations via sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_closed_with_status_label_is_a_violation():
    pd = _load()
    issues = [
        {"number": 1, "state": "CLOSED", "labels": ["type/task", "status/ready"]},
        {"number": 2, "state": "CLOSED", "labels": ["status/blocked", "area/ci"]},
        {"number": 3, "state": "OPEN", "labels": ["status/ready"]},      # open => fine
        {"number": 4, "state": "CLOSED", "labels": ["type/feature"]},     # no status/* => fine
    ]
    v = pd.closed_no_status_label(issues)
    assert any("#1" in x and "status/ready" in x for x in v)
    assert any("#2" in x and "status/blocked" in x for x in v)
    assert not any("#3" in x for x in v)      # open issue keeps its status label
    assert not any("#4" in x for x in v)      # closed but clean
    assert len(v) == 2


def test_clean_state_has_no_violations():
    pd = _load()
    issues = [
        {"number": 1, "state": "CLOSED", "labels": ["type/task"]},
        {"number": 2, "state": "OPEN", "labels": ["status/ready", "type/feature"]},
    ]
    assert pd.closed_no_status_label(issues) == []


def test_registry_exposes_the_seed_check_with_a_heal():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert "closed-no-status-label" in names
    assert names["closed-no-status-label"].heal is not None     # healable, not assert-only


def test_heal_removes_only_status_labels_on_closed_issues():
    pd = _load()
    calls = []

    class FakeClient:
        def remove_label(self, number, label):
            calls.append((number, label))

    issues = [
        {"number": 1, "state": "CLOSED", "labels": ["status/ready", "type/task"]},
        {"number": 2, "state": "OPEN", "labels": ["status/ready"]},        # must NOT touch (open)
        {"number": 3, "state": "CLOSED", "labels": ["area/ci"]},           # nothing to remove
    ]
    acted = pd._heal_closed_no_status_label(FakeClient(), issues)
    assert calls == [(1, "status/ready")]
    assert len(acted) == 1


def test_auth_error_type_exists_for_fail_closed():
    pd = _load()
    assert issubclass(pd.AuthError, Exception)


# ── board invariant (#191): closed issue ⇒ board Done ─────────────────────────
def test_board_closed_not_done_is_a_violation():
    pd = _load()
    data = {
        "items": [
            {"number": 1, "item_id": "PVTI_a", "board": "In Progress"},  # closed + not Done -> violation
            {"number": 2, "item_id": "PVTI_b", "board": "Done"},          # closed + Done -> fine
            {"number": 3, "item_id": "PVTI_c", "board": "In Progress"},  # OPEN + In Progress -> fine
            {"number": 4, "item_id": "PVTI_d", "board": "Todo"},          # closed + Todo -> violation
        ],
        "issues": [
            {"number": 1, "state": "CLOSED"}, {"number": 2, "state": "CLOSED"},
            {"number": 3, "state": "OPEN"}, {"number": 4, "state": "CLOSED"},
        ],
    }
    v = pd.board_closed_must_be_done(data)
    assert any("#1" in x for x in v) and any("#4" in x for x in v)
    assert not any("#2" in x for x in v) and not any("#3" in x for x in v)
    assert len(v) == 2


def test_board_heal_sets_only_closed_non_done_cards():
    pd = _load()
    calls = []

    class FakeClient:
        def set_board_done(self, item_id):
            calls.append(item_id)

    data = {
        "items": [
            {"number": 1, "item_id": "PVTI_a", "board": "In Progress"},
            {"number": 2, "item_id": "PVTI_b", "board": "Done"},
            {"number": 3, "item_id": "PVTI_c", "board": "In Progress"},
        ],
        "issues": [{"number": 1, "state": "CLOSED"}, {"number": 2, "state": "CLOSED"},
                   {"number": 3, "state": "OPEN"}],
    }
    acted = pd._heal_board_closed_must_be_done(FakeClient(), data)
    assert calls == ["PVTI_a"] and len(acted) == 1


def test_board_check_registered_with_heal():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert "board-closed-is-done" in names and names["board-closed-is-done"].heal is not None


# ── issue/milestone invariants (#192) ─────────────────────────────────────────
def test_open_epic_without_milestone_is_flagged():
    pd = _load()
    issues = [
        {"number": 75, "state": "OPEN", "labels": ["type/feature", "area/skills"], "milestone": None},
        {"number": 20, "state": "OPEN", "labels": ["type/feature"], "milestone": "Enterprise"},  # ok
        {"number": 9, "state": "OPEN", "labels": ["type/task"], "milestone": None},              # not an epic
        {"number": 8, "state": "CLOSED", "labels": ["type/feature"], "milestone": None},          # closed
    ]
    v = pd.open_epic_has_milestone(issues)
    assert v == [x for x in v if "#75" in x] and len(v) == 1


def test_delivered_milestone_must_be_closed():
    pd = _load()
    ms = [
        {"number": 3, "title": "Skill-gen", "state": "open", "open": 0, "closed": 6},   # delivered, still open -> flag
        {"number": 1, "title": "Enterprise", "state": "open", "open": 5, "closed": 0},  # active -> fine
        {"number": 9, "title": "Empty new", "state": "open", "open": 0, "closed": 0},   # brand new -> NOT flagged
        {"number": 6, "title": "Done phase", "state": "closed", "open": 0, "closed": 6},# already closed -> fine
    ]
    v = pd.delivered_milestone_closed(ms)
    assert len(v) == 1 and "M3" in v[0]


def test_epic_milestone_check_is_warn_only():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert names["open-epic-has-milestone"].warn is True
    assert names["delivered-milestone-closed"].heal is not None


def test_open_milestone_without_description_is_flagged():
    pd = _load()
    ms = [
        {"number": 7, "title": "No-desc active", "state": "open", "open": 3, "closed": 0, "description": ""},   # flag
        {"number": 1, "title": "Has-desc", "state": "open", "open": 5, "closed": 0, "description": "Enterprise…"},  # ok
        {"number": 9, "title": "New empty", "state": "open", "open": 0, "closed": 0, "description": ""},        # no work -> ok
        {"number": 3, "title": "Closed", "state": "closed", "open": 0, "closed": 6, "description": ""},          # closed -> ok
    ]
    v = pd.open_milestone_has_description(ms)
    assert len(v) == 1 and "M7" in v[0]
    assert pd.registry() and any(c.name == "open-milestone-has-description" and c.warn for c in pd.registry())


# ── DEV_LOOP self-consistency (#197) ──────────────────────────────────────────
def test_devloop_dangling_workflow_and_stale_version_are_caught():
    pd = _load()
    known = {"ci.yml", "reconcile.yml", "epic.yml"}
    text = ("run ci.yml and reconcile.yml; form epic.yml; but mirror-to-dev.yml is cited.\n"
            "Stand: aktuell v0.0.7 ...")
    v = pd.devloop_dangling_refs(text, known, "0.0.14")
    assert any("mirror-to-dev.yml" in x for x in v)      # ghost workflow caught
    assert any("v0.0.7" in x and "0.0.14" in x for x in v)  # stale current version caught
    assert not any("ci.yml" in x or "epic.yml" in x for x in v)  # real refs fine


def test_devloop_clean_text_passes():
    pd = _load()
    known = {"ci.yml", "node-client.yml"}
    text = "use ci.yml + node-client.yml; aktuell v0.0.14; historical range v0.0.1-v0.0.14."
    assert pd.devloop_dangling_refs(text, known, "0.0.14") == []


def test_real_devloop_is_self_consistent():
    pd = _load()
    d = pd._fetch_devloop()
    assert pd.devloop_dangling_refs(d["text"], d["known"], d["version"]) == [], "live DEV_LOOP drift"


# ── upstream round-trip invariants (#194) ─────────────────────────────────────
def test_upstream_closed_resolved_without_released_is_drift():
    pd = _load()
    issues = [
        {"number": 5, "state": "CLOSED", "labels": ["triaged", "resolved"]},            # closed off delivery -> flag
        {"number": 6, "state": "CLOSED", "labels": ["resolved", "released"]},           # delivered -> fine
        {"number": 4, "state": "CLOSED", "labels": ["triaged"]},                        # never resolved (won't-fix) -> fine
        {"number": 7, "state": "OPEN", "labels": ["resolved"]},                         # open, awaiting delivery -> fine
    ]
    v = pd.upstream_closed_must_be_released(issues)
    assert len(v) == 1 and "ironclad#5" in v[0]


def test_upstream_released_heal_stamps_only_the_stranded_one():
    pd = _load()
    calls = []

    class FakeClient:
        def add_label(self, number, label, repo=None):
            calls.append((number, label, repo))

    issues = [
        {"number": 5, "state": "CLOSED", "labels": ["triaged", "resolved"]},
        {"number": 6, "state": "CLOSED", "labels": ["resolved", "released"]},
        {"number": 4, "state": "CLOSED", "labels": ["triaged"]},
    ]
    acted = pd._heal_upstream_released(FakeClient(), issues)
    assert len(acted) == 1
    assert calls == [(5, "released", pd.PUBLIC_REPO)]


def test_triaged_without_mirror_is_warned():
    pd = _load()
    public_open = [
        {"number": 11, "state": "OPEN", "labels": ["triaged"]},   # no mirror -> warn
        {"number": 12, "state": "OPEN", "labels": ["triaged"]},   # mirror exists -> fine
        {"number": 13, "state": "OPEN", "labels": []},            # not triaged yet -> fine
    ]
    v = pd.upstream_triaged_has_mirror(public_open, {12})
    assert len(v) == 1 and "ironclad#11" in v[0]


def test_mirror_wiring_live_flags_only_a_failed_last_run():
    pd = _load()
    assert pd.mirror_wiring_live({}) == []                                           # no run yet
    assert pd.mirror_wiring_live({"status": "completed", "conclusion": "success"}) == []
    assert pd.mirror_wiring_live({"status": "in_progress", "conclusion": None}) == []  # still running
    bad = pd.mirror_wiring_live({"status": "completed", "conclusion": "failure"})
    assert len(bad) == 1 and "failure" in bad[0]


# ── board: open + assigned ⇒ In Progress (#194, the epic-visibility gap) ───────
def test_open_assigned_not_started_is_a_violation():
    pd = _load()
    data = {
        "items": [
            {"number": 1, "board": "Todo"},          # open+assigned+Todo -> flag
            {"number": 3, "board": "In Progress"},    # already started -> fine
            {"number": 4, "board": "In Review"},      # PR linked -> started -> fine
        ],
        "issues": [
            {"number": 1, "state": "OPEN", "assignees": ["MarcoWolf"]},
            {"number": 2, "state": "OPEN", "assignees": ["MarcoWolf"]},   # not on board at all -> flag
            {"number": 3, "state": "OPEN", "assignees": ["MarcoWolf"]},
            {"number": 4, "state": "OPEN", "assignees": ["MarcoWolf"]},
            {"number": 5, "state": "OPEN", "assignees": []},               # unassigned -> fine (Todo legit)
        ],
    }
    v = pd.open_assigned_not_started(data)
    assert any("#1" in x for x in v) and any("#2" in x for x in v)
    assert not any("#3" in x or "#4" in x or "#5" in x for x in v)
    assert len(v) == 2


def test_open_assigned_heal_moves_only_stranded_cards():
    pd = _load()
    moved = []

    class FakeClient:
        def ensure_board_in_progress(self, url):
            moved.append(url)

    data = {
        "items": [{"number": 1, "board": "Todo"}, {"number": 3, "board": "In Progress"}],
        "issues": [
            {"number": 1, "state": "OPEN", "assignees": ["x"], "url": "u1"},
            {"number": 3, "state": "OPEN", "assignees": ["x"], "url": "u3"},
        ],
    }
    acted = pd._heal_open_assigned_not_started(FakeClient(), data)
    assert moved == ["u1"] and len(acted) == 1


def test_upstream_checks_grouped_for_token_routing():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert names["upstream-closed-is-released"].group == "upstream"
    assert names["upstream-closed-is-released"].heal is not None
    assert names["upstream-triaged-has-mirror"].warn is True
    assert names["mirror-wiring-live"].warn is True
    assert names["open-assigned-in-progress"].group == "repo"      # board write -> PROJECTS_TOKEN
    assert names["open-assigned-in-progress"].heal is not None
