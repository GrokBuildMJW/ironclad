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


# ── label taxonomy + anchor hygiene (#215) ────────────────────────────────────
def test_parse_taxonomy_includes_mklabels_and_area_loop():
    pd = _load()
    text = (
        'mklabel "type/bug" d73a4a "A defect"\n'
        'mklabel "status/ready" 0e8a16 "ready"\n'
        '          for a in engine ack ci docs; do\n'
        '            mklabel "area/$a" c5def5 "Area: $a"\n'
        '          done\n'
    )
    tax = pd.parse_taxonomy(text)
    assert "type/bug" in tax and "status/ready" in tax
    assert {"area/engine", "area/ack", "area/ci", "area/docs"} <= tax
    assert "area/$a" not in tax     # the loop template is not a real label


def test_labels_match_taxonomy_flags_rogue_and_missing():
    pd = _load()
    taxonomy = {"type/bug", "type/feature", "area/ci"}
    defined = {"type/bug", "type/feature", "wontfix"}    # missing area/ci; rogue wontfix
    v = pd.labels_match_taxonomy(defined, taxonomy)
    assert any("wontfix" in x and "rogue" in x for x in v)
    assert any("area/ci" in x and "missing" in x for x in v)
    assert len(v) == 2
    assert pd.labels_match_taxonomy(taxonomy, taxonomy) == []   # in sync → clean


def test_issue_missing_type_label_warns():
    pd = _load()
    issues = [
        {"number": 1, "state": "OPEN", "labels": ["area/ci"]},                 # 0 type/* -> warn
        {"number": 2, "state": "OPEN", "labels": ["type/bug", "type/task"]},   # 2 type/* -> warn
        {"number": 3, "state": "OPEN", "labels": ["type/feature"]},            # exactly 1 -> fine
        {"number": 4, "state": "CLOSED", "labels": []},                        # closed -> ignored
    ]
    v = pd.issue_missing_type_label(issues)
    assert any("#1" in x for x in v) and any("#2" in x for x in v)
    assert not any("#3" in x or "#4" in x for x in v)
    assert len(v) == 2


def test_merged_pr_without_issue_excludes_release_prs():
    pd = _load()
    prs = [
        {"number": 10, "title": "fix: a bug", "closes": 0},          # unanchored -> warn
        {"number": 11, "title": "feat: x", "closes": 1},             # anchored -> fine
        {"number": 12, "title": "release: v0.0.15", "closes": 0},    # release -> excluded
    ]
    v = pd.merged_pr_without_issue(prs)
    assert len(v) == 1 and "#10" in v[0]


def test_label_hygiene_checks_registered_as_warn():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    for n in ("labels-match-taxonomy", "issue-has-type-label", "merged-pr-anchored"):
        assert names[n].warn is True


# ── required-checks live ⟺ SSOT (#214) ────────────────────────────────────────
def test_required_checks_live_flags_dropped_and_stale():
    pd = _load()
    data = {"repo": "GrokBuildMJW/ironclad",
            "ssot": ["test (3.10)", "secret-scan"], "live": ["test (3.10)", "extra-thing"]}
    v = pd.required_checks_live_match(data)
    assert any("secret-scan" in x and "gate dropped" in x for x in v)        # in SSOT, not live
    assert any("extra-thing" in x and "undocumented" in x for x in v)        # live, not in SSOT
    assert len(v) == 2


def test_required_checks_live_clean_and_failsoft():
    pd = _load()
    same = ["test (3.10)", "secret-scan"]
    assert pd.required_checks_live_match({"repo": "r", "ssot": same, "live": list(same)}) == []
    assert pd.required_checks_live_match({"repo": "r", "ssot": same, "live": None}) == []   # unreadable → inert
    assert pd.registry() and any(c.name == "required-checks-live" for c in pd.registry())


