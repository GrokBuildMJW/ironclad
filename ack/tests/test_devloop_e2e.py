"""End-to-end integration of the machine-gated dev-loop (epic #262, S7 / ADR 0002), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Wires the REAL
modules — `driver.Driver` over `DriverOps` built from the real `worktree` (a temp git repo) + the
real `guards` GATE + the real coupling guards — with a FAKE coder-agent. Proves the whole chain:
a unit that produces code + a test drives to the MERGE human-stop (green PR-ready); a unit that
omits its test is BLOCKED fail-closed at IMPLEMENT->GATE. (The LIVE run with a real claude agent +
engine is the operator's C2 confirmation; this is the automatable proof.)
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_E2E = _REPO / "scripts" / "devloop" / "e2e.py"

pytestmark = pytest.mark.skipif(
    not _E2E.is_file(),
    reason="private dev-loop e2e (scripts/devloop/e2e.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_e2e", _E2E)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _init_repo(root: Path):
    root.mkdir(parents=True)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True, capture_output=True)
    (root / "seed").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "seed"], check=True, capture_output=True)


def _run(e2e, tmp_path, *, write_test: bool):
    repo = tmp_path / "repo"; _init_repo(repo)

    def agent(handle, argv):
        wt = Path(handle.path)
        (wt / "src.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        if write_test:
            (wt / "tests").mkdir(exist_ok=True)
            (wt / "tests" / "test_src.py").write_text("def test_f():\n    assert True\n", encoding="utf-8")
        return (0, "ok")

    ops = e2e.build_real_ops(repo, tmp_path / "wt", agent)
    unit = e2e.Unit(issue=269, branch="feat/devloop-e2e-269")
    return e2e.Driver(ops).run(unit, ["agent"])


def test_e2e_code_plus_test_reaches_the_merge_human_stop(tmp_path):
    e2e = _load()
    out = _run(e2e, tmp_path, write_test=True)
    assert out.state == "MERGE" and out.status == "stopped-at-human-gate"
    assert out.worktree_disposed
    assert any(r["dst"] == "MERGE" for r in out.trace)         # drove the whole chain to the gate


def test_e2e_skipping_the_test_is_blocked_at_the_gate(tmp_path):
    e2e = _load()
    out = _run(e2e, tmp_path, write_test=False)
    assert out.status == "halted" and out.state == "IMPLEMENT"  # code-needs-test coupling fired
    assert any("without a test" in r for r in out.reasons)
    assert out.worktree_disposed                                # still cleaned up


# ── real composed gate plan (#312 S5): the core/ gate is NOT the sys.exit(0) stub ──
def test_gate_plan_stages_export_first_then_real_guards():
    e2e = _load()
    target = {"boundary_cmd": "python scripts/ci/check_core_boundary.py",
              "gate_profile": ["boundary", "pytest", "doc-reality-audit", "test-counts", "node-boundary",
                               "english-only", "secret-scan", "deploy-consistency"]}
    plan = e2e.gate_plan(target, "/base")
    names = [n for n, _ in plan]
    assert names[0] == "stage+secret-scan"                       # the export is staged BEFORE the audit
    assert "secret-scan" not in names                            # folded into the stage step
    assert names.index("stage+secret-scan") < names.index("doc-reality-audit")
    assert {"boundary", "pytest", "doc-reality-audit", "test-counts", "node-boundary",
            "deploy-consistency"}.issubset(set(names))
    # every step is a real argv from the base root — never the sys.exit(0) no-op
    assert not any("sys.exit(0)" in " ".join(argv) for _, argv in plan)
    stage_argv = dict(plan)["stage+secret-scan"]
    assert stage_argv[1].replace("\\", "/").endswith("/base/scripts/ci/export_core.py") and "--require-scanner" in stage_argv
