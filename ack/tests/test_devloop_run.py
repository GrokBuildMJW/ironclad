"""Runnable dev-loop engine CLI (epic #262 follow-up #294), offline.

Lives in `scripts/devloop/` (private) -> skips in an installed/clean-room tree. Pins that `--status`
flips INERT->LIVE when both seams are set, that a unit fares through the driver to the MERGE
human-stop with the dry-run fake agent AND via a real injected `--agent` command, and that a unit
whose agent omits the test is BLOCKED.
"""
from __future__ import annotations

import importlib.util
import os
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
    # #397 S14c: mirror the real repo — .devloop/ (engine runtime state) is gitignored so the ledger/lock it
    # writes never makes the delivery tree "dirty" (the GO-binding pre-flight requires a clean base_ref tip).
    (root / ".gitignore").write_text(".devloop/\n", encoding="utf-8")
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


def test_status_inert_then_danger_then_live(monkeypatch):
    # #348 S15/S9: INERT (no seams) -> DANGER (K set but the merge-walk UNBUILT = vacuous green) -> LIVE
    # (both seams AND the merge-walk built). Since S9 the walk IS built (MERGE_WALK_BUILT default True), so
    # the DANGER state is reproduced by forcing it back to False (the activation-ordering trap guard).
    r = _load()
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)
    monkeypatch.delenv("GX10_DEVLOOP_GO_SECRET", raising=False)
    assert "engine is INERT" in r.status()
    monkeypatch.setenv("GX10_DEVLOOP_MARKER_KEY", "k")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "g")
    monkeypatch.setattr(r.marker, "MERGE_WALK_BUILT", False)       # the pre-S9 / regressed trap
    s = r.status()
    assert "engine is DANGER" in s and "DANGER: K is SET" in s and "engine is LIVE" not in s
    monkeypatch.setattr(r.marker, "MERGE_WALK_BUILT", True)        # S9: the merge-walk + stamper exist
    assert "engine is LIVE" in r.status()


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


# ── single-driver + autopilot reconciliation (#312 S4, ADR-0002 D7) ──
def test_run_unit_refuses_when_autopilot_enabled(tmp_path, monkeypatch):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    monkeypatch.setenv("AUTOPILOT_ENABLED", "1")          # a second steering authority is enabled
    out = r.run_unit(str(repo), 325, "feat/devloop-run-325", None)
    assert out.status == "refused" and out.guard == "autopilot-reconciliation" and out.state != "MERGE"