# ── plugin-mirror parity (#213) ───────────────────────────────────────────────
def test_plugin_triaged_without_mirror_is_warned():
    pd = _load()
    plugin_open = [
        {"number": 3, "labels": ["needs/framework", "triaged"]},   # triaged, no mirror -> warn
        {"number": 4, "labels": ["needs/framework", "triaged"]},   # triaged, mirror exists -> fine
        {"number": 5, "labels": ["needs/framework"]},              # not triaged yet -> fine
    ]
    v = pd.plugin_triaged_has_mirror(plugin_open, {4})
    assert len(v) == 1 and "#3" in v[0]


def test_plugin_triaged_inert_when_repo_unreachable():
    pd = _load()
    assert pd.plugin_triaged_has_mirror([], {7}) == []   # fail-soft: no plugin issues -> no findings


def test_plugin_checks_registered_as_warn_upstream_group():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert names["plugin-triaged-has-mirror"].warn is True and names["plugin-triaged-has-mirror"].group == "upstream"
    assert names["plugin-mirror-live"].warn is True and names["plugin-mirror-live"].group == "upstream"


# ── release tag <-> CHANGELOG <-> pyproject coupling (#212) ────────────────────
def test_release_tag_without_changelog_is_an_orphan():
    pd = _load()
    data = {"tags": ["0.0.15", "0.0.2", "0.0.1"], "changelog": ["0.0.15"], "version": "0.0.15"}
    v = pd.release_tag_has_changelog(data)
    assert any("v0.0.2" in x for x in v) and any("v0.0.1" in x for x in v)
    assert not any("v0.0.15" in x for x in v)
    assert len(v) == 2


def test_changelog_without_tag_warns_excluding_current_version():
    pd = _load()
    # 0.0.14 lost its tag (drift → warn); 0.0.16 is the current pyproject cut-but-unreleased (excluded)
    data = {"tags": ["0.0.15"], "changelog": ["0.0.16", "0.0.15", "0.0.14"], "version": "0.0.16"}
    v = pd.changelog_has_release_tag(data)
    assert any("0.0.14" in x for x in v)
    assert not any("0.0.16" in x for x in v)   # current version is allowed to be untagged (pending release)
    assert not any("0.0.15" in x for x in v)
    assert len(v) == 1


def test_norm_ver_strips_leading_v():
    pd = _load()
    assert pd._norm_ver("v0.0.15") == "0.0.15" and pd._norm_ver("0.0.15") == "0.0.15"


def test_release_coupling_checks_registered():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert "release-tag-has-changelog" in names and names["release-tag-has-changelog"].warn is False
    assert names["changelog-has-release-tag"].warn is True


def test_upstream_checks_grouped_for_token_routing():
    pd = _load()
    names = {c.name: c for c in pd.registry()}
    assert names["upstream-closed-is-released"].group == "upstream"
    assert names["upstream-closed-is-released"].heal is not None
    assert names["upstream-triaged-has-mirror"].warn is True
    assert names["mirror-wiring-live"].warn is True
    assert names["open-assigned-in-progress"].group == "repo"      # board write -> PROJECTS_TOKEN
    assert names["open-assigned-in-progress"].heal is not None


# ── audit S2 follow-ups: epic completeness (F-I-01) + upstream delivery (F-F-01), epic #223 ──────
def test_closed_epic_without_native_subissues_is_a_violation():
    pd = _load()
    gf = pd._PRE_NATIVE_SUBISSUE_EPICS
    epics = [
        {"number": 300, "parent": None, "sub_total": 0, "sub_open": 0},   # new epic, no tracking -> FAIL
        {"number": 301, "parent": None, "sub_total": 3, "sub_open": 1},   # new epic, an open sub -> FAIL
        {"number": 302, "parent": None, "sub_total": 2, "sub_open": 0},   # new epic, fully tracked -> fine
    ]
    v = pd.closed_epic_untracked(epics, gf)
    assert any("#300" in x and "NO native sub-issues" in x for x in v)
    assert any("#301" in x and "still-OPEN" in x for x in v)
    assert not any("#302" in x for x in v)
    assert len(v) == 2


