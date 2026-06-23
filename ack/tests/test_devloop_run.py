"""Runnable dev-loop engine CLI (epic #262 follow-up #294), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that `--status`
flips INERT->LIVE when both seams are set, that a unit fares through the driver to the MERGE
human-stop with the dry-run fake agent AND via a real injected `--agent` command, and that a unit
whose agent omits the test is BLOCKED.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RUN = _REPO / "scripts" / "devloop" / "run.py"

pytestmark = pytest.mark.skipif(
    not _RUN.is_file(),
    reason="private dev-loop runner (scripts/devloop/run.py) absent — clean-room tree",
)


def _load():
    spec = importlib.util.spec_from_file_location("_devloop_run", _RUN)
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


def _agent_script(tmp_path: Path, name: str, *, with_test: bool) -> Path:
    body = "import pathlib\npathlib.Path('src.py').write_text('def f():\\n    return 1\\n')\n"
    if with_test:
        body += ("pathlib.Path('tests').mkdir(exist_ok=True)\n"
                 "pathlib.Path('tests/test_src.py').write_text('def test_f():\\n    assert True\\n')\n")
    s = tmp_path / name
    s.write_text(body, encoding="utf-8")
    return s


def test_status_flips_inert_to_live(monkeypatch):
    r = _load()
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)
    monkeypatch.delenv("GX10_DEVLOOP_GO_SECRET", raising=False)
    assert "INERT" in r.status()
    monkeypatch.setenv("GX10_DEVLOOP_MARKER_KEY", "k")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "g")
    assert "LIVE" in r.status()


def test_run_unit_dry_fake_reaches_merge(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    out = r.run_unit(str(repo), 294, "feat/devloop-run-294", None)
    assert out.state == "MERGE" and out.status == "stopped-at-human-gate"


def test_run_unit_real_agent_command_and_skip_test_blocked(tmp_path):
    r = _load()
    repo1 = tmp_path / "r1"; _init_repo(repo1)
    ok = _agent_script(tmp_path, "ok.py", with_test=True)
    out_ok = r.run_unit(str(repo1), 294, "feat/devloop-run-294", f'{sys.executable} "{ok}"')
    assert out_ok.status == "stopped-at-human-gate"               # real --agent path drives to MERGE

    repo2 = tmp_path / "r2"; _init_repo(repo2)
    bad = _agent_script(tmp_path, "bad.py", with_test=False)
    out_bad = r.run_unit(str(repo2), 294, "feat/devloop-run-294", f'{sys.executable} "{bad}"')
    assert out_bad.status == "halted" and out_bad.state == "IMPLEMENT"   # skip-test BLOCKED