def test_run_unit_refuses_a_second_driver(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    held = r.lock.acquire(Path(repo) / ".devloop" / "driver.lock")   # a "first driver" already holds it
    try:
        out = r.run_unit(str(repo), 325, "feat/devloop-run-325", None)
        assert out.status == "refused" and out.guard == "single-driver-lock"
    finally:
        r.lock.release(held)


# ── economics ABORT + resume idempotency (#312 S5) ──
def test_run_unit_skips_an_already_merged_unit(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    out = r.run_unit(str(repo), 326, "feat/devloop-run-326", None, merged_issues={326})
    assert out.status == "skipped" and out.guard == "already-merged"      # not re-driven


def test_run_unit_aborts_on_poison_cap_or_over_budget(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    out = r.run_unit(str(repo), 326, "feat/devloop-run-326", None, attempt=3, cap=3)
    assert out.status == "aborted" and out.guard == "economics"          # poison-capped -> ABORT, no paid retry


# ── real composed gate wiring + D4 credential boundary (#312 S1/S5/S11 — the C2 entry point) ──
def test_run_unit_passes_target_for_the_real_composed_gate(tmp_path, monkeypatch):
    # without a target run.py used the sys.exit(0) no-op gate; with one it must compose the REAL gate
    # from the integrity-pinned base. Stub build_real_ops/Driver to assert the target + base are threaded.
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    captured = {}

    def fake_build(repo_root, wt, runner, *, target=None, base_path=None):
        captured["target"] = target
        return object()

    class _Drv:
        def __init__(self, ops):
            pass

        def run(self, unit, argv):
            captured["base"] = unit.base
            return r.driver.Outcome("MERGE", "stopped-at-human-gate", None, [], [], True)

    monkeypatch.setattr(r.e2e, "build_real_ops", fake_build)
    monkeypatch.setattr(r.e2e, "Driver", _Drv)
    tgt = r.spec.TARGETS["core-monorepo"]
    out = r.run_unit(str(repo), 312, "feat/devloop-run-312", None, target=tgt)
    assert captured["target"] is tgt                                     # the REAL gate, not the no-op stub
    assert captured["base"] == "main"                                    # base taken from the descriptor
    assert out.state == "MERGE"


def test_agent_runs_with_a_scrubbed_env_no_tokens(monkeypatch):
    # D4: the coder-agent subprocess must never inherit GH/UPSTREAM/PROJECTS tokens or the marker key.
    r = _load()
    monkeypatch.setenv("GH_TOKEN", "secret-xyz")
    monkeypatch.setenv("UPSTREAM_TOKEN", "secret-abc")
    monkeypatch.setenv("GX10_DEVLOOP_MARKER_KEY", "k-secret")
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(kw)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(r.subprocess, "run", fake_run)
    runner = r._agent_runner("claude --print", r.credentials.scrub_agent_env(os.environ))

    class _H:
        path = "."
    rc, _out = runner(_H(), ["agent"])
    env = captured["env"]
    assert rc == 0
    assert "GH_TOKEN" not in env and "UPSTREAM_TOKEN" not in env and "GX10_DEVLOOP_MARKER_KEY" not in env
    assert r.credentials.leaked_secrets(env) == []                      # audited clean


def test_run_unit_tool_fences_a_merge_or_push_agent_command(tmp_path):
    # the agent produces, it never delivers — a command carrying a push/merge verb is refused up front.
    r = _load()
    out = r.run_unit(str(tmp_path / "norepo"), 312, "feat/devloop-run-312",
                     "claude --print ; git push origin main")
    assert out.status == "refused" and out.guard == "tool-fence" and out.state != "MERGE"


def test_main_rejects_an_unknown_target():
    r = _load()
    rc = r.main(["--run", "--repo", ".", "--issue", "1", "--branch", "feat/x-y-1", "--target", "nope"])
    assert rc == 2                                                       # fail-closed on an unknown descriptor


# ── #348 S2 credential-lane hardening (refuse_to_start + leaked_secrets wired; hardened agent env) ──
def _stub_driver(r, monkeypatch, outcome=None):
    """Stub build_real_ops + Driver so a unit reaches the (stubbed) driver without running the real gate."""
    monkeypatch.setattr(r.e2e, "build_real_ops", lambda *a, **k: object())
    out = outcome or r.driver.Outcome("MERGE", "stopped-at-human-gate", None, [], [], True)

    class _Drv:
        def __init__(self, ops):
            pass

        def run(self, unit, argv):
            return out

    monkeypatch.setattr(r.e2e, "Driver", _Drv)


def test_run_unit_refuses_an_over_scoped_declared_token(tmp_path, monkeypatch):
    # deploy seam: the driver token is declared to reach targets OUTSIDE the unit's target repo -> refuse
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    tgt = r.spec.TARGETS["core-monorepo"]                                # allowed = the target's repo
    monkeypatch.setenv("GX10_DEVLOOP_TOKEN_TARGETS",
                       f"{tgt['repo']},GrokBuildMJW/ironclad,pypi")
    out = r.run_unit(str(repo), 350, "feat/devloop-cred-harden-350", None, target=tgt)
    assert out.status == "refused" and out.guard == "credential-scope" and out.state != "MERGE"
    assert any("ironclad" in x for x in out.reasons)


def test_run_unit_credential_scope_passes_when_declared_scope_matches(tmp_path, monkeypatch):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    tgt = r.spec.TARGETS["core-monorepo"]
    monkeypatch.setenv("GX10_DEVLOOP_TOKEN_TARGETS", str(tgt["repo"]))   # exactly the target repo -> OK
    _stub_driver(r, monkeypatch)
    out = r.run_unit(str(repo), 350, "feat/devloop-cred-harden-350", None, target=tgt)
    assert out.status == "stopped-at-human-gate"                         # scope gate did NOT refuse


def test_run_unit_scope_gate_inert_when_undeclared(tmp_path, monkeypatch):
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    tgt = r.spec.TARGETS["core-monorepo"]
    monkeypatch.delenv("GX10_DEVLOOP_TOKEN_TARGETS", raising=False)      # undeclared -> inert (Phase-2b)
    _stub_driver(r, monkeypatch)
    out = r.run_unit(str(repo), 350, "feat/devloop-cred-harden-350", None, target=tgt)
    assert out.status == "stopped-at-human-gate"


def test_run_unit_hands_agent_a_hardened_env(tmp_path, monkeypatch):
    # the production path must give the agent the HARDENED env: secrets scrubbed AND the git/gh push
    # credential discovery redirected (GIT_CONFIG_NOSYSTEM etc.), with the leak audit clean.
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    monkeypatch.setenv("GH_TOKEN", "secret-xyz")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "go-secret")
    captured = {}

    def fake_runner(agent_cmd, env):
        captured["env"] = env
        return lambda handle, argv: (0, "ok")

    monkeypatch.setattr(r, "_agent_runner", fake_runner)
    _stub_driver(r, monkeypatch)
    r.run_unit(str(repo), 350, "feat/devloop-cred-harden-350", f'{sys.executable} -c "pass"')
    env = captured["env"]
    assert "GH_TOKEN" not in env and "GX10_DEVLOOP_GO_SECRET" not in env
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1" and env.get("GIT_TERMINAL_PROMPT") == "0"
    assert r.credentials.leaked_secrets(env, ignore=r.credentials.HARDENING_KEYS) == []


def test_run_unit_fails_closed_if_a_secret_survives_hardening(tmp_path, monkeypatch):
    # a hardening regression that leaves a token in the agent env must REFUSE, never launch the agent.
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    monkeypatch.setattr(r.credentials, "harden_agent_env",
                        lambda env, scratch: {"GH_TOKEN": "leak", "PATH": "/x"})
    out = r.run_unit(str(repo), 350, "feat/devloop-cred-harden-350", f'{sys.executable} -c "pass"')
    assert out.status == "refused" and out.guard == "credential-leak"
    assert any("GH_TOKEN" in x for x in out.reasons)


# ── #348 S8: merged_issues from gh + the MERGE->DELIVER CLI (parks without a GO) ──
def test_merged_issues_from_gh_parses_closing_refs(monkeypatch, tmp_path):
    r = _load()

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = ('[{"closingIssuesReferences":[{"number":350},{"number":351}]},'
                      '{"closingIssuesReferences":[{"number":350}]}]')
            stderr = ""
        return P()

    monkeypatch.setattr(r.subprocess, "run", fake_run)
    assert r.merged_issues_from_gh(str(tmp_path)) == [350, 351]      # deduped, sorted

    def fail_run(cmd, **kw):
        class P:
            returncode = 1
            stdout = ""
            stderr = "no gh"
        return P()

    monkeypatch.setattr(r.subprocess, "run", fail_run)
    assert r.merged_issues_from_gh(str(tmp_path)) == []              # best-effort: failure -> []


def test_pyproject_version_reads_core(tmp_path):
    r = _load()
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "pyproject.toml").write_text('version = "0.0.16"', encoding="utf-8")
    assert r._pyproject_version(str(tmp_path)) == "0.0.16"
    assert r._pyproject_version(str(tmp_path / "nope")) == ""


def test_deliver_cli_parks_without_a_go(monkeypatch, tmp_path):
    # --deliver builds the leg but PARKS without a GO (never auto-pushes). A clean main checkout + the
    # bound args pass the pre-flight; the stubbed assembly avoids any real git/gh/build; the park is safe.
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)                       # HEAD == main, clean
    fake = r.deliver.DeliverOps(
        stage_base=lambda: "B",
        deliver_gate=lambda b: r.e2e.guards.GuardResult("deliver", True),
        authorize=lambda: (False, "awaiting operator GO"),
        execute=lambda a, w: (True, []),
        dispose=lambda b: None,
    )
    monkeypatch.setattr(r.e2e, "build_real_deliver_ops", lambda *a, **k: fake)
    # #397 S14c: target test-pypi (the proof target, no production-first guard); the descriptor supplies the
    # release_repo, so no --release-repo is passed.
    rc = r.main(["--deliver", "--target", "test-pypi", "--repo", str(repo), "--tag", "v9.9.9", "--issue", "357"])
    assert rc == 0                                                   # parked = ok, nothing pushed