def test_closed_epic_grandfathered_and_leaf_are_exempt():
    pd = _load()
    gf = pd._PRE_NATIVE_SUBISSUE_EPICS
    epics = [
        {"number": 210, "parent": None, "sub_total": 0, "sub_open": 0},   # grandfathered (< #223) -> exempt
        {"number": 303, "parent": 188, "sub_total": 0, "sub_open": 0},    # itself a sub-issue (leaf) -> exempt
    ]
    assert pd.closed_epic_untracked(epics, gf) == []
    assert 210 in gf and 188 in gf and 223 not in gf   # #223 stays a live positive control


def test_closed_epic_tracked_registered_fail_closed_repo_no_heal():
    pd = _load()
    c = {x.name: x for x in pd.registry()}["closed-epic-tracked"]
    assert c.warn is False and c.group == "repo" and c.heal is None


def test_upstream_resolves_unstamped_is_flagged():
    pd = _load()
    data = {
        "prs": [
            {"number": 1, "body": "fix\n\nResolves upstream: ironclad#5"},   # #5 open, no resolved -> flag
            {"number": 2, "body": "Resolves upstream: ironclad#6"},          # #6 resolved -> fine
            {"number": 3, "body": "Resolves upstream: ironclad#7"},          # #7 closed -> fine
            {"number": 4, "body": "no upstream reference here"},             # no ref -> ignored
            {"number": 5, "body": "Resolves upstream: ironclad#999"},        # not in fetched set -> can't assert
        ],
        "public": [
            {"number": 5, "state": "OPEN", "labels": ["triaged"]},
            {"number": 6, "state": "OPEN", "labels": ["triaged", "resolved"]},
            {"number": 7, "state": "CLOSED", "labels": ["resolved", "released"]},
        ],
    }
    v = pd.upstream_resolves_delivered(data)
    assert len(v) == 1 and "#1" in v[0] and "ironclad#5" in v[0]


def test_upstream_resolves_delivered_registered_warn_upstream_flag_only():
    pd = _load()
    c = {x.name: x for x in pd.registry()}["upstream-resolves-delivered"]
    assert c.warn is True and c.group == "upstream" and c.heal is None   # flag-only: no public-write heal


# ── epic-form C2 parity (F-B-02, epic #244 wave-3 C2) ─────────────────────────
def test_epic_form_c2_parity_flags_a_missing_gate():
    pd = _load()
    full = ("id: completion\n    value: |\n"
            "      - [ ] **Complete**\n      - [ ] **End-to-end runnable**\n"
            "      - [ ] **Feature coverage met**\n      - [ ] **Tests current**\n"
            "      - [ ] **Docs public-grade**\n      - [ ] **Prune-on-close**\n"
            "      - [ ] **Release (public)**\n      - [ ] **Post-publish verified**\n")
    assert pd.epic_form_c2_parity(full) == []                       # all 8 gates present
    v = pd.epic_form_c2_parity(full.replace("      - [ ] **Prune-on-close**\n", ""))
    assert len(v) == 1 and "Prune-on-close" in v[0]                 # missing box flagged


def test_epic_form_c2_parity_registered_fail_closed():
    pd = _load()
    c = {x.name: x for x in pd.registry()}["epic-form-c2-parity"]
    assert c.warn is False and c.group == "repo" and c.heal is None


def test_real_epic_form_carries_all_c2_gates():
    pd = _load()
    txt = (_REPO / ".github" / "ISSUE_TEMPLATE" / "epic.yml").read_text(encoding="utf-8")
    assert pd.epic_form_c2_parity(txt) == []                        # the shipped form is complete


