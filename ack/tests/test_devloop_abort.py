"""ABORT / ROLLBACK deterministic teardown (epic #262, S10 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that abort
tears every artefact down, that it is **best-effort** (a failing step does not stop the rest, so an
aborted unit is never left half-cleaned), and that the concrete local-branch delete works on a real
temp repo.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_ABORT = _REPO / "scripts" / "devloop" / "abort.py"

pytestmark = pytest.mark.skipif(
    not _ABORT.is_file(),
    reason="private dev-loop abort (scripts/devloop/abort.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_abort", _ABORT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ops(a, *, fail=None):
    done = {"calls": [], "log": []}

    def step(name):
        def fn(*_a):
            if fail == name:
                raise RuntimeError(f"{name} boom")
            done["calls"].append(name)
        return fn

    ops = a.AbortOps(
        dispose_worktree=step("worktree"),
        delete_local_branch=step("local"),
        delete_remote_branch=step("remote"),
        close_pr=step("pr"),
        unassign_issue=step("unassign"),
        reset_board=step("board"),
        log=lambda rec: done["log"].append(rec),
    )
    return ops, done


def test_abort_tears_every_artefact_down():
    a = _load()
    state = a.AbortState(unit=272, branch="feat/x-272", worktree={"h": 1}, pr=99)
    ops, done = _ops(a)
    res = a.abort_unit(state, ops, reason="gate-exhausted")
    assert res.clean
    assert done["calls"] == ["worktree", "local", "remote", "pr", "unassign", "board"]
    assert done["log"][0]["abort"] == 272 and done["log"][0]["reason"] == "gate-exhausted"


def test_abort_is_best_effort_one_failure_does_not_stop_the_rest():
    a = _load()
    state = a.AbortState(unit=272, branch="feat/x-272", worktree={"h": 1}, pr=99)
    ops, done = _ops(a, fail="pr")                       # closing the PR blows up
    res = a.abort_unit(state, ops, reason="ci-red")
    assert not res.clean and any("pr:" in e for e in res.errors)
    assert "unassign" in done["calls"] and "board" in done["calls"]   # later steps STILL ran


def test_abort_skips_artefacts_that_never_existed():
    a = _load()
    ops, done = _ops(a)
    a.abort_unit(a.AbortState(unit=272), ops, reason="never-branched")   # no branch/worktree/pr
    assert done["calls"] == ["unassign", "board"]        # only issue/board reset


def test_delete_local_branch_real_git(tmp_path):
    a = _load()
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True, capture_output=True)
    (repo / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "i"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "branch", "feat/x-272"], check=True, capture_output=True)
    a.delete_local_branch(repo, "feat/x-272")
    out = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout
    assert "feat/x-272" not in out