def test_deliver_cli_preflight_refuses_misbound_before_consuming_a_go(tmp_path):
    # #348 S8 review fix: fail closed BEFORE a GO is consumed when the invocation is mis-bound. Uses test-pypi
    # (the descriptor supplies release_repo + no production-first guard), so the preflight legs are exercised
    # directly (#397 S14c).
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)
    assert r.main(["--deliver", "--target", "nope"]) == 2            # unknown target
    # missing --tag / --issue -> preflight refused (return 2), no leg built
    assert r.main(["--deliver", "--target", "test-pypi", "--repo", str(repo), "--tag", "v1"]) == 2   # no --issue
    # a dirty working tree -> refused (the GO would bind a tree that differs from the pushed base_ref)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    assert r.main(["--deliver", "--target", "test-pypi", "--repo", str(repo), "--tag", "v1",
                   "--issue", "357"]) == 2                           # dirty tree


# ── #395 S14a: the operator GO-mint seam (run.py --mint-go) ──
def _init_repo_with_pyproject(root: Path, version: str = "0.0.16"):
    _init_repo(root)
    (root / "core").mkdir()
    (root / "core" / "pyproject.toml").write_text(f'version = "{version}"\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "pyproject"], check=True, capture_output=True)


def test_mint_go_binds_index_and_cut_and_hides_secret(tmp_path, monkeypatch):
    # the minted GO is bound to THIS cut (HEAD tree + version + target index + operator + unit), is usable
    # for that cut, is REJECTED for the production index (blocker D1-1), and the secret never appears.
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "the-secret")
    rc, msg = r.mint_go(str(repo), "test-pypi", "v0.0.16", "alice", 364)
    assert rc == 0
    assert "the-secret" not in msg                                   # NEVER prints the secret
    tree = r._git_head_tree(str(repo)); secret = b"the-secret"
    expected = r.dial.compute_go(364, "DELIVER", "alice", secret, tree_sha=tree, version="0.0.16",
                                 release_index="testpypi")
    assert expected in msg                                           # the printed GO is the testpypi-bound token
    # round-trip: the minted GO authorizes the SAME (testpypi) cut ...
    ok, _ = r.deliver.authorize_delivery(go=expected, unit=364, operator="alice", secret=secret,
                                         tree_sha=tree, version="0.0.16", release_index="testpypi",
                                         ledger_path=tmp_path / "ok.jsonl")
    assert ok
    # ... but is REJECTED for the production (pypi) index — Test-PyPI FIRST is engine-enforced
    lp2 = tmp_path / "wrong.jsonl"
    no, _ = r.deliver.authorize_delivery(go=expected, unit=364, operator="alice", secret=secret,
                                         tree_sha=tree, version="0.0.16", release_index="pypi", ledger_path=lp2)
    assert not no and r.deliver.ledger.read_all(lp2) == []