# ── upstream released-but-open reconciler (F-F-03, epic #244 wave-3 C4) ───────
def test_upstream_released_but_still_open_is_flagged():
    pd = _load()
    issues = [
        {"number": 8, "state": "OPEN", "labels": ["released", "resolved"]},   # stuck: delivered, not closed
        {"number": 9, "state": "CLOSED", "labels": ["released"]},             # closed -> fine
        {"number": 10, "state": "OPEN", "labels": ["resolved"]},              # pending, no released -> fine
    ]
    v = pd.upstream_released_not_closed(issues)
    assert len(v) == 1 and "#8" in v[0]                                       # only the OPEN+released one


def test_upstream_released_not_closed_registered_warn_flag_only():
    pd = _load()
    c = {x.name: x for x in pd.registry()}["upstream-released-not-closed"]
    assert c.warn is True and c.group == "upstream" and c.heal is None        # flag-only: no public-write


def test_devloop_merge_evidence_registered_and_inert_without_key(monkeypatch):
    # epic #262 S8: the marker reconciler is wired in, fail-closed (warn=False) once active, and
    # INERT before GX10_DEVLOOP_MARKER_KEY is set — making NO GitHub calls (offline-safe).
    pd = _load()
    c = {x.name: x for x in pd.registry()}["devloop-merge-has-evidence"]
    assert c.warn is False and c.group == "repo"
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)
    data = c.fetch(None)
    assert data["active"] is False and c.assert_fn(data) == []                 # inert => no violations


# ── scheduled-workflow liveness invariant (#303): the watcher is watched ──────
def test_scheduled_workflow_crons_finds_only_cron_workflows():
    pd = _load()
    texts = {
        "reconcile.yml": "on:\n  schedule:\n    - cron: \"23 5 * * *\"\n  workflow_dispatch:\n",
        "ci.yml": "on:\n  pull_request:\n    paths: [core/**]\n",                 # event-only => not scheduled
        "mirror.yml": "on:\n  schedule:\n    - cron: '*/15 * * * *'\n",
    }
    out = pd.scheduled_workflow_crons(texts)
    assert set(out) == {"reconcile.yml", "mirror.yml"}                            # ci.yml excluded
    assert out["reconcile.yml"] == ["23 5 * * *"] and out["mirror.yml"] == ["*/15 * * * *"]


def test_scheduled_liveness_flags_a_workflow_with_only_failures():
    # the reconcile.yml-dead class: runs exist but none succeeded => silently broken.
    pd = _load()
    data = {"dead.yml": [{"status": "completed", "conclusion": "failure"},
                         {"status": "completed", "conclusion": "failure"}]}
    out = pd.scheduled_workflow_liveness(data)
    assert len(out) == 1 and "dead.yml" in out[0] and "silently broken" in out[0]


def test_scheduled_liveness_passes_when_any_recent_run_succeeded():
    pd = _load()
    data = {"ok.yml": [{"status": "completed", "conclusion": "failure"},        # latest flaked...
                       {"status": "completed", "conclusion": "success"}]}        # ...but a recent success exists
    assert pd.scheduled_workflow_liveness(data) == []


def test_scheduled_liveness_treats_skipped_as_healthy():
    # a secret-gated scheduled job that conditionally skips is alive, not broken.
    pd = _load()
    assert pd.scheduled_workflow_liveness({"gated.yml": [{"status": "completed", "conclusion": "skipped"}]}) == []


def test_scheduled_liveness_ignores_never_run_and_in_progress_only():
    # no completed run yet (brand-new / never fired) or only in-flight => cannot prove broken, no flag.
    pd = _load()
    assert pd.scheduled_workflow_liveness({"new.yml": []}) == []
    assert pd.scheduled_workflow_liveness({"running.yml": [{"status": "in_progress", "conclusion": None}]}) == []


def test_scheduled_liveness_registered_fail_closed_repo_group():
    pd = _load()
    c = {x.name: x for x in pd.registry()}["scheduled-workflow-liveness"]
    assert c.warn is False and c.group == "repo" and c.heal is None             # fail-closed, assert-only


