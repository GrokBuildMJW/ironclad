"""Driver resume / idempotency (epic #262, S14 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that unit-state
is re-derived from durable artefacts (no separate drift-prone state file), that effectful steps are
idempotent on resume (a killed-mid-IMPLEMENT driver does not duplicate the branch/PR/merge), and the
forward-only ledger cross-check.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RESUME = _REPO / "scripts" / "devloop" / "resume.py"

pytestmark = pytest.mark.skipif(
    not _RESUME.is_file(),
    reason="private dev-loop resume (scripts/devloop/resume.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_resume", _RESUME)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_derive_state_from_artefacts():
    r = _load()
    assert r.derive_state({}) == "SELECT"
    assert r.derive_state({"branch": True}) == "BRANCH"
    assert r.derive_state({"branch": True, "diff": True}) == "GATE"
    assert r.derive_state({"pr": 290, "ci": "pending"}) == "CI"
    assert r.derive_state({"pr": 290, "ci": "green"}) == "MERGE"
    assert r.derive_state({"merged": True}) == "done"


def test_resume_is_idempotent_no_duplicate_after_mid_implement_kill():
    r = _load()
    # a driver killed mid-IMPLEMENT: the branch + worktree already exist
    after_kill = {"branch": True, "worktree": True}
    assert r.derive_state(after_kill) == "BRANCH"            # resumes here, not from scratch
    assert r.should_skip("create-branch", after_kill)        # do NOT recreate the branch
    assert r.should_skip("create-worktree", after_kill)      # nor the worktree
    assert not r.should_skip("open-pr", after_kill)          # the PR step still runs
    # already merged => merge is skipped (no double-merge)
    assert r.should_skip("merge", {"merged": True})
    assert not r.should_skip("merge", {"pr": 1})


def test_forward_only_ledger_cross_check():
    r = _load()
    assert r.consistent_with_ledger({"pr": 1, "ci": "green", "ledger_last": "GATE"}) == []   # MERGE >= GATE
    behind = r.consistent_with_ledger({"branch": True, "ledger_last": "PR"})                  # BRANCH < PR
    assert behind and "behind ledger" in behind[0]


def test_already_merged_idempotency():
    r = _load()
    assert r.already_merged(322, {322, 323, 325})        # delivered -> do not re-drive
    assert not r.already_merged(326, {322, 323, 325})    # not yet delivered -> steer it
    assert not r.already_merged(326, [])                 # empty / None set is safe


def test_already_merged_vs_already_published_are_split():
    # #348 S8 / deep-review #6: merged != delivered. A merged-but-unpublished unit stays DELIVER-eligible;
    # only already_published is terminal.
    r = _load()
    assert r.already_merged(357, [357, 350]) and not r.already_merged(999, [357])
    assert r.already_published(350, [350]) and not r.already_published(357, [350])
    # the corner the conflation would break: merged True, published False -> still deliverable
    assert r.already_merged(357, [357]) and not r.already_published(357, [350])