def test_mint_go_rejected_for_a_different_unit_or_operator(tmp_path, monkeypatch):
    # int round-trip fidelity (unit N accepted, N+1 rejected) + operator binding — nothing consumed on a miss
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "s")
    tree = r._git_head_tree(str(repo)); secret = b"s"
    go = r.dial.compute_go(364, "DELIVER", "alice", secret, tree_sha=tree, version="0.0.16", release_index="testpypi")
    lp = tmp_path / "l.jsonl"
    base = dict(operator="alice", secret=secret, tree_sha=tree, version="0.0.16", release_index="testpypi", ledger_path=lp)
    assert r.deliver.authorize_delivery(go=go, unit=364, **base)[0]                        # the bound unit -> ok
    assert not r.deliver.authorize_delivery(go=go, unit=365, **{**base, "ledger_path": tmp_path / "a"})[0]   # N+1
    assert not r.deliver.authorize_delivery(go=go, unit=364, operator="mallory", secret=secret, tree_sha=tree,
                                            version="0.0.16", release_index="testpypi",
                                            ledger_path=tmp_path / "b")[0]                  # wrong operator


def test_mint_go_refuses_fail_closed_at_mint_time(tmp_path, monkeypatch):
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    # no secret -> refuse (mint never produces an unverifiable token)
    monkeypatch.delenv("GX10_DEVLOOP_GO_SECRET", raising=False)
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET_FILE", str(tmp_path / "absent"))
    assert r.mint_go(str(repo), "test-pypi", "v1", "alice", 364)[0] == 2
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "s")
    assert r.mint_go(str(repo), "nope", "v1", "alice", 364)[0] == 2                         # unknown target
    assert r.mint_go(str(repo), "test-pypi", "v1", "", 364)[0] == 2                         # missing operator
    assert r.mint_go(str(repo), "test-pypi", "v1", "alice", 0)[0] == 2                      # missing issue
    # dirty tree -> refuse (the GO would bind a tree that differs from the pushed base_ref)
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    rc, why = r.mint_go(str(repo), "test-pypi", "v1", "alice", 364)
    assert rc == 2 and "dirty" in why


