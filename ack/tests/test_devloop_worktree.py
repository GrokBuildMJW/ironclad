"""Git-worktree isolation + Produce!=Apply (epic #262, S5 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Exercises the real
git plumbing against a temp repo: a worktree is isolated outside the live tree, the agent's changes
are captured as a diff, the live tree stays confined, apply commits onto the unit branch, a timeout
is RED (not a hang), dispose is idempotent, and confinement actually flags a dirtied live tree.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_WT = _REPO / "scripts" / "devloop" / "worktree.py"

pytestmark = pytest.mark.skipif(
    not _WT.is_file(),
    reason="private dev-loop worktree (scripts/devloop/worktree.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_worktree", _WT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _init_repo(root: Path):
    root.mkdir(parents=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(root)], check=True, capture_output=True, text=True)
    _git(root, "config", "user.name", "t"); _git(root, "config", "user.email", "t@local")
    (root / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A"); _git(root, "commit", "-m", "init")


def test_full_cycle_isolated_capture_confine_apply_dispose(tmp_path):
    wt = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    wtpath = tmp_path / "wt-unit"

    h = wt.create_worktree(repo, wtpath, "feat/devloop-x-1", base="main")
    assert Path(h.path).is_dir() and h.branch == "feat/devloop-x-1"

    # Produce: the agent edits inside the worktree only
    (Path(h.path) / "f.txt").write_text("hello world\n", encoding="utf-8")
    diff = wt.capture_diff(h)
    assert "hello world" in diff

    # confined: the live tree is untouched
    assert wt.confinement_violations(repo) == []

    # Apply: commit onto the unit branch (the single deterministic mechanism)
    sha = wt.apply(h, "feat: x")
    assert len(sha) >= 7

    # teardown is idempotent
    wt.dispose(repo, h)
    assert not Path(h.path).exists()
    wt.dispose(repo, h)              # again — no error


def test_run_in_worktree_timeout_is_red(tmp_path):
    wt = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    h = wt.create_worktree(repo, tmp_path / "wt-t", "feat/devloop-t-2", base="main")
    rc, out = wt.run_in_worktree(h, [sys.executable, "-c", "print('ok')"], timeout=30)
    assert rc == 0 and "ok" in out
    rc2, out2 = wt.run_in_worktree(h, [sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert rc2 == -1 and "TIMEOUT" in out2          # a hang is RED, fail-closed
    wt.dispose(repo, h)


def test_confinement_flags_a_dirtied_live_tree(tmp_path):
    wt = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    (repo / "leaked.txt").write_text("written outside the worktree\n", encoding="utf-8")
    violations = wt.confinement_violations(repo)
    assert any("leaked.txt" in v for v in violations)   # an out-of-worktree write is caught