# ── no-ungated-autonomous-launcher (epic #312 S4, ADR-0002 D7) ──
def test_no_ungated_launcher_flags_an_enabled_default():
    pd = _load()
    assert pd.no_ungated_autonomous_launcher({"text": "AUTOPILOT_ENABLED = True\nAUTOPILOT_AUTOPLAN = False\n"})
    out = pd.no_ungated_autonomous_launcher({"text": "AUTOPILOT_ENABLED = True\n"})
    assert any("AUTOPILOT_ENABLED" in x and "not False" in x for x in out)


def test_no_ungated_launcher_passes_off_by_default_and_is_inert_without_gx10():
    pd = _load()
    assert pd.no_ungated_autonomous_launcher({"text": "AUTOPILOT_ENABLED = False\nAUTOPILOT_AUTOPLAN = False\n"}) == []
    assert pd.no_ungated_autonomous_launcher({"text": ""}) == []                # gx10 absent => inert
    c = {x.name: x for x in pd.registry()}["no-ungated-autonomous-launcher"]
    assert c.warn is False and c.group == "repo" and c.heal is None             # fail-closed, assert-only


# ── devloop-no-stdlib-shadow (epic #337 — the select.py→stdlib-select sys.path[0] hijack) ──
def test_no_stdlib_shadow_flags_a_stdlib_collision():
    # the original regression: scripts/devloop/select.py shadowed stdlib `select`, crashing
    # subprocess→selectors→select when marker.py ran as a script (sys.path[0] = scripts/devloop).
    pd = _load()
    out = pd.no_stdlib_shadow({"stems": ["driver", "select", "marker"]})
    assert len(out) == 1 and "select" in out[0] and "shadows the stdlib module" in out[0]


def test_no_stdlib_shadow_passes_clean_and_is_inert_when_empty():
    pd = _load()
    assert pd.no_stdlib_shadow({"stems": ["driver", "selection", "marker", "guards"]}) == []
    assert pd.no_stdlib_shadow({"stems": []}) == []                             # no dir => inert


def test_no_stdlib_shadow_real_tree_is_clean_and_registered_fail_closed():
    # the live invariant over the actual scripts/devloop tree must be clean (no module shadows stdlib).
    pd = _load()
    assert pd.no_stdlib_shadow(pd._fetch_devloop_modules()) == []
    c = {x.name: x for x in pd.registry()}["devloop-no-stdlib-shadow"]
    assert c.warn is False and c.group == "repo" and c.heal is None             # fail-closed, assert-only


def test_fetch_devloop_evidence_active_assembles_real_merges_not_empty(monkeypatch, tmp_path):
    # #348 S9: the merges:[] vacuous-green foot-gun is closed — when K is set, _fetch_devloop_evidence
    # assembles the real DELIVERY merges from the ledger via the merge-walk (a markerless one is flagged).
    pd = _load()
    monkeypatch.setenv("GX10_DEVLOOP_MARKER_KEY", "K")
    monkeypatch.setenv("GX10_DEVLOOP_HWM_FILE", str(tmp_path / "nohwm"))     # HWM -> 0 (no grandfathering)

    class _FakeLedger:
        @staticmethod
        def read_all(_p):
            return [{"seq": 0, "payload": {"surface": "DELIVER", "status": "delivered", "sha": "s1",
                                            "tree_sha": "t1", "gate_results": {}, "marker": None}}]

        @staticmethod
        def verify_chain(_p):
            return []                                                    # intact chain

    monkeypatch.setattr(pd, "_devloop_ledger", lambda: _FakeLedger)
    d = pd._fetch_devloop_evidence()
    assert d["active"] is True and len(d["merges"]) == 1 and d["merges"][0]["sha"] == "s1"   # NOT merges:[]
    assert d["chain_errors"] == []
    assert pd.devloop_merge_has_evidence(d)                                   # the markerless delivery is flagged


