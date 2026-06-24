"""Machine-gated dev-loop process-coupling guards (epic #262, S3 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Each coupling guard
gets its positive AND negative case (ADR-0007): branch format, exactly-one-issue, C0-present,
code=>test co-presence, capability=>docs.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_COUPLING = _REPO / "scripts" / "devloop" / "coupling.py"

pytestmark = pytest.mark.skipif(
    not _COUPLING.is_file(),
    reason="private dev-loop coupling guards (scripts/devloop/coupling.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_coupling", _COUPLING)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_branch_name_valid():
    c = _load()
    assert c.branch_name_valid("feat/devloop-coupling-265")
    assert c.branch_name_valid("fix/mirror-liveness-258")
    assert not c.branch_name_valid("my-random-branch")
    assert not c.branch_name_valid("feat/no-issue-number")


def test_exactly_one_linked_issue():
    c = _load()
    assert c.exactly_one_linked_issue("...\n\nCloses #265")
    none = c.exactly_one_linked_issue("no link here")
    assert not none and "no closing issue" in none.reasons[0]
    two = c.exactly_one_linked_issue("Closes #1 and Fixes #2")
    assert not two and "2 issues" in two.reasons[0]


def test_c0_present_blocks_on_unresolved_fork():
    c = _load()
    assert c.c0_present(["type/task", "area/ci"])
    blocked = c.c0_present(["status/blocked"])
    assert not blocked and "status/blocked" in blocked.reasons[0]
    assert not c.c0_present(["status/needs-decision"])


def test_code_change_requires_test():
    c = _load()
    assert c.code_change_requires_test(["scripts/devloop/coupling.py",
                                        "ack/tests/test_devloop_coupling.py"])     # source + test
    red = c.code_change_requires_test(["scripts/devloop/coupling.py"])                  # source, no test
    assert not red and "without a test" in red.reasons[0]
    assert c.code_change_requires_test(["docs/x.md", "README.md"])                      # no source => pass


def test_capability_change_requires_docs():
    c = _load()
    caps = ["*/ack/sdk.py"]
    docs = ["*.md"]
    red = c.capability_change_requires_docs(["ack/sdk.py"], capability_patterns=caps, doc_patterns=docs)
    assert not red and "without docs" in red.reasons[0]
    assert c.capability_change_requires_docs(["ack/sdk.py", "docs/status.md"],
                                             capability_patterns=caps, doc_patterns=docs)
    assert c.capability_change_requires_docs(["ack/other.py"],                     # no capability touched
                                             capability_patterns=caps, doc_patterns=docs)


def test_branch_matches_issue():
    c = _load()
    assert c.branch_matches_issue("feat/devloop-github-io-324", 324)             # trailing -324 == unit #324
    assert c.branch_matches_issue("fix/mirror-liveness-258", 258)
    bad = c.branch_matches_issue("feat/devloop-github-io-324", 999)              # diff wired onto wrong branch
    assert not bad and "wrong-branch" in bad.reasons[0]
    assert not c.branch_matches_issue("feat/no-number-here", 324)               # no trailing issue number


# ── self-modification protected class (epic #312 S2, ADR-0002 D5) ──
def test_self_mod_protected_passes_an_ordinary_unit():
    c = _load()
    # a normal core/ unit touches nothing protected → propose-and-gate as usual
    assert c.self_mod_protected(["ack/foo.py", "ack/tests/test_foo.py", "docs/status.md"])


def test_self_mod_protected_flags_every_protected_path():
    c = _load()
    for prot in ("scripts/devloop/driver.py", "scripts/devloop/guards.py", "scripts/ci/check_core_boundary.py",
                 "core/.github/workflows/publish.yml", ".github/required-status-checks.yml",
                 ".github/workflows/reconcile.yml", ".github/workflows/devloop-on-merge.yml",
                 ".github/DEV_LOOP.md", ".github/ISSUE_TEMPLATE/epic.yml"):
        r = c.self_mod_protected([prot])
        assert not r.passed, f"{prot} should be protected"
        assert "BLOCKED for out-of-band review" in r.reasons[0]
    # a backslash / leading-./ path normalises the same way
    assert not c.self_mod_protected(["./scripts\\devloop\\marker.py"]).passed
    # a sibling that merely starts with a protected name but is NOT under it is clean
    assert c.self_mod_protected(["scripts/ci_notes.md", "scripts/devloop_readme.md"])


def test_self_mod_protected_public_workflows_are_a_directory_prefix():
    # #348 S3: the PUBLIC delivery-integrity workflows are a DIRECTORY PREFIX, so the existing
    # publish/clean-room/release-close AND a NET-NEW agent-authored OIDC workflow all route to BLOCKED —
    # the residual the three-exact-paths approach would have missed.
    c = _load()
    for wf in ("core/.github/workflows/publish.yml", "core/.github/workflows/clean-room.yml",
               "core/.github/workflows/release-close.yml", "core/.github/workflows/ci.yml",
               "core/.github/workflows/sneaky-oidc-publish.yml"):   # net-new -> still BLOCKED
        assert not c.self_mod_protected([wf]).passed, wf
    # a diff that weakens the branch-protection SSOT is BLOCKED too
    assert not c.self_mod_protected([".github/required-status-checks.yml"]).passed
    # negative: a sibling outside the workflows dir, and an ordinary core change, both pass
    assert c.self_mod_protected(["core/.github/workflows-notes.md"]).passed     # not under the dir
    assert c.self_mod_protected(["ack/server.py", "docs/status.md"]).passed


def test_docs_public_grade_is_two_executable_guards_not_a_label():
    # #312 S6: docs-public-grade == the GATE doc-reality-audit guard + the capability-needs-docs coupling
    # guard (both already wired), NOT the inert dod_profile string.
    c = _load()
    assert c.docs_public_grade_guards() == ["doc-reality-audit", "capability-needs-docs"]
    # the second is this module's real coupling guard (its GuardResult name)
    name = c.capability_change_requires_docs(["x.py"], capability_patterns=[], doc_patterns=[]).name
    assert name == "capability-needs-docs"