def test_mint_go_refuses_empty_version(tmp_path, monkeypatch):
    # no core/pyproject.toml -> empty version -> refuse at mint (mirrors authorize_delivery must-fix #5)
    r = _load()
    repo = tmp_path / "repo"; _init_repo(repo)                       # NO core/pyproject.toml
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "s")
    rc, why = r.mint_go(str(repo), "test-pypi", "v1", "alice", 364)
    assert rc == 2 and "tree_sha/version" in why


def test_main_mint_go_dispatch(tmp_path, monkeypatch):
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    monkeypatch.setenv("GX10_DEVLOOP_GO_SECRET", "s")
    assert r.main(["--mint-go", "--target", "test-pypi", "--repo", str(repo), "--tag", "v0.0.16",
                   "--operator", "alice", "--issue", "364"]) == 0
    assert r.main(["--mint-go", "--target", "test-pypi", "--repo", str(repo), "--tag", "v0.0.16",
                   "--issue", "364"]) == 2                           # missing --operator -> nonzero


# ── #396 S14b: the DELIVER -> DELIVERED completion gate (run.py --complete-delivery) ──
def _write_ledger(repo: Path, payloads):
    import json
    d = repo / ".devloop"; d.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"seq": i, "prev_hash": "x", "payload": p, "hash": "h"}) for i, p in enumerate(payloads)]
    (d / "ledger.jsonl").write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


def _ledger_payloads(repo: Path):
    import json
    f = repo / ".devloop" / "ledger.jsonl"
    return [json.loads(l)["payload"] for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]


_PENDING = {"surface": "DELIVER", "status": "delivered-pending", "sha": "treeX", "tree_sha": "treeX",
            "gate_results": {"clean-room": 0}, "marker": "abc123", "unit": 364}


def test_complete_delivery_green_flips_to_delivered_terminal(tmp_path):
    # green smoke + a trivially-closed round-trip (no upstream refs) -> terminal `delivered`, carrying the
    # marker from the pending stamp record (so merge_walk/published count it). done-means-deployed.
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [_PENDING])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/ironclad-testpypi",
                                  smoke_reader=lambda: "success", body_reader=lambda: "no upstream refs")
    assert rc == 0 and "DELIVERED" in msg
    term = [p for p in _ledger_payloads(repo) if p.get("status") == "delivered"]
    assert term and term[-1]["unit"] == 364 and term[-1]["marker"] == "abc123" and term[-1]["tree_sha"] == "treeX"


def test_complete_delivery_red_smoke_is_a_yank_candidate(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [_PENDING])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                  smoke_reader=lambda: "failure", body_reader=lambda: "")
    assert rc == 1 and "yank" in msg.lower()
    assert not any(p.get("status") == "delivered" for p in _ledger_payloads(repo))     # never terminal on red


def test_complete_delivery_pending_when_smoke_unconcluded(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [_PENDING])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                  smoke_reader=lambda: None, body_reader=lambda: "")
    assert rc == 0 and "PENDING" in msg
    assert not any(p.get("status") == "delivered" for p in _ledger_payloads(repo))