def test_devloop_evidence_broken_ledger_chain_is_a_hard_violation(monkeypatch, tmp_path):
    # #358 review: the merge-walk trusts seq/marker from the ledger, so a tampered/truncated/reordered
    # chain (which could grandfather-evade or hide a record) must fail closed BEFORE the marker check.
    pd = _load()
    monkeypatch.setenv("GX10_DEVLOOP_MARKER_KEY", "K")
    monkeypatch.setenv("GX10_DEVLOOP_HWM_FILE", str(tmp_path / "nohwm"))

    class _TamperedLedger:
        @staticmethod
        def read_all(_p):
            return []                                                    # truncated to hide the record(s)

        @staticmethod
        def verify_chain(_p):
            return ["record 0: prev_hash break (chain reordered/truncated)"]

    monkeypatch.setattr(pd, "_devloop_ledger", lambda: _TamperedLedger)
    d = pd._fetch_devloop_evidence()
    assert d["chain_errors"]
    violations = pd.devloop_merge_has_evidence(d)
    assert any("ledger chain broken" in v for v in violations)            # green-via-truncation is refused


# ── Phase-2b cross-cutting invariants (#360 S10a): pure-logic + wiring, each with its negative ──
def test_marker_key_requires_walk_activation_ordering(monkeypatch):
    pd = _load()
    # the trap: K set while the walk is UNBUILT => violation (active-but-vacuously-green)
    assert pd.marker_key_requires_walk({"key_set": True, "walk_built": False})
    # safe orderings: K with the walk built, or no K
    assert pd.marker_key_requires_walk({"key_set": True, "walk_built": True}) == []
    assert pd.marker_key_requires_walk({"key_set": False, "walk_built": False}) == []
    # wiring: today K is unset + MERGE_WALK_BUILT True => fetch is non-violating
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)
    assert pd.marker_key_requires_walk(pd._fetch_marker_key_walk()) == []


def test_deliver_dial_stays_supervised_rejects_auto(monkeypatch):
    pd = _load()
    assert pd.deliver_dial_stays_supervised({"auto_refused": False})       # a regressed hard-force is flagged
    assert pd.deliver_dial_stays_supervised({"auto_refused": True}) == []
    # wiring: the REAL probe authorizes nothing under {DELIVER:auto} + no GO
    monkeypatch.delenv("GX10_DEVLOOP_GO_SECRET", raising=False)
    assert pd._fetch_deliver_dial_probe()["auto_refused"] is True
    assert pd.deliver_dial_stays_supervised(pd._fetch_deliver_dial_probe()) == []


def test_required_checks_ssot_protected(monkeypatch):
    pd = _load()
    assert pd.required_checks_ssot_protected({"probes": {".github/required-status-checks.yml": False}})
    assert pd.required_checks_ssot_protected({"probes": {"a": True, "b": True}}) == []
    # wiring: the real coupling protected-class blocks every delivery-integrity SSOT path
    probes = pd._fetch_required_checks_protected()["probes"]
    assert probes and all(probes.values())
    assert pd.required_checks_ssot_protected({"probes": probes}) == []


def test_delivery_pending_unresolved(monkeypatch):
    pd = _load()
    pend = [{"payload": {"surface": "DELIVER", "status": "delivered-unrecorded", "sha": "s1"}}]
    assert pd.delivery_pending_unresolved(pend)                            # shipped-but-pending, never resolved
    resolved = pend + [{"payload": {"surface": "DELIVER", "status": "delivered", "sha": "s1"}}]
    assert pd.delivery_pending_unresolved(resolved) == []                  # a later delivered record resolves it
    assert pd.delivery_pending_unresolved([]) == []                        # inert until pending records exist


def test_devloop_ledger_chain_intact_is_always_on(monkeypatch):
    pd = _load()
    assert pd.devloop_ledger_chain_intact(["record 2: hash mismatch (payload tampered)"])
    assert pd.devloop_ledger_chain_intact([]) == []
    # wiring: not K-gated — a missing/empty ledger yields no errors (inert), a real chain is verified
    assert pd.devloop_ledger_chain_intact(pd._fetch_devloop_ledger_chain()) == []
