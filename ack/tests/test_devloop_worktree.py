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


def test_pinned_base_has_unit_docs_but_untampered_gate_scripts(tmp_path):
    # NC-1 / ADR-0002 D5 (#312 S2): the integrity-pinned base checkout carries the unit's (non-protected)
    # change AND the BASE's OWN guard scripts — so the gate sees the unit's docs but never the agent copy.
    wt = _load()
    repo = tmp_path / "repo"; repo.mkdir(parents=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(repo)], check=True, capture_output=True, text=True)
    _git(repo, "config", "user.name", "t"); _git(repo, "config", "user.email", "t@local")
    (repo / "scripts" / "ci").mkdir(parents=True); (repo / "core" / "docs").mkdir(parents=True)
    (repo / "scripts" / "ci" / "tool.py").write_text("GATE-V1\n", encoding="utf-8")   # the base's gate script
    (repo / "core" / "docs" / "x.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "init")

    # the agent edits a NON-protected doc in its unit worktree
    h = wt.create_worktree(repo, tmp_path / "wt", "feat/devloop-x-1", base="main")
    (Path(h.path) / "core" / "docs" / "x.md").write_text("v2 (unit edit)\n", encoding="utf-8")
    diff = wt.capture_diff(h)

    base = wt.create_pinned_base(repo, tmp_path / "base", "main", diff)
    # the unit's doc change IS present in the pinned base (so the gate audits the unit's docs)
    assert (Path(base) / "core" / "docs" / "x.md").read_text(encoding="utf-8") == "v2 (unit edit)\n"
    # the gate script is the BASE's OWN, untampered copy (never the agent-mutated tree)
    assert (Path(base) / "scripts" / "ci" / "tool.py").read_text(encoding="utf-8") == "GATE-V1\n"


def test_create_release_base_is_a_clean_no_diff_pinned_base(tmp_path):
    # #348 S4: the DELIVER staging primitive is a CLEAN integrity-pinned checkout with NO unit diff —
    # distinct from create_pinned_base(diff). The release base == base_ref tree exactly (no agent change).
    wt = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    (repo / "core").mkdir(parents=True, exist_ok=True)
    (repo / "core" / "pyproject.toml").write_text("version = '0.0.1'\n", encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "release base")
    base = wt.create_release_base(repo, tmp_path / "rel-base", "main")
    # the base carries the committed tree, with NOTHING extra applied (no unit diff exists per release)
    assert (Path(base) / "core" / "pyproject.toml").read_text(encoding="utf-8") == "version = '0.0.1'\n"
    porcelain = subprocess.run(["git", "-C", base, "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
    assert porcelain == ""                                          # clean — no diff applied
    wt.dispose(repo, wt.WorktreeHandle(path=base, branch="", base="main"))   # disposes like a worktree


def test_engine_git_ops_ignore_a_planted_hook(tmp_path):
    # Default C / ADR-0002 D5 (#312 S2): confinement is blind to .git/, so a unit could plant a hook.
    # The engine disables hooks for its git ops, so a planted pre-commit never fires during apply.
    wt = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    hooks = repo / ".git" / "hooks"; hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (hooks / "pre-commit").chmod(0o755)

    h = wt.create_worktree(repo, tmp_path / "wt", "feat/devloop-x-1", base="main")
    (Path(h.path) / "f.txt").write_text("changed\n", encoding="utf-8")
    # control: a RAW commit (no hooks override) is blocked by the planted hook on this platform...
    subprocess.run(["git", "-C", h.path, "add", "-A"], check=True, capture_output=True, text=True)
    raw = subprocess.run(["git", "-C", h.path, "commit", "-m", "x"], capture_output=True, text=True)
    if raw.returncode == 0:
        pytest.skip("git did not run the planted hook here — isolation test inconclusive on this platform")
    # ...but the engine's apply (hooks disabled, Default C) commits cleanly despite the hook
    sha = wt.apply(h, "unit change")
    assert sha and len(sha) >= 7