def test_complete_delivery_pending_when_roundtrip_not_closed(tmp_path):
    # smoke green BUT an upstream ref is not released+closed -> fail-closed pending (never premature DELIVERED)
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [_PENDING])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                  smoke_reader=lambda: "success",
                                  body_reader=lambda: "Resolves upstream: ironclad#7",
                                  issue_reader=lambda n: {"labels": [], "state": "OPEN"})
    assert rc == 0 and "PENDING" in msg and not any(p.get("status") == "delivered" for p in _ledger_payloads(repo))


def test_complete_delivery_refuses_without_a_pending_record(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                  smoke_reader=lambda: "success", body_reader=lambda: "")
    assert rc == 2 and "no delivered-pending" in msg


def test_complete_delivery_is_idempotent(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [_PENDING, {**_PENDING, "status": "delivered"}])
    rc, msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                  smoke_reader=lambda: "success", body_reader=lambda: "")
    assert rc == 0 and "already DELIVERED" in msg
    assert len((repo / ".devloop" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()) == 2   # nothing appended


def test_complete_delivery_prefers_the_marker_bearing_stamp_record(tmp_path):
    # a bare lifecycle log record (no marker, later seq) + the marker stamp record both pending -> the terminal
    # record copies the MARKER-bearing stamp so the merge-walk keeps evidence.
    r = _load()
    repo = tmp_path / "repo"
    log_rec = {"surface": "DELIVER", "state": "DELIVER", "status": "delivered-pending", "sha": "treeX",
               "tree_sha": "treeX", "unit": 364, "reasons": ["x"]}     # no marker / gate_results
    _write_ledger(repo, [_PENDING, log_rec])
    rc, _msg = r.complete_delivery(str(repo), "test-pypi", 364, "o/r",
                                   smoke_reader=lambda: "success", body_reader=lambda: "")
    assert rc == 0
    term = [p for p in _ledger_payloads(repo) if p.get("status") == "delivered"][-1]
    assert term["marker"] == "abc123"                                  # copied from the stamp, not the markerless log


def test_main_complete_delivery_dispatch(tmp_path):
    r = _load()
    repo = tmp_path / "repo"; _write_ledger(repo, [])
    assert r.main(["--complete-delivery", "--repo", str(repo), "--target", "test-pypi", "--issue", "0"]) == 2
    assert r.main(["--complete-delivery", "--repo", str(repo), "--target", "nope", "--issue", "364"]) == 2


# ── #397 S14c: Test-PyPI index routing + the Test-PyPI-FIRST machine guard ──
def test_testpypi_proven_keys_on_terminal_testpypi_and_version(tmp_path):
    r = _load()
    proof = [{"payload": {"surface": "DELIVER", "status": "delivered", "release_index": "testpypi", "version": "0.0.16"}}]
    assert r._testpypi_proven(proof, "0.0.16")
    assert not r._testpypi_proven(proof, "0.0.17")                                  # a different version is not proven
    pending = [{"payload": {"surface": "DELIVER", "status": "delivered-pending", "release_index": "testpypi", "version": "0.0.16"}}]
    assert not r._testpypi_proven(pending, "0.0.16")                                # a pending (non-terminal) does not count
    prod = [{"payload": {"surface": "DELIVER", "status": "delivered", "release_index": "pypi", "version": "0.0.16"}}]
    assert not r._testpypi_proven(prod, "0.0.16")                                   # a production record is not a Test-PyPI proof


def test_deliver_production_refused_without_a_testpypi_proof(tmp_path, monkeypatch):
    # the Test-PyPI-FIRST guard: a production (core-monorepo) cut is refused before any push/GO unless the
    # ledger carries a terminal Test-PyPI DELIVERED for THIS version. release_repo comes from the descriptor.
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    monkeypatch.delenv("GX10_DEVLOOP_TOKEN_TARGETS", raising=False)
    rc = r.main(["--deliver", "--target", "core-monorepo", "--repo", str(repo), "--tag", "v0.0.16", "--issue", "364"])
    assert rc == 2                                                                  # Test-PyPI FIRST: not proven


def test_deliver_production_allowed_after_testpypi_proof_parks_without_go(tmp_path, monkeypatch):
    # with the proof present the guard passes; without a GO the leg PARKS (no push). Stub the assembly.
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    _write_ledger(repo, [{"surface": "DELIVER", "status": "delivered", "release_index": "testpypi",
                          "version": "0.0.16", "unit": 364}])
    fake = r.deliver.DeliverOps(
        stage_base=lambda: "B", deliver_gate=lambda b: r.e2e.guards.GuardResult("deliver", True),
        authorize=lambda: (False, "awaiting operator GO"), execute=lambda a, w: (True, []), dispose=lambda b: None)
    monkeypatch.setattr(r.e2e, "build_real_deliver_ops", lambda *a, **k: fake)
    monkeypatch.delenv("GX10_DEVLOOP_TOKEN_TARGETS", raising=False)
    rc = r.main(["--deliver", "--target", "core-monorepo", "--repo", str(repo), "--tag", "v0.0.16", "--issue", "364"])
    assert rc == 0                                                                  # guard passed; parked awaiting GO


def test_deliver_refuses_a_release_repo_conflicting_with_the_descriptor(tmp_path):
    # #397 S14c: the descriptor's release_repo is authoritative — a conflicting operator --release-repo refuses.
    r = _load()
    repo = tmp_path / "repo"; _init_repo_with_pyproject(repo, "0.0.16")
    rc = r.main(["--deliver", "--target", "test-pypi", "--repo", str(repo), "--tag", "v0.0.16",
                 "--issue", "364", "--release-repo", "attacker/evil"])
    assert rc == 2


def test_bash_exe_resolves_full_path_skipping_the_windows_wsl_shim(monkeypatch):
    # #409: on Windows a bare 'bash' resolves via CreateProcess to System32\bash.exe (the WSL launcher) before
    # the PATH's Git Bash -> resolve a full path that skips the WSL/WindowsApps shims; POSIX keeps bare 'bash';
    # GX10_DEVLOOP_BASH overrides.
    import shutil
    r = _load()
    monkeypatch.setenv("GX10_DEVLOOP_BASH", "/custom/bash")
    assert r._bash_exe() == "/custom/bash"                                   # explicit override wins
    monkeypatch.delenv("GX10_DEVLOOP_BASH", raising=False)
    monkeypatch.setattr(r.os, "name", "posix")
    assert r._bash_exe() == "bash"                                          # POSIX: a bare bash is correct
    monkeypatch.setattr(r.os, "name", "nt")
    monkeypatch.setattr(shutil, "which", lambda _x: r"C:\Windows\System32\bash.exe")    # the WSL shim
    monkeypatch.setattr(r.os.path, "isfile", lambda p: p == r"C:\Program Files\Git\usr\bin\bash.exe")
    assert r._bash_exe() == r"C:\Program Files\Git\usr\bin\bash.exe"        # skip System32 -> Git Bash


def test_activate_marker_seam_sets_grandfather_hwm_and_never_sets_k(tmp_path, monkeypatch):
    # #359 S10: the operator K-activation seam sets the grandfather high-water-mark (= ledger length) and
    # prints the final step, but NEVER sets K; a premature K (walk unbuilt) is refused by the interlock.
    import json
    r = _load()
    repo = tmp_path / "repo"
    (repo / ".devloop").mkdir(parents=True)
    lines = [json.dumps({"seq": i, "prev_hash": "x", "payload": {"surface": "DRIVER"}, "hash": "h"})
             for i in range(3)]
    (repo / ".devloop" / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("GX10_DEVLOOP_HWM_FILE", str(tmp_path / "hwm"))
    monkeypatch.delenv("GX10_DEVLOOP_MARKER_KEY", raising=False)

    out = r.activate_marker(str(repo))
    assert "PASSED" in out and "ledger seq 3" in out
    assert r.marker.read_high_water_mark() == 3                       # grandfather boundary = ledger length
    assert "GX10_DEVLOOP_MARKER_KEY" not in os.environ               # the tool NEVER sets K

    monkeypatch.setattr(r.marker, "MERGE_WALK_BUILT", False)          # interlock: premature K
    assert "REFUSED" in r.activate_marker(str(repo))
